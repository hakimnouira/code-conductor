from pathlib import Path

import pytest

from app.main import _load_project_rules, _project_rules_path


pytestmark = pytest.mark.unit


def test_project_rules_are_loaded_from_workspace_only(tmp_path: Path):
    rules = tmp_path / ".code-conductor" / "rules.md"
    rules.parent.mkdir()
    rules.write_text("Use pytest and preserve the existing API.", encoding="utf-8")

    assert _load_project_rules(tmp_path) == "Use pytest and preserve the existing API."
    assert _project_rules_path(tmp_path) == rules


def test_missing_project_rules_are_optional(tmp_path: Path):
    assert _load_project_rules(tmp_path) == ""


def test_project_rules_reject_symlink_escape(tmp_path: Path):
    root = tmp_path / "workspace"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "rules.md").write_text("outside", encoding="utf-8")
    config_dir = root / ".code-conductor"
    config_dir.mkdir()
    try:
        (config_dir / "rules.md").symlink_to(outside / "rules.md")
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation is unavailable in this environment")

    with pytest.raises(Exception):
        _project_rules_path(root)
