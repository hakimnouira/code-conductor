from pathlib import Path

import pytest

from app.provenance import collect_workspace_provenance


def test_non_git_workspace_gets_complete_deterministic_fingerprint(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('first')\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")

    first = collect_workspace_provenance(tmp_path)
    second = collect_workspace_provenance(tmp_path)

    assert first["status"] == "captured"
    assert first["workspace_kind"] == "filesystem"
    assert first["snapshot_complete"] is True
    assert first["snapshot_sha256"]
    assert first["snapshot_sha256"] == second["snapshot_sha256"]
    assert first["files_hashed"] == 2

    (tmp_path / "src" / "app.py").write_text("print('changed')\n", encoding="utf-8")
    changed = collect_workspace_provenance(tmp_path)
    assert changed["snapshot_sha256"] != first["snapshot_sha256"]


def test_non_git_workspace_fingerprint_does_not_follow_symlinks(tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-outside-secret.txt")
    outside.write_text("secret outside the workspace", encoding="utf-8")
    link = tmp_path / "external-link"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"Symlink creation is not available: {exc}")

    result = collect_workspace_provenance(tmp_path)
    assert result["status"] == "captured"
    assert result["snapshot_complete"] is True
    assert result["snapshot_sha256"]
    assert "secret outside the workspace" not in str(result)


def test_non_git_workspace_reports_partial_snapshot_when_entry_limit_is_exceeded(tmp_path, monkeypatch):
    import app.provenance as provenance_module

    monkeypatch.setattr(provenance_module, "_MAX_UNTRACKED_FILES", 2)
    for index in range(4):
        (tmp_path / f"file-{index}.txt").write_text(str(index), encoding="utf-8")

    result = collect_workspace_provenance(tmp_path)
    assert result["status"] == "partial"
    assert result["snapshot_complete"] is False
    assert result["snapshot_sha256"]


def test_non_git_workspace_fingerprint_includes_file_mode(tmp_path):
    target = tmp_path / "run.sh"
    target.write_text("#!/bin/sh\\necho ok\\n", encoding="utf-8")
    target.chmod(0o644)
    before = collect_workspace_provenance(tmp_path)
    target.chmod(0o755)
    after = collect_workspace_provenance(tmp_path)

    assert before["snapshot_complete"] is True
    assert after["snapshot_complete"] is True
    assert before["snapshot_sha256"] != after["snapshot_sha256"]


def test_non_git_workspace_fingerprint_excludes_self_generated_evidence_artifacts(tmp_path):
    (tmp_path / "src.py").write_text("value = 1\\n", encoding="utf-8")
    artifacts = tmp_path / ".code-conductor" / "artifacts" / "task-1"
    artifacts.mkdir(parents=True)
    manifest = artifacts / "evidence.json"
    manifest.write_text('{"status":"running"}', encoding="utf-8")

    first = collect_workspace_provenance(tmp_path)
    manifest.write_text('{"status":"completed","new":"evidence"}', encoding="utf-8")
    second = collect_workspace_provenance(tmp_path)

    assert first["snapshot_complete"] is True
    assert second["snapshot_complete"] is True
    assert first["snapshot_sha256"] == second["snapshot_sha256"]
    assert ".code-conductor/artifacts/**" in first["excluded_paths"]
