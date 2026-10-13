import asyncio
import json
import random
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
    from app.core.config import get_unified_api_config
    return get_unified_api_config()


class DualModelAdapter(BaseAgentAdapter):
    """
    Dual-Model Consultation & Synthesis Adapter.
    Queries two independent AI models simultaneously (e.g. Groq + APMix / CodeCraft),
    compares both perspectives, synthesizes a conclusive answer, and formulates
    concrete follow-up actions based on that conclusion.
    """

    def __init__(self):
        self._active_cancels: dict[str, asyncio.Event] = {}

    @property
    def agent_id(self) -> str:
        return "dual"

    @property
    def name(self) -> str:
        return "Dual Model Compare & Conclude (Groq + APMix)"

    @property
    def description(self) -> str:
        return (
            "Queries 2 independent models in parallel, compares both answers, "
            "and generates a synthesized conclusion and actionable recommendation."
        )

    async def check_availability(self) -> tuple[AvailabilityState, str]:
        cfg = load_api_config()
        if not cfg:
            return (
                AvailabilityState.UNAUTHENTICATED,
                f"No API key configuration found at {CONFIG_PATH}.",
            )

        groq_keys = cfg.get("groq", {}).get("keys", [])
        apmix_keys = cfg.get("apmix", {}).get("keys", [])
        cc_keys = cfg.get("codecraft", {}).get("keys", [])
        xpl_keys = cfg.get("experientiallabs", {}).get("keys", [])
        thk_keys = cfg.get("tokenharbor", {}).get("keys", [])

        ready_count = sum(1 for k in [groq_keys, apmix_keys, cc_keys, xpl_keys, thk_keys] if len(k) > 0)
        if ready_count < 2:
            return (
                AvailabilityState.DEGRADED,
                "Dual consultation requires at least 2 active providers configured in api_keys.json.",
            )

        return (
            AvailabilityState.READY,
            f"Dual consultation engine ready with {ready_count} providers available.",
        )

    async def get_capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(
            supports_streaming=True,
            supports_code_edits=False,
            supports_terminal_execution=False,
            supports_subagents=False,
            supported_models=["dual-groq-apmix", "dual-random-pair"],
        )

    async def stream_task(
        self, task_id: str, request: TaskCreateRequest
    ) -> AsyncIterator[AgentEvent]:
        state, diag = await self.check_availability()
        if state not in (AvailabilityState.READY, AvailabilityState.DEGRADED):
            yield AgentEvent(
                task_id=task_id,
                event_type=EventType.ERROR,
                text=f"Dual consultation unavailable: {diag}",
                timestamp=time.time(),
            )
            return

        cancel_event = asyncio.Event()
        self._active_cancels[task_id] = cancel_event

        yield AgentEvent(
            task_id=task_id,
            event_type=EventType.STARTED,
            text="Querying 2 independent models in parallel for comparative synthesis...",
            timestamp=time.time(),
        )

        cfg = load_api_config()
        prompt = request.prompt

        # Run dual queries asynchronously in worker thread
        try:
            dual_result = await asyncio.to_thread(self._run_dual_and_synthesize, cfg, prompt, cancel_event)
        except Exception as e:
            yield AgentEvent(
                task_id=task_id,
                event_type=EventType.ERROR,
                text=f"Dual model execution failed: {str(e)}",
                timestamp=time.time(),
            )
            self._active_cancels.pop(task_id, None)
            return

        # Stream formatted markdown output
        words = dual_result.split(" ")
        for i, word in enumerate(words):
            if cancel_event.is_set():
                yield AgentEvent(
                    task_id=task_id,
                    event_type=EventType.CANCELLED,
                    text="Dual consultation cancelled.",
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
            await asyncio.sleep(0.008)

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

    def _post_json(self, url: str, payload: dict, headers: dict, timeout: int = 12) -> dict:
        ctx = ssl.create_default_context()
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) CodeConductor/1.0",
                "Content-Type": "application/json",
                **headers,
            },
        )
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _query_provider(self, prov: str, config: dict, prompt: str, system: str) -> dict:
        """Queries a single provider with key rotation and failover."""
        if prov == "groq":
            cfg = config.get("groq", {})
            keys = cfg.get("keys", [])
            url = f"{cfg.get('base_url', 'https://api.groq.com/openai/v1')}/chat/completions"
            model = cfg.get("default_model", "qwen/qwen3.8-27b")
            for k in keys:
                try:
                    res = self._post_json(
                        url,
                        {
                            "model": model,
                            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                            "max_tokens": 1500,
                        },
                        {"Authorization": f"Bearer {k}"},
                        timeout=10,
                    )
                    content = res["choices"][0]["message"].get("content", "").strip()
                    return {"name": f"Groq ({model})", "content": content}
                except Exception:
                    continue

        elif prov == "apmix":
            cfg = config.get("apmix", {})
            keys = cfg.get("keys", [])
            url = f"{cfg.get('base_url', 'https://api.apmix.ai/v1')}/chat/completions"
            model = "claude-sonnet-4-6-free"
            for k in keys:
                try:
                    res = self._post_json(
                        url,
                        {
                            "model": model,
                            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                            "max_tokens": 1500,
                        },
                        {"Authorization": f"Bearer {k}"},
                        timeout=10,
                    )
                    content = res["choices"][0]["message"].get("content", "").strip()
                    return {"name": f"APMix ({model})", "content": content}
                except Exception:
                    continue

        elif prov == "codecraft":
            cfg = config.get("codecraft", {})
            keys = cfg.get("keys", [])
            url = f"{cfg.get('base_url', 'https://codecraftapi.com/v1')}/chat/completions"
            model = cfg.get("default_model", "gemma-2-2b")
            for k in keys:
                try:
                    res = self._post_json(
                        url,
                        {
                            "model": model,
                            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                            "max_tokens": 1024,
                        },
                        {"Authorization": f"Bearer {k}"},
                        timeout=10,
                    )
                    content = res["choices"][0]["message"].get("content", "").strip()
                    return {"name": f"CodeCraft ({model})", "content": content}
                except Exception:
                    continue

        elif prov == "experientiallabs":
            cfg = config.get("experientiallabs", {})
            keys = cfg.get("keys", [])
            url = f"{cfg.get('base_url', 'https://api.experientiallabs.ai/v1')}/chat/completions"
            model = "aion-3.0-mini"
            for k in keys:
                try:
                    res = self._post_json(
                        url,
                        {
                            "model": model,
                            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                            "max_tokens": 1024,
                        },
                        {"Authorization": f"Bearer {k}"},
                        timeout=10,
                    )
                    content = res["choices"][0]["message"].get("content", "").strip()
                    return {"name": f"Experiential Labs ({model})", "content": content}
                except Exception:
                    continue

        return {"name": prov, "content": f"Unable to reach {prov}."}

    def _run_dual_and_synthesize(self, config: dict, prompt: str, cancel_event: asyncio.Event) -> str:
        # Choose two distinct candidate providers
        all_candidates = ["groq", "apmix", "codecraft", "experientiallabs"]
        # Filter by configured keys
        configured = [c for c in all_candidates if config.get(c, {}).get("keys")]
        if len(configured) >= 2:
            random.shuffle(configured)
            p1, p2 = configured[0], configured[1]
        else:
            p1, p2 = "groq", "apmix"

        system = "You are a senior software engineering specialist providing concise, rigorous, and direct technical answers."

        res1 = self._query_provider(p1, config, prompt, system)
        if cancel_event.is_set():
            return "Cancelled"
        res2 = self._query_provider(p2, config, prompt, system)
        if cancel_event.is_set():
            return "Cancelled"

        model_a_name = res1["name"]
        model_a_text = res1["content"]
        model_b_name = res2["name"]
        model_b_text = res2["content"]

        # Run synthesis prompt
        synthesis_prompt = f"""You are an expert AI arbiter. Two independent models answered the user request:

[USER PROMPT]:
{prompt}

[MODEL A: {model_a_name}]:
{model_a_text}

[MODEL B: {model_b_name}]:
{model_b_text}

Task:
1. Provide a quick 2-bullet comparison (points of agreement vs difference).
2. Produce a clear, decisive FINAL CONCLUSION that takes the best parts of both.
3. Formulate the concrete next action to take based on that conclusion.

Keep the response organized with clear markdown headers."""

        synth_res = self._query_provider(p1, config, synthesis_prompt, "You are a concise, decisive AI technical arbiter.")
        synth_text = synth_res.get("content", "")
        if not synth_text or "Unable to reach" in synth_text:
            synth_res = self._query_provider(p2, config, synthesis_prompt, "You are a concise, decisive AI technical arbiter.")
            synth_text = synth_res.get("content", "")

        output = f"""# ⚖️ Dual-Model Consultation & Synthesis

### 🤖 Model A: {model_a_name}
{model_a_text}

---

### 🧠 Model B: {model_b_name}
{model_b_text}

---

### 🎯 Arbitration, Conclusion & Action Plan
{synth_text}
"""
        return output
