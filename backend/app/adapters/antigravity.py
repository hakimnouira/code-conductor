import asyncio
import os
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


class AntigravityAdapter(BaseAgentAdapter):
    """
    Google Antigravity IDE agent adapter connecting strictly to the local
    Antigravity IDE agentapi process. Only reports READY if a real session
    is active; never disguises cloud API keys as Antigravity responses.
    """

    def __init__(self, agentapi_executable: str | None = None):
        self._custom_executable = agentapi_executable
        self._active_cancels: dict[str, asyncio.Event] = {}

    @property
    def agent_id(self) -> str:
        return "antigravity"

    @property
    def name(self) -> str:
        return "Google Antigravity IDE"

    @property
    def description(self) -> str:
        return (
            "Direct interface to local Google Antigravity IDE agent session "
            "(requires running Antigravity IDE with an active project)."
        )

    def find_agentapi_bat(self) -> str | None:
        if self._custom_executable and os.path.exists(self._custom_executable):
            return self._custom_executable

        user_profile = os.environ.get("USERPROFILE", "")
        candidates = [
            os.path.join(user_profile, ".gemini", "antigravity", "bin", "agentapi.bat"),
            os.path.join(user_profile, ".gemini", "antigravity-ide", "bin", "agentapi.bat"),
        ]
        for c in candidates:
            if os.path.exists(c):
                return c
        return None

    def _probe_connection(self, agentapi_path: str) -> tuple[bool, str]:
        """Probes whether agentapi is connected to an active IDE project session."""
        try:
            proc = subprocess.run(
                [agentapi_path, "--help"],
                capture_output=True,
                text=True,
                timeout=4,
            )
            if proc.returncode != 0:
                return False, f"agentapi CLI returned code {proc.returncode}."

            # Verify if project environment is configured
            test_proc = subprocess.run(
                [agentapi_path, "get-conversation-metadata", "__probe__"],
                capture_output=True,
                text=True,
                timeout=4,
            )
            out = (test_proc.stdout + test_proc.stderr).lower()
            if "project_id is required" in out or "rpc error" in out:
                return False, "IDE project environment not initialized (open workspace in Antigravity IDE)."

            return True, "Connected to active Antigravity IDE session."
        except Exception as e:
            return False, f"Agent probe error: {str(e)}"

    async def check_availability(self) -> tuple[AvailabilityState, str]:
        agentapi = self.find_agentapi_bat()
        if not agentapi:
            return (
                AvailabilityState.DISABLED,
                "Google Antigravity agentapi CLI not found in ~/.gemini/antigravity/bin. Launch Antigravity IDE to install.",
            )

        is_connected, msg = await asyncio.to_thread(self._probe_connection, agentapi)
        if is_connected:
            return (
                AvailabilityState.READY,
                "Google Antigravity IDE agent session is active.",
            )

        return (
            AvailabilityState.DISABLED,
            f"Google Antigravity IDE session unavailable: {msg}",
        )

    async def get_capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(
            supports_streaming=True,
            supports_code_edits=True,
            supports_terminal_execution=False,
            supports_subagents=True,
            supported_models=["antigravity-default", "gemini-2.5-pro", "gemini-2.5-flash"],
        )

    async def stream_task(self, task_id: str, request: TaskCreateRequest) -> AsyncIterator[AgentEvent]:
        state, diag = await self.check_availability()
        if state != AvailabilityState.READY:
            yield AgentEvent(
                task_id=task_id,
                event_type=EventType.ERROR,
                text=f"Antigravity unavailable: {diag}",
                timestamp=time.time(),
            )
            return

        agentapi = self.find_agentapi_bat()
        if not agentapi:
            yield AgentEvent(
                task_id=task_id,
                event_type=EventType.ERROR,
                text="Antigravity agentapi CLI path could not be resolved.",
                timestamp=time.time(),
            )
            return

        cancel_event = asyncio.Event()
        self._active_cancels[task_id] = cancel_event

        yield AgentEvent(
            task_id=task_id,
            event_type=EventType.STARTED,
            text="Google Antigravity task started in local IDE session.",
            timestamp=time.time(),
        )

        cmd = [agentapi, "new-conversation", request.prompt]
        if request.model and request.model in ["flash_lite", "flash", "pro"]:
            cmd = [agentapi, "new-conversation", f"--model={request.model}", request.prompt]

        try:
            proc = await asyncio.to_thread(
                lambda: subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
            )

            while True:
                line = await asyncio.to_thread(proc.stdout.readline)
                if not line:
                    break
                if cancel_event.is_set():
                    proc.kill()
                    yield AgentEvent(
                        task_id=task_id,
                        event_type=EventType.CANCELLED,
                        text="Antigravity execution cancelled.",
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
            if proc.returncode == 0:
                yield AgentEvent(
                    task_id=task_id,
                    event_type=EventType.COMPLETED,
                    text="Antigravity task completed successfully.",
                    timestamp=time.time(),
                )
            else:
                stderr_text = await asyncio.to_thread(proc.stderr.read)
                yield AgentEvent(
                    task_id=task_id,
                    event_type=EventType.ERROR,
                    text=f"Antigravity error: {stderr_text.strip()}",
                    timestamp=time.time(),
                )
        except Exception as e:
            yield AgentEvent(
                task_id=task_id,
                event_type=EventType.ERROR,
                text=f"Failed to execute Antigravity session: {str(e)}",
                timestamp=time.time(),
            )
        finally:
            self._active_cancels.pop(task_id, None)

    async def cancel_task(self, task_id: str) -> bool:
        event = self._active_cancels.get(task_id)
        if event:
            event.set()
            return True
        return False
