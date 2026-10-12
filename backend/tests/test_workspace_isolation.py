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
