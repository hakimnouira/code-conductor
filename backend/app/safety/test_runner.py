import asyncio
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from app.models.schemas import TestRunResponse


class RunnerSecurityError(Exception):
    __test__ = False

# Backward compatibility alias
TestRunnerSecurityError = RunnerSecurityError


async def _read_bounded(stream: asyncio.StreamReader, limit: int = 1_000_000) -> tuple[bytes, bool]:
    """Drain a subprocess pipe while retaining only its final bounded byte window."""
    retained = bytearray()
    total = 0
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            break
        total += len(chunk)
        retained.extend(chunk)
        if len(retained) > limit:
            del retained[:-limit]
    return bytes(retained), total > limit


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
            "npm_build": [npm_path, "run", "build"],
            "vitest": [npx_path, "vitest", "run"],
            "node": [node_path],
        }

    def _docker_sandbox_command(
        self,
        command_key: str,
        extra_args: list[str],
    ) -> tuple[list[str], str, str]:
        """Build a fail-closed Docker command with no network and bounded resources."""
        image = os.environ.get("CODE_CONDUCTOR_TEST_SANDBOX_IMAGE", "").strip()
        if not image or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]{0,200}", image):
            raise TestRunnerSecurityError(
                "Docker sandboxing requires CODE_CONDUCTOR_TEST_SANDBOX_IMAGE to name a valid, prebuilt image."
            )
        docker = shutil.which("docker")
        if not docker:
            raise TestRunnerSecurityError(
                "Docker sandboxing is enabled but the Docker CLI is unavailable; refusing to run tests on the host."
            )
        source = self.workspace_root.as_posix()
        if "," in source:
            raise TestRunnerSecurityError("Workspace paths containing commas cannot be mounted safely in Docker mode.")
        container_name = "code-conductor-test-" + uuid.uuid4().hex[:16]
        commands = {
            "pytest": ["python", "-m", "pytest"],
            "unittest": ["python", "-m", "unittest"],
            "python": ["python"],
            "npm_test": ["npm", "test"],
            "npm_build": ["npm", "run", "build"],
            "vitest": ["npx", "vitest", "run"],
            "node": ["node"],
        }
        inner_command = commands[command_key] + list(extra_args)
        command = [
            docker, "run", "--rm", "--pull=never", "--name", container_name,
            "--network", "none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit=256",
            "--memory=2g",
            "--memory-swap=2g",
            "--cpus=2",
            "--ulimit", "nofile=1024:1024",
            "--tmpfs", "/tmp:rw,nosuid,size=256m",
            # Provide disposable tool caches and a bounded temporary home instead of
            # forwarding host environment variables or mounting the host home directory.
            "--env", "HOME=/tmp",
            "--env", "TMPDIR=/tmp",
            "--env", "CI=1",
            "--env", "PYTHONDONTWRITEBYTECODE=1",
            "--env", "npm_config_cache=/tmp/npm-cache",
            "--mount", f"type=bind,source={source},target=/workspace,rw",
            "--workdir", "/workspace",
            image,
            *inner_command,
        ]
        return command, container_name, docker

    @staticmethod
    def _stop_docker_container(docker: str | None, container_name: str | None) -> None:
        if not docker or not container_name:
            return
        try:
            subprocess.run(
                [docker, "rm", "-f", container_name],
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass

    def discover_runner(self) -> str | None:
        """Select a likely test runner from explicit test configuration or test files.

        Merely having a generic pyproject.toml/setup.cfg is not evidence that pytest
        is configured; doing so can trigger a failing verification run in unrelated
        Python projects.
        """
        import json

        root = self.workspace_root
        if (root / "pytest.ini").is_file():
            return "pytest"

        pyproject = root / "pyproject.toml"
        if pyproject.is_file():
            try:
                text = pyproject.read_text(encoding="utf-8")
                if "[tool.pytest.ini_options]" in text:
                    return "pytest"
            except (OSError, UnicodeError):
                pass

        setup_cfg = root / "setup.cfg"
        if setup_cfg.is_file():
            try:
                text = setup_cfg.read_text(encoding="utf-8")
                if "[tool:pytest]" in text:
                    return "pytest"
            except (OSError, UnicodeError):
                pass

        tests_dir = root / "tests"
        if tests_dir.is_dir() and any(tests_dir.glob("test_*.py")):
            return "pytest"
        if any(root.glob("test_*.py")):
            return "pytest"

        package_file = root / "package.json"
        if package_file.is_file():
            try:
                package = json.loads(package_file.read_text(encoding="utf-8"))
                scripts = package.get("scripts", {})
                if isinstance(scripts, dict) and isinstance(scripts.get("test"), str) and scripts["test"].strip():
                    return "npm_test"
                if isinstance(scripts, dict) and isinstance(scripts.get("build"), str) and scripts["build"].strip():
                    return "npm_build"
            except (OSError, UnicodeError, json.JSONDecodeError):
                return None
        return None

    def discover_build_runner(self) -> str | None:
        """Discover a separately configured build check for JavaScript projects."""
        import json

        package_file = self.workspace_root / "package.json"
        if not package_file.is_file():
            return None
        try:
            package = json.loads(package_file.read_text(encoding="utf-8"))
            scripts = package.get("scripts", {})
            if isinstance(scripts, dict) and isinstance(scripts.get("build"), str) and scripts["build"].strip():
                return "npm_build"
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        return None

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

        user_args = list(extra_args or [])
        self.validate_args(user_args)
        execution_mode = os.environ.get("CODE_CONDUCTOR_TEST_SANDBOX", "host").strip().lower() or "host"
        container_name: str | None = None
        docker_executable: str | None = None
        if execution_mode == "docker":
            full_cmd, container_name, docker_executable = self._docker_sandbox_command(command_key, user_args)
        elif execution_mode == "host":
            full_cmd = list(runners[command_key]) + user_args
        else:
            raise TestRunnerSecurityError(
                "CODE_CONDUCTOR_TEST_SANDBOX must be 'host' or 'docker'."
            )
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
                start_new_session=(sys.platform != "win32"),
            )

            stdout_reader = asyncio.create_task(_read_bounded(proc.stdout))
            stderr_reader = asyncio.create_task(_read_bounded(proc.stderr))
            await asyncio.wait_for(
                asyncio.gather(proc.wait(), stdout_reader, stderr_reader),
                timeout=float(timeout_seconds),
            )
            (stdout_b, stdout_truncated) = stdout_reader.result()
            (stderr_b, stderr_truncated) = stderr_reader.result()
            duration_ms = (time.perf_counter() - t_start) * 1000.0

            exit_code = proc.returncode if proc.returncode is not None else 0
            stdout_str = stdout_b.decode("utf-8", errors="replace")
            stderr_str = stderr_b.decode("utf-8", errors="replace")
            if stdout_truncated:
                stdout_str = "[earlier stdout truncated]\n" + stdout_str
            if stderr_truncated:
                stderr_str = "[earlier stderr truncated]\n" + stderr_str

            return TestRunResponse(
                command=cmd_display,
                exit_code=exit_code,
                passed=(exit_code == 0),
                stdout=stdout_str,
                stderr=stderr_str,
                duration_ms=round(duration_ms, 2),
                execution_mode=execution_mode,
            )

        except asyncio.CancelledError:
            # Cancelling the parent task must not leave tests, browsers, or build
            # subprocesses running in the background.
            self._stop_docker_container(docker_executable, container_name)
            if proc and proc.pid:
                if sys.platform == "win32":
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                        capture_output=True,
                    )
                else:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except (OSError, ProcessLookupError):
                        try:
                            proc.kill()
                        except OSError:
                            pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=3)
                except (asyncio.TimeoutError, OSError):
                    pass
            raise
        except asyncio.TimeoutError:
            duration_ms = (time.perf_counter() - t_start) * 1000.0
            self._stop_docker_container(docker_executable, container_name)
            if proc and proc.pid:
                if sys.platform == "win32":
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                        capture_output=True,
                    )
                else:
                    try:
                        # Kill the whole POSIX process group, not just the direct
                        # test runner, so spawned browser/build children do not leak.
                        os.killpg(proc.pid, signal.SIGKILL)
                    except (OSError, ProcessLookupError):
                        try:
                            proc.kill()
                        except OSError:
                            pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=3)
                except (asyncio.TimeoutError, OSError):
                    pass

            return TestRunResponse(
                command=cmd_display,
                exit_code=-1,
                passed=False,
                stdout="",
                stderr=f"Test run timed out after {timeout_seconds} seconds.",
                duration_ms=round(duration_ms, 2),
                execution_mode=execution_mode,
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
                execution_mode=execution_mode,
            )