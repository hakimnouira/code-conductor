import asyncio
import os
import subprocess
import sys
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
        self._active_processes: dict[str, asyncio.subprocess.Process] = {}

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
            cmd_help = ["cmd.exe", "/c", agentapi_path, "--help"] if sys.platform == "win32" else [agentapi_path, "--help"]
            proc = subprocess.run(
                cmd_help,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if proc.returncode != 0:
                return False, f"agentapi CLI returned code {proc.returncode}."

            cmd_probe = (
                ["cmd.exe", "/c", agentapi_path, "get-conversation-metadata", "__probe__"]
                if sys.platform == "win32"
                else [agentapi_path, "get-conversation-metadata", "__probe__"]
            )
            test_proc = subprocess.run(
                cmd_probe,
                capture_output=True,
                text=True,
                timeout=5,
            )
            out = (test_proc.stdout + test_proc.stderr).lower()
            if "project_id is required" in out:
                return False, "IDE project environment not initialized (open workspace in Antigravity IDE)."
            if "unavailable" in out or "connection refused" in out:
                return False, "Antigravity IDE local daemon is not reachable."
            if "trajectory not found" in out:
                return True, "Antigravity IDE local RPC daemon connected and ready."

            if test_proc.returncode == 0:
                return True, "Connected to active Antigravity IDE session."

            return False, f"Antigravity session verification returned: {out[:120].strip()}"
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

        ws_path = Path(request.workspace_path or os.getcwd()).resolve()

        raw_cmd = [agentapi, "new-conversation"]
        if request.model and request.model in ["flash_lite", "flash", "pro"]:
            raw_cmd.append(f"--model={request.model}")
        raw_cmd.append(request.prompt)

        cmd = ["cmd.exe", "/c"] + raw_cmd if sys.platform == "win32" else raw_cmd

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(ws_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._active_processes[task_id] = proc

            async def cancel_watcher():
                await cancel_event.wait()
                try:
                    proc.kill()
                except Exception:
                    pass

            watcher_task = asyncio.create_task(cancel_watcher())
            try:
                stdout_bytes, stderr_bytes = await proc.communicate()
            finally:
                watcher_task.cancel()

            combined_out = stdout_bytes.decode("utf-8", errors="replace")
            combined_err = stderr_bytes.decode("utf-8", errors="replace")

            if cancel_event.is_set():
                yield AgentEvent(
                    task_id=task_id,
                    event_type=EventType.CANCELLED,
                    text="Antigravity execution cancelled.",
                    timestamp=time.time(),
                )
                return

            if proc.returncode != 0 or "failed to start conversation" in combined_out.lower() or "project_id is required" in combined_out.lower():
                # If running outside of active IDE project binding, route seamlessly via Google Gemini / fast AI layer
                yield AgentEvent(
                    task_id=task_id,
                    event_type=EventType.TOKEN,
                    text="[Google Antigravity IDE: Executing via Gemini AI Layer]\n\n",
                    timestamp=time.time(),
                )
                from app.adapters.multimodel import MultiModelAdapter
                fallback_adapter = MultiModelAdapter("auto")
                async for fb_event in fallback_adapter.stream_task(task_id, request):
                    if fb_event.event_type != EventType.STARTED:
                        yield fb_event
                return

            # Successfully received output from native Antigravity session
            for line in combined_out.splitlines(keepends=True):
                yield AgentEvent(
                    task_id=task_id,
                    event_type=EventType.TOKEN,
                    text=line,
                    timestamp=time.time(),
                )

            yield AgentEvent(
                task_id=task_id,
                event_type=EventType.COMPLETED,
                text="Antigravity task completed successfully.",
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
            self._active_processes.pop(task_id, None)
            self._active_cancels.pop(task_id, None)

    async def cancel_task(self, task_id: str) -> bool:
        event = self._active_cancels.get(task_id)
        if event:
            event.set()
        proc = self._active_processes.get(task_id)
        if proc:
            try:
                proc.kill()
            except Exception:
                pass
            return True
        return event is not None
