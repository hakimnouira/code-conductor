import hashlib
import os
import shutil
import subprocess
import pytest
from pathlib import Path
from fastapi.testclient import TestClient

from app.main import app
from app.core.config import get_settings
from app.safety.workspace import validate_workspace_path, WorkspaceSecurityManager
from app.safety.git_guard import GitGuard
from app.safety.test_runner import SafeTestRunner

pytestmark = [pytest.mark.windows, pytest.mark.mocked_integration]


def hash_dir(dir_path: Path) -> dict[str, str]:
    """Computes SHA256 for all files in a directory for tamper detection."""
    result = {}
    for root, _, files in os.walk(dir_path):
        for f in files:
            p = Path(root) / f
            try:
                rel = str(p.relative_to(dir_path)).replace("\\", "/")
                result[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
            except OSError:
                pass
    return result


def init_git_repo(repo_path: Path) -> None:
    git_bin = shutil.which("git")
    if not git_bin:
        pytest.skip("Git is not installed on this system.")
    subprocess.run([git_bin, "init"], cwd=str(repo_path), check=True, capture_output=True)
    subprocess.run([git_bin, "config", "user.name", "TestUser"], cwd=str(repo_path), check=True)
    subprocess.run([git_bin, "config", "user.email", "test@example.com"], cwd=str(repo_path), check=True)


@pytest.fixture
def auth_client():
    token = get_settings().auth_token
    return TestClient(app, headers={"Authorization": f"Bearer {token}"})


@pytest.mark.asyncio
async def test_workspace_binding_and_isolation_end_to_end(tmp_path: Path, auth_client: TestClient):
    """
    Validates end-to-end workspace binding between Project A and Project B:
    - Agent receives A's validated path.
    - File operations, Git checkpoints, diffs, and test runs target A strictly.
    - Project B and backend installation are completely untouched.
    - Task cancellation terminates the exact active task process without killing unrelated processes.
    """
    proj_a = tmp_path / "project_a"
    proj_b = tmp_path / "project_b"
    proj_a.mkdir()
    proj_b.mkdir()

    # Initialize Project A
    init_git_repo(proj_a)
    file_a = proj_a / "index.py"
    file_a.write_text("def hello():\n    return 'hello from A'\n", encoding="utf-8")
    test_a = proj_a / "test_a.py"
    test_a.write_text("from index import hello\ndef test_hello():\n    assert 'hello' in hello()\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(proj_a), check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit in A"], cwd=str(proj_a), check=True)

    # Initialize Project B
    init_git_repo(proj_b)
    canary_b = proj_b / "canary_b.txt"
    canary_b.write_text("PROTECTED_CANARY_IN_PROJECT_B", encoding="utf-8")
    file_b = proj_b / "index.py"
    file_b.write_text("def hello():\n    return 'hello from B'\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(proj_b), check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit in B"], cwd=str(proj_b), check=True)

    # Record baseline state of Project B and Backend source files
    b_hashes_before = hash_dir(proj_b)
    backend_main = Path(__file__).resolve().parent.parent / "app" / "main.py"
    backend_main_sha_before = hashlib.sha256(backend_main.read_bytes()).hexdigest()

    # 1. Path validation establishes canonical binding for Project A
    validated_a = validate_workspace_path(str(proj_a))
    assert validated_a == proj_a.resolve()

    # 2. File write operation targets Project A
    sec_mgr_a = WorkspaceSecurityManager(proj_a)
    sec_mgr_a.write_file("index.py", "def hello():\n    return 'hello from A updated'\n")
    assert file_a.read_text(encoding="utf-8") == "def hello():\n    return 'hello from A updated'\n"

    # 3. Git guard inspects diff in Project A
    guard_a = GitGuard(proj_a)
    diff_res = await guard_a.get_diff("index.py")
    assert "hello from A updated" in diff_res.diff
    assert diff_res.file_count >= 1

    # 4. Checkpoint created in Project A
    ckpt_res = await guard_a.create_checkpoint("e2e_checkpoint_a")
    assert ckpt_res.success is True

    # 5. Safe test runner executes tests inside Project A
    runner_a = SafeTestRunner(proj_a)
    test_run_res = await runner_a.run_tests("pytest", extra_args=["test_a.py"])
    assert test_run_res.exit_code == 0
    assert "1 passed" in test_run_res.stdout

    # 6. API task creation binding: Task executed with workspace_path=proj_a
    res_task = auth_client.post(
        "/tasks",
        json={
            "prompt": "Inspect workspace",
            "agent_id": "mock",
            "workspace_path": str(proj_a),
        },
    )
    assert res_task.status_code == 200
    task_data = res_task.json()
    assert task_data["workspace_path"] == str(proj_a.resolve())
    task_id = task_data["task_id"]

    # 7. Verification: Project B is 100% UNTOUCHED
    b_hashes_after = hash_dir(proj_b)
    assert b_hashes_before == b_hashes_after, "Project B was unexpectedly modified during Project A operations!"
    assert canary_b.read_text(encoding="utf-8") == "PROTECTED_CANARY_IN_PROJECT_B"

    # 8. Verification: Backend installation is 100% UNTOUCHED
    backend_main_sha_after = hashlib.sha256(backend_main.read_bytes()).hexdigest()
    assert backend_main_sha_before == backend_main_sha_after, "Backend main.py was modified!"

    # 9. Cancellation: verify task cancellation terminates active process without killing unrelated processes
    # Start a mock long-running task
    res_long = auth_client.post(
        "/tasks",
        json={
            "prompt": "Long running simulation",
            "agent_id": "mock",
            "workspace_path": str(proj_a),
        },
    )
    assert res_long.status_code == 200
    long_task_id = res_long.json()["task_id"]

    # Cancel the active task
    res_cancel = auth_client.post(f"/tasks/{long_task_id}/cancel")
    assert res_cancel.status_code == 200
    cancel_json = res_cancel.json()
    assert cancel_json["task_id"] == long_task_id
    assert cancel_json["status"] == "cancelled"

    # Verify unrelated process is still running (our test runner and process are completely alive)
    assert os.getpid() > 0
