import asyncio
import os
import shutil
import time
from pathlib import Path

from app.models.schemas import (
    CheckpointResponse,
    GitDiffResponse,
    GitStatusResponse,
)


class GitGuard:
    """
    Safely inspects git status, computes diffs, and creates safety checkpoints.
    Uses array-based subprocesses without shell=True.
    """

    def __init__(self, workspace_root: str | Path):
        self.workspace_root = Path(workspace_root).resolve()
        self._git_path = shutil.which("git")

    def is_git_available(self) -> bool:
        return self._git_path is not None

    def is_git_repo(self) -> bool:
        if not self.is_git_available():
            return False
        return (self.workspace_root / ".git").exists()

    async def _run_git(self, *args: str, timeout: float = 15.0) -> tuple[int, str, str]:
        """Runs a git command inside the workspace directory."""
        if not self._git_path:
            return -1, "", "Git executable not found in PATH."

        try:
            proc = await asyncio.create_subprocess_exec(
                self._git_path,
                *args,
                cwd=str(self.workspace_root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            stdout_str = stdout_b.decode("utf-8", errors="replace")
            stderr_str = stderr_b.decode("utf-8", errors="replace")
            return proc.returncode or 0, stdout_str, stderr_str
        except asyncio.TimeoutError:
            return -1, "", "Git command timed out."
        except Exception as e:
            return -1, "", str(e)

    async def get_status(self) -> GitStatusResponse:
        """Retrieves structured git status."""
        if not self.is_git_repo():
            return GitStatusResponse(
                is_repo=False,
                branch="",
                clean=True,
                error="Directory is not a git repository.",
            )

        code, stdout, stderr = await self._run_git("status", "--porcelain", "-b")
        if code != 0:
            return GitStatusResponse(
                is_repo=True,
                clean=False,
                error=stderr.strip() or f"Git exited with code {code}",
            )

        branch = "unknown"
        modified: list[str] = []
        untracked: list[str] = []
        deleted: list[str] = []

        for line in stdout.splitlines():
            line = line.rstrip()
            if not line:
                continue

            if line.startswith("## "):
                # Branch info: '## master...origin/master [ahead 1]' or '## main'
                branch_part = line[3:].split("...")[0].split(" ")[0].strip()
                branch = branch_part
                continue

            status_code = line[:2]
            filename = line[3:].strip()
            # Handle quoted paths
            if filename.startswith('"') and filename.endswith('"'):
                filename = filename[1:-1]

            if status_code in ("??", "A ", " A"):
                untracked.append(filename)
            elif "D" in status_code:
                deleted.append(filename)
            else:
                modified.append(filename)

        clean = len(modified) == 0 and len(untracked) == 0 and len(deleted) == 0

        return GitStatusResponse(
            is_repo=True,
            branch=branch,
            clean=clean,
            modified=modified,
            untracked=untracked,
            deleted=deleted,
        )

    async def get_diff(self, file_path: str | None = None, staged: bool = False) -> GitDiffResponse:
        """Computes git diff for workspace or specific file."""
        if not self.is_git_repo():
            return GitDiffResponse(diff="", file_count=0, error="Not a git repository.")

        cmd = ["diff"]
        if staged:
            cmd.append("--staged")

        if file_path:
            cmd.extend(["--", file_path])

        code, stdout, stderr = await self._run_git(*cmd)
        if code != 0:
            return GitDiffResponse(diff="", file_count=0, error=stderr.strip())

        # Count changed files in diff
        file_count = stdout.count("diff --git ")
        return GitDiffResponse(diff=stdout, file_count=file_count)

    async def create_checkpoint(self, name: str | None = None) -> CheckpointResponse:
        """
        Creates a safety checkpoint without modifying working tree.
        Uses `git stash create` to capture unstaged/staged dirty changes,
        and creates a reference branch `ai-safety/<id>` pointing to current state.
        """
        if not self.is_git_repo():
            return CheckpointResponse(
                checkpoint_id="",
                success=False,
                message="Cannot create checkpoint: not a git repository.",
            )

        ts = int(time.time())
        checkpoint_id = f"ckpt-{ts}" if not name else f"ckpt-{name.replace(' ', '_')}-{ts}"

        # 1. Create a stash commit object representing working tree (leaves index/worktree untouched)
        code, stash_sha, _ = await self._run_git("stash", "create", f"Safety checkpoint: {checkpoint_id}")
        stash_sha = stash_sha.strip()

        # 2. Also record a lightweight branch at HEAD
        branch_name = f"ai-safety/{checkpoint_id}"
        await self._run_git("branch", branch_name)

        target_ref = stash_sha if stash_sha else branch_name
        return CheckpointResponse(
            checkpoint_id=checkpoint_id,
            success=True,
            message=f"Safety checkpoint created successfully at reference '{target_ref}' (branch: {branch_name}).",
        )

    async def restore_checkpoint(self, checkpoint_id: str) -> CheckpointResponse:
        """
        Attempts to restore a safety branch created earlier.
        """
        if not self.is_git_repo():
            return CheckpointResponse(
                checkpoint_id=checkpoint_id,
                success=False,
                message="Cannot restore: not a git repository.",
            )

        branch_name = f"ai-safety/{checkpoint_id}"
        # Checkout the safety branch files or checkout files from branch
        code, _, stderr = await self._run_git("checkout", branch_name, "--", ".")
        if code != 0:
            return CheckpointResponse(
                checkpoint_id=checkpoint_id,
                success=False,
                message=f"Failed to restore checkpoint: {stderr.strip()}",
            )

        return CheckpointResponse(
            checkpoint_id=checkpoint_id,
            success=True,
            message=f"Restored files from checkpoint '{checkpoint_id}'.",
        )
