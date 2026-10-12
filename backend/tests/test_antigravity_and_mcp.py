import asyncio
import os
import sys
import tempfile
from pathlib import Path
import pytest
from app.adapters.antigravity import AntigravityAdapter
from app.mcp.client import MCPClientManager, MCPServerConfig
from app.models.schemas import AvailabilityState, EventType, TaskCreateRequest

pytestmark = [pytest.mark.mocked_integration, pytest.mark.unit]

@pytest.mark.asyncio
async def test_antigravity_metadata():
    adapter = AntigravityAdapter()
    assert adapter.agent_id == "antigravity"
    assert "Antigravity" in adapter.name
    caps = await adapter.get_capabilities()
    assert caps.supports_subagents is True
    assert caps.supports_streaming is True
    assert "gemini-2.5-pro" in caps.supported_models

@pytest.mark.asyncio
async def test_antigravity_availability():
    adapter = AntigravityAdapter()
    state, diag = await adapter.check_availability()
    assert state in (AvailabilityState.READY, AvailabilityState.DISABLED, AvailabilityState.MISSING_CLI, AvailabilityState.ERROR)
    assert len(diag) > 0

@pytest.mark.asyncio
async def test_antigravity_disabled_diagnostic(monkeypatch):
    adapter = AntigravityAdapter()
    monkeypatch.setattr(adapter, "find_cli", lambda: None)

    state, diag = await adapter.check_availability()
    assert state == AvailabilityState.MISSING_CLI
    assert "not found" in diag.lower()

@pytest.mark.asyncio
async def test_antigravity_stream_when_disabled(monkeypatch):
    adapter = AntigravityAdapter()
    monkeypatch.setattr(adapter, "find_agentapi_bat", lambda: None)

    req = TaskCreateRequest(prompt="Analyze architecture", agent_id="antigravity")
    events = []
    async for event in adapter.stream_task("ag-task-1", req):
        events.append(event)

    assert len(events) >= 1
    assert events[0].event_type == EventType.ERROR
    assert "not found" in events[0].text.lower()


@pytest.mark.asyncio
async def test_mcp_client_tool_discovery_and_call():
    # Create a real test MCP server using MCP 2.x
    with tempfile.TemporaryDirectory() as tmp_dir:
        server_script = Path(tmp_dir) / "test_mcp_server.py"
        server_script.write_text(
            'import asyncio\n'
            'from mcp.server.mcpserver import MCPServer\n'
            '\n'
            'app = MCPServer(name="test-calculator")\n'
            '\n'
            '@app.tool()\n'
            'def add_numbers(a: int, b: int) -> int:\n'
            '    """Add two numbers together."""\n'
            '    return a + b\n'
            '\n'
            '@app.tool()\n'
            'def echo(message: str) -> str:\n'
            '    """Echo input message."""\n'
            '    return f"echo: {message}"\n'
            '\n'
            '@app.tool()\n'
            'async def slow_op(seconds: float) -> str:\n'
            '    """Simulate long running tool call."""\n'
            '    await asyncio.sleep(seconds)\n'
            '    return "completed"\n'
            '\n'
            'if __name__ == "__main__":\n'
            '    app.run(transport="stdio")\n',
            encoding="utf-8"
        )

        manager = MCPClientManager()
        config = MCPServerConfig(
            name="test-server",
            command=sys.executable,
            args=[str(server_script)],
        )
        manager.register_server(config)

        # 1. Connection diagnostics
        diag = await manager.get_server_diagnostics("test-server")
        assert diag.status == "connected"
        assert diag.tool_count >= 2

        # 2. Tool discovery
        tools = await manager.list_tools("test-server")
        tool_names = [t.name for t in tools]
        assert "add_numbers" in tool_names
        assert "echo" in tool_names

        # 3. Tool invocation
        result = await manager.call_tool("test-server", "add_numbers", {"a": 15, "b": 27})
        res_str = str(result)
        assert "42" in res_str

        # 4. Tool allowlist enforcement
        config_restricted = MCPServerConfig(
            name="restricted-server",
            command=sys.executable,
            args=[str(server_script)],
            tool_allowlist=["echo"],
        )
        manager.register_server(config_restricted)

        restricted_tools = await manager.list_tools("restricted-server")
        assert len(restricted_tools) == 1
        assert restricted_tools[0].name == "echo"

        with pytest.raises(PermissionError):
            await manager.call_tool("restricted-server", "add_numbers", {"a": 1, "b": 2})

        # 5. Tool timeout enforcement
        with pytest.raises(asyncio.TimeoutError):
            await manager.call_tool("test-server", "slow_op", {"seconds": 1.0}, timeout=0.1)


@pytest.mark.asyncio
async def test_antigravity_cancellation(monkeypatch):
    adapter = AntigravityAdapter()
    with tempfile.TemporaryDirectory() as tmp_dir:
        fake_cli = Path(tmp_dir) / "agy"
        fake_cli.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys, time\n"
            "if '--version' in sys.argv:\n"
            "    print('agy test 1.0')\n"
            "    raise SystemExit(0)\n"
            "message = json.loads(sys.stdin.readline())\n"
            "print(json.dumps({'event':'init','init':{'permission_mode':'request-review'}}), flush=True)\n"
            "time.sleep(3)\n"
            "print(json.dumps({'event':'result','result':{'status':'SUCCESS','response':'done'}}), flush=True)\n",
            encoding="utf-8",
        )
        fake_cli.chmod(0o755)
        monkeypatch.setattr(adapter, "find_cli", lambda: str(fake_cli))
        req = TaskCreateRequest(prompt="Long analysis", agent_id="antigravity", workspace_path=tmp_dir)
        events = []

        async def run_and_collect():
            async for ev in adapter.stream_task("ag-cancel-task", req):
                events.append(ev)
                if ev.event_type == EventType.STARTED:
                    await asyncio.sleep(0.05)
                    assert await adapter.cancel_task("ag-cancel-task") is True

        await asyncio.wait_for(run_and_collect(), timeout=4.0)
        event_types = [event.event_type for event in events]
        assert EventType.STARTED in event_types
        assert EventType.CANCELLED in event_types
