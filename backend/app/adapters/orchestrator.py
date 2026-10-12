import asyncio
import time
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
from app.orchestrator.executor import ParallelOrchestrator
from app.orchestrator.planner import PlanDecomposer


class OrchestratorAdapter(BaseAgentAdapter):
    """
    Multi-Agent Orchestrator Adapter:
    - Plans and decomposes prompts into disjoint subtask DAGs.
    - Executes independent subtasks in parallel across available agents.
    - Gates outputs through an automated code review agent.
    - Tracks audit history immutably.
    """

    def __init__(self, workspace_root: str | Path | None = None):
        self.workspace_root = Path(workspace_root) if workspace_root else Path.cwd()
        self._active_cancels: dict[str, asyncio.Event] = {}

    @property
    def agent_id(self) -> str:
        return "orchestrator"

    @property
    def name(self) -> str:
        return "Multi-Agent Orchestrator"

    @property
    def description(self) -> str:
        return (
            "Autonomous multi-agent coordinator: breaks requests into a DAG of subtasks, "
            "executes disjoint file edits in parallel, reviews code, and records audit logs."
        )

    async def check_availability(self) -> tuple[AvailabilityState, str]:
        return (
            AvailabilityState.READY,
            "Multi-Agent Orchestrator is active with DAG planning, parallel workers, and code review.",
        )

    async def get_capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(
            supports_streaming=True,
            supports_code_edits=True,
            supports_terminal_execution=False,
            supports_subagents=True,
            supported_models=["multi-agent-dag"],
        )

    async def stream_task(self, task_id: str, request: TaskCreateRequest) -> AsyncIterator[AgentEvent]:
        cancel_event = asyncio.Event()
        self._active_cancels[task_id] = cancel_event

        ws = Path(request.workspace_path or self.workspace_root).resolve()
        planner = PlanDecomposer(ws)
        plan = planner.decompose(request.prompt)

        executor = ParallelOrchestrator(ws)
        async for event in executor.run_plan_stream(task_id, plan):
            if cancel_event.is_set():
                yield AgentEvent(
                    task_id=task_id,
                    event_type=EventType.CANCELLED,
                    text="Multi-Agent Orchestration cancelled by user.",
                    timestamp=time.time(),
                )
                break
            yield event

        self._active_cancels.pop(task_id, None)

    async def cancel_task(self, task_id: str) -> bool:
        event = self._active_cancels.get(task_id)
        if event:
            event.set()
            return True
        return False
