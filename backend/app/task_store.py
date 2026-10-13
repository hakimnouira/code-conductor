"""Durable local task state and append-only task event history.

The database contains task prompts and execution metadata, but never provider API keys.
It is local-only and can be relocated with CODE_CONDUCTOR_TASK_DB.
"""
from __future__ import annotations

import json
import os
import sqlite3
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
    now = float(workflow.get("updated_at") or __import__("time").time())
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
