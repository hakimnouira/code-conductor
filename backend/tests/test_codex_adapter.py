import asyncio
import os
import sys
import tempfile
from pathlib import Path
import pytest
from app.adapters.codex import CodexAdapter
from app.models.schemas import AvailabilityState, EventType, TaskCreateRequest

@pytest.mark.asyncio
async def test_codex_adapter_metadata():
    adapter = CodexAdapter()
    assert adapter.agent_id == "codex"
    assert "OpenAI Codex" in adapter.name
    caps = await adapter.get_capabilities()
    assert caps.supports_streaming is True
    assert caps.supports_code_edits is True
    assert caps.supports_terminal_execution is True

@pytest.mark.asyncio
async def test_codex_availability_missing_cli():
    # Provide a non-existent binary path
    adapter = CodexAdapter(executable_path=r"C:\non_existent_path\codex.exe")
    state, diag = await adapter.check_availability()
    assert state == AvailabilityState.MISSING_CLI
    assert "powershell" in diag or "install" in diag

@pytest.mark.asyncio
async def test_codex_availability_unauthenticated(monkeypatch):
    # Use python.exe as mock executable, but ensure ~/.codex has no auth
    with tempfile.TemporaryDirectory() as tmp_userprofile:
        monkeypatch.setenv("USERPROFILE", tmp_userprofile)
        adapter = CodexAdapter(executable_path=sys.executable)
        state, diag = await adapter.check_availability()
        assert state == AvailabilityState.UNAUTHENTICATED
        assert "codex login" in diag

@pytest.mark.asyncio
async def test_codex_availability_ready(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp_userprofile:
        monkeypatch.setenv("USERPROFILE", tmp_userprofile)
        codex_dir = Path(tmp_userprofile) / ".codex"
        codex_dir.mkdir(parents=True, exist_ok=True)
        # Create dummy auth file
        (codex_dir / "auth.json").write_text('{"token": "mock_chatgpt_token"}', encoding="utf-8")

        adapter = CodexAdapter(executable_path=sys.executable)
        state, diag = await adapter.check_availability()
        assert state == AvailabilityState.READY
        assert "ready" in diag.lower()

@pytest.mark.asyncio
async def test_codex_stream_task_when_missing():
    adapter = CodexAdapter(executable_path=r"C:\non_existent_path\codex.exe")
    req = TaskCreateRequest(prompt="Write a function", agent_id="codex")
    events = []
    async for event in adapter.stream_task("test-task-1", req):
        events.append(event)

    assert len(events) >= 1
    assert events[0].event_type == EventType.ERROR
    assert "unavailable" in events[0].text.lower()

@pytest.mark.asyncio
async def test_codex_execution_and_cancellation(monkeypatch):
    # Test execution & cancellation using a mock python script that simulates a long-running CLI
    with tempfile.TemporaryDirectory() as tmp_dir:
        monkeypatch.setenv("USERPROFILE", tmp_dir)
        codex_dir = Path(tmp_dir) / ".codex"
        codex_dir.mkdir(parents=True, exist_ok=True)
        (codex_dir / "auth.json").write_text('{"token": "dummy"}', encoding="utf-8")

        # Create a mock script that runs slowly and echoes lines
        script_file = Path(tmp_dir) / "mock_codex.py"
        script_file.write_text(
            'import time, sys\n'
            'print("Codex processing workspace...", flush=True)\n'
            'time.sleep(0.5)\n'
            'print("Editing files...", flush=True)\n'
            'time.sleep(2.0)\n'
            'print("Done", flush=True)\n',
            encoding="utf-8"
        )

        # Batch runner wrapping the script
        cmd_file = Path(tmp_dir) / "mock_codex.bat"
        cmd_file.write_text(f'@"{sys.executable}" "{script_file}" %*\n', encoding="utf-8")

        adapter = CodexAdapter(executable_path=str(cmd_file))
        req = TaskCreateRequest(prompt="Refactor code", agent_id="codex", workspace_path=tmp_dir)

        task_id = "test-codex-cancel"
        events = []
        async def run_stream():
            async for ev in adapter.stream_task(task_id, req):
                events.append(ev)
                if ev.event_type == EventType.TOKEN and "processing" in ev.text:
                    # Cancel while running
                    await adapter.cancel_task(task_id)

        await asyncio.wait_for(run_stream(), timeout=10.0)

        assert any(ev.event_type == EventType.STARTED for ev in events)
        assert any(ev.event_type in (EventType.CANCELLED, EventType.ERROR) for ev in events)
