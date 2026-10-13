"""Bounded, high-confidence secret-pattern scan for files changed during a task."""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any


_EXCLUDED_DIRS = {
    ".git", ".venv", "venv", "node_modules", ".code-conductor", "__pycache__",
    "dist", "build", "coverage", ".next", ".turbo", "vendor", ".pytest_cache",
}
_TEXT_SUFFIXES = {
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".json",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".env", ".sh", ".ps1", ".bat",
    ".md", ".txt", ".xml", ".html", ".css", ".sql", ".properties",
}
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key_header", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("aws_access_key_id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,})\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
    ("openai_style_key", re.compile(r"\bsk-[A-Za-z0-9]{40,}\b")),
)
_MAX_FILES = 3000
_MAX_FILE_BYTES = 2 * 1024 * 1024
_MAX_TOTAL_BYTES = 25 * 1024 * 1024
_MAX_FINDINGS = 50


def scan_recent_secrets(workspace: str | Path, started_at: float | None) -> dict[str, Any]:
    """Scan only recent text-file changes; never return matched secret text."""
    root = Path(workspace).resolve()
    cutoff = max(0.0, float(started_at or time.time()) - 2.0)
    scanned = 0
    total_bytes = 0
    findings: list[dict[str, Any]] = []
    truncated = False

    for current, dirs, filenames in os.walk(root):
        dirs[:] = [name for name in dirs if name.lower() not in _EXCLUDED_DIRS]
        for filename in filenames:
            path = Path(current) / filename
            if not (path.suffix.lower() in _TEXT_SUFFIXES or filename.lower().startswith(".env")):
                continue
            try:
                stat = path.stat()
                if not path.is_file() or stat.st_mtime < cutoff:
                    continue
                if stat.st_size > _MAX_FILE_BYTES or total_bytes + stat.st_size > _MAX_TOTAL_BYTES:
                    truncated = True
                    continue
                raw = path.read_bytes()
            except (OSError, PermissionError):
                continue

            scanned += 1
            total_bytes += len(raw)
            text = raw.decode("utf-8", errors="ignore")
            try:
                relative = path.relative_to(root).as_posix()
            except ValueError:
                continue
            for line_number, line in enumerate(text.splitlines(), start=1):
                for label, pattern in _PATTERNS:
                    if pattern.search(line):
                        findings.append({
                            "file": relative[:500],
                            "line": line_number,
                            "pattern": label,
                        })
                        if len(findings) >= _MAX_FINDINGS:
                            truncated = True
                            break
                if len(findings) >= _MAX_FINDINGS:
                    break
            if scanned >= _MAX_FILES or len(findings) >= _MAX_FINDINGS:
                truncated = True
                break
        if scanned >= _MAX_FILES or len(findings) >= _MAX_FINDINGS:
            break

    return {
        "status": "warning" if findings else ("passed" if scanned else "skipped"),
        "scanned_files": scanned,
        "bytes_scanned": total_bytes,
        "findings": findings,
        "truncated": truncated,
        "reason": "no_recent_text_changes" if not scanned else "",
    }
