"""Browser session attachment and automation bridge.

Enables Code Conductor to attach directly to an active browser session
via Chrome DevTools Protocol (CDP), discover active tabs, switch targets,
navigate, evaluate scripts, and capture screenshots.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import urllib.parse
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.core.security import require_bearer_token

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
