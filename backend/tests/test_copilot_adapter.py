import asyncio
import pytest
from app.adapters.copilot import CopilotAdapter
from app.models.schemas import AvailabilityState, EventType, TaskCreateRequest

@pytest.mark.asyncio
async def test_copilot_adapter_metadata():
    adapter = CopilotAdapter()
    assert adapter.agent_id == "copilot"
    assert "GitHub Copilot" in adapter.name
    caps = await adapter.get_capabilities()
    assert caps.supports_streaming is True
    assert caps.supports_code_edits is True
    assert caps.supports_terminal_execution is False
    assert len(caps.supported_models) >= 1

@pytest.mark.asyncio
async def test_copilot_real_availability_check():
    adapter = CopilotAdapter()
    state, diag = await adapter.check_availability()
    # On this host, the SDK and runtime are installed. Since the user hasn't run gh auth login,
    # it must report UNAUTHENTICATED with clear instructions to run gh auth login.
    assert state in (AvailabilityState.READY, AvailabilityState.UNAUTHENTICATED)
    if state == AvailabilityState.UNAUTHENTICATED:
        assert "gh auth login" in diag or "not signed in" in diag.lower() or "not authenticated" in diag.lower()
    else:
        assert "ready" in diag.lower()

@pytest.mark.asyncio
async def test_copilot_stream_task_when_unauthenticated():
    adapter = CopilotAdapter()
    state, _ = await adapter.check_availability()
    if state == AvailabilityState.UNAUTHENTICATED:
        req = TaskCreateRequest(prompt="Write a hello world program", agent_id="copilot")
        events = []
        async for event in adapter.stream_task("copilot-test-1", req):
            events.append(event)

        assert len(events) >= 1
        assert events[0].event_type == EventType.ERROR
        assert "unavailable" in events[0].text.lower() or "not signed in" in events[0].text.lower()

@pytest.mark.asyncio
async def test_copilot_cancel_task():
    adapter = CopilotAdapter()
    cancel_res = await adapter.cancel_task("non-existent-task")
    assert cancel_res is False


def test_copilot_permission_policy(tmp_path):
    from pathlib import Path
    from app.adapters.copilot import CopilotPermissionPolicy

    policy = CopilotPermissionPolicy(workspace_root=tmp_path, allow_terminal=False)

    # Mock request classes
    class MockReadRequest:
        path = str(tmp_path / "main.py")

    class MockShellRequest:
        command = "rm -rf /"

    class MockOutsideWriteRequest:
        path = "C:\\Windows\\System32\\calc.exe"

    class MockInsideWriteRequest:
        path = str(tmp_path / "output.py")

    # 1. Read is approved
    res_read = policy.evaluate(MockReadRequest(), {})
    assert type(res_read).__name__ == "PermissionDecisionApproveOnce"

    # 2. Shell is rejected by default
    res_shell = policy.evaluate(MockShellRequest(), {})
    assert type(res_shell).__name__ == "PermissionDecisionReject"

    # 3. Outside workspace write is rejected
    res_outside = policy.evaluate(MockOutsideWriteRequest(), {})
    assert type(res_outside).__name__ == "PermissionDecisionReject"

    # 4. Inside workspace write is approved
    res_inside = policy.evaluate(MockInsideWriteRequest(), {})
    assert type(res_inside).__name__ == "PermissionDecisionApproveOnce"
