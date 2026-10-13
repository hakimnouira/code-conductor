import difflib
import fnmatch
import os
import tempfile
from pathlib import Path


class SafetyError(Exception):
    """Base exception for safety violations."""
    pass


class WorkspaceValidationError(SafetyError):
    """Raised when a workspace path is invalid, non-existent, or escapes allowed boundaries."""
    pass


class PathTraversalError(SafetyError):
    """Raised when an operation attempts to escape the workspace root."""
    pass


class SensitiveFileBlockedError(SafetyError):
    """Raised when an operation accesses a protected or sensitive file."""
    pass


FORBIDDEN_SYSTEM_ROOTS = [
    Path(os.environ.get("SystemRoot", "C:\\Windows")).resolve(),
    Path(os.environ.get("ProgramFiles", "C:\\Program Files")).resolve(),
    Path(os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)")).resolve(),
    Path("/etc").resolve(),
    Path("/usr").resolve(),
    Path("/bin").resolve(),
    Path("/sbin").resolve(),
]



def is_contained_in_workspace(target: str | Path | None, workspace_root: str | Path | None) -> bool:
    """
    Canonical path containment check.
    Guarantees that target resolves strictly inside workspace_root (or is workspace_root itself).
    Handles:
    - Sibling directories with identical textual prefixes (e.g. /workspace vs /workspace-evil)
    - Symlinks pointing outside workspace
    - Nonexistent destinations within workspace
    - Windows drive differences (e.g. C: vs D:)
    - Path traversal attempts (../../)
    Rejects any path whose containment cannot be established.
    """
    if not target or not workspace_root:
        return False

    try:
        root_path = Path(workspace_root).resolve()
        t_path = Path(target)
        if not t_path.is_absolute():
            t_path = root_path / t_path
        resolved_target = t_path.resolve()

        # Check drive letter on Windows / multi-drive systems
        if root_path.drive or resolved_target.drive:
            if root_path.drive.lower() != resolved_target.drive.lower():
                return False

        # Use os.path.normcase for case-insensitive platform comparison (Windows)
        norm_root = Path(os.path.normcase(str(root_path)))
        norm_target = Path(os.path.normcase(str(resolved_target)))

        if norm_target == norm_root:
            return True

        rel = norm_target.relative_to(norm_root)
        rel_str = str(rel)
        if rel_str == ".." or rel_str.startswith(".." + os.sep) or rel_str.startswith("../"):
            return False
        return True
    except (ValueError, RuntimeError, OSError, TypeError):
        return False


def validate_workspace_path(
    path_str: str | Path | None,
    default_root: str | Path | None = None,
    allowed_roots: list[str | Path] | None = None,
) -> Path:
    """
    Validates that a workspace path:
    1. Is non-empty, falling back to default_root if not provided.
    2. Resolves to a canonical existing directory.
    3. Does not point to critical OS system directories or root drives.
    4. Lies within allowed roots if allowed_roots is specified.
    """
    target = path_str or default_root
    if not target:
        raise WorkspaceValidationError("No workspace path specified and no default workspace is configured.")

    try:
        resolved = Path(target).resolve()
    except Exception as e:
        raise WorkspaceValidationError(f"Invalid workspace path '{target}': {str(e)}")

    if not resolved.exists():
        raise WorkspaceValidationError(f"Workspace path does not exist: '{resolved}'")
    if not resolved.is_dir():
        raise WorkspaceValidationError(f"Workspace path is not a directory: '{resolved}'")

    # Reject root drive
    if resolved.parent == resolved:
        raise WorkspaceValidationError(f"Root drive '{resolved}' cannot be used directly as a workspace.")

    # Reject critical system directories using canonical containment
    for sys_root in FORBIDDEN_SYSTEM_ROOTS:
        if resolved == sys_root or is_contained_in_workspace(resolved, sys_root):
            raise WorkspaceValidationError(f"Forbidden system directory '{resolved}' cannot be used as a workspace.")

    # If allowed_roots are configured, enforce boundary containment
    if allowed_roots:
        is_allowed = any(
            is_contained_in_workspace(resolved, ar)
            for ar in allowed_roots
        )
        if not is_allowed:
            resolved_allowed = [str(Path(r).resolve()) for r in allowed_roots]
            raise WorkspaceValidationError(
                f"Workspace '{resolved}' is not within configured allowed workspace roots: {resolved_allowed}"
            )

    return resolved



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
    - Canonical path resolution supporting workspace directories and user disk paths.
    - Protection of critical OS directories and sensitive credential files.
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
        normalized = rel_path.replace("\\", "/")
        parts = normalized.split("/")

        for pattern in self.blocked_patterns:
            pat_norm = pattern.replace("\\", "/")
            if fnmatch.fnmatch(normalized.lower(), pat_norm.lower()):
                return True
            if parts and fnmatch.fnmatch(parts[-1].lower(), pat_norm.lower()):
                return True
            for segment in parts:
                if fnmatch.fnmatch(segment.lower(), pat_norm.lower()):
                    return True
        return False

    def resolve_safe_path(self, raw_path: str | Path) -> Path:
        """
        Resolves a path to a canonical Path object and verifies it resides
        within workspace or valid user disks, protecting OS system folders.
        """
        input_path = Path(raw_path)
        if input_path.is_absolute():
            resolved = input_path.resolve()
        else:
            resolved = (self.workspace_root / input_path).resolve()
            # Relative paths are always scoped to the selected workspace. This also
            # blocks symlinks inside the workspace that resolve to an outside target.
            try:
                resolved.relative_to(self.workspace_root)
            except ValueError as exc:
                raise PathTraversalError(
                    f"Access denied: relative path '{raw_path}' escapes workspace '{self.workspace_root}'."
                ) from exc

        # Reject OS system directories (C:\Windows, etc.)
        for sys_root in FORBIDDEN_SYSTEM_ROOTS:
            if resolved == sys_root or is_contained_in_workspace(resolved, sys_root):
                raise PathTraversalError(
                    f"Access denied: '{raw_path}' resolves inside protected system directory '{sys_root}'"
                )

        # Check sensitive file pattern
        try:
            rel_path = resolved.relative_to(self.workspace_root)
            rel_str = str(rel_path).replace("\\", "/")
            if self.is_sensitive(rel_str):
                raise SensitiveFileBlockedError(
                    f"Access denied: File '{rel_str}' matches sensitive file protection rules."
                )
        except ValueError:
            # Cross-drive or absolute path outside workspace_root
            if self.is_sensitive(resolved.name):
                raise SensitiveFileBlockedError(
                    f"Access denied: File '{resolved.name}' matches sensitive file protection rules."
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
        Safely writes content to a file across workspace or user disks.
        Returns:
            (bytes_written, unified_diff_string)
        """
        target_path = self.resolve_safe_path(raw_path)
        try:
            rel_path = str(target_path.relative_to(self.workspace_root)).replace("\\", "/")
        except ValueError:
            rel_path = str(target_path).replace("\\", "/")

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
