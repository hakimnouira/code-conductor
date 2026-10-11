import asyncio
import os
import shutil
import subprocess
import time
from collections.abc import AsyncIterator
from pathlib import Path
from app.adapters.base import BaseAgentAdapter
from app.models.schemas import (
    AgentCapabilities,
    AgentEvent,
    AvailabilityState,
    EventType,
    TaskCreateRequest,
)

class CodexAdapter(BaseAgentAdapter):
    """OpenAI Codex CLI adapter reusing existing ChatGPT subscription authentication."""

    def __init__(self, executable_path: str | None = None):
        self._custom_executable = executable_path
        self._active_processes: dict[str, asyncio.subprocess.Process] = {}
        self._active_cancels: dict[str, asyncio.Event] = {}

    @property
    def agent_id(self) -> str:
        return "codex"

    @property
    def name(self) -> str:
        return "OpenAI Codex CLI"

    @property
    def description(self) -> str:
        return "Autonomous coding agent reusing existing ChatGPT Plus/Team subscription without API keys."

    def find_executable(self) -> str | None:
        """Find the codex CLI executable in PATH or standard installation locations."""
        if self._custom_executable and os.path.exists(self._custom_executable):
            return self._custom_executable

        # 1. Check system PATH
        which_path = shutil.which("codex") or shutil.which("codex.exe") or shutil.which("codex.cmd")
        if which_path:
            return which_path

        # 2. Check standard Windows locations
        user_profile = os.environ.get("USERPROFILE", "")
        candidates = [
            os.path.join(user_profile, ".local", "bin", "codex.exe"),
            os.path.join(user_profile, "AppData", "Roaming", "npm", "codex.cmd"),
            os.path.join(user_profile, "AppData", "Local", "Programs", "codex", "codex.exe"),
            r"C:\Program Files\OpenAI\Codex\codex.exe",
        ]
        for candidate in candidates:
            if os.path.exists(candidate):
                return candidate

        return None

    def has_chatgpt_auth(self) -> bool:
        """Check if ~/.codex contains an existing authenticated session."""
        user_profile = os.environ.get("USERPROFILE", "")
        codex_dir = Path(user_profile) / ".codex"
        if not codex_dir.exists():
            return False

        # Look for session/auth artifacts created by `codex login`
        possible_auth_files = [
            codex_dir / "auth.json",
            codex_dir / "session.json",
            codex_dir / "config.toml",
            codex_dir / "credentials.json",
        ]
        for f in possible_auth_files:
            if f.exists() and f.stat().st_size > 0:
                return True

        return False

    async def check_availability(self) -> tuple[AvailabilityState, str]:
        exe = self.find_executable()
        if not exe:
            return (
                AvailabilityState.MISSING_CLI,
                "OpenAI Codex CLI is not installed or not found in PATH. "
                "Install it on Windows with: powershell -c \"irm https://chatgpt.com/codex/install.ps1 | iex\"",
            )

        if not self.has_chatgpt_auth():
            return (
                AvailabilityState.UNAUTHENTICATED,
                f"Codex CLI found at '{exe}', but no active ChatGPT login detected in ~/.codex. "
                "Please run 'codex login' in your terminal to authenticate your existing ChatGPT subscription.",
            )

        return (
            AvailabilityState.READY,
            f"Codex CLI is ready at '{exe}' using authenticated ChatGPT subscription (no API key required).",
        )

    async def get_capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(
            supports_streaming=True,
            supports_code_edits=True,
            supports_terminal_execution=True,
            supports_subagents=False,
            supported_models=["codex-default", "o4-mini", "gpt-4.1"],
        )

    async def stream_task(self, task_id: str, request: TaskCreateRequest) -> AsyncIterator[AgentEvent]:
        state, diag = await self.check_availability()
        if state != AvailabilityState.READY:
            yield AgentEvent(
                task_id=task_id,
                event_type=EventType.ERROR,
                text=f"Codex unavailable: {diag}",
                timestamp=time.time(),
            )
            return

        exe = self.find_executable()
        if not exe:
            yield AgentEvent(
                task_id=task_id,
                event_type=EventType.ERROR,
                text="Executable could not be resolved.",
                timestamp=time.time(),
            )
            return

        # Scope strictly to the requested workspace
        workspace = Path(request.workspace_path or os.getcwd()).resolve()
        if not workspace.exists():
            yield AgentEvent(
                task_id=task_id,
                event_type=EventType.ERROR,
                text=f"Workspace path does not exist: {workspace}",
                timestamp=time.time(),
            )
            return

        cancel_event = asyncio.Event()
        self._active_cancels[task_id] = cancel_event

        yield AgentEvent(
            task_id=task_id,
            event_type=EventType.STARTED,
            text=f"Codex task started. Scoped to workspace: {workspace}",
            timestamp=time.time(),
        )

        # Build non-interactive command line
        # Use exec non-interactive mode with workspace scoping
        cmd = [exe, "exec", "--cwd", str(workspace), request.prompt]
        if request.model and request.model != "default":
            cmd.extend(["--model", request.model])

        env = dict(os.environ)
        # Ensure we do NOT force an OPENAI_API_KEY if the user relies on their existing ChatGPT subscription
        if "OPENAI_API_KEY" in env and not self.has_chatgpt_auth():
            # Keep if that's all they have
            pass
        elif self.has_chatgpt_auth():
            # Prefer existing ChatGPT subscription credentials
            pass

        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(workspace),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=creationflags,
            )
            self._active_processes[task_id] = proc

            # Stream stdout lines
            async def read_stream(stream, is_stderr=False):
                while True:
                    line = await stream.readline()
                    if not line:
                        break
                    decoded = line.decode("utf-8", errors="replace")
                    yield AgentEvent(
                        task_id=task_id,
                        event_type=EventType.LOG if is_stderr else EventType.TOKEN,
                        text=decoded,
                        timestamp=time.time(),
                    )

            if proc.stdout:
                async for event in read_stream(proc.stdout, is_stderr=False):
                    if cancel_event.is_set():
                        yield AgentEvent(
                            task_id=task_id,
                            event_type=EventType.CANCELLED,
                            text="Codex execution cancelled.",
                            timestamp=time.time(),
                        )
                        return
                    yield event

            await proc.wait()

            if cancel_event.is_set():
                yield AgentEvent(
                    task_id=task_id,
                    event_type=EventType.CANCELLED,
                    text="Codex execution cancelled by user request.",
                    timestamp=time.time(),
                )
            elif proc.returncode == 0:
                yield AgentEvent(
                    task_id=task_id,
                    event_type=EventType.COMPLETED,
                    text="Codex task completed successfully.",
                    timestamp=time.time(),
                )
            else:
                stderr_output = ""
                if proc.stderr:
                    err_bytes = await proc.stderr.read()
                    stderr_output = err_bytes.decode("utf-8", errors="replace")
                yield AgentEvent(
                    task_id=task_id,
                    event_type=EventType.ERROR,
                    text=f"Codex exited with code {proc.returncode}. Details: {stderr_output}",
                    timestamp=time.time(),
                )

        except Exception as e:
            yield AgentEvent(
                task_id=task_id,
                event_type=EventType.ERROR,
                text=f"Process launch error: {str(e)}",
                timestamp=time.time(),
            )
        finally:
            self._active_processes.pop(task_id, None)
            self._active_cancels.pop(task_id, None)

    async def cancel_task(self, task_id: str) -> bool:
        cancel_event = self._active_cancels.get(task_id)
        if cancel_event:
            cancel_event.set()

        proc = self._active_processes.get(task_id)
        if proc and proc.pid:
            # On Windows, kill entire process tree to prevent orphaned worker or compiler processes
            if os.name == "nt":
                try:
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                    )
                except Exception:
                    pass
            else:
                try:
                    proc.kill()
                except Exception:
                    pass
            return True

        return False
