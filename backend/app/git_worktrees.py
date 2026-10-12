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
from app.safety.workspace import WorkspaceValidationError, validate_workspace_path

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
        workspace = validate_workspace_path(workspace_path)
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
