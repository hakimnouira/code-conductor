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

        groq_keys = cfg.get("groq", {}).get("keys", [])
        codecraft_keys = cfg.get("codecraft", {}).get("keys", [])
        apmix_keys = cfg.get("apmix", {}).get("keys", [])
        xpl_keys = cfg.get("experientiallabs", {}).get("keys", [])
        thk_keys = cfg.get("tokenharbor", {}).get("keys", [])

        if self.provider == "groq":
            if not groq_keys:
                return AvailabilityState.UNAUTHENTICATED, "No Groq API keys configured."
            return AvailabilityState.READY, f"Groq engine ready with {len(groq_keys)} rotating API keys."

        if self.provider == "codecraft":
            if not codecraft_keys:
                return AvailabilityState.UNAUTHENTICATED, "No CodeCraft API keys configured."
            return AvailabilityState.READY, f"CodeCraft engine ready with {len(codecraft_keys)} active keys."

        if self.provider == "apmix":
            if not apmix_keys:
                return AvailabilityState.UNAUTHENTICATED, "No APMix API keys configured."
            return AvailabilityState.READY, f"APMix gateway ready with {len(apmix_keys)} keys."

        if self.provider == "experientiallabs":
            if not xpl_keys:
                return AvailabilityState.UNAUTHENTICATED, "No Experiential Labs keys configured."
            return AvailabilityState.READY, f"Experiential Labs ready with {len(xpl_keys)} keys."

        # For "auto" / "multimodel"
        total_keys = len(groq_keys) + len(codecraft_keys) + len(apmix_keys) + len(xpl_keys) + len(thk_keys)
        if total_keys == 0:
            return AvailabilityState.UNAUTHENTICATED, "No active API keys found in api_keys.json."

        return (
            AvailabilityState.READY,
            f"Multi-Model Router ready with {total_keys} total keys across Groq, CodeCraft, and APMix.",
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
            await asyncio.sleep(0.01)

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
        providers_to_try = []
        if self.provider in ("groq", "codecraft", "apmix", "experientiallabs"):
            providers_to_try.append(self.provider)
            # Add other providers as fallback
            for p in ["groq", "codecraft", "apmix", "experientiallabs"]:
                if p not in providers_to_try:
                    providers_to_try.append(p)
        else:
            # Default auto sequence: Groq (ultra fast 8ms) -> CodeCraft -> APMix -> ExperientialLabs
            providers_to_try = ["groq", "codecraft", "apmix", "experientiallabs"]

        for prov in providers_to_try:
            if prov == "groq":
                groq_cfg = config.get("groq", {})
                keys = groq_cfg.get("keys", [])
                url = f"{groq_cfg.get('base_url', 'https://api.groq.com/openai/v1')}/chat/completions"
                model = groq_cfg.get("default_model", "qwen/qwen3.8-27b")
                for key in keys:
                    try:
                        res = self._post_json(
                            url,
                            {
                                "model": model,
                                "messages": [
                                    {"role": "system", "content": system},
                                    {"role": "user", "content": prompt},
                                ],
                                "max_tokens": 2048,
                            },
                            headers={"Authorization": f"Bearer {key}"},
                            timeout=8,
                        )
                        if "choices" in res and res["choices"]:
                            return res["choices"][0]["message"].get("content", "").strip()
                    except Exception:
                        continue

            elif prov == "codecraft":
                cc_cfg = config.get("codecraft", {})
                keys = cc_cfg.get("keys", [])
                url = f"{cc_cfg.get('base_url', 'https://codecraftapi.com/v1')}/chat/completions"
                model = cc_cfg.get("default_model", "gemma-2-2b")
                for key in keys:
                    try:
                        res = self._post_json(
                            url,
                            {
                                "model": model,
                                "messages": [
                                    {"role": "system", "content": system},
                                    {"role": "user", "content": prompt},
                                ],
                                "max_tokens": 1024,
                            },
                            headers={"Authorization": f"Bearer {key}"},
                            timeout=6,
                        )
                        if "choices" in res and res["choices"]:
                            return res["choices"][0]["message"].get("content", "").strip()
                    except Exception:
                        continue

            elif prov == "apmix":
                apmix_cfg = config.get("apmix", {})
                keys = apmix_cfg.get("keys", [])
                url = f"{apmix_cfg.get('base_url', 'https://api.apmix.ai/v1')}/chat/completions"
                model = "claude-sonnet-4-6-free"
                for key in keys:
                    try:
                        res = self._post_json(
                            url,
                            {
                                "model": model,
                                "messages": [
                                    {"role": "system", "content": system},
                                    {"role": "user", "content": prompt},
                                ],
                                "max_tokens": 2048,
                            },
                            headers={"Authorization": f"Bearer {key}"},
                            timeout=6,
                        )
                        if "choices" in res and res["choices"]:
                            return res["choices"][0]["message"].get("content", "").strip()
                    except Exception:
                        continue

            elif prov == "experientiallabs":
                xpl_cfg = config.get("experientiallabs", {})
                keys = xpl_cfg.get("keys", [])
                url = f"{xpl_cfg.get('base_url', 'https://api.experientiallabs.ai/v1')}/chat/completions"
                model = "aion-3.0-mini"
                for key in keys:
                    try:
                        res = self._post_json(
                            url,
                            {
                                "model": model,
                                "messages": [
                                    {"role": "system", "content": system},
                                    {"role": "user", "content": prompt},
                                ],
                                "max_tokens": 1024,
                            },
                            headers={"Authorization": f"Bearer {key}"},
                            timeout=6,
                        )
                        if "choices" in res and res["choices"]:
                            return res["choices"][0]["message"].get("content", "").strip()
                    except Exception:
                        continue

        return "All configured API keys were exhausted or timed out."

    def _post_json(self, url: str, payload: dict, headers: dict, timeout: int = 10) -> dict:
        ctx = ssl.create_default_context()
        data = json.dumps(payload).encode("utf-8")
        all_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) CodeConductor/1.0",
            "Content-Type": "application/json",
            **headers,
        }
        req = urllib.request.Request(url, data=data, headers=all_headers)
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
