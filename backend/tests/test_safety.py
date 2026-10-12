import os
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app
from app.safety.git_guard import GitGuard
from app.safety.test_runner import SafeTestRunner, TestRunnerSecurityError
from app.safety.workspace import (
    PathTraversalError,
    SensitiveFileBlockedError,
    WorkspaceSecurityManager,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def temp_workspace(tmp_path: Path):
    """Creates a temporary workspace with test files."""
    ws = tmp_path / "test_workspace"
    ws.mkdir()
    (ws / "hello.py").write_text("print('hello world')\n", encoding="utf-8")
    (ws / "docs").mkdir()
    (ws / "docs" / "readme.md").write_text("# Readme\n", encoding="utf-8")

    # Sensitive files
    (ws / ".env").write_text("SECRET=123\n", encoding="utf-8")
    (ws / "keys.pem").write_text("CERT\n", encoding="utf-8")
    (ws / "my_api_keys.json").write_text("{}", encoding="utf-8")

    return ws


# -------------------------------------------------------------
# 1. WorkspaceSecurityManager Unit Tests
# -------------------------------------------------------------

def test_workspace_safe_read_and_write(temp_workspace: Path):
    mgr = WorkspaceSecurityManager(temp_workspace)

    # Read safe file
    content = mgr.read_file("hello.py")
    assert "print('hello world')" in content

    # Write safe file (atomic edit with diff)
    bytes_written, diff = mgr.write_file("hello.py", "print('hello modified')\n", atomic=True)
    assert bytes_written > 0
    assert "-print('hello world')" in diff
    assert "+print('hello modified')" in diff

    # Verify content updated
    updated = mgr.read_file("hello.py")
    assert "print('hello modified')" in updated

    # Write new file in new nested directory
    bytes_w, diff_new = mgr.write_file("nested/dir/new.txt", "new file content")
    assert bytes_w == len("new file content")
    assert (temp_workspace / "nested" / "dir" / "new.txt").exists()


def test_workspace_blocks_path_traversal(temp_workspace: Path):
    mgr = WorkspaceSecurityManager(temp_workspace)

    with pytest.raises(PathTraversalError):
        mgr.resolve_safe_path("../outside.txt")

    with pytest.raises(PathTraversalError):
        mgr.read_file("../../secret.txt")

    with pytest.raises(PathTraversalError):
        mgr.write_file("../malicious.py", "hack")


def test_workspace_blocks_sensitive_files(temp_workspace: Path):
    mgr = WorkspaceSecurityManager(temp_workspace)

    # Blocked .env
    with pytest.raises(SensitiveFileBlockedError):
        mgr.read_file(".env")

    with pytest.raises(SensitiveFileBlockedError):
        mgr.write_file(".env.production", "SECRET=abc")

    # Blocked certificates / keys
    with pytest.raises(SensitiveFileBlockedError):
        mgr.read_file("keys.pem")

    with pytest.raises(SensitiveFileBlockedError):
        mgr.read_file("my_api_keys.json")


def test_workspace_list_safe_files(temp_workspace: Path):
    mgr = WorkspaceSecurityManager(temp_workspace)
    files = mgr.list_safe_files()

    # Should contain safe files
    assert "hello.py" in files
    assert "docs/readme.md" in files

    # Should NOT contain sensitive files
    assert ".env" not in files
    assert "keys.pem" not in files
    assert "my_api_keys.json" not in files


# -------------------------------------------------------------
# 2. GitGuard Unit Tests
# -------------------------------------------------------------

@pytest.mark.asyncio
async def test_git_guard_non_repo(tmp_path: Path):
    guard = GitGuard(tmp_path)
    status = await guard.get_status()
    assert status.is_repo is False
    assert "not a git repository" in status.error.lower()


@pytest.mark.asyncio
async def test_git_guard_live_repo():
    # Workspace root is our active git repository
    settings = get_settings()
    guard = GitGuard(settings.workspace_root)
    assert guard.is_git_available() is True
    assert guard.is_git_repo() is True

    status = await guard.get_status()
    assert status.is_repo is True
    assert status.branch != ""

    diff = await guard.get_diff()
    assert isinstance(diff.diff, str)


@pytest.mark.asyncio
async def test_git_guard_checkpoint():
    settings = get_settings()
    guard = GitGuard(settings.workspace_root)
    res = await guard.create_checkpoint("test_checkpoint")
    assert res.success is True
    assert "ckpt-test_checkpoint" in res.checkpoint_id


# -------------------------------------------------------------
# 3. SafeTestRunner Unit Tests
# -------------------------------------------------------------

@pytest.mark.asyncio
async def test_safe_test_runner_forbidden_args(temp_workspace: Path):
    runner = SafeTestRunner(temp_workspace)

    # Shell characters must be rejected
    with pytest.raises(TestRunnerSecurityError):
        await runner.run_tests("pytest", extra_args=["-k", "test; rm -rf /"])

    with pytest.raises(TestRunnerSecurityError):
        await runner.run_tests("pytest", extra_args=["test && echo pwned"])

    # Unknown runner rejected
    with pytest.raises(TestRunnerSecurityError):
        await runner.run_tests("bash", extra_args=["-c", "whoami"])


@pytest.mark.asyncio
async def test_safe_test_runner_python_exec(temp_workspace: Path):
    runner = SafeTestRunner(temp_workspace)
    res = await runner.run_tests(
        command_key="python",
        extra_args=["-c", "print('hello from runner')"],
        timeout_seconds=10,
    )
    assert res.passed is True
    assert res.exit_code == 0
    assert "hello from runner" in res.stdout
    assert res.duration_ms > 0


@pytest.mark.asyncio
async def test_safe_test_runner_timeout(temp_workspace: Path):
    runner = SafeTestRunner(temp_workspace)
    # Create a script that sleeps for 5 seconds
    script_file = temp_workspace / "sleep_script.py"
    script_file.write_text("import time\ntime.sleep(5)\n", encoding="utf-8")

    res = await runner.run_tests(
        command_key="python",
        extra_args=["sleep_script.py"],
        timeout_seconds=1,
    )
    assert res.passed is False
    assert res.exit_code == -1
    assert "timed out" in res.stderr.lower()


# -------------------------------------------------------------
# 4. FastAPI Endpoints Integration Tests
# -------------------------------------------------------------

@pytest.fixture
def client():
    return TestClient(app)


def test_api_workspace_file_endpoints(client: TestClient):
    token = get_settings().auth_token
    headers = {"Authorization": f"Bearer {token}"}

    # 1. Write a file
    write_res = client.post(
        "/workspace/file/write",
        json={"path": "backend/tests/fixtures/test_write.txt", "content": "safe file write"},
        headers=headers,
    )
    assert write_res.status_code == 200
    write_data = write_res.json()
    assert write_data["bytes_written"] == len("safe file write")

    # 2. Read the file
    read_res = client.post(
        "/workspace/file/read",
        json={"path": "backend/tests/fixtures/test_write.txt"},
        headers=headers,
    )
    assert read_res.status_code == 200
    assert read_res.json()["content"] == "safe file write"

    # 3. Clean up the created test file
    ws_root = Path(get_settings().workspace_root)
    fixture_file = ws_root / "backend" / "tests" / "fixtures" / "test_write.txt"
    if fixture_file.exists():
        fixture_file.unlink()

    # 4. Try reading blocked path (path traversal)
    bad_read = client.post(
        "/workspace/file/read",
        json={"path": "../../../boot.ini"},
        headers=headers,
    )
    assert bad_read.status_code == 403

    # 5. Try reading sensitive file (.env)
    blocked_read = client.post(
        "/workspace/file/read",
        json={"path": ".env"},
        headers=headers,
    )
    assert blocked_read.status_code == 403


def test_api_workspace_git_and_test_endpoints(client: TestClient):
    token = get_settings().auth_token
    headers = {"Authorization": f"Bearer {token}"}

    # Git status
    status_res = client.get("/workspace/git/status", headers=headers)
    assert status_res.status_code == 200
    assert "is_repo" in status_res.json()

    # Git diff
    diff_res = client.post("/workspace/git/diff", json={}, headers=headers)
    assert diff_res.status_code == 200
    assert "diff" in diff_res.json()

    # Test run
    test_res = client.post(
        "/workspace/test/run",
        json={
            "command_key": "python",
            "extra_args": ["-c", "print('api test run passed')"],
            "timeout_seconds": 10,
        },
        headers=headers,
    )
    assert test_res.status_code == 200
    data = test_res.json()
    assert data["passed"] is True
    assert "api test run passed" in data["stdout"]
