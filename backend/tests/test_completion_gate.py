from types import SimpleNamespace

from app.completion_gate import evaluate_completion_gate


def provenance(status="captured", complete=True, fingerprint="abc123"):
    return {
        "kind": "workspace_provenance",
        "status": status,
        "details": {"snapshot_complete": complete, "snapshot_sha256": fingerprint},
    }


def test_auto_verify_requires_successful_test_or_build_evidence():
    request = SimpleNamespace(auto_verify=True, browser_verify_url=None)
    missing = evaluate_completion_gate(request, [], "passed")
    assert "automated_test_or_build_evidence_missing" in missing["failures"]
    assert "workspace_provenance_evidence_missing" in missing["failures"]
    passed = evaluate_completion_gate(
        request,
        [{"kind": "automated_tests", "status": "passed", "details": {"exit_code": 0}}, provenance()],
        "passed",
    )
    assert passed["passed"] is True
    assert passed["workspace_snapshot_sha256"] == "abc123"
    assert passed["workspace_snapshot_complete"] is True
    assert passed["workspace_provenance_status"] == "captured"
    failed = evaluate_completion_gate(
        request,
        [{"kind": "automated_tests", "status": "failed", "details": {"exit_code": 1}}, provenance()],
        "failed",
    )
    assert "automated_tests_not_passed" in failed["failures"]
    assert "verification_status_not_passed" in failed["failures"]


def test_browser_requirement_needs_explicit_pass_evidence():
    request = SimpleNamespace(auto_verify=False, browser_verify_url="http://localhost:3000")
    missing = evaluate_completion_gate(request, [provenance()], "")
    assert missing["passed"] is False
    assert "browser_verification_evidence_missing" in missing["failures"]

    passed = evaluate_completion_gate(
        request,
        [{"kind": "browser_verification", "status": "passed", "details": {"checks": {"title": True}}}, provenance()],
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


def test_verified_completion_requires_complete_workspace_fingerprint():
    request = SimpleNamespace(auto_verify=True, browser_verify_url=None)
    tests = {"kind": "automated_tests", "status": "passed", "details": {"exit_code": 0}}

    for evidence, expected_failure in (
        ([tests], "workspace_provenance_evidence_missing"),
        ([tests, provenance(status="partial", complete=False)], "workspace_provenance_not_captured"),
        ([tests, provenance(complete=False)], "workspace_snapshot_incomplete"),
        ([tests, provenance(fingerprint="")], "workspace_snapshot_fingerprint_missing"),
    ):
        result = evaluate_completion_gate(request, evidence, "passed")
        assert result["passed"] is False
        assert expected_failure in result["failures"]


def test_requested_verification_rejects_unknown_or_not_requested_status():
    request = SimpleNamespace(auto_verify=True, browser_verify_url=None, acceptance_criteria=[])
    evidence = [
        {"kind": "automated_tests", "status": "passed", "details": {"exit_code": 0}},
        provenance(),
    ]
    for status in ("", "not_requested", "unknown", "failed", "partial"):
        result = evaluate_completion_gate(request, evidence, status)
        assert result["passed"] is False
        assert "verification_status_not_passed" in result["failures"]