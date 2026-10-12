import os
import shutil
import subprocess
import pytest
from pathlib import Path
from app.safety.git_guard import GitGuard
from app.models.schemas import CheckpointResponse

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
async def test_durable_checkpoint_preserves_dirty_state(tmp_path: Path):
    init_test_git_repo(tmp_path)
    guard = GitGuard(tmp_path)

    # Setup dirty state:
    # 1. file1.txt: modified unstaged
    file1 = tmp_path / "file1.txt"
    file1.write_text("modified unstaged content", encoding="utf-8")

    # 2. file2.txt: tracked and staged
    file2 = tmp_path / "file2.txt"
    file2.write_text("staged content", encoding="utf-8")
    subprocess.run(["git", "add", "file2.txt"], cwd=str(tmp_path), check=True)

    # 3. file3.txt: untracked
    file3 = tmp_path / "file3.txt"
    file3.write_text("untracked content", encoding="utf-8")

    # Create checkpoint
    res: CheckpointResponse = await guard.create_checkpoint(name="checkpoint_test")
    assert res.success is True
    ckpt_id = res.checkpoint_id
    assert len(ckpt_id) > 0

    # Verify working tree was untouched during creation
    assert file1.read_text(encoding="utf-8") == "modified unstaged content"
    assert file2.read_text(encoding="utf-8") == "staged content"
    assert file3.read_text(encoding="utf-8") == "untracked content"

    # Now make subsequent modifications that conflict
    file1.write_text("subsequent conflicting work", encoding="utf-8")
    file4 = tmp_path / "file4_new.txt"
    file4.write_text("file4 created after checkpoint", encoding="utf-8")

    # Refusal to overwrite conflicting work without force=True
    refused = await guard.restore_checkpoint(ckpt_id, force=False)
    assert refused.success is False
    assert "Refusing to restore" in refused.message
    assert "file1.txt" in refused.message

    # Verify conflicting files are still intact
    assert file1.read_text(encoding="utf-8") == "subsequent conflicting work"
    assert file4.exists() is True

    # Successful exact restoration with force=True
    restored = await guard.restore_checkpoint(ckpt_id, force=True)
    assert restored.success is True
    assert "Restored files from checkpoint" in restored.message

    # Verify exact state was restored
    assert file1.read_text(encoding="utf-8") == "modified unstaged content"
    assert file2.read_text(encoding="utf-8") == "staged content"
    assert file3.read_text(encoding="utf-8") == "untracked content"
    # file4 was created after checkpoint, so it must be removed
    assert file4.exists() is False

@pytest.mark.asyncio
async def test_checkpoint_non_git_repo(tmp_path: Path):
    guard = GitGuard(tmp_path)
    res = await guard.create_checkpoint("fail_test")
    assert res.success is False
    assert "not a git repository" in res.message

    res_restore = await guard.restore_checkpoint("non_existent")
    assert res_restore.success is False
    assert "not a git repository" in res_restore.message
