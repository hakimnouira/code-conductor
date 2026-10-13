"""Bounded provenance capture tying task evidence to a Git revision and working-tree snapshot.

This is an integrity fingerprint, not a proof that the code is correct or that every
file in an arbitrarily large workspace was captured. Limits are explicit in the result.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import time
from pathlib import Path
from typing import Any

_MAX_UNTRACKED_FILES = 2_000
_MAX_UNTRACKED_BYTES = 50 * 1024 * 1024
_COMMAND_TIMEOUT_SECONDS = 10


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=_COMMAND_TIMEOUT_SECONDS,
        check=False,
    )


def collect_workspace_provenance(workspace: str | Path) -> dict[str, Any]:
    """Return bounded, secret-safe workspace provenance without returning file contents."""
    captured_at = time.time()
    try:
        root = Path(workspace).resolve(strict=True)
        if not root.is_dir():
            raise ValueError("workspace_not_directory")
        repo_result = _git(root, "rev-parse", "--show-toplevel")
        if repo_result.returncode != 0:
            return {
                "status": "partial",
                "captured_at": captured_at,
                "workspace_kind": "non_git",
                "snapshot_complete": False,
                "reason": "not_a_git_repository",
            }
        repo_root = Path(os.fsdecode(repo_result.stdout).strip()).resolve(strict=True)
        if not root.is_relative_to(repo_root):
            return {
                "status": "error",
                "captured_at": captured_at,
                "workspace_kind": "git",
                "snapshot_complete": False,
                "reason": "workspace_outside_repository",
            }

        head_result = _git(repo_root, "rev-parse", "HEAD")
        if head_result.returncode != 0:
            raise RuntimeError("git_head_unavailable")
        head = os.fsdecode(head_result.stdout).strip()
        status_result = _git(repo_root, "status", "--porcelain=v1", "--untracked-files=all", "-z")
        diff_result = _git(repo_root, "diff", "--binary", "HEAD", "--")
        if status_result.returncode != 0 or diff_result.returncode != 0:
            raise RuntimeError("git_worktree_snapshot_failed")

        digest = hashlib.sha256()
        digest.update(b"code-conductor-workspace-provenance-v1\0")
        digest.update(head.encode("ascii", errors="replace"))
        digest.update(b"\0")
        digest.update(status_result.stdout)
        digest.update(b"\0")
        digest.update(diff_result.stdout)
        untracked_result = _git(repo_root, "ls-files", "--others", "--exclude-standard", "-z")
        if untracked_result.returncode != 0:
            raise RuntimeError("git_untracked_list_failed")

        untracked_paths = [p for p in untracked_result.stdout.split(b"\0") if p]
        complete = len(untracked_paths) <= _MAX_UNTRACKED_FILES
        scanned_files = 0
        scanned_bytes = 0
        for raw_path in untracked_paths[:_MAX_UNTRACKED_FILES]:
            rel = Path(os.fsdecode(raw_path))
            candidate = (repo_root / rel).resolve(strict=False)
            # Never follow symlinks outside the repository or hash their targets.
            original = repo_root / rel
            digest.update(b"untracked\0")
            digest.update(raw_path)
            digest.update(b"\0")
            try:
                if original.is_symlink():
                    digest.update(b"symlink\0")
                    digest.update(os.fsencode(os.readlink(original)))
                    scanned_files += 1
                    continue
                if not candidate.is_relative_to(repo_root) or not candidate.is_file():
                    digest.update(b"unreadable_or_escaping\0")
                    complete = False
                    continue
                size = candidate.stat().st_size
                if scanned_bytes + size > _MAX_UNTRACKED_BYTES:
                    complete = False
                    digest.update(b"size_limit_reached\0")
                    break
                with candidate.open("rb") as stream:
                    remaining = size
                    while remaining:
                        chunk = stream.read(min(1024 * 1024, remaining))
                        if not chunk:
                            complete = False
                            break
                        digest.update(chunk)
                        remaining -= len(chunk)
                        scanned_bytes += len(chunk)
                scanned_files += 1
            except OSError:
                complete = False
                digest.update(b"read_error\0")

        return {
            "status": "captured" if complete else "partial",
            "captured_at": captured_at,
            "workspace_kind": "git",
            "repository_root": str(repo_root),
            "workspace_relative_path": str(root.relative_to(repo_root)).replace("\\", "/"),
            "head_commit": head,
            "dirty": bool(status_result.stdout),
            "changed_paths": len([p for p in status_result.stdout.split(b"\0") if p]),
            "untracked_files_hashed": scanned_files,
            "untracked_bytes_hashed": scanned_bytes,
            "snapshot_sha256": digest.hexdigest(),
            "snapshot_complete": complete,
            "limits": {
                "max_untracked_files": _MAX_UNTRACKED_FILES,
                "max_untracked_bytes": _MAX_UNTRACKED_BYTES,
            },
        }
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        return {
            "status": "error",
            "captured_at": captured_at,
            "workspace_kind": "unknown",
            "snapshot_complete": False,
            "reason": str(exc)[:200] or type(exc).__name__,
        }
