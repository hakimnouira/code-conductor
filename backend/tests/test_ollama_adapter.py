import asyncio
import json
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route
from app.adapters.ollama import OllamaAdapter
from app.models.schemas import AvailabilityState, EventType, TaskCreateRequest

pytestmark = pytest.mark.mocked_integration

@pytest.mark.asyncio
async def test_ollama_metadata():
    adapter = OllamaAdapter()
    assert adapter.agent_id == "ollama"
    assert "Ollama" in adapter.name
    caps = await adapter.get_capabilities()
    assert caps.supports_streaming is True
    assert caps.supports_code_edits is True
    assert len(caps.supported_models) >= 1

@pytest.mark.asyncio
async def test_ollama_connectivity_offline_diagnostic():
    # Point to an unused high port to simulate daemon being offline
    adapter = OllamaAdapter(
        base_url="http://127.0.0.1:59999",
        lm_studio_url="http://127.0.0.1:59998/v1",
    )
    state, diag = await adapter.check_availability()
    assert state == AvailabilityState.MISSING_CLI
    assert "ollama serve" in diag or "ollama.com" in diag

@pytest.mark.asyncio
async def test_ollama_stream_when_offline():
    adapter = OllamaAdapter(
        base_url="http://127.0.0.1:59999",
        lm_studio_url="http://127.0.0.1:59998/v1",
    )
    req = TaskCreateRequest(prompt="Write python code", agent_id="ollama")
    events = []
    async for event in adapter.stream_task("offline-task-1", req):
        events.append(event)

    assert len(events) >= 1
    assert events[0].event_type == EventType.ERROR
    assert "unavailable" in events[0].text.lower()

@pytest.mark.asyncio
async def test_ollama_mock_server_flow():
    async def handle_tags(request):
        return JSONResponse({"models": [{"name": "qwen2.5-coder:7b"}]})

    async def handle_chat(request):
        async def generate_chunks():
            tokens = ["def ", "fibonacci(n):", "\n    ", "return ", "n"]
            for t in tokens:
                chunk = json.dumps({"message": {"content": t}, "done": False}) + "\n"
                yield chunk.encode("utf-8")
                await asyncio.sleep(0.01)

            done_chunk = json.dumps({"message": {"content": ""}, "done": True}) + "\n"
            yield done_chunk.encode("utf-8")

        return StreamingResponse(generate_chunks(), media_type="application/x-ndjson")

    routes = [
        Route("/api/tags", endpoint=handle_tags, methods=["GET"]),
        Route("/api/chat", endpoint=handle_chat, methods=["POST"]),
    ]
    starlette_app = Starlette(routes=routes)
    config = uvicorn.Config(starlette_app, host="127.0.0.1", port=21434, log_level="warning")
    server = uvicorn.Server(config)

    server_task = asyncio.create_task(server.serve())
    # Give server a moment to bind
    for _ in range(20):
        if server.started:
            break
        await asyncio.sleep(0.05)

    try:
        adapter = OllamaAdapter(base_url="http://127.0.0.1:21434")
        state, diag = await adapter.check_availability()
        assert state == AvailabilityState.READY
        assert "qwen2.5-coder" in diag

        req = TaskCreateRequest(prompt="Implement fibonacci", agent_id="ollama")
        events = []
        async for event in adapter.stream_task("task-local-1", req):
            events.append(event)

        assert any(e.event_type == EventType.STARTED for e in events)
        token_texts = "".join(e.text for e in events if e.event_type == EventType.TOKEN)
        assert "fibonacci" in token_texts
        assert any(e.event_type == EventType.COMPLETED for e in events)
    finally:
        server.should_exit = True
        await server_task
