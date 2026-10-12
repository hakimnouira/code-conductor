import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path

from app.models.schemas import (
    AgentEvent,
    EventType,
    TaskCreateRequest,
)
from app.orchestrator.history import AuditHistoryTracker
from app.orchestrator.models import (
    ExecutionPlan,
    OrchestrationAuditRecord,
    ReviewResult,
    Subtask,
    SubtaskStatus,
)
from app.orchestrator.reviewer import CodeReviewer


class ParallelOrchestrator:
    """
    Executes an ExecutionPlan across parallel workers:
    - Runs disjoint subtasks in concurrent batches via asyncio.gather.
    - Captures outputs, execution time, and generated diffs.
    - Invokes CodeReviewer gatekeeper.
    - Persists execution state to AuditHistoryTracker.
    """

    def __init__(self, workspace_root: str | Path | None = None):
        self.workspace_root = Path(workspace_root) if workspace_root else Path.cwd()
        self.reviewer = CodeReviewer()
        self.history_tracker = AuditHistoryTracker()

    async def execute_subtask(self, subtask: Subtask, task_id: str) -> Subtask:
        subtask.status = SubtaskStatus.RUNNING
        t_start = time.perf_counter()

        from app.adapters.registry import get_agent_registry
        registry = get_agent_registry()

        # Resolve target agent adapter
        adapter = registry.get(subtask.assigned_agent) or await registry.resolve_agent("auto")
        if not adapter:
            subtask.status = SubtaskStatus.FAILED
            subtask.error = f"Agent '{subtask.assigned_agent}' not found."
            subtask.duration_ms = (time.perf_counter() - t_start) * 1000.0
            return subtask

        req = TaskCreateRequest(
            prompt=subtask.prompt,
            workspace_path=str(self.workspace_root),
        )

        output_tokens: list[str] = []
        try:
            async for event in adapter.stream_task(f"{task_id}-{subtask.id}", req):
                if event.event_type == EventType.TOKEN:
                    output_tokens.append(event.text)
                elif event.event_type == EventType.ERROR:
                    subtask.error = event.text

            subtask.output = "".join(output_tokens).strip()
            subtask.duration_ms = round((time.perf_counter() - t_start) * 1000.0, 2)
            subtask.status = SubtaskStatus.COMPLETED if not subtask.error else SubtaskStatus.FAILED
        except Exception as e:
            subtask.status = SubtaskStatus.FAILED
            subtask.error = str(e)
            subtask.duration_ms = round((time.perf_counter() - t_start) * 1000.0, 2)

        return subtask

    async def run_plan_stream(
        self,
        task_id: str,
        plan: ExecutionPlan,
    ) -> AsyncIterator[AgentEvent]:
        t_start = time.time()
        yield AgentEvent(
            task_id=task_id,
            event_type=EventType.STARTED,
            text=f"Multi-Agent Plan started: '{plan.goal}' with {len(plan.subtasks)} subtasks across {len(plan.parallel_batches)} batches.",
            timestamp=time.time(),
        )

        subtask_map = {s.id: s for s in plan.subtasks}

        # Execute batch by batch
        for b_idx, batch_ids in enumerate(plan.parallel_batches):
            batch_subtasks = [subtask_map[tid] for tid in batch_ids]

            # Filter out tasks whose dependencies failed
            runnable: list[Subtask] = []
            for st in batch_subtasks:
                failed_deps = [dep for dep in st.dependencies if subtask_map.get(dep) and subtask_map[dep].status != SubtaskStatus.COMPLETED]
                if failed_deps:
                    st.status = SubtaskStatus.SKIPPED
                    st.error = f"Skipped: dependency '{failed_deps[0]}' did not complete."
                    yield AgentEvent(
                        task_id=task_id,
                        event_type=EventType.LOG,
                        text=f"[Skipped] {st.title} (unmet dependencies)",
                        timestamp=time.time(),
                    )
                else:
                    runnable.append(st)

            if not runnable:
                continue

            batch_titles = ", ".join(t.title for t in runnable)
            yield AgentEvent(
                task_id=task_id,
                event_type=EventType.LOG,
                text=f"[Batch {b_idx + 1}/{len(plan.parallel_batches)}] Running {len(runnable)} parallel workers: {batch_titles}",
                timestamp=time.time(),
            )

            # Concurrent execution of disjoint tasks
            results = await asyncio.gather(*[self.execute_subtask(st, task_id) for st in runnable])

            for res in results:
                status_icon = "[PASSED]" if res.status == SubtaskStatus.COMPLETED else "[FAILED]"
                yield AgentEvent(
                    task_id=task_id,
                    event_type=EventType.TOKEN,
                    text=f"\n### {status_icon} Subtask: {res.title} ({res.assigned_agent})\n{res.output}\n",
                    timestamp=time.time(),
                )

        # Automated Code Review Step
        yield AgentEvent(
            task_id=task_id,
            event_type=EventType.LOG,
            text="[Code Review Gate] Inspecting candidate outputs and security policies...",
            timestamp=time.time(),
        )

        review = self.reviewer.review_changes(plan.goal, plan.subtasks)
        verdict = "APPROVED" if review.approved else "ACTION REQUIRED"

        yield AgentEvent(
            task_id=task_id,
            event_type=EventType.TOKEN,
            text=(
                f"\n---\n### Code Review Gate: {verdict} (Score: {review.score}/100)\n"
                f"{review.summary}\n"
            ),
            timestamp=time.time(),
        )

        if review.issues:
            for issue in review.issues:
                yield AgentEvent(
                    task_id=task_id,
                    event_type=EventType.LOG,
                    text=f"[{issue.severity.upper()}] {issue.file}: {issue.message}",
                    timestamp=time.time(),
                )

        # Audit History Record
        t_end = time.time()
        audit_record = OrchestrationAuditRecord(
            task_id=task_id,
            goal=plan.goal,
            plan=plan,
            review=review,
            success=review.approved,
            total_duration_ms=round((t_end - t_start) * 1000.0, 2),
            created_at=t_start,
            completed_at=t_end,
        )
        self.history_tracker.append_record(audit_record)

        yield AgentEvent(
            task_id=task_id,
            event_type=EventType.COMPLETED,
            text="Multi-agent orchestration completed.",
            timestamp=time.time(),
        )
