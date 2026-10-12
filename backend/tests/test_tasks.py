import json
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
async def test_create_task_and_stream(auth_headers):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Create task
        payload = {"prompt": "Hello world from test", "agent_id": "mock"}
        res = await client.post("/tasks", json=payload, headers=auth_headers)
        assert res.status_code == 200
        task_data = res.json()
        task_id = task_data["task_id"]
        assert task_id.startswith("task-")

        # Stream SSE events
        stream_res = await client.get(f"/tasks/{task_id}/events", headers=auth_headers)
        assert stream_res.status_code == 200
        content = stream_res.text
        assert "started" in content
        assert "completed" in content
        assert "[DONE]" in content

@pytest.mark.asyncio
async def test_cancel_task(auth_headers):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Create task
        payload = {"prompt": "Task to be cancelled", "agent_id": "mock"}
        res = await client.post("/tasks", json=payload, headers=auth_headers)
        assert res.status_code == 200
        task_id = res.json()["task_id"]

        # Cancel immediately
        cancel_res = await client.post(f"/tasks/{task_id}/cancel", headers=auth_headers)
        assert cancel_res.status_code == 200
        cancel_data = cancel_res.json()
        assert cancel_data["task_id"] == task_id

@pytest.mark.asyncio
async def test_task_failure_handling(auth_headers):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Non-existent task stream returns 404
        res_events = await client.get("/tasks/non-existent-task-id/events", headers=auth_headers)
        assert res_events.status_code == 404

        # Non-existent task cancel returns 404
        res_cancel = await client.post("/tasks/non-existent-task-id/cancel", headers=auth_headers)
        assert res_cancel.status_code == 404

        # Unresolvable agent returns 404
        res_bad_agent = await client.post(
            "/tasks",
            json={"prompt": "test", "agent_id": "totally_invalid_agent_xyz"},
            headers=auth_headers,
        )
        assert res_bad_agent.status_code == 404

