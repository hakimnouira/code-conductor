"""Unit tests for evidence-based Playwright verification reports."""
import pytest

from app.browser_automation import _validate_target_url, build_browser_report, evaluate_acceptance_assertions, execute_browser_steps
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


@pytest.mark.asyncio
async def test_browser_step_runner_executes_actions_in_order_with_bounded_timeouts():
    calls = []

    class FakeLocator:
        @property
        def first(self):
            return self

        async def fill(self, value, timeout):
            calls.append(("fill", value, timeout))

        async def click(self, timeout):
            calls.append(("click", timeout))

        async def wait_for(self, state, timeout):
            calls.append(("wait_for", state, timeout))

    class FakePage:
        def locator(self, selector):
            calls.append(("locator", selector))
            return FakeLocator()

    results = await execute_browser_steps(FakePage(), [
        {"action": "fill", "selector": "#query", "value": "hello", "timeout_ms": 250},
        {"action": "click", "selector": "#submit"},
        {"action": "wait_for_selector", "selector": "#results"},
    ])

    assert [item["passed"] for item in results] == [True, True, True]
    assert calls == [
        ("locator", "#query"), ("fill", "hello", 250),
        ("locator", "#submit"), ("click", 5000),
        ("locator", "#results"), ("wait_for", "visible", 5000),
    ]
    assert all(item["duration_ms"] >= 0 for item in results)


@pytest.mark.asyncio
async def test_browser_step_runner_stops_after_first_failed_action():
    calls = []

    class FakeLocator:
        @property
        def first(self):
            return self

        async def click(self, timeout):
            calls.append("click")
            raise RuntimeError("element did not become actionable")

        async def fill(self, value, timeout):
            calls.append("fill")

    class FakePage:
        def locator(self, selector):
            calls.append(selector)
            return FakeLocator()

    results = await execute_browser_steps(FakePage(), [
        {"action": "click", "selector": "#submit"},
        {"action": "fill", "selector": "#next", "value": "should not run"},
    ])
    assert len(results) == 1
    assert results[0]["passed"] is False
    assert "actionable" in results[0]["error"]
    assert calls == ["#submit", "click"]


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


@pytest.mark.asyncio
async def test_browser_connection_falls_back_to_managed_chromium():
    from app.browser_automation import connect_or_launch_browser

    calls = []

    class Context:
        pass

    class Browser:
        contexts = []
        async def new_context(self):
            calls.append("new_context")
            return Context()

    class Chromium:
        async def connect_over_cdp(self, endpoint, timeout):
            calls.append(("cdp", endpoint, timeout))
            raise ConnectionError("no CDP browser is running")
        async def launch(self, headless):
            calls.append(("launch", headless))
            return Browser()

    class Playwright:
        chromium = Chromium()

    browser, context, engine, owned = await connect_or_launch_browser(Playwright(), "http://127.0.0.1:9222")
    assert isinstance(browser, Browser)
    assert isinstance(context, Context)
    assert engine == "playwright-managed-chromium"
    assert owned is True
    assert calls == [("cdp", "http://127.0.0.1:9222", 5000), ("launch", True), "new_context"]


@pytest.mark.asyncio
async def test_browser_connection_preserves_existing_cdp_browser():
    from app.browser_automation import connect_or_launch_browser

    context = object()
    calls = []

    class Browser:
        contexts = [context]
        async def new_context(self):
            raise AssertionError("Should reuse the connected browser context")

    browser = Browser()

    class Chromium:
        async def connect_over_cdp(self, endpoint, timeout):
            calls.append(("cdp", endpoint, timeout))
            return browser
        async def launch(self, headless):
            raise AssertionError("Must not launch a second browser when CDP works")

    class Playwright:
        chromium = Chromium()

    got_browser, got_context, engine, owned = await connect_or_launch_browser(Playwright(), "http://127.0.0.1:9222")
    assert got_browser is browser
    assert got_context is context
    assert engine == "playwright-cdp"
    assert owned is False
    assert calls == [("cdp", "http://127.0.0.1:9222", 5000)]


@pytest.mark.asyncio
async def test_browser_connection_explains_missing_playwright_browser():
    from app.browser_automation import connect_or_launch_browser

    class Chromium:
        async def connect_over_cdp(self, endpoint, timeout):
            raise ConnectionError("CDP unavailable")
        async def launch(self, headless):
            raise RuntimeError("executable missing")

    class Playwright:
        chromium = Chromium()

    with pytest.raises(RuntimeError, match="python -m playwright install chromium"):
        await connect_or_launch_browser(Playwright(), "http://127.0.0.1:9222")



@pytest.mark.asyncio
async def test_browser_step_runner_supports_explicit_dom_assertions():
    class FakeLocator:
        @property
        def first(self):
            return self

        async def inner_text(self, timeout):
            return "Saved successfully"

        async def input_value(self, timeout):
            return "alice@example.com"

        async def count(self):
            return 2

        async def is_enabled(self, timeout):
            return True

    class FakePage:
        def locator(self, selector):
            return FakeLocator()

    results = await execute_browser_steps(FakePage(), [
        {"action": "assert_text", "selector": "[role='status']", "value": "Saved"},
        {"action": "assert_value", "selector": "#email", "value": "alice@example.com"},
        {"action": "assert_count", "selector": ".result-row", "value": "2"},
        {"action": "assert_enabled", "selector": "#submit"},
        {"action": "assert_disabled", "selector": "#cancel"},
    ])

    assert [item["passed"] for item in results] == [True, True, True, True, False]
    assert results[0]["observed_text"] == "Saved successfully"
    assert results[1]["observed_value"] == "alice@example.com"
    assert results[2]["observed_count"] == 2
    assert results[3]["observed_enabled"] is True


def test_browser_assertion_schema_requires_valid_expected_values():
    assert BrowserActionStep(
        action="assert_text", selector="[role='status']", value="Saved"
    ).value == "Saved"
    assert BrowserActionStep(
        action="assert_count", selector=".row", value="0"
    ).value == "0"
    with pytest.raises(Exception):
        BrowserActionStep(action="assert_text", selector="[role='status']")
    with pytest.raises(Exception):
        BrowserActionStep(action="assert_count", selector=".row", value="-1")
    with pytest.raises(Exception):
        BrowserActionStep(action="assert_count", selector=".row", value="many")

def test_browser_report_fails_closed_when_main_document_status_is_unknown():
    report = build_browser_report(
        url="http://localhost:5173/",
        title="Dashboard",
        ready_state="complete",
        body_text="Dashboard rendered",
        main_document_status=None,
        page_errors=[],
        console_errors=[],
        http_errors=[],
        failed_requests=[],
        screenshot="data:image/png;base64,ZmFrZQ==",
    )
    assert report["success"] is False
    assert report["checks"]["main_document_http_ok"] is False


def test_browser_report_rejects_invalid_main_document_status_values():
    for status in (0, -1, 400, 503):
        report = build_browser_report(
            url="http://localhost:5173/",
            title="Dashboard",
            ready_state="complete",
            body_text="Dashboard rendered",
            main_document_status=status,
            page_errors=[],
            console_errors=[],
            http_errors=[],
            failed_requests=[],
            screenshot="data:image/png;base64,ZmFrZQ==",
        )
        assert report["checks"]["main_document_http_ok"] is False
