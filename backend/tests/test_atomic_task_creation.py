from concurrent.futures import ThreadPoolExecutor

from app.main import TaskRecord
from app.models.schemas import TaskCreateRequest, TaskStatus
from app.task_store import create_task_if_absent, get_saved_task


def test_atomic_task_insert_keeps_the_first_request(tmp_path, monkeypatch):
    monkeypatch.setenv("CODE_CONDUCTOR_TASK_DB", str(tmp_path / "tasks.sqlite3"))

    def create(prompt):
        return create_task_if_absent(
            {
                "task_id": "task-idempotency-race",
                "status": "pending",
                "created_at": 1000.0,
            },
            '{"prompt":' + __import__("json").dumps(prompt) + "}",
            '{"prompt":' + __import__("json").dumps(prompt) + "}",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, ["first request", "second request"]))

    assert sorted(results) == [False, True]
    saved = get_saved_task("task-idempotency-race")
    assert saved is not None
    assert saved["request"]["prompt"] in {"first request", "second request"}
    assert saved["snapshot"]["task_id"] == "task-idempotency-race"


def test_duplicate_task_record_does_not_overwrite_active_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("CODE_CONDUCTOR_TASK_DB", str(tmp_path / "tasks.sqlite3"))
    request = TaskCreateRequest(
        prompt="same idempotency key",
        agent_id="mock",
        workspace_path=str(tmp_path),
        github_research=False,
        idempotency_key="same-key",
    )
    first = TaskRecord("task-duplicate-record", request, "mock")
    assert first.initial_persisted is True
    first.started_at = 1001.0
    first.status = TaskStatus.RUNNING

    duplicate = TaskRecord("task-duplicate-record", request, "mock")
    assert duplicate.initial_persisted is False
    saved = get_saved_task("task-duplicate-record")
    assert saved is not None
    assert saved["snapshot"]["status"] == "running"
    assert saved["snapshot"]["started_at"] == 1001.0
