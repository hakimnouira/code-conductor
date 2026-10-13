"""Durable interval scheduler for saved Code Conductor workflows.

Schedule definitions and run records live in SQLite. A claimed run is persisted before
execution, so backend restarts can recover queued/running runs without losing history.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from app.notifications import notify_schedule_outcome
from app.task_store import (
    claim_due_schedules,
    get_incomplete_schedule_runs,
    is_scheduler_paused,
    list_schedule_runs,
    update_schedule,
    update_schedule_run,
)

Dispatch = Callable[[dict[str, Any]], Awaitable[str]]
StatusLookup = Callable[[str], Awaitable[dict[str, Any] | None]]

_scheduler_started_at: float | None = None
_scheduler_last_tick_at: float | None = None
_scheduler_running = False
_scheduler_poll_seconds = 5.0


def get_scheduler_diagnostics() -> dict[str, Any]:
    """Expose a safe heartbeat snapshot for local operational diagnostics."""
    now = time.time()
    age = max(0.0, now - _scheduler_last_tick_at) if _scheduler_last_tick_at else None
    healthy = bool(
        _scheduler_running
        and age is not None
        and age <= max(30.0, _scheduler_poll_seconds * 3.0)
    )
    try:
        paused: bool | None = is_scheduler_paused()
    except Exception:
        paused = None
    return {
        "status": "healthy" if healthy else ("stale" if _scheduler_running else "stopped"),
        "running": _scheduler_running,
        "paused": paused,
        "started_at": _scheduler_started_at,
        "last_tick_at": _scheduler_last_tick_at,
        "last_tick_age_seconds": round(age, 2) if age is not None else None,
        "poll_seconds": _scheduler_poll_seconds,
    }


async def _monitor_run(
    run: dict[str, Any],
    dispatch: Dispatch,
    status_lookup: StatusLookup,
    stop_event: asyncio.Event,
) -> None:
    run_id = str(run["run_id"])
    schedule_id = str(run["schedule_id"])
    workflow = run["workflow"]
    attempt = int(run.get("attempt") or 0)
    task_id = str(run.get("task_id") or "")
    dispatch_failures = 0

    while not stop_event.is_set():
        if not task_id:
            # A global pause prevents new dispatches but leaves existing task monitors
            # alive. The durable run stays pending until an operator resumes schedules.
            if is_scheduler_paused():
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=_scheduler_poll_seconds)
                    return
                except asyncio.TimeoutError:
                    continue
            # Reuse the same attempt key when a dispatch response is lost. A backend
            # restart after task persistence but before saving task_id must not spawn
            # a second provider task.
            if str(run.get("status") or "queued") != "running" or attempt <= 0:
                attempt += 1
            update_schedule_run(run_id, status="running", attempt=attempt, started_at=time.time(), error="")
            run["status"] = "running"
            dispatch_payload = dict(workflow)
            dispatch_payload["_code_conductor_idempotency_key"] = (
                f"code-conductor:schedule:{run_id}:attempt:{attempt}"
            )
            try:
                task_id = await dispatch(dispatch_payload)
                update_schedule_run(run_id, task_id=task_id, status="running", attempt=attempt, error="")
                dispatch_failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error = str(exc)[:1200]
                dispatch_failures += 1
                if dispatch_failures <= int(run.get("max_retries") or 0):
                    # Keep this logical attempt running so retries/restarts reuse its
                    # idempotency key. A new attempt is only created after a known
                    # terminal task failure.
                    update_schedule_run(run_id, status="running", attempt=attempt, error=error)
                    try:
                        await asyncio.wait_for(
                            stop_event.wait(), timeout=min(2 ** min(dispatch_failures, 4), 15)
                        )
                        return
                    except asyncio.TimeoutError:
                        continue
                update_schedule_run(
                    run_id, status="failed", completed_at=time.time(),
                    attempt=attempt, error=error,
                )
                update_schedule(schedule_id, last_run_at=time.time(), last_status="failed", failure_streak_delta=1)
                notify_schedule_outcome(
                    str(run.get("name") or f"Workflow {schedule_id[:8]}"),
                    "failed",
                    error,
                )
                return

        try:
            state = await status_lookup(task_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A transient database or in-memory status lookup error must not cause
            # the scheduler to dispatch a duplicate task or abandon the run.
            update_schedule_run(run_id, error=f"Temporary task status lookup failure: {str(exc)[:500]}")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
            continue
        if state is None:
            terminal_status, error = "failed", f"Task {task_id} was not found after scheduling."
        else:
            status = str(state.get("status") or "unknown")
            if status in ("pending", "running"):
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass
                continue
            terminal_status = "completed" if status == "completed" else "failed"
            error = str(state.get("error") or "")[:1200]

        if terminal_status == "failed" and attempt <= int(run.get("max_retries") or 0):
            update_schedule_run(
                run_id, status="queued", task_id="", attempt=attempt, error=error,
            )
            run["status"] = "queued"
            task_id = ""
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=min(2 ** min(attempt, 4), 15))
                return
            except asyncio.TimeoutError:
                continue

        now = time.time()
        update_schedule_run(
            run_id, status=terminal_status, completed_at=now,
            task_id=task_id, attempt=attempt, error=error,
        )
        update_schedule(
            schedule_id,
            last_run_at=now,
            last_status=terminal_status,
            last_task_id=task_id,
            failure_streak_delta=0 if terminal_status == "completed" else 1,
        )
        notify_schedule_outcome(
            str(run.get("name") or f"Workflow {schedule_id[:8]}"),
            terminal_status,
            error,
        )
        return


async def scheduler_loop(
    dispatch: Dispatch,
    status_lookup: StatusLookup,
    *,
    poll_seconds: float = 5.0,
) -> None:
    """Run durable schedules until stopped; safely reattach monitors after restart."""
    global _scheduler_started_at, _scheduler_last_tick_at, _scheduler_running, _scheduler_poll_seconds
    _scheduler_started_at = time.time()
    _scheduler_last_tick_at = _scheduler_started_at
    _scheduler_running = True
    _scheduler_poll_seconds = max(1.0, float(poll_seconds))
    stop_event = asyncio.Event()
    monitors: dict[str, asyncio.Task[None]] = {}

    def attach(run: dict[str, Any]) -> None:
        run_id = str(run["run_id"])
        existing = monitors.get(run_id)
        if existing and not existing.done():
            return
        task = asyncio.create_task(_monitor_run(run, dispatch, status_lookup, stop_event))
        monitors[run_id] = task
        task.add_done_callback(lambda finished, key=run_id: monitors.pop(key, None) if monitors.get(key) is finished else None)

    try:
        logger = logging.getLogger(__name__)
        while True:
            _scheduler_last_tick_at = time.time()
            try:
                # Re-read incomplete runs on each tick. A temporary SQLite lock
                # during startup must not permanently disable autonomous workflows.
                for run in get_incomplete_schedule_runs():
                    attach(run)
                if not is_scheduler_paused():
                    for run in claim_due_schedules(time.time(), limit=20):
                        attach(run)
                for run in get_incomplete_schedule_runs():
                    attach(run)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Scheduled workflow polling failed; retrying next tick")
            await asyncio.sleep(max(1.0, poll_seconds))
    except asyncio.CancelledError:
        _scheduler_running = False
        stop_event.set()
        for task in list(monitors.values()):
            task.cancel()
        await asyncio.gather(*monitors.values(), return_exceptions=True)
        raise
