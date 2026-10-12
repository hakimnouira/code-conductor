"""Unit tests for the browser session attachment and CDP router."""
import pytest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient

from app.main import app
from app.browser_session import get_current_endpoint, set_current_endpoint

client = TestClient(app)
AUTH_HEADERS = {"Authorization": "Bearer 5e542c42b89db11fc7a10a2e19fa6e0929fd09d8d5a4c204a5414ead6c42a6be"}


def test_browser_status_unauthorized():
    res = client.get("/browser/status")
    assert res.status_code == 401


def test_browser_configure_endpoint():
    res = client.post(
        "/browser/configure",
        headers=AUTH_HEADERS,
        json={"endpoint": "http://127.0.0.1:9222"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True
    assert "127.0.0.1:9222" in data["endpoint"]
    assert get_current_endpoint() == "http://127.0.0.1:9222"


@patch("app.browser_session._fetch_cdp_json")
def test_browser_status_connected(mock_fetch):
    mock_fetch.side_effect = [
        {"Browser": "Chrome/146.0", "Protocol-Version": "1.3", "User-Agent": "TestAgent"},
        [
            {"id": "tab-1", "title": "Dashboard", "url": "https://example.com", "type": "page"},
            {"id": "tab-2", "title": "Docs", "url": "https://docs.com", "type": "page"},
        ],
    ]
    res = client.get("/browser/status", headers=AUTH_HEADERS)
    assert res.status_code == 200
    data = res.json()
    assert data["connected"] is True
    assert data["browser"] == "Chrome/146.0"
    assert data["tab_count"] == 2


@patch("app.browser_session._fetch_cdp_json")
def test_browser_tabs_listing(mock_fetch):
    mock_fetch.return_value = [
        {"id": "tab-1", "title": "Botkeep", "url": "https://botkeep.cloud", "type": "page", "webSocketDebuggerUrl": "ws://tab1"},
        {"id": "bg-1", "title": "Service Worker", "url": "", "type": "service_worker"},
    ]
    res = client.get("/browser/tabs", headers=AUTH_HEADERS)
    assert res.status_code == 200
    tabs = res.json()
    assert len(tabs) == 1
    assert tabs[0]["title"] == "Botkeep"
    assert tabs[0]["id"] == "tab-1"


@patch("app.browser_session._fetch_cdp_json")
def test_browser_status_offline_graceful(mock_fetch):
    mock_fetch.side_effect = ConnectionRefusedError("Connection refused")
    res = client.get("/browser/status", headers=AUTH_HEADERS)
    assert res.status_code == 200
    data = res.json()
    assert data["connected"] is False
    assert "error" in data
