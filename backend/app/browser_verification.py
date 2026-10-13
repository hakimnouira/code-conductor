"""Playwright-backed application verification with structured evidence."""
from __future__ import annotations

from typing import Any
from pydantic import BaseModel, Field


class BrowserAssertions(BaseModel):
    expected_text: list[str] = Field(default_factory=list, max_length=30)
    forbidden_text: list[str] = Field(default_factory=list, max_length=30)
    required_selectors: list[str] = Field(default_factory=list, max_length=30)
    expected_url_contains: str | None = Field(default=None, max_length=512)


async def verify_with_playwright(payload: Any) -> dict[str, Any]:
    """Launch an isolated Chromium context and collect page, console, network and assertion evidence."""
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright is not installed. Run scripts/setup.ps1 or install backend requirements."
        ) from exc

    browser = None
    console_errors: list[str] = []
    page_errors: list[str] = []
    failed_requests: list[dict[str, str]] = []
    bad_responses: list[dict[str, Any]] = []
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context(viewport={"width": 1440, "height": 1000})
            page = await context.new_page()
            page.on("console", lambda msg: console_errors.append(msg.text[:500]) if msg.type == "error" and len(console_errors) < 30 else None)
            page.on("pageerror", lambda exc: page_errors.append(str(exc)[:500]) if len(page_errors) < 30 else None)
            page.on("requestfailed", lambda req: failed_requests.append({
                "url": req.url[:1000], "error": (req.failure or "request failed")[:300]
            }) if len(failed_requests) < 30 else None)
            page.on("response", lambda response: bad_responses.append({
                "url": response.url[:1000], "status": response.status
            }) if response.status >= 400 and len(bad_responses) < 30 else None)

            response = await page.goto(payload.url, wait_until="domcontentloaded", timeout=20000)
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                # Long polling/WebSockets should not fail verification by themselves.
                pass
            if payload.wait_seconds:
                await page.wait_for_timeout(min(int(payload.wait_seconds), 10) * 1000)

            page_state = await page.evaluate("""() => ({
                url: location.href,
                title: document.title || '',
                ready_state: document.readyState,
                body_text: (document.body?.innerText || '').slice(0, 12000),
                body_text_length: (document.body?.innerText || '').length,
                links: document.querySelectorAll('a[href]').length,
                buttons: document.querySelectorAll('button, [role=button]').length,
                inputs: document.querySelectorAll('input, textarea, select').length,
                visible_error_markers: ['Application error', 'Internal Server Error', 'This site can’t be reached', 'This site can\'t be reached'].filter(marker => (document.body?.innerText || '').includes(marker))
            })""")
            assertions = getattr(payload, "assertions", None)
            expected_text = list(getattr(assertions, "expected_text", []) or [])
            forbidden_text = list(getattr(assertions, "forbidden_text", []) or [])
            required_selectors = list(getattr(assertions, "required_selectors", []) or [])
            expected_url_contains = getattr(assertions, "expected_url_contains", None)
            text = str(page_state.get("body_text", ""))
            assertion_results: list[dict[str, Any]] = []
            for expected in expected_text:
                assertion_results.append({"kind": "expected_text", "value": expected, "passed": expected.casefold() in text.casefold()})
            for forbidden in forbidden_text:
                assertion_results.append({"kind": "forbidden_text", "value": forbidden, "passed": forbidden.casefold() not in text.casefold()})
            for selector in required_selectors:
                try:
                    count = await page.locator(selector).count()
                    assertion_results.append({"kind": "required_selector", "value": selector, "count": count, "passed": count > 0})
                except Exception as exc:
                    assertion_results.append({"kind": "required_selector", "value": selector, "passed": False, "error": str(exc)[:250]})
            if expected_url_contains:
                assertion_results.append({"kind": "expected_url", "value": expected_url_contains, "passed": expected_url_contains in str(page_state.get("url", ""))})

            screenshot = await page.screenshot(type="png", full_page=True, animations="disabled", timeout=10000)
            checks = {
                "http_response_ok": response is not None and response.status < 400,
                "document_loaded": page_state.get("ready_state") in ("interactive", "complete"),
                "has_title": bool(str(page_state.get("title", "")).strip()),
                "has_body_content": page_state.get("body_text_length", 0) > 0,
                "no_common_error_page": not page_state.get("visible_error_markers"),
                "no_uncaught_page_errors": not page_errors,
                "no_console_errors": not console_errors,
                "no_failed_requests": not failed_requests,
                "no_http_error_responses": not bad_responses,
                "assertions_passed": all(item["passed"] for item in assertion_results),
            }
            return {
                "success": all(checks.values()),
                "engine": "playwright-chromium",
                "url": page_state.get("url", payload.url),
                "title": page_state.get("title", ""),
                "ready_state": page_state.get("ready_state", "unknown"),
                "http_status": response.status if response else None,
                "body_text_excerpt": str(page_state.get("body_text", ""))[:6000],
                "counts": {key: page_state.get(key, 0) for key in ("links", "buttons", "inputs")},
                "error_markers": page_state.get("visible_error_markers", []),
                "checks": checks,
                "assertions": assertion_results,
                "console_errors": console_errors,
                "page_errors": page_errors,
                "failed_requests": failed_requests,
                "http_error_responses": bad_responses,
                "screenshot": "data:image/png;base64," + __import__("base64").b64encode(screenshot).decode("ascii"),
                "note": "Playwright verification checks browser console, uncaught page errors, failed requests, HTTP error responses, explicit assertions and a full-page screenshot. Add application-specific assertions for meaningful end-to-end coverage.",
            }
    finally:
        if browser is not None:
            await browser.close()
