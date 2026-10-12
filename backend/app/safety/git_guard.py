import asyncio
import json
import os
import shutil
import time
import uuid
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

    async def _run_git(
        self,
        *args: str,
        timeout: float = 15.0,
        env: dict[str, str] | None = None,
    ) -> tuple[int, str, str]:
        """Runs a git command inside the workspace directory."""
        if not self._git_path:
            return -1, "", "Git executable not found in PATH."

        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)

        try:
            proc = await asyncio.create_subprocess_exec(
                self._git_path,
                *args,
                cwd=str(self.workspace_root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=merged_env,
            )
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            stdout_str = stdout_b.decode("utf-8", errors="replace")
            stderr_str = stderr_b.decode("utf-8", errors="replace")
            return proc.returncode or 0, stdout_str, stderr_str
        except asyncio.TimeoutError:
            return -1, "", "Git command timed out."
        except Exception as e:
            return -1, "", str(e)

    async def _run_git_bytes(
        self,
        *args: str,
        timeout: float = 15.0,
        env: dict[str, str] | None = None,
    ) -> tuple[int, bytes, str]:
        """Runs a git command inside the workspace directory returning raw stdout bytes."""
        if not self._git_path:
            return -1, b"", "Git executable not found in PATH."

        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)

        try:
            proc = await asyncio.create_subprocess_exec(
                self._git_path,
                *args,
                cwd=str(self.workspace_root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=merged_env,
            )
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            stderr_str = stderr_b.decode("utf-8", errors="replace")
            return proc.returncode or 0, stdout_b, stderr_str
        except asyncio.TimeoutError:
            return -1, b"", "Git command timed out."
        except Exception as e:
            return -1, b"", str(e)

    async def is_path_ignored(self, rel_path: str) -> bool:
        """Checks if a relative path is ignored by git (.gitignore)."""
        code, _, _ = await self._run_git("check-ignore", rel_path)
        return code == 0


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
        Durable checkpoint mechanism:
        Captures the entire working directory state (staged, unstaged, untracked, deleted)
        using an isolated temporary Git index file, preserving the user's current index and working tree.
        Records an isolated Git tree and commit under refs/aihub/checkpoints/<id> and metadata.
        """
        if not self.is_git_repo():
            return CheckpointResponse(
                checkpoint_id="",
                success=False,
                message="Cannot create checkpoint: not a git repository.",
            )

        ts = int(time.time())
        clean_name = ("-" + name.strip().replace(" ", "_").replace("/", "_")) if name else ""
        checkpoint_id = f"ckpt{clean_name}-{ts}-{uuid.uuid4().hex[:6]}"


        git_dir = self.workspace_root / ".git"
        tmp_idx_path = git_dir / f"aihub_tmp_idx_{checkpoint_id}"
        tmp_env = {"GIT_INDEX_FILE": str(tmp_idx_path)}

        try:
            # Check if repo has HEAD commit
            head_code, head_sha, _ = await self._run_git("rev-parse", "--verify", "HEAD")
            has_head = head_code == 0 and len(head_sha.strip()) > 0
            head_sha = head_sha.strip() if has_head else None

            # 1. Initialize temporary index with current HEAD (if available)
            if has_head:
                await self._run_git("read-tree", "HEAD", env=tmp_env)

            # 2. Add all current working directory files (tracked, untracked, modified, deleted)
            add_code, _, add_err = await self._run_git("add", "-A", ".", env=tmp_env)
            if add_code != 0:
                return CheckpointResponse(
                    checkpoint_id=checkpoint_id,
                    success=False,
                    message=f"Failed to stage files in checkpoint index: {add_err.strip()}",
                )

            # 3. Write tree from the temporary index
            code, tree_sha, err = await self._run_git("write-tree", env=tmp_env)
            tree_sha = tree_sha.strip()
            if code != 0 or not tree_sha:
                return CheckpointResponse(
                    checkpoint_id=checkpoint_id,
                    success=False,
                    message=f"Failed to create checkpoint tree: {err.strip()}",
                )

            # 4. Commit tree to an isolated commit object
            commit_args = ["commit-tree", tree_sha, "-m", f"AI Hub checkpoint: {checkpoint_id}"]
            if has_head and head_sha:
                commit_args.extend(["-p", head_sha])

            code, commit_sha, err = await self._run_git(*commit_args, env=tmp_env)
            commit_sha = commit_sha.strip()
            if code != 0 or not commit_sha:
                return CheckpointResponse(
                    checkpoint_id=checkpoint_id,
                    success=False,
                    message=f"Failed to create checkpoint commit: {err.strip()}",
                )

            # 5. Update ref refs/aihub/checkpoints/<id>
            ref_name = f"refs/aihub/checkpoints/{checkpoint_id}"
            await self._run_git("update-ref", ref_name, commit_sha)

            # Also maintain backward compatible branch name ai-safety/<id>
            legacy_branch = f"ai-safety/{checkpoint_id}"
            await self._run_git("branch", legacy_branch, commit_sha)

            # Persist checkpoint metadata in .git/aihub_checkpoints/<id>.json
            meta_dir = git_dir / "aihub_checkpoints"
            meta_dir.mkdir(parents=True, exist_ok=True)
            meta_file = meta_dir / f"{checkpoint_id}.json"
            meta_data = {
                "checkpoint_id": checkpoint_id,
                "name": name or "",
                "tree_sha": tree_sha,
                "commit_sha": commit_sha,
                "head_sha": head_sha or "",
                "created_at": time.time(),
            }
            meta_file.write_text(json.dumps(meta_data, indent=2), encoding="utf-8")

            return CheckpointResponse(
                checkpoint_id=checkpoint_id,
                success=True,
                message=f"Safety checkpoint created successfully (ref: {ref_name}, tree: {tree_sha[:8]}).",
            )
        finally:
            if tmp_idx_path.exists():
                try:
                    tmp_idx_path.unlink()
                except OSError:
                    pass

    async def get_tree_entries(self, tree_sha: str) -> dict[str, tuple[str, str]]:
        """
        Retrieves all blob entries for a git tree object.
        Returns a dict mapping relative_path -> (mode, blob_sha).
        Uses ls-tree -r -z for safe handling of all filenames.
        """
        code, stdout_b, _ = await self._run_git_bytes("ls-tree", "-r", "-z", tree_sha)
        if code != 0:
            return {}

        entries: dict[str, tuple[str, str]] = {}
        raw_items = stdout_b.split(b"\x00")
        for item in raw_items:
            if not item:
                continue
            parts = item.split(b"\t", 1)
            if len(parts) != 2:
                continue
            meta_part, path_part = parts
            meta_tokens = meta_part.decode("utf-8", errors="replace").split()
            if len(meta_tokens) >= 3 and meta_tokens[1] == "blob":
                mode = meta_tokens[0]
                sha = meta_tokens[2]
                rel_path = path_part.decode("utf-8", errors="replace").replace("\\", "/")
                entries[rel_path] = (mode, sha)

        return entries

    async def restore_checkpoint(self, checkpoint_id: str, force: bool = False) -> CheckpointResponse:
        """
        Safe transactional restore mechanism:
        1. Resolves checkpoint tree SHA without touching the real Git index.
        2. Inspects working tree:
           - Checks if checkpointed files have been modified or deleted.
           - If conflicting changes exist and force=False, refuses to restore to prevent data loss.
        3. Preserves:
           - Ignored files (.gitignore, .env, local configs).
           - Unrelated user files and directories absent from the checkpoint.
           - The original Git index and any pre-existing staged changes.
        4. Restores files transactionally using an isolated temporary GIT_INDEX_FILE.
        5. Verifies working tree and index before declaring success; rolls back on any error.
        """
        if not self.is_git_repo():
            return CheckpointResponse(
                checkpoint_id=checkpoint_id,
                success=False,
                message="Cannot restore: not a git repository.",
            )

        git_dir = self.workspace_root / ".git"
        meta_file = git_dir / "aihub_checkpoints" / f"{checkpoint_id}.json"
        tree_sha = ""

        if meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
                tree_sha = meta.get("tree_sha", "")
            except Exception:
                pass

        if not tree_sha:
            ref_candidates = [
                f"refs/aihub/checkpoints/{checkpoint_id}",
                f"refs/heads/ai-safety/{checkpoint_id}",
                f"ai-safety/{checkpoint_id}",
                checkpoint_id,
            ]
            for ref in ref_candidates:
                c, out, _ = await self._run_git("rev-parse", "--verify", f"{ref}^{{tree}}")
                if c == 0 and out.strip():
                    tree_sha = out.strip()
                    break

        if not tree_sha:
            return CheckpointResponse(
                checkpoint_id=checkpoint_id,
                success=False,
                message=f"Checkpoint '{checkpoint_id}' not found in repository.",
            )

        # 1. Retrieve entries recorded in this checkpoint tree
        checkpoint_entries = await self.get_tree_entries(tree_sha)
        if not checkpoint_entries:
            # Tree may be empty (valid empty commit)
            c, out, _ = await self._run_git("rev-parse", "--verify", f"{tree_sha}^{{tree}}")
            if c != 0:
                return CheckpointResponse(
                    checkpoint_id=checkpoint_id,
                    success=False,
                    message=f"Failed to inspect checkpoint tree object '{tree_sha}'.",
                )

        # 2. Detect conflicts before modifying any files on disk
        conflicts: list[str] = []
        for rel_p, (_, expected_sha) in checkpoint_entries.items():
            abs_p = self.workspace_root / rel_p
            if abs_p.exists():
                c, cur_sha, _ = await self._run_git("hash-object", str(abs_p))
                if c == 0 and cur_sha.strip() != expected_sha:
                    conflicts.append(f"{rel_p} (modified)")
            else:
                conflicts.append(f"{rel_p} (missing)")

        if conflicts and not force:
            sample = ", ".join(conflicts[:5])
            if len(conflicts) > 5:
                sample += f" (+{len(conflicts) - 5} more)"
            return CheckpointResponse(
                checkpoint_id=checkpoint_id,
                success=False,
                message=(
                    f"Refusing to restore checkpoint '{checkpoint_id}': conflicting uncommitted or subsequent "
                    f"changes detected in [{sample}]. Pass force=True to overwrite."
                ),
            )

        # 3. Snapshot pre-restoration state of checkpointed files and .git/index for transactional rollback & verification
        index_file = git_dir / "index"
        orig_index_bytes: bytes | None = None
        if index_file.exists():
            try:
                orig_index_bytes = index_file.read_bytes()
            except OSError:
                pass

        backup_files: dict[str, bytes | None] = {}
        for rel_p in checkpoint_entries:
            abs_p = self.workspace_root / rel_p
            if abs_p.exists() and abs_p.is_file():
                try:
                    backup_files[rel_p] = abs_p.read_bytes()
                except OSError:
                    backup_files[rel_p] = None
            else:
                backup_files[rel_p] = None

        # 4. Perform restoration using isolated temporary index file
        # This completely guarantees the real .git/index is NEVER modified.
        # Furthermore, checkout-index -a ONLY updates files in the temporary index,
        # leaving ignored files, untracked files, and unrelated user files completely untouched.
        restore_idx_path = git_dir / f"aihub_restore_idx_{uuid.uuid4().hex[:8]}"
        restore_env = {"GIT_INDEX_FILE": str(restore_idx_path)}

        try:
            # Read checkpoint tree into isolated temporary index
            c, _, err = await self._run_git("read-tree", tree_sha, env=restore_env)
            if c != 0:
                raise RuntimeError(f"Failed to load checkpoint tree into temporary index: {err.strip()}")

            # Checkout all files from isolated index into workspace
            c, _, err = await self._run_git("checkout-index", "-a", "-f", "--", env=restore_env)
            if c != 0:
                raise RuntimeError(f"Failed to checkout checkpoint files: {err.strip()}")

            # 5. Verify working tree files match checkpoint exactly
            for rel_p, (_, expected_sha) in checkpoint_entries.items():
                abs_p = self.workspace_root / rel_p
                if not abs_p.exists():
                    raise RuntimeError(f"Verification failed: expected file '{rel_p}' is missing after restoration.")
                c, actual_sha, _ = await self._run_git("hash-object", str(abs_p))
                if c != 0 or actual_sha.strip() != expected_sha:
                    raise RuntimeError(
                        f"Verification failed: hash mismatch on '{rel_p}' (expected {expected_sha}, got {actual_sha.strip()})."
                    )

            # 6. Verify original Git index was preserved
            if orig_index_bytes is not None:
                if not index_file.exists() or index_file.read_bytes() != orig_index_bytes:
                    raise RuntimeError("Verification failed: original Git index was unexpectedly altered.")
            elif index_file.exists():
                raise RuntimeError("Verification failed: Git index file was created when none previously existed.")

        except Exception as ex:
            # ROLLBACK TRANSACTION: restore pre-restoration file contents and remove newly created files
            for rel_p, orig_bytes in backup_files.items():
                abs_p = self.workspace_root / rel_p
                try:
                    if orig_bytes is not None:
                        abs_p.parent.mkdir(parents=True, exist_ok=True)
                        abs_p.write_bytes(orig_bytes)
                    else:
                        abs_p.unlink(missing_ok=True)
                except Exception:
                    pass

            return CheckpointResponse(
                checkpoint_id=checkpoint_id,
                success=False,
                message=f"Checkpoint restoration failed: {str(ex)}. Transaction rolled back safely.",
            )
        finally:
            if restore_idx_path.exists():
                try:
                    restore_idx_path.unlink()
                except OSError:
                    pass

        return CheckpointResponse(
            checkpoint_id=checkpoint_id,
            success=True,
            message=(
                f"Restored files from checkpoint '{checkpoint_id}' exactly (tree: {tree_sha[:8]}). "
                f"Original Git index and untracked/ignored files preserved."
            ),
        )


