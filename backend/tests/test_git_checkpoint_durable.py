import os
import shutil
import subprocess
import pytest
from pathlib import Path
from app.safety.git_guard import GitGuard
from app.models.schemas import CheckpointResponse

pytestmark = [pytest.mark.unit, pytest.mark.windows]

def init_test_git_repo(repo_path: Path) -> None:
    git_bin = shutil.which("git")
    if not git_bin:
        pytest.skip("Git is not installed on this system.")

    subprocess.run([git_bin, "init"], cwd=str(repo_path), check=True, capture_output=True)
    subprocess.run([git_bin, "config", "user.name", "TestUser"], cwd=str(repo_path), check=True)
    subprocess.run([git_bin, "config", "user.email", "test@example.com"], cwd=str(repo_path), check=True)

    # Initial commit
    file1 = repo_path / "file1.txt"
    file1.write_text("initial content", encoding="utf-8")
    subprocess.run([git_bin, "add", "file1.txt"], cwd=str(repo_path), check=True)
    subprocess.run([git_bin, "commit", "-m", "Initial commit"], cwd=str(repo_path), check=True)

@pytest.mark.asyncio
async def test_durable_checkpoint_preserves_dirty_state_and_safe_restore(tmp_path: Path):
    init_test_git_repo(tmp_path)
    guard = GitGuard(tmp_path)

    # 1. Setup .gitignore and ignored local configuration files
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text(".env\nlocal_config.json\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "commit", "-m", "Add gitignore"], cwd=str(tmp_path), check=True)

    env_file = tmp_path / ".env"
    env_file.write_text("DATABASE_URL=postgres://local/test\nSECRET=supersecret", encoding="utf-8")
    local_cfg = tmp_path / "local_config.json"
    local_cfg.write_text('{"env": "development", "debug": true}', encoding="utf-8")

    # 2. Tracked file with unstaged modification
    file1 = tmp_path / "file1.txt"
    file1.write_text("modified unstaged content v1", encoding="utf-8")

    # 3. Tracked file staged into index (pre-existing staged index state)
    staged_file = tmp_path / "staged_file.txt"
    staged_file.write_text("pre-existing staged index content", encoding="utf-8")
    subprocess.run(["git", "add", "staged_file.txt"], cwd=str(tmp_path), check=True)

    # 4. Untracked file
    untracked_file = tmp_path / "untracked_work.txt"
    untracked_file.write_text("untracked work v1", encoding="utf-8")

    # Verify status before checkpoint: staged_file is staged
    status_before = subprocess.run(["git", "status", "--porcelain"], cwd=str(tmp_path), capture_output=True, text=True, check=True)
    assert "A  staged_file.txt" in status_before.stdout or "M  staged_file.txt" in status_before.stdout

    # Create safety checkpoint
    res: CheckpointResponse = await guard.create_checkpoint(name="checkpoint_test")
    assert res.success is True
    ckpt_id = res.checkpoint_id
    assert len(ckpt_id) > 0

    # Verify working tree and staged index were untouched during creation
    assert file1.read_text(encoding="utf-8") == "modified unstaged content v1"
    assert staged_file.read_text(encoding="utf-8") == "pre-existing staged index content"
    assert untracked_file.read_text(encoding="utf-8") == "untracked work v1"
    assert env_file.read_text(encoding="utf-8") == "DATABASE_URL=postgres://local/test\nSECRET=supersecret"

    status_after_create = subprocess.run(["git", "status", "--porcelain"], cwd=str(tmp_path), capture_output=True, text=True, check=True)
    assert "A  staged_file.txt" in status_after_create.stdout or "M  staged_file.txt" in status_after_create.stdout

    # Now make subsequent modifications:
    # a) Conflicting subsequent edit to file1.txt
    file1.write_text("conflicting subsequent edit v2", encoding="utf-8")
    # b) Unrelated user file created after checkpoint
    file_after = tmp_path / "file_after_checkpoint.txt"
    file_after.write_text("new file created after checkpoint", encoding="utf-8")
    # c) Ignored config modification
    env_file.write_text("DATABASE_URL=postgres://local/test\nSECRET=supersecret\nEXTRA=val", encoding="utf-8")

    # 5. Refusal to restore conflicting changes without force=True
    refused = await guard.restore_checkpoint(ckpt_id, force=False)
    assert refused.success is False
    assert "Refusing to restore" in refused.message
    assert "file1.txt" in refused.message

    # Verify nothing was changed or lost during refused restore
    assert file1.read_text(encoding="utf-8") == "conflicting subsequent edit v2"
    assert file_after.exists() is True
    assert env_file.exists() is True

    # 6. Force restoration:
    restored = await guard.restore_checkpoint(ckpt_id, force=True)
    assert restored.success is True
    assert "Restored files from checkpoint" in restored.message

    # Verify requirements:
    # 1. file1.txt restored to checkpoint state
    assert file1.read_text(encoding="utf-8") == "modified unstaged content v1"
    # 2. staged_file.txt restored to checkpoint state
    assert staged_file.read_text(encoding="utf-8") == "pre-existing staged index content"
    # 3. untracked_file.txt restored to checkpoint state
    assert untracked_file.read_text(encoding="utf-8") == "untracked work v1"
    # 4. Ignored files preserved (.env and local_config.json)
    assert env_file.exists() is True
    assert local_cfg.exists() is True
    assert local_cfg.read_text(encoding="utf-8") == '{"env": "development", "debug": true}'
    # 5. Unrelated user files created after checkpoint are preserved
    assert file_after.exists() is True
    assert file_after.read_text(encoding="utf-8") == "new file created after checkpoint"
    # 6. Pre-existing staged index state is preserved
    status_final = subprocess.run(["git", "status", "--porcelain"], cwd=str(tmp_path), capture_output=True, text=True, check=True)
    assert "A  staged_file.txt" in status_final.stdout or "M  staged_file.txt" in status_final.stdout


@pytest.mark.asyncio
async def test_checkpoint_non_git_repo(tmp_path: Path):
    guard = GitGuard(tmp_path)
    res = await guard.create_checkpoint("fail_test")
    assert res.success is False
    assert "not a git repository" in res.message

    res_restore = await guard.restore_checkpoint("non_existent")
    assert res_restore.success is False
    assert "not a git repository" in res_restore.message

