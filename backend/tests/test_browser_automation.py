"""Unit tests for evidence-based Playwright verification reports."""
import pytest

from app.browser_automation import _validate_target_url, build_browser_report


pytestmark = pytest.mark.unit


def test_browser_report_passes_with_clean_runtime_evidence():
    report = build_browser_report(
        url="http://127.0.0.1:5173/",
        title="Dashboard",
        ready_state="complete",
        body_text="Code Conductor dashboard",
        main_document_status=200,
        page_errors=[],
        console_errors=[],
        http_errors=[],
        failed_requests=[],
        screenshot="data:image/png;base64,AA==",
    )
    assert report["success"] is True
    assert report["checks"]["main_document_http_ok"] is True
    assert report["verification_engine"] == "playwright-cdp"


def test_browser_report_fails_for_javascript_and_main_document_errors():
    report = build_browser_report(
        url="http://localhost:5173/",
        title="Vite",
        ready_state="complete",
        body_text="Uncaught exception while loading",
        main_document_status=500,
        page_errors=["TypeError: cannot read property"],
        console_errors=["Uncaught TypeError"],
        http_errors=[{"url": "http://localhost:5173/", "status": 500}],
        failed_requests=[],
        screenshot=None,
    )
    assert report["success"] is False
    assert report["checks"]["no_uncaught_page_errors"] is False
    assert report["checks"]["main_document_http_ok"] is False
    assert "uncaught exception" in report["error_markers"]


def test_subresource_failures_are_reported_without_false_failure():
    report = build_browser_report(
        url="http://localhost:5173/",
        title="App",
        ready_state="interactive",
        body_text="App loaded",
        main_document_status=200,
        page_errors=[],
        console_errors=[],
        http_errors=[{"url": "http://localhost:5173/favicon.ico", "status": 404}],
        failed_requests=[{"url": "http://localhost:5173/analytics", "failure": "blocked"}],
        screenshot=None,
    )
    assert report["success"] is True
    assert report["http_errors"][0]["status"] == 404
    assert report["failed_requests"][0]["failure"] == "blocked"


@pytest.mark.parametrize("url", ["file:///etc/passwd", "javascript:alert(1)", "https://user:pass@example.com", "not-a-url"])
def test_browser_verification_rejects_unsafe_urls(url):
    with pytest.raises(ValueError):
        _validate_target_url(url)



def test_application_assertion_failure_blocks_browser_success():
    report = build_browser_report(
        url="http://localhost:5173/",
        title="Dashboard",
        ready_state="complete",
        body_text="Dashboard loaded",
        main_document_status=200,
        page_errors=[],
        console_errors=[],
        http_errors=[],
        failed_requests=[],
        screenshot="data:image/png;base64,AA==",
        application_assertions=[
            {"kind": "expected_text", "value": "Signed in", "passed": False},
            {"kind": "required_selector", "value": "main", "passed": True},
        ],
    )
    assert report["success"] is False
    assert report["checks"]["application_assertions_passed"] is False
    assert report["application_assertions"][0]["value"] == "Signed in"


def test_application_assertions_are_reported_without_masking_runtime_failures():
    report = build_browser_report(
        url="http://localhost:5173/",
        title="Dashboard",
        ready_state="complete",
        body_text="Dashboard loaded",
        main_document_status=200,
        page_errors=["TypeError: app crashed"],
        console_errors=[],
        http_errors=[],
        failed_requests=[],
        screenshot=None,
        application_assertions=[
            {"kind": "expected_text", "value": "Dashboard", "passed": True},
        ],
    )
    assert report["success"] is False
    assert report["checks"]["application_assertions_passed"] is True
    assert report["checks"]["no_uncaught_page_errors"] is False
