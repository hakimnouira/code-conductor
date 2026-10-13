import json
from types import SimpleNamespace

from app.completion_gate import evaluate_completion_gate
from app.models.schemas import TaskCreateRequest, WorkflowPresetRequest
from app.task_store import list_workflows, save_workflow


def test_task_acceptance_criteria_are_bounded_and_normalized():
    request = TaskCreateRequest(
        prompt="Implement feature",
        acceptance_criteria=["  preserves API compatibility  ", "", "handles invalid input"],
    )
    assert request.acceptance_criteria == ["preserves API compatibility", "handles invalid input"]


def test_acceptance_criteria_require_passing_automated_evidence():
    request = SimpleNamespace(
        auto_verify=False,
        acceptance_criteria=["preserves API compatibility"],
        browser_verify_url=None,
    )
    missing = evaluate_completion_gate(request, [], "not_requested")
    assert missing["passed"] is False
    assert "automated_test_or_build_evidence_missing" in missing["failures"]

    passed = evaluate_completion_gate(
        request,
        [{"kind": "automated_tests", "status": "passed", "details": {"exit_code": 0}}],
        "passed",
    )
    assert passed["passed"] is True


def test_workflow_acceptance_criteria_persist_across_database_reads(tmp_path, monkeypatch):
    monkeypatch.setenv("CODE_CONDUCTOR_TASK_DB", str(tmp_path / "tasks.sqlite3"))
    request = WorkflowPresetRequest(
        name="API compatibility",
        prompt="Implement the endpoint",
        acceptance_criteria=["preserves existing response schema", "returns 404 for missing records"],
    )
    save_workflow({
        "workflow_id": "workflow-acceptance-test",
        "name": request.name,
        "prompt": request.prompt,
        "agent_id": request.agent_id,
        "auto_verify": request.auto_verify,
        "acceptance_criteria": request.acceptance_criteria,
    })
    restored = next(item for item in list_workflows() if item["workflow_id"] == "workflow-acceptance-test")
    assert restored["acceptance_criteria"] == request.acceptance_criteria
