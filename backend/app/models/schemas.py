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

class TaskResponse(BaseModel):
    task_id: str
    status: TaskStatus
    agent_id: str
    prompt: str
    created_at: float
    message: str = "Task queued"

class EventType(str, Enum):
    STARTED = "started"
    TOKEN = "token"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    LOG = "log"
    DIFF = "diff"
    ERROR = "error"
    COMPLETED = "completed"
    CANCELLED = "cancelled"

class AgentEvent(BaseModel):
    task_id: str
    event_type: EventType
    data: dict[str, Any] = Field(default_factory=dict)
    text: str = ""
    timestamp: float

class HealthResponse(BaseModel):
    status: Literal["healthy", "degraded", "unhealthy"] = "healthy"
    version: str
    uptime_seconds: float
    workspace: str
    active_tasks: int
