"""Safe, shallow import of public GitHub repositories for local analysis.

Cloning never runs project code, package managers, build steps, or Git hooks.
"""
import asyncio
import re
import shutil
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.config import get_settings
from app.core.security import require_bearer_token
from app.safety.workspace import WorkspaceValidationError, validate_workspace_path

router = APIRouter(prefix="/workspace/repository", tags=["repository import"])


class RepositoryCloneRequest(BaseModel):
    repository_url: str
    parent_path: str | None = None
    directory_name: str | None = None


def _parse_public_github_url(value: str) -> tuple[str, str]:
    parsed = urlparse(value.strip())
    if parsed.scheme != "https" or parsed.hostname != "github.com":
        raise ValueError("Only public repositories hosted on https://github.com are supported.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Repository URL must not contain credentials, query parameters, or fragments.")
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) != 2:
        raise ValueError("Use a repository URL like https://github.com/owner/repository.")
    owner, repo = parts
    repo = repo.removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", owner) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", repo):
        raise ValueError("Repository URL contains an invalid owner or repository name.")
    if owner in {".", ".."} or repo in {".", ".."}:
        raise ValueError("Invalid repository path.")
    return f"https://github.com/{owner}/{repo}.git", repo


@router.post("/clone")
async def clone_public_repository(
    req: RepositoryCloneRequest,
    _: str = Depends(require_bearer_token),
):
    try:
        clone_url, repo_name = _parse_public_github_url(req.repository_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        parent = validate_workspace_path(
            req.parent_path,
            default_root=get_settings().workspace_root,
        )
    except WorkspaceValidationError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid destination parent: {exc}") from exc

    directory_name = (req.directory_name or repo_name).strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", directory_name) or directory_name in {".", ".."}:
        raise HTTPException(status_code=400, detail="Destination folder name may contain only letters, numbers, dots, underscores, and hyphens.")

    destination = (parent / directory_name).resolve()
    try:
        destination.relative_to(parent.resolve())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Destination must stay inside the selected workspace parent.") from exc

    if destination.exists():
        raise HTTPException(status_code=409, detail=f"Destination already exists: {destination}")

    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "clone", "--depth", "1", "--no-tags", "--",
            clone_url, str(destination),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            shutil.rmtree(destination, ignore_errors=True)
            raise HTTPException(status_code=504, detail="Repository clone timed out after 180 seconds.")
        if proc.returncode != 0:
            shutil.rmtree(destination, ignore_errors=True)
            detail = (stderr or stdout).decode("utf-8", errors="replace").strip()[-1200:]
            raise HTTPException(status_code=502, detail="Git clone failed: " + (detail or "unknown Git error"))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail="Git was not found. Install Git and ensure it is on PATH.") from exc
    except HTTPException:
        raise
    except Exception as exc:
        shutil.rmtree(destination, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"Could not clone repository: {exc}") from exc

    try:
        branch_proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(destination), "branch", "--show-current",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        branch_out, _ = await asyncio.wait_for(branch_proc.communicate(), timeout=5)
        branch = branch_out.decode("utf-8", errors="replace").strip() or "default"
    except Exception:
        branch = "default"

    return {
        "repository_url": "https://github.com/" + clone_url.removeprefix("https://github.com/").removesuffix(".git"),
        "repository_name": repo_name,
        "workspace_path": str(destination),
        "branch": branch,
        "cloned": True,
        "analysis_safety": "Repository downloaded only. No project code, dependency installation, or build scripts were executed.",
    }
