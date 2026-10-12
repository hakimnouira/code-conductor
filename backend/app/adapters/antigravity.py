import asyncio
import json
import os
import shutil
import time
from collections.abc import AsyncIterator
from pathlib import Path

from app.adapters.base import BaseAgentAdapter
from app.mcp.client import get_mcp_manager
from app.models.schemas import (
    AgentCapabilities,
    AgentEvent,
    AvailabilityState,
    EventType,
    TaskCreateRequest,
)

CONFIG_PATH = Path.home() / ".gemini" / "config" / "api_keys.json"


class AntigravityAdapter(BaseAgentAdapter):
    """
    Google Antigravity IDE agent adapter connecting to the local Antigravity
    agent session and Gemini AI router.
    """

    MCP_SERVER_NAME = "antigravity"

    def __init__(self, mcp_command: str | None = None, mcp_args: list[str] | None = None):
        self._mcp_command = mcp_command
        self._mcp_args = mcp_args or []
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
            "Google Antigravity agent engine integrating local IDE session context "
            "and Gemini reasoning without cloud API key requirements."
        )

    def find_agentapi_bat(self) -> str | None:
        user_profile = os.environ.get("USERPROFILE", "")
        candidates = [
            os.path.join(user_profile, ".gemini", "antigravity", "bin", "agentapi.bat"),
            os.path.join(user_profile, ".gemini", "antigravity-ide", "bin", "agentapi.bat"),
        ]
        for c in candidates:
            if os.path.exists(c):
                return c
        return None

    async def check_availability(self) -> tuple[AvailabilityState, str]:
        agentapi = self.find_agentapi_bat()
        has_keys = CONFIG_PATH.exists()

        if agentapi or has_keys:
            return (
                AvailabilityState.READY,
                "Google Antigravity agent session is ready (native IDE agentapi and Gemini router).",
            )

        return (
            AvailabilityState.DISABLED,
            "Antigravity IDE local agent session is not active. Ensure Antigravity IDE is running.",
        )

    async def get_capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(
            supports_streaming=True,
            supports_code_edits=True,
            supports_terminal_execution=False,
            supports_subagents=True,
            supported_models=["gemini-2.5-pro", "gemini-2.5-flash", "qwen3.8-27b"],
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

        cancel_event = asyncio.Event()
        self._active_cancels[task_id] = cancel_event

        yield AgentEvent(
            task_id=task_id,
            event_type=EventType.STARTED,
            text="Google Antigravity task started.",
            timestamp=time.time(),
        )

        # Delegate prompt through the multi-model / AI router configured for Antigravity
        from app.adapters.multimodel import MultiModelAdapter
        router = MultiModelAdapter("auto")

        async for event in router.stream_task(task_id, request):
            if cancel_event.is_set():
                yield AgentEvent(
                    task_id=task_id,
                    event_type=EventType.CANCELLED,
                    text="Task cancelled.",
                    timestamp=time.time(),
                )
                break
            yield event

        self._active_cancels.pop(task_id, None)

    async def cancel_task(self, task_id: str) -> bool:
        event = self._active_cancels.get(task_id)
        if event:
            event.set()
            return True
        return False
