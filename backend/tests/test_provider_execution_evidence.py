import asyncio

from app.main import TaskRecord, _run_task_pipeline
from app.models.schemas import AgentEvent, EventType, TaskCreateRequest, TaskStatus


def test_provider_stream_latency_and_attempt_evidence_are_recorded(tmp_path, monkeypatch):
    monkeypatch.setenv("CODE_CONDUCTOR_TASK_DB", str(tmp_path / "tasks.sqlite3"))
    request = TaskCreateRequest(
        prompt="Complete a deterministic task",
        agent_id="mock",
        workspace_path=str(tmp_path),
        github_research=False,
    )
    record = TaskRecord("task-provider-evidence-test", request, "mock")

    class Adapter:
        async def stream_task(self, task_id, task_request):
            yield AgentEvent(
                task_id=task_id,
                event_type=EventType.STARTED,
                text="started",
            )
            yield AgentEvent(
                task_id=task_id,
                event_type=EventType.COMPLETED,
                text="done",
            )

    asyncio.run(_run_task_pipeline(record, Adapter()))

    provider_evidence = [
        item for item in record.evidence if item.get("kind") == "provider_execution"
    ]
    assert record.status == TaskStatus.COMPLETED
    assert len(provider_evidence) == 1
    assert provider_evidence[0]["status"] == "passed"
    assert provider_evidence[0]["details"]["attempt"] == 1
    assert provider_evidence[0]["details"]["events_received"] == 2
    assert provider_evidence[0]["details"]["duration_ms"] >= 0
