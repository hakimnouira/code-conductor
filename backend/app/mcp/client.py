import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Any
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

@dataclass
class MCPServerConfig:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    tool_allowlist: list[str] | None = None
    enabled: bool = True

@dataclass
class ToolDefinition:
    name: str
    description: str | None
    input_schema: dict[str, Any]

@dataclass
class ServerDiagnostic:
    name: str
    status: str
    tool_count: int
    error: str | None = None
    response_time_ms: float = 0.0

class MCPClientManager:
    """Manages MCP connections, tool discovery, and invocation using the official MCP SDK."""

    def __init__(self):
        self._servers: dict[str, MCPServerConfig] = {}

    def register_server(self, config: MCPServerConfig) -> None:
        self._servers[config.name] = config

    def get_server(self, name: str) -> MCPServerConfig | None:
        return self._servers.get(name)

    def list_servers(self) -> list[str]:
        return list(self._servers.keys())

    def _get_server_params(self, config: MCPServerConfig) -> StdioServerParameters:
        merged_env = dict(os.environ)
        merged_env.update(config.env)
        return StdioServerParameters(
            command=config.command,
            args=config.args,
            env=merged_env,
        )

    async def get_server_diagnostics(self, name: str, timeout: float = 8.0) -> ServerDiagnostic:
        config = self._servers.get(name)
        if not config:
            return ServerDiagnostic(name=name, status="unconfigured", tool_count=0, error="Server not configured")
        if not config.enabled:
            return ServerDiagnostic(name=name, status="disabled", tool_count=0, error="Server is disabled")

        start = time.perf_counter()
        try:
            params = self._get_server_params(config)
            async with stdio_client(params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await asyncio.wait_for(session.initialize(), timeout=timeout)
                    tools_result = await asyncio.wait_for(session.list_tools(), timeout=timeout)
                    elapsed = (time.perf_counter() - start) * 1000.0
                    return ServerDiagnostic(
                        name=name,
                        status="connected",
                        tool_count=len(tools_result.tools),
                        response_time_ms=round(elapsed, 2),
                    )
        except Exception as e:
            elapsed = (time.perf_counter() - start) * 1000.0
            return ServerDiagnostic(
                name=name,
                status="error",
                tool_count=0,
                error=str(e),
                response_time_ms=round(elapsed, 2),
            )

    async def list_tools(self, name: str, timeout: float = 8.0) -> list[ToolDefinition]:
        config = self._servers.get(name)
        if not config:
            raise ValueError(f"MCP server '{name}' is not configured.")

        params = self._get_server_params(config)
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await asyncio.wait_for(session.initialize(), timeout=timeout)
                tools_result = await asyncio.wait_for(session.list_tools(), timeout=timeout)

                tools: list[ToolDefinition] = []
                for t in tools_result.tools:
                    # Enforce allowlist if defined
                    if config.tool_allowlist is not None and t.name not in config.tool_allowlist:
                        continue
                    schema = t.inputSchema if hasattr(t, "inputSchema") else {}
                    tools.append(ToolDefinition(
                        name=t.name,
                        description=getattr(t, "description", None),
                        input_schema=schema,
                    ))
                return tools

    async def call_tool(
        self,
        name: str,
        tool_name: str,
        arguments: dict[str, Any],
        timeout: float = 30.0,
    ) -> Any:
        config = self._servers.get(name)
        if not config:
            raise ValueError(f"MCP server '{name}' is not configured.")

        # Enforce allowlist
        if config.tool_allowlist is not None and tool_name not in config.tool_allowlist:
            raise PermissionError(f"Tool '{tool_name}' is not in the allowlist for server '{name}'.")

        params = self._get_server_params(config)
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await asyncio.wait_for(session.initialize(), timeout=timeout)
                result = await asyncio.wait_for(session.call_tool(tool_name, arguments), timeout=timeout)
                return result

_mcp_manager: MCPClientManager | None = None

def get_mcp_manager() -> MCPClientManager:
    global _mcp_manager
    if _mcp_manager is None:
        _mcp_manager = MCPClientManager()
    return _mcp_manager
