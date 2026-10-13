"""Durable interval scheduler for saved Code Conductor workflows.

Schedule definitions and run records live in SQLite. A claimed run is persisted before
execution, so backend restarts can recover queued/running runs without losing history.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from app.task_store import (
    claim_due_schedules,
    get_incomplete_schedule_runs,
    list_schedule_runs,
    update_schedule,
    update_schedule_run,
)

Dispatch = Callable[[dict[str, Any]], Awaitable[str]]
StatusLookup = Callable[[str], Awaitable[dict[str, Any] | None]]


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

    while not stop_event.is_set():
        if not task_id:
            attempt += 1
            update_schedule_run(run_id, status="running", attempt=attempt, started_at=time.time(), error="")
            try:
                task_id = await dispatch(workflow)
                update_schedule_run(run_id, task_id=task_id, status="running", attempt=attempt)
            except Exception as exc:
                error = str(exc)[:1200]
                if attempt <= int(run.get("max_retries") or 0):
                    update_schedule_run(run_id, status="queued", attempt=attempt, error=error)
                    task_id = ""
                    await asyncio.sleep(min(2 ** min(attempt, 4), 15))
                    continue
                update_schedule_run(
                    run_id, status="failed", completed_at=time.time(),
                    attempt=attempt, error=error,
                )
                update_schedule(schedule_id, last_run_at=time.time(), last_status="failed", failure_streak_delta=1)
                return

        try:
            state = await status_lookup(task_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Preserve the running record and retry the same task lookup. Do not
            # dispatch duplicate work because a transient status query failed.
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
        return


async def scheduler_loop(
    dispatch: Dispatch,
    status_lookup: StatusLookup,
    *,
    poll_seconds: float = 5.0,
) -> None:
    """Run durable schedules until stopped; safely reattach monitors after restart."""
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
        import logging
        logger = logging.getLogger(__name__)
        while True:
            try:
                # Re-read incomplete runs every tick. This also recovers a transient
                # startup DB lock without permanently disabling scheduling.
                for run in get_incomplete_schedule_runs():
                    attach(run)
                for run in claim_due_schedules(time.time(), limit=20):
                    attach(run)
                for run in get_incomplete_schedule_runs():
                    attach(run)
            except asyncio.CancelledError:
                raise
            except Exception:
                # SQLite can be briefly busy during task/event writes. Keep the
                # scheduler alive and retry on the next tick instead of losing it.
                logger.exception("Scheduled workflow polling failed; retrying next tick")
            await asyncio.sleep(max(1.0, poll_seconds))
    except asyncio.CancelledError:
        stop_event.set()
        for task in list(monitors.values()):
            task.cancel()
        await asyncio.gather(*monitors.values(), return_exceptions=True)
        raise
