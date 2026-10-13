"""Browser session attachment and automation bridge.

Enables Code Conductor to attach directly to an active browser session
via Chrome DevTools Protocol (CDP), discover active tabs, switch targets,
navigate, evaluate scripts, and capture screenshots.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import urllib.parse
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from app.core.security import require_bearer_token
from app.browser_automation import run_browser_automation

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/browser", tags=["browser"])

# Default to standard Chrome DevTools Protocol port or environment override
_DEFAULT_CDP_ENDPOINT = os.getenv("BROWSER_CDP_ENDPOINT", "http://127.0.0.1:9222")
_CURRENT_CDP_ENDPOINT = _DEFAULT_CDP_ENDPOINT


class BrowserConfigPayload(BaseModel):
    endpoint: str = Field(..., description="HTTP URL of the browser DevTools Protocol endpoint (e.g. http://localhost:9222)")


class BrowserNavigatePayload(BaseModel):
    url: str = Field(..., description="Target URL to navigate to")
    tab_id: str | None = Field(default=None, description="Optional target tab ID; defaults to first active tab")


class BrowserEvalPayload(BaseModel):
    expression: str = Field(..., description="JavaScript expression to evaluate in the tab")
    tab_id: str | None = Field(default=None, description="Optional target tab ID")


class BrowserVerifyPayload(BaseModel):
    url: str = Field(..., description="HTTP(S) URL to open and verify")
    tab_id: str | None = None
    wait_seconds: int = Field(default=2, ge=1, le=10)
    required_text: list[str] = Field(default_factory=list, max_length=20)
    forbidden_text: list[str] = Field(default_factory=list, max_length=20)
    visible_selectors: list[str] = Field(default_factory=list, max_length=20)
    hidden_selectors: list[str] = Field(default_factory=list, max_length=20)


def get_current_endpoint() -> str:
    global _CURRENT_CDP_ENDPOINT
    return _CURRENT_CDP_ENDPOINT.rstrip("/")


def set_current_endpoint(endpoint: str) -> str:
    global _CURRENT_CDP_ENDPOINT
    cleaned = endpoint.strip().rstrip("/")
    if not (cleaned.startswith("http://") or cleaned.startswith("https://")):
        cleaned = f"http://{cleaned}"
    _CURRENT_CDP_ENDPOINT = cleaned
    return _CURRENT_CDP_ENDPOINT


async def _fetch_cdp_json(path: str, timeout: float = 3.0) -> Any:
    import httpx
    base = get_current_endpoint()
    url = f"{base}{path}"
    async with httpx.AsyncClient(timeout=timeout) as client:
        res = await client.get(url)
        res.raise_for_status()
        return res.json()


@router.get("/status")
async def get_browser_status(_: str = Depends(require_bearer_token)) -> dict[str, Any]:
    """Check connectivity to the active browser instance."""
    endpoint = get_current_endpoint()
    try:
        version_data = await _fetch_cdp_json("/json/version", timeout=2.0)
        tabs_data = await _fetch_cdp_json("/json/list", timeout=2.0)
        tabs = [t for t in tabs_data if isinstance(t, dict) and t.get("type") in ("page", "iframe", None)]
        return {
            "connected": True,
            "endpoint": endpoint,
            "browser": version_data.get("Browser", "Unknown"),
            "protocol_version": version_data.get("Protocol-Version", "1.3"),
            "user_agent": version_data.get("User-Agent", ""),
            "tab_count": len(tabs),
            "web_socket_debugger_url": version_data.get("webSocketDebuggerUrl"),
        }
    except Exception as exc:
        return {
            "connected": False,
            "endpoint": endpoint,
            "error": str(exc),
            "tab_count": 0,
        }


@router.post("/configure")
async def configure_browser_endpoint(
    payload: BrowserConfigPayload,
    _: str = Depends(require_bearer_token),
) -> dict[str, Any]:
    """Switch or update the browser DevTools Protocol target endpoint."""
    new_endpoint = set_current_endpoint(payload.endpoint)
    # Immediately check connectivity with the new endpoint
    try:
        version_data = await _fetch_cdp_json("/json/version", timeout=2.0)
        return {
            "success": True,
            "endpoint": new_endpoint,
            "connected": True,
            "browser": version_data.get("Browser", "Unknown"),
        }
    except Exception as exc:
        return {
            "success": True,
            "endpoint": new_endpoint,
            "connected": False,
            "warning": f"Endpoint updated to {new_endpoint}, but connection test failed: {exc}",
        }


@router.get("/tabs")
async def list_browser_tabs(_: str = Depends(require_bearer_token)) -> list[dict[str, Any]]:
    """List all open pages/tabs in the attached browser session."""
    try:
        tabs = await _fetch_cdp_json("/json/list")
        if not isinstance(tabs, list):
            return []
        sanitized = []
        for t in tabs:
            if not isinstance(t, dict):
                continue
            tab_type = t.get("type", "page")
            if tab_type in ("page", "other", None):
                sanitized.append({
                    "id": t.get("id"),
                    "title": t.get("title", "Untitled"),
                    "url": t.get("url", ""),
                    "type": tab_type,
                    "webSocketDebuggerUrl": t.get("webSocketDebuggerUrl"),
                })
        return sanitized
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Unable to communicate with browser at {get_current_endpoint()}: {exc}",
        )


@router.post("/tabs/open")
async def open_browser_tab(
    url: str = Query("https://google.com"),
    _: str = Depends(require_bearer_token),
) -> dict[str, Any]:
    """Open a new tab at the given URL in the attached browser."""
    import httpx
    base = get_current_endpoint()
    encoded_url = urllib.parse.quote(url, safe="")
    target = f"{base}/json/new?{encoded_url}"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            res = await client.put(target)
            res.raise_for_status()
            return res.json()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to open browser tab: {exc}",
        )


@router.post("/tabs/activate/{tab_id}")
async def activate_browser_tab(
    tab_id: str,
    _: str = Depends(require_bearer_token),
) -> dict[str, Any]:
    """Bring a specific browser tab into active focus."""
    import httpx
    base = get_current_endpoint()
    target = f"{base}/json/activate/{tab_id}"
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            res = await client.get(target)
            return {"success": res.status_code == 200, "tab_id": tab_id}
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to activate tab {tab_id}: {exc}",
        )


@router.post("/tabs/close/{tab_id}")
async def close_browser_tab(
    tab_id: str,
    _: str = Depends(require_bearer_token),
) -> dict[str, Any]:
    """Close a specific browser tab."""
    import httpx
    base = get_current_endpoint()
    target = f"{base}/json/close/{tab_id}"
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            res = await client.get(target)
            return {"success": res.status_code == 200, "tab_id": tab_id}
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to close tab {tab_id}: {exc}",
        )


@router.post("/navigate")
async def navigate_browser(
    payload: BrowserNavigatePayload,
    _: str = Depends(require_bearer_token),
) -> dict[str, Any]:
    """Navigate an active tab to a new URL via WebSocket CDP command."""
    import websockets
    tabs = await _fetch_cdp_json("/json/list")
    target_tab = None
    if payload.tab_id:
        target_tab = next((t for t in tabs if t.get("id") == payload.tab_id), None)
    if not target_tab:
        target_tab = next((t for t in tabs if t.get("type") == "page"), None)

    if not target_tab or not target_tab.get("webSocketDebuggerUrl"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No navigable page tab found in the attached browser session",
        )

    ws_url = target_tab["webSocketDebuggerUrl"]
    try:
        async with websockets.connect(ws_url, close_timeout=3.0) as ws:
            cmd = {
                "id": 1,
                "method": "Page.navigate",
                "params": {"url": payload.url},
            }
            await ws.send(json.dumps(cmd))
            raw = await ws.recv()
            resp = json.loads(raw)
            return {
                "success": "error" not in resp,
                "tab_id": target_tab.get("id"),
                "result": resp.get("result", {}),
            }
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"WebSocket CDP navigation failed: {exc}",
        )


@router.post("/eval")
async def eval_browser_script(
    payload: BrowserEvalPayload,
    _: str = Depends(require_bearer_token),
) -> dict[str, Any]:
    """Evaluate JavaScript expression in the tab and return result."""
    import websockets
    tabs = await _fetch_cdp_json("/json/list")
    target_tab = None
    if payload.tab_id:
        target_tab = next((t for t in tabs if t.get("id") == payload.tab_id), None)
    if not target_tab:
        target_tab = next((t for t in tabs if t.get("type") == "page"), None)

    if not target_tab or not target_tab.get("webSocketDebuggerUrl"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No active page tab found for evaluation",
        )

    ws_url = target_tab["webSocketDebuggerUrl"]
    try:
        async with websockets.connect(ws_url, close_timeout=3.0) as ws:
            cmd = {
                "id": 2,
                "method": "Runtime.evaluate",
                "params": {
                    "expression": payload.expression,
                    "returnByValue": True,
                    "awaitPromise": True,
                },
            }
            await ws.send(json.dumps(cmd))
            raw = await ws.recv()
            resp = json.loads(raw)
            val = resp.get("result", {}).get("result", {}).get("value")
            return {
                "success": "error" not in resp,
                "tab_id": target_tab.get("id"),
                "value": val,
                "raw": resp.get("result"),
            }
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"JavaScript evaluation failed: {exc}",
        )


@router.get("/screenshot")
async def capture_browser_screenshot(
    tab_id: str | None = None,
    _: str = Depends(require_bearer_token),
) -> dict[str, Any]:
    """Capture a screenshot from the active tab and return base64 PNG."""
    import websockets
    tabs = await _fetch_cdp_json("/json/list")
    target_tab = None
    if tab_id:
        target_tab = next((t for t in tabs if t.get("id") == tab_id), None)
    if not target_tab:
        target_tab = next((t for t in tabs if t.get("type") == "page"), None)

    if not target_tab or not target_tab.get("webSocketDebuggerUrl"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No active page tab available to screenshot",
        )

    ws_url = target_tab["webSocketDebuggerUrl"]
    try:
        async with websockets.connect(ws_url, close_timeout=5.0) as ws:
            cmd = {
                "id": 3,
                "method": "Page.captureScreenshot",
                "params": {"format": "png"},
            }
            await ws.send(json.dumps(cmd))
            raw = await ws.recv()
            resp = json.loads(raw)
            data = resp.get("result", {}).get("data", "")
            return {
                "success": bool(data),
                "tab_id": target_tab.get("id"),
                "image_data": f"data:image/png;base64,{data}" if data else None,
            }
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Screenshot capture failed: {exc}",
        )


@router.post("/automation-test")
async def browser_automation_test(
    payload: BrowserVerifyPayload,
    _: str = Depends(require_bearer_token),
) -> dict[str, Any]:
    """Run Playwright-based verification in the configured Chromium browser."""
    try:
        return await run_browser_automation(
            payload.url,
            wait_seconds=payload.wait_seconds,
            required_text=payload.required_text,
            forbidden_text=payload.forbidden_text,
            visible_selectors=payload.visible_selectors,
            hidden_selectors=payload.hidden_selectors,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("Browser automation verification failed")
        raise HTTPException(status_code=502, detail=f"Browser automation verification failed: {str(exc)[:500]}")


@router.post("/verify")
async def verify_browser_page(
    payload: BrowserVerifyPayload,
    _: str = Depends(require_bearer_token),
) -> dict[str, Any]:
    """Navigate to a local/dev URL and collect a bounded page smoke-check plus screenshot."""
    parsed = urllib.parse.urlparse(payload.url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise HTTPException(status_code=400, detail="Verification URL must be a valid HTTP(S) URL without embedded credentials.")

    import websockets

    tabs = await _fetch_cdp_json("/json/list")
    target_tab = next((t for t in tabs if payload.tab_id and t.get("id") == payload.tab_id), None)
    if not target_tab:
        target_tab = next((t for t in tabs if t.get("type") == "page"), None)
    if not target_tab or not target_tab.get("webSocketDebuggerUrl"):
        raise HTTPException(status_code=404, detail="No active page tab found. Connect or launch a browser first.")

    ws_url = target_tab["webSocketDebuggerUrl"]

    async def command(command_id: int, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        async with websockets.connect(ws_url, close_timeout=3.0) as ws:
            await ws.send(json.dumps({"id": command_id, "method": method, "params": params or {}}))
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                message = json.loads(raw)
                if message.get("id") == command_id:
                    if "error" in message:
                        raise RuntimeError(message["error"].get("message", "CDP command failed"))
                    return message.get("result", {})

    try:
        await command(101, "Page.enable")
        await command(102, "Page.navigate", {"url": payload.url})
        await asyncio.sleep(payload.wait_seconds)
        evaluated = await command(103, "Runtime.evaluate", {
            "expression": """(() => ({
              url: location.href,
              title: document.title || '',
              ready_state: document.readyState,
              body_text: (document.body?.innerText || '').slice(0, 6000),
              body_text_length: (document.body?.innerText || '').length,
              links: document.querySelectorAll('a[href]').length,
              buttons: document.querySelectorAll('button, [role=button]').length,
              inputs: document.querySelectorAll('input, textarea, select').length,
              visible_error_markers: ['Application error', 'Internal Server Error', 'This site can’t be reached', 'This site can't be reached'].filter(marker => (document.body?.innerText || '').includes(marker))
            }))()""",
            "returnByValue": True,
            "awaitPromise": True,
        })
        page = evaluated.get("result", {}).get("value", {})
        screenshot = await command(104, "Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
        image_data = screenshot.get("data", "")
        checks = {
            "document_loaded": page.get("ready_state") in ("interactive", "complete"),
            "has_title": bool(page.get("title", "").strip()),
            "has_body_content": page.get("body_text_length", 0) > 0,
            "no_common_error_page": not page.get("visible_error_markers"),
        }
        return {
            "success": all(checks.values()),
            "url": page.get("url", payload.url),
            "title": page.get("title", ""),
            "ready_state": page.get("ready_state", "unknown"),
            "body_text_excerpt": page.get("body_text", ""),
            "counts": {key: page.get(key, 0) for key in ("links", "buttons", "inputs")},
            "error_markers": page.get("visible_error_markers", []),
            "checks": checks,
            "screenshot": f"data:image/png;base64,{image_data}" if image_data else None,
            "note": "Smoke-check only: this does not replace application-specific assertions or inspect browser console/network errors.",
        }
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Browser verification failed: {str(exc)[:300]}")


@router.post("/launch")
async def launch_browser_daemon(
    url: str = Query("https://google.com"),
    _: str = Depends(require_bearer_token),
) -> dict[str, Any]:
    """Launch CloakBrowser with persistent user data directory and remote debugging enabled."""
    from app.browser.cloak_launcher import launch_cloakbrowser
    try:
        res = await launch_cloakbrowser(open_url=url)
        return res
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to launch CloakBrowser: {exc}",
        )


@router.get("/models")
async def list_configured_web_models(
    _: str = Depends(require_bearer_token),
) -> list[dict[str, Any]]:
    """List all configured web AI models and their live CloakBrowser tab availability."""
    from app.web_models_store import load_web_models
    from app.browser.cloak_launcher import is_cdp_connected
    from app.models.schemas import AvailabilityState
    from urllib.parse import urlparse
    import httpx

    models = load_web_models()
    connected = await is_cdp_connected(timeout=0.6)
    active_tabs = []
    if connected:
        try:
            async with httpx.AsyncClient(timeout=1.0) as client:
                res = await client.get("http://127.0.0.1:9222/json/list")
                active_tabs = res.json()
        except Exception:
            active_tabs = []

    results = []
    for m in models:
        if not connected:
            state = AvailabilityState.MISSING_CLI
            diag = "CloakBrowser is offline. Click 'Launch CloakBrowser' to connect."
        else:
            domain = urlparse(m.url).netloc.lower()
            matching = [t for t in active_tabs if domain in t.get("url", "").lower()]
            if matching:
                state = AvailabilityState.READY
                diag = f"{m.name} tab is active and ready in CloakBrowser."
            else:
                state = AvailabilityState.READY
                diag = f"CloakBrowser connected. Tab will open automatically on demand."

        results.append({
            "id": m.id,
            "name": m.name,
            "url": m.url,
            "input_selector": m.input_selector,
            "send_selector": m.send_selector,
            "response_selector": m.response_selector,
            "description": m.description,
            "is_preset": m.is_preset,
            "state": state.value,
            "diagnostic": diag,
        })
    return results


@router.post("/models")
async def add_or_update_web_model_endpoint(
    payload: dict[str, Any],
    req: Request = None,
    open_now: bool = Query(True),
    _: str = Depends(require_bearer_token),
) -> dict[str, Any]:
    """Add a new web model or update an existing one. Automatically opens in CloakBrowser for login."""
    from app.web_models_store import WebModelConfig, save_web_model
    from app.adapters.web_model import WebBrowserModelAdapter
    try:
        model_id = payload.get("id") or f"web-{payload.get('name', 'custom').lower().replace(' ', '-')}"
        cfg = WebModelConfig(
            id=model_id,
            name=payload.get("name", "Custom Web Model"),
            url=payload.get("url", "https://chatgpt.com"),
            input_selector=payload.get("input_selector") or None,
            send_selector=payload.get("send_selector") or None,
            response_selector=payload.get("response_selector") or None,
            description=payload.get("description") or f"Web AI chat at {payload.get('url')} via CloakBrowser",
            is_preset=payload.get("is_preset", False),
        )
        save_web_model(cfg)

        # Update registry if running inside FastAPI app
        try:
            from app.main import app as main_app
            if hasattr(main_app.state, "agent_registry"):
                main_app.state.agent_registry.register(WebBrowserModelAdapter(cfg))
        except Exception:
            pass

        # If requested, open in CloakBrowser so user can immediately log in
        opened_tab = None
        if open_now:
            try:
                from app.browser.cloak_launcher import is_cdp_connected, launch_cloakbrowser
                if not await is_cdp_connected():
                    await launch_cloakbrowser(open_url=cfg.url)
                else:
                    adapter = WebBrowserModelAdapter(cfg)
                    opened_tab = await adapter._get_or_open_tab()
            except Exception as exc:
                logger.warning(f"Could not automatically open tab: {exc}")

        return {
            "success": True,
            "model": cfg.model_dump(),
            "opened_tab": bool(opened_tab),
            "message": f"Web model '{cfg.name}' registered. Tab opened in CloakBrowser for login.",
        }
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to add web model: {exc}",
        )


@router.delete("/models/{model_id}")
async def delete_web_model_endpoint(
    model_id: str,
    _: str = Depends(require_bearer_token),
) -> dict[str, Any]:
    """Remove a configured web model."""
    from app.web_models_store import delete_web_model
    delete_web_model(model_id)
    try:
        from app.main import app as main_app
        if hasattr(main_app.state, "agent_registry"):
            main_app.state.agent_registry.unregister(model_id)
    except Exception:
        pass
    return {"success": True, "deleted_id": model_id}


@router.post("/models/{model_id}/open")
async def open_model_in_browser(
    model_id: str,
    _: str = Depends(require_bearer_token),
) -> dict[str, Any]:
    """Opens or focuses the tab for this web model in CloakBrowser so the user can log in."""
    from app.web_models_store import load_web_models
    from app.adapters.web_model import WebBrowserModelAdapter
    from app.browser.cloak_launcher import is_cdp_connected, launch_cloakbrowser

    models = {m.id: m for m in load_web_models()}
    if model_id not in models:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Web model not found.")

    target_model = models[model_id]
    if not await is_cdp_connected():
        return await launch_cloakbrowser(open_url=target_model.url)

    adapter = WebBrowserModelAdapter(target_model)
    tab = await adapter._get_or_open_tab()
    return {
        "success": True,
        "model_id": model_id,
        "tab_id": tab.get("id"),
        "url": tab.get("url"),
        "message": f"Opened {target_model.name} in CloakBrowser for authentication.",
    }

