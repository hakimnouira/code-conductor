import asyncio
import json
import time
from collections.abc import AsyncIterator
import httpx
from app.adapters.base import BaseAgentAdapter
from app.models.schemas import (
    AgentCapabilities,
    AgentEvent,
    AvailabilityState,
    EventType,
    TaskCreateRequest,
)

class OllamaAdapter(BaseAgentAdapter):
    """Local model adapter supporting Ollama and OpenAI-compatible LM Studio."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        lm_studio_url: str = "http://127.0.0.1:1234/v1",
        default_model: str = "qwen2.5-coder",
        timeout: float = 60.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.lm_studio_url = lm_studio_url.rstrip("/")
        self.default_model = default_model
        self.timeout = timeout
        self._active_cancels: dict[str, asyncio.Event] = {}

    @property
    def agent_id(self) -> str:
        return "ollama"

    @property
    def name(self) -> str:
        return "Ollama / Local Models"

    @property
    def description(self) -> str:
        return "Local inference via Ollama or LM Studio. Runs 100% locally with zero cloud API keys."

    async def _probe_ollama(self) -> tuple[bool, list[str]]:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(3.0)) as client:
                res = await client.get(f"{self.base_url}/api/tags")
                if res.status_code == 200:
                    data = res.json()
                    models = [m["name"] for m in data.get("models", [])]
                    return True, models
        except Exception:
            pass
        return False, []

    async def _probe_lm_studio(self) -> tuple[bool, list[str]]:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(3.0)) as client:
                res = await client.get(f"{self.lm_studio_url}/models")
                if res.status_code == 200:
                    data = res.json()
                    models = [m["id"] for m in data.get("data", [])]
                    return True, models
        except Exception:
            pass
        return False, []

    async def check_availability(self) -> tuple[AvailabilityState, str]:
        # 1. Try Ollama first
        ollama_online, ollama_models = await self._probe_ollama()
        if ollama_online:
            if ollama_models:
                return (
                    AvailabilityState.READY,
                    f"Ollama is online on {self.base_url}. Installed models: {', '.join(ollama_models[:5])}",
                )
            else:
                return (
                    AvailabilityState.UNAUTHENTICATED,
                    f"Ollama daemon is running on {self.base_url}, but no models are downloaded. "
                    "Run 'ollama pull qwen2.5-coder' or 'ollama pull llama3' in PowerShell to download a model.",
                )

        # 2. Try LM Studio fallback
        lms_online, lms_models = await self._probe_lm_studio()
        if lms_online:
            if lms_models:
                return (
                    AvailabilityState.READY,
                    f"LM Studio local server is online on {self.lm_studio_url}. Models: {', '.join(lms_models[:5])}",
                )
            else:
                return (
                    AvailabilityState.UNAUTHENTICATED,
                    f"LM Studio local server is running on {self.lm_studio_url}, but no model is currently loaded. "
                    "Please load and start a model in the LM Studio Local Server tab.",
                )

        # Neither local engine is running
        return (
            AvailabilityState.MISSING_CLI,
            f"No local model engine detected. Ollama is unreachable on {self.base_url} and LM Studio is unreachable on {self.lm_studio_url}. "
            "To use local models without API keys: install Ollama from https://ollama.com and run 'ollama serve'.",
        )

    async def get_capabilities(self) -> AgentCapabilities:
        _, models = await self._probe_ollama()
        if not models:
            _, models = await self._probe_lm_studio()
        if not models:
            models = ["qwen2.5-coder", "llama3.2", "codellama", "deepseek-coder"]

        return AgentCapabilities(
            supports_streaming=True,
            supports_code_edits=True,
            supports_terminal_execution=False,
            supports_subagents=False,
            supported_models=models,
        )

    async def stream_task(self, task_id: str, request: TaskCreateRequest) -> AsyncIterator[AgentEvent]:
        state, diag = await self.check_availability()
        if state != AvailabilityState.READY:
            yield AgentEvent(
                task_id=task_id,
                event_type=EventType.ERROR,
                text=f"Local model engine unavailable: {diag}",
                timestamp=time.time(),
            )
            return

        cancel_event = asyncio.Event()
        self._active_cancels[task_id] = cancel_event

        # Check which engine is available
        ollama_online, ollama_models = await self._probe_ollama()
        use_ollama = ollama_online and len(ollama_models) > 0
        model_name = request.model or (ollama_models[0] if ollama_models else self.default_model)

        yield AgentEvent(
            task_id=task_id,
            event_type=EventType.STARTED,
            text=f"Local model task started with model '{model_name}'.",
            timestamp=time.time(),
        )

        try:
            if use_ollama:
                # Stream using Ollama chat NDJSON API
                payload = {
                    "model": model_name,
                    "messages": [{"role": "user", "content": request.prompt}],
                    "stream": True,
                }
                async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout, connect=5.0)) as client:
                    async with client.stream("POST", f"{self.base_url}/api/chat", json=payload) as resp:
                        if resp.status_code != 200:
                            err_text = await resp.aread()
                            yield AgentEvent(
                                task_id=task_id,
                                event_type=EventType.ERROR,
                                text=f"Ollama HTTP {resp.status_code}: {err_text.decode('utf-8', errors='replace')}",
                                timestamp=time.time(),
                            )
                            return

                        async for line in resp.aiter_lines():
                            if cancel_event.is_set():
                                yield AgentEvent(
                                    task_id=task_id,
                                    event_type=EventType.CANCELLED,
                                    text="Local model task cancelled by user request.",
                                    timestamp=time.time(),
                                )
                                return

                            if not line.strip():
                                continue

                            try:
                                chunk = json.loads(line)
                                text_token = chunk.get("message", {}).get("content", "")
                                if text_token:
                                    yield AgentEvent(
                                        task_id=task_id,
                                        event_type=EventType.TOKEN,
                                        text=text_token,
                                        timestamp=time.time(),
                                    )
                                if chunk.get("done", False):
                                    break
                            except Exception:
                                continue
            else:
                # Stream using LM Studio / OpenAI-compatible SSE API
                payload = {
                    "model": model_name,
                    "messages": [{"role": "user", "content": request.prompt}],
                    "stream": True,
                }
                async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout, connect=5.0)) as client:
                    async with client.stream("POST", f"{self.lm_studio_url}/chat/completions", json=payload) as resp:
                        if resp.status_code != 200:
                            err_text = await resp.aread()
                            yield AgentEvent(
                                task_id=task_id,
                                event_type=EventType.ERROR,
                                text=f"LM Studio HTTP {resp.status_code}: {err_text.decode('utf-8', errors='replace')}",
                                timestamp=time.time(),
                            )
                            return

                        async for line in resp.aiter_lines():
                            if cancel_event.is_set():
                                yield AgentEvent(
                                    task_id=task_id,
                                    event_type=EventType.CANCELLED,
                                    text="Local model task cancelled.",
                                    timestamp=time.time(),
                                )
                                return

                            trimmed = line.strip()
                            if trimmed.startswith("data:"):
                                data_str = trimmed[5:].strip()
                                if data_str == "[DONE]":
                                    break
                                try:
                                    chunk = json.loads(data_str)
                                    delta = chunk.get("choices", [{}])[0].get("delta", {}).get("content", "")
                                    if delta:
                                        yield AgentEvent(
                                            task_id=task_id,
                                            event_type=EventType.TOKEN,
                                            text=delta,
                                            timestamp=time.time(),
                                        )
                                except Exception:
                                    continue

            yield AgentEvent(
                task_id=task_id,
                event_type=EventType.COMPLETED,
                text="Local model inference completed successfully.",
                timestamp=time.time(),
            )
        except Exception as e:
            yield AgentEvent(
                task_id=task_id,
                event_type=EventType.ERROR,
                text=f"Local model error: {str(e)}",
                timestamp=time.time(),
            )
        finally:
            self._active_cancels.pop(task_id, None)

    async def cancel_task(self, task_id: str) -> bool:
        event = self._active_cancels.get(task_id)
        if event:
            event.set()
            return True
        return False
