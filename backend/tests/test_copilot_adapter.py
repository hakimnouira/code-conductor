import asyncio
import pytest
from app.adapters.copilot import CopilotAdapter
from app.models.schemas import AvailabilityState, EventType, TaskCreateRequest

pytestmark = [pytest.mark.live_provider, pytest.mark.mocked_integration]

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
    import os
    from pathlib import Path
    from app.adapters.copilot import CopilotPermissionPolicy

    policy = CopilotPermissionPolicy(workspace_root=tmp_path, allow_terminal=False)

    try:
        from copilot.generated.session_events import (
            PermissionRequestWrite,
            PermissionRequestRead,
            PermissionRequestShell,
            PermissionRequestUrl,
            PermissionRequestMcp,
        )
        has_sdk_types = True
    except ImportError:
        has_sdk_types = False

    # 1. Read is approved
    if has_sdk_types:
        read_req = PermissionRequestRead(
            intention="inspect",
            path=str(tmp_path / "main.py"),
            resolved_path=str(tmp_path / "main.py"),
        )
    else:
        class MockReadRequest:
            resolved_path = str(tmp_path / "main.py")
        read_req = MockReadRequest()
    res_read = policy.evaluate(read_req, {})
    assert type(res_read).__name__ == "PermissionDecisionApproveOnce"

    # 2. Shell is rejected by default
    if has_sdk_types:
        shell_req = PermissionRequestShell(
            can_offer_session_approval=True,
            commands=[],
            full_command_text="rm -rf /",
            has_write_file_redirection=False,
            intention="exec",
            possible_paths=[],
            possible_urls=[],
        )
    else:
        class MockShellRequest:
            command = "rm -rf /"
        shell_req = MockShellRequest()
    res_shell = policy.evaluate(shell_req, {})
    assert type(res_shell).__name__ == "PermissionDecisionReject"

    # 3. Inside workspace write is approved
    if has_sdk_types:
        write_inside = PermissionRequestWrite(
            can_offer_session_approval=True,
            diff="",
            file_name="output.py",
            intention="write code",
            resolved_path=str(tmp_path / "output.py"),
        )
    else:
        class MockInsideWriteRequest:
            resolved_path = str(tmp_path / "output.py")
        write_inside = MockInsideWriteRequest()
    res_inside = policy.evaluate(write_inside, {})
    assert type(res_inside).__name__ == "PermissionDecisionApproveOnce"

    # 4. Outside workspace write is rejected
    if has_sdk_types:
        write_outside = PermissionRequestWrite(
            can_offer_session_approval=True,
            diff="",
            file_name="calc.exe",
            intention="malicious write",
            resolved_path="C:\\Windows\\System32\\calc.exe",
        )
    else:
        class MockOutsideWriteRequest:
            resolved_path = "C:\\Windows\\System32\\calc.exe"
        write_outside = MockOutsideWriteRequest()
    res_outside = policy.evaluate(write_outside, {})
    assert type(res_outside).__name__ == "PermissionDecisionReject"

    # 5. Write without verifiable target path is rejected
    if has_sdk_types:
        write_nopath = PermissionRequestWrite(
            can_offer_session_approval=True,
            diff="",
            file_name="",
            intention="unspecified target",
            resolved_path=None,
        )
    else:
        class MockNoPathWriteRequest:
            resolved_path = None
        write_nopath = MockNoPathWriteRequest()
    res_nopath = policy.evaluate(write_nopath, {})
    assert type(res_nopath).__name__ == "PermissionDecisionReject"

    # 6. Write through symlink pointing outside workspace is rejected
    outside_target = tmp_path.parent / "outside_secret.txt"
    outside_target.write_text("secret", encoding="utf-8")
    symlink_path = tmp_path / "symlink_leak.txt"
    try:
        os.symlink(str(outside_target), str(symlink_path))
        if has_sdk_types:
            write_symlink = PermissionRequestWrite(
                can_offer_session_approval=True,
                diff="",
                file_name="symlink_leak.txt",
                intention="symlink escape",
                resolved_path=str(symlink_path),
            )
        else:
            class MockSymlinkWriteRequest:
                resolved_path = str(symlink_path)
            write_symlink = MockSymlinkWriteRequest()
        res_symlink = policy.evaluate(write_symlink, {})
        assert type(res_symlink).__name__ == "PermissionDecisionReject"
    except (OSError, NotImplementedError):
        pass

    # 7. Network URL request is rejected by default
    if has_sdk_types:
        url_req = PermissionRequestUrl(intention="network", url="https://malicious-exfiltration.com")
        res_url = policy.evaluate(url_req, {})
        assert type(res_url).__name__ == "PermissionDecisionReject"

    # 8. External MCP tool is rejected by default
    if has_sdk_types:
        mcp_req = PermissionRequestMcp(
            read_only=False,
            server_name="ext-server",
            tool_name="format_disk",
            tool_title="format disk",
        )
        res_mcp = policy.evaluate(mcp_req, {})
        assert type(res_mcp).__name__ == "PermissionDecisionReject"


