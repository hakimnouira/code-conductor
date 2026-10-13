"""Playwright-backed browser verification over the user's existing Chromium CDP session.

This attaches to an explicitly configured local browser; it does not download or launch
a browser binary. Diagnostics distinguish fatal page/runtime failures from request noise.
"""
from __future__ import annotations

import asyncio
import base64
import time
from typing import Any
from urllib.parse import urlsplit


def _validate_target_url(url: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("Verification URL must be HTTP(S), include a host, and contain no embedded credentials.")


async def execute_browser_steps(page: Any, steps: list[Any]) -> list[dict[str, Any]]:
    """Execute a bounded, selector-only sequence and stop at the first failure."""
    results: list[dict[str, Any]] = []
    for index, raw_step in enumerate(steps[:20]):
        step = raw_step.model_dump() if hasattr(raw_step, "model_dump") else dict(raw_step)
        action = str(step.get("action") or "")
        selector = str(step.get("selector") or "").strip()
        value = step.get("value")
        timeout_ms = min(max(int(step.get("timeout_ms") or 5000), 100), 10000)
        started = time.monotonic()
        result: dict[str, Any] = {
            "index": index,
            "action": action,
            "selector": selector[:500],
            "passed": False,
        }
        try:
            if not selector:
                raise ValueError("Selector must not be blank.")
            locator = page.locator(selector).first
            if action == "click":
                await locator.click(timeout=timeout_ms)
            elif action == "fill":
                await locator.fill(str(value or ""), timeout=timeout_ms)
            elif action == "check":
                await locator.check(timeout=timeout_ms)
            elif action == "uncheck":
                await locator.uncheck(timeout=timeout_ms)
            elif action == "select_option":
                await locator.select_option(str(value), timeout=timeout_ms)
            elif action == "press":
                await locator.press(str(value), timeout=timeout_ms)
            elif action == "wait_for_selector":
                await locator.wait_for(state="visible", timeout=timeout_ms)
            else:
                raise ValueError(f"Unsupported browser action: {action}")
            result["passed"] = True
        except Exception as exc:
            result["error"] = str(exc)[:500]
        result["duration_ms"] = round((time.monotonic() - started) * 1000, 2)
        results.append(result)
        if not result["passed"]:
            break
    return results


def evaluate_acceptance_assertions(
    *,
    body_text: str,
    required_text: list[str] | None = None,
    forbidden_text: list[str] | None = None,
    visible_selectors: list[str] | None = None,
    hidden_selectors: list[str] | None = None,
    selector_visibility: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Evaluate explicit user acceptance criteria against captured page evidence."""
    page_text = body_text.casefold()
    selector_visibility = selector_visibility or {}
    required_results = [
        {"value": value, "passed": value.casefold() in page_text}
        for value in (required_text or [])
    ]
    forbidden_results = [
        {"value": value, "passed": value.casefold() not in page_text}
        for value in (forbidden_text or [])
    ]
    visible_results = []
    for selector in visible_selectors or []:
        state = selector_visibility.get(selector, {})
        visible_results.append({
            "selector": selector,
            "passed": bool(state.get("visible")) and not state.get("error"),
            **({"error": str(state["error"])[:300]} if state.get("error") else {}),
        })
    hidden_results = []
    for selector in hidden_selectors or []:
        state = selector_visibility.get(selector, {})
        hidden_results.append({
            "selector": selector,
            "passed": not bool(state.get("visible")) and not state.get("error"),
            **({"error": str(state["error"])[:300]} if state.get("error") else {}),
        })
    groups = [required_results, forbidden_results, visible_results, hidden_results]
    return {
        "passed": all(item["passed"] for group in groups for item in group),
        "required_text": required_results,
        "forbidden_text": forbidden_results,
        "visible_selectors": visible_results,
        "hidden_selectors": hidden_results,
    }


def build_browser_report(
    *,
    url: str,
    title: str,
    ready_state: str,
    body_text: str,
    main_document_status: int | None,
    page_errors: list[str],
    console_errors: list[str],
    http_errors: list[dict[str, Any]],
    failed_requests: list[dict[str, Any]],
    screenshot: str | None,
    assertions: dict[str, Any] | None = None,
    verification_errors: list[str] | None = None,
    screenshot_scope: str = "full_page",
    action_results: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a stable, testable verification report from captured browser evidence."""
    text = body_text[:12000]
    lower_text = text.lower()
    error_markers = [
        marker for marker in (
            "application error", "internal server error", "something went wrong",
            "uncaught exception", "failed to compile", "vite error",
        ) if marker in lower_text
    ]
    assertions = assertions or {"passed": True, "required_text": [], "forbidden_text": [], "visible_selectors": [], "hidden_selectors": []}
    action_results = action_results or []
    checks = {
        "page_loaded": ready_state in ("interactive", "complete"),
        "main_document_http_ok": main_document_status is None or main_document_status < 400,
        "no_uncaught_page_errors": not page_errors,
        "no_console_errors": not console_errors,
        "no_common_error_markers": not error_markers,
        "acceptance_assertions_passed": bool(assertions.get("passed", True)),
        "browser_actions_passed": all(item.get("passed", False) for item in action_results),
        "no_verification_errors": not (verification_errors or []),
    }
    return {
        "success": all(checks.values()),
        "url": url,
        "title": title[:500],
        "ready_state": ready_state,
        "checks": checks,
        "main_document_status": main_document_status,
        "page_errors": page_errors[:30],
        "console_errors": console_errors[:30],
        "http_errors": http_errors[:50],
        "failed_requests": failed_requests[:50],
        "error_markers": error_markers,
        "acceptance_assertions": assertions,
        "action_results": action_results[:20],
        "verification_errors": (verification_errors or [])[:20],
        "screenshot_scope": screenshot_scope,
        "body_text_excerpt": text[:6000],
        "screenshot": screenshot,
        "verification_engine": "playwright-cdp",
        "limitations": [
            "This is browser-observed evidence, not a proof of application correctness or security.",
            "Failed subresource requests are reported separately and do not alone fail the check.",
        ],
    }


async def run_browser_automation(
    url: str,
    wait_seconds: int = 2,
    *,
    required_text: list[str] | None = None,
    forbidden_text: list[str] | None = None,
    visible_selectors: list[str] | None = None,
    hidden_selectors: list[str] | None = None,
    steps: list[Any] | None = None,
) -> dict[str, Any]:
    """Navigate a fresh tab, collect runtime/network evidence, and capture a screenshot."""
    _validate_target_url(url)
    required_text = [value.strip()[:500] for value in (required_text or []) if isinstance(value, str) and value.strip()][:20]
    forbidden_text = [value.strip()[:500] for value in (forbidden_text or []) if isinstance(value, str) and value.strip()][:20]
    visible_selectors = [value.strip()[:500] for value in (visible_selectors or []) if isinstance(value, str) and value.strip()][:20]
    hidden_selectors = [value.strip()[:500] for value in (hidden_selectors or []) if isinstance(value, str) and value.strip()][:20]
    from playwright.async_api import async_playwright

    from app.browser_session import get_current_endpoint

    endpoint = get_current_endpoint()
    page_errors: list[str] = []
    console_errors: list[str] = []
    verification_errors: list[str] = []
    http_errors: list[dict[str, Any]] = []
    failed_requests: list[dict[str, Any]] = []
    main_document_status: int | None = None
    screenshot_uri: str | None = None
    title = ""
    ready_state = "unavailable"
    body_text = ""
    assertions: dict[str, Any] = {"passed": True, "required_text": [], "forbidden_text": [], "visible_selectors": [], "hidden_selectors": []}
    actual_url = url
    screenshot_scope = "full_page"
    action_results: list[dict[str, Any]] = []
    page = None
    playwright = await async_playwright().start()
    try:
        browser = await playwright.chromium.connect_over_cdp(endpoint, timeout=5000)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = await context.new_page()
        page.set_default_timeout(7000)
        page.on("pageerror", lambda exc: page_errors.append(str(exc)[:500]))
        page.on("console", lambda msg: console_errors.append(msg.text[:500]) if msg.type == "error" else None)

        def on_response(response: Any) -> None:
            nonlocal main_document_status
            try:
                request = response.request
                if int(response.status) >= 400:
                    http_errors.append({
                        "url": str(response.url)[:1000],
                        "status": int(response.status),
                        "resource_type": str(request.resource_type),
                    })
                try:
                    if request.is_navigation_request() and request.frame == page.main_frame:
                        main_document_status = int(response.status)
                except Exception:
                    pass
            except Exception:
                return

        page.on("response", on_response)
        page.on("requestfailed", lambda request: failed_requests.append({
            "url": str(request.url)[:1000],
            "failure": str(request.failure or "request failed")[:300],
            "resource_type": str(request.resource_type),
        }))
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                # SPAs, websockets and polling apps may never become network-idle.
                pass
            await asyncio.sleep(min(max(int(wait_seconds), 0), 10))
            actual_url = page.url
            title = await page.title()
            ready_state = await page.evaluate("document.readyState")
            body_text = await page.locator("body").inner_text(timeout=3000)
        except Exception as exc:
            page_errors.append(f"Navigation or page inspection failed: {str(exc)[:500]}")
            try:
                actual_url = page.url
                title = await page.title()
                ready_state = await page.evaluate("document.readyState")
                body_text = await page.locator("body").inner_text(timeout=1000)
            except Exception:
                pass
        action_results = await execute_browser_steps(page, steps or [])

        selector_visibility: dict[str, dict[str, Any]] = {}
        for selector in dict.fromkeys(visible_selectors + hidden_selectors):
            try:
                locator = page.locator(selector)
                count = await locator.count()
                visible = bool(count and await locator.first.is_visible())
                selector_visibility[selector] = {"visible": visible}
            except Exception as selector_error:
                selector_visibility[selector] = {"visible": False, "error": str(selector_error)[:300]}
        assertions = evaluate_acceptance_assertions(
            body_text=body_text,
            required_text=required_text,
            forbidden_text=forbidden_text,
            visible_selectors=visible_selectors,
            hidden_selectors=hidden_selectors,
            selector_visibility=selector_visibility,
        )
        try:
            image = await page.screenshot(type="png", full_page=True, animations="disabled", timeout=5000)
            if len(image) > 8 * 1024 * 1024:
                image = await page.screenshot(type="png", full_page=False, animations="disabled", timeout=5000)
                screenshot_scope = "viewport_fallback_size_limit"
            screenshot_uri = "data:image/png;base64," + base64.b64encode(image).decode("ascii")
        except Exception as exc:
            verification_errors.append(f"Screenshot capture failed: {str(exc)[:300]}")
        return build_browser_report(
            url=actual_url,
            title=title,
            ready_state=ready_state,
            body_text=body_text,
            main_document_status=main_document_status,
            page_errors=page_errors,
            console_errors=console_errors,
            http_errors=http_errors,
            failed_requests=failed_requests,
            screenshot=screenshot_uri,
            assertions=assertions,
            verification_errors=verification_errors,
            screenshot_scope=screenshot_scope,
            action_results=action_results,
        )
    finally:
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass
        # Stop the Playwright client without shutting down the user's browser.
        await playwright.stop()
