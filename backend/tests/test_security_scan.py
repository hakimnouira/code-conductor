"""Regression tests for the bounded secret-pattern scanner."""
import os
import time

import pytest

from app.safety.security_scan import scan_recent_secrets


pytestmark = pytest.mark.unit


def test_scanner_reports_high_confidence_secret_without_returning_value(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret = "AKIA" + "1234567890ABCDEF"
    source = workspace / "settings.py"
    source.write_text(f'AWS_ACCESS_KEY = "{secret}"\n', encoding="utf-8")

    result = scan_recent_secrets(workspace, time.time() - 1)

    assert result["status"] == "warning"
    assert result["scanned_files"] == 1
    assert result["findings"] == [{
        "file": "settings.py",
        "line": 1,
        "pattern": "aws_access_key_id",
    }]
    assert secret not in str(result)


def test_scanner_ignores_unchanged_files_and_symlinks_outside_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    old_file = workspace / "old.py"
    old_file.write_text('key = "' + "AKIA" + "1234567890ABCDEF" + '"\n', encoding="utf-8")
    old = time.time() - 3600
    os.utime(old_file, (old, old))

    outside = tmp_path / "outside.py"
    outside.write_text('key = "' + "AKIA" + "1234567890ABCDEF" + '"\n', encoding="utf-8")
    (workspace / "linked.py").symlink_to(outside)

    result = scan_recent_secrets(workspace, time.time())

    assert result["status"] == "skipped"
    assert result["scanned_files"] == 0
    assert result["findings"] == []
