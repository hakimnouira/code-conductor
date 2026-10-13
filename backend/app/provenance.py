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
_FILESYSTEM_EXCLUDED_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    "dist", "build", "out", "coverage", "htmlcov",
}
_FILESYSTEM_EXCLUDED_FILES = {".env", "api_keys.json"}


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


def _collect_filesystem_provenance(root: Path, captured_at: float) -> dict[str, Any]:
    """Fingerprint a non-Git workspace without following symlinks or reading unbounded data."""
    digest = hashlib.sha256()
    digest.update(b"code-conductor-filesystem-provenance-v1\\0")
    complete = True
    scanned_files = 0
    scanned_bytes = 0
    scanned_entries = 0
    stack = [root]

    while stack:
        directory = stack.pop()
        try:
            # Bound directory enumeration itself: sorting an unbounded scandir result
            # would defeat the memory limit even if hashing later stopped at the cap.
            entries = []
            overflow = False
            with os.scandir(directory) as iterator:
                for entry in iterator:
                    if len(entries) >= _MAX_UNTRACKED_FILES + 1:
                        overflow = True
                        break
                    entries.append(entry)
            if overflow:
                complete = False
                digest.update(b"directory_entry_limit_reached\\0")
                digest.update(directory.relative_to(root).as_posix().encode("utf-8", errors="surrogateescape"))
                continue
            entries.sort(key=lambda entry: entry.name, reverse=True)
        except OSError:
            complete = False
            digest.update(b"directory_read_error\\0")
            continue

        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(root).as_posix()
            # Exclude generated/dependency directories consistently with the repository
            # ignore rules. Evidence artifacts are generated by the verifier itself;
            # including them would make the fingerprint self-referential.
            parts = Path(relative).parts
            basename = parts[-1] if parts else ""
            if (
                relative == ".code-conductor/artifacts"
                or relative.startswith(".code-conductor/artifacts/")
                or any(part in _FILESYSTEM_EXCLUDED_DIRS for part in parts)
                or basename in _FILESYSTEM_EXCLUDED_FILES
                or basename.startswith(".env.")
                or basename.endswith((".log", ".zip"))
            ):
                continue
            scanned_entries += 1
            if scanned_entries > _MAX_UNTRACKED_FILES:
                complete = False
                digest.update(b"entry_limit_reached\\0")
                stack.clear()
                break

            digest.update(b"path\\0")
            digest.update(relative.encode("utf-8", errors="surrogateescape"))
            digest.update(b"\\0")
            try:
                metadata = entry.stat(follow_symlinks=False)
                digest.update(b"mode\\0")
                digest.update(str(metadata.st_mode).encode("ascii"))
                digest.update(b"\\0")
                if entry.is_symlink():
                    digest.update(b"symlink\\0")
                    digest.update(os.fsencode(os.readlink(path)))
                    continue
                if entry.is_dir(follow_symlinks=False):
                    digest.update(b"directory\\0")
                    stack.append(path)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    digest.update(b"special_file\\0")
                    complete = False
                    continue

                size = metadata.st_size
                digest.update(b"file\\0")
                digest.update(str(size).encode("ascii"))
                digest.update(b"\\0")
                if scanned_bytes + size > _MAX_UNTRACKED_BYTES:
                    digest.update(b"file_size_limit_reached\\0")
                    complete = False
                    continue
                with path.open("rb") as stream:
                    remaining = size
                    while remaining:
                        chunk = stream.read(min(1024 * 1024, remaining))
                        if not chunk:
                            complete = False
                            digest.update(b"short_read\\0")
                            break
                        digest.update(chunk)
                        remaining -= len(chunk)
                        scanned_bytes += len(chunk)
                after = path.stat(follow_symlinks=False)
                if (
                    after.st_size != metadata.st_size
                    or after.st_mtime_ns != metadata.st_mtime_ns
                    or after.st_mode != metadata.st_mode
                ):
                    complete = False
                    digest.update(b"file_changed_during_scan\\0")
                scanned_files += 1
            except OSError:
                complete = False
                digest.update(b"read_error\\0")

    return {
        "status": "captured" if complete else "partial",
        "captured_at": captured_at,
        "workspace_kind": "filesystem",
        "excluded_paths": [
            ".code-conductor/artifacts/**", ".git/**", ".venv/**", "venv/**",
            "node_modules/**", "__pycache__/**", ".pytest_cache/**", "dist/**",
            "build/**", "out/**", "coverage/**", "htmlcov/**", ".env*",
            "api_keys.json", "*.log", "*.zip",
        ],
        "snapshot_sha256": digest.hexdigest(),
        "snapshot_complete": complete,
        "files_hashed": scanned_files,
        "entries_scanned": scanned_entries,
        "bytes_hashed": scanned_bytes,
        "limits": {
            "max_entries": _MAX_UNTRACKED_FILES,
            "max_bytes": _MAX_UNTRACKED_BYTES,
        },
    }


def collect_workspace_provenance(workspace: str | Path) -> dict[str, Any]:
    """Return bounded, secret-safe workspace provenance without returning file contents."""
    captured_at = time.time()
    try:
        root = Path(workspace).resolve(strict=True)
        if not root.is_dir():
            raise ValueError("workspace_not_directory")
        repo_result = _git(root, "rev-parse", "--show-toplevel")
        if repo_result.returncode != 0:
            return _collect_filesystem_provenance(root, captured_at)
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