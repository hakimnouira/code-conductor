import asyncio
from pathlib import Path

import pytest

from app import main
from app.main import TaskRecord
from app.models.schemas import TaskCreateRequest, TaskStatus


@pytest.mark.asyncio
async def test_startup_recovery_resumes_unstarted_tasks_but_never_replays_running_tasks(tmp_path, monkeypatch):
    monkeypatch.setenv("CODE_CONDUCTOR_TASK_DB", str(tmp_path / "tasks.sqlite3"))
    monkeypatch.setattr(main, "_tasks", {})
    monkeypatch.setattr(
        main,
        "validate_workspace_path",
        lambda workspace_path, **_kwargs: Path(workspace_path).resolve(),
    )

    pending_request = TaskCreateRequest(
        prompt="Queued but not started",
        agent_id="mock",
        workspace_path=str(tmp_path),
        github_research=False,
    )
    pending = TaskRecord(
        "task-recovery-pending",
        pending_request,
        "mock",
        initial_request_json=pending_request.model_dump_json(),
    )
    main._tasks[pending.task_id] = pending

    running_request = TaskCreateRequest(
        prompt="Already running before crash",
        agent_id="mock",
        workspace_path=str(tmp_path),
        github_research=False,
    )
    running = TaskRecord(
        "task-recovery-running",
        running_request,
        "mock",
        restored={
            "task_id": "task-recovery-running",
            "status": TaskStatus.RUNNING.value,
            "started_at": 1000.0,
            "original_request_available": True,
        },
        initial_request_json=running_request.model_dump_json(),
    )
    main._tasks[running.task_id] = running

    class Adapter:
        agent_id = "mock"

    class Registry:
        async def resolve_agent(self, agent_id, prompt):
            assert agent_id == "mock"
            assert prompt == "Queued but not started"
            return Adapter()

    monkeypatch.setattr(main, "get_agent_registry", lambda: Registry())
    resumed = []

    async def fake_pipeline(record, adapter):
        resumed.append((record.task_id, adapter.agent_id))

    monkeypatch.setattr(main, "_run_task_pipeline", fake_pipeline)

    await main._resume_pending_tasks_after_restart()
    await asyncio.sleep(0)

    assert resumed == [("task-recovery-pending", "mock")]
    assert pending.evidence[-1]["kind"] == "restart_recovery"
    assert pending.evidence[-1]["details"]["automatic_replay"] is True
    assert running.status == TaskStatus.RUNNING
    assert all(item[0] != running.task_id for item in resumed)
