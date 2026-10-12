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
async def test_list_antigravity_history(auth_headers):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/antigravity/history", headers=auth_headers)
        assert res.status_code == 200
        data = res.json()
        assert isinstance(data, list)
        if len(data) > 0:
            first = data[0]
            assert "id" in first
            assert "title" in first
            assert "message_count" in first


@pytest.mark.asyncio
async def test_get_antigravity_conversation_detail(auth_headers):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # First list to get an existing id
        list_res = await client.get("/antigravity/history", headers=auth_headers)
        assert list_res.status_code == 200
        convs = list_res.json()
        if not convs:
            pytest.skip("No conversations found in environment")

        conv_id = convs[0]["id"]
        res = await client.get(f"/antigravity/history/{conv_id}", headers=auth_headers)
        assert res.status_code == 200
        detail = res.json()
        assert detail["id"] == conv_id
        assert "messages" in detail
        assert isinstance(detail["messages"], list)
        assert "artifacts" in detail


@pytest.mark.asyncio
async def test_get_invalid_conversation_id(auth_headers):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/antigravity/history/invalid..id!@#", headers=auth_headers)
        assert res.status_code == 400
