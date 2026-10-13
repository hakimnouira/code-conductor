"""Durable scheduled workflow storage and lifecycle API tests."""
import time

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.task_store import claim_due_schedules, create_schedule, get_incomplete_schedule_runs, get_schedule, list_schedule_runs, save_workflow

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_schedules_are_persistent_pauseable_and_keep_run_history(tmp_path, monkeypatch):
    monkeypatch.setenv("CODE_CONDUCTOR_TASK_DB", str(tmp_path / "schedule-api.sqlite3"))
    headers = {"Authorization": f"Bearer {get_settings().auth_token}"}
    async with AsyncClient(transport=ASGITransport(app=__import__("app.main", fromlist=["app"]).app), base_url="http://test") as client:
        workflow = await client.post("/workflows", headers=headers, json={
            "name": "Nightly check", "prompt": "Run tests and summarize failures", "agent_id": "auto",
            "browser_steps": [
                {"action": "fill", "selector": "input[name='q']", "value": "smoke test"},
                {"action": "click", "selector": "button[type='submit']"},
            ],
        })
        assert workflow.status_code == 200
        workflow_id = workflow.json()["workflow_id"]
        assert workflow.json()["browser_steps"][0]["action"] == "fill"
        assert workflow.json()["browser_steps"][1]["selector"] == "button[type='submit']"

        unauthenticated = await client.get("/schedules")
        assert unauthenticated.status_code == 401

        created = await client.post("/schedules", headers=headers, json={
            "workflow_id": workflow_id, "interval_minutes": 60, "max_retries": 2
        })
        assert created.status_code == 200
        schedule = created.json()
        assert schedule["enabled"] is True
        assert schedule["interval_minutes"] == 60
        assert schedule["max_retries"] == 2
        assert schedule["next_run_at"] > time.time()

        listing = await client.get("/schedules", headers=headers)
        assert any(row["schedule_id"] == schedule["schedule_id"] for row in listing.json())

        paused = await client.put(f"/schedules/{schedule['schedule_id']}", headers=headers, json={"enabled": False})
        assert paused.status_code == 200
        assert paused.json()["enabled"] is False

        resumed = await client.put(f"/schedules/{schedule['schedule_id']}", headers=headers, json={"enabled": True})
        assert resumed.status_code == 200
        assert resumed.json()["enabled"] is True
        assert resumed.json()["next_run_at"] > time.time()

        runs = await client.get(f"/schedules/{schedule['schedule_id']}/runs", headers=headers)
        assert runs.status_code == 200
        assert runs.json() == []

        deleted = await client.delete(f"/schedules/{schedule['schedule_id']}", headers=headers)
        assert deleted.status_code == 200
        missing = await client.put(f"/schedules/{schedule['schedule_id']}", headers=headers, json={"enabled": False})
        assert missing.status_code == 404


def test_due_schedule_claim_is_atomic_and_persists_recoverable_run(tmp_path, monkeypatch):
    monkeypatch.setenv("CODE_CONDUCTOR_TASK_DB", str(tmp_path / "schedule-claim.sqlite3"))
    now = time.time()
    workflow = {
        "workflow_id": "workflow-1", "name": "Scheduled tests", "prompt": "Run tests",
        "agent_id": "auto", "auto_verify": True, "browser_verify_url": "",
        "browser_steps": [{"action": "click", "selector": "[data-testid='run']"}],
    }
    created = create_schedule({
        "schedule_id": "schedule-1", "workflow_id": "workflow-1", "workflow": workflow,
        "name": "Scheduled tests", "workspace_path": "", "interval_minutes": 15,
        "enabled": True, "next_run_at": now - 1, "max_retries": 1,
        "created_at": now, "updated_at": now,
    })
    assert created["schedule_id"] == "schedule-1"

    due = claim_due_schedules(now, limit=10)
    assert len(due) == 1
    assert due[0]["workflow"]["prompt"] == "Run tests"
    assert due[0]["workflow"]["browser_steps"][0]["action"] == "click"
    assert due[0]["status"] == "queued"
    assert get_incomplete_schedule_runs()[0]["run_id"] == due[0]["run_id"]
    assert get_schedule("schedule-1")["next_run_at"] > now
    assert claim_due_schedules(now, limit=10) == []
    assert list_schedule_runs("schedule-1")[0]["status"] == "queued"


@pytest.mark.asyncio
async def test_run_now_api_requires_auth_and_returns_durable_run(tmp_path, monkeypatch):
    from app.main import app

    monkeypatch.setenv("CODE_CONDUCTOR_TASK_DB", str(tmp_path / "schedule-run-now-api.sqlite3"))
    headers = {"Authorization": f"Bearer {get_settings().auth_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        workflow_response = await client.post("/workflows", headers=headers, json={
            "name": "Immediate", "prompt": "Run a real browser flow", "agent_id": "auto"
        })
        workflow_id = workflow_response.json()["workflow_id"]
        schedule_response = await client.post("/schedules", headers=headers, json={
            "workflow_id": workflow_id, "interval_minutes": 60, "max_retries": 1
        })
        schedule_id = schedule_response.json()["schedule_id"]

        unauthorized = await client.post(f"/schedules/{schedule_id}/run-now")
        assert unauthorized.status_code == 401

        queued = await client.post(f"/schedules/{schedule_id}/run-now", headers=headers)
        assert queued.status_code == 200
        assert queued.json()["status"] == "queued"
        history = await client.get(f"/schedules/{schedule_id}/runs", headers=headers)
        assert history.status_code == 200
        assert history.json()[0]["run_id"] == queued.json()["run_id"]

        duplicate = await client.post(f"/schedules/{schedule_id}/run-now", headers=headers)
        assert duplicate.status_code == 409


@pytest.mark.asyncio
async def test_scheduled_run_records_task_result(tmp_path, monkeypatch):
    import asyncio
    from app.scheduler import _monitor_run

    monkeypatch.setenv("CODE_CONDUCTOR_TASK_DB", str(tmp_path / "schedule-run.sqlite3"))
    now = time.time()
    workflow = {"prompt": "Run tests", "agent_id": "auto", "workspace_path": ""}
    create_schedule({
        "schedule_id": "schedule-run", "workflow_id": "workflow-run", "workflow": workflow,
        "name": "Test run", "workspace_path": "", "interval_minutes": 15,
        "enabled": True, "next_run_at": now + 900, "max_retries": 0,
        "created_at": now, "updated_at": now,
    })
    from app.task_store import claim_due_schedules
    run = claim_due_schedules(now + 901)[0]

    async def dispatch(_workflow):
        return "task-completed"

    async def status_lookup(task_id):
        assert task_id == "task-completed"
        return {"status": "completed", "error": None}

    await _monitor_run(run, dispatch, status_lookup, asyncio.Event())
    history = list_schedule_runs("schedule-run")
    assert history[0]["status"] == "completed"
    assert history[0]["task_id"] == "task-completed"
    assert history[0]["attempt"] == 1



@pytest.mark.asyncio
async def test_run_now_is_durable_does_not_shift_cadence_and_rejects_overlap(tmp_path, monkeypatch):
    from app.task_store import queue_schedule_run_now

    monkeypatch.setenv("CODE_CONDUCTOR_TASK_DB", str(tmp_path / "schedule-run-now.sqlite3"))
    now = time.time()
    create_schedule({
        "schedule_id": "schedule-now", "workflow_id": "workflow-now",
        "workflow": {"prompt": "Run browser checks", "agent_id": "auto"},
        "name": "Immediate check", "workspace_path": "", "interval_minutes": 60,
        "enabled": False, "next_run_at": now + 3600, "max_retries": 2,
        "created_at": now, "updated_at": now,
    })
    cadence_before = get_schedule("schedule-now")["next_run_at"]
    run, conflict = queue_schedule_run_now("schedule-now", now=now + 1)
    assert conflict is False
    assert run is not None and run["status"] == "queued"
    assert run["scheduled_for"] == now + 1
    assert get_schedule("schedule-now")["next_run_at"] == cadence_before
    assert get_incomplete_schedule_runs()[0]["run_id"] == run["run_id"]

    duplicate, conflict = queue_schedule_run_now("schedule-now", now=now + 2)
    assert duplicate is None
    assert conflict is True
    assert queue_schedule_run_now("missing-schedule", now=now)[0] is None


def test_active_schedule_does_not_overlap_and_skips_missed_ticks(tmp_path, monkeypatch):
    from app.task_store import update_schedule

    monkeypatch.setenv("CODE_CONDUCTOR_TASK_DB", str(tmp_path / "schedule-overlap.sqlite3"))
    now = time.time()
    create_schedule({
        "schedule_id": "schedule-overlap", "workflow_id": "workflow-overlap",
        "workflow": {"prompt": "Run tests", "agent_id": "auto"},
        "name": "Overlap test", "workspace_path": "", "interval_minutes": 15,
        "enabled": True, "next_run_at": now - 1, "max_retries": 0,
        "created_at": now, "updated_at": now,
    })
    first = claim_due_schedules(now)
    assert len(first) == 1
    update_schedule("schedule-overlap", next_run_at=now - 1)
    # The queued run is still active, so a new occurrence is skipped rather than
    # launching overlapping work; the next interval is moved into the future.
    assert claim_due_schedules(now + 1) == []
    assert get_schedule("schedule-overlap")["next_run_at"] > now
    assert len(list_schedule_runs("schedule-overlap")) == 1


@pytest.mark.asyncio
async def test_transient_status_lookup_error_does_not_dispatch_duplicate_task(tmp_path, monkeypatch):
    import asyncio
    from app.scheduler import _monitor_run

    monkeypatch.setenv("CODE_CONDUCTOR_TASK_DB", str(tmp_path / "schedule-transient.sqlite3"))
    now = time.time()
    create_schedule({
        "schedule_id": "schedule-transient", "workflow_id": "workflow-transient",
        "workflow": {"prompt": "Run tests", "agent_id": "auto"},
        "name": "Transient status", "workspace_path": "", "interval_minutes": 15,
        "enabled": True, "next_run_at": now - 1, "max_retries": 1,
        "created_at": now, "updated_at": now,
    })
    run = claim_due_schedules(now)[0]
    calls = {"dispatch": 0, "status": 0}

    async def dispatch(_workflow):
        calls["dispatch"] += 1
        return "task-stable"

    async def status_lookup(_task_id):
        calls["status"] += 1
        if calls["status"] == 1:
            raise RuntimeError("temporary status database lock")
        return {"status": "completed", "error": None}

    async def fast_wait_for(awaitable, timeout):
        if hasattr(awaitable, "close"):
            awaitable.close()
        raise asyncio.TimeoutError()

    monkeypatch.setattr("app.scheduler.asyncio.wait_for", fast_wait_for)
    await _monitor_run(run, dispatch, status_lookup, asyncio.Event())
    assert calls == {"dispatch": 1, "status": 2}
    assert list_schedule_runs("schedule-transient")[0]["status"] == "completed"



@pytest.mark.asyncio
async def test_uncertain_dispatch_retry_reuses_same_idempotency_key(tmp_path, monkeypatch):
    import asyncio
    from app.scheduler import _monitor_run

    monkeypatch.setenv("CODE_CONDUCTOR_TASK_DB", str(tmp_path / "schedule-idempotency.sqlite3"))
    now = time.time()
    workflow = {"prompt": "Run tests", "agent_id": "auto", "workspace_path": ""}
    create_schedule({
        "schedule_id": "schedule-idempotency", "workflow_id": "workflow-idempotency",
        "workflow": workflow, "name": "Idempotency test", "workspace_path": "",
        "interval_minutes": 15, "enabled": True, "next_run_at": now + 900,
        "max_retries": 1, "created_at": now, "updated_at": now,
    })
    run = claim_due_schedules(now + 901)[0]
    observed_keys = []
    created_tasks = {}

    async def dispatch(payload):
        key = payload["_code_conductor_idempotency_key"]
        observed_keys.append(key)
        # Simulate task persistence succeeding but the HTTP/dispatch response being lost.
        task_id = created_tasks.setdefault(key, "task-created-once")
        if len(observed_keys) == 1:
            raise RuntimeError("dispatch response lost")
        return task_id

    async def status_lookup(task_id):
        assert task_id == "task-created-once"
        return {"status": "completed", "error": None}

    async def fast_wait_for(awaitable, timeout):
        if hasattr(awaitable, "close"):
            awaitable.close()
        raise asyncio.TimeoutError()

    monkeypatch.setattr("app.scheduler.asyncio.wait_for", fast_wait_for)
    await _monitor_run(run, dispatch, status_lookup, asyncio.Event())

    assert len(observed_keys) == 2
    assert observed_keys[0] == observed_keys[1]
    assert len(created_tasks) == 1
    history = list_schedule_runs("schedule-idempotency")
    assert history[0]["status"] == "completed"
    assert history[0]["attempt"] == 1



@pytest.mark.asyncio
async def test_global_pause_defers_new_dispatch_until_resumed(tmp_path, monkeypatch):
    import asyncio
    from app.scheduler import _monitor_run

    monkeypatch.setenv("CODE_CONDUCTOR_TASK_DB", str(tmp_path / "scheduler-pause.sqlite3"))
    monkeypatch.setattr("app.scheduler._scheduler_poll_seconds", 0.01)
    pause_states = iter([True, False])
    monkeypatch.setattr("app.scheduler.is_scheduler_paused", lambda: next(pause_states, False))

    now = time.time()
    create_schedule({
        "schedule_id": "schedule-pause-test", "workflow_id": "workflow-pause-test",
        "workflow": {"prompt": "Run a task", "agent_id": "mock", "workspace_path": ""},
        "name": "Pause test", "workspace_path": "", "interval_minutes": 15,
        "enabled": True, "next_run_at": now + 900, "max_retries": 0,
        "created_at": now, "updated_at": now,
    })
    run = claim_due_schedules(now + 901)[0]
    dispatched = []

    async def dispatch(payload):
        dispatched.append(payload)
        return "task-after-resume"

    async def status_lookup(task_id):
        assert task_id == "task-after-resume"
        return {"status": "completed", "error": None}

    await _monitor_run(run, dispatch, status_lookup, asyncio.Event())
    assert len(dispatched) == 1
    assert dispatched[0]["_code_conductor_idempotency_key"].startswith(
        "code-conductor:schedule:"
    )
