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
from app.github_research import research_github, should_research
from app.antigravity_history import router as antigravity_history_router
from app.browser_session import router as browser_session_router
from app.guided_workflow import router as guided_workflow_router
from app.api.openai_compat import router as openai_compat_router



# In-memory task tracker
class TaskRecord:
    def __init__(self, task_id: str, request: TaskCreateRequest, agent_id: str):
        self.task_id = task_id
        self.request = request
        self.agent_id = agent_id
        self.status = TaskStatus.PENDING
        self.created_at = time.time()
        self.started_at: float | None = None
        self.completed_at: float | None = None
        self.error: str | None = None
        self.event_queue: asyncio.Queue[str | None] = asyncio.Queue()

    def snapshot(self) -> dict:
        """Return a stable status payload without exposing request secrets."""
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
app.include_router(browser_session_router)
app.include_router(guided_workflow_router)
app.include_router(openai_compat_router)

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
        async for event in adapter.stream_task(record.task_id, record.request):
            payload = event.model_dump_json()
            await record.event_queue.put(payload)
            if event.event_type in (EventType.COMPLETED, EventType.CANCELLED, EventType.ERROR):
                terminal_event_seen = True
                if record.status != TaskStatus.CANCELLED:
                    if event.event_type == EventType.COMPLETED:
                        record.status = TaskStatus.COMPLETED
                    elif event.event_type == EventType.CANCELLED:
                        record.status = TaskStatus.CANCELLED
                    else:
                        record.status = TaskStatus.FAILED
                        record.error = event.text or "Agent reported an execution error."
                record.completed_at = time.time()
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
        # Signal stream termination
        await record.event_queue.put(None)

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
