"""Durable local task state and append-only task event history.

The database contains task prompts and execution metadata, but never provider API keys.
It is local-only and can be relocated with CODE_CONDUCTOR_TASK_DB.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from app.cron_schedule import next_cron_occurrence


def _database_path() -> Path:
    override = os.environ.get("CODE_CONDUCTOR_TASK_DB", "").strip()
    path = Path(override).expanduser() if override else Path.home() / ".code-conductor" / "tasks.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(_database_path(), timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            request_json TEXT NOT NULL,
            snapshot_json TEXT NOT NULL,
            initial_request_json TEXT NOT NULL DEFAULT '',
            updated_at REAL NOT NULL
        )"""
    )
    task_columns = {row["name"] for row in connection.execute("PRAGMA table_info(tasks)").fetchall()}
    if "initial_request_json" not in task_columns:
        connection.execute("ALTER TABLE tasks ADD COLUMN initial_request_json TEXT NOT NULL DEFAULT ''")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS task_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            payload TEXT NOT NULL,
            created_at REAL NOT NULL
        )"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_events_task_id_event_id ON task_events(task_id, event_id)"
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS scheduled_workflows (
            schedule_id TEXT PRIMARY KEY,
            workflow_id TEXT NOT NULL,
            workflow_json TEXT NOT NULL,
            name TEXT NOT NULL,
            workspace_path TEXT NOT NULL DEFAULT '',
            interval_minutes INTEGER NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            next_run_at REAL NOT NULL,
            max_retries INTEGER NOT NULL DEFAULT 1,
            failure_streak INTEGER NOT NULL DEFAULT 0,
            last_run_at REAL,
            last_status TEXT NOT NULL DEFAULT '',
            last_task_id TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            schedule_type TEXT NOT NULL DEFAULT 'interval',
            cron_expression TEXT NOT NULL DEFAULT '',
            timezone TEXT NOT NULL DEFAULT 'UTC',
            misfire_policy TEXT NOT NULL DEFAULT 'run_once'
        )"""
    )
    # Migrate databases created before cron/timezone scheduling was introduced.
    schedule_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(scheduled_workflows)").fetchall()
    }
    for column, definition in (
        ("schedule_type", "TEXT NOT NULL DEFAULT 'interval'"),
        ("cron_expression", "TEXT NOT NULL DEFAULT ''"),
        ("timezone", "TEXT NOT NULL DEFAULT 'UTC'"),
        ("misfire_policy", "TEXT NOT NULL DEFAULT 'run_once'"),
    ):
        if column not in schedule_columns:
            connection.execute(f"ALTER TABLE scheduled_workflows ADD COLUMN {column} {definition}")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS schedule_runs (
            run_id TEXT PRIMARY KEY,
            schedule_id TEXT NOT NULL,
            workflow_json TEXT NOT NULL,
            scheduled_for REAL NOT NULL,
            started_at REAL,
            completed_at REAL,
            status TEXT NOT NULL,
            task_id TEXT NOT NULL DEFAULT '',
            attempt INTEGER NOT NULL DEFAULT 0,
            max_retries INTEGER NOT NULL DEFAULT 1,
            error TEXT NOT NULL DEFAULT ''
        )"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_schedule_runs_status ON schedule_runs(status, scheduled_for)"
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS workflow_presets (
            workflow_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            prompt TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            auto_verify INTEGER NOT NULL DEFAULT 0,
            browser_verify_url TEXT NOT NULL DEFAULT '',
            browser_required_text TEXT NOT NULL DEFAULT '[]',
            browser_forbidden_text TEXT NOT NULL DEFAULT '[]',
            browser_visible_selectors TEXT NOT NULL DEFAULT '[]',
            browser_hidden_selectors TEXT NOT NULL DEFAULT '[]',
            browser_steps TEXT NOT NULL DEFAULT '[]',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )"""
    )
    # Upgrade databases created by earlier Code Conductor versions without losing presets.
    existing_columns = {row["name"] for row in connection.execute("PRAGMA table_info(workflow_presets)").fetchall()}
    for column in (
        "browser_required_text", "browser_forbidden_text",
        "browser_visible_selectors", "browser_hidden_selectors", "browser_steps",
    ):
        if column not in existing_columns:
            connection.execute(f"ALTER TABLE workflow_presets ADD COLUMN {column} TEXT NOT NULL DEFAULT '[]'")
    connection.commit()
    return connection


def save_task(
    snapshot: dict[str, Any],
    request_json: str,
    initial_request_json: str | None = None,
) -> None:
    """Persist current execution state while preserving the immutable original request."""
    initial_json = initial_request_json or request_json
    with _connect() as db:
        db.execute(
            """INSERT INTO tasks(
                   task_id, request_json, snapshot_json, initial_request_json, updated_at
               ) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(task_id) DO UPDATE SET
                 request_json=excluded.request_json,
                 snapshot_json=excluded.snapshot_json,
                 initial_request_json=CASE
                     WHEN tasks.initial_request_json = '' THEN excluded.initial_request_json
                     ELSE tasks.initial_request_json
                 END,
                 updated_at=excluded.updated_at""",
            (
                snapshot["task_id"],
                request_json,
                json.dumps(snapshot, ensure_ascii=False),
                initial_json,
                snapshot.get("completed_at") or snapshot.get("started_at") or snapshot["created_at"],
            ),
        )


def append_event(task_id: str, payload: str, created_at: float) -> int:
    with _connect() as db:
        cursor = db.execute(
            "INSERT INTO task_events(task_id, payload, created_at) VALUES (?, ?, ?)",
            (task_id, payload, created_at),
        )
        return int(cursor.lastrowid)


def events_after(task_id: str, after_event_id: int = 0, limit: int = 200) -> list[dict[str, Any]]:
    with _connect() as db:
        rows = db.execute(
            """SELECT event_id, payload, created_at FROM task_events
               WHERE task_id = ? AND event_id > ? ORDER BY event_id ASC LIMIT ?""",
            (task_id, max(0, after_event_id), min(max(1, limit), 1000)),
        ).fetchall()
    return [dict(row) for row in rows]


def get_saved_tasks(limit: int = 100) -> list[dict[str, Any]]:
    with _connect() as db:
        rows = db.execute(
            "SELECT request_json, snapshot_json, initial_request_json FROM tasks ORDER BY updated_at DESC LIMIT ?",
            (min(max(1, limit), 500),),
        ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        try:
            current_request = json.loads(row["request_json"])
            original_json = row["initial_request_json"] or row["request_json"]
            try:
                initial_request = json.loads(original_json)
            except (TypeError, json.JSONDecodeError):
                initial_request = current_request
            result.append({
                "request": current_request,
                "initial_request": initial_request,
                "snapshot": json.loads(row["snapshot_json"]),
            })
        except (TypeError, json.JSONDecodeError):
            continue
    return result



def _decode_json_list(value: Any) -> list[str]:
    try:
        decoded = json.loads(value) if isinstance(value, str) else value
        if not isinstance(decoded, list):
            return []
        return [str(item).strip()[:500] for item in decoded if isinstance(item, str) and item.strip()][:20]
    except (TypeError, json.JSONDecodeError):
        return []


def list_workflows(limit: int = 100) -> list[dict[str, Any]]:
    with _connect() as db:
        rows = db.execute(
            """SELECT workflow_id, name, prompt, agent_id, auto_verify,
                      browser_verify_url, browser_required_text, browser_forbidden_text,
                      browser_visible_selectors, browser_hidden_selectors, browser_steps, created_at, updated_at
               FROM workflow_presets ORDER BY updated_at DESC LIMIT ?""",
            (min(max(1, limit), 200),),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["auto_verify"] = bool(item["auto_verify"])
        for field in ("browser_required_text", "browser_forbidden_text", "browser_visible_selectors", "browser_hidden_selectors"):
            item[field] = _decode_json_list(item[field])
        try:
            steps = json.loads(item.get("browser_steps") or "[]")
            item["browser_steps"] = [step for step in steps if isinstance(step, dict)][:20] if isinstance(steps, list) else []
        except (TypeError, json.JSONDecodeError):
            item["browser_steps"] = []
        result.append(item)
    return result


def save_workflow(workflow: dict[str, Any]) -> dict[str, Any]:
    now = float(workflow.get("updated_at") or time.time())
    created_at = float(workflow.get("created_at") or now)
    with _connect() as db:
        db.execute(
            """INSERT INTO workflow_presets(
                    workflow_id, name, prompt, agent_id, auto_verify,
                    browser_verify_url, browser_required_text, browser_forbidden_text,
                    browser_visible_selectors, browser_hidden_selectors, browser_steps, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(workflow_id) DO UPDATE SET
                    name=excluded.name,
                    prompt=excluded.prompt,
                    agent_id=excluded.agent_id,
                    auto_verify=excluded.auto_verify,
                    browser_verify_url=excluded.browser_verify_url,
                    browser_required_text=excluded.browser_required_text,
                    browser_forbidden_text=excluded.browser_forbidden_text,
                    browser_visible_selectors=excluded.browser_visible_selectors,
                    browser_hidden_selectors=excluded.browser_hidden_selectors,
                    browser_steps=excluded.browser_steps,
                    updated_at=excluded.updated_at""",
            (
                workflow["workflow_id"], workflow["name"], workflow["prompt"],
                workflow.get("agent_id", "auto"), int(bool(workflow.get("auto_verify", False))),
                workflow.get("browser_verify_url", ""),
                json.dumps(_decode_json_list(workflow.get("browser_required_text", [])), ensure_ascii=False),
                json.dumps(_decode_json_list(workflow.get("browser_forbidden_text", [])), ensure_ascii=False),
                json.dumps(_decode_json_list(workflow.get("browser_visible_selectors", [])), ensure_ascii=False),
                json.dumps(_decode_json_list(workflow.get("browser_hidden_selectors", [])), ensure_ascii=False),
                json.dumps([step for step in workflow.get("browser_steps", []) if isinstance(step, dict)][:20], ensure_ascii=False),
                created_at, now,
            ),
        )
    return next((item for item in list_workflows(200) if item["workflow_id"] == workflow["workflow_id"]), dict(workflow))


def delete_workflow(workflow_id: str) -> bool:
    with _connect() as db:
        cursor = db.execute("DELETE FROM workflow_presets WHERE workflow_id = ?", (workflow_id,))
        return cursor.rowcount > 0


def create_schedule(schedule: dict[str, Any]) -> dict[str, Any]:
    with _connect() as db:
        db.execute(
            """INSERT INTO scheduled_workflows(
                schedule_id, workflow_id, workflow_json, name, workspace_path,
                interval_minutes, enabled, next_run_at, max_retries,
                failure_streak, last_run_at, last_status, last_task_id, created_at, updated_at,
                schedule_type, cron_expression, timezone, misfire_policy
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, '', '', ?, ?, ?, ?, ?, ?)""",
            (
                schedule["schedule_id"], schedule["workflow_id"],
                json.dumps(schedule["workflow"], ensure_ascii=False),
                schedule["name"], schedule.get("workspace_path") or "",
                int(schedule["interval_minutes"]), int(bool(schedule.get("enabled", True))),
                float(schedule["next_run_at"]), int(schedule.get("max_retries", 1)),
                float(schedule["created_at"]), float(schedule["updated_at"]),
                schedule.get("schedule_type", "interval"), schedule.get("cron_expression") or "",
                schedule.get("timezone", "UTC"), schedule.get("misfire_policy", "run_once"),
            ),
        )
    return get_schedule(schedule["schedule_id"]) or dict(schedule)


def _schedule_row(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    try:
        result["workflow"] = json.loads(result.pop("workflow_json"))
    except (TypeError, json.JSONDecodeError):
        result["workflow"] = {}
    result["enabled"] = bool(result["enabled"])
    return result


def get_schedule(schedule_id: str) -> dict[str, Any] | None:
    with _connect() as db:
        row = db.execute("SELECT * FROM scheduled_workflows WHERE schedule_id = ?", (schedule_id,)).fetchone()
    return _schedule_row(row) if row else None


def list_schedules(limit: int = 100) -> list[dict[str, Any]]:
    with _connect() as db:
        rows = db.execute(
            "SELECT * FROM scheduled_workflows ORDER BY created_at DESC LIMIT ?",
            (min(max(1, limit), 200),),
        ).fetchall()
    return [_schedule_row(row) for row in rows]


def update_schedule(
    schedule_id: str,
    *,
    enabled: bool | None = None,
    next_run_at: float | None = None,
    last_run_at: float | None = None,
    last_status: str | None = None,
    last_task_id: str | None = None,
    failure_streak_delta: int | None = None,
) -> bool:
    assignments = ["updated_at = ?"]
    values: list[Any] = [time.time()]
    for column, value in (
        ("enabled", int(enabled) if enabled is not None else None),
        ("next_run_at", next_run_at),
        ("last_run_at", last_run_at),
        ("last_status", last_status),
        ("last_task_id", last_task_id),
    ):
        if value is not None:
            assignments.append(f"{column} = ?")
            values.append(value)
    with _connect() as db:
        if failure_streak_delta is not None:
            if failure_streak_delta == 0:
                db.execute("UPDATE scheduled_workflows SET failure_streak = 0 WHERE schedule_id = ?", (schedule_id,))
            else:
                db.execute(
                    "UPDATE scheduled_workflows SET failure_streak = MAX(0, failure_streak + ?) WHERE schedule_id = ?",
                    (failure_streak_delta, schedule_id),
                )
        values.append(schedule_id)
        cursor = db.execute(
            f"UPDATE scheduled_workflows SET {', '.join(assignments)} WHERE schedule_id = ?",
            values,
        )
    return cursor.rowcount > 0


def delete_schedule(schedule_id: str) -> bool:
    with _connect() as db:
        cursor = db.execute("DELETE FROM scheduled_workflows WHERE schedule_id = ?", (schedule_id,))
    return cursor.rowcount > 0


def claim_due_schedules(now: float, limit: int = 20) -> list[dict[str, Any]]:
    """Atomically advance due schedules and persist one queued run for each occurrence."""
    claimed: list[dict[str, Any]] = []
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        rows = db.execute(
            """SELECT * FROM scheduled_workflows
               WHERE enabled = 1 AND next_run_at <= ?
               ORDER BY next_run_at ASC LIMIT ?""",
            (now, min(max(1, limit), 100)),
        ).fetchall()
        for row in rows:
            schedule = _schedule_row(row)
            active = db.execute(
                "SELECT 1 FROM schedule_runs WHERE schedule_id = ? AND status IN ('queued', 'running') LIMIT 1",
                (schedule["schedule_id"],),
            ).fetchone()
            scheduled_for = float(schedule["next_run_at"])
            overdue_by = max(0.0, now - scheduled_for)
            if schedule.get("schedule_type") == "cron":
                next_scheduled = next_cron_occurrence(
                    schedule["cron_expression"], schedule["timezone"], scheduled_for
                )
                interval_seconds = max(60.0, next_scheduled - scheduled_for)
                next_run = next_cron_occurrence(
                    schedule["cron_expression"], schedule["timezone"], now
                )
            else:
                interval_seconds = int(schedule["interval_minutes"]) * 60
                next_run = now + interval_seconds
            if active:
                # Never overlap executions of one workflow; move the next occurrence
                # forward rather than accumulating a backlog.
                db.execute(
                    "UPDATE scheduled_workflows SET next_run_at = ?, updated_at = ? WHERE schedule_id = ?",
                    (next_run, now, schedule["schedule_id"]),
                )
                continue
            if schedule.get("misfire_policy", "run_once") == "skip" and overdue_by > interval_seconds:
                # Explicit skip policy: if the scheduler was down for more than one
                # interval, record no stale run and advance to the next future tick.
                db.execute(
                    "UPDATE scheduled_workflows SET next_run_at = ?, updated_at = ? WHERE schedule_id = ?",
                    (next_run, now, schedule["schedule_id"]),
                )
                continue
            run_id = uuid.uuid4().hex
            # run_once executes a single overdue occurrence; all missed occurrences
            # are collapsed into one run and the next future occurrence is persisted.
            db.execute(
                "UPDATE scheduled_workflows SET next_run_at = ?, updated_at = ? WHERE schedule_id = ?",
                (next_run, now, schedule["schedule_id"]),
            )
            db.execute(
                """INSERT INTO schedule_runs(
                    run_id, schedule_id, workflow_json, scheduled_for, started_at,
                    completed_at, status, task_id, attempt, max_retries, error
                ) VALUES (?, ?, ?, ?, NULL, NULL, 'queued', '', 0, ?, '')""",
                (
                    run_id, schedule["schedule_id"], json.dumps(schedule["workflow"], ensure_ascii=False),
                    float(schedule["next_run_at"]), int(schedule["max_retries"]),
                ),
            )
            claimed.append({
                "run_id": run_id,
                "schedule_id": schedule["schedule_id"],
                "name": schedule["name"],
                "workflow": schedule["workflow"],
                "status": "queued",
                "task_id": "",
                "attempt": 0,
                "max_retries": int(schedule["max_retries"]),
            })
        db.commit()
    return claimed


def queue_schedule_run_now(schedule_id: str, now: float | None = None) -> tuple[dict[str, Any] | None, bool]:
    """Persist an immediate run; return (run, active_conflict), without moving the cadence."""
    scheduled_for = float(now if now is not None else time.time())
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        schedule = db.execute(
            "SELECT schedule_id, workflow_json, max_retries FROM scheduled_workflows WHERE schedule_id = ?",
            (schedule_id,),
        ).fetchone()
        if not schedule:
            db.rollback()
            return None, False
        active = db.execute(
            "SELECT 1 FROM schedule_runs WHERE schedule_id = ? AND status IN ('queued', 'running') LIMIT 1",
            (schedule_id,),
        ).fetchone()
        if active:
            db.rollback()
            return None, True
        try:
            workflow = json.loads(schedule["workflow_json"])
        except (TypeError, json.JSONDecodeError):
            db.rollback()
            raise ValueError("Saved schedule contains invalid workflow JSON.")
        run_id = uuid.uuid4().hex
        max_retries = int(schedule["max_retries"])
        db.execute(
            """INSERT INTO schedule_runs(
                run_id, schedule_id, workflow_json, scheduled_for, started_at,
                completed_at, status, task_id, attempt, max_retries, error
            ) VALUES (?, ?, ?, ?, NULL, NULL, 'queued', '', 0, ?, '')""",
            (run_id, schedule_id, json.dumps(workflow, ensure_ascii=False), scheduled_for, max_retries),
        )
        db.commit()
    return {
        "run_id": run_id, "schedule_id": schedule_id, "scheduled_for": scheduled_for,
        "status": "queued", "task_id": "", "attempt": 0, "max_retries": max_retries,
    }, False


def get_incomplete_schedule_runs(limit: int = 200) -> list[dict[str, Any]]:
    with _connect() as db:
        rows = db.execute(
            """SELECT schedule_runs.*, scheduled_workflows.name AS schedule_name
               FROM schedule_runs
               LEFT JOIN scheduled_workflows ON scheduled_workflows.schedule_id = schedule_runs.schedule_id
               WHERE schedule_runs.status IN ('queued', 'running')
               ORDER BY schedule_runs.scheduled_for ASC LIMIT ?""",
            (min(max(1, limit), 500),),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["name"] = item.pop("schedule_name", None) or f"Workflow {str(item.get('schedule_id', ''))[:8]}"
        try:
            item["workflow"] = json.loads(item.pop("workflow_json"))
        except (TypeError, json.JSONDecodeError):
            item["workflow"] = {}
        result.append(item)
    return result


def update_schedule_run(run_id: str, **changes: Any) -> bool:
    allowed = {"status", "task_id", "attempt", "error", "started_at", "completed_at"}
    assignments = []
    values: list[Any] = []
    for key, value in changes.items():
        if key not in allowed:
            raise ValueError(f"Unsupported schedule run field: {key}")
        assignments.append(f"{key} = ?")
        values.append(value)
    if not assignments:
        return False
    values.append(run_id)
    with _connect() as db:
        cursor = db.execute(f"UPDATE schedule_runs SET {', '.join(assignments)} WHERE run_id = ?", values)
    return cursor.rowcount > 0


def list_schedule_runs(schedule_id: str, limit: int = 50) -> list[dict[str, Any]]:
    with _connect() as db:
        rows = db.execute(
            """SELECT run_id, schedule_id, scheduled_for, started_at, completed_at,
                      status, task_id, attempt, max_retries, error
               FROM schedule_runs WHERE schedule_id = ?
               ORDER BY scheduled_for DESC LIMIT ?""",
            (schedule_id, min(max(1, limit), 200)),
        ).fetchall()
    return [dict(row) for row in rows]



def get_schedule_run_summary(limit: int = 10) -> dict[str, Any]:
    """Return aggregate schedule-run counts and a small, secret-safe recent-run list."""
    with _connect() as db:
        count_rows = db.execute(
            "SELECT status, COUNT(*) AS count FROM schedule_runs GROUP BY status"
        ).fetchall()
        recent_rows = db.execute(
            """SELECT schedule_runs.run_id, schedule_runs.schedule_id,
                      COALESCE(scheduled_workflows.name, '') AS name,
                      schedule_runs.scheduled_for, schedule_runs.started_at,
                      schedule_runs.completed_at, schedule_runs.status,
                      schedule_runs.task_id, schedule_runs.attempt
               FROM schedule_runs
               LEFT JOIN scheduled_workflows
                 ON scheduled_workflows.schedule_id = schedule_runs.schedule_id
               ORDER BY schedule_runs.scheduled_for DESC
               LIMIT ?""",
            (min(max(1, limit), 50),),
        ).fetchall()
    return {
        "counts_by_status": {str(row["status"]): int(row["count"]) for row in count_rows},
        "recent_runs": [dict(row) for row in recent_rows],
    }
