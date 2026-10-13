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
    Dual-Model Consultation & Synthesis Engine.
    Queries two independent AI models simultaneously (with random pairing across Qwen 27B,
    GPT-OSS 120B, Claude, GPT-6 Luna, etc.), compares both perspectives side-by-side,
    and synthesizes a conclusive, actionable recommendation.
    """

    def __init__(self):
        self._active_cancels: dict[str, asyncio.Event] = {}

    @property
    def agent_id(self) -> str:
        return "dual"

    @property
    def name(self) -> str:
        return "Dual Model Compare & Conclude (Random Dual AI)"

    @property
    def description(self) -> str:
        return (
            "Queries 2 independent models in parallel (Qwen, GPT-OSS 120B, Claude, etc.), "
            "compares their answers, and delivers a synthesized conclusion and action plan."
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
        thk_keys = cfg.get("tokenharbor", {}).get("keys", [])

        total_keys = len(groq_keys) + len(apmix_keys) + len(cc_keys) + len(thk_keys)
        if total_keys == 0:
            return (
                AvailabilityState.UNAUTHENTICATED,
                "No API keys configured in api_keys.json.",
            )

        return (
            AvailabilityState.READY,
            f"Dual consultation engine ready with {len(groq_keys)} fast keys across multiple frontier models.",
        )

    async def get_capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(
            supports_streaming=True,
            supports_code_edits=True,
            supports_terminal_execution=True,
            supports_subagents=False,
            supported_models=[
                "dual-random-pair",
                "qwen-vs-gptoss120b",
                "qwen-vs-claude",
            ],
        )

    async def stream_task(
        self, task_id: str, request: TaskCreateRequest
    ) -> AsyncIterator[AgentEvent]:
        state, diag = await self.check_availability()
        if state != AvailabilityState.READY:
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
            await asyncio.sleep(0.006)

        # ---------------- Phase 2: Action Execution via Antigravity ----------------
        if not cancel_event.is_set() and request.workspace_path:
            yield AgentEvent(
                task_id=task_id,
                event_type=EventType.LOG,
                text="⚖️ Dual consultation complete. Google Antigravity is now taking action in the workspace...",
                timestamp=time.time(),
            )

            from app.adapters.antigravity import AntigravityAdapter
            from app.adapters.codex import CodexAdapter

            action_prompt = (
                f"Based on the dual-model consultation verdict below, inspect the workspace and take direct action to resolve the user's issue:\n\n"
                f"{dual_result}\n\n"
                f"Original user request: {request.prompt}"
            )

            action_req = TaskCreateRequest(
                prompt=action_prompt,
                agent_id="antigravity",
                workspace_path=request.workspace_path,
                permission_mode=request.permission_mode,
                god_mode_confirmed=request.god_mode_confirmed,
                model=request.model,
            )

            executor: BaseAgentAdapter = AntigravityAdapter()
            exec_state, _ = await executor.check_availability()
            if exec_state != AvailabilityState.READY:
                codex = CodexAdapter()
                c_state, _ = await codex.check_availability()
                if c_state == AvailabilityState.READY:
                    executor = codex

            has_completed = False
            try:
                async for ev in executor.stream_task(task_id, action_req):
                    if cancel_event.is_set():
                        break
                    if ev.event_type == EventType.STARTED:
                        yield AgentEvent(
                            task_id=task_id,
                            event_type=EventType.LOG,
                            text=ev.text,
                            timestamp=ev.timestamp,
                            data=ev.data,
                        )
                        continue
                    if ev.event_type == EventType.COMPLETED:
                        has_completed = True
                    yield ev
            except Exception as exc:
                yield AgentEvent(
                    task_id=task_id,
                    event_type=EventType.LOG,
                    text=f"Workspace action notice: {exc}",
                    timestamp=time.time(),
                )

            if not has_completed and not cancel_event.is_set():
                yield AgentEvent(
                    task_id=task_id,
                    event_type=EventType.COMPLETED,
                    text="",
                    timestamp=time.time(),
                )
        else:
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

    def _post_json(self, url: str, payload: dict, headers: dict, timeout: int = 6) -> dict:
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

    def _query_model(self, model_spec: dict, config: dict, prompt: str, system: str) -> dict:
        provider = model_spec["provider"]
        model_id = model_spec["model_id"]
        display_name = model_spec["display_name"]

        if provider == "groq":
            groq_cfg = config.get("groq", {})
            keys = groq_cfg.get("keys", [])
            url = f"{groq_cfg.get('base_url', 'https://api.groq.com/openai/v1')}/chat/completions"
            for k in keys:
                try:
                    res = self._post_json(
                        url,
                        {
                            "model": model_id,
                            "messages": [
                                {"role": "system", "content": system},
                                {"role": "user", "content": prompt},
                            ],
                            "max_tokens": 1500,
                        },
                        {"Authorization": f"Bearer {k}"},
                        timeout=12,
                    )
                    content = res["choices"][0]["message"].get("content", "").strip()
                    if content:
                        return {"name": display_name, "content": content}
                except Exception:
                    continue

        elif provider == "apmix":
            apmix_cfg = config.get("apmix", {})
            keys = apmix_cfg.get("keys", [])
            url = f"{apmix_cfg.get('base_url', 'https://api.apmix.ai/v1')}/chat/completions"
            for k in keys:
                try:
                    res = self._post_json(
                        url,
                        {
                            "model": model_id,
                            "messages": [
                                {"role": "system", "content": system},
                                {"role": "user", "content": prompt},
                            ],
                            "max_tokens": 1500,
                        },
                        {"Authorization": f"Bearer {k}"},
                        timeout=10,
                    )
                    content = res["choices"][0]["message"].get("content", "").strip()
                    if content:
                        return {"name": display_name, "content": content}
                except Exception:
                    continue

        elif provider == "tokenharbor":
            th_cfg = config.get("tokenharbor", {})
            keys = th_cfg.get("keys", [])
            url = f"{th_cfg.get('base_url', 'https://api.tokenharbor.ai/v1')}/chat/completions"
            for k in keys:
                try:
                    res = self._post_json(
                        url,
                        {
                            "model": model_id,
                            "messages": [
                                {"role": "system", "content": system},
                                {"role": "user", "content": prompt},
                            ],
                            "max_tokens": 1500,
                        },
                        {"Authorization": f"Bearer {k}"},
                        timeout=10,
                    )
                    content = res["choices"][0]["message"].get("content", "").strip()
                    if content:
                        return {"name": display_name, "content": content}
                except Exception:
                    continue

        return {"name": display_name, "content": f"Provider {provider} ({model_id}) temporarily unreachable."}

    def _run_dual_and_synthesize(self, config: dict, prompt: str, cancel_event: asyncio.Event) -> str:
        # Dynamic pool of models across providers
        model_pool = [
            {"provider": "groq", "model_id": "qwen/qwen3.8-27b", "display_name": "Qwen 3.8 (27B)"},
            {"provider": "groq", "model_id": "openai/gpt-oss-120b", "display_name": "GPT-OSS (120B Frontier)"},
            {"provider": "groq", "model_id": "openai/gpt-oss-20b", "display_name": "GPT-OSS (20B Speed)"},
            {"provider": "groq", "model_id": "canopylabs/orpheus-v1-english", "display_name": "Orpheus v1"},
            {"provider": "apmix", "model_id": "claude-sonnet-4-6-free", "display_name": "Claude Sonnet 4.6"},
            {"provider": "tokenharbor", "model_id": "claude-haiku-5.5-free", "display_name": "Claude Haiku 5.5"},
        ]

        # Shuffle and select 2 distinct models
        random.shuffle(model_pool)
        model_a = model_pool[0]
        # Ensure model_b is distinct
        model_b = next((m for m in model_pool[1:] if m["model_id"] != model_a["model_id"]), model_pool[1])

        system = "You are a senior software engineering specialist providing concise, rigorous, and direct technical answers."

        res1 = self._query_model(model_a, config, prompt, system)
        if cancel_event.is_set():
            return "Cancelled"

        # If model_a failed, pick another from pool
        if "unreachable" in res1["content"]:
            for alt in model_pool:
                if alt["model_id"] not in (model_a["model_id"], model_b["model_id"]):
                    res1 = self._query_model(alt, config, prompt, system)
                    if "unreachable" not in res1["content"]:
                        model_a = alt
                        break

        res2 = self._query_model(model_b, config, prompt, system)
        if cancel_event.is_set():
            return "Cancelled"

        # If model_b failed, pick another from pool
        if "unreachable" in res2["content"]:
            for alt in model_pool:
                if alt["model_id"] not in (model_a["model_id"], model_b["model_id"]):
                    res2 = self._query_model(alt, config, prompt, system)
                    if "unreachable" not in res2["content"]:
                        model_b = alt
                        break

        model_a_name = res1["name"]
        model_a_text = res1["content"]
        model_b_name = res2["name"]
        model_b_text = res2["content"]

        # Run synthesis arbitration prompt with the frontier model
        synthesis_prompt = f"""You are a senior technical arbiter. Two independent AI models answered the user prompt:

[USER PROMPT]:
{prompt}

[MODEL A ({model_a_name})]:
{model_a_text}

[MODEL B ({model_b_name})]:
{model_b_text}

Task:
1. Provide a concise 2-bullet comparison (points of agreement vs differences).
2. Produce a clear, decisive FINAL CONCLUSION taking the best of both.
3. Formulate the concrete next action to take based on that conclusion.

Use clear markdown headers."""

        # Synthesize with Qwen or GPT-OSS
        synth_spec = {"provider": "groq", "model_id": "qwen/qwen3.8-27b", "display_name": "Synthesizer"}
        synth_res = self._query_model(synth_spec, config, synthesis_prompt, "You are a decisive technical arbiter.")
        synth_text = synth_res.get("content", "")
        if not synth_text or "unreachable" in synth_text:
            synth_spec = {"provider": "groq", "model_id": "openai/gpt-oss-120b", "display_name": "Synthesizer"}
            synth_res = self._query_model(synth_spec, config, synthesis_prompt, "You are a decisive technical arbiter.")
            synth_text = synth_res.get("content", "")

        return f"""# ⚖️ Dual-Model Consultation & Synthesis

### 🤖 Model A: {model_a_name}
{model_a_text}

---

### 🧠 Model B: {model_b_name}
{model_b_text}

---

### 🎯 Arbitration, Comparison & Final Conclusion
{synth_text}
"""
