import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock
from app.web_models_store import (
    WebModelConfig,
    load_web_models,
    save_web_model,
    delete_web_model,
    PRESET_MODELS,
    _default_store_path,
    STORE_PATH,
)
from app.adapters.web_model import WebBrowserModelAdapter
from app.models.schemas import AvailabilityState, TaskCreateRequest, EventType


def test_web_model_store_defaults_to_checkout_and_supports_override(monkeypatch, tmp_path):
    monkeypatch.delenv("CODE_CONDUCTOR_WEB_MODELS_PATH", raising=False)
    expected = Path(__file__).resolve().parents[2] / ".web_models.json"
    assert _default_store_path() == expected

    custom_path = tmp_path / "custom-models.json"
    monkeypatch.setenv("CODE_CONDUCTOR_WEB_MODELS_PATH", str(custom_path))
    assert _default_store_path() == custom_path


def test_presets_exist():
    models = load_web_models()
    ids = [m.id for m in models]
    assert "web-chatgpt" in ids
    assert "web-claude" in ids
    assert "web-deepseek" in ids


def test_save_and_delete_web_model(monkeypatch, tmp_path):
    monkeypatch.setattr("app.web_models_store.STORE_PATH", tmp_path / "web_models.json")
    custom = WebModelConfig(
        id="web-test-custom",
        name="Test Custom Model",
        url="https://example.com/chat",
        description="Testing custom web model",
    )
    saved = save_web_model(custom)
    assert any(m.id == "web-test-custom" for m in saved)

    after_delete = delete_web_model("web-test-custom")
    assert not any(m.id == "web-test-custom" for m in after_delete)


@pytest.mark.asyncio
async def test_web_model_availability_missing_cdp():
    model = PRESET_MODELS[0]
    adapter = WebBrowserModelAdapter(model)

    with patch("app.adapters.web_model.is_cdp_connected", new_callable=AsyncMock) as mock_cdp:
        mock_cdp.return_value = False
        state, diag = await adapter.check_availability()
        assert state == AvailabilityState.MISSING_CLI
        assert "not currently attached" in diag


@pytest.mark.asyncio
async def test_web_model_availability_ready():
    model = PRESET_MODELS[0]
    adapter = WebBrowserModelAdapter(model)

    with patch("app.adapters.web_model._get_cached_cdp_status", new_callable=AsyncMock) as mock_status:
        mock_status.return_value = (True, [{"url": "https://chatgpt.com/c/123", "type": "page"}])
        state, diag = await adapter.check_availability()
        assert state == AvailabilityState.READY
        assert "ChatGPT" in diag


@pytest.mark.asyncio
async def test_openai_compat_list_models():
    from httpx import ASGITransport, AsyncClient
    from app.main import app
    from app.core.config import get_or_create_token

    token = get_or_create_token()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/v1/models", headers={"Authorization": f"Bearer {token}"})
        assert res.status_code == 200
        data = res.json()
        assert "data" in data
        model_ids = [m["id"] for m in data["data"]]
        assert "web-chatgpt" in model_ids
        assert "web-claude" in model_ids


@pytest.mark.asyncio
async def test_web_model_stream_task_events():
    model = PRESET_MODELS[0]
    adapter = WebBrowserModelAdapter(model)
    req = TaskCreateRequest(prompt="Hello", agent_id=model.id)

    with patch("app.adapters.web_model.is_cdp_connected", new_callable=AsyncMock) as mock_cdp, \
         patch.object(adapter, "_get_or_open_tab", new_callable=AsyncMock) as mock_tab:
        mock_cdp.return_value = False
        # When CloakBrowser fails to launch in unit test, it yields STARTED and ERROR gracefully
        with patch("app.adapters.web_model.launch_cloakbrowser", side_effect=RuntimeError("No browser")):
            events = []
            async for ev in adapter.stream_task("task-123", req):
                events.append(ev)
            
            assert len(events) >= 2
            assert events[0].event_type == EventType.STARTED
            assert events[-1].event_type == EventType.ERROR
            assert "task-123" == events[0].task_id

