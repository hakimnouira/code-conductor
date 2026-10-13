"""Durable local task state and append-only task event history.

The database contains task prompts and execution metadata, but never provider API keys.
It is local-only and can be relocated with CODE_CONDUCTOR_TASK_DB.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any


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
            updated_at REAL NOT NULL
        )"""
    )
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
            updated_at REAL NOT NULL
        )"""
    )
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
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )"""
    )
    connection.commit()
    return connection


def save_task(snapshot: dict[str, Any], request_json: str) -> None:
    with _connect() as db:
        db.execute(
            """INSERT INTO tasks(task_id, request_json, snapshot_json, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(task_id) DO UPDATE SET
                 request_json=excluded.request_json,
                 snapshot_json=excluded.snapshot_json,
                 updated_at=excluded.updated_at""",
            (
                snapshot["task_id"],
                request_json,
                json.dumps(snapshot, ensure_ascii=False),
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
            "SELECT request_json, snapshot_json FROM tasks ORDER BY updated_at DESC LIMIT ?",
            (min(max(1, limit), 500),),
        ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        try:
            result.append({
                "request": json.loads(row["request_json"]),
                "snapshot": json.loads(row["snapshot_json"]),
            })
        except (TypeError, json.JSONDecodeError):
            continue
    return result



def list_workflows(limit: int = 100) -> list[dict[str, Any]]:
    with _connect() as db:
        rows = db.execute(
            """SELECT workflow_id, name, prompt, agent_id, auto_verify,
                      browser_verify_url, created_at, updated_at
               FROM workflow_presets ORDER BY updated_at DESC LIMIT ?""",
            (min(max(1, limit), 200),),
        ).fetchall()
    return [
        {
            **dict(row),
            "auto_verify": bool(row["auto_verify"]),
        }
        for row in rows
    ]


def save_workflow(workflow: dict[str, Any]) -> dict[str, Any]:
    now = float(workflow.get("updated_at") or time.time())
    created_at = float(workflow.get("created_at") or now)
    with _connect() as db:
        db.execute(
            """INSERT INTO workflow_presets(
                    workflow_id, name, prompt, agent_id, auto_verify,
                    browser_verify_url, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(workflow_id) DO UPDATE SET
                    name=excluded.name,
                    prompt=excluded.prompt,
                    agent_id=excluded.agent_id,
                    auto_verify=excluded.auto_verify,
                    browser_verify_url=excluded.browser_verify_url,
                    updated_at=excluded.updated_at""",
            (
                workflow["workflow_id"], workflow["name"], workflow["prompt"],
                workflow.get("agent_id", "auto"), int(bool(workflow.get("auto_verify", False))),
                workflow.get("browser_verify_url", ""), created_at, now,
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
                failure_streak, last_run_at, last_status, last_task_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, '', '', ?, ?)""",
            (
                schedule["schedule_id"], schedule["workflow_id"],
                json.dumps(schedule["workflow"], ensure_ascii=False),
                schedule["name"], schedule.get("workspace_path") or "",
                int(schedule["interval_minutes"]), int(bool(schedule.get("enabled", True))),
                float(schedule["next_run_at"]), int(schedule.get("max_retries", 1)),
                float(schedule["created_at"]), float(schedule["updated_at"]),
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
            run_id = uuid.uuid4().hex
            # Skip missed intervals instead of flooding the machine after a long shutdown.
            next_run = now + int(schedule["interval_minutes"]) * 60
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
                "workflow": schedule["workflow"],
                "status": "queued",
                "task_id": "",
                "attempt": 0,
                "max_retries": int(schedule["max_retries"]),
            })
        db.commit()
    return claimed


def get_incomplete_schedule_runs(limit: int = 200) -> list[dict[str, Any]]:
    with _connect() as db:
        rows = db.execute(
            """SELECT * FROM schedule_runs WHERE status IN ('queued', 'running')
               ORDER BY scheduled_for ASC LIMIT ?""",
            (min(max(1, limit), 500),),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
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
