import time
from enum import Enum
from typing import Any
from pydantic import BaseModel, Field


class SubtaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class Subtask(BaseModel):
    id: str
    title: str
    prompt: str
    target_files: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    assigned_agent: str = "auto"
    status: SubtaskStatus = SubtaskStatus.PENDING
    output: str = ""
    diff: str = ""
    duration_ms: float = 0.0
    error: str = ""


class ExecutionPlan(BaseModel):
    plan_id: str
    goal: str
    subtasks: list[Subtask] = Field(default_factory=list)
    parallel_batches: list[list[str]] = Field(default_factory=list)
    created_at: float = Field(default_factory=time.time)


class ReviewIssue(BaseModel):
    severity: str = "warning"  # "info", "warning", "error"
    file: str = ""
    message: str


class ReviewResult(BaseModel):
    approved: bool
    score: int = 100  # 0 to 100
    summary: str
    issues: list[ReviewIssue] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)


class OrchestrationAuditRecord(BaseModel):
    task_id: str
    goal: str
    plan: ExecutionPlan
    review: ReviewResult | None = None
    success: bool = True
    total_duration_ms: float = 0.0
    created_at: float
    completed_at: float
