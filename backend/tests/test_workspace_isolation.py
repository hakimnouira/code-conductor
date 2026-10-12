import os
import pytest
from pathlib import Path
from fastapi.testclient import TestClient
from app.main import app
from app.core.config import get_settings
from app.safety.workspace import (
    WorkspaceSecurityManager,
    WorkspaceValidationError,
    PathTraversalError,
    validate_workspace_path,
)

pytestmark = [pytest.mark.unit, pytest.mark.windows]

@pytest.fixture
def auth_client():
    token = get_settings().auth_token
    return TestClient(app, headers={"Authorization": f"Bearer {token}"})

def test_validate_workspace_path_rejections(tmp_path: Path):
    # 1. Non-existent path
    non_existent = tmp_path / "does_not_exist"
    with pytest.raises(WorkspaceValidationError, match="does not exist"):
        validate_workspace_path(str(non_existent))

    # 2. File instead of directory
    f = tmp_path / "test.txt"
    f.write_text("hello", encoding="utf-8")
    with pytest.raises(WorkspaceValidationError, match="not a directory"):
        validate_workspace_path(str(f))

    # 3. Root drive
    with pytest.raises(WorkspaceValidationError, match="Root drive"):
        validate_workspace_path("C:\\")

    # 4. Critical OS system directory
    sys_dir = os.environ.get("SystemRoot", "C:\\Windows")
    if os.path.exists(sys_dir):
        with pytest.raises(WorkspaceValidationError, match="Forbidden system directory"):
            validate_workspace_path(sys_dir)

def test_validate_workspace_path_allowed_roots(tmp_path: Path):
    allowed_root = tmp_path / "allowed_projects"
    allowed_root.mkdir()
    project1 = allowed_root / "proj1"
    project1.mkdir()

    outside_project = tmp_path / "unauthorized_proj"
    outside_project.mkdir()

    # Inside allowed root passes
    validated = validate_workspace_path(str(project1), allowed_roots=[allowed_root])
    assert validated == project1.resolve()

    # Outside allowed root raises
    with pytest.raises(WorkspaceValidationError, match="not within configured allowed workspace roots"):
        validate_workspace_path(str(outside_project), allowed_roots=[allowed_root])

def test_api_workspace_isolation(auth_client: TestClient, tmp_path: Path):
    ws_a = tmp_path / "workspace_a"
    ws_b = tmp_path / "workspace_b"
    ws_a.mkdir()
    ws_b.mkdir()

    # Write file to workspace A
    file_a = ws_a / "hello.txt"
    file_a.write_text("content in A", encoding="utf-8")

    # Read from workspace A succeeds
    res_a = auth_client.post(
        "/workspace/file/read",
        json={"path": "hello.txt", "workspace_path": str(ws_a)},
    )
    assert res_a.status_code == 200
    assert res_a.json()["content"] == "content in A"

    # Reading file from workspace B fails with 404 (file does not exist in B)
    res_b = auth_client.post(
        "/workspace/file/read",
        json={"path": "hello.txt", "workspace_path": str(ws_b)},
    )
    assert res_b.status_code == 404

    # Path traversal outside workspace A is rejected with 403
    res_traversal = auth_client.post(
        "/workspace/file/read",
        json={"path": "../workspace_b/secret.txt", "workspace_path": str(ws_a)},
    )
    assert res_traversal.status_code == 403

    # Invalid / forbidden workspace path is rejected with 400
    res_invalid_ws = auth_client.post(
        "/workspace/file/read",
        json={"path": "hello.txt", "workspace_path": "C:\\Windows"},
    )
    assert res_invalid_ws.status_code == 400

    # Task creation with invalid workspace path rejected with 400
    res_task = auth_client.post(
        "/tasks",
        json={
            "prompt": "Test prompt",
            "agent_id": "mock",
            "workspace_path": "C:\\Windows",
        },
    )
    assert res_task.status_code == 400
    assert "Invalid workspace path" in res_task.json()["detail"]


def test_canonical_containment_sibling_prefix(tmp_path: Path):
    from app.safety.workspace import is_contained_in_workspace

    ws = tmp_path / "workspace"
    ws.mkdir()
    ws_sibling = tmp_path / "workspace_evil"
    ws_sibling.mkdir()

    # Direct sibling is rejected
    assert is_contained_in_workspace(ws_sibling, ws) is False
    assert is_contained_in_workspace(ws_sibling / "secret.txt", ws) is False

    # Inside workspace is accepted
    assert is_contained_in_workspace(ws, ws) is True
    assert is_contained_in_workspace(ws / "file.txt", ws) is True
    assert is_contained_in_workspace("sub/dir/nested.py", ws) is True


def test_canonical_containment_nonexistent_destination(tmp_path: Path):
    from app.safety.workspace import is_contained_in_workspace

    ws = tmp_path / "workspace"
    ws.mkdir()

    # Nonexistent file/folder inside workspace is accepted
    nonexistent_inside = ws / "nonexistent_dir" / "file.txt"
    assert is_contained_in_workspace(nonexistent_inside, ws) is True

    # Nonexistent file outside workspace is rejected
    nonexistent_outside = ws.parent / "other_nonexistent" / "file.txt"
    assert is_contained_in_workspace(nonexistent_outside, ws) is False


def test_canonical_containment_traversal_attempts(tmp_path: Path):
    from app.safety.workspace import is_contained_in_workspace

    ws = tmp_path / "workspace"
    ws.mkdir()

    assert is_contained_in_workspace(ws / ".." / "evil.txt", ws) is False
    assert is_contained_in_workspace(ws / "sub" / ".." / ".." / "evil.txt", ws) is False
    assert is_contained_in_workspace("../evil.txt", ws) is False
    assert is_contained_in_workspace("..", ws) is False


def test_canonical_containment_drive_differences():
    from app.safety.workspace import is_contained_in_workspace

    # Drive difference test
    assert is_contained_in_workspace("D:\\other\\file.txt", "C:\\workspace") is False
    assert is_contained_in_workspace("C:\\workspace\\file.txt", "D:\\workspace") is False


def test_canonical_containment_symlinks(tmp_path: Path):
    from app.safety.workspace import is_contained_in_workspace

    ws = tmp_path / "workspace"
    ws.mkdir()
    outside = tmp_path / "outside_dir"
    outside.mkdir()
    target_outside_file = outside / "sensitive.txt"
    target_outside_file.write_text("classified", encoding="utf-8")

    link_to_outside = ws / "symlink_outside"
    try:
        os.symlink(str(outside), str(link_to_outside), target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation requires elevated privileges or is not supported.")

    # Symlink pointing outside workspace resolves outside and must be rejected
    assert is_contained_in_workspace(link_to_outside / "sensitive.txt", ws) is False

    # Symlink pointing inside workspace is accepted
    inside_target = ws / "inside_target"
    inside_target.mkdir()
    link_to_inside = ws / "symlink_inside"
    try:
        os.symlink(str(inside_target), str(link_to_inside), target_is_directory=True)
        assert is_contained_in_workspace(link_to_inside / "nested.txt", ws) is True
    except (OSError, NotImplementedError):
        pass


def test_copilot_permission_handler_containment(tmp_path: Path):
    from app.adapters.copilot import CopilotPermissionHandler

    ws = tmp_path / "workspace"
    ws.mkdir()
    ws_evil = tmp_path / "workspace_evil"
    ws_evil.mkdir()

    handler = CopilotPermissionHandler(workspace_root=ws, allow_terminal=False)

    class DummyReq:
        def __init__(self, kind: str, path: str):
            self.kind = kind
            self.path = path

    # Sibling with same textual prefix must be rejected
    evil_req = DummyReq("read", str(ws_evil / "passwords.txt"))
    res = handler.evaluate(evil_req, None)
    assert type(res).__name__ == "PermissionDecisionReject"

    # Traversal attempt must be rejected
    traversal_req = DummyReq("write", str(ws / ".." / "evil.txt"))
    res = handler.evaluate(traversal_req, None)
    assert type(res).__name__ == "PermissionDecisionReject"

    # Valid inside path is accepted
    valid_req = DummyReq("read", str(ws / "main.py"))
    res = handler.evaluate(valid_req, None)
    assert type(res).__name__ == "PermissionDecisionApproveOnce"

