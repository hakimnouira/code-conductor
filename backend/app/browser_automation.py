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
    checks = {
        "page_loaded": ready_state in ("interactive", "complete"),
        "main_document_http_ok": main_document_status is None or main_document_status < 400,
        "no_uncaught_page_errors": not page_errors,
        "no_console_errors": not console_errors,
        "no_common_error_markers": not error_markers,
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
        "body_text_excerpt": text[:6000],
        "screenshot": screenshot,
        "verification_engine": "playwright-cdp",
        "limitations": [
            "This is browser-observed evidence, not a proof of application correctness or security.",
            "Failed subresource requests are reported separately and do not alone fail the check.",
        ],
    }


async def run_browser_automation(url: str, wait_seconds: int = 2) -> dict[str, Any]:
    """Navigate a fresh tab, collect runtime/network evidence, and capture a screenshot."""
    _validate_target_url(url)
    from playwright.async_api import async_playwright

    from app.browser_session import get_current_endpoint

    endpoint = get_current_endpoint()
    page_errors: list[str] = []
    console_errors: list[str] = []
    http_errors: list[dict[str, Any]] = []
    failed_requests: list[dict[str, Any]] = []
    main_document_status: int | None = None
    screenshot_uri: str | None = None
    title = ""
    ready_state = "unavailable"
    body_text = ""
    actual_url = url
    browser = None
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
                if request.is_navigation_request() and request.frame == page.main_frame:
                    main_document_status = int(response.status)
                if int(response.status) >= 400:
                    http_errors.append({
                        "url": str(response.url)[:1000],
                        "status": int(response.status),
                        "resource_type": str(request.resource_type),
                    })
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
        try:
            image = await page.screenshot(type="png", full_page=True, animations="disabled", timeout=5000)
            screenshot_uri = "data:image/png;base64," + base64.b64encode(image).decode("ascii")
        except Exception as exc:
            console_errors.append(f"Screenshot capture failed: {str(exc)[:300]}")
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
        )
    finally:
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass
        # Stop the Playwright client without shutting down the user's browser.
        await playwright.stop()
