"""Durable local scheduler for autonomous Code Conductor workflows.

Jobs and run history live in the same SQLite database as task history. The
scheduler uses atomic due-job claims so multiple loop ticks cannot start a job
twice, records recoverable failures, and delegates actual work to the existing
task pipeline.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator

from app.core.config import get_settings
from app.core.security import require_bearer_token
from app.safety.workspace import WorkspaceValidationError, validate_workspace_path
from app.task_store import (
    claim_due_scheduled_jobs,
    create_scheduled_job,
    delete_scheduled_job,
    finish_scheduled_run,
    get_scheduled_job,
    list_scheduled_jobs,
    list_scheduled_runs,
    recover_interrupted_scheduled_runs,
    set_scheduled_job_enabled,
    start_scheduled_run,
    update_scheduled_job,
)

router = APIRouter(prefix="/scheduler", tags=["scheduler"])

Dispatcher = Callable[[dict[str, Any]], Awaitable[str]]
TaskReader = Callable[[str], Awaitable[dict[str, Any] | None]]
_dispatcher: Dispatcher | None = None
_task_reader: TaskReader | None = None
_loop_task: asyncio.Task | None = None
_running_runs: set[str] = set()
_stop_event: asyncio.Event | None = None


class ScheduledJobCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    prompt: str = Field(min_length=1, max_length=20000)
    workspace_path: str | None = None
    agent_id: str = Field(default="auto", min_length=1, max_length=80)
    run_at: datetime | None = None
    interval_seconds: int | None = Field(default=None, ge=60, le=31536000)
    auto_verify: bool = False
    verification_max_attempts: int = Field(default=2, ge=1, le=3)
    browser_verify_url: str | None = Field(default=None, max_length=2048)
    browser_expected_text: list[str] = Field(default_factory=list, max_length=30)
    browser_forbidden_text: list[str] = Field(default_factory=list, max_length=30)
    browser_required_selectors: list[str] = Field(default_factory=list, max_length=30)
    browser_expected_url_contains: str | None = Field(default=None, max_length=512)
    github_research: bool = True
    max_retries: int = Field(default=2, ge=0, le=5)
    enabled: bool = True

    @model_validator(mode="after")
    def validate_schedule(self):
        if (self.run_at is None) == (self.interval_seconds is None):
            raise ValueError("Provide exactly one of run_at (one-time) or interval_seconds (recurring).")
        if not self.name.strip() or not self.prompt.strip():
            raise ValueError("Name and prompt must not be blank.")
        return self


class ScheduledJobToggle(BaseModel):
    enabled: bool


def configure_scheduler(dispatcher: Dispatcher, task_reader: TaskReader) -> None:
    global _dispatcher, _task_reader
    _dispatcher = dispatcher
    _task_reader = task_reader


def _timestamp(value: datetime) -> float:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def _public_job(item: dict[str, Any]) -> dict[str, Any]:
    result = dict(item)
    payload = result.pop("payload", {})
    result.update(payload)
    return result


@router.get("/jobs")
async def get_scheduled_jobs(_: str = Depends(require_bearer_token)):
    return [_public_job(item) for item in list_scheduled_jobs()]


@router.post("/jobs")
async def create_scheduled_job_endpoint(req: ScheduledJobCreate, _: str = Depends(require_bearer_token)):
    if req.browser_verify_url:
        from urllib.parse import urlsplit
        parsed = urlsplit(req.browser_verify_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise HTTPException(status_code=422, detail="Browser verification URL must be HTTP(S), include a host, and contain no credentials.")
    try:
        workspace = validate_workspace_path(req.workspace_path, default_root=get_settings().workspace_root)
    except WorkspaceValidationError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid workspace path: {exc}") from exc

    now = time.time()
    interval = req.interval_seconds
    run_at = _timestamp(req.run_at) if req.run_at else now
    payload = {
        "prompt": req.prompt.strip(),
        "agent_id": req.agent_id.strip() or "auto",
        "workspace_path": str(workspace),
        "auto_verify": req.auto_verify,
        "verification_max_attempts": req.verification_max_attempts,
        "browser_verify_url": req.browser_verify_url or "",
        "browser_expected_text": req.browser_expected_text,
        "browser_forbidden_text": req.browser_forbidden_text,
        "browser_required_selectors": req.browser_required_selectors,
        "browser_expected_url_contains": req.browser_expected_url_contains,
        "github_research": req.github_research,
        "permission_mode": "safe",
        "god_mode_confirmed": False,
    }
    job = {
        "job_id": uuid.uuid4().hex,
        "name": req.name.strip(),
        "payload": payload,
        "schedule_type": "interval" if interval else "once",
        "interval_seconds": interval,
        "next_run_at": now + interval if interval else run_at,
        "retry_count": 0,
        "max_retries": req.max_retries,
        "enabled": req.enabled,
        "created_at": now,
        "updated_at": now,
    }
    created = create_scheduled_job(job)
    return _public_job(created)


@router.get("/runs")
async def get_scheduled_runs(
    limit: int = Query(default=100, ge=1, le=500),
    job_id: str | None = None,
    _: str = Depends(require_bearer_token),
):
    return list_scheduled_runs(limit=limit, job_id=job_id)


@router.patch("/jobs/{job_id}")
async def toggle_scheduled_job(job_id: str, req: ScheduledJobToggle, _: str = Depends(require_bearer_token)):
    item = set_scheduled_job_enabled(job_id, req.enabled)
    if not item:
        raise HTTPException(status_code=404, detail="Scheduled job not found.")
    return _public_job(item)


@router.post("/jobs/{job_id}/run-now")
async def run_scheduled_job_now(job_id: str, _: str = Depends(require_bearer_token)):
    item = update_scheduled_job(job_id, {"enabled": True, "retry_at": time.time(), "updated_at": time.time()})
    if not item:
        raise HTTPException(status_code=404, detail="Scheduled job not found.")
    return {"job_id": job_id, "queued": True, "message": "The job will be picked up by the scheduler shortly."}


@router.delete("/jobs/{job_id}")
async def remove_scheduled_job(job_id: str, _: str = Depends(require_bearer_token)):
    if not delete_scheduled_job(job_id):
        raise HTTPException(status_code=404, detail="Scheduled job not found.")
    return {"job_id": job_id, "deleted": True}


async def _execute_job(job: dict[str, Any]) -> None:
    job_id = str(job["job_id"])
    run_id = uuid.uuid4().hex
    started_at = time.time()
    start_scheduled_run({
        "run_id": run_id,
        "job_id": job_id,
        "scheduled_at": float(job.get("due_at") or job.get("next_run_at") or started_at),
        "started_at": started_at,
        "status": "running",
    })
    try:
        if _dispatcher is None or _task_reader is None:
            raise RuntimeError("Scheduler task dispatcher is not configured.")
        task_id = await _dispatcher(dict(job.get("payload") or {}))
        update_scheduled_job(job_id, {"last_task_id": task_id, "updated_at": time.time()})
        deadline = time.monotonic() + 4 * 60 * 60
        snapshot = None
        while time.monotonic() < deadline:
            snapshot = await _task_reader(task_id)
            if snapshot and snapshot.get("status") in ("completed", "failed", "cancelled"):
                break
            await asyncio.sleep(1.5)
        if not snapshot or snapshot.get("status") not in ("completed", "failed", "cancelled"):
            raise TimeoutError("Scheduled task did not reach a terminal state within four hours.")
        task_status = str(snapshot.get("status"))
        ok = task_status == "completed"
        error = None if ok else str(snapshot.get("error") or f"Task ended with status {task_status}")[:1500]
        latest = get_scheduled_job(job_id) or job
        retries = int(latest.get("retry_count", 0))
        max_retries = int(latest.get("max_retries", 0))
        retry_at = None
        if not ok and retries < max_retries:
            retries += 1
            retry_at = time.time() + min(60 * (2 ** (retries - 1)), 900)
        finish_scheduled_run({
            "run_id": run_id, "job_id": job_id,
            "status": "completed" if ok else task_status,
            "task_id": task_id, "error": error,
            "result": snapshot, "finished_at": time.time(),
        }, retry_at=retry_at, retry_count=retries if not ok else 0)
    except Exception as exc:
        latest = get_scheduled_job(job_id) or job
        retries = int(latest.get("retry_count", 0))
        max_retries = int(latest.get("max_retries", 0))
        retry_at = None
        if retries < max_retries:
            retries += 1
            retry_at = time.time() + min(60 * (2 ** (retries - 1)), 900)
        finish_scheduled_run({
            "run_id": run_id, "job_id": job_id, "status": "failed",
            "task_id": None, "error": str(exc)[:1500],
            "result": None, "finished_at": time.time(),
        }, retry_at=retry_at, retry_count=retries)
    finally:
        _running_runs.discard(job_id)


async def _scheduler_loop() -> None:
    while _stop_event is not None and not _stop_event.is_set():
        try:
            due = claim_due_scheduled_jobs(time.time())
            for job in due:
                job_id = str(job["job_id"])
                if job_id in _running_runs:
                    continue
                _running_runs.add(job_id)
                asyncio.create_task(_execute_job(job), name=f"scheduled-job-{job_id}")
        except Exception:
            # A transient SQLite or provider issue must not kill the scheduler loop.
            import logging
            logging.getLogger(__name__).exception("Scheduler tick failed")
        try:
            await asyncio.wait_for(_stop_event.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pass


async def start_scheduler() -> None:
    global _loop_task, _stop_event
    if _loop_task and not _loop_task.done():
        return
    recover_interrupted_scheduled_runs(time.time())
    _stop_event = asyncio.Event()
    _loop_task = asyncio.create_task(_scheduler_loop(), name="code-conductor-scheduler")


async def stop_scheduler() -> None:
    global _loop_task, _stop_event
    if _stop_event:
        _stop_event.set()
    if _loop_task:
        await _loop_task
    _loop_task = None
    _stop_event = None
