"""Safe, isolated Git worktree management for parallel coding tasks."""
from __future__ import annotations

import hashlib
import re
import subprocess
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.security import require_bearer_token
from app.core.config import get_settings
from app.safety.workspace import WorkspaceValidationError, validate_workspace_path
from app.safety.test_runner import SafeTestRunner

router = APIRouter(prefix="/workspace/git/worktrees", tags=["git-worktrees"])
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,79}$")


def _managed_root(repo_root: Path) -> Path:
    digest = hashlib.sha256(str(repo_root.resolve()).encode("utf-8")).hexdigest()[:16]
    return (Path.home() / ".code-conductor" / "worktrees" / digest).resolve()


def _git(repo: Path, *args: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(status_code=502, detail=f"Git command failed: {str(exc)[:300]}") from exc


def _repo_root(workspace_path: str | None) -> Path:
    try:
        workspace = validate_workspace_path(workspace_path, default_root=get_settings().workspace_root)
    except WorkspaceValidationError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid workspace path: {exc}") from exc
    result = _git(workspace, "rev-parse", "--show-toplevel")
    if result.returncode != 0:
        raise HTTPException(status_code=400, detail="Selected workspace is not inside a Git repository.")
    return Path(result.stdout.strip()).resolve()


def _validate_branch(branch: str) -> str:
    if (not _BRANCH_RE.fullmatch(branch) or ".." in branch or "@{" in branch
            or branch.endswith((".", "/", ".lock")) or "//" in branch):
        raise HTTPException(status_code=400, detail="Invalid Git branch name.")
    return branch


class WorktreeCreateRequest(BaseModel):
    workspace_path: str | None = None
    branch_name: str = Field(default_factory=lambda: "cc/task-" + uuid.uuid4().hex[:8])
    base_ref: str = "HEAD"


class WorktreeRemoveRequest(BaseModel):
    workspace_path: str | None = None
    worktree_path: str


@router.post("")
async def create_worktree(req: WorktreeCreateRequest, _: str = Depends(require_bearer_token)):
    repo = _repo_root(req.workspace_path)
    branch = _validate_branch(req.branch_name)
    base_ref = req.base_ref.strip()
    if not base_ref or base_ref.startswith("-") or any(c in base_ref for c in ("\n", "\r", "\0")):
        raise HTTPException(status_code=400, detail="Invalid base Git reference.")
    managed = _managed_root(repo)
    target = (managed / (branch.replace("/", "__") + "-" + uuid.uuid4().hex[:6])).resolve()
    if not target.is_relative_to(managed):
        raise HTTPException(status_code=400, detail="Worktree path escaped managed directory.")
    target.parent.mkdir(parents=True, exist_ok=True)
    result = _git(repo, "worktree", "add", "-b", branch, str(target), base_ref, timeout=90)
    if result.returncode != 0:
        raise HTTPException(status_code=409, detail=f"Could not create worktree: {result.stderr[-1000:]}")
    return {"success": True, "repository": str(repo), "branch": branch, "worktree_path": str(target), "base_ref": base_ref}


@router.get("")
async def list_worktrees(workspace_path: str | None = None, _: str = Depends(require_bearer_token)):
    repo = _repo_root(workspace_path)
    result = _git(repo, "worktree", "list", "--porcelain")
    if result.returncode != 0:
        raise HTTPException(status_code=502, detail=f"Could not list worktrees: {result.stderr[-500:]}")
    entries: list[dict[str, str | bool]] = []
    current: dict[str, str | bool] = {}
    for line in result.stdout.splitlines() + [""]:
        if not line.strip():
            if current:
                entries.append(current)
                current = {}
        elif line.startswith("worktree "):
            current["path"] = line[9:]
        elif line.startswith("HEAD "):
            current["head"] = line[5:]
        elif line.startswith("branch "):
            current["branch"] = line[7:].removeprefix("refs/heads/")
        elif line == "detached":
            current["detached"] = True
        elif line == "bare":
            current["bare"] = True
    managed = _managed_root(repo)
    return {"repository": str(repo), "worktrees": [
        item | {"managed_by_code_conductor": Path(str(item["path"])).resolve().is_relative_to(managed)}
        for item in entries
    ]}


@router.delete("")
async def remove_worktree(req: WorktreeRemoveRequest, _: str = Depends(require_bearer_token)):
    repo = _repo_root(req.workspace_path)
    managed = _managed_root(repo)
    target = Path(req.worktree_path).expanduser().resolve()
    if not target.is_relative_to(managed) or target == managed:
        raise HTTPException(status_code=403, detail="Only Code Conductor-managed worktrees can be removed by this endpoint.")
    result = _git(repo, "worktree", "remove", "--force", str(target), timeout=60)
    if result.returncode != 0:
        raise HTTPException(status_code=409, detail=f"Could not remove worktree: {result.stderr[-1000:]}")
    return {"success": True, "removed": str(target)}

class WorktreeValidateRequest(BaseModel):
    workspace_path: str | None = None
    worktree_path: str
    base_ref: str = Field(default="HEAD", min_length=1, max_length=200)
    timeout_seconds: int = Field(default=120, ge=10, le=600)


def _validate_base_ref(base_ref: str) -> str:
    value = base_ref.strip()
    if not value or value.startswith("-") or any(char in value for char in ("\\n", "\\r", "\\0")):
        raise HTTPException(status_code=400, detail="Invalid integration base reference.")
    return value


@router.post("/validate")
async def validate_worktree_integration(
    req: WorktreeValidateRequest,
    _: str = Depends(require_bearer_token),
):
    """Merge a worktree into a disposable base worktree and run checks without changing the user's base branch."""
    repo = _repo_root(req.workspace_path)
    managed = _managed_root(repo)
    try:
        target = Path(req.worktree_path).expanduser().resolve(strict=True)
    except OSError as exc:
        raise HTTPException(status_code=400, detail="Worktree path does not exist.") from exc
    if not target.is_relative_to(managed) or target == managed or not target.is_dir():
        raise HTTPException(status_code=403, detail="Only Code Conductor-managed worktrees can be validated.")

    registered = _git(repo, "worktree", "list", "--porcelain")
    registered_paths = {
        Path(line[9:]).resolve()
        for line in registered.stdout.splitlines()
        if line.startswith("worktree ")
    }
    if registered.returncode != 0 or target not in registered_paths:
        raise HTTPException(status_code=400, detail="The selected path is not a registered worktree for this repository.")

    branch_result = _git(target, "symbolic-ref", "--short", "HEAD")
    if branch_result.returncode != 0:
        raise HTTPException(status_code=400, detail="Detached worktrees cannot be integration-validated.")
    branch = _validate_branch(branch_result.stdout.strip())
    dirty = _git(target, "status", "--porcelain")
    if dirty.returncode != 0:
        raise HTTPException(status_code=502, detail="Could not inspect the worktree state.")
    if dirty.stdout.strip():
        raise HTTPException(
            status_code=409,
            detail="Commit or discard worktree changes before integration validation; uncommitted changes are not included in the merge simulation.",
        )

    base_ref = _validate_base_ref(req.base_ref)
    base_result = _git(repo, "rev-parse", "--verify", f"{base_ref}^{{commit}}")
    if base_result.returncode != 0:
        raise HTTPException(status_code=400, detail="Integration base reference does not resolve to a commit.")
    base_sha = base_result.stdout.strip()

    managed.mkdir(parents=True, exist_ok=True)
    integration = (managed / ("integration-" + uuid.uuid4().hex[:12])).resolve()
    if not integration.is_relative_to(managed):
        raise HTTPException(status_code=400, detail="Temporary integration path escaped the managed worktree directory.")

    added = False
    try:
        add_result = _git(repo, "worktree", "add", "--detach", str(integration), base_sha, timeout=90)
        if add_result.returncode != 0:
            raise HTTPException(status_code=502, detail=f"Could not create temporary integration worktree: {add_result.stderr[-1000:]}")
        added = True

        merge_result = _git(integration, "merge", "--no-commit", "--no-ff", branch, timeout=90)
        if merge_result.returncode != 0:
            conflicts_result = _git(integration, "diff", "--name-only", "--diff-filter=U")
            conflicts = [line for line in conflicts_result.stdout.splitlines() if line][:200]
            _git(integration, "merge", "--abort")
            return {
                "success": False,
                "status": "merge_conflict",
                "repository": str(repo),
                "base_ref": base_ref,
                "base_commit": base_sha,
                "branch": branch,
                "conflicts": conflicts,
                "details": merge_result.stderr[-4000:],
                "base_branch_modified": False,
            }

        whitespace = _git(integration, "diff", "--check", base_sha)
        if whitespace.returncode != 0:
            return {
                "success": False,
                "status": "diff_check_failed",
                "repository": str(repo),
                "base_ref": base_ref,
                "base_commit": base_sha,
                "branch": branch,
                "details": whitespace.stdout[-4000:] + whitespace.stderr[-4000:],
                "base_branch_modified": False,
            }

        changed = _git(integration, "diff", "--name-only", base_sha)
        runner = SafeTestRunner(integration)
        test_key = runner.discover_runner()
        if test_key is None:
            return {
                "success": False,
                "status": "no_supported_test_runner",
                "repository": str(repo),
                "base_ref": base_ref,
                "base_commit": base_sha,
                "branch": branch,
                "changed_files": [line for line in changed.stdout.splitlines() if line][:500],
                "checks": [],
                "base_branch_modified": False,
            }

        checks: list[dict] = []
        test_result = await runner.run_tests(command_key=test_key, timeout_seconds=req.timeout_seconds)
        checks.append({
            "kind": "tests" if test_key != "npm_build" else "build",
            "command": test_result.command,
            "passed": test_result.passed,
            "exit_code": test_result.exit_code,
            "duration_ms": test_result.duration_ms,
            "execution_mode": test_result.execution_mode,
            "stdout_tail": test_result.stdout[-4000:],
            "stderr_tail": test_result.stderr[-4000:],
        })
        if test_result.passed:
            build_key = runner.discover_build_runner()
            if build_key and build_key != test_key:
                build_result = await runner.run_tests(command_key=build_key, timeout_seconds=req.timeout_seconds)
                checks.append({
                    "kind": "build",
                    "command": build_result.command,
                    "passed": build_result.passed,
                    "exit_code": build_result.exit_code,
                    "duration_ms": build_result.duration_ms,
                    "execution_mode": build_result.execution_mode,
                    "stdout_tail": build_result.stdout[-4000:],
                    "stderr_tail": build_result.stderr[-4000:],
                })
        passed = bool(checks) and all(item["passed"] for item in checks)
        return {
            "success": passed,
            "status": "passed" if passed else "checks_failed",
            "repository": str(repo),
            "base_ref": base_ref,
            "base_commit": base_sha,
            "branch": branch,
            "changed_files": [line for line in changed.stdout.splitlines() if line][:500],
            "checks": checks,
            "base_branch_modified": False,
            "note": "Validation ran on a disposable merged worktree; no merge was applied to the base branch.",
        }
    finally:
        if added:
            _git(repo, "worktree", "remove", "--force", str(integration), timeout=60)
            _git(repo, "worktree", "prune", timeout=30)
