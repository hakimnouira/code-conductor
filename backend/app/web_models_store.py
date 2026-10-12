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

def _default_store_path() -> Path:
    """Resolve the web-model store relative to this checkout, with an optional override."""
    override = os.environ.get("CODE_CONDUCTOR_WEB_MODELS_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parents[2] / ".web_models.json"


STORE_PATH = _default_store_path()


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
        input_selector="div.ProseMirror, [contenteditable='true'], textarea, #prompt-textarea",
        send_selector="button[data-testid='send-button'], button[aria-label*='Send' i]",
        response_selector="h4[data-conversation-role='assistant'], div[data-message-author-role='assistant'], div.markdown",
        description="ChatGPT web chat using your logged-in ChatGPT Plus/Free account via CloakBrowser",
        is_preset=True,
    ),
    WebModelConfig(
        id="web-claude",
        name="Claude Web",
        url="https://claude.ai/new",
        input_selector="div.ProseMirror[contenteditable='true'], [contenteditable='true'], textarea",
        send_selector="button[aria-label*='Send' i], button[data-testid='chat-input-send']",
        response_selector=".font-claude-message, div.prose",
        description="Claude web chat using your logged-in Anthropic account via CloakBrowser",
        is_preset=True,
    ),
    WebModelConfig(
        id="web-deepseek",
        name="DeepSeek Web",
        url="https://chat.deepseek.com",
        input_selector="textarea[placeholder*='DeepSeek' i], textarea.ds-scroll-area, textarea",
        send_selector="div.ds-button--circle:not(.ds-button--disabled), div[role='button'].ds-button--primary:not(.ds-button--disabled), div.ds-button--circle",
        response_selector=".ds-markdown, div.ds-markdown, div.markdown",
        description="DeepSeek chat using your logged-in DeepSeek account via CloakBrowser",
        is_preset=True,
    ),
    WebModelConfig(
        id="web-manus",
        name="Manus Web",
        url="https://manus.im/app",
        input_selector="div.tiptap.ProseMirror, div.ProseMirror[contenteditable='true'], [contenteditable='true'], textarea",
        send_selector="button[class*='accent-send-button'], button.bg-\\[var\\(--accent-send-button\\)\\]",
        response_selector="[class*='u-break-words'], [class*='whitespace-pre-wrap'], div.prose, [class*='message']",
        description="Manus Web AI workspace via CloakBrowser",
        is_preset=True,
    ),
    WebModelConfig(
        id="web-muse",
        name="Muse AI Web",
        url="https://muse.ai/",
        input_selector="textarea[placeholder='Message'], textarea, [contenteditable='true']",
        send_selector="button[aria-label*='Send' i], button:has(svg)",
        response_selector=".hatch-chat-groupable-bubble, .prose, [class*='hatch-chat-']",
        description="Muse AI chat using your logged-in Muse account via CloakBrowser",
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
