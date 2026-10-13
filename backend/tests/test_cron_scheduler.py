"""Cron parsing, timezone handling, and durable schedule policy tests."""
from datetime import datetime, timezone
import time

import pytest
from httpx import ASGITransport, AsyncClient

from app.cron_schedule import CronExpressionError, next_cron_occurrence, parse_cron
from app.task_store import claim_due_schedules, create_schedule, get_schedule, list_schedule_runs
from app.core.config import get_settings
from app.main import app


pytestmark = pytest.mark.unit


def test_cron_occurrence_uses_the_requested_timezone():
    after = datetime(2026, 10, 12, 7, 30, tzinfo=timezone.utc).timestamp()
    result = next_cron_occurrence("0 9 * * 1-5", "Africa/Tunis", after)
    local = datetime.fromtimestamp(result, timezone.utc).astimezone(
        __import__("zoneinfo").ZoneInfo("Africa/Tunis")
    )
    assert local.weekday() == 0
    assert (local.hour, local.minute) == (9, 0)
    assert result > after


@pytest.mark.parametrize("expression", ["", "* * *", "60 * * * *", "0 25 * * *", "*/0 * * * *", "5-2 * * * *"])
def test_invalid_cron_expressions_are_rejected(expression):
    with pytest.raises(CronExpressionError):
        parse_cron(expression)


def test_cron_schedule_persists_next_occurrence_and_run_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("CODE_CONDUCTOR_TASK_DB", str(tmp_path / "cron.sqlite3"))
    now = time.time()
    create_schedule({
        "schedule_id": "cron-schedule",
        "workflow_id": "workflow-cron",
        "workflow": {"prompt": "Run checks", "agent_id": "auto"},
        "name": "Cron checks",
        "workspace_path": "",
        "interval_minutes": 60,
        "schedule_type": "cron",
        "cron_expression": "* * * * *",
        "timezone": "UTC",
        "misfire_policy": "run_once",
        "enabled": True,
        "next_run_at": now - 1,
        "max_retries": 1,
        "created_at": now,
        "updated_at": now,
    })
    claimed = claim_due_schedules(now)
    assert len(claimed) == 1
    schedule = get_schedule("cron-schedule")
    assert schedule["schedule_type"] == "cron"
    assert schedule["cron_expression"] == "* * * * *"
    assert schedule["timezone"] == "UTC"
    assert schedule["misfire_policy"] == "run_once"
    assert schedule["next_run_at"] > now
    assert list_schedule_runs("cron-schedule")[0]["status"] == "queued"


def test_skip_misfire_policy_drops_stale_occurrence(tmp_path, monkeypatch):
    monkeypatch.setenv("CODE_CONDUCTOR_TASK_DB", str(tmp_path / "cron-misfire.sqlite3"))
    now = time.time()
    create_schedule({
        "schedule_id": "skip-schedule",
        "workflow_id": "workflow-skip",
        "workflow": {"prompt": "Run checks", "agent_id": "auto"},
        "name": "Skip stale run",
        "workspace_path": "",
        "interval_minutes": 15,
        "schedule_type": "interval",
        "cron_expression": "",
        "timezone": "UTC",
        "misfire_policy": "skip",
        "enabled": True,
        "next_run_at": now - 7200,
        "max_retries": 0,
        "created_at": now,
        "updated_at": now,
    })
    assert claim_due_schedules(now) == []
    assert get_schedule("skip-schedule")["next_run_at"] > now
    assert list_schedule_runs("skip-schedule") == []


@pytest.mark.asyncio
async def test_cron_schedule_api_validates_and_returns_timezone(tmp_path, monkeypatch):
    monkeypatch.setenv("CODE_CONDUCTOR_TASK_DB", str(tmp_path / "cron-api.sqlite3"))
    headers = {"Authorization": f"Bearer {get_settings().auth_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        workflow = await client.post("/workflows", headers=headers, json={
            "name": "Weekday report", "prompt": "Run checks and report failures", "agent_id": "auto"
        })
        assert workflow.status_code == 200
        workflow_id = workflow.json()["workflow_id"]
        response = await client.post("/schedules", headers=headers, json={
            "workflow_id": workflow_id,
            "cron_expression": "0 9 * * 1-5",
            "timezone": "Africa/Tunis",
            "misfire_policy": "skip",
        })
        assert response.status_code == 200, response.text
        schedule = response.json()
        assert schedule["schedule_type"] == "cron"
        assert schedule["cron_expression"] == "0 9 * * 1-5"
        assert schedule["timezone"] == "Africa/Tunis"
        assert schedule["misfire_policy"] == "skip"
        assert schedule["next_run_at"] > time.time()

        invalid = await client.post("/schedules", headers=headers, json={
            "workflow_id": workflow_id,
            "cron_expression": "61 * * * *",
            "timezone": "Africa/Tunis",
        })
        assert invalid.status_code == 422
