import pytest
from httpx import ASGITransport, AsyncClient
from app.core.config import get_settings
from app.main import app

@pytest.fixture
def auth_headers():
    token = get_settings().auth_token
    return {"Authorization": f"Bearer {token}"}

@pytest.mark.asyncio
async def test_health_check(auth_headers):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/health", headers=auth_headers)
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "healthy"
        assert "uptime_seconds" in data
        assert "workspace" in data

@pytest.mark.asyncio
async def test_agent_discovery(auth_headers):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/agents", headers=auth_headers)
        assert res.status_code == 200
        agents = res.json()
        assert len(agents) >= 1
        mock_agent = next((a for a in agents if a["id"] == "mock"), None)
        assert mock_agent is not None
        assert mock_agent["state"] == "ready"
        assert mock_agent["capabilities"]["supports_streaming"] is True
