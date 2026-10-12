import pytest

from app.git_worktrees import _validate_branch


pytestmark = pytest.mark.unit


@pytest.mark.parametrize("branch", ["main", "feature/task-123", "cc/agent-a", "fix.test"])
def test_valid_worktree_branch_names(branch: str):
    assert _validate_branch(branch) == branch


@pytest.mark.parametrize("branch", ["", "../escape", "bad..name", "-option", "name/", "name.lock", "two//slashes"])
def test_invalid_worktree_branch_names_are_rejected(branch: str):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        _validate_branch(branch)
    assert exc.value.status_code == 400
