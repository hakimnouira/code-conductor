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

class BrowserAssertions(BaseModel):
    """Application-specific checks applied to real browser evidence."""
    expected_text: list[str] = Field(default_factory=list, max_length=30)
    forbidden_text: list[str] = Field(default_factory=list, max_length=30)
    required_selectors: list[str] = Field(default_factory=list, max_length=30)
    expected_url_contains: str | None = Field(default=None, max_length=512)
    expected_title_contains: str | None = Field(default=None, max_length=512)


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
    auto_verify: bool = False
    verification_max_attempts: int = Field(default=2, ge=1, le=3)
    browser_verify_url: str | None = None
    browser_assertions: BrowserAssertions = Field(default_factory=BrowserAssertions)

class WorkflowPresetRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    prompt: str = Field(min_length=1, max_length=20000)
    agent_id: str = Field(default="auto", min_length=1, max_length=80)
    auto_verify: bool = False
    browser_verify_url: str | None = Field(default=None, max_length=2048)
    browser_assertions: BrowserAssertions = Field(default_factory=BrowserAssertions)


class WorkflowPresetResponse(BaseModel):
    workflow_id: str
    name: str
    prompt: str
    agent_id: str
    auto_verify: bool = False
    browser_verify_url: str = ""
    browser_assertions: BrowserAssertions = Field(default_factory=BrowserAssertions)
    created_at: float
    updated_at: float


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



class ScheduleCreateRequest(BaseModel):
    workflow_id: str = Field(min_length=1, max_length=80)
    interval_minutes: int = Field(default=60, ge=15, le=43200)
    max_retries: int = Field(default=1, ge=0, le=3)
    enabled: bool = True
    workspace_path: str | None = None


class ScheduleUpdateRequest(BaseModel):
    enabled: bool


class ScheduleResponse(BaseModel):
    schedule_id: str
    workflow_id: str
    name: str
    workspace_path: str = ""
    interval_minutes: int
    enabled: bool
    next_run_at: float
    max_retries: int
    failure_streak: int = 0
    last_run_at: float | None = None
    last_status: str = ""
    last_task_id: str = ""
    created_at: float
    updated_at: float


class ScheduleRunResponse(BaseModel):
    run_id: str
    schedule_id: str
    scheduled_for: float
    started_at: float | None = None
    completed_at: float | None = None
    status: str
    task_id: str = ""
    attempt: int = 0
    max_retries: int = 1
    error: str = ""
