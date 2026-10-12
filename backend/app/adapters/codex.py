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
        if self._custom_executable:
            return self._custom_executable if os.path.exists(self._custom_executable) else None

        # 1. Check system PATH
        which_path = shutil.which("codex") or shutil.which("codex.exe") or shutil.which("codex.cmd")
        if which_path:
            return which_path

        # 2. Check standard Windows locations and VS Code extensions
        user_profile = os.environ.get("USERPROFILE", "")
        candidates = [
            os.path.join(user_profile, ".local", "bin", "codex.exe"),
            os.path.join(user_profile, "AppData", "Roaming", "npm", "codex.cmd"),
            os.path.join(user_profile, "AppData", "Local", "Programs", "codex", "codex.exe"),
            r"C:\Program Files\OpenAI\Codex\codex.exe",
        ]
        # Search VS Code extensions folder for bundled official codex.exe
        vscode_ext_pattern = os.path.join(
            user_profile, ".vscode", "extensions", "openai.chatgpt*", "bin", "windows-x86_64", "codex.exe"
        )
        import glob
        candidates.extend(glob.glob(vscode_ext_pattern))

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

    def has_api_key(self) -> bool:
        from app.core.config import get_unified_api_config
        cfg = get_unified_api_config()
        return bool(
            os.environ.get("OPENAI_API_KEY")
            or os.environ.get("CODEX_API_KEY")
            or cfg.get("openai", {}).get("api_key")
            or cfg.get("codex", {}).get("api_key")
        )

    async def check_availability(self) -> tuple[AvailabilityState, str]:
        exe = self.find_executable()
        if not exe:
            return (
                AvailabilityState.MISSING_CLI,
                "OpenAI Codex CLI is not installed or not found in PATH. "
                "Install it on Windows with: powershell -c \"irm https://chatgpt.com/codex/install.ps1 | iex\"",
            )

        has_auth = self.has_chatgpt_auth()
        has_key = self.has_api_key()

        if not has_auth and not has_key:
            return (
                AvailabilityState.UNAUTHENTICATED,
                f"Codex CLI found at '{exe}', but no active login detected in ~/.codex. "
                "Simply add OPENAI_API_KEY=... to your .env file, or run 'codex login' to link your ChatGPT subscription.",
            )

        mode_str = "using authenticated ChatGPT subscription" if has_auth else "using OPENAI_API_KEY from .env"
        return (
            AvailabilityState.READY,
            f"Codex CLI is ready at '{exe}' {mode_str}.",
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

        # Scope strictly to the validated workspace
        try:
            from app.safety.workspace import validate_workspace_path
            from app.core.config import get_settings
            workspace = validate_workspace_path(request.workspace_path, default_root=get_settings().workspace_root)
        except Exception as e:
            yield AgentEvent(
                task_id=task_id,
                event_type=EventType.ERROR,
                text=f"Invalid workspace path: {str(e)}",
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
        # Build non-interactive command line using official Codex CLI exec syntax
        cmd = [exe, "exec", "-C", str(workspace), "--skip-git-repo-check"]
        if request.model and request.model != "default":
            cmd.extend(["-m", request.model])
        cmd.append(request.prompt)

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

        def spawn_proc():
            return subprocess.Popen(
                cmd,
                cwd=str(workspace),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
            )

        try:
            proc = await asyncio.to_thread(spawn_proc)
            self._active_processes[task_id] = proc

            # Stream stdout lines
            while True:
                line = await asyncio.to_thread(proc.stdout.readline)
                if not line:
                    break
                if cancel_event.is_set():
                    if os.name == "nt" and proc.pid:
                        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
                    else:
                        proc.kill()
                    yield AgentEvent(
                        task_id=task_id,
                        event_type=EventType.CANCELLED,
                        text="Codex execution cancelled.",
                        timestamp=time.time(),
                    )
                    return
                yield AgentEvent(
                    task_id=task_id,
                    event_type=EventType.TOKEN,
                    text=line,
                    timestamp=time.time(),
                )

            await asyncio.to_thread(proc.wait)

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
                stderr_output = await asyncio.to_thread(proc.stderr.read)
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
                text=f"Process launch error: {type(e).__name__}: {str(e)}",
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
