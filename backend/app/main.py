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
from fastapi import Depends, FastAPI, HTTPException, status
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
from app.antigravity_history import router as antigravity_history_router



# In-memory task tracker
class TaskRecord:
    def __init__(self, task_id: str, request: TaskCreateRequest, agent_id: str):
        self.task_id = task_id
        self.request = request
        self.agent_id = agent_id
        self.status = TaskStatus.PENDING
        self.created_at = time.time()
        self.event_queue: asyncio.Queue[str | None] = asyncio.Queue()

_tasks: dict[str, TaskRecord] = {}
_start_time = time.time()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    yield
    # Shutdown: cancel all active tasks
    for task in _tasks.values():
        if task.status in (TaskStatus.PENDING, TaskStatus.RUNNING):
            task.status = TaskStatus.CANCELLED
            await task.event_queue.put(None)

app = FastAPI(
    title="Unified AI Coding Hub Backend",
    version=get_settings().version,
    lifespan=lifespan,
)

app.include_router(repository_import_router)
app.include_router(antigravity_history_router)

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
    allow_methods=["GET", "POST", "OPTIONS"],
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
    record.status = TaskStatus.RUNNING
    try:
        async for event in adapter.stream_task(record.task_id, record.request):
            payload = event.model_dump_json()
            await record.event_queue.put(payload)
            if event.event_type == EventType.COMPLETED:
                record.status = TaskStatus.COMPLETED
            elif event.event_type == EventType.CANCELLED:
                record.status = TaskStatus.CANCELLED
            elif event.event_type == EventType.ERROR:
                record.status = TaskStatus.FAILED
    except Exception as e:
        record.status = TaskStatus.FAILED
        err_event = {
            "task_id": record.task_id,
            "event_type": "error",
            "text": f"Execution error: {str(e)}",
            "timestamp": time.time(),
            "data": {},
        }
        await record.event_queue.put(json.dumps(err_event))
    finally:
        # Signal stream termination
        await record.event_queue.put(None)

@app.get("/tasks/{task_id}/events")
async def stream_task_events(
    task_id: str,
    _: str = Depends(require_bearer_token),
):
    record = _tasks.get(task_id)
    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task '{task_id}' not found.",
        )

    async def event_generator() -> AsyncIterator[dict]:
        while True:
            item = await record.event_queue.get()
            if item is None:
                # End of stream event
                yield {"event": "done", "data": "[DONE]"}
                break
            yield {"event": "message", "data": item}

    return EventSourceResponse(event_generator())

@app.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, _: str = Depends(require_bearer_token)):
    record = _tasks.get(task_id)
    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task '{task_id}' not found.",
        )

    registry = get_agent_registry()
    adapter = registry.get(record.agent_id)
    cancelled = False
    if adapter:
        cancelled = await adapter.cancel_task(task_id)

    record.status = TaskStatus.CANCELLED
    return {"task_id": task_id, "cancelled": cancelled, "status": record.status}

# ---------------------------------------------------------
# Workspace Security, Git Guard, and Test Runner (Milestone 7)
# ---------------------------------------------------------

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
