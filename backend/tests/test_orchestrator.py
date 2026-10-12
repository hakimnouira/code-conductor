import time
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app
from app.orchestrator.executor import ParallelOrchestrator
from app.orchestrator.history import AuditHistoryTracker
from app.orchestrator.models import (
    ExecutionPlan,
    OrchestrationAuditRecord,
    ReviewResult,
    Subtask,
    SubtaskStatus,
)
from app.orchestrator.planner import PlanDecomposer
from app.orchestrator.reviewer import CodeReviewer


# -------------------------------------------------------------
# 1. PlanDecomposer Tests
# -------------------------------------------------------------

def test_plan_decomposer_heuristic():
    planner = PlanDecomposer()
    plan = planner.heuristic_decompose("Refactor user auth module and add unit tests")

    assert plan.plan_id.startswith("plan-")
    assert len(plan.subtasks) >= 2
    assert len(plan.parallel_batches) >= 1

    # Check dependency ordering
    task_map = {t.id: t for t in plan.subtasks}
    assert "subtask-1" in task_map
    assert "subtask-2" in task_map


def test_disjoint_parallel_batch_computation():
    planner = PlanDecomposer()

    # Create 3 subtasks:
    # Task 1 & Task 2 have no dependencies and touch disjoint files -> same batch!
    # Task 3 depends on Task 1 and Task 2 -> second batch!
    t1 = Subtask(
        id="t1",
        title="Edit auth",
        prompt="Update auth",
        target_files=["src/auth.py"],
        dependencies=[],
    )
    t2 = Subtask(
        id="t2",
        title="Edit billing",
        prompt="Update billing",
        target_files=["src/billing.py"],
        dependencies=[],
    )
    t3 = Subtask(
        id="t3",
        title="Integration test",
        prompt="Run integration test",
        target_files=["tests/test_integration.py"],
        dependencies=["t1", "t2"],
    )

    batches = planner.compute_parallel_batches([t1, t2, t3])
    assert len(batches) == 2
    # Batch 1 should contain both disjoint tasks t1 and t2
    assert set(batches[0]) == {"t1", "t2"}
    # Batch 2 should contain dependent task t3
    assert batches[1] == ["t3"]


# -------------------------------------------------------------
# 2. CodeReviewer Tests
# -------------------------------------------------------------

def test_code_reviewer_clean_code():
    reviewer = CodeReviewer()
    s1 = Subtask(
        id="s1",
        title="Implement helper",
        prompt="Write helper",
        target_files=["src/utils.py"],
        output="def add(a, b):\n    return a + b\n",
        status=SubtaskStatus.COMPLETED,
    )

    res = reviewer.review_changes("Add helper function", [s1])
    assert res.approved is True
    assert res.score >= 80
    assert len(res.issues) == 0


def test_code_reviewer_catches_dangerous_code():
    reviewer = CodeReviewer()
    # Subtask with dangerous eval and hardcoded secret
    s1 = Subtask(
        id="s1",
        title="Execute eval",
        prompt="Do stuff",
        target_files=["src/dangerous.py"],
        output="api_key = 'abcdef1234567890abcdef123456'\nresult = eval(user_input)\n",
        status=SubtaskStatus.COMPLETED,
    )

    res = reviewer.review_changes("Danger test", [s1])
    assert res.approved is False
    assert res.score < 70
    assert any("eval" in i.message.lower() for i in res.issues)
    assert any("secret" in i.message.lower() or "api key" in i.message.lower() for i in res.issues)


# -------------------------------------------------------------
# 3. AuditHistoryTracker Tests
# -------------------------------------------------------------

def test_audit_history_tracker(tmp_path: Path):
    history_file = tmp_path / "test_history.jsonl"
    tracker = AuditHistoryTracker(history_file=history_file)

    assert len(tracker.list_records()) == 0

    plan = ExecutionPlan(plan_id="p1", goal="Test goal", subtasks=[], parallel_batches=[])
    rec = OrchestrationAuditRecord(
        task_id="task-123",
        goal="Test goal",
        plan=plan,
        review=ReviewResult(approved=True, score=95, summary="Passed", issues=[], suggestions=[]),
        success=True,
        total_duration_ms=120.5,
        created_at=time.time() - 1,
        completed_at=time.time(),
    )

    tracker.append_record(rec)

    records = tracker.list_records()
    assert len(records) == 1
    assert records[0].task_id == "task-123"

    retrieved = tracker.get_record("task-123")
    assert retrieved is not None
    assert retrieved.goal == "Test goal"

    tracker.clear()
    assert len(tracker.list_records()) == 0


# -------------------------------------------------------------
# 4. ParallelOrchestrator Execution Tests
# -------------------------------------------------------------

@pytest.mark.asyncio
async def test_parallel_orchestrator_execution(tmp_path: Path):
    orchestrator = ParallelOrchestrator(workspace_root=tmp_path)
    orchestrator.history_tracker = AuditHistoryTracker(tmp_path / "hist.jsonl")

    # Plan with 2 disjoint mock tasks
    s1 = Subtask(
        id="s1",
        title="Mock worker 1",
        prompt="Task 1",
        target_files=["file1.py"],
        assigned_agent="mock",
    )
    s2 = Subtask(
        id="s2",
        title="Mock worker 2",
        prompt="Task 2",
        target_files=["file2.py"],
        assigned_agent="mock",
    )
    plan = ExecutionPlan(
        plan_id="p_test",
        goal="Run parallel mock workers",
        subtasks=[s1, s2],
        parallel_batches=[["s1", "s2"]],
    )

    events = []
    async for event in orchestrator.run_plan_stream("task-orch-1", plan):
        events.append(event)

    assert len(events) >= 4
    assert any(e.event_type.value == "started" for e in events)
    assert any(e.event_type.value == "completed" for e in events)

    # Subtasks should be completed
    assert s1.status == SubtaskStatus.COMPLETED
    assert s2.status == SubtaskStatus.COMPLETED
    assert "Echo" in s1.output


    # Audit history should contain record
    assert len(orchestrator.history_tracker.list_records()) == 1


# -------------------------------------------------------------
# 5. FastAPI Endpoints Integration Tests
# -------------------------------------------------------------

@pytest.fixture
def client():
    return TestClient(app)


def test_api_orchestrator_plan_and_history(client: TestClient, tmp_path: Path):
    token = get_settings().auth_token
    headers = {"Authorization": f"Bearer {token}"}

    # 1. Test POST /orchestrator/plan
    plan_res = client.post(
        "/orchestrator/plan",
        json={"prompt": "Build user notification service with tests"},
        headers=headers,
    )
    assert plan_res.status_code == 200
    plan_data = plan_res.json()
    assert "plan_id" in plan_data
    assert len(plan_data["subtasks"]) >= 2
    assert len(plan_data["parallel_batches"]) >= 1

    # 2. Test GET /orchestrator/history
    hist_res = client.get("/orchestrator/history", headers=headers)
    assert hist_res.status_code == 200
    assert isinstance(hist_res.json(), list)
