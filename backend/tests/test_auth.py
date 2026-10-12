import pytest
from httpx import ASGITransport, AsyncClient
from app.core.config import get_settings
from app.main import app

pytestmark = pytest.mark.unit

@pytest.mark.asyncio
async def test_auth_missing_header():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/health")
        assert res.status_code == 401
        assert "Missing Authorization header" in res.json()["detail"]

@pytest.mark.asyncio
async def test_auth_invalid_token():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/health", headers={"Authorization": "Bearer wrong_token_123"})
        assert res.status_code == 401
        assert "Invalid or expired bearer token" in res.json()["detail"]

@pytest.mark.asyncio
async def test_auth_valid_token():
    token = get_settings().auth_token
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/health", headers={"Authorization": f"Bearer {token}"})
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "healthy"
        assert data["version"] == get_settings().version
