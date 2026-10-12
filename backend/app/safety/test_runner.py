import asyncio
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from app.models.schemas import TestRunResponse


class RunnerSecurityError(Exception):
    __test__ = False

# Backward compatibility alias
TestRunnerSecurityError = RunnerSecurityError

FORBIDDEN_ARG_CHARS = {";", "&", "|", "`", "$", "<", ">", "\n", "\r"}


class SafeTestRunner:
    """
    Executes workspace tests safely:
    - Predefined allowed runners (pytest, npm test, python).
    - Strict argument validation (no shell metacharacters, array exec only).
    - Enforced timeouts with process tree termination on Windows.
    """

    def __init__(self, workspace_root: str | Path):
        self.workspace_root = Path(workspace_root).resolve()

    def get_allowed_runners(self) -> dict[str, list[str]]:
        py_exe = sys.executable
        npm_cmd = "npm.cmd" if sys.platform == "win32" else "npm"
        npx_cmd = "npx.cmd" if sys.platform == "win32" else "npx"
        node_cmd = "node.exe" if sys.platform == "win32" else "node"
        npm_path = shutil.which(npm_cmd) or npm_cmd
        npx_path = shutil.which(npx_cmd) or npx_cmd
        node_path = shutil.which(node_cmd) or node_cmd

        return {
            "pytest": [py_exe, "-m", "pytest"],
            "unittest": [py_exe, "-m", "unittest"],
            "python": [py_exe],
            "npm_test": [npm_path, "test"],
            "vitest": [npx_path, "vitest", "run"],
            "node": [node_path],
        }

    def validate_args(self, args: list[str]) -> None:
        """Ensures arguments contain no forbidden shell metacharacters."""
        for arg in args:
            for char in FORBIDDEN_ARG_CHARS:
                if char in arg:
                    raise TestRunnerSecurityError(
                        f"Disallowed character '{char}' detected in test argument '{arg}'."
                    )

    async def run_tests(
        self,
        command_key: str = "pytest",
        extra_args: list[str] | None = None,
        timeout_seconds: int = 45,
    ) -> TestRunResponse:
        runners = self.get_allowed_runners()
        if command_key not in runners:
            raise TestRunnerSecurityError(
                f"Unknown test runner '{command_key}'. Allowed: {list(runners.keys())}"
            )

        base_cmd = list(runners[command_key])
        user_args = list(extra_args or [])
        self.validate_args(user_args)

        full_cmd = base_cmd + user_args
        cmd_display = " ".join(full_cmd)

        t_start = time.perf_counter()
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0

        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *full_cmd,
                cwd=str(self.workspace_root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=creationflags,
            )

            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(),
                timeout=float(timeout_seconds),
            )
            duration_ms = (time.perf_counter() - t_start) * 1000.0

            exit_code = proc.returncode if proc.returncode is not None else 0
            stdout_str = stdout_b.decode("utf-8", errors="replace")
            stderr_str = stderr_b.decode("utf-8", errors="replace")

            return TestRunResponse(
                command=cmd_display,
                exit_code=exit_code,
                passed=(exit_code == 0),
                stdout=stdout_str,
                stderr=stderr_str,
                duration_ms=round(duration_ms, 2),
            )

        except asyncio.TimeoutError:
            duration_ms = (time.perf_counter() - t_start) * 1000.0
            if proc and proc.pid:
                if sys.platform == "win32":
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                        capture_output=True,
                    )
                else:
                    try:
                        proc.kill()
                    except OSError:
                        pass

            return TestRunResponse(
                command=cmd_display,
                exit_code=-1,
                passed=False,
                stdout="",
                stderr=f"Test run timed out after {timeout_seconds} seconds.",
                duration_ms=round(duration_ms, 2),
            )
        except Exception as e:
            duration_ms = (time.perf_counter() - t_start) * 1000.0
            return TestRunResponse(
                command=cmd_display,
                exit_code=-1,
                passed=False,
                stdout="",
                stderr=f"Execution error: {str(e)}",
                duration_ms=round(duration_ms, 2),
            )
