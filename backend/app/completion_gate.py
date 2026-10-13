"""Evidence gate for task completion.

A provider's terminal event is not enough when the user explicitly requested
automated or browser verification. This module is intentionally pure for easy testing.
"""
from __future__ import annotations

from typing import Any


def evaluate_completion_gate(request: Any, evidence: list[dict[str, Any]], verification_status: str = "") -> dict[str, Any]:
    latest: dict[str, dict[str, Any]] = {}
    for item in evidence:
        kind = str(item.get("kind") or "")
        if kind:
            latest[kind] = item

    failures: list[str] = []
    required_checks: list[str] = []
    verification_requested = bool(
        getattr(request, "auto_verify", False)
        or getattr(request, "acceptance_criteria", [])
        or getattr(request, "browser_verify_url", None)
    )

    if verification_requested:
        required_checks.append("workspace_provenance")
        provenance = latest.get("workspace_provenance")
        if provenance is None:
            failures.append("workspace_provenance_evidence_missing")
        else:
            details = provenance.get("details")
            details = details if isinstance(details, dict) else {}
            if provenance.get("status") != "captured":
                failures.append("workspace_provenance_not_captured")
            if details.get("snapshot_complete") is not True:
                failures.append("workspace_snapshot_incomplete")
            if not details.get("snapshot_sha256"):
                failures.append("workspace_snapshot_fingerprint_missing")

    if bool(getattr(request, "auto_verify", False) or getattr(request, "acceptance_criteria", [])):
        test_item = latest.get("automated_tests")
        build_item = latest.get("build")
        if test_item is None and build_item is None:
            failures.append("automated_test_or_build_evidence_missing")
        for kind, item in (("automated_tests", test_item), ("build", build_item)):
            if item is None:
                continue
            required_checks.append(kind)
            if item.get("status") != "passed":
                failures.append(f"{kind}_not_passed")

    if bool(getattr(request, "browser_verify_url", None)):
        required_checks.append("browser_verification")
        browser_item = latest.get("browser_verification")
        if browser_item is None:
            failures.append("browser_verification_evidence_missing")
        elif browser_item.get("status") != "passed":
            failures.append("browser_verification_not_passed")

    for item in latest.values():
        details = item.get("details")
        if isinstance(details, dict) and details.get("required") is True and item.get("status") != "passed":
            failures.append(f"required_evidence_not_passed:{item.get('kind', 'unknown')}")

    if verification_status == "failed":
        failures.append("verification_status_failed")

    # Preserve order while avoiding duplicate explanations.
    failures = list(dict.fromkeys(failures))
    return {
        "passed": not failures,
        "required_checks": required_checks,
        "failures": failures,
    }