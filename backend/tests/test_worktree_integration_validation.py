import asyncio
import subprocess
from pathlib import Path

from app import git_worktrees
from app.git_worktrees import WorktreeValidateRequest, validate_worktree_integration


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repository(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True)
    _git(path, "config", "user.name", "Code Conductor Tests")
    _git(path, "config", "user.email", "tests@example.invalid")
    (path / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "base")
    return path


def _managed_worktree(repo: Path, managed: Path, branch: str) -> Path:
    managed.mkdir(parents=True, exist_ok=True)
    target = managed / branch.replace("/", "__")
    _git(repo, "worktree", "add", "-b", branch, str(target), "HEAD")
    return target


def _patch_validation_roots(monkeypatch, repo: Path, managed: Path):
    monkeypatch.setattr(git_worktrees, "_repo_root", lambda _workspace: repo.resolve())
    monkeypatch.setattr(git_worktrees, "_managed_root", lambda _repo: managed.resolve())


def test_integration_validation_runs_checks_on_disposable_merge(tmp_path, monkeypatch):
    repo = _repository(tmp_path / "repo")
    managed = tmp_path / "managed"
    target = _managed_worktree(repo, managed, "cc/agent-a")
    (target / "sample.py").write_text("VALUE = 2\n", encoding="utf-8")
    (target / "test_sample.py").write_text(
        "from sample import VALUE\n\ndef test_integrated_change():\n    assert VALUE == 2\n",
        encoding="utf-8",
    )
    _git(target, "add", ".")
    _git(target, "commit", "-m", "implement feature")
    base_commit = _git(repo, "rev-parse", "HEAD")
    _patch_validation_roots(monkeypatch, repo, managed)
    monkeypatch.setenv("CODE_CONDUCTOR_TEST_SANDBOX", "host")

    result = asyncio.run(validate_worktree_integration(
        WorktreeValidateRequest(
            workspace_path=str(repo),
            worktree_path=str(target),
            base_ref="HEAD",
            timeout_seconds=30,
        ),
        "test-token",
    ))

    assert result["success"] is True
    assert result["status"] == "passed"
    assert result["branch"] == "cc/agent-a"
    assert result["checks"][0]["passed"] is True
    assert result["base_branch_modified"] is False
    assert _git(repo, "rev-parse", "HEAD") == base_commit
    assert (target / "sample.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert not any(path.name.startswith("integration-") for path in managed.iterdir())


def test_integration_validation_reports_merge_conflicts_without_mutating_base(tmp_path, monkeypatch):
    repo = _repository(tmp_path / "repo")
    managed = tmp_path / "managed"
    target = _managed_worktree(repo, managed, "cc/conflict")
    (target / "sample.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(target, "add", "sample.py")
    _git(target, "commit", "-m", "branch change")
    (repo / "sample.py").write_text("VALUE = 3\n", encoding="utf-8")
    _git(repo, "add", "sample.py")
    _git(repo, "commit", "-m", "base change")
    base_commit = _git(repo, "rev-parse", "HEAD")
    _patch_validation_roots(monkeypatch, repo, managed)

    result = asyncio.run(validate_worktree_integration(
        WorktreeValidateRequest(
            workspace_path=str(repo),
            worktree_path=str(target),
            base_ref="HEAD",
            timeout_seconds=30,
        ),
        "test-token",
    ))

    assert result["success"] is False
    assert result["status"] == "merge_conflict"
    assert "sample.py" in result["conflicts"]
    assert result["base_branch_modified"] is False
    assert _git(repo, "rev-parse", "HEAD") == base_commit
    assert (repo / "sample.py").read_text(encoding="utf-8") == "VALUE = 3\n"
    assert not any(path.name.startswith("integration-") for path in managed.iterdir())