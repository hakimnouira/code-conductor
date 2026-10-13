import time

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.task_store import (
    claim_due_scheduled_jobs,
    create_scheduled_job,
    finish_scheduled_run,
    get_scheduled_job,
    list_scheduled_runs,
    start_scheduled_run,
)

pytestmark = pytest.mark.mocked_integration


@pytest.fixture
def auth_headers():
    return {"Authorization": f"Bearer {get_settings().auth_token}"}


def _job(job_id="schedule-test", *, next_run_at=None, interval_seconds=None):
    now = time.time()
    return {
        "job_id": job_id,
        "name": "Test schedule",
        "payload": {"prompt": "echo scheduled test", "agent_id": "mock", "workspace_path": "."},
        "schedule_type": "interval" if interval_seconds else "once",
        "interval_seconds": interval_seconds,
        "next_run_at": next_run_at if next_run_at is not None else now - 1,
        "retry_count": 0,
        "max_retries": 1,
        "enabled": True,
        "created_at": now,
        "updated_at": now,
    }


def test_due_schedule_is_claimed_once_and_interval_is_advanced(monkeypatch, tmp_path):
    monkeypatch.setenv("CODE_CONDUCTOR_TASK_DB", str(tmp_path / "scheduler.sqlite3"))
    job = create_scheduled_job(_job(interval_seconds=120))
    first = claim_due_scheduled_jobs(time.time())
    assert [item["job_id"] for item in first] == [job["job_id"]]
    assert first[0]["due_at"] is not None
    assert claim_due_scheduled_jobs(time.time()) == []
    saved = get_scheduled_job(job["job_id"])
    assert saved["next_run_at"] > time.time()


def test_scheduled_run_history_persists_result_and_retry(monkeypatch, tmp_path):
    monkeypatch.setenv("CODE_CONDUCTOR_TASK_DB", str(tmp_path / "scheduler.sqlite3"))
    job = create_scheduled_job(_job())
    start_scheduled_run({
        "run_id": "run-test", "job_id": job["job_id"], "scheduled_at": time.time(),
        "started_at": time.time(), "status": "running",
    })
    retry_at = time.time() + 60
    finish_scheduled_run({
        "run_id": "run-test", "job_id": job["job_id"], "status": "failed",
        "task_id": None, "error": "provider unavailable", "result": None, "finished_at": time.time(),
    }, retry_at=retry_at, retry_count=1)
    saved = get_scheduled_job(job["job_id"])
    assert saved["retry_at"] == pytest.approx(retry_at)
    assert saved["retry_count"] == 1
    runs = list_scheduled_runs(limit=10, job_id=job["job_id"])
    assert runs[0]["status"] == "failed"
    assert runs[0]["error"] == "provider unavailable"


@pytest.mark.asyncio
async def test_scheduler_api_validates_and_lists_jobs(auth_headers, monkeypatch, tmp_path):
    monkeypatch.setenv("CODE_CONDUCTOR_TASK_DB", str(tmp_path / "api-scheduler.sqlite3"))
    from app.main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        invalid = await client.post("/scheduler/jobs", headers=auth_headers, json={
            "name": "Bad schedule", "prompt": "do work"
        })
        assert invalid.status_code == 422
        created = await client.post("/scheduler/jobs", headers=auth_headers, json={
            "name": "Nightly maintenance", "prompt": "run tests", "agent_id": "mock",
            "interval_seconds": 3600, "workspace_path": str(tmp_path),
            "github_research": False,
        })
        assert created.status_code == 200
        job = created.json()
        assert job["schedule_type"] == "interval"
        assert job["interval_seconds"] == 3600
        listing = await client.get("/scheduler/jobs", headers=auth_headers)
        assert listing.status_code == 200
        assert any(item["job_id"] == job["job_id"] for item in listing.json())
        runs = await client.get("/scheduler/runs", headers=auth_headers)
        assert runs.status_code == 200
