import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app


pytestmark = pytest.mark.unit


def test_browser_verify_rejects_non_http_urls():
    token = get_settings().auth_token
    with TestClient(app) as client:
        response = client.post(
            "/browser/verify",
            json={"url": "javascript:alert(1)"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 400
    assert "HTTP(S)" in response.json()["detail"]


def test_browser_verify_rejects_embedded_credentials():
    token = get_settings().auth_token
    with TestClient(app) as client:
        response = client.post(
            "/browser/verify",
            json={"url": "https://user:password@example.com"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 400
