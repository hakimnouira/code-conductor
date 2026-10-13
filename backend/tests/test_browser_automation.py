"""Unit tests for evidence-based Playwright verification reports."""
import pytest

from app.browser_automation import _validate_target_url, build_browser_report, evaluate_acceptance_assertions
from app.browser_session import BrowserVerifyPayload
from app.models.schemas import BrowserActionStep


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


def test_acceptance_assertions_check_expected_text_and_selectors():
    assertions = evaluate_acceptance_assertions(
        body_text="Welcome to Code Conductor. Dashboard ready.",
        required_text=["Welcome to Code Conductor", "Dashboard ready"],
        forbidden_text=["Application error"],
        visible_selectors=["main", "[data-testid='task-list']"],
        hidden_selectors=["[role='alert']"],
        selector_visibility={
            "main": {"visible": True},
            "[data-testid='task-list']": {"visible": True},
            "[role='alert']": {"visible": False},
        },
    )
    assert assertions["passed"] is True
    assert all(item["passed"] for item in assertions["required_text"])
    assert assertions["hidden_selectors"][0]["passed"] is True


def test_acceptance_assertions_fail_when_expected_ui_is_missing():
    assertions = evaluate_acceptance_assertions(
        body_text="Welcome. Application error.",
        required_text=["Dashboard"],
        forbidden_text=["Application error"],
        visible_selectors=["main"],
        hidden_selectors=[],
        selector_visibility={"main": {"visible": False}},
    )
    assert assertions["passed"] is False
    assert assertions["required_text"][0]["passed"] is False
    assert assertions["forbidden_text"][0]["passed"] is False
    assert assertions["visible_selectors"][0]["passed"] is False


def test_browser_report_fails_when_acceptance_assertions_fail():
    report = build_browser_report(
        url="http://localhost:5173/",
        title="Dashboard",
        ready_state="complete",
        body_text="Page loaded",
        main_document_status=200,
        page_errors=[],
        console_errors=[],
        http_errors=[],
        failed_requests=[],
        screenshot=None,
        assertions={"passed": False, "required_text": [{"value": "Dashboard", "passed": False}]},
    )
    assert report["success"] is False
    assert report["checks"]["acceptance_assertions_passed"] is False


def test_browser_payload_bounds_assertion_count_and_item_length():
    payload = BrowserVerifyPayload(
        url="http://127.0.0.1:5173",
        required_text=["  " + ("x" * 700) + "  ", "Dashboard"],
    )
    assert len(payload.required_text[0]) == 500
    assert payload.required_text[1] == "Dashboard"
    with pytest.raises(Exception):
        BrowserVerifyPayload(url="http://127.0.0.1:5173", required_text=[str(i) for i in range(21)])


def test_browser_actions_are_included_in_verification_evidence():
    report = build_browser_report(
        url="http://localhost:5173/",
        title="Dashboard",
        ready_state="complete",
        body_text="Dashboard",
        main_document_status=200,
        page_errors=[],
        console_errors=[],
        http_errors=[],
        failed_requests=[],
        screenshot=None,
        action_results=[
            {"index": 0, "action": "fill", "selector": "#search", "passed": True, "duration_ms": 12},
            {"index": 1, "action": "click", "selector": "#submit", "passed": False, "error": "timeout"},
        ],
    )
    assert report["success"] is False
    assert report["checks"]["browser_actions_passed"] is False
    assert report["action_results"][1]["error"] == "timeout"


def test_browser_action_schema_is_bounded_and_requires_values():
    step = BrowserActionStep(action="fill", selector="  input[name='q']  ", value="hello")
    assert step.selector == "input[name='q']"
    assert step.timeout_ms == 5000
    with pytest.raises(Exception):
        BrowserActionStep(action="fill", selector="input[name='q']")
    with pytest.raises(Exception):
        BrowserActionStep(action="click", selector="button", timeout_ms=20000)
    with pytest.raises(Exception):
        BrowserActionStep(action="click", selector="   ")


def test_browser_report_fails_and_labels_screenshot_infrastructure_errors():
    report = build_browser_report(
        url="http://localhost:5173/",
        title="Dashboard",
        ready_state="complete",
        body_text="Dashboard",
        main_document_status=200,
        page_errors=[],
        console_errors=[],
        http_errors=[],
        failed_requests=[],
        screenshot=None,
        verification_errors=["Screenshot capture failed: timeout"],
        screenshot_scope="viewport_fallback_size_limit",
    )
    assert report["success"] is False
    assert report["checks"]["no_verification_errors"] is False
    assert report["console_errors"] == []
    assert report["screenshot_scope"] == "viewport_fallback_size_limit"
