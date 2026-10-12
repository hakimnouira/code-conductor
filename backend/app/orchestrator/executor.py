import asyncio
import hashlib
import re
import subprocess
import uuid
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
        self.workspace_root = Path(workspace_root).resolve() if workspace_root else Path.cwd().resolve()
        self.reviewer = CodeReviewer()
        self.history_tracker = AuditHistoryTracker()
        self._isolated_mode = False
        self._repo_root: Path | None = None
        self._workspace_relative = Path(".")
        self._worker_worktrees: dict[str, dict[str, str]] = {}
        self._worktree_lock = asyncio.Lock()

    def _git(self, root: Path, *args: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, check=False,
        )

    def _prepare_isolated_mode(self) -> str:
        """Enable parallel workers only when a clean Git base can be isolated safely."""
        try:
            result = self._git(self.workspace_root, "rev-parse", "--show-toplevel")
            if result.returncode != 0:
                return "Workspace is not a Git repository; workers will run sequentially."
            repo_root = Path(result.stdout.strip()).resolve()
            status = self._git(repo_root, "status", "--porcelain", "--untracked-files=all")
            if status.returncode != 0:
                return "Git status could not be read; workers will run sequentially."
            if status.stdout.strip():
                return "Workspace has uncommitted changes; workers will run sequentially to preserve them."
            self._workspace_relative = self.workspace_root.relative_to(repo_root)
            self._repo_root = repo_root
            self._isolated_mode = True
            return "Clean Git workspace detected; independent workers will use isolated worktrees."
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            return f"Worktree isolation unavailable ({str(exc)[:160]}); workers will run sequentially."

    def _create_worker_worktree(self, task_id: str, subtask_id: str) -> Path:
        if not self._repo_root:
            raise RuntimeError("No Git repository configured for isolated workers.")
        safe_task = re.sub(r"[^A-Za-z0-9._-]", "-", task_id)[:60]
        safe_subtask = re.sub(r"[^A-Za-z0-9._-]", "-", subtask_id)[:40]
        branch = f"cc/{safe_task}/{safe_subtask}-{uuid.uuid4().hex[:6]}"
        digest = hashlib.sha256(str(self._repo_root).encode("utf-8")).hexdigest()[:16]
        managed_root = (Path.home() / ".code-conductor" / "worktrees" / digest).resolve()
        managed_root.mkdir(parents=True, exist_ok=True)
        target = (managed_root / f"{safe_task}-{safe_subtask}-{uuid.uuid4().hex[:6]}").resolve()
        if not target.is_relative_to(managed_root):
            raise RuntimeError("Worker worktree path escaped its managed root.")
        result = self._git(self._repo_root, "worktree", "add", "-b", branch, str(target), "HEAD", timeout=90)
        if result.returncode != 0:
            raise RuntimeError(f"Could not create worker worktree: {result.stderr[-800:]}")
        self._worker_worktrees[subtask_id] = {"path": str(target), "branch": branch}
        return (target / self._workspace_relative).resolve()

    def _remove_worker_worktree(self, subtask_id: str, delete_branch: bool = True) -> None:
        info = self._worker_worktrees.get(subtask_id)
        if not info or not self._repo_root:
            return
        path = Path(info["path"]).resolve()
        try:
            if path.is_relative_to((Path.home() / ".code-conductor" / "worktrees").resolve()):
                self._git(self._repo_root, "worktree", "remove", "--force", str(path), timeout=60)
                if delete_branch:
                    self._git(self._repo_root, "branch", "-D", info["branch"])
                self._worker_worktrees.pop(subtask_id, None)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def _integrate_worker(self, subtask: Subtask) -> None:
        """Commit safe worker changes and merge them only when Git can do so cleanly."""
        info = self._worker_worktrees.get(subtask.id)
        if not info or not self._repo_root:
            return
        worktree = Path(info["path"]).resolve()
        if subtask.status != SubtaskStatus.COMPLETED:
            subtask.error = (subtask.error + " " if subtask.error else "") + f"Worker changes retained at {worktree}"
            return
        try:
            status = self._git(worktree, "status", "--porcelain", "--untracked-files=all")
            if status.returncode != 0:
                raise RuntimeError(status.stderr[-500:])
            if not status.stdout.strip():
                self._remove_worker_worktree(subtask.id)
                return

            changed_paths: list[str] = []
            blocked: list[str] = []
            for line in status.stdout.splitlines():
                raw_path = line[3:].split(" -> ")[-1].strip()
                normalized = raw_path.replace("\\", "/")
                lower = normalized.lower()
                segments = lower.split("/")
                if (
                    any(segment == ".git" for segment in segments)
                    or any(segment.startswith(".env") for segment in segments)
                    or lower.endswith((".pem", ".key", ".pfx", ".p12", ".pkcs12"))
                    or any(token in segments for token in ("credentials", "secrets"))
                    or any("api_keys" in segment for segment in segments)
                ):
                    blocked.append(raw_path)
                    continue
                resolved = (worktree / raw_path).resolve()
                if not resolved.is_relative_to(worktree):
                    blocked.append(raw_path)
                    continue
                changed_paths.append(raw_path)

            if blocked:
                raise RuntimeError("Refusing to integrate sensitive or escaping paths: " + ", ".join(blocked[:8]))
            if not changed_paths:
                raise RuntimeError("Worker produced no safe, stageable file changes.")

            staged = self._git(worktree, "add", "--", *changed_paths)
            if staged.returncode != 0:
                raise RuntimeError(f"Could not stage worker changes: {staged.stderr[-500:]}")
            diff = self._git(worktree, "diff", "--cached", "--")
            if diff.returncode != 0:
                raise RuntimeError(f"Could not inspect worker diff: {diff.stderr[-500:]}")
            subtask.diff = diff.stdout[:30000]
            commit = self._git(
                worktree,
                "-c", "user.name=Code Conductor",
                "-c", "user.email=code-conductor@localhost",
                "commit", "-m", f"Code Conductor: {subtask.title[:100]}",
                timeout=90,
            )
            if commit.returncode != 0:
                raise RuntimeError(f"Could not commit worker changes: {commit.stderr[-700:]}")
            merge = self._git(self._repo_root, "merge", "--no-ff", "--no-edit", info["branch"], timeout=90)
            if merge.returncode != 0:
                self._git(self._repo_root, "merge", "--abort")
                raise RuntimeError(
                    f"Integration conflict. Worker branch {info['branch']} and worktree {worktree} were retained. "
                    + merge.stderr[-700:]
                )
            self._remove_worker_worktree(subtask.id)
        except (OSError, subprocess.TimeoutExpired, RuntimeError, ValueError) as exc:
            subtask.status = SubtaskStatus.FAILED
            subtask.error = f"Worker integration failed: {str(exc)[:1200]}"
            if subtask.id in self._worker_worktrees:
                subtask.error += f" Worker branch/worktree retained for recovery: {self._worker_worktrees[subtask.id]['path']}"

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

        worker_workspace = self.workspace_root
        try:
            if self._isolated_mode:
                async with self._worktree_lock:
                    worker_workspace = self._create_worker_worktree(task_id, subtask.id)
        except Exception as exc:
            subtask.status = SubtaskStatus.FAILED
            subtask.error = f"Could not isolate worker workspace: {str(exc)[:800]}"
            subtask.duration_ms = round((time.perf_counter() - t_start) * 1000.0, 2)
            return subtask

        req = TaskCreateRequest(
            prompt=subtask.prompt,
            workspace_path=str(worker_workspace),
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

        isolation_message = self._prepare_isolated_mode()
        yield AgentEvent(
            task_id=task_id,
            event_type=EventType.LOG,
            text=isolation_message,
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

            # Parallel execution is enabled only for a clean Git workspace where each worker
            # can safely edit an isolated worktree. Dirty/non-Git workspaces are serialized.
            if self._isolated_mode:
                results = await asyncio.gather(*[self.execute_subtask(st, task_id) for st in runnable])
                for res in results:
                    self._integrate_worker(res)
            else:
                results = []
                for worker in runnable:
                    results.append(await self.execute_subtask(worker, task_id))

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
