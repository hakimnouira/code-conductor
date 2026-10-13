import json

import pytest

from app import main
from app.main import TaskRecord
from app.models.schemas import TaskCreateRequest, TaskStatus


@pytest.mark.asyncio
async def test_finalization_rejects_verified_task_when_workspace_fingerprint_is_incomplete(tmp_path, monkeypatch):
    monkeypatch.setenv("CODE_CONDUCTOR_TASK_DB", str(tmp_path / "tasks.sqlite3"))
    request = TaskCreateRequest(
        prompt="Make a code change",
        agent_id="mock",
        workspace_path=str(tmp_path),
        github_research=False,
        auto_verify=True,
    )
    record = TaskRecord("task-incomplete-provenance", request, "mock")
    record.verification_status = "passed"
    record.add_evidence(
        "automated_tests",
        "passed",
        "Tests passed",
        details={"exit_code": 0},
    )
    monkeypatch.setattr(
        main,
        "collect_workspace_provenance",
        lambda _path: {
            "status": "partial",
            "workspace_kind": "filesystem",
            "snapshot_sha256": "partial-fingerprint",
            "snapshot_complete": False,
            "reason": "entry_limit_reached",
        },
    )

    completed = await main._finalize_task_completion(record, object())

    assert completed is False
    assert record.status == TaskStatus.FAILED
    assert "workspace_provenance_not_captured" in record.error
    assert "workspace_snapshot_incomplete" in record.error
    assert record.evidence[-1]["kind"] == "completion_gate"
    assert record.evidence[-1]["status"] == "failed"
    queued_events = list(record.event_queue.queue._queue)
    assert any(json.loads(event).get("event_type") == "error" for event in queued_events if event)
    assert not any(
        json.loads(event).get("event_type") == "completed"
        for event in queued_events if event
    )
