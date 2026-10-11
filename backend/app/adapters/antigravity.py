import asyncio
import os
import shutil
import time
from collections.abc import AsyncIterator
from app.adapters.base import BaseAgentAdapter
from app.mcp.client import MCPServerConfig, get_mcp_manager
from app.models.schemas import (
    AgentCapabilities,
    AgentEvent,
    AvailabilityState,
    EventType,
    TaskCreateRequest,
)

class AntigravityAdapter(BaseAgentAdapter):
    """Antigravity IDE agent adapter using standard Model Context Protocol (MCP)."""

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
        return "Google Antigravity IDE (MCP)"

    @property
    def description(self) -> str:
        return "Local Antigravity IDE agent engine accessed via standard MCP tool interface without external API keys."

    def find_mcp_binary(self) -> str | None:
        if self._mcp_command and (os.path.exists(self._mcp_command) or shutil.which(self._mcp_command)):
            return self._mcp_command

        # Check standard CLI names
        which = shutil.which("antigravity-mcp") or shutil.which("agy")
        if which:
            return which

        user_profile = os.environ.get("USERPROFILE", "")
        candidates = [
            os.path.join(user_profile, ".local", "bin", "antigravity-mcp.exe"),
            os.path.join(user_profile, "AppData", "Roaming", "npm", "antigravity-mcp.cmd"),
        ]
        for c in candidates:
            if os.path.exists(c):
                return c
        return None

    async def check_availability(self) -> tuple[AvailabilityState, str]:
        mcp_manager = get_mcp_manager()
        server = mcp_manager.get_server(self.MCP_SERVER_NAME)

        # Check if an MCP server was explicitly registered or discovered
        if not server:
            binary = self.find_mcp_binary()
            if binary:
                # Auto-register found binary
                mcp_manager.register_server(MCPServerConfig(
                    name=self.MCP_SERVER_NAME,
                    command=binary,
                    args=self._mcp_args,
                ))
                server = mcp_manager.get_server(self.MCP_SERVER_NAME)

        if server:
            diag = await mcp_manager.get_server_diagnostics(self.MCP_SERVER_NAME)
            if diag.status == "connected":
                return (
                    AvailabilityState.READY,
                    f"Antigravity MCP server connected successfully ({diag.tool_count} tools available).",
                )
            else:
                return (
                    AvailabilityState.ERROR,
                    f"Antigravity MCP server failed to connect: {diag.error}",
                )

        # If no MCP server binary exists or language server is not exposed via standard MCP
        return (
            AvailabilityState.DISABLED,
            "Antigravity IDE local agent MCP integration is currently disabled/unreachable. "
            "The installed Antigravity IDE does not expose an open standard MCP stdio endpoint by default. "
            "To enable: "
            "1. Install antigravity-mcp: 'pip install git+https://github.com/Kio-pon/antigravity-mcp.git' "
            "2. Ensure Antigravity IDE is open and signed in. "
            "(Private internal CSRF endpoints will not be scraped without supported integration).",
        )

    async def get_capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(
            supports_streaming=True,
            supports_code_edits=True,
            supports_terminal_execution=False,
            supports_subagents=True,
            supported_models=["gemini-2.5-pro", "gemini-2.5-flash"],
        )

    async def stream_task(self, task_id: str, request: TaskCreateRequest) -> AsyncIterator[AgentEvent]:
        state, diag = await self.check_availability()
        if state != AvailabilityState.READY:
            yield AgentEvent(
                task_id=task_id,
                event_type=EventType.ERROR,
                text=f"Antigravity integration disabled: {diag}",
                timestamp=time.time(),
            )
            return

        cancel_event = asyncio.Event()
        self._active_cancels[task_id] = cancel_event

        yield AgentEvent(
            task_id=task_id,
            event_type=EventType.STARTED,
            text="Antigravity MCP task started.",
            timestamp=time.time(),
        )

        try:
            mcp_manager = get_mcp_manager()
            # Call dispatch_gemini_agent tool if available
            result = await mcp_manager.call_tool(
                self.MCP_SERVER_NAME,
                "dispatch_gemini_agent",
                {"task": request.prompt, "model": request.model or "gemini-2.5-pro"},
                timeout=60.0,
            )

            yield AgentEvent(
                task_id=task_id,
                event_type=EventType.TOKEN,
                text=str(result),
                timestamp=time.time(),
            )

            yield AgentEvent(
                task_id=task_id,
                event_type=EventType.COMPLETED,
                text="Antigravity task completed.",
                timestamp=time.time(),
            )
        except Exception as e:
            yield AgentEvent(
                task_id=task_id,
                event_type=EventType.ERROR,
                text=f"Antigravity MCP error: {str(e)}",
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
