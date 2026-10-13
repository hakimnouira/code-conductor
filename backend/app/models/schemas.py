import time
from enum import Enum
from typing import Any, Literal
from pydantic import BaseModel, Field, field_validator, model_validator

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

class BrowserActionStep(BaseModel):
    """A bounded, selector-based browser action; arbitrary JavaScript is not accepted."""
    action: Literal["click", "fill", "check", "uncheck", "select_option", "press", "wait_for_selector"]
    selector: str = Field(min_length=1, max_length=500)
    value: str | None = Field(default=None, max_length=1000)
    timeout_ms: int = Field(default=5000, ge=100, le=10000)

    @field_validator("selector")
    @classmethod
    def selector_must_not_be_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Browser action selector must not be blank.")
        return cleaned

    @model_validator(mode="after")
    def require_action_value(self):
        if self.action in ("fill", "select_option", "press") and self.value is None:
            raise ValueError(f"Browser action '{self.action}' requires a value.")
        return self


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
    browser_required_text: list[str] = Field(default_factory=list, max_length=20)
    browser_forbidden_text: list[str] = Field(default_factory=list, max_length=20)
    browser_visible_selectors: list[str] = Field(default_factory=list, max_length=20)
    browser_hidden_selectors: list[str] = Field(default_factory=list, max_length=20)
    browser_steps: list[BrowserActionStep] = Field(default_factory=list, max_length=20)

    @field_validator("browser_required_text", "browser_forbidden_text", "browser_visible_selectors", "browser_hidden_selectors")
    @classmethod
    def bound_browser_assertion_values(cls, values: list[str]) -> list[str]:
        return [value.strip()[:500] for value in values if value.strip()][:20]


class WorkflowPresetRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    prompt: str = Field(min_length=1, max_length=20000)
    agent_id: str = Field(default="auto", min_length=1, max_length=80)
    auto_verify: bool = False
    browser_verify_url: str | None = Field(default=None, max_length=2048)
    browser_required_text: list[str] = Field(default_factory=list, max_length=20)
    browser_forbidden_text: list[str] = Field(default_factory=list, max_length=20)
    browser_visible_selectors: list[str] = Field(default_factory=list, max_length=20)
    browser_hidden_selectors: list[str] = Field(default_factory=list, max_length=20)
    browser_steps: list[BrowserActionStep] = Field(default_factory=list, max_length=20)

    @field_validator("browser_required_text", "browser_forbidden_text", "browser_visible_selectors", "browser_hidden_selectors")
    @classmethod
    def bound_browser_assertion_values(cls, values: list[str]) -> list[str]:
        return [value.strip()[:500] for value in values if value.strip()][:20]


class WorkflowPresetResponse(BaseModel):
    workflow_id: str
    name: str
    prompt: str
    agent_id: str
    auto_verify: bool = False
    browser_verify_url: str = ""
    browser_required_text: list[str] = Field(default_factory=list)
    browser_forbidden_text: list[str] = Field(default_factory=list)
    browser_visible_selectors: list[str] = Field(default_factory=list)
    browser_hidden_selectors: list[str] = Field(default_factory=list)
    browser_steps: list[BrowserActionStep] = Field(default_factory=list)
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
    execution_mode: Literal["host", "docker"] = "host"



class ScheduleCreateRequest(BaseModel):
    workflow_id: str = Field(min_length=1, max_length=80)
    interval_minutes: int = Field(default=60, ge=15, le=43200)
    cron_expression: str | None = Field(default=None, max_length=100)
    timezone: str = Field(default="UTC", min_length=1, max_length=100)
    misfire_policy: Literal["skip", "run_once"] = "run_once"
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
    schedule_type: Literal["interval", "cron"] = "interval"
    cron_expression: str = ""
    timezone: str = "UTC"
    misfire_policy: Literal["skip", "run_once"] = "run_once"
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
