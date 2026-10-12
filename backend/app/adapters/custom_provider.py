"""OpenAI-compatible custom endpoint adapter.

This is a text/model endpoint adapter, not a full tool-using coding agent. It
never claims to edit files or execute commands; use a provider's official coding
CLI/SDK when those capabilities are needed.
"""
import asyncio
import json
import time
from collections.abc import AsyncIterator
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.adapters.base import BaseAgentAdapter
from app.core.provider_store import get_keys
from app.models.schemas import AgentCapabilities, AgentEvent, AvailabilityState, EventType, TaskCreateRequest


class CustomProviderAdapter(BaseAgentAdapter):
    def __init__(self, config: dict):
        self.config = config
        self._cancel: dict[str, asyncio.Event] = {}

    @property
    def agent_id(self) -> str:
        return "custom-" + self.config["id"]

    @property
    def name(self) -> str:
        return self.config["name"]

    @property
    def description(self) -> str:
        return f"OpenAI-compatible endpoint · {self.config['model']}"

    async def check_availability(self) -> tuple[AvailabilityState, str]:
        if not get_keys(self.config["id"]):
            return AvailabilityState.UNAUTHENTICATED, "API key is missing from the OS credential store."
        return AvailabilityState.READY, "Configured endpoint; connectivity is checked when a task runs."

    async def get_capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(
            supports_streaming=False,
            supports_code_edits=False,
            supports_terminal_execution=False,
            supports_subagents=False,
            supported_models=[self.config["model"]],
        )

    def _query(self, prompt: str) -> str:
        keys = get_keys(self.config["id"])
        if not keys:
            raise RuntimeError("API key is not available in the OS credential store.")
        base = self.config["base_url"].rstrip("/")
        url = base if base.endswith("/chat/completions") else base + "/chat/completions"
        payload = json.dumps({
            "model": self.config["model"],
            "messages": [
                {"role": "system", "content": "You are a helpful software engineering assistant. Do not claim to have edited files or run commands unless a connected tool actually did so."},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
        }).encode("utf-8")
        errors = []
        for index, key in enumerate(keys, start=1):
            req = Request(url, data=payload, headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"}, method="POST")
            try:
                with urlopen(req, timeout=90) as response:
                    body = json.loads(response.read().decode("utf-8"))
                try:
                    return body["choices"][0]["message"]["content"] or ""
                except (KeyError, IndexError, TypeError) as exc:
                    raise RuntimeError("Endpoint response was not a compatible chat-completions response.") from exc
            except HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:220]
                errors.append(f"key {index}: HTTP {exc.code} {detail}")
            except (URLError, TimeoutError) as exc:
                errors.append(f"key {index}: connection failed ({exc})")
            except Exception as exc:
                errors.append(f"key {index}: {exc}")
        raise RuntimeError("All configured API keys failed: " + "; ".join(errors[-4:]))

    async def stream_task(self, task_id: str, request: TaskCreateRequest) -> AsyncIterator[AgentEvent]:
        cancel = asyncio.Event()
        self._cancel[task_id] = cancel
        yield AgentEvent(task_id=task_id, event_type=EventType.STARTED, text=f"Querying {self.name}. This provider returns text only; it cannot directly edit files.", timestamp=time.time())
        try:
            result = await asyncio.to_thread(self._query, request.prompt)
            if cancel.is_set():
                yield AgentEvent(task_id=task_id, event_type=EventType.CANCELLED, text="Request cancelled.", timestamp=time.time())
                return
            # Deliver in chunks so the UI remains responsive.
            for start in range(0, len(result), 160):
                if cancel.is_set():
                    yield AgentEvent(task_id=task_id, event_type=EventType.CANCELLED, text="Request cancelled.", timestamp=time.time())
                    return
                yield AgentEvent(task_id=task_id, event_type=EventType.TOKEN, text=result[start:start + 160], timestamp=time.time())
            yield AgentEvent(task_id=task_id, event_type=EventType.COMPLETED, text="Provider response completed.", timestamp=time.time())
        except Exception as exc:
            yield AgentEvent(task_id=task_id, event_type=EventType.ERROR, text=f"Provider request failed: {exc}", timestamp=time.time())
        finally:
            self._cancel.pop(task_id, None)

    async def cancel_task(self, task_id: str) -> bool:
        event = self._cancel.get(task_id)
        if event:
            event.set()
            return True
        return False
