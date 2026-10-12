import pytest
from httpx import ASGITransport, AsyncClient
from app.core.config import get_settings
from app.main import app

pytestmark = pytest.mark.mocked_integration

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
async def test_agent_routing_and_auto_resolution(monkeypatch):
    from app.adapters.registry import get_agent_registry
    registry = get_agent_registry()

    # Explicit agent
    adapter = await registry.resolve_agent("mock", "Any prompt")
    assert adapter is not None
    assert adapter.agent_id == "mock"

    # Auto routing must not disguise the mock adapter as a real provider when no worker is ready.
    async def no_ready_workers():
        return []
    monkeypatch.setattr(registry, "get_ready_workers", no_ready_workers)
    auto_adapter = await registry.resolve_agent("auto", "Refactor module")
    assert auto_adapter is None

    # Invalid agent returns None
    invalid_adapter = await registry.resolve_agent("non_existent_engine_id_xyz", "prompt")
    assert invalid_adapter is None


@pytest.mark.asyncio
async def test_auto_detect_provider(auth_headers):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Test Groq detection
        res = await client.post(
            "/providers/auto-detect",
            json={"site": "console.groq.com", "is_free": True, "api_key": "gsk_test123"},
            headers=auth_headers,
        )
        assert res.status_code == 200
        data = res.json()
        assert data["success"] is True
        assert data["detected"]["name"] == "Groq Cloud"
        assert data["detected"]["base_url"] == "https://api.groq.com/openai/v1"
        assert data["detected"]["model"] == "qwen/qwen3.8-27b"

        # Verify it now appears in /providers and /agents
        p_res = await client.get("/providers", headers=auth_headers)
        assert p_res.status_code == 200
        p_list = p_res.json()
        assert any(p["id"] == "groq" for p in p_list)

