import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from sse_starlette.sse import EventSourceResponse

from app.adapters.registry import get_agent_registry
from app.core.config import get_settings
from app.core.security import require_bearer_token
from app.models.schemas import (
    AgentInfo,
    EventType,
    HealthResponse,
    TaskCreateRequest,
    TaskResponse,
    TaskStatus,
)

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

# Strictly restrict CORS to local origin
app.add_middleware(
    CORSMiddleware,
    allow_origins=["vscode-webview://*", "http://127.0.0.1", "http://localhost"],
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
    registry = get_agent_registry()
    adapter = await registry.resolve_agent(request.agent_id)
    if not adapter:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent '{request.agent_id}' not found.",
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
async def stream_task_events(task_id: str, _: str = Depends(require_bearer_token)):
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
