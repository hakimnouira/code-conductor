"""Machine-readable task evidence and artifact manifest tests."""
import json
import sqlite3
import subprocess

import pytest

from app.main import TaskRecord
from app.models.schemas import TaskCreateRequest, TaskStatus
from app.task_store import get_saved_tasks
from app.provenance import collect_workspace_provenance


pytestmark = pytest.mark.unit


def test_evidence_is_attached_to_task_snapshot_and_atomic_manifest(tmp_path, monkeypatch):
    monkeypatch.setenv("CODE_CONDUCTOR_TASK_DB", str(tmp_path / "tasks.sqlite3"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    request = TaskCreateRequest(
        prompt="Implement and verify a feature",
        agent_id="mock",
        workspace_path=str(workspace),
        auto_verify=True,
    )
    task = TaskRecord("task-evidence", request, "mock")
    task.status = TaskStatus.COMPLETED
    task.verification_status = "passed"
    task.add_evidence(
        "automated_tests",
        "passed",
        "pytest passed with exit code 0.",
        details={"command": "python -m pytest", "exit_code": 0, "duration_ms": 23.5},
    )
    task.add_evidence(
        "task_completion",
        "completed",
        "Agent execution reached a terminal completion event.",
        details={"verification_status": task.verification_status},
    )

    manifest_path = workspace / ".code-conductor" / "artifacts" / "task-evidence" / "evidence.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    assert manifest["task_id"] == "task-evidence"
    assert manifest["task_status"] == "completed"
    assert manifest["verification_status"] == "passed"
    assert [item["kind"] for item in manifest["evidence"]] == [
        "automated_tests",
        "task_completion",
    ]
    assert manifest["evidence"][0]["details"]["exit_code"] == 0
    assert not manifest_path.with_suffix(".json.tmp").exists()

    persisted = next(
        item["snapshot"] for item in get_saved_tasks()
        if item["snapshot"]["task_id"] == "task-evidence"
    )
    assert persisted["verification_status"] == "passed"
    assert len(persisted["evidence"]) == 2


def test_evidence_manifest_does_not_follow_workspace_escape(tmp_path, monkeypatch):
    monkeypatch.setenv("CODE_CONDUCTOR_TASK_DB", str(tmp_path / "tasks-escape.sqlite3"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / ".code-conductor").symlink_to(outside, target_is_directory=True)
    task = TaskRecord(
        "task-escape",
        TaskCreateRequest(prompt="safe task", workspace_path=str(workspace)),
        "mock",
    )

    # Evidence stays in the durable database; a hostile symlink cannot redirect
    # the manifest outside the chosen workspace.
    task.add_evidence("automated_tests", "passed", "test")
    assert not (outside / "artifacts" / "task-escape" / "evidence.json").exists()
    assert task.snapshot()["evidence"][0]["kind"] == "automated_tests"



def test_workspace_provenance_fingerprints_revision_tracked_and_untracked_changes(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(workspace), *args],
            check=True,
            capture_output=True,
            text=True,
        )

    git("init")
    git("config", "user.name", "Code Conductor Tests")
    git("config", "user.email", "tests@example.invalid")
    tracked = workspace / "app.py"
    tracked.write_text("value = 1\n", encoding="utf-8")
    git("add", "app.py")
    git("commit", "-m", "initial")
    head = git("rev-parse", "HEAD").stdout.strip()

    tracked.write_text("value = 2\n", encoding="utf-8")
    untracked = workspace / "new_module.py"
    untracked.write_text("token = 'never-return-this-value'\n", encoding="utf-8")

    first = collect_workspace_provenance(workspace)
    assert first["status"] == "captured"
    assert first["head_commit"] == head
    assert first["dirty"] is True
    assert first["snapshot_complete"] is True
    assert first["snapshot_sha256"]
    assert first["untracked_files_hashed"] == 1
    assert "never-return-this-value" not in json.dumps(first)

    untracked.write_text("token = 'changed-value'\n", encoding="utf-8")
    second = collect_workspace_provenance(workspace)
    assert second["snapshot_sha256"] != first["snapshot_sha256"]


def test_workspace_provenance_marks_non_git_workspace_partial(tmp_path):
    result = collect_workspace_provenance(tmp_path)
    assert result["status"] == "partial"
    assert result["workspace_kind"] == "non_git"
    assert result["snapshot_complete"] is False
    assert result["reason"] == "not_a_git_repository"



def test_original_task_request_survives_mutable_execution_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv("CODE_CONDUCTOR_TASK_DB", str(tmp_path / "original-request.sqlite3"))
    request = TaskCreateRequest(
        prompt="Original user instruction",
        agent_id="mock",
        workspace_path=str(tmp_path),
        github_research=False,
    )
    task = TaskRecord("task-original-request", request, "mock")
    task.request.prompt += "\n\nTemporary repair diagnostics added during execution."
    task._persist()

    saved = next(
        item for item in get_saved_tasks()
        if item["snapshot"]["task_id"] == "task-original-request"
    )
    assert saved["initial_request"]["prompt"] == "Original user instruction"
    assert "Temporary repair diagnostics" in saved["request"]["prompt"]



def test_original_request_column_migrates_existing_task_database(tmp_path, monkeypatch):
    database = tmp_path / "legacy-tasks.sqlite3"
    original_request = TaskCreateRequest(
        prompt="Legacy task",
        agent_id="mock",
        workspace_path=str(tmp_path),
    ).model_dump()
    snapshot = {
        "task_id": "task-legacy",
        "status": "completed",
        "created_at": 1.0,
        "started_at": 1.0,
        "completed_at": 2.0,
        "verification_status": "not_requested",
        "evidence": [],
    }
    with sqlite3.connect(database) as db:
        db.execute(
            "CREATE TABLE tasks (task_id TEXT PRIMARY KEY, request_json TEXT NOT NULL, snapshot_json TEXT NOT NULL, updated_at REAL NOT NULL)"
        )
        db.execute(
            "INSERT INTO tasks(task_id, request_json, snapshot_json, updated_at) VALUES (?, ?, ?, ?)",
            ("task-legacy", json.dumps(original_request), json.dumps(snapshot), 2.0),
        )

    monkeypatch.setenv("CODE_CONDUCTOR_TASK_DB", str(database))
    saved = get_saved_tasks()
    assert saved[0]["initial_request"]["prompt"] == "Legacy task"
    assert saved[0]["request"]["prompt"] == "Legacy task"
