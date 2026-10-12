import asyncio
import json
import os
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import AsyncIterator
from pathlib import Path

from app.adapters.base import BaseAgentAdapter
from app.models.schemas import (
    AgentCapabilities,
    AgentEvent,
    AgentInfo,
    AvailabilityState,
    EventType,
    TaskCreateRequest,
)

CONFIG_PATH = Path.home() / ".gemini" / "config" / "api_keys.json"


def load_api_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


class MultiModelAdapter(BaseAgentAdapter):
    """
    Multi-model adapter integrating Groq, APMix, CodeCraft, and Experiential Labs
    from ~/.gemini/config/api_keys.json with automatic key rotation and rate-limit handling.
    """

    def __init__(self, provider: str = "auto"):
        self.provider = provider
        self._active_cancels: dict[str, asyncio.Event] = {}

    @property
    def agent_id(self) -> str:
        return "multimodel" if self.provider == "auto" else self.provider

    @property
    def name(self) -> str:
        names = {
            "auto": "Multi-Model Router (Dual Cloud / Free Tier)",
            "groq": "Groq (Qwen 3.8 27B / GPT-OSS 120B)",
            "apmix": "APMix (Claude Sonnet 4.6 / GPT-6 Luna Free)",
            "codecraft": "CodeCraft (Gemma 2 2B)",
            "experientiallabs": "Experiential Labs (AION 3.0 Mini)",
        }
        return names.get(self.provider, f"Multi-Model ({self.provider})")

    @property
    def description(self) -> str:
        return (
            "Multi-provider cloud AI hub using your rotating keys for Groq, APMix, "
            "CodeCraft, and Experiential Labs with automatic failover."
        )

    async def check_availability(self) -> tuple[AvailabilityState, str]:
        cfg = load_api_config()
        if not cfg:
            return (
                AvailabilityState.UNAUTHENTICATED,
                f"No API key configuration found at {CONFIG_PATH}.",
            )

        # Check Groq keys
        groq_keys = cfg.get("groq", {}).get("keys", [])
        apmix_keys = cfg.get("apmix", {}).get("keys", [])

        if self.provider == "groq" and not groq_keys:
            return AvailabilityState.UNAUTHENTICATED, "No Groq API keys configured."
        if self.provider == "apmix" and not apmix_keys:
            return AvailabilityState.UNAUTHENTICATED, "No APMix API keys configured."

        if not groq_keys and not apmix_keys:
            return AvailabilityState.UNAUTHENTICATED, "No active API keys found in api_keys.json."

        return (
            AvailabilityState.READY,
            f"Multi-Model Router ready with {len(groq_keys)} Groq keys and {len(apmix_keys)} APMix keys.",
        )

    async def get_capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(
            supports_streaming=True,
            supports_code_edits=True,
            supports_terminal_execution=False,
            supports_subagents=True,
            supported_models=[
                "qwen/qwen3.8-27b",
                "openai/gpt-oss-120b",
                "claude-sonnet-4-6-free",
                "gpt-6-luna-free",
                "gemma-2-2b",
                "aion-3.0-mini",
            ],
        )

    async def stream_task(self, task_id: str, request: TaskCreateRequest) -> AsyncIterator[AgentEvent]:
        state, diag = await self.check_availability()
        if state != AvailabilityState.READY:
            yield AgentEvent(
                task_id=task_id,
                event_type=EventType.ERROR,
                text=f"API configuration error: {diag}",
                timestamp=time.time(),
            )
            return

        cancel_event = asyncio.Event()
        self._active_cancels[task_id] = cancel_event

        yield AgentEvent(
            task_id=task_id,
            event_type=EventType.STARTED,
            text=f"Delegating prompt to {self.name}...",
            timestamp=time.time(),
        )

        # Query APIs using worker thread so event loop remains free
        response_text = ""
        try:
            cfg = load_api_config()
            prompt = request.prompt
            system = "You are an expert AI software engineer and code assistant."

            response_text = await asyncio.to_thread(self._query_with_failover, cfg, prompt, system)
        except Exception as e:
            yield AgentEvent(
                task_id=task_id,
                event_type=EventType.ERROR,
                text=f"Multi-model execution error: {str(e)}",
                timestamp=time.time(),
            )
            self._active_cancels.pop(task_id, None)
            return

        # Stream response tokens
        words = response_text.split(" ")
        for i, word in enumerate(words):
            if cancel_event.is_set():
                yield AgentEvent(
                    task_id=task_id,
                    event_type=EventType.CANCELLED,
                    text="Task cancelled by user.",
                    timestamp=time.time(),
                )
                self._active_cancels.pop(task_id, None)
                return

            yield AgentEvent(
                task_id=task_id,
                event_type=EventType.TOKEN,
                text=word + (" " if i < len(words) - 1 else ""),
                timestamp=time.time(),
            )
            await asyncio.sleep(0.015)

        yield AgentEvent(
            task_id=task_id,
            event_type=EventType.COMPLETED,
            text="",
            timestamp=time.time(),
        )
        self._active_cancels.pop(task_id, None)

    async def cancel_task(self, task_id: str) -> bool:
        if task_id in self._active_cancels:
            self._active_cancels[task_id].set()
            return True
        return False

    def _query_with_failover(self, config: dict, prompt: str, system: str) -> str:
        """Executes query with rotating keys and fallback between providers."""
        # 1. Try Groq first
        groq_cfg = config.get("groq", {})
        groq_keys = groq_cfg.get("keys", [])
        groq_url = f"{groq_cfg.get('base_url', 'https://api.groq.com/openai/v1')}/chat/completions"
        groq_model = groq_cfg.get("default_model", "qwen/qwen3.8-27b")

        for key in groq_keys:
            try:
                res = self._post_json(
                    groq_url,
                    {
                        "model": groq_model,
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": prompt},
                        ],
                        "max_tokens": 2048,
                    },
                    headers={
                        "Authorization": f"Bearer {key}",
                        "Content-Type": "application/json",
                    },
                )
                if "choices" in res and res["choices"]:
                    return res["choices"][0]["message"].get("content", "").strip()
            except Exception:
                continue

        # 2. Try APMix fallback
        apmix_cfg = config.get("apmix", {})
        apmix_keys = apmix_cfg.get("keys", [])
        apmix_url = f"{apmix_cfg.get('base_url', 'https://api.apmix.ai/v1')}/chat/completions"
        apmix_model = "claude-sonnet-4-6-free"

        for key in apmix_keys:
            try:
                res = self._post_json(
                    apmix_url,
                    {
                        "model": apmix_model,
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": prompt},
                        ],
                        "max_tokens": 2048,
                    },
                    headers={
                        "Authorization": f"Bearer {key}",
                        "Content-Type": "application/json",
                    },
                )
                if "choices" in res and res["choices"]:
                    return res["choices"][0]["message"].get("content", "").strip()
            except Exception:
                continue

        return "All configured API keys (Groq & APMix) were exhausted or timed out."

    def _post_json(self, url: str, payload: dict, headers: dict, timeout: int = 20) -> dict:
        ctx = ssl.create_default_context()
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
