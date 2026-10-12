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

@pytest.mark.asyncio
async def test_agent_routing_and_auto_resolution():
    from app.adapters.registry import get_agent_registry
    registry = get_agent_registry()

    # Explicit agent
    adapter = await registry.resolve_agent("mock", "Any prompt")
    assert adapter is not None
    assert adapter.agent_id == "mock"

    # Auto resolution falls back to available ready engine (e.g. mock or codex if ready)
    auto_adapter = await registry.resolve_agent("auto", "Refactor module")
    assert auto_adapter is not None

    # Invalid agent returns None
    invalid_adapter = await registry.resolve_agent("non_existent_engine_id_xyz", "prompt")
    assert invalid_adapter is None

