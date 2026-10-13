import asyncio
import json
import time
import pytest
from httpx import ASGITransport, AsyncClient
from app.core.config import get_settings
from app.main import TaskRecord, _browser_verification_gate, _verify_browser_for_task, _tasks, app
from app.models.schemas import TaskCreateRequest, TaskStatus

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


@pytest.mark.asyncio
async def test_task_status_endpoint_reports_terminal_state(auth_headers):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/tasks",
            json={"prompt": "Check status endpoint", "agent_id": "mock", "github_research": False},
            headers=auth_headers,
        )
        assert res.status_code == 200
        task_id = res.json()["task_id"]

        # Consume the stream so the mock adapter finishes before checking state.
        stream = await client.get(f"/tasks/{task_id}/events", headers=auth_headers)
        assert "[DONE]" in stream.text

        status_res = await client.get(f"/tasks/{task_id}", headers=auth_headers)
        assert status_res.status_code == 200
        payload = status_res.json()
        assert payload["task_id"] == task_id
        assert payload["status"] == "completed"
        assert payload["started_at"] is not None
        assert payload["completed_at"] is not None
        assert payload["error"] is None


@pytest.mark.asyncio
async def test_task_status_endpoint_rejects_unknown_task(auth_headers):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/tasks/missing-task", headers=auth_headers)
        assert response.status_code == 404



@pytest.mark.asyncio
async def test_task_events_include_replayable_ids_and_can_resume(auth_headers):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(
            "/tasks",
            json={"prompt": "Replay task events", "agent_id": "mock", "github_research": False},
            headers=auth_headers,
        )
        assert created.status_code == 200
        task_id = created.json()["task_id"]

        stream = await client.get(f"/tasks/{task_id}/events", headers=auth_headers)
        assert stream.status_code == 200
        assert "id: " in stream.text
        ids = [
            int(line[4:])
            for line in stream.text.splitlines()
            if line.startswith("id: ") and line[4:].isdigit()
        ]
        assert ids
        last_id = max(ids)

        resumed = await client.get(
            f"/tasks/{task_id}/events?after={last_id}",
            headers=auth_headers,
        )
        assert resumed.status_code == 200
        assert "[DONE]" in resumed.text
        assert "Replay task events" not in resumed.text


@pytest.mark.asyncio
async def test_task_list_includes_completed_task(auth_headers):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(
            "/tasks",
            json={"prompt": "Task history listing", "agent_id": "mock", "github_research": False},
            headers=auth_headers,
        )
        assert created.status_code == 200
        task_id = created.json()["task_id"]

        # Wait for the mock task to finish before asserting its durable status.
        for _ in range(30):
            status_response = await client.get(f"/tasks/{task_id}", headers=auth_headers)
            if status_response.json()["status"] in {"completed", "failed", "cancelled"}:
                break
            await asyncio.sleep(0.05)

        listing = await client.get("/tasks?limit=20", headers=auth_headers)
        assert listing.status_code == 200
        found = next(item for item in listing.json() if item["task_id"] == task_id)
        assert found["status"] == "completed"



@pytest.mark.asyncio
async def test_browser_verification_saves_screenshot_inside_workspace(tmp_path, monkeypatch):
    import base64

    async def fake_verify(url, wait_seconds=2, **assertions):
        assert url == "http://localhost:3000"
        assert wait_seconds == 2
        assert assertions["required_text"] == []
        return {
            "success": True,
            "url": url,
            "title": "Test app",
            "checks": {"document_loaded": True, "has_title": True},
            "error_markers": [],
            "body_text_excerpt": "hello",
            "screenshot": "data:image/png;base64," + base64.b64encode(b"test-png").decode("ascii"),
        }

    monkeypatch.setattr("app.main.run_browser_automation", fake_verify)
    record = TaskRecord(
        "task-browser-artifact-test",
        TaskCreateRequest(
            prompt="verify local app",
            workspace_path=str(tmp_path),
            browser_verify_url="http://localhost:3000",
            github_research=False,
        ),
        "mock",
    )
    success, diagnostic = await _verify_browser_for_task(record)
    artifact = tmp_path / ".code-conductor" / "artifacts" / record.task_id / "browser-verification.png"
    assert success is True
    assert artifact.read_bytes() == b"test-png"
    assert "browser-verification.png" in diagnostic


@pytest.mark.asyncio
async def test_browser_verification_gate_retries_then_fails(monkeypatch, tmp_path):
    async def fake_check(record):
        return False, "visible error marker: Application error"

    monkeypatch.setattr("app.main._verify_browser_for_task", fake_check)
    record = TaskRecord(
        "task-browser-retry-test",
        TaskCreateRequest(
            prompt="fix local app",
            workspace_path=str(tmp_path),
            browser_verify_url="http://localhost:3000",
            verification_max_attempts=2,
            github_research=False,
        ),
        "mock",
    )
    action, attempts = await _browser_verification_gate(record, 0)
    assert action == "retry"
    assert attempts == 1
    assert "BROWSER VERIFICATION FAILED" in record.request.prompt

    action, attempts = await _browser_verification_gate(record, attempts)
    assert action == "failed"
    assert attempts == 2
    assert record.status.value == "failed"



@pytest.mark.asyncio
async def test_browser_screenshot_artifact_endpoint_is_authenticated_and_workspace_scoped(auth_headers, tmp_path):
    from pathlib import Path

    task_id = "task-artifact-route-test"
    record = TaskRecord(
        task_id,
        TaskCreateRequest(prompt="artifact route test", workspace_path=str(tmp_path), github_research=False),
        "mock",
    )
    _tasks[task_id] = record
    artifact = Path(tmp_path) / ".code-conductor" / "artifacts" / task_id / "browser-verification.png"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(b"png-fixture")
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                f"/tasks/{task_id}/artifacts/browser-verification.png",
                headers=auth_headers,
            )
            assert response.status_code == 200
            assert response.content == b"png-fixture"
            assert response.headers["content-type"].startswith("image/png")
    finally:
        _tasks.pop(task_id, None)



@pytest.mark.asyncio
async def test_persisted_event_history_restores_completed_task_details(auth_headers):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(
            "/tasks",
            json={"prompt": "Persisted event history", "agent_id": "mock", "github_research": False},
            headers=auth_headers,
        )
        assert created.status_code == 200
        task_id = created.json()["task_id"]

        for _ in range(100):
            status_response = await client.get(f"/tasks/{task_id}", headers=auth_headers)
            if status_response.json()["status"] in {"completed", "failed", "cancelled"}:
                break
            await asyncio.sleep(0.05)

        history = await client.get(f"/tasks/{task_id}/event-history", headers=auth_headers)
        assert history.status_code == 200
        body = history.json()
        assert body["task_id"] == task_id
        assert body["events"]
        ids = [event["event_id"] for event in body["events"]]
        assert ids == sorted(ids)
        assert any(event["event_type"] == "completed" for event in body["events"])



@pytest.mark.asyncio
async def test_task_creation_is_idempotent_and_rejects_key_reuse_with_different_request(auth_headers):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        payload = {
            "prompt": "Idempotent scheduled task",
            "agent_id": "mock",
            "github_research": False,
            "idempotency_key": "test-idempotency-key-2026-10",
        }
        first, replay = await asyncio.gather(
            client.post("/tasks", json=payload, headers=auth_headers),
            client.post("/tasks", json=payload, headers=auth_headers),
        )
        assert first.status_code == 200
        assert replay.status_code == 200
        task_id = first.json()["task_id"]
        assert replay.json()["task_id"] == task_id
        assert replay.json()["message"] == "Returning the existing task for this idempotency key."

        changed = {**payload, "prompt": "Different task with same key"}
        conflict = await client.post("/tasks", json=changed, headers=auth_headers)
        assert conflict.status_code == 409



@pytest.mark.asyncio
async def test_retry_endpoint_uses_original_request_and_tracks_lineage(auth_headers, tmp_path, monkeypatch):
    monkeypatch.setenv("CODE_CONDUCTOR_TASK_DB", str(tmp_path / "retry-endpoint.sqlite3"))
    workspace = tmp_path / "retry-workspace"
    workspace.mkdir()
    original_prompt = "Repair the failing unit test without changing its assertions."
    request = TaskCreateRequest(
        prompt=original_prompt,
        agent_id="mock",
        workspace_path=str(workspace),
        github_research=False,
    )
    failed = TaskRecord("task-retry-source", request, "mock")
    failed.request.prompt += "\n\nAUTOMATED VERIFICATION FAILED: transient diagnostic text."
    failed.status = TaskStatus.FAILED
    failed.error = "Simulated prior failure."
    failed.completed_at = time.time()
    failed._persist()
    _tasks[failed.task_id] = failed

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/tasks/{failed.task_id}/retry", headers=auth_headers)

    assert response.status_code == 200
    new_task_id = response.json()["task_id"]
    assert new_task_id != failed.task_id
    retried = _tasks[new_task_id]
    assert retried.request.prompt == original_prompt
    assert retried.request.retry_of == failed.task_id
    assert retried.request.idempotency_key.startswith(f"retry:{failed.task_id}:")


@pytest.mark.asyncio
async def test_retry_endpoint_rejects_active_task(auth_headers):
    active = TaskRecord(
        "task-retry-active",
        TaskCreateRequest(prompt="Still running", agent_id="mock", github_research=False),
        "mock",
    )
    active.status = TaskStatus.RUNNING
    active.started_at = time.time()
    _tasks[active.task_id] = active
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/tasks/{active.task_id}/retry", headers=auth_headers)
    assert response.status_code == 409



@pytest.mark.asyncio
async def test_retry_endpoint_rejects_legacy_task_without_original_request(auth_headers):
    legacy = TaskRecord(
        "task-legacy-retry",
        TaskCreateRequest(prompt="Mutable legacy request", agent_id="mock", github_research=False),
        "mock",
        restored={
            "status": "failed",
            "created_at": time.time(),
            "completed_at": time.time(),
            "error": "Backend restarted",
            "evidence": [],
        },
        initial_request_json="",
    )
    _tasks[legacy.task_id] = legacy
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/tasks/{legacy.task_id}/retry", headers=auth_headers)
    assert response.status_code == 409
    assert "original request was not preserved" in response.json()["detail"].lower()
