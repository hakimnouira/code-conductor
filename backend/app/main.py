import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import secrets
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPAuthorizationCredentials
from sse_starlette.sse import EventSourceResponse

from app.adapters.registry import get_agent_registry
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
)
from app.safety.git_guard import GitGuard
from app.safety.test_runner import SafeTestRunner, TestRunnerSecurityError


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
async def stream_task_events(
    task_id: str,
    token: str | None = None,
    auth_header: HTTPAuthorizationCredentials | None = Depends(security_scheme),
):
    settings = get_settings()
    provided_token = auth_header.credentials if auth_header else token
    if not provided_token or not secrets.compare_digest(provided_token, settings.auth_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing bearer token",
        )

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

def _get_sec_manager() -> WorkspaceSecurityManager:
    return WorkspaceSecurityManager(get_settings().workspace_root)

def _get_git_guard() -> GitGuard:
    return GitGuard(get_settings().workspace_root)

def _get_test_runner() -> SafeTestRunner:
    return SafeTestRunner(get_settings().workspace_root)

@app.post("/workspace/file/read", response_model=FileReadResponse)
async def read_workspace_file(req: FileReadRequest, _: str = Depends(require_bearer_token)):
    mgr = _get_sec_manager()
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
    mgr = _get_sec_manager()
    try:
        bytes_written, diff = mgr.write_file(req.path, req.content, atomic=req.atomic)
        return FileWriteResponse(path=req.path, bytes_written=bytes_written, diff=diff)
    except (PathTraversalError, SensitiveFileBlockedError) as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

@app.get("/workspace/files", response_model=SafeFileListResponse)
async def list_workspace_files(subpath: str = "", _: str = Depends(require_bearer_token)):
    mgr = _get_sec_manager()
    try:
        files = mgr.list_safe_files(subpath=subpath)
        return SafeFileListResponse(root=str(mgr.workspace_root), files=files, total_count=len(files))
    except (PathTraversalError, SensitiveFileBlockedError) as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))

@app.get("/workspace/git/status", response_model=GitStatusResponse)
async def get_git_status(_: str = Depends(require_bearer_token)):
    guard = _get_git_guard()
    return await guard.get_status()

@app.post("/workspace/git/diff", response_model=GitDiffResponse)
async def get_git_diff(req: GitDiffRequest, _: str = Depends(require_bearer_token)):
    guard = _get_git_guard()
    return await guard.get_diff(file_path=req.file_path, staged=req.staged)

@app.post("/workspace/git/checkpoint", response_model=CheckpointResponse)
async def create_git_checkpoint(req: CheckpointRequest, _: str = Depends(require_bearer_token)):
    guard = _get_git_guard()
    return await guard.create_checkpoint(req.name)

@app.post("/workspace/git/restore", response_model=CheckpointResponse)
async def restore_git_checkpoint(req: CheckpointRequest, _: str = Depends(require_bearer_token)):
    if not req.name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Checkpoint identifier required")
    guard = _get_git_guard()
    return await guard.restore_checkpoint(req.name)

@app.post("/workspace/test/run", response_model=TestRunResponse)
async def run_tests(req: TestRunRequest, _: str = Depends(require_bearer_token)):
    runner = _get_test_runner()
    try:
        return await runner.run_tests(
            command_key=req.command_key,
            extra_args=req.extra_args,
            timeout_seconds=req.timeout_seconds,
        )
    except TestRunnerSecurityError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))

@app.get("/web", response_class=HTMLResponse)

async def web_chat_interface():
    token = get_settings().auth_token
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Unified AI Coding Hub - Live Web Chat</title>
  <style>
    body {{
      margin: 0; padding: 20px;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
      background: #1e1e1e; color: #d4d4d4; display: flex; flex-direction: column; height: 95vh;
    }}
    .header {{ display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #333; padding-bottom: 12px; }}
    select, button, textarea {{
      background: #252526; color: #ccc; border: 1px solid #3c3c3c; border-radius: 4px; padding: 8px 12px; font-family: inherit; font-size: 13px;
    }}
    .chat-box {{ flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: 12px; padding: 16px 0; }}
    .msg {{ padding: 10px 14px; border-radius: 6px; max-width: 80%; line-height: 1.5; word-break: break-word; }}
    .msg.user {{ align-self: flex-end; background: #0e639c; color: #fff; }}
    .msg.assistant {{ align-self: flex-start; background: #2d2d2d; border: 1px solid #3e3e42; }}
    .msg.system {{ align-self: center; font-size: 12px; color: #888; font-style: italic; }}
    .input-row {{ display: flex; flex-direction: column; gap: 8px; border-top: 1px solid #333; padding-top: 12px; }}
    textarea {{ width: 100%; height: 70px; resize: vertical; box-sizing: border-box; }}
    .btn-row {{ display: flex; justify-content: space-between; }}
    button {{ cursor: pointer; background: #0e639c; color: white; border: none; font-weight: 500; }}
    button:hover {{ background: #1177bb; }}
    button#cancelBtn {{ background: #e51400; display: none; }}
  </style>
</head>
<body>
  <div class="header">
    <h2>Unified AI Coding Hub</h2>
    <div>
      <label>Agent: </label>
      <select id="agentSelect"><option value="auto">Auto Router</option></select>
    </div>
  </div>

  <div class="chat-box" id="chatBox">
    <div class="msg system">Live Web Chat connected to 127.0.0.1:8765. Ready for tasks.</div>
  </div>

  <div class="input-row">
    <textarea id="promptInput" placeholder="Type instructions for Codex, Copilot, Antigravity, or Mock... (Ctrl+Enter to send)"></textarea>
    <div class="btn-row">
      <button id="cancelBtn">Cancel Task</button>
      <div style="flex:1;"></div>
      <button id="sendBtn">Send Task</button>
    </div>
  </div>

  <script>
    const token = "{token}";
    const chatBox = document.getElementById('chatBox');
    const promptInput = document.getElementById('promptInput');
    const sendBtn = document.getElementById('sendBtn');
    const cancelBtn = document.getElementById('cancelBtn');
    const agentSelect = document.getElementById('agentSelect');

    let activeTaskId = null;
    let eventSource = null;
    let curAssistantMsg = null;

    function addMsg(role, text) {{
      const el = document.createElement('div');
      el.className = 'msg ' + role;
      el.textContent = text;
      chatBox.appendChild(el);
      chatBox.scrollTop = chatBox.scrollHeight;
      return el;
    }}

    async function loadAgents() {{
      try {{
        const res = await fetch('/agents', {{ headers: {{ 'Authorization': 'Bearer ' + token }} }});
        const agents = await res.json();
        agentSelect.innerHTML = '<option value="auto">Auto Router</option>';
        agents.forEach(a => {{
          const opt = document.createElement('option');
          opt.value = a.id;
          opt.textContent = a.name + ' [' + a.state + ']';
          agentSelect.appendChild(opt);
        }});
      }} catch (e) {{
        addMsg('system', 'Failed to load agents: ' + e);
      }}
    }}
    loadAgents();

    sendBtn.onclick = async () => {{
      const text = promptInput.value.trim();
      if (!text) return;
      promptInput.value = '';
      sendBtn.disabled = true;
      cancelBtn.style.display = 'inline-block';
      addMsg('user', text);
      curAssistantMsg = addMsg('assistant', '');

      try {{
        const res = await fetch('/tasks', {{
          method: 'POST',
          headers: {{ 'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ prompt: text, agent_id: agentSelect.value }})
        }});
        const data = await res.json();
        activeTaskId = data.task_id;

        eventSource = new EventSource('/tasks/' + activeTaskId + '/events?token=' + token);
        eventSource.onmessage = (e) => {{
          try {{
            const ev = JSON.parse(e.data);
            if (ev.event_type === 'token') {{
              curAssistantMsg.textContent += ev.text;
            }} else if (ev.event_type === 'started') {{
              curAssistantMsg.textContent = ev.text + '\\n';
            }} else if (ev.event_type === 'error') {{
              curAssistantMsg.textContent += '\\n[Error] ' + ev.text;
            }}
            chatBox.scrollTop = chatBox.scrollHeight;
          }} catch (_) {{}}
        }};

        eventSource.addEventListener('done', () => {{
          finishTask();
        }});

        eventSource.onerror = () => {{
          finishTask();
        }};
      }} catch (err) {{
        curAssistantMsg.textContent = 'Error: ' + err;
        finishTask();
      }}
    }};

    cancelBtn.onclick = async () => {{
      if (activeTaskId) {{
        await fetch('/tasks/' + activeTaskId + '/cancel', {{
          method: 'POST',
          headers: {{ 'Authorization': 'Bearer ' + token }}
        }});
      }}
      finishTask();
    }};

    function finishTask() {{
      if (eventSource) {{
        eventSource.close();
        eventSource = null;
      }}
      sendBtn.disabled = false;
      cancelBtn.style.display = 'none';
      activeTaskId = null;
      curAssistantMsg = null;
    }}

    promptInput.onkeydown = (e) => {{
      if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {{
        e.preventDefault();
        sendBtn.click();
      }}
    }};
  </script>
</body>
</html>"""
