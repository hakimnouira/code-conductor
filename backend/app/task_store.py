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


# Durable scheduler state. Jobs are separate from reusable workflow presets: a preset
# is a template, while a scheduled job has a clock, retry policy and run history.
def _ensure_scheduler_tables(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS scheduled_jobs (
        job_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        schedule_type TEXT NOT NULL CHECK(schedule_type IN ('once', 'interval')),
        interval_seconds INTEGER,
        next_run_at REAL NOT NULL,
        retry_at REAL,
        retry_count INTEGER NOT NULL DEFAULT 0,
        max_retries INTEGER NOT NULL DEFAULT 2,
        enabled INTEGER NOT NULL DEFAULT 1,
        last_run_at REAL,
        last_status TEXT NOT NULL DEFAULT 'scheduled',
        last_task_id TEXT,
        last_error TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS scheduled_runs (
        run_id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL,
        scheduled_at REAL NOT NULL,
        started_at REAL NOT NULL,
        finished_at REAL,
        status TEXT NOT NULL,
        task_id TEXT,
        error TEXT,
        result_json TEXT
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_scheduled_jobs_due ON scheduled_jobs(enabled, next_run_at)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_scheduled_runs_job_started ON scheduled_runs(job_id, started_at DESC)")


def _decode_scheduled_job(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    try:
        item["payload"] = json.loads(item.pop("payload_json"))
    except (TypeError, json.JSONDecodeError):
        item["payload"] = {}
    item["enabled"] = bool(item["enabled"])
    return item


def create_scheduled_job(job: dict[str, Any]) -> dict[str, Any]:
    with _connect() as db:
        _ensure_scheduler_tables(db)
        db.execute(
            """INSERT INTO scheduled_jobs(
                job_id, name, payload_json, schedule_type, interval_seconds,
                next_run_at, retry_at, retry_count, max_retries, enabled,
                last_run_at, last_status, last_task_id, last_error, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, NULL, 'scheduled', NULL, NULL, ?, ?)""",
            (
                job["job_id"], job["name"], json.dumps(job.get("payload", {}), ensure_ascii=False),
                job["schedule_type"], job.get("interval_seconds"), job["next_run_at"],
                job.get("retry_count", 0), job.get("max_retries", 2), int(bool(job.get("enabled", True))),
                job["created_at"], job["updated_at"],
            ),
        )
        row = db.execute("SELECT * FROM scheduled_jobs WHERE job_id = ?", (job["job_id"],)).fetchone()
    return _decode_scheduled_job(row) or dict(job)


def get_scheduled_job(job_id: str) -> dict[str, Any] | None:
    with _connect() as db:
        _ensure_scheduler_tables(db)
        row = db.execute("SELECT * FROM scheduled_jobs WHERE job_id = ?", (job_id,)).fetchone()
    return _decode_scheduled_job(row)


def list_scheduled_jobs(limit: int = 200) -> list[dict[str, Any]]:
    with _connect() as db:
        _ensure_scheduler_tables(db)
        rows = db.execute(
            "SELECT * FROM scheduled_jobs ORDER BY enabled DESC, next_run_at ASC LIMIT ?",
            (min(max(1, limit), 500),),
        ).fetchall()
    return [_decode_scheduled_job(row) for row in rows if row is not None]


def update_scheduled_job(job_id: str, changes: dict[str, Any]) -> dict[str, Any] | None:
    allowed = {
        "enabled", "next_run_at", "retry_at", "retry_count", "last_run_at",
        "last_status", "last_task_id", "last_error", "updated_at",
    }
    clean = {key: value for key, value in changes.items() if key in allowed}
    if not clean:
        return get_scheduled_job(job_id)
    assignments = ", ".join(f"{key} = ?" for key in clean)
    values = [int(value) if key == "enabled" else value for key, value in clean.items()]
    with _connect() as db:
        _ensure_scheduler_tables(db)
        db.execute(f"UPDATE scheduled_jobs SET {assignments} WHERE job_id = ?", (*values, job_id))
        row = db.execute("SELECT * FROM scheduled_jobs WHERE job_id = ?", (job_id,)).fetchone()
    return _decode_scheduled_job(row)


def set_scheduled_job_enabled(job_id: str, enabled: bool) -> dict[str, Any] | None:
    return update_scheduled_job(
        job_id,
        {"enabled": enabled, "updated_at": time.time(), "retry_at": None if not enabled else None},
    )


def delete_scheduled_job(job_id: str) -> bool:
    with _connect() as db:
        _ensure_scheduler_tables(db)
        db.execute("DELETE FROM scheduled_runs WHERE job_id = ?", (job_id,))
        cursor = db.execute("DELETE FROM scheduled_jobs WHERE job_id = ?", (job_id,))
    return cursor.rowcount > 0


def claim_due_scheduled_jobs(now: float) -> list[dict[str, Any]]:
    """Atomically claim due jobs, advance recurring schedules, and prevent overlapping runs."""
    claimed: list[dict[str, Any]] = []
    with _connect() as db:
        _ensure_scheduler_tables(db)
        db.execute("BEGIN IMMEDIATE")
        rows = db.execute(
            """SELECT j.* FROM scheduled_jobs j
               WHERE j.enabled = 1
                 AND (COALESCE(j.retry_at, j.next_run_at) <= ?)
                 AND NOT EXISTS (
                   SELECT 1 FROM scheduled_runs r
                   WHERE r.job_id = j.job_id AND r.status = 'running'
                 )
               ORDER BY COALESCE(j.retry_at, j.next_run_at) ASC LIMIT 50""",
            (now,),
        ).fetchall()
        for row in rows:
            item = _decode_scheduled_job(row)
            if not item:
                continue
            due_at = item.get("retry_at") if item.get("retry_at") is not None and item["retry_at"] <= now else item["next_run_at"]
            if item["schedule_type"] == "interval" and (item.get("retry_at") is None or item["retry_at"] > now):
                db.execute(
                    "UPDATE scheduled_jobs SET next_run_at = ?, last_run_at = ?, last_status = 'running', retry_at = NULL, updated_at = ? WHERE job_id = ?",
                    (now + int(item["interval_seconds"]), now, now, item["job_id"]),
                )
            else:
                db.execute(
                    "UPDATE scheduled_jobs SET enabled = 0, last_run_at = ?, last_status = 'running', retry_at = NULL, updated_at = ? WHERE job_id = ?",
                    (now, now, item["job_id"]),
                )
            item["due_at"] = due_at
            item["last_status"] = "running"
            item["last_run_at"] = now
            if item["schedule_type"] == "once":
                item["enabled"] = False
            claimed.append(item)
        db.commit()
    return claimed


def start_scheduled_run(run: dict[str, Any]) -> None:
    with _connect() as db:
        _ensure_scheduler_tables(db)
        db.execute(
            """INSERT INTO scheduled_runs(run_id, job_id, scheduled_at, started_at, status, task_id, error, result_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run["run_id"], run["job_id"], run["scheduled_at"], run["started_at"],
                run.get("status", "running"), run.get("task_id"), run.get("error"),
                json.dumps(run.get("result"), ensure_ascii=False) if run.get("result") is not None else None,
            ),
        )


def finish_scheduled_run(run: dict[str, Any], *, retry_at: float | None, retry_count: int) -> None:
    status_value = str(run.get("status", "failed"))
    error = run.get("error")
    result_json = json.dumps(run.get("result"), ensure_ascii=False) if run.get("result") is not None else None
    with _connect() as db:
        _ensure_scheduler_tables(db)
        db.execute(
            """UPDATE scheduled_runs SET finished_at = ?, status = ?, task_id = ?, error = ?, result_json = ?
               WHERE run_id = ?""",
            (run.get("finished_at", time.time()), status_value, run.get("task_id"), error, result_json, run["run_id"]),
        )
        db.execute(
            """UPDATE scheduled_jobs SET last_status = ?, last_task_id = ?, last_error = ?,
               retry_count = ?, retry_at = ?, enabled = CASE WHEN ? IS NOT NULL THEN 1 ELSE enabled END,
               updated_at = ? WHERE job_id = ?""",
            (status_value, run.get("task_id"), error, retry_count, retry_at, retry_at, time.time(), run["job_id"]),
        )


def list_scheduled_runs(limit: int = 100, job_id: str | None = None) -> list[dict[str, Any]]:
    with _connect() as db:
        _ensure_scheduler_tables(db)
        if job_id:
            rows = db.execute(
                "SELECT * FROM scheduled_runs WHERE job_id = ? ORDER BY started_at DESC LIMIT ?",
                (job_id, min(max(1, limit), 500)),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM scheduled_runs ORDER BY started_at DESC LIMIT ?",
                (min(max(1, limit), 500),),
            ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["result"] = json.loads(item.pop("result_json")) if item.get("result_json") else None
        except (TypeError, json.JSONDecodeError):
            item["result"] = None
        result.append(item)
    return result


def recover_interrupted_scheduled_runs(now: float) -> None:
    """Turn orphaned in-progress runs into visible failures and schedule bounded retries."""
    with _connect() as db:
        _ensure_scheduler_tables(db)
        db.execute("BEGIN IMMEDIATE")
        rows = db.execute("SELECT run_id, job_id FROM scheduled_runs WHERE status = 'running'").fetchall()
        for row in rows:
            db.execute(
                "UPDATE scheduled_runs SET status = 'failed', error = ?, finished_at = ? WHERE run_id = ?",
                ("Backend restarted during scheduled execution; task status must be checked before retry.", now, row["run_id"]),
            )
            job = db.execute("SELECT retry_count, max_retries, schedule_type FROM scheduled_jobs WHERE job_id = ?", (row["job_id"],)).fetchone()
            if not job:
                continue
            retries = int(job["retry_count"])
            can_retry = retries < int(job["max_retries"])
            db.execute(
                """UPDATE scheduled_jobs SET last_status = 'failed',
                   last_error = ?, retry_count = ?, retry_at = ?, enabled = CASE WHEN ? THEN 1 ELSE enabled END,
                   updated_at = ? WHERE job_id = ?""",
                (
                    "Backend restarted during scheduled execution.",
                    retries + 1 if can_retry else retries,
                    now + 60 if can_retry else None,
                    1 if can_retry else 0,
                    now, row["job_id"],
                ),
            )
        db.commit()
