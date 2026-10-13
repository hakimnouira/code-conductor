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
            "name": "Nightly check", "prompt": "Run tests and summarize failures", "agent_id": "auto"
        })
        assert workflow.status_code == 200
        workflow_id = workflow.json()["workflow_id"]

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
    assert due[0]["status"] == "queued"
    assert get_incomplete_schedule_runs()[0]["run_id"] == due[0]["run_id"]
    assert get_schedule("schedule-1")["next_run_at"] > now
    assert claim_due_schedules(now, limit=10) == []
    assert list_schedule_runs("schedule-1") == []
