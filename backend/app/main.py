import asyncio
import base64
from collections import Counter
import hashlib
import json
import os
import sys
import time
import uuid
from urllib.parse import urlsplit

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import secrets
from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
from fastapi.security import HTTPAuthorizationCredentials
from sse_starlette.sse import EventSourceResponse

from app.adapters.registry import get_agent_registry
from app.adapters.custom_provider import CustomProviderAdapter
from app.core.provider_store import delete_provider, list_providers, upsert_provider
from app.core.config import get_settings
from app.core.security import require_bearer_token, security_scheme
from app.models.schemas import (
    AgentInfo,
    CheckpointRequest,
    CheckpointResponse,
    EventType,
    FileReadRequest,
    FileReadResponse,
    FileWriteRequest,
    FileWriteResponse,
    GitDiffRequest,
    GitDiffResponse,
    GitStatusResponse,
    HealthResponse,
    SafeFileListResponse,
    ScheduleCreateRequest,
    ScheduleResponse,
    ScheduleRunResponse,
    ScheduleUpdateRequest,
    TaskCreateRequest,
    TaskResponse,
    WorkflowPresetRequest,
    WorkflowPresetResponse,
    TaskStatus,
    TestRunRequest,
    TestRunResponse,
)
from app.safety.workspace import (
    WorkspaceSecurityManager,
    PathTraversalError,
    SensitiveFileBlockedError,
    WorkspaceValidationError,
    validate_workspace_path,
)
from app.safety.git_guard import GitGuard
from app.safety.test_runner import SafeTestRunner, TestRunnerSecurityError
from app.safety.security_scan import scan_recent_secrets
from app.provenance import collect_workspace_provenance
from app.orchestrator.history import AuditHistoryTracker
from app.orchestrator.models import ExecutionPlan, OrchestrationAuditRecord
from app.orchestrator.planner import PlanDecomposer
from pathlib import Path
from app.repository_import import router as repository_import_router
from app.github_research import research_github, should_research
from app.antigravity_history import router as antigravity_history_router
from app.browser_session import router as browser_session_router
from app.browser_automation import run_browser_automation
from app.scheduler import get_scheduler_diagnostics, scheduler_loop
from app.cron_schedule import CronExpressionError, next_cron_occurrence
from app.guided_workflow import router as guided_workflow_router
from app.api.openai_compat import router as openai_compat_router
from app.git_worktrees import router as git_worktrees_router
from app.task_leases import claim_task_lease, renew_task_lease, release_task_lease
from app.completion_gate import evaluate_completion_gate
from app.task_recovery import should_resume_pending_task
from app.task_store import (
    append_event, delete_schedule, delete_workflow, events_after,
    get_saved_task, get_saved_tasks, get_schedule, get_schedule_run_summary, list_schedule_runs,
    set_scheduler_paused,
    list_schedules, list_workflows, save_task, save_workflow, create_schedule, update_schedule,
    queue_schedule_run_now, create_task_if_absent,
)



# Task records are cached in memory for active execution, with durable local state/events.
class PersistentEventQueue:
    def __init__(self, record: "TaskRecord"):
        self.record = record
        self.queue: asyncio.Queue[str | None] = asyncio.Queue()

    async def put(self, item: str | None) -> None:
        if isinstance(item, str):
            timestamp = time.time()
            try:
                parsed = json.loads(item)
                timestamp = float(parsed.get("timestamp", timestamp))
            except (ValueError, TypeError, AttributeError):
                pass
            append_event(self.record.task_id, item, timestamp)
            self.record._persist()
        await self.queue.put(item)


class TaskRecord:
    def __init__(
        self,
        task_id: str,
        request: TaskCreateRequest,
        agent_id: str,
        restored: dict | None = None,
        initial_request_json: str | None = None,
    ):
        self.task_id = task_id
        self.request = request
        self.agent_id = agent_id
        self.initial_request_json = (
            request.model_dump_json() if initial_request_json is None else initial_request_json
        )
        self.original_request_available = (
            bool(restored.get("original_request_available", bool(initial_request_json)))
            if restored is not None else True
        )
        self.request_fingerprint = str(
            (restored or {}).get("request_fingerprint")
            or hashlib.sha256(request.model_dump_json().encode("utf-8")).hexdigest()
        )
        self._status = TaskStatus.PENDING
        self.created_at = time.time()
        self.started_at: float | None = None
        self.completed_at: float | None = None
        self.error: str | None = None
        self.evidence: list[dict] = []
        self.verification_status = "not_requested"
        self.event_queue = PersistentEventQueue(self)
        if restored:
            try:
                self._status = TaskStatus(restored.get("status", TaskStatus.PENDING.value))
            except ValueError:
                self._status = TaskStatus.FAILED
            self.created_at = float(restored.get("created_at") or self.created_at)
            self.started_at = restored.get("started_at")
            self.completed_at = restored.get("completed_at")
            self.error = restored.get("error")
            evidence = restored.get("evidence", [])
            self.evidence = [item for item in evidence if isinstance(item, dict)] if isinstance(evidence, list) else []
            self.verification_status = str(restored.get("verification_status") or "not_requested")
        if restored is not None:
            self._persist()
            self.initial_persisted = True
        else:
            # INSERT OR IGNORE makes the first durable writer authoritative when
            # two backend processes receive the same idempotency key concurrently.
            self.initial_persisted = create_task_if_absent(
                self.snapshot(),
                self.request.model_dump_json(),
                self.initial_request_json,
            )

    def add_evidence(
        self,
        kind: str,
        status: str,
        summary: str,
        *,
        details: dict | None = None,
        artifacts: list[str] | None = None,
    ) -> dict:
        """Attach bounded, machine-readable evidence to the task and artifact manifest."""
        item = {
            "evidence_id": uuid.uuid4().hex,
            "kind": str(kind)[:80],
            "status": str(status)[:40],
            "summary": str(summary)[:1000],
            "timestamp": time.time(),
            "details": details or {},
            "artifacts": [str(path)[:500] for path in (artifacts or [])[:20]],
        }
        self.evidence.append(item)
        self.evidence = self.evidence[-200:]
        self._persist()
        try:
            root = Path(self.request.workspace_path).resolve()
            artifact_dir = root / ".code-conductor" / "artifacts" / self.task_id
            # Resolve before creating anything: an existing symlink must not redirect
            # directory creation outside the selected workspace.
            resolved_dir = artifact_dir.resolve()
            if not resolved_dir.is_relative_to(root):
                raise ValueError("Evidence artifact directory escapes the selected workspace.")
            resolved_dir.mkdir(parents=True, exist_ok=True)
            manifest = {
                "schema_version": 1,
                "task_id": self.task_id,
                "task_status": self.status.value,
                "verification_status": self.verification_status,
                "updated_at": time.time(),
                "evidence": self.evidence,
            }
            target = resolved_dir / "evidence.json"
            temporary = resolved_dir / "evidence.json.tmp"
            temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(target)
        except (OSError, ValueError, TypeError):
            # The durable task database remains authoritative if workspace artifacts
            # cannot be written (for example, a read-only workspace).
            pass
        return item

    @property
    def status(self) -> TaskStatus:
        return self._status

    @status.setter
    def status(self, value: TaskStatus) -> None:
        self._status = value
        if hasattr(self, "event_queue"):
            self._persist()

    def _persist(self) -> None:
        save_task(
            self.snapshot(),
            self.request.model_dump_json(),
            self.initial_request_json,
        )

    def snapshot(self) -> dict:
        """Return a stable status payload without exposing provider secrets."""
        return {
            "task_id": self.task_id,
            "status": self.status.value,
            "agent_id": self.agent_id,
            "request_fingerprint": self.request_fingerprint,
            "original_request_available": self.original_request_available,
            "retry_of": self.request.retry_of,
            "prompt": self.request.prompt,
            "acceptance_criteria": self.request.acceptance_criteria,
            "workspace_path": self.request.workspace_path,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "error": self.error,
            "verification_status": self.verification_status,
            "evidence": self.evidence,
        }


_tasks: dict[str, TaskRecord] = {}
_idempotency_lock = asyncio.Lock()
_start_time = time.time()


async def _restore_task_records(task_id: str | None = None) -> None:
    """Restore history and mark interrupted tasks instead of pretending they resumed."""
    saved_items = [get_saved_task(task_id)] if task_id else get_saved_tasks(limit=500)
    for item in saved_items:
        if item is None:
            continue
        snapshot = item["snapshot"]
        task_id = snapshot.get("task_id")
        if not task_id or task_id in _tasks:
            continue
        try:
            request = TaskCreateRequest.model_validate(item["request"])
            record = TaskRecord(
                task_id=task_id,
                request=request,
                agent_id=str(snapshot.get("agent_id") or request.agent_id),
                restored=snapshot,
                initial_request_json=(
                    json.dumps(item.get("initial_request") or {}, ensure_ascii=False)
                    if item.get("initial_request_available", False) else ""
                ),
            )
        except Exception:
            continue
        _tasks[task_id] = record
        if record.status == TaskStatus.RUNNING or (
            record.status == TaskStatus.PENDING
            and not should_resume_pending_task(
                record.status.value, record.started_at, record.original_request_available
            )
        ):
            interrupted_status = record.status
            record.error = (
                "Backend restarted while this task was running. Automatic replay is disabled "
                "because provider side effects may already have occurred."
                if interrupted_status == TaskStatus.RUNNING
                else "Queued task cannot be resumed because its immutable original request is unavailable."
            )
            record.completed_at = time.time()
            record.status = TaskStatus.FAILED
            record.add_evidence(
                "restart_recovery", "failed", record.error,
                details={
                    "reason": "backend_restart",
                    "previous_status": interrupted_status.value,
                    "automatic_replay": False,
                    "recoverable": bool(record.original_request_available),
                },
            )
            await record.event_queue.put(json.dumps({
                "task_id": task_id,
                "event_type": "error",
                "text": record.error,
                "timestamp": record.completed_at,
                "data": {
                    "reason": "backend_restart",
                    "recoverable": bool(record.original_request_available),
                    "automatic_replay": False,
                },
            }))
            record._persist()


async def _resume_pending_tasks_after_restart() -> None:
    """Resume only durable tasks proven not to have started before a crash."""
    registry = get_agent_registry()
    for record in list(_tasks.values()):
        if not should_resume_pending_task(
            record.status.value, record.started_at, record.original_request_available
        ):
            continue
        try:
            record.request.workspace_path = str(validate_workspace_path(
                record.request.workspace_path,
                default_root=get_settings().workspace_root,
            ))
            adapter = await registry.resolve_agent(record.request.agent_id, record.request.prompt)
            if not adapter:
                raise RuntimeError("No configured provider could resolve the queued task.")
        except Exception as exc:
            record.error = "Queued task could not resume after restart: " + str(exc)[:400]
            record.completed_at = time.time()
            record.status = TaskStatus.FAILED
            record.add_evidence(
                "restart_recovery", "failed", record.error,
                details={"reason": "resume_unavailable", "automatic_replay": False},
            )
            await record.event_queue.put(json.dumps({
                "task_id": record.task_id,
                "event_type": "error",
                "text": record.error,
                "timestamp": record.completed_at,
                "data": {"reason": "resume_unavailable", "automatic_replay": False},
            }))
            await record.event_queue.put(None)
            continue

        record.agent_id = adapter.agent_id
        record.add_evidence(
            "restart_recovery", "passed",
            "Resuming a queued task that had not started before the backend restart.",
            details={"reason": "unstarted_pending_task", "automatic_replay": True},
        )
        await record.event_queue.put(json.dumps({
            "task_id": record.task_id,
            "event_type": "log",
            "text": "Backend recovery resumed this queued task; provider execution had not started before the restart.",
            "timestamp": time.time(),
            "data": {"phase": "restart_recovery", "status": "resumed"},
        }))
        asyncio.create_task(
            _run_task_pipeline(record, adapter),
            name=f"recovered-task-{record.task_id}",
        )


async def _dispatch_scheduled_workflow(workflow: dict) -> str:
    """Dispatch a scheduled workflow through the same validated task path as interactive work."""
    request = TaskCreateRequest(
        prompt=str(workflow.get("prompt") or ""),
        agent_id=str(workflow.get("agent_id") or "auto"),
        idempotency_key=str(workflow.get("_code_conductor_idempotency_key") or "") or None,
        workspace_path=str(workflow.get("workspace_path") or "") or None,
        auto_verify=bool(workflow.get("auto_verify", False)),
        acceptance_criteria=workflow.get("acceptance_criteria") or [],
        browser_verify_url=str(workflow.get("browser_verify_url") or "") or None,
        browser_required_text=workflow.get("browser_required_text") or [],
        browser_forbidden_text=workflow.get("browser_forbidden_text") or [],
        browser_visible_selectors=workflow.get("browser_visible_selectors") or [],
        browser_hidden_selectors=workflow.get("browser_hidden_selectors") or [],
        browser_steps=workflow.get("browser_steps") or [],
        github_research=False,
        permission_mode="safe",
    )
    response = await create_task(request, "scheduler")
    return response.task_id


async def _lookup_scheduled_task(task_id: str) -> dict | None:
    record = _tasks.get(task_id)
    return record.snapshot() if record else None


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _restore_task_records()
    await _resume_pending_tasks_after_restart()
    scheduler_task = asyncio.create_task(
        scheduler_loop(_dispatch_scheduled_workflow, _lookup_scheduled_task),
        name="code-conductor-scheduler",
    )
    try:
        yield
    finally:
        scheduler_task.cancel()
        await asyncio.gather(scheduler_task, return_exceptions=True)
        # Shutdown marks active work as interrupted; it is not silently resumed.
        for task in _tasks.values():
            if task.status in (TaskStatus.PENDING, TaskStatus.RUNNING):
                task.status = TaskStatus.CANCELLED
                task.error = "Backend shut down while this task was active."
                task.completed_at = time.time()
                await task.event_queue.put(json.dumps({
                    "task_id": task.task_id,
                    "event_type": "cancelled",
                    "text": task.error,
                    "timestamp": task.completed_at,
                    "data": {"reason": "backend_shutdown"},
                }))
                task._persist()
                await task.event_queue.put(None)


app = FastAPI(
    title="Unified AI Coding Hub Backend",
    version=get_settings().version,
    lifespan=lifespan,
)

app.include_router(repository_import_router)
app.include_router(antigravity_history_router)
app.include_router(browser_session_router)
app.include_router(guided_workflow_router)
app.include_router(openai_compat_router)
app.include_router(git_worktrees_router)

# Strictly restrict CORS to local origin
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "vscode-webview://*",
        "http://127.0.0.1", "http://localhost",
        "http://127.0.0.1:5173", "http://localhost:5173",
        "http://127.0.0.1:4173", "http://localhost:4173",
    ],
    allow_credentials=True,
    # The dashboard uses PUT/DELETE for project rules and browser model management.
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

@app.get("/health", response_model=HealthResponse)
async def get_health(_: str = Depends(require_bearer_token)):
    settings = get_settings()
    active = sum(1 for t in _tasks.values() if t.status == TaskStatus.RUNNING)
    return HealthResponse(
        status="healthy",
        version=settings.version,
        uptime_seconds=time.time() - _start_time,
        workspace=settings.workspace_root,
        active_tasks=active,
    )


@app.get("/diagnostics")
async def get_runtime_diagnostics(_: str = Depends(require_bearer_token)):
    """Expose authenticated, secret-safe scheduler/task health and recent run aggregates."""
    now = time.time()
    database_status = "healthy"
    database_error = ""
    try:
        saved_tasks = get_saved_tasks(limit=500)
        schedules = list_schedules(limit=200)
        run_summary = get_schedule_run_summary(limit=10)
    except Exception as exc:
        saved_tasks = []
        schedules = []
        run_summary = {"counts_by_status": {}, "recent_runs": []}
        database_status = "degraded"
        database_error = type(exc).__name__

    task_counts = Counter(
        str(item.get("snapshot", {}).get("status") or "unknown")
        for item in saved_tasks
    )
    verification_counts = Counter(
        str(item.get("snapshot", {}).get("verification_status") or "not_requested")
        for item in saved_tasks
    )
    stale_threshold_seconds = 15 * 60
    stale_tasks = []
    for task in _tasks.values():
        if task.status != TaskStatus.RUNNING or task.started_at is None:
            continue
        age = max(0.0, now - float(task.started_at))
        if age >= stale_threshold_seconds:
            stale_tasks.append({
                "task_id": task.task_id,
                "agent_id": task.agent_id,
                "age_seconds": round(age, 2),
            })

    scheduler = get_scheduler_diagnostics()
    overall_status = (
        "healthy"
        if database_status == "healthy"
        and scheduler["status"] == "healthy"
        and not stale_tasks
        else "degraded"
    )
    return {
        "status": overall_status,
        "captured_at": now,
        "uptime_seconds": round(now - _start_time, 2),
        "database": {"status": database_status, "error_type": database_error or None},
        "scheduler": scheduler,
        "tasks": {
            "persisted_sample_size": len(saved_tasks),
            "counts_by_status": dict(task_counts),
            "verification_counts": dict(verification_counts),
            "active_in_memory": sum(1 for task in _tasks.values() if task.status == TaskStatus.RUNNING),
            "stale_threshold_seconds": stale_threshold_seconds,
            "stale_tasks": sorted(stale_tasks, key=lambda item: item["age_seconds"], reverse=True)[:20],
        },
        "schedules": {
            "sample_size": len(schedules),
            "enabled": sum(1 for schedule in schedules if schedule.get("enabled")),
            "runs": run_summary,
        },
    }

class GitHubResearchRequest(BaseModel):
    prompt: str
    workspace_path: str | None = None
    clone_best: bool = True


@app.post("/research/github")
async def preview_github_research(req: GitHubResearchRequest, _: str = Depends(require_bearer_token)):
    ws = _resolve_validated_ws(req.workspace_path)
    return await research_github(req.prompt, str(ws), clone_best=req.clone_best)


@app.get("/agents", response_model=list[AgentInfo])
async def list_agents(_: str = Depends(require_bearer_token)):
    registry = get_agent_registry()
    return await registry.list_agents()

async def _create_task_record(
    request: TaskCreateRequest,
    validated_ws: Path,
    task_id: str,
) -> TaskResponse:
    registry = get_agent_registry()
    adapter = await registry.resolve_agent(request.agent_id, request.prompt)
    if not adapter:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent '{request.agent_id}' could not be resolved or no provider engine is currently active.",
        )

    record = TaskRecord(task_id=task_id, request=request, agent_id=adapter.agent_id)
    if not record.initial_persisted:
        # Another process atomically created this deterministic task ID first. Never
        # replace its request or status with this process's duplicate in-memory record.
        saved = get_saved_task(task_id)
        if saved is None:
            raise HTTPException(status_code=503, detail="Task creation raced with another worker; durable state could not be read.")
        saved_snapshot = saved["snapshot"]
        if saved_snapshot.get("request_fingerprint") != record.request_fingerprint:
            raise HTTPException(status_code=409, detail="Idempotency key was already used with a different task request.")
        existing_request = TaskCreateRequest.model_validate(saved["request"])
        existing = TaskRecord(
            task_id=task_id,
            request=existing_request,
            agent_id=str(saved_snapshot.get("agent_id") or existing_request.agent_id),
            restored=saved_snapshot,
            initial_request_json=(
                json.dumps(saved.get("initial_request") or {}, ensure_ascii=False)
                if saved.get("initial_request_available", False) else ""
            ),
        )
        _tasks[task_id] = existing
        return TaskResponse(
            task_id=existing.task_id,
            status=existing.status,
            agent_id=existing.agent_id,
            prompt=existing.request.prompt,
            created_at=existing.created_at,
            message="Returning the task atomically created by another backend worker.",
            workspace_path=existing.request.workspace_path,
        )

    _tasks[task_id] = record
    # Spawn background task processing that feeds the event queue.
    asyncio.create_task(_run_task_pipeline(record, adapter))
    return TaskResponse(
        task_id=task_id,
        status=TaskStatus.PENDING,
        agent_id=adapter.agent_id,
        prompt=request.prompt,
        created_at=record.created_at,
        message=f"Task assigned to '{adapter.name}'",
        workspace_path=str(validated_ws),
    )


@app.post("/tasks", response_model=TaskResponse)
async def create_task(request: TaskCreateRequest, _: str = Depends(require_bearer_token)):
    if request.permission_mode == "god" and not request.god_mode_confirmed:
        raise HTTPException(status_code=400, detail="God Mode requires explicit confirmation in the task request.")

    # Validate and canonicalize the workspace before computing an idempotency
    # fingerprint, so equivalent requests resolve to the same task identity.
    try:
        validated_ws = validate_workspace_path(
            request.workspace_path,
            default_root=get_settings().workspace_root,
        )
        request.workspace_path = str(validated_ws)
    except WorkspaceValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid workspace path: {str(e)}",
        )

    request_fingerprint = hashlib.sha256(request.model_dump_json().encode("utf-8")).hexdigest()
    task_id = (
        "task-" + hashlib.sha256(request.idempotency_key.encode("utf-8")).hexdigest()[:16]
        if request.idempotency_key
        else f"task-{uuid.uuid4().hex[:8]}"
    )

    if request.idempotency_key:
        # Serialize same-process duplicate requests, including the async provider
        # lookup. The durable deterministic task ID additionally handles restarts.
        async with _idempotency_lock:
            if task_id not in _tasks:
                await _restore_task_records(task_id=task_id)
            existing = _tasks.get(task_id)
            if existing:
                if existing.request_fingerprint != request_fingerprint:
                    raise HTTPException(
                        status_code=409,
                        detail="Idempotency key was already used with a different task request.",
                    )
                return TaskResponse(
                    task_id=existing.task_id,
                    status=existing.status,
                    agent_id=existing.agent_id,
                    prompt=request.prompt,
                    created_at=existing.created_at,
                    message="Returning the existing task for this idempotency key.",
                    workspace_path=existing.request.workspace_path,
                )
            return await _create_task_record(request, validated_ws, task_id)

    return await _create_task_record(request, validated_ws, task_id)

async def _verify_browser_for_task(record: TaskRecord) -> tuple[bool, str]:
    """Run an opt-in browser smoke-check and persist its screenshot as a workspace artifact."""
    target_url = record.request.browser_verify_url
    if not target_url:
        return True, ""

    try:
        result = await run_browser_automation(
            target_url,
            wait_seconds=2,
            required_text=record.request.browser_required_text,
            forbidden_text=record.request.browser_forbidden_text,
            visible_selectors=record.request.browser_visible_selectors,
            hidden_selectors=record.request.browser_hidden_selectors,
            steps=record.request.browser_steps,
        )
        screenshot_uri = result.pop("screenshot", None)
        artifact_relative = None
        if isinstance(screenshot_uri, str) and screenshot_uri.startswith("data:image/png;base64,"):
            root = Path(record.request.workspace_path).resolve()
            artifact_dir = root / ".code-conductor" / "artifacts" / record.task_id
            # Resolve before mkdir to prevent a pre-existing symlink from redirecting
            # screenshot writes outside the selected workspace.
            resolved_dir = artifact_dir.resolve()
            if not resolved_dir.is_relative_to(root):
                raise ValueError("Browser artifact directory escapes the selected workspace.")
            resolved_dir.mkdir(parents=True, exist_ok=True)
            image_path = resolved_dir / "browser-verification.png"
            if not image_path.resolve().is_relative_to(root):
                raise ValueError("Browser screenshot path escapes the selected workspace.")
            image_bytes = base64.b64decode(screenshot_uri.split(",", 1)[1], validate=True)
            image_path.write_bytes(image_bytes)
            artifact_relative = str(image_path.relative_to(root)).replace("\\", "/")

        result["screenshot_artifact"] = artifact_relative
        success = bool(result.get("success"))
        diagnostic = json.dumps({
            "url": result.get("url", target_url),
            "title": result.get("title", ""),
            "checks": result.get("checks", {}),
            "acceptance_assertions": result.get("acceptance_assertions", {}),
            "action_results": result.get("action_results", []),
            "main_document_status": result.get("main_document_status"),
            "page_errors": result.get("page_errors", []),
            "console_errors": result.get("console_errors", []),
            "http_errors": result.get("http_errors", []),
            "failed_requests": result.get("failed_requests", []),
            "verification_engine": result.get("verification_engine", "unknown"),
            "verification_errors": result.get("verification_errors", []),
            "screenshot_scope": result.get("screenshot_scope", "full_page"),
            "limitations": result.get("limitations", []),
            "error_markers": result.get("error_markers", []),
            "body_text_excerpt": result.get("body_text_excerpt", "")[-3000:],
            "screenshot_artifact": artifact_relative,
        }, ensure_ascii=False)
        decoded_diagnostic = json.loads(diagnostic)
        record.verification_status = "passed" if success else "failed"
        record.add_evidence(
            "browser_verification",
            "passed" if success else "failed",
            "Browser acceptance checks " + ("passed." if success else "failed."),
            details=decoded_diagnostic,
            artifacts=[artifact_relative] if artifact_relative else [],
        )
        await record.event_queue.put(json.dumps({
            "task_id": record.task_id,
            "event_type": "log",
            "text": "Browser verification " + ("passed." if success else "failed."),
            "timestamp": time.time(),
            "data": {
                "phase": "browser_verification",
                "status": "passed" if success else "failed",
                "result": decoded_diagnostic,
            },
        }))
        return success, diagnostic
    except Exception as exc:
        diagnostic = f"Browser verification could not complete: {str(exc)[:500]}"
        record.verification_status = "failed"
        record.add_evidence(
            "browser_verification", "error", diagnostic,
            details={"error": str(exc)[:1000]},
        )
        await record.event_queue.put(json.dumps({
            "task_id": record.task_id,
            "event_type": "log",
            "text": diagnostic,
            "timestamp": time.time(),
            "data": {"phase": "browser_verification", "status": "error"},
        }))
        return False, diagnostic


async def _browser_verification_gate(record: TaskRecord, attempts: int) -> tuple[str, int]:
    """Return passed/retry/failed and use bounded agent repair attempts for browser failures."""
    if not record.request.browser_verify_url:
        return "passed", attempts
    success, diagnostic = await _verify_browser_for_task(record)
    if success:
        return "passed", attempts

    attempts += 1
    if attempts < record.request.verification_max_attempts:
        record.request.prompt += (
            "\n\nBROWSER VERIFICATION FAILED. Fix the visible application issue; do not "
            "remove checks or hide errors. Then summarize the repair.\n"
            + diagnostic[-5000:]
        )
        await record.event_queue.put(json.dumps({
            "task_id": record.task_id,
            "event_type": "log",
            "text": "Sending browser verification findings to the agent for a repair attempt.",
            "timestamp": time.time(),
            "data": {"phase": "browser_verification", "status": "repair_requested", "attempt": attempts},
        }))
        return "retry", attempts

    record.status = TaskStatus.FAILED
    record.error = f"Browser verification failed after {attempts} attempt(s)."
    record.completed_at = time.time()
    await record.event_queue.put(json.dumps({
        "task_id": record.task_id,
        "event_type": "error",
        "text": record.error + " " + diagnostic[-1000:],
        "timestamp": record.completed_at,
        "data": {"phase": "browser_verification", "status": "failed", "attempts": attempts},
    }))
    return "failed", attempts


class TaskExecutionTimeout(TimeoutError):
    """Raised when a task exceeds its configured wall-clock deadline."""


def _task_timeout_seconds() -> int:
    try:
        configured = int(os.getenv("CODE_CONDUCTOR_TASK_TIMEOUT_SECONDS", "3600"))
    except (TypeError, ValueError):
        configured = 3600
    return min(max(configured, 60), 86400)


async def _await_task_deadline(awaitable, deadline: float, timeout_seconds: int):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TaskExecutionTimeout(
            f"Task exceeded its {timeout_seconds}-second execution deadline."
        )
    try:
        return await asyncio.wait_for(awaitable, timeout=remaining)
    except asyncio.TimeoutError as exc:
        raise TaskExecutionTimeout(
            f"Task exceeded its {timeout_seconds}-second execution deadline."
        ) from exc


async def _stream_task_events_with_deadline(adapter, record: TaskRecord, deadline: float, timeout_seconds: int):
    iterator = adapter.stream_task(record.task_id, record.request).__aiter__()
    while True:
        try:
            event = await _await_task_deadline(iterator.__anext__(), deadline, timeout_seconds)
        except StopAsyncIteration:
            return
        yield event


async def _finalize_task_completion(record: TaskRecord, terminal_event) -> bool:
    """Make completion contingent on the verification the request actually required."""
    gate = evaluate_completion_gate(
        record.request,
        record.evidence,
        record.verification_status,
    )
    if not gate["passed"]:
        record.status = TaskStatus.FAILED
        record.error = "Completion gate rejected the task: " + ", ".join(gate["failures"])
        record.completed_at = time.time()
        record.add_evidence(
            "completion_gate", "failed",
            "Task completion was rejected because required verification evidence is missing or failed.",
            details=gate,
        )
        await record.event_queue.put(json.dumps({
            "task_id": record.task_id,
            "event_type": "error",
            "text": record.error,
            "timestamp": record.completed_at,
            "data": {"phase": "completion_gate", **gate},
        }))
        return False

    record.status = TaskStatus.COMPLETED
    record.completed_at = time.time()
    record.add_evidence(
        "completion_gate", "passed",
        "All verification checks required by this task have passing evidence.",
        details=gate,
    )
    await record.event_queue.put(terminal_event.model_dump_json())
    return True


async def _run_task_pipeline(record: TaskRecord, adapter):
    # Status is authoritative: a provider stream must never resurrect a cancelled
    # task or leave a task permanently RUNNING when it closes without a terminal event.
    if record.status == TaskStatus.CANCELLED:
        await record.event_queue.put(None)
        return

    # SQLite's atomic lease claim prevents two backend processes from executing the
    # same durable task ID concurrently. A duplicate worker must not overwrite the
    # active worker's persisted task snapshot.
    lease_owner = uuid.uuid4().hex
    claimed = await asyncio.to_thread(claim_task_lease, record.task_id, lease_owner)
    if not claimed:
        record.error = "Another backend worker currently owns this task execution lease."
        record._status = TaskStatus.FAILED
        await record.event_queue.queue.put(None)
        return

    async def renew_lease():
        while True:
            await asyncio.sleep(20)
            renewed = await asyncio.to_thread(renew_task_lease, record.task_id, lease_owner)
            if not renewed:
                # Do not silently claim that this worker still owns the execution slot.
                record.error = "Task execution lease was lost; another worker may have recovered it."
                record._status = TaskStatus.FAILED
                return

    lease_heartbeat = asyncio.create_task(renew_lease(), name=f"task-lease-{record.task_id}")
    record.status = TaskStatus.RUNNING
    record.started_at = time.time()
    if record.request.acceptance_criteria:
        criteria_text = "\n".join(f"- {criterion}" for criterion in record.request.acceptance_criteria)
        record.request.prompt += (
            "\n\nEXPLICIT ACCEPTANCE CRITERIA (all must pass):\n"
            + criteria_text
            + "\nImplement these criteria and ensure automated tests/build checks validate them. Do not claim completion if any criterion is unmet."
        )
        await record.event_queue.put(json.dumps({
            "task_id": record.task_id,
            "event_type": "log",
            "text": f"Loaded {len(record.request.acceptance_criteria)} explicit acceptance criterion/criteria; automated verification is required.",
            "timestamp": time.time(),
            "data": {"phase": "acceptance_criteria", "count": len(record.request.acceptance_criteria)},
        }))
    timeout_seconds = _task_timeout_seconds()
    deadline = time.monotonic() + timeout_seconds
    terminal_event_seen = False
    try:
        if record.request.github_research:
            research_event = {
                "task_id": record.task_id,
                "event_type": "log",
                "text": "Evaluating whether GitHub references could improve this task…",
                "timestamp": time.time(),
                "data": {"phase": "github_research", "status": "started"},
            }
            await record.event_queue.put(json.dumps(research_event))
            try:
                research = await _await_task_deadline(
                    research_github(record.request.prompt, record.request.workspace_path, clone_best=True),
                    deadline, timeout_seconds,
                )
                status_text = research.get("status", "unavailable")
                repos = research.get("repositories", [])
                if status_text == "complete" and repos:
                    names = ", ".join(item.get("full_name", "") for item in repos[:3])
                    research_event = {
                        "task_id": record.task_id, "event_type": "log",
                        "text": f"GitHub research found {len(repos)} relevant reference(s): {names}. " + ("A permissively licensed reference was cloned for inspection." if any(item.get("cloned") for item in repos) else "No reference was cloned; inspect licenses and compatibility before reuse."),
                        "timestamp": time.time(),
                        "data": {"phase": "github_research", "status": status_text, "query": research.get("query"), "repositories": repos, "cache_hit": research.get("cache_hit", False)},
                    }
                    record.request.prompt = record.request.prompt + "\n\n" + research.get("injected_context", "")
                elif status_text == "skipped":
                    research_event = {
                        "task_id": record.task_id, "event_type": "log",
                        "text": "GitHub research skipped: " + str(research.get("reason", "task classified as trivial")) + ".",
                        "timestamp": time.time(),
                        "data": {"phase": "github_research", "status": "skipped", "reason": research.get("reason")},
                    }
                else:
                    research_event = {
                        "task_id": record.task_id, "event_type": "log",
                        "text": "GitHub research unavailable; continuing with the current codebase. " + str(research.get("reason", "")),
                        "timestamp": time.time(),
                        "data": {"phase": "github_research", "status": "unavailable", "query": research.get("query")},
                    }
                await record.event_queue.put(json.dumps(research_event))
            except TaskExecutionTimeout:
                raise
            except Exception as research_error:
                await record.event_queue.put(json.dumps({
                    "task_id": record.task_id, "event_type": "log",
                    "text": "GitHub research failed safely; continuing without external references: " + str(research_error)[:240],
                    "timestamp": time.time(), "data": {"phase": "github_research", "status": "error"},
                }))
        project_rules = _load_project_rules(record.request.workspace_path)
        if project_rules:
            record.request.prompt += (
                "\\n\\nPROJECT-SPECIFIC INSTRUCTIONS (from .code-conductor/rules.md):\\n"
                + project_rules
            )
            await record.event_queue.put(json.dumps({
                "task_id": record.task_id, "event_type": "log",
                "text": "Loaded workspace-specific project rules from .code-conductor/rules.md.",
                "timestamp": time.time(),
                "data": {"phase": "project_context", "status": "loaded", "characters": len(project_rules)},
            }))

        verification_attempt = 0
        browser_verification_attempts = 0
        while True:
            terminal_event = None
            async for event in _stream_task_events_with_deadline(adapter, record, deadline, timeout_seconds):
                if event.event_type == EventType.COMPLETED:
                    # Delay success until requested verification has completed.
                    terminal_event = event
                    continue
                payload = event.model_dump_json()
                await record.event_queue.put(payload)
                if event.event_type in (EventType.CANCELLED, EventType.ERROR):
                    terminal_event_seen = True
                    if record.status != TaskStatus.CANCELLED:
                        if event.event_type == EventType.CANCELLED:
                            record.status = TaskStatus.CANCELLED
                        else:
                            record.status = TaskStatus.FAILED
                            record.error = event.text or "Agent reported an execution error."
                    record.completed_at = time.time()
                    break

            if record.status in (TaskStatus.CANCELLED, TaskStatus.FAILED):
                break
            if terminal_event is None:
                break

            if not (record.request.auto_verify or record.request.acceptance_criteria):
                browser_action, browser_verification_attempts = await _await_task_deadline(
                    _browser_verification_gate(record, browser_verification_attempts),
                    deadline, timeout_seconds,
                )
                if browser_action == "retry":
                    continue
                if browser_action == "failed":
                    terminal_event_seen = True
                    break
                await _finalize_task_completion(record, terminal_event)
                terminal_event_seen = True
                break

            runner = SafeTestRunner(record.request.workspace_path)
            command_key = runner.discover_runner()
            if command_key is None:
                record.verification_status = "failed"
                record.status = TaskStatus.FAILED
                record.error = (
                    "Auto Verify could not establish completion: no supported test or build "
                    "command was detected. Add a test/build script or configure a supported "
                    "runner, then retry the task."
                )
                record.completed_at = time.time()
                record.add_evidence(
                    "automated_tests", "failed",
                    record.error,
                    details={
                        "reason": "no_supported_test_command",
                        "required": True,
                        "verification_status": "failed",
                    },
                )
                await record.event_queue.put(json.dumps({
                    "task_id": record.task_id, "event_type": "error",
                    "text": record.error,
                    "timestamp": record.completed_at,
                    "data": {
                        "phase": "verification",
                        "status": "failed",
                        "reason": "no_supported_test_command",
                    },
                }))
                terminal_event_seen = True
                break

            verification_attempt += 1
            result = await _await_task_deadline(
                runner.run_tests(command_key=command_key, timeout_seconds=120),
                deadline, timeout_seconds,
            )
            # Cancellation can arrive while the test subprocess is running. Do not
            # emit stale verification evidence or let this pipeline overwrite it.
            if record.status == TaskStatus.CANCELLED:
                break
            check_kind = "build" if command_key == "npm_build" else "automated_tests"
            record.verification_status = "passed" if result.passed else "failed"
            record.add_evidence(
                check_kind,
                "passed" if result.passed else "failed",
                f"{'Build' if check_kind == 'build' else command_key} {'passed' if result.passed else 'failed'} with exit code {result.exit_code}.",
                details={
                    "command": result.command,
                    "execution_mode": result.execution_mode,
                    "exit_code": result.exit_code,
                    "duration_ms": result.duration_ms,
                    "stdout_tail": result.stdout[-12000:],
                    "stderr_tail": result.stderr[-12000:],
                },
            )
            verification_result = result
            verification_command = command_key
            build_key = runner.discover_build_runner() if command_key != "npm_build" else None
            if result.passed and build_key:
                build_result = await _await_task_deadline(
                    runner.run_tests(command_key=build_key, timeout_seconds=120),
                    deadline, timeout_seconds,
                )
                if record.status == TaskStatus.CANCELLED:
                    break
                record.add_evidence(
                    "build",
                    "passed" if build_result.passed else "failed",
                    f"Project build {'passed' if build_result.passed else 'failed'} with exit code {build_result.exit_code}.",
                    details={
                        "command": build_result.command,
                        "execution_mode": build_result.execution_mode,
                        "exit_code": build_result.exit_code,
                        "duration_ms": build_result.duration_ms,
                        "stdout_tail": build_result.stdout[-12000:],
                        "stderr_tail": build_result.stderr[-12000:],
                    },
                )
                await record.event_queue.put(json.dumps({
                    "task_id": record.task_id,
                    "event_type": "log",
                    "text": f"Build verification: {'passed' if build_result.passed else 'failed'} (exit {build_result.exit_code}).",
                    "timestamp": time.time(),
                    "data": {
                        "phase": "build_verification",
                        "status": "passed" if build_result.passed else "failed",
                        "command": build_result.command,
                        "exit_code": build_result.exit_code,
                        "duration_ms": build_result.duration_ms,
                    },
                }))
                if not build_result.passed:
                    verification_result = build_result
                    verification_command = build_key
            verification_passed = result.passed and verification_result.passed
            record.verification_status = "passed" if verification_passed else "failed"
            await record.event_queue.put(json.dumps({
                "task_id": record.task_id, "event_type": "log",
                "text": f"Verification attempt {verification_attempt}/{record.request.verification_max_attempts}: {command_key} {'passed' if result.passed else 'failed'} (exit {result.exit_code}).",
                "timestamp": time.time(),
                "data": {
                    "phase": "verification",
                    "status": "passed" if result.passed else "failed",
                    "attempt": verification_attempt,
                    "max_attempts": record.request.verification_max_attempts,
                    "command": result.command,
                    "exit_code": result.exit_code,
                    "duration_ms": result.duration_ms,
                    "stdout": result.stdout[-12000:],
                    "stderr": result.stderr[-12000:],
                },
            }))
            if verification_passed:
                browser_action, browser_verification_attempts = await _await_task_deadline(
                    _browser_verification_gate(record, browser_verification_attempts),
                    deadline, timeout_seconds,
                )
                if browser_action == "retry":
                    continue
                if browser_action == "failed":
                    terminal_event_seen = True
                    break
                await _finalize_task_completion(record, terminal_event)
                terminal_event_seen = True
                break

            if verification_attempt >= record.request.verification_max_attempts:
                record.status = TaskStatus.FAILED
                record.error = f"Verification failed after {verification_attempt} attempt(s): {verification_command} exited {verification_result.exit_code}."
                record.completed_at = time.time()
                await record.event_queue.put(json.dumps({
                    "task_id": record.task_id, "event_type": "error",
                    "text": record.error,
                    "timestamp": record.completed_at,
                    "data": {"phase": "verification", "command": verification_result.command, "exit_code": verification_result.exit_code},
                }))
                terminal_event_seen = True
                break

            # Feed bounded diagnostics back to the same agent for a repair attempt.
            diagnostics = (verification_result.stdout + "\n" + verification_result.stderr)[-12000:]
            record.request.prompt += (
                "\n\nAUTOMATED VERIFICATION FAILED. Fix the underlying issue, do not hide or "
                "delete tests. Then summarize the repair.\n"
                f"Command: {verification_result.command}\nExit code: {verification_result.exit_code}\n"
                f"Diagnostics (truncated):\n{diagnostics}"
            )
            await record.event_queue.put(json.dumps({
                "task_id": record.task_id, "event_type": "log",
                "text": "Sending verification diagnostics to the agent for a repair attempt.",
                "timestamp": time.time(),
                "data": {"phase": "verification", "status": "repair_requested", "attempt": verification_attempt},
            }))
    except asyncio.CancelledError:
        record.status = TaskStatus.CANCELLED
        record.completed_at = time.time()
        raise
    except TaskExecutionTimeout as e:
        record.status = TaskStatus.FAILED
        record.error = str(e)[:1000]
        record.completed_at = time.time()
        record.add_evidence(
            "execution_timeout", "failed", record.error,
            details={"timeout_seconds": timeout_seconds},
        )
        await record.event_queue.put(json.dumps({
            "task_id": record.task_id,
            "event_type": "error",
            "text": record.error,
            "timestamp": record.completed_at,
            "data": {"reason": "task_timeout", "timeout_seconds": timeout_seconds},
        }))
    except Exception as e:
        if record.status != TaskStatus.CANCELLED:
            record.status = TaskStatus.FAILED
            record.error = str(e)[:1000]
        record.completed_at = time.time()
        err_event = {
            "task_id": record.task_id,
            "event_type": "error",
            "text": f"Execution error: {str(e)[:1000]}",
            "timestamp": time.time(),
            "data": {},
        }
        await record.event_queue.put(json.dumps(err_event))
    finally:
        if record.status == TaskStatus.RUNNING:
            # A provider that ends its stream without completed/error is not success.
            record.status = TaskStatus.FAILED
            record.error = "Agent stream ended without a terminal event."
            record.completed_at = time.time()
            await record.event_queue.put(json.dumps({
                "task_id": record.task_id,
                "event_type": "error",
                "text": record.error,
                "timestamp": record.completed_at,
                "data": {"reason": "missing_terminal_event"},
            }))
        elif record.completed_at is None and record.status in (
            TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED
        ):
            record.completed_at = time.time()
        if record.status == TaskStatus.COMPLETED and not any(
            item.get("kind") == "security_scan" for item in record.evidence
        ):
            try:
                scan_result = await asyncio.to_thread(
                    scan_recent_secrets, record.request.workspace_path, record.started_at
                )
                scan_status = scan_result.get("status", "skipped")
                if scan_status == "warning":
                    record.verification_status = "needs_attention"
                    scan_summary = "High-confidence secret patterns were found in files changed during this task; review before sharing."
                elif scan_status == "passed":
                    scan_summary = "No high-confidence secret patterns were found in the bounded scan of recent text changes."
                else:
                    scan_summary = "Secret scan skipped because no recent text-file changes were detected."
                record.add_evidence(
                    "security_scan",
                    scan_status,
                    scan_summary,
                    details=scan_result,
                )
            except Exception as scan_error:
                record.verification_status = "needs_attention"
                record.add_evidence(
                    "security_scan",
                    "error",
                    "The secret scan could not complete; manual review is needed.",
                    details={"error": str(scan_error)[:500]},
                )
        if not any(item.get("kind") == "workspace_provenance" for item in record.evidence):
            try:
                provenance = await asyncio.to_thread(
                    collect_workspace_provenance, record.request.workspace_path
                )
            except Exception as provenance_error:
                provenance = {
                    "status": "error",
                    "snapshot_complete": False,
                    "reason": str(provenance_error)[:200],
                }
            provenance_status = str(provenance.get("status") or "error")
            provenance_summary = (
                "Captured a bounded Git revision and working-tree fingerprint."
                if provenance_status == "captured"
                else "Workspace provenance is partial or unavailable; do not treat it as a complete source snapshot."
            )
            record.add_evidence(
                "workspace_provenance", provenance_status, provenance_summary,
                details=provenance,
            )
        provenance_evidence = next(
            (item for item in reversed(record.evidence) if item.get("kind") == "workspace_provenance"),
            {},
        )
        provenance_details = provenance_evidence.get("details") or {}
        # Tie the completion decision to the final workspace snapshot captured after
        # verification. This is an integrity fingerprint, not proof of code correctness.
        completion_gate_evidence = next(
            (item for item in reversed(record.evidence) if item.get("kind") == "completion_gate"),
            None,
        )
        if completion_gate_evidence is not None:
            gate_details = completion_gate_evidence.setdefault("details", {})
            gate_details["workspace_snapshot_sha256"] = provenance_details.get("snapshot_sha256")
            gate_details["workspace_snapshot_complete"] = provenance_details.get("snapshot_complete", False)
            gate_details["workspace_head_commit"] = provenance_details.get("head_commit")
        if record.status == TaskStatus.COMPLETED and not any(
            item.get("kind") == "task_completion" for item in record.evidence
        ):
            record.add_evidence(
                "task_completion",
                "completed",
                "Agent execution reached a terminal completion event.",
                details={
                    "agent_id": record.agent_id,
                    "verification_status": record.verification_status,
                    "verification_requested": bool(
                        record.request.auto_verify or record.request.acceptance_criteria or record.request.browser_verify_url
                    ),
                    "retry_of": record.request.retry_of,
                    "workspace_provenance_status": provenance_evidence.get("status"),
                    "workspace_head_commit": provenance_details.get("head_commit"),
                    "workspace_snapshot_sha256": provenance_details.get("snapshot_sha256"),
                    "workspace_snapshot_complete": provenance_details.get("snapshot_complete", False),
                },
            )
        record._persist()
        # Signal stream termination
        await record.event_queue.put(None)
        lease_heartbeat.cancel()
        await asyncio.gather(lease_heartbeat, return_exceptions=True)
        await asyncio.to_thread(release_task_lease, record.task_id, lease_owner)

@app.post("/scheduler/pause")
async def pause_scheduled_dispatch(_: str = Depends(require_bearer_token)):
    set_scheduler_paused(True)
    return {
        "paused": True,
        "message": "New scheduled runs are paused. Already-dispatched tasks continue.",
    }


@app.post("/scheduler/resume")
async def resume_scheduled_dispatch(_: str = Depends(require_bearer_token)):
    set_scheduler_paused(False)
    return {"paused": False, "message": "Scheduled dispatch is resumed."}


@app.get("/schedules", response_model=list[ScheduleResponse])
async def get_schedules(limit: int = Query(default=100, ge=1, le=200), _: str = Depends(require_bearer_token)):
    """List durable recurring workflow schedules and their latest status."""
    return list_schedules(limit)


@app.post("/schedules", response_model=ScheduleResponse)
async def create_scheduled_workflow(req: ScheduleCreateRequest, _: str = Depends(require_bearer_token)):
    """Schedule a saved workflow to run repeatedly on this local machine."""
    workflow = next((item for item in list_workflows(200) if item["workflow_id"] == req.workflow_id), None)
    if not workflow:
        raise HTTPException(status_code=404, detail="Saved workflow not found.")
    workspace_path = ""
    if req.workspace_path:
        try:
            workspace_path = str(validate_workspace_path(
                req.workspace_path, default_root=get_settings().workspace_root
            ))
        except WorkspaceValidationError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid workspace path: {str(exc)}")
    workflow_snapshot = {
        "prompt": workflow["prompt"],
        "agent_id": workflow["agent_id"],
        "auto_verify": bool(workflow.get("auto_verify", False)),
        "acceptance_criteria": workflow.get("acceptance_criteria") or [],
        "browser_verify_url": workflow.get("browser_verify_url") or "",
        "browser_required_text": workflow.get("browser_required_text") or [],
        "browser_forbidden_text": workflow.get("browser_forbidden_text") or [],
        "browser_visible_selectors": workflow.get("browser_visible_selectors") or [],
        "browser_hidden_selectors": workflow.get("browser_hidden_selectors") or [],
        "browser_steps": workflow.get("browser_steps") or [],
        "workspace_path": workspace_path,
    }
    now = time.time()
    cron_expression = (req.cron_expression or "").strip()
    schedule_type = "cron" if cron_expression else "interval"
    try:
        next_run_at = (
            next_cron_occurrence(cron_expression, req.timezone, now)
            if schedule_type == "cron"
            else now + req.interval_minutes * 60
        )
    except CronExpressionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    item = create_schedule({
        "schedule_id": uuid.uuid4().hex,
        "workflow_id": req.workflow_id,
        "workflow": workflow_snapshot,
        "name": workflow["name"],
        "workspace_path": workspace_path,
        "interval_minutes": req.interval_minutes,
        "schedule_type": schedule_type,
        "cron_expression": cron_expression,
        "timezone": req.timezone,
        "misfire_policy": req.misfire_policy,
        "enabled": req.enabled,
        "next_run_at": next_run_at,
        "max_retries": req.max_retries,
        "created_at": now,
        "updated_at": now,
    })
    return item


@app.put("/schedules/{schedule_id}", response_model=ScheduleResponse)
async def set_scheduled_workflow(schedule_id: str, req: ScheduleUpdateRequest, _: str = Depends(require_bearer_token)):
    """Pause or resume a schedule without deleting its run history."""
    item = get_schedule(schedule_id)
    if not item:
        raise HTTPException(status_code=404, detail="Schedule not found.")
    next_run = None
    if req.enabled:
        now = time.time()
        try:
            next_run = (
                next_cron_occurrence(item["cron_expression"], item["timezone"], now)
                if item.get("schedule_type") == "cron"
                else now + int(item["interval_minutes"]) * 60
            )
        except CronExpressionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    update_schedule(schedule_id, enabled=req.enabled, next_run_at=next_run)
    return get_schedule(schedule_id)


@app.post("/schedules/{schedule_id}/run-now", response_model=ScheduleRunResponse)
async def run_scheduled_workflow_now(schedule_id: str, _: str = Depends(require_bearer_token)):
    """Queue one durable immediate run without changing the recurring schedule cadence."""
    try:
        run, active_conflict = queue_schedule_run_now(schedule_id)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    if active_conflict:
        raise HTTPException(status_code=409, detail="This schedule already has a queued or running execution.")
    if run is None:
        raise HTTPException(status_code=404, detail="Schedule not found.")
    return run


@app.delete("/schedules/{schedule_id}")
async def remove_scheduled_workflow(schedule_id: str, _: str = Depends(require_bearer_token)):
    if not delete_schedule(schedule_id):
        raise HTTPException(status_code=404, detail="Schedule not found.")
    return {"schedule_id": schedule_id, "deleted": True}


@app.get("/schedules/{schedule_id}/runs", response_model=list[ScheduleRunResponse])
async def get_scheduled_workflow_runs(
    schedule_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    _: str = Depends(require_bearer_token),
):
    return list_schedule_runs(schedule_id, limit)


@app.get("/workflows", response_model=list[WorkflowPresetResponse])
async def get_workflows(limit: int = Query(default=100, ge=1, le=200), _: str = Depends(require_bearer_token)):
    """List locally stored reusable task workflows."""
    return list_workflows(limit)


@app.post("/workflows", response_model=WorkflowPresetResponse)
async def create_workflow(req: WorkflowPresetRequest, _: str = Depends(require_bearer_token)):
    """Save a reusable prompt and its execution settings locally."""
    name = req.name.strip()
    prompt_text = req.prompt.strip()
    if not name or not prompt_text:
        raise HTTPException(status_code=422, detail="Workflow name and prompt must not be blank.")
    if req.browser_verify_url:
        parsed_url = urlsplit(req.browser_verify_url)
        if (
            parsed_url.scheme not in ("http", "https")
            or not parsed_url.hostname
            or parsed_url.username is not None
            or parsed_url.password is not None
        ):
            raise HTTPException(
                status_code=422,
                detail="Browser verification URL must be HTTP(S), include a host, and contain no credentials.",
            )
    now = time.time()
    item = {
        "workflow_id": uuid.uuid4().hex,
        "name": name,
        "prompt": prompt_text,
        "agent_id": req.agent_id.strip() or "auto",
        "auto_verify": req.auto_verify,
        "acceptance_criteria": req.acceptance_criteria,
        "browser_verify_url": req.browser_verify_url or "",
        "browser_required_text": req.browser_required_text,
        "browser_forbidden_text": req.browser_forbidden_text,
        "browser_visible_selectors": req.browser_visible_selectors,
        "browser_hidden_selectors": req.browser_hidden_selectors,
        "browser_steps": [step.model_dump() for step in req.browser_steps],
        "created_at": now,
        "updated_at": now,
    }
    return save_workflow(item)


@app.delete("/workflows/{workflow_id}")
async def remove_workflow(workflow_id: str, _: str = Depends(require_bearer_token)):
    if not delete_workflow(workflow_id):
        raise HTTPException(status_code=404, detail="Workflow not found.")
    return {"workflow_id": workflow_id, "deleted": True}


@app.get("/tasks")
async def list_tasks(limit: int = Query(default=100, ge=1, le=500), _: str = Depends(require_bearer_token)):
    """List persisted task snapshots, newest first."""
    # Restore lazily for ASGI test clients that do not run lifespan hooks.
    if not _tasks:
        await _restore_task_records()
    return [item.snapshot() for item in sorted(_tasks.values(), key=lambda t: t.created_at, reverse=True)[:limit]]


@app.get("/tasks/{task_id}")
async def get_task_status(task_id: str, _: str = Depends(require_bearer_token)):
    """Return authoritative task state so clients can recover after losing an SSE stream."""
    record = _tasks.get(task_id)
    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task '{task_id}' not found.",
        )
    return record.snapshot()


@app.post("/tasks/{task_id}/retry", response_model=TaskResponse)
async def retry_task(task_id: str, _: str = Depends(require_bearer_token)):
    """Create a new task from the immutable original request of a failed/cancelled task."""
    record = _tasks.get(task_id)
    if record is None:
        await _restore_task_records(task_id=task_id)
        record = _tasks.get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found.")
    if record.status not in (TaskStatus.FAILED, TaskStatus.CANCELLED):
        raise HTTPException(
            status_code=409,
            detail="Only failed or cancelled tasks can be retried.",
        )
    if not record.original_request_available or not record.initial_request_json:
        raise HTTPException(
            status_code=409,
            detail="The original request was not preserved for this older task. Resubmit the prompt instead of retrying modified diagnostics.",
        )
    try:
        original_payload = json.loads(record.initial_request_json)
        request = TaskCreateRequest.model_validate(original_payload)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=409, detail="The original task request cannot be restored safely.") from exc

    # A retry is a new execution with explicit lineage, never a replay of the old
    # idempotency key. Keep the original prompt/configuration rather than any
    # diagnostics appended to the mutable in-flight request.
    request.retry_of = task_id
    request.idempotency_key = f"retry:{task_id}:{uuid.uuid4().hex}"
    return await create_task(request, "retry")


@app.get("/tasks/{task_id}/evidence")
async def get_task_evidence(task_id: str, _: str = Depends(require_bearer_token)):
    """Return the machine-readable evidence attached to a task."""
    record = _tasks.get(task_id)
    if not record:
        # Restore persisted history for clients that reconnect after a backend restart.
        await _restore_task_records()
        record = _tasks.get(task_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found.")
    return {
        "task_id": record.task_id,
        "task_status": record.status.value,
        "verification_status": record.verification_status,
        "evidence": record.evidence,
        "manifest_path": f".code-conductor/artifacts/{record.task_id}/evidence.json",
    }


@app.get("/tasks/{task_id}/artifacts/browser-verification.png")
async def get_browser_verification_artifact(task_id: str, _: str = Depends(require_bearer_token)):
    """Serve a task's browser screenshot only from its validated workspace artifact directory."""
    record = _tasks.get(task_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found.")
    root = _resolve_validated_ws(record.request.workspace_path)
    target = root / ".code-conductor" / "artifacts" / record.task_id / "browser-verification.png"
    resolved = target.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise HTTPException(status_code=403, detail="Artifact path escapes the selected workspace.")
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="Browser verification screenshot is not available.")
    return FileResponse(
        path=resolved,
        media_type="image/png",
        filename="browser-verification.png",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/tasks/{task_id}/event-history")
async def get_task_event_history(
    task_id: str,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=500, ge=1, le=1000),
    _: str = Depends(require_bearer_token),
):
    """Return persisted task events as JSON so completed history can be rehydrated."""
    if task_id not in _tasks:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found.")
    result = []
    rows = events_after(task_id, after_event_id=after, limit=limit)
    for row in rows:
        try:
            payload = json.loads(row["payload"])
            if isinstance(payload, dict):
                payload["event_id"] = int(row["event_id"])
                result.append(payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    return {
        "task_id": task_id,
        "events": result,
        "next_cursor": result[-1]["event_id"] if result else after,
    }


@app.get("/tasks/{task_id}/events")
async def stream_task_events(
    task_id: str,
    after: int = Query(default=0, ge=0, description="Replay events after this event ID"),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    _: str = Depends(require_bearer_token),
):
    record = _tasks.get(task_id)
    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task '{task_id}' not found.",
        )

    cursor = after
    if last_event_id:
        try:
            cursor = max(cursor, int(last_event_id))
        except ValueError:
            raise HTTPException(status_code=400, detail="Last-Event-ID must be an integer.")

    async def event_generator() -> AsyncIterator[dict]:
        event_cursor = cursor
        terminal_idle_polls = 0
        while True:
            batch = events_after(task_id, event_cursor, limit=200)
            for item in batch:
                event_cursor = int(item["event_id"])
                yield {"id": str(event_cursor), "event": "message", "data": item["payload"]}
            if batch:
                terminal_idle_polls = 0
                continue

            current = _tasks.get(task_id)
            terminal = current is not None and current.status in (
                TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED
            )
            if terminal:
                # Allow a final terminal/error event to be committed after its status update.
                terminal_idle_polls += 1
                if terminal_idle_polls >= 2:
                    yield {"event": "done", "data": "[DONE]"}
                    break
            await asyncio.sleep(0.2)

    return EventSourceResponse(event_generator())

@app.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, _: str = Depends(require_bearer_token)):
    record = _tasks.get(task_id)
    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task '{task_id}' not found.",
        )

    if record.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
        return {"task_id": task_id, "cancelled": False, "status": record.status}

    registry = get_agent_registry()
    adapter = registry.get(record.agent_id)
    cancelled = False
    if adapter:
        cancelled = await adapter.cancel_task(task_id)

    # Cancellation is final even if the provider emits a late completion event.
    record.status = TaskStatus.CANCELLED
    record.completed_at = time.time()
    await record.event_queue.put(json.dumps({
        "task_id": task_id,
        "event_type": "cancelled",
        "text": "Task cancelled by user.",
        "timestamp": record.completed_at,
        "data": {},
    }))
    record._persist()
    return {"task_id": task_id, "cancelled": cancelled, "status": record.status}

# ---------------------------------------------------------
# Workspace Security, Git Guard, and Test Runner (Milestone 7)
# ---------------------------------------------------------

def _project_rules_path(workspace_root: Path) -> Path:
    """Resolve workspace-local agent instructions without allowing symlink escape."""
    root = workspace_root.resolve()
    rules_path = root / ".code-conductor" / "rules.md"
    resolved = rules_path.resolve()
    if not resolved.is_relative_to(root):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Project rules path escapes workspace.")
    return rules_path


def _load_project_rules(workspace_root: str | Path) -> str:
    try:
        path = _project_rules_path(Path(workspace_root))
        if not path.is_file() or path.is_symlink():
            return ""
        # Keep prompt injection bounded and avoid reading unbounded files into agent context.
        with path.open("r", encoding="utf-8") as handle:
            return handle.read(20_001)[:20_000].strip()
    except (OSError, UnicodeError, HTTPException):
        return ""


@app.get("/workspace/rules")
async def get_project_rules(workspace_path: str | None = None, _: str = Depends(require_bearer_token)):
    root = _resolve_validated_ws(workspace_path)
    path = _project_rules_path(root)
    content = _load_project_rules(root)
    return {"workspace_path": str(root), "path": str(path), "content": content, "exists": path.is_file()}


class ProjectRulesRequest(BaseModel):
    content: str
    workspace_path: str | None = None


@app.put("/workspace/rules")
async def save_project_rules(req: ProjectRulesRequest, _: str = Depends(require_bearer_token)):
    root = _resolve_validated_ws(req.workspace_path)
    if len(req.content) > 20_000:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Project rules are limited to 20,000 characters.")
    path = _project_rules_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Re-check containment after directory creation to defend against unexpected symlink changes.
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Project rules path escapes workspace.")
    temp_path = path.with_suffix(".md.tmp")
    temp_path.write_text(req.content, encoding="utf-8")
    temp_path.replace(path)
    return {"success": True, "workspace_path": str(root), "path": str(path), "characters": len(req.content)}


@app.delete("/workspace/rules")
async def delete_project_rules(workspace_path: str | None = None, _: str = Depends(require_bearer_token)):
    root = _resolve_validated_ws(workspace_path)
    path = _project_rules_path(root)
    try:
        path.unlink()
        return {"success": True, "deleted": True}
    except FileNotFoundError:
        return {"success": True, "deleted": False}


def _resolve_validated_ws(workspace_path: str | None = None) -> Path:
    try:
        return validate_workspace_path(
            workspace_path,
            default_root=get_settings().workspace_root,
        )
    except WorkspaceValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid workspace path: {str(e)}",
        )

def _get_sec_manager(ws: str | None = None) -> WorkspaceSecurityManager:
    return WorkspaceSecurityManager(_resolve_validated_ws(ws))

def _get_git_guard(ws: str | None = None) -> GitGuard:
    return GitGuard(_resolve_validated_ws(ws))

def _get_test_runner(ws: str | None = None) -> SafeTestRunner:
    return SafeTestRunner(_resolve_validated_ws(ws))

@app.post("/workspace/file/read", response_model=FileReadResponse)
async def read_workspace_file(req: FileReadRequest, _: str = Depends(require_bearer_token)):
    mgr = _get_sec_manager(req.workspace_path)
    try:
        content = mgr.read_file(req.path)
        return FileReadResponse(
            path=req.path,
            content=content,
            size_bytes=len(content.encode("utf-8")),
        )
    except (PathTraversalError, SensitiveFileBlockedError) as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

@app.post("/workspace/file/write", response_model=FileWriteResponse)
async def write_workspace_file(req: FileWriteRequest, _: str = Depends(require_bearer_token)):
    mgr = _get_sec_manager(req.workspace_path)
    try:
        bytes_written, diff = mgr.write_file(req.path, req.content, atomic=req.atomic)
        return FileWriteResponse(path=req.path, bytes_written=bytes_written, diff=diff)
    except (PathTraversalError, SensitiveFileBlockedError) as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

@app.get("/workspace/files", response_model=SafeFileListResponse)
async def list_workspace_files(subpath: str = "", workspace_path: str | None = None, _: str = Depends(require_bearer_token)):
    mgr = _get_sec_manager(workspace_path)
    try:
        files = mgr.list_safe_files(subpath=subpath)
        return SafeFileListResponse(root=str(mgr.workspace_root), files=files, total_count=len(files))
    except (PathTraversalError, SensitiveFileBlockedError) as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))

@app.get("/workspace/git/status", response_model=GitStatusResponse)
async def get_git_status(workspace_path: str | None = None, _: str = Depends(require_bearer_token)):
    guard = _get_git_guard(workspace_path)
    return await guard.get_status()

@app.post("/workspace/git/diff", response_model=GitDiffResponse)
async def get_git_diff(req: GitDiffRequest, _: str = Depends(require_bearer_token)):
    guard = _get_git_guard(req.workspace_path)
    return await guard.get_diff(file_path=req.file_path, staged=req.staged)

@app.post("/workspace/git/checkpoint", response_model=CheckpointResponse)
async def create_git_checkpoint(req: CheckpointRequest, _: str = Depends(require_bearer_token)):
    guard = _get_git_guard(req.workspace_path)
    return await guard.create_checkpoint(req.name)

@app.post("/workspace/git/restore", response_model=CheckpointResponse)
async def restore_git_checkpoint(req: CheckpointRequest, _: str = Depends(require_bearer_token)):
    if not req.name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Checkpoint identifier required")
    guard = _get_git_guard(req.workspace_path)
    return await guard.restore_checkpoint(req.name, force=req.force)

@app.get("/workspace/test/discover")
async def discover_tests(workspace_path: str | None = None, _: str = Depends(require_bearer_token)):
    """Report a supported test runner detected from workspace metadata."""
    runner = _get_test_runner(workspace_path)
    return {"workspace_path": str(runner.workspace_root), "command_key": runner.discover_runner()}


@app.post("/workspace/test/run", response_model=TestRunResponse)
async def run_tests(req: TestRunRequest, _: str = Depends(require_bearer_token)):
    runner = _get_test_runner(req.workspace_path)
    try:
        return await runner.run_tests(
            command_key=req.command_key,
            extra_args=req.extra_args,
            timeout_seconds=req.timeout_seconds,
        )
    except TestRunnerSecurityError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))

# ---------------------------------------------------------
# Multi-Agent Orchestration & Audit History (Milestone 8)
# ---------------------------------------------------------

@app.post("/orchestrator/plan", response_model=ExecutionPlan)
async def generate_orchestration_plan(req: TaskCreateRequest, _: str = Depends(require_bearer_token)):
    ws = _resolve_validated_ws(req.workspace_path)
    planner = PlanDecomposer(ws)
    return planner.decompose(req.prompt)


@app.get("/orchestrator/history", response_model=list[OrchestrationAuditRecord])
async def list_orchestration_history(limit: int = 50, _: str = Depends(require_bearer_token)):
    tracker = AuditHistoryTracker()
    return tracker.list_records(limit=limit)

@app.get("/orchestrator/history/{task_id}", response_model=OrchestrationAuditRecord)
async def get_orchestration_record(task_id: str, _: str = Depends(require_bearer_token)):
    tracker = AuditHistoryTracker()
    record = tracker.get_record(task_id)
    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Audit record '{task_id}' not found.",
        )
    return record


# ---------------------------------------------------------
# User-configured OpenAI-compatible providers
# ---------------------------------------------------------
class ProviderConfigRequest(BaseModel):
    id: str
    name: str
    base_url: str
    model: str
    api_key: str | None = None


@app.get("/providers")
async def get_providers(_: str = Depends(require_bearer_token)):
    return list_providers()


@app.post("/providers")
async def save_provider(req: ProviderConfigRequest, _: str = Depends(require_bearer_token)):
    try:
        provider = upsert_provider(
            provider_id=req.id, name=req.name, base_url=req.base_url,
            model=req.model, api_key=req.api_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    get_agent_registry().register(CustomProviderAdapter(provider))
    return provider


@app.post("/providers/{provider_id}/test")
async def test_provider(provider_id: str, _: str = Depends(require_bearer_token)):
    provider = next((p for p in list_providers() if p["id"] == provider_id), None)
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found.")
    adapter = CustomProviderAdapter(provider)
    try:
        result = await asyncio.to_thread(adapter._query, "Reply with exactly: OK")
        return {"ok": True, "provider_id": provider_id, "model": provider["model"], "sample": result[:160]}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Provider test failed: {str(exc)[:500]}")


@app.delete("/providers/{provider_id}")
async def remove_provider(provider_id: str, _: str = Depends(require_bearer_token)):
    if not delete_provider(provider_id):
        raise HTTPException(status_code=404, detail="Provider not found.")
    get_agent_registry().unregister("custom-" + provider_id)
    return {"deleted": True, "provider_id": provider_id}


class SmartProviderDetectRequest(BaseModel):
    site: str
    is_free: bool = True
    api_key: str


KNOWN_PROVIDER_RULES = [
    {
        "keys": ["groq", "groq.com", "console.groq.com"],
        "id": "groq",
        "name": "Groq Cloud",
        "base_url": "https://api.groq.com/openai/v1",
        "model_free": "qwen/qwen3.8-27b",
        "model_paid": "llama-3.3-70b-versatile",
    },
    {
        "keys": ["openrouter", "openrouter.ai"],
        "id": "openrouter",
        "name": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "model_free": "meta-llama/llama-3.3-70b-instruct:free",
        "model_paid": "anthropic/claude-3.5-sonnet",
    },
    {
        "keys": ["deepseek", "deepseek.com"],
        "id": "deepseek",
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "model_free": "deepseek-chat",
        "model_paid": "deepseek-chat",
    },
    {
        "keys": ["together", "together.ai", "together.xyz"],
        "id": "together",
        "name": "Together AI",
        "base_url": "https://api.together.xyz/v1",
        "model_free": "meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo",
        "model_paid": "meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo",
    },
    {
        "keys": ["mistral", "mistral.ai"],
        "id": "mistral",
        "name": "Mistral AI",
        "base_url": "https://api.mistral.ai/v1",
        "model_free": "mistral-small-latest",
        "model_paid": "mistral-large-latest",
    },
    {
        "keys": ["cerebras", "cerebras.ai"],
        "id": "cerebras",
        "name": "Cerebras",
        "base_url": "https://api.cerebras.ai/v1",
        "model_free": "llama3.1-70b",
        "model_paid": "llama3.1-70b",
    },
    {
        "keys": ["sambanova", "sambanova.ai"],
        "id": "sambanova",
        "name": "SambaNova",
        "base_url": "https://api.sambanova.ai/v1",
        "model_free": "Meta-Llama-3.1-70B-Instruct",
        "model_paid": "Meta-Llama-3.1-70B-Instruct",
    },
    {
        "keys": ["apmix", "apmix.net"],
        "id": "apmix",
        "name": "APMix Gateway",
        "base_url": "https://api.apmix.net/v1",
        "model_free": "claude-sonnet-4-6-free",
        "model_paid": "claude-sonnet-4-6",
    },
    {
        "keys": ["perplexity", "perplexity.ai"],
        "id": "perplexity",
        "name": "Perplexity",
        "base_url": "https://api.perplexity.ai",
        "model_free": "llama-3.1-sonar-small-128k-online",
        "model_paid": "llama-3.1-sonar-large-128k-online",
    },
    {
        "keys": ["ollama", "localhost:11434"],
        "id": "ollama",
        "name": "Ollama Local",
        "base_url": "http://localhost:11434/v1",
        "model_free": "llama3.2",
        "model_paid": "llama3.2",
    },
]


@app.post("/providers/auto-detect")
async def auto_detect_provider(req: SmartProviderDetectRequest, _: str = Depends(require_bearer_token)):
    """Automatically figure out provider details from site name or URL, tier, and API key."""
    site_clean = req.site.strip().lower()
    matched = None
    for rule in KNOWN_PROVIDER_RULES:
        if any(k in site_clean for k in rule["keys"]):
            matched = rule
            break

    if matched:
        prov_id = matched["id"]
        name = matched["name"]
        base_url = matched["base_url"]
        model = matched["model_free"] if req.is_free else matched["model_paid"]
    else:
        # Generic heuristic from URL or string
        import re
        domain = re.sub(r"^https?://", "", site_clean).split("/")[0].replace("www.", "")
        short_name = domain.split(".")[0] or "custom"
        prov_id = re.sub(r"[^a-z0-9_-]", "", short_name)[:16] or "custom-ai"
        name = short_name.capitalize() + " AI"
        base_url = site_clean if site_clean.startswith("http") else f"https://api.{domain}/v1"
        model = "default-model"

    try:
        provider = upsert_provider(
            provider_id=prov_id,
            name=name,
            base_url=base_url,
            model=model,
            api_key=req.api_key.strip(),
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to configure provider: {exc}")

    get_agent_registry().register(CustomProviderAdapter(provider))
    return {
        "success": True,
        "provider": provider,
        "detected": {
            "name": name,
            "base_url": base_url,
            "model": model,
            "tier": "free" if req.is_free else "paid"
        }
    }


@app.post("/system/shutdown")
async def system_shutdown(_: str = Depends(require_bearer_token)):
    """Cleanly shut down the backend process."""
    import asyncio
    import os
    import signal

    async def _do_exit():
        await asyncio.sleep(0.5)
        # On Windows, kill current process cleanly
        os.kill(os.getpid(), signal.SIGTERM)

    asyncio.create_task(_do_exit())
    return {"shutdown": True, "message": "Code Conductor backend is shutting down"}