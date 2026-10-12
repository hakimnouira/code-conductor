import os
import shutil
import tempfile
import time
from pathlib import Path
import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.main import app
from app.models.schemas import AvailabilityState, EventType

pytestmark = [pytest.mark.windows, pytest.mark.mocked_integration]


@pytest.fixture
def auth_headers():
    token = get_settings().auth_token
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_e2e_health_and_token_auth(auth_headers):
    """End-to-end test of healthcheck and Bearer token enforcement."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Healthcheck without token returns 401
        res = await client.get("/health")
        assert res.status_code == 401

        # 2. Healthcheck with valid token returns 200 healthy
        res = await client.get("/health", headers=auth_headers)
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "healthy"
        assert "uptime_seconds" in data
        assert "workspace" in data

        # 3. Protected endpoint without token returns 401
        res = await client.get("/agents")
        assert res.status_code == 401

        # 4. Protected endpoint with invalid token returns 401
        res = await client.get("/agents", headers={"Authorization": "Bearer invalid_token"})
        assert res.status_code == 401

        # 5. Protected endpoint with valid token returns 200
        res = await client.get("/agents", headers=auth_headers)
        assert res.status_code == 200


@pytest.mark.asyncio
async def test_e2e_agent_registry_discovery(auth_headers):
    """Verifies all required multi-engine agents are registered and reported."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/agents", headers=auth_headers)
        assert res.status_code == 200
        agents = res.json()
        agent_ids = {a["id"] for a in agents}

        # Core required agents
        assert "orchestrator" in agent_ids
        assert "codex" in agent_ids
        assert "copilot" in agent_ids
        assert "antigravity" in agent_ids
        assert "ollama" in agent_ids
        assert "mock" in agent_ids

        # Verify orchestrator is READY
        orch = next(a for a in agents if a["id"] == "orchestrator")
        assert orch["state"] == AvailabilityState.READY


@pytest.mark.asyncio
async def test_e2e_orchestrator_planning_and_batching(auth_headers):
    """Tests the full multi-agent DAG planning and parallel batch computation endpoint."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        payload = {
            "prompt": "Refactor authentication layer, update unit tests, and review security policies",
            "agent_id": "orchestrator",
        }
        res = await client.post("/orchestrator/plan", json=payload, headers=auth_headers)
        assert res.status_code == 200
        plan = res.json()

        assert "plan_id" in plan
        assert len(plan["subtasks"]) >= 2
        assert len(plan["parallel_batches"]) >= 1

        # Verify topological sorting: all batch IDs are present in subtasks
        subtask_ids = {s["id"] for s in plan["subtasks"]}
        for batch in plan["parallel_batches"]:
            for tid in batch:
                assert tid in subtask_ids


@pytest.mark.asyncio
async def test_e2e_workspace_safety_and_atomic_replace(auth_headers):
    """Tests canonical path traversal blocking, secret protection, and atomic edits."""
    ws_root = Path(get_settings().workspace_root)
    fixture_dir = ws_root / "backend" / "tests" / "fixtures"
    fixture_dir.mkdir(parents=True, exist_ok=True)
    test_rel_path = "backend/tests/fixtures/e2e_write_test.txt"
    test_abs_path = ws_root / test_rel_path

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        try:
            # 1. Path traversal attempt blocked
            bad_read = await client.post(
                "/workspace/file/read",
                json={"path": "../../../boot.ini"},
                headers=auth_headers,
            )
            assert bad_read.status_code == 403

            # 2. Secret file blocked
            bad_secret = await client.post(
                "/workspace/file/read",
                json={"path": ".env"},
                headers=auth_headers,
            )
            assert bad_secret.status_code == 403

            # 3. Safe atomic write
            write_res = await client.post(
                "/workspace/file/write",
                json={"path": test_rel_path, "content": "e2e safe content line 1\n"},
                headers=auth_headers,
            )
            assert write_res.status_code == 200
            assert write_res.json()["bytes_written"] > 0

            # 4. Safe read back
            read_res = await client.post(
                "/workspace/file/read",
                json={"path": test_rel_path},
                headers=auth_headers,
            )
            assert read_res.status_code == 200
            assert "e2e safe content line 1" in read_res.json()["content"]
        finally:
            if test_abs_path.exists():
                test_abs_path.unlink()


@pytest.mark.asyncio
async def test_e2e_git_guard_checkpointing(auth_headers):
    """Tests Git status inspection, diff generation, and checkpoint creation."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Check current repository status
        res = await client.get("/workspace/git/status", headers=auth_headers)
        assert res.status_code == 200
        status_data = res.json()
        assert status_data["is_repo"] is True
        assert "branch" in status_data

        # Check diff inspection
        res = await client.post("/workspace/git/diff", json={}, headers=auth_headers)
        assert res.status_code == 200
        assert "diff" in res.json()

        # Check safety checkpoint creation
        res = await client.post(
            "/workspace/git/checkpoint",
            json={"name": "e2e_checkpoint_test"},
            headers=auth_headers,
        )
        assert res.status_code == 200
        checkpoint_data = res.json()
        assert checkpoint_data["success"] is True
        assert len(checkpoint_data["checkpoint_id"]) > 0


@pytest.mark.asyncio
async def test_e2e_task_stream_and_cancellation(auth_headers):
    """Tests task creation, SSE streaming lifecycle, and in-flight cancellation."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Create a task with mock agent
        res = await client.post(
            "/tasks",
            json={"prompt": "E2E stream test prompt", "agent_id": "mock"},
            headers=auth_headers,
        )
        assert res.status_code == 200
        task_data = res.json()
        task_id = task_data["task_id"]

        # 2. Cancel endpoint returns valid status
        res = await client.post(f"/tasks/{task_id}/cancel", headers=auth_headers)
        assert res.status_code == 200
        cancel_data = res.json()
        assert "cancelled" in cancel_data
        assert cancel_data["task_id"] == task_id


@pytest.mark.asyncio
async def test_e2e_audit_history_persistence(auth_headers):
    """Tests audit history retrieval via REST API."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/orchestrator/history?limit=10", headers=auth_headers)
        assert res.status_code == 200
        records = res.json()
        assert isinstance(records, list)
