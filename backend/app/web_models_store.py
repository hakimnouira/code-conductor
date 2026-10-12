"""Web Models storage and configuration.

Persists user-added and preset Web AI models that are operated via CloakBrowser.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

STORE_PATH = Path(r"D:\work\code-conductor\.web_models.json")


class WebModelConfig(BaseModel):
    id: str = Field(..., description="Unique model ID, e.g. web-chatgpt, web-claude")
    name: str = Field(..., description="Display name, e.g. ChatGPT Web (CloakBrowser)")
    url: str = Field(..., description="Web AI link to open and login")
    input_selector: str | None = Field(default=None, description="CSS selector for prompt input box")
    send_selector: str | None = Field(default=None, description="CSS selector for send/submit button")
    response_selector: str | None = Field(default=None, description="CSS selector for assistant reply container")
    description: str = Field(default="Web AI model via authenticated CloakBrowser session")
    is_preset: bool = Field(default=False)
    created_at: float = Field(default_factory=time.time)


PRESET_MODELS: list[WebModelConfig] = [
    WebModelConfig(
        id="web-chatgpt",
        name="ChatGPT Web",
        url="https://chatgpt.com",
        input_selector="div.ProseMirror, #prompt-textarea, textarea, div[contenteditable='true']",
        send_selector="button[data-testid='send-button'], button[aria-label*='Send']",
        response_selector="h4[data-conversation-role='assistant'], div[data-message-author-role='assistant'], div.markdown",
        description="ChatGPT web chat using your logged-in ChatGPT Plus/Free account via CloakBrowser",
        is_preset=True,
    ),
    WebModelConfig(
        id="web-claude",
        name="Claude Web",
        url="https://claude.ai/new",
        input_selector="div.ProseMirror[contenteditable='true'], div[contenteditable='true'], textarea",
        send_selector="button[data-testid='chat-input-send'], button[aria-label*='Send']",
        response_selector=".font-claude-message, div.prose",
        description="Claude web chat using your logged-in Anthropic account via CloakBrowser",
        is_preset=True,
    ),
    WebModelConfig(
        id="web-deepseek",
        name="DeepSeek Web",
        url="https://chat.deepseek.com",
        input_selector="textarea#chat-input, textarea",
        send_selector="div[role='button']:has(svg), button[aria-label*='Send']",
        response_selector=".ds-markdown, .chat-message",
        description="DeepSeek chat using your logged-in DeepSeek account via CloakBrowser",
        is_preset=True,
    ),
    WebModelConfig(
        id="web-arena",
        name="LMSYS Chatbot Arena",
        url="https://lmarena.ai",
        input_selector="textarea, input[type='text']",
        send_selector="button",
        response_selector=".message-content, .chatbot-message",
        description="LMSYS Chatbot Arena direct web testing via CloakBrowser",
        is_preset=True,
    ),
    WebModelConfig(
        id="web-manus",
        name="Manus Web",
        url="https://manus.im",
        input_selector="textarea, input[type='text'], div[contenteditable='true']",
        send_selector="button",
        response_selector=".message, .response",
        description="Manus Web AI workspace via CloakBrowser",
        is_preset=True,
    ),
]


def load_web_models() -> list[WebModelConfig]:
    """Loads all models, ensuring presets exist."""
    models_dict = {m.id: m for m in PRESET_MODELS}
    if STORE_PATH.exists():
        try:
            raw = json.loads(STORE_PATH.read_text(encoding="utf-8"))
            for item in raw:
                try:
                    cfg = WebModelConfig(**item)
                    models_dict[cfg.id] = cfg
                except Exception:
                    pass
        except Exception as exc:
            logger.warning(f"Failed to read web models store: {exc}")

    return list(models_dict.values())


def save_web_model(model: WebModelConfig) -> list[WebModelConfig]:
    """Adds or updates a web model."""
    models = {m.id: m for m in load_web_models()}
    models[model.id] = model
    _persist(list(models.values()))
    return list(models.values())


def delete_web_model(model_id: str) -> list[WebModelConfig]:
    """Deletes a custom web model."""
    models = {m.id: m for m in load_web_models()}
    if model_id in models:
        del models[model_id]
        _persist(list(models.values()))
    return list(models.values())


def _persist(models: list[WebModelConfig]) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STORE_PATH.write_text(
        json.dumps([m.model_dump() for m in models], indent=2),
        encoding="utf-8",
    )
