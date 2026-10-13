"""Integrity checks for the offline coding-agent benchmark suite."""
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SUITE = ROOT / "backend" / "benchmarks" / "suite.json"


def test_benchmark_suite_has_unique_cases_and_executable_acceptance_tests():
    suite = json.loads(SUITE.read_text(encoding="utf-8"))
    cases = suite["tasks"]
    ids = [case["id"] for case in cases]
    assert cases
    assert len(ids) == len(set(ids))

    for case in cases:
        fixture = ROOT / "backend" / "benchmarks" / "fixtures" / case["fixture"]
        assert fixture.is_dir(), case["id"]
        assert case["prompt"].strip()
        assert case.get("expected_verification_status", "passed") == "passed"
        if (fixture / "package.json").exists():
            package = json.loads((fixture / "package.json").read_text(encoding="utf-8"))
            assert package.get("scripts", {}).get("test"), case["id"]
            assert list(fixture.glob("*.test.js")), case["id"]
        else:
            assert list(fixture.glob("test_*.py")), case["id"]
