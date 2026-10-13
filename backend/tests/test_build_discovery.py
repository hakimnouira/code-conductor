"""Build-check discovery tests."""
import json

import pytest

from app.safety.test_runner import SafeTestRunner


pytestmark = pytest.mark.unit


def test_build_script_is_used_when_no_test_script_exists(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({
        "scripts": {"build": "tsc -b"}
    }), encoding="utf-8")

    runner = SafeTestRunner(tmp_path)
    assert runner.discover_runner() == "npm_build"
    assert runner.discover_build_runner() == "npm_build"


def test_build_check_is_discovered_separately_from_tests(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({
        "scripts": {"test": "vitest run", "build": "vite build"}
    }), encoding="utf-8")

    runner = SafeTestRunner(tmp_path)
    assert runner.discover_runner() == "npm_test"
    assert runner.discover_build_runner() == "npm_build"
