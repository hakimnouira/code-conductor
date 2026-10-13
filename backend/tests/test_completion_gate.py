from types import SimpleNamespace

from app.completion_gate import evaluate_completion_gate


def test_auto_verify_requires_successful_test_or_build_evidence():
    request = SimpleNamespace(auto_verify=True, browser_verify_url=None)
    assert evaluate_completion_gate(request, [], "passed")["failures"] == [
        "automated_test_or_build_evidence_missing"
    ]
    passed = evaluate_completion_gate(
        request,
        [{"kind": "automated_tests", "status": "passed", "details": {"exit_code": 0}}],
        "passed",
    )
    assert passed["passed"] is True
    failed = evaluate_completion_gate(
        request,
        [{"kind": "automated_tests", "status": "failed", "details": {"exit_code": 1}}],
        "failed",
    )
    assert "automated_tests_not_passed" in failed["failures"]
    assert "verification_status_failed" in failed["failures"]


def test_browser_requirement_needs_explicit_pass_evidence():
    request = SimpleNamespace(auto_verify=False, browser_verify_url="http://localhost:3000")
    missing = evaluate_completion_gate(request, [], "")
    assert missing["passed"] is False
    assert "browser_verification_evidence_missing" in missing["failures"]

    passed = evaluate_completion_gate(
        request,
        [{"kind": "browser_verification", "status": "passed", "details": {"checks": {"title": True}}}],
        "passed",
    )
    assert passed["passed"] is True


def test_required_evidence_cannot_be_skipped_or_failed():
    request = SimpleNamespace(auto_verify=False, browser_verify_url=None)
    result = evaluate_completion_gate(
        request,
        [{"kind": "custom_acceptance", "status": "skipped", "details": {"required": True}}],
    )
    assert result["passed"] is False
    assert "required_evidence_not_passed:custom_acceptance" in result["failures"]


def test_unrequested_verification_does_not_block_completion():
    request = SimpleNamespace(auto_verify=False, browser_verify_url=None)
    result = evaluate_completion_gate(request, [], "")
    assert result["passed"] is True
    assert result["required_checks"] == []
