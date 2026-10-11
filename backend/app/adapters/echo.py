import asyncio
import time
from collections.abc import AsyncIterator
from app.adapters.base import BaseAgentAdapter
from app.models.schemas import (
    AgentCapabilities,
    AgentEvent,
    AvailabilityState,
    EventType,
    TaskCreateRequest,
)

class EchoMockAdapter(BaseAgentAdapter):
    """Deterministic Mock/Echo agent adapter for unit and UI testing."""

    def __init__(self):
        self._active_tasks: dict[str, asyncio.Event] = {}

    @property
    def agent_id(self) -> str:
        return "mock"

    @property
    def name(self) -> str:
        return "Mock Echo Agent"

    @property
    def description(self) -> str:
        return "Deterministic testing agent that streams simulated responses."

    async def check_availability(self) -> tuple[AvailabilityState, str]:
        return AvailabilityState.READY, "Mock agent is always available for local verification."

    async def get_capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(
            supports_streaming=True,
            supports_code_edits=True,
            supports_terminal_execution=False,
            supports_subagents=False,
            supported_models=["mock-echo-v1"],
        )

    async def stream_task(self, task_id: str, request: TaskCreateRequest) -> AsyncIterator[AgentEvent]:
        cancel_event = asyncio.Event()
        self._active_tasks[task_id] = cancel_event

        now = time.time()
        yield AgentEvent(
            task_id=task_id,
            event_type=EventType.STARTED,
            text=f"Mock agent started task '{task_id}'.",
            timestamp=now,
        )

        words = f"Echo response to: '{request.prompt}'. Working directory is validated.".split(" ")
        try:
            for word in words:
                if cancel_event.is_set():
                    yield AgentEvent(
                        task_id=task_id,
                        event_type=EventType.CANCELLED,
                        text="Task execution was cancelled by user request.",
                        timestamp=time.time(),
                    )
                    return

                await asyncio.sleep(0.05)
                yield AgentEvent(
                    task_id=task_id,
                    event_type=EventType.TOKEN,
                    text=word + " ",
                    timestamp=time.time(),
                )

            yield AgentEvent(
                task_id=task_id,
                event_type=EventType.COMPLETED,
                text="Mock task completed successfully.",
                timestamp=time.time(),
            )
        finally:
            self._active_tasks.pop(task_id, None)

    async def cancel_task(self, task_id: str) -> bool:
        event = self._active_tasks.get(task_id)
        if event:
            event.set()
            return True
        return False
