import asyncio
import json
import sys
import time
import uuid

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import secrets
from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
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
    TaskCreateRequest,
    TaskResponse,
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
from app.orchestrator.history import AuditHistoryTracker
from app.orchestrator.models import ExecutionPlan, OrchestrationAuditRecord
from app.orchestrator.planner import PlanDecomposer
from pathlib import Path
from app.repository_import import router as repository_import_router
from app.github_research import research_github, should_research
from app.antigravity_history import router as antigravity_history_router
from app.browser_session import router as browser_session_router
from app.guided_workflow import router as guided_workflow_router
from app.api.openai_compat import router as openai_compat_router
from app.git_worktrees import router as git_worktrees_router
from app.task_store import append_event, events_after, get_saved_tasks, save_task



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
    ):
        self.task_id = task_id
        self.request = request
        self.agent_id = agent_id
        self._status = TaskStatus.PENDING
        self.created_at = time.time()
        self.started_at: float | None = None
        self.completed_at: float | None = None
        self.error: str | None = None
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
        self._persist()

    @property
    def status(self) -> TaskStatus:
        return self._status

    @status.setter
    def status(self, value: TaskStatus) -> None:
        self._status = value
        if hasattr(self, "event_queue"):
            self._persist()

    def _persist(self) -> None:
        save_task(self.snapshot(), self.request.model_dump_json())

    def snapshot(self) -> dict:
        """Return a stable status payload without exposing provider secrets."""
        return {
            "task_id": self.task_id,
            "status": self.status.value,
            "agent_id": self.agent_id,
            "prompt": self.request.prompt,
            "workspace_path": self.request.workspace_path,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "error": self.error,
        }


_tasks: dict[str, TaskRecord] = {}
_start_time = time.time()


async def _restore_task_records() -> None:
    """Restore history and mark interrupted tasks instead of pretending they resumed."""
    for item in get_saved_tasks(limit=500):
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
            )
        except Exception:
            continue
        _tasks[task_id] = record
        if record.status in (TaskStatus.PENDING, TaskStatus.RUNNING):
            record.error = "Backend restarted while this task was active; execution was not resumed."
            record.status = TaskStatus.FAILED
            record.completed_at = time.time()
            await record.event_queue.put(json.dumps({
                "task_id": task_id,
                "event_type": "error",
                "text": record.error,
                "timestamp": record.completed_at,
                "data": {"reason": "backend_restart", "recoverable": True},
            }))
            record._persist()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _restore_task_records()
    yield
    # Shutdown marks active work as interrupted; it is not silently resumed on restart.
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

@app.post("/tasks", response_model=TaskResponse)
async def create_task(request: TaskCreateRequest, _: str = Depends(require_bearer_token)):
    if request.permission_mode == "god" and not request.god_mode_confirmed:
        raise HTTPException(status_code=400, detail="God Mode requires explicit confirmation in the task request.")
    registry = get_agent_registry()
    adapter = await registry.resolve_agent(request.agent_id, request.prompt)
    if not adapter:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent '{request.agent_id}' could not be resolved or no provider engine is currently active.",
        )

    # Validate workspace path security containment
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

    task_id = f"task-{uuid.uuid4().hex[:8]}"
    record = TaskRecord(task_id=task_id, request=request, agent_id=adapter.agent_id)
    _tasks[task_id] = record

    # Spawn background task processing that feeds the event queue
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

async def _run_task_pipeline(record: TaskRecord, adapter):
    # Status is authoritative: a provider stream must never resurrect a cancelled
    # task or leave a task permanently RUNNING when it closes without a terminal event.
    if record.status == TaskStatus.CANCELLED:
        await record.event_queue.put(None)
        return
    record.status = TaskStatus.RUNNING
    record.started_at = time.time()
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
                research = await research_github(record.request.prompt, record.request.workspace_path, clone_best=True)
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
        while True:
            terminal_event = None
            async for event in adapter.stream_task(record.task_id, record.request):
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

            if not record.request.auto_verify:
                await record.event_queue.put(terminal_event.model_dump_json())
                terminal_event_seen = True
                record.status = TaskStatus.COMPLETED
                record.completed_at = time.time()
                break

            runner = SafeTestRunner(record.request.workspace_path)
            command_key = runner.discover_runner()
            if command_key is None:
                await record.event_queue.put(json.dumps({
                    "task_id": record.task_id, "event_type": "log",
                    "text": "Verification skipped: no supported test command was detected. Enable a test script or configure tests, then rerun with Auto Verify.",
                    "timestamp": time.time(),
                    "data": {"phase": "verification", "status": "skipped"},
                }))
                await record.event_queue.put(terminal_event.model_dump_json())
                terminal_event_seen = True
                record.status = TaskStatus.COMPLETED
                record.completed_at = time.time()
                break

            verification_attempt += 1
            result = await runner.run_tests(command_key=command_key, timeout_seconds=120)
            # Cancellation can arrive while the test subprocess is running. Do not
            # emit a stale verification result or let this pipeline overwrite it.
            if record.status == TaskStatus.CANCELLED:
                break
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
            if result.passed:
                await record.event_queue.put(terminal_event.model_dump_json())
                terminal_event_seen = True
                record.status = TaskStatus.COMPLETED
                record.completed_at = time.time()
                break

            if verification_attempt >= record.request.verification_max_attempts:
                record.status = TaskStatus.FAILED
                record.error = f"Verification failed after {verification_attempt} attempt(s): {command_key} exited {result.exit_code}."
                record.completed_at = time.time()
                await record.event_queue.put(json.dumps({
                    "task_id": record.task_id, "event_type": "error",
                    "text": record.error,
                    "timestamp": record.completed_at,
                    "data": {"phase": "verification", "command": result.command, "exit_code": result.exit_code},
                }))
                terminal_event_seen = True
                break

            # Feed bounded diagnostics back to the same agent for a repair attempt.
            diagnostics = (result.stdout + "\n" + result.stderr)[-12000:]
            record.request.prompt += (
                "\n\nAUTOMATED VERIFICATION FAILED. Fix the underlying issue, do not hide or "
                "delete tests. Then summarize the repair.\n"
                f"Command: {result.command}\nExit code: {result.exit_code}\n"
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
        record._persist()
        # Signal stream termination
        await record.event_queue.put(None)

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
        terminal_idle_polls = 0
        while True:
            batch = events_after(task_id, cursor, limit=200)
            for item in batch:
                cursor = int(item["event_id"])
                yield {"id": str(cursor), "event": "message", "data": item["payload"]}
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
