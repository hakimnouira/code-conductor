"""Bounded provenance capture tying task evidence to a Git revision and working-tree snapshot.

This is an integrity fingerprint, not a proof that the code is correct. Command output,
untracked-file counts, and untracked content are explicitly capped so a huge workspace
cannot force unbounded memory use. File contents are never returned.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import time
from pathlib import Path
from threading import Thread
from typing import Any

_MAX_STATUS_BYTES = 5 * 1024 * 1024
_MAX_DIFF_BYTES = 50 * 1024 * 1024
_MAX_UNTRACKED_LIST_BYTES = 5 * 1024 * 1024
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


def _git_bounded_capture(
    root: Path,
    *args: str,
    max_bytes: int,
) -> tuple[bytes, bool, int | None, bool]:
    """Capture at most max_bytes from a Git command, killing it on overflow/timeout."""
    process = subprocess.Popen(
        ["git", "-C", str(root), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    captured = bytearray()
    truncated = False
    timed_out = False

    def consume_stdout() -> None:
        nonlocal truncated
        assert process.stdout is not None
        while True:
            # read1 avoids waiting for a full megabyte when a pipe has less data.
            chunk = process.stdout.read1(min(1024 * 1024, max_bytes + 1 - len(captured)))
            if not chunk:
                break
            remaining = max_bytes - len(captured)
            if len(chunk) > remaining:
                if remaining > 0:
                    captured.extend(chunk[:remaining])
                truncated = True
                try:
                    process.kill()
                except OSError:
                    pass
                break
            captured.extend(chunk)
        try:
            process.stdout.close()
        except OSError:
            pass

    reader = Thread(target=consume_stdout, daemon=True)
    reader.start()
    try:
        process.wait(timeout=_COMMAND_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
    reader.join(timeout=2)
    if reader.is_alive():
        timed_out = True
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        reader.join(timeout=1)

    returncode = process.poll()
    complete = not truncated and not timed_out and returncode == 0
    return bytes(captured), complete, returncode, (truncated or timed_out)


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

        status_output, status_complete, status_code, status_limited = _git_bounded_capture(
            repo_root, "status", "--porcelain=v1", "--untracked-files=all", "-z",
            max_bytes=_MAX_STATUS_BYTES,
        )
        diff_output, diff_complete, diff_code, diff_limited = _git_bounded_capture(
            repo_root, "diff", "--binary", "HEAD", "--", max_bytes=_MAX_DIFF_BYTES,
        )
        untracked_output, untracked_complete, untracked_code, untracked_limited = _git_bounded_capture(
            repo_root, "ls-files", "--others", "--exclude-standard", "-z",
            max_bytes=_MAX_UNTRACKED_LIST_BYTES,
        )
        for name, code, complete in (
            ("git_status", status_code, status_complete),
            ("git_diff", diff_code, diff_complete),
            ("git_untracked_list", untracked_code, untracked_complete),
        ):
            if not complete and code not in (0, None) and not (
                status_limited if name == "git_status"
                else diff_limited if name == "git_diff"
                else untracked_limited
            ):
                raise RuntimeError(f"{name}_failed")

        raw_paths = untracked_output.split(b"\0")
        if untracked_output and not untracked_output.endswith(b"\0"):
            # The last item may be a truncated path and must never be opened.
            raw_paths = raw_paths[:-1]
        untracked_paths = [p for p in raw_paths if p]
        complete = (
            status_complete
            and diff_complete
            and untracked_complete
            and len(untracked_paths) <= _MAX_UNTRACKED_FILES
        )

        digest = hashlib.sha256()
        digest.update(b"code-conductor-workspace-provenance-v2\0")
        digest.update(head.encode("ascii", errors="replace"))
        digest.update(b"\0status\0")
        digest.update(status_output)
        digest.update(b"\0status_complete=" + str(status_complete).encode("ascii"))
        digest.update(b"\0diff\0")
        digest.update(diff_output)
        digest.update(b"\0diff_complete=" + str(diff_complete).encode("ascii"))
        digest.update(b"\0untracked_list\0")
        digest.update(untracked_output)
        digest.update(b"\0untracked_list_complete=" + str(untracked_complete).encode("ascii"))

        scanned_files = 0
        scanned_bytes = 0
        for raw_path in untracked_paths[:_MAX_UNTRACKED_FILES]:
            rel = Path(os.fsdecode(raw_path))
            candidate = (repo_root / rel).resolve(strict=False)
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
            "dirty": bool(status_output),
            "changed_paths": len([p for p in status_output.split(b"\0") if p]),
            "status_bytes_hashed": len(status_output),
            "diff_bytes_hashed": len(diff_output),
            "diff_complete": diff_complete,
            "untracked_path_list_bytes_hashed": len(untracked_output),
            "untracked_files_hashed": scanned_files,
            "untracked_bytes_hashed": scanned_bytes,
            "snapshot_sha256": digest.hexdigest(),
            "snapshot_complete": complete,
            "limits": {
                "max_status_bytes": _MAX_STATUS_BYTES,
                "max_diff_bytes": _MAX_DIFF_BYTES,
                "max_untracked_list_bytes": _MAX_UNTRACKED_LIST_BYTES,
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
