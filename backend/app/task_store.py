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
    initial_json = request_json if initial_request_json is None else initial_request_json
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


def _decode_saved_task_row(row: sqlite3.Row) -> dict[str, Any] | None:
    try:
        current_request = json.loads(row["request_json"])
        snapshot = json.loads(row["snapshot_json"])
        original_json = row["initial_request_json"] or row["request_json"]
        try:
            initial_request = json.loads(original_json)
        except (TypeError, json.JSONDecodeError):
            initial_request = current_request
        original_available = bool(row["initial_request_json"]) and bool(
            snapshot.get("original_request_available", True)
        )
        return {
            "request": current_request,
            "initial_request": initial_request,
            "initial_request_available": original_available,
            "snapshot": snapshot,
        }
    except (TypeError, json.JSONDecodeError):
        return None


def get_saved_task(task_id: str) -> dict[str, Any] | None:
    """Fetch one task by primary key without depending on the history page limit."""
    with _connect() as db:
        row = db.execute(
            "SELECT request_json, snapshot_json, initial_request_json FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    return _decode_saved_task_row(row) if row is not None else None


def get_saved_tasks(limit: int = 100) -> list[dict[str, Any]]:
    with _connect() as db:
        rows = db.execute(
            "SELECT request_json, snapshot_json, initial_request_json FROM tasks ORDER BY updated_at DESC LIMIT ?",
            (min(max(1, limit), 500),),
        ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = _decode_saved_task_row(row)
        if item is not None:
            result.append(item)
    return result


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
