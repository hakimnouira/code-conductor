import difflib
import fnmatch
import os
import tempfile
from pathlib import Path


class SafetyError(Exception):
    """Base exception for safety violations."""
    pass


class PathTraversalError(SafetyError):
    """Raised when an operation attempts to escape the workspace root."""
    pass


class SensitiveFileBlockedError(SafetyError):
    """Raised when an operation accesses a protected or sensitive file."""
    pass


DEFAULT_BLOCKED_PATTERNS = [
    ".env*",
    "*.env",
    "*.pem",
    "*.key",
    "*.pfx",
    "*.p12",
    "*.pkcs12",
    "id_rsa*",
    "id_ecdsa*",
    "id_ed25519*",
    "id_dsa*",
    "*api_keys*.json",
    "*credential*",
    "*secret*",
    ".git/*",
    ".git",
    "node_modules/*",
    ".venv/*",
]


class WorkspaceSecurityManager:
    """
    Enforces safe workspace file operations:
    - Canonical path resolution preventing path traversal.
    - Blocklist of sensitive credentials, certificates, and internal files.
    - Atomic writes with unified diff tracking.
    """

    def __init__(
        self,
        workspace_root: str | Path,
        blocked_patterns: list[str] | None = None,
    ):
        self.workspace_root = Path(workspace_root).resolve()
        self.blocked_patterns = (
            list(blocked_patterns) if blocked_patterns is not None else list(DEFAULT_BLOCKED_PATTERNS)
        )

    def is_sensitive(self, rel_path: str) -> bool:
        """Checks if a relative path matches any blocked pattern."""
        # Normalize slashes for glob matching
        normalized = rel_path.replace("\\", "/")
        parts = normalized.split("/")

        for pattern in self.blocked_patterns:
            pat_norm = pattern.replace("\\", "/")
            # Check full relative path
            if fnmatch.fnmatch(normalized.lower(), pat_norm.lower()):
                return True
            # Check filename only
            if parts and fnmatch.fnmatch(parts[-1].lower(), pat_norm.lower()):
                return True
            # Check any path segment
            for segment in parts:
                if fnmatch.fnmatch(segment.lower(), pat_norm.lower()):
                    return True
        return False

    def resolve_safe_path(self, raw_path: str | Path) -> Path:
        """
        Resolves a path to a canonical Path object and verifies it resides
        strictly within the workspace root and does not touch sensitive files.
        """
        input_path = Path(raw_path)
        if input_path.is_absolute():
            resolved = input_path.resolve()
        else:
            resolved = (self.workspace_root / input_path).resolve()

        # Check path traversal
        try:
            rel_path = resolved.relative_to(self.workspace_root)
        except ValueError:
            raise PathTraversalError(
                f"Access denied: '{raw_path}' resolves outside workspace root '{self.workspace_root}'"
            )

        # On Windows, verify case-insensitive prefix match as well
        if not str(resolved).lower().startswith(str(self.workspace_root).lower()):
            raise PathTraversalError(
                f"Access denied: '{raw_path}' escapes workspace root '{self.workspace_root}'"
            )

        # Check sensitive file pattern
        rel_str = str(rel_path).replace("\\", "/")
        if self.is_sensitive(rel_str):
            raise SensitiveFileBlockedError(
                f"Access denied: File '{rel_str}' matches sensitive file protection rules."
            )

        return resolved

    def read_file(self, raw_path: str | Path) -> str:
        """Reads file content safely."""
        target_path = self.resolve_safe_path(raw_path)
        if not target_path.exists():
            raise FileNotFoundError(f"File not found: '{raw_path}'")
        if not target_path.is_file():
            raise IsADirectoryError(f"Target '{raw_path}' is a directory, not a file.")

        return target_path.read_text(encoding="utf-8", errors="replace")

    def write_file(
        self,
        raw_path: str | Path,
        content: str,
        atomic: bool = True,
    ) -> tuple[int, str]:
        """
        Safely writes content to a file.
        Returns:
            (bytes_written, unified_diff_string)
        """
        target_path = self.resolve_safe_path(raw_path)
        rel_path = str(target_path.relative_to(self.workspace_root)).replace("\\", "/")

        old_content = ""
        if target_path.exists() and target_path.is_file():
            old_content = target_path.read_text(encoding="utf-8", errors="replace")

        # Generate unified diff
        diff_lines = list(
            difflib.unified_diff(
                old_content.splitlines(keepends=True),
                content.splitlines(keepends=True),
                fromfile=f"a/{rel_path}",
                tofile=f"b/{rel_path}",
            )
        )
        diff_str = "".join(diff_lines)

        # Ensure parent directories exist
        target_path.parent.mkdir(parents=True, exist_ok=True)

        content_bytes = content.encode("utf-8")

        if atomic:
            # Write to a temp file in the same directory, then atomic replace
            fd, tmp_file = tempfile.mkstemp(
                dir=str(target_path.parent),
                prefix=f".{target_path.name}.tmp-",
            )
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(content_bytes)
                os.replace(tmp_file, str(target_path))
            except Exception:
                if os.path.exists(tmp_file):
                    try:
                        os.remove(tmp_file)
                    except OSError:
                        pass
                raise
        else:
            target_path.write_bytes(content_bytes)

        return len(content_bytes), diff_str

    def list_safe_files(self, subpath: str = "", max_files: int = 500) -> list[str]:
        """
        Lists safe files within workspace or subfolder, excluding ignored and sensitive files.
        """
        start_dir = self.resolve_safe_path(subpath) if subpath else self.workspace_root
        if not start_dir.is_dir():
            return []

        safe_files = []
        for root, dirs, files in os.walk(str(start_dir)):
            # Prune ignored directories in-place
            dirs[:] = [
                d for d in dirs
                if d not in {".git", ".venv", "node_modules", "__pycache__", ".vscode", ".idea"}
                and not self.is_sensitive(f"{d}/")
            ]

            for file_name in files:
                abs_f = Path(root) / file_name
                try:
                    rel_f = str(abs_f.relative_to(self.workspace_root)).replace("\\", "/")
                    if not self.is_sensitive(rel_f):
                        safe_files.append(rel_f)
                        if len(safe_files) >= max_files:
                            return safe_files
                except ValueError:
                    continue

        return safe_files
