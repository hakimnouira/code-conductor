import asyncio

from app import main
from app.main import TaskRecord, _run_task_pipeline
from app.models.schemas import AgentEvent, EventType, TaskCreateRequest, TaskStatus
from app.task_store import get_saved_task


def test_lost_execution_lease_stops_provider_without_overwriting_shared_state(tmp_path, monkeypatch):
    monkeypatch.setenv("CODE_CONDUCTOR_TASK_DB", str(tmp_path / "tasks.sqlite3"))
    request = TaskCreateRequest(
        prompt="A task that must stop if it loses ownership",
        agent_id="mock",
        workspace_path=str(tmp_path),
        github_research=False,
    )
    record = TaskRecord("task-lost-lease-test", request, "mock")
    real_sleep = asyncio.sleep
    cancelled = []

    async def fast_heartbeat_sleep(delay):
        if delay >= 20:
            return
        await real_sleep(delay)

    monkeypatch.setattr(main.asyncio, "sleep", fast_heartbeat_sleep)
    monkeypatch.setattr(main, "renew_task_lease", lambda *_args, **_kwargs: False)

    class Adapter:
        async def stream_task(self, task_id, task_request):
            yield AgentEvent(task_id=task_id, event_type=EventType.STARTED, text="started")
            while True:
                await real_sleep(0.01)

        async def cancel_task(self, task_id):
            cancelled.append(task_id)
            return True

    asyncio.run(_run_task_pipeline(record, Adapter()))

    saved = get_saved_task(record.task_id)
    assert saved is not None
    assert saved["snapshot"]["status"] == TaskStatus.RUNNING.value
    assert record.status == TaskStatus.FAILED
    assert cancelled == [record.task_id]
