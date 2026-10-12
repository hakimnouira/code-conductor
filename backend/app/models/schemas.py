import time
from enum import Enum
from typing import Any, Literal
from pydantic import BaseModel, Field

class AgentId(str, Enum):
    AUTO = "auto"
    MOCK = "mock"
    CODEX = "codex"
    COPILOT = "copilot"
    ANTIGRAVITY = "antigravity"
    OLLAMA = "ollama"

class AvailabilityState(str, Enum):
    READY = "ready"
    MISSING_CLI = "missing_cli"
    UNAUTHENTICATED = "unauthenticated"
    DISABLED = "disabled"
    QUOTA_EXHAUSTED = "quota_exhausted"
    ERROR = "error"

class AgentCapabilities(BaseModel):
    supports_streaming: bool = True
    supports_code_edits: bool = True
    supports_terminal_execution: bool = False
    supports_subagents: bool = False
    supported_models: list[str] = Field(default_factory=list)

class AgentInfo(BaseModel):
    id: str
    name: str
    description: str
    state: AvailabilityState
    diagnostic: str = ""
    capabilities: AgentCapabilities = Field(default_factory=AgentCapabilities)
    is_default: bool = False

class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

class TaskCreateRequest(BaseModel):
    prompt: str
    agent_id: str = "auto"
    workspace_path: str | None = None
    model: str | None = None
    stream: bool = True
    auto_approve: bool = False
    permission_mode: Literal["safe", "balanced", "god"] = "safe"
    god_mode_confirmed: bool = False
    github_research: bool = True

class TaskResponse(BaseModel):
    task_id: str
    status: TaskStatus
    agent_id: str
    prompt: str
    created_at: float
    message: str = "Task queued"
    workspace_path: str | None = None

class EventType(str, Enum):
    STARTED = "started"
    TOKEN = "token"
    STEP_UPDATE = "step_update"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    LOG = "log"
    DIFF = "diff"
    ERROR = "error"
    COMPLETED = "completed"
    CANCELLED = "cancelled"

class AgentEvent(BaseModel):
    task_id: str = ""
    event_type: EventType
    data: dict[str, Any] = Field(default_factory=dict)
    text: str = ""
    timestamp: float = Field(default_factory=time.time)

class HealthResponse(BaseModel):
    status: Literal["healthy", "degraded", "unhealthy"] = "healthy"
    version: str
    uptime_seconds: float
    workspace: str
    active_tasks: int

# Safety & Workspace Models (Milestone 7)
class FileReadRequest(BaseModel):
    path: str
    workspace_path: str | None = None

class FileReadResponse(BaseModel):
    path: str
    content: str
    size_bytes: int

class FileWriteRequest(BaseModel):
    path: str
    content: str
    atomic: bool = True
    workspace_path: str | None = None

class FileWriteResponse(BaseModel):
    path: str
    bytes_written: int
    diff: str

class SafeFileListResponse(BaseModel):
    root: str
    files: list[str]
    total_count: int

class GitStatusResponse(BaseModel):
    is_repo: bool
    branch: str = ""
    clean: bool = True
    modified: list[str] = Field(default_factory=list)
    untracked: list[str] = Field(default_factory=list)
    deleted: list[str] = Field(default_factory=list)
    error: str = ""

class GitDiffRequest(BaseModel):
    file_path: str | None = None
    staged: bool = False
    workspace_path: str | None = None

class GitDiffResponse(BaseModel):
    diff: str
    file_count: int = 0
    error: str = ""

class CheckpointRequest(BaseModel):
    name: str | None = None
    force: bool = False
    workspace_path: str | None = None

class CheckpointResponse(BaseModel):
    checkpoint_id: str
    success: bool
    message: str

class TestRunRequest(BaseModel):
    command_key: str = "pytest"
    extra_args: list[str] = Field(default_factory=list)
    timeout_seconds: int = 30
    workspace_path: str | None = None


class TestRunResponse(BaseModel):
    command: str
    exit_code: int
    passed: bool
    stdout: str
    stderr: str
    duration_ms: float

