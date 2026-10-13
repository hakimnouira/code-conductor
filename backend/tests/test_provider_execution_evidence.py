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

def test_provider_disconnect_fails_closed_and_releases_execution_lease(tmp_path, monkeypatch):
    from app.task_leases import claim_task_lease

    monkeypatch.setenv("CODE_CONDUCTOR_TASK_DB", str(tmp_path / "tasks.sqlite3"))
    request = TaskCreateRequest(
        prompt="A provider disconnects during execution",
        agent_id="mock",
        workspace_path=str(tmp_path),
        github_research=False,
    )
    record = TaskRecord("task-provider-disconnect-test", request, "mock")

    class DisconnectingAdapter:
        async def stream_task(self, task_id, task_request):
            yield AgentEvent(task_id=task_id, event_type=EventType.STARTED, text="started")
            raise ConnectionError("simulated provider disconnect")

        async def cancel_task(self, task_id):
            return True

    asyncio.run(_run_task_pipeline(record, DisconnectingAdapter()))

    assert record.status == TaskStatus.FAILED
    assert "simulated provider disconnect" in (record.error or "")
    assert not any(item.get("kind") == "completion_gate" and item.get("status") == "passed" for item in record.evidence)
    provider = next(item for item in record.evidence if item.get("kind") == "provider_execution")
    assert provider["status"] == "failed"
    assert provider["details"]["terminal_event_seen"] is False
    assert claim_task_lease(record.task_id, "next-worker", now=10_000_000)


def test_provider_stream_ending_without_terminal_event_is_not_success(tmp_path, monkeypatch):
    monkeypatch.setenv("CODE_CONDUCTOR_TASK_DB", str(tmp_path / "tasks.sqlite3"))
    request = TaskCreateRequest(
        prompt="Provider exits without a terminal event",
        agent_id="mock",
        workspace_path=str(tmp_path),
        github_research=False,
    )
    record = TaskRecord("task-provider-no-terminal-test", request, "mock")

    class IncompleteAdapter:
        async def stream_task(self, task_id, task_request):
            yield AgentEvent(task_id=task_id, event_type=EventType.STARTED, text="started")

    asyncio.run(_run_task_pipeline(record, IncompleteAdapter()))

    assert record.status == TaskStatus.FAILED
    assert record.error == "Agent stream ended without a terminal event."
    provider = next(item for item in record.evidence if item.get("kind") == "provider_execution")
    assert provider["status"] == "incomplete"
    assert provider["details"]["terminal_event_seen"] is False
