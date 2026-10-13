"""SQLite-backed execution leases prevent duplicate workers running one task.

Leases are local to the configured task database. A worker must renew its lease
while executing and may only release a lease it owns.
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path


DEFAULT_LEASE_SECONDS = 120


def _database_path() -> Path:
    override = os.environ.get("CODE_CONDUCTOR_TASK_DB", "").strip()
    path = Path(override).expanduser() if override else Path.home() / ".code-conductor" / "tasks.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _connect() -> sqlite3.Connection:
    db = sqlite3.connect(_database_path(), timeout=5.0)
    db.execute("PRAGMA busy_timeout = 5000")
    db.execute(
        """CREATE TABLE IF NOT EXISTS task_execution_leases (
            task_id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            expires_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )"""
    )
    return db


def claim_task_lease(
    task_id: str,
    owner_id: str,
    ttl_seconds: int = DEFAULT_LEASE_SECONDS,
    *,
    now: float | None = None,
) -> bool:
    """Atomically claim an unowned/expired lease, or renew this owner's lease."""
    current = time.time() if now is None else float(now)
    expires = current + max(10, int(ttl_seconds))
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT owner_id, expires_at FROM task_execution_leases WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is not None and row[0] != owner_id and float(row[1]) > current:
            return False
        db.execute(
            """INSERT INTO task_execution_leases(task_id, owner_id, expires_at, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(task_id) DO UPDATE SET
                 owner_id=excluded.owner_id,
                 expires_at=excluded.expires_at,
                 updated_at=excluded.updated_at""",
            (task_id, owner_id, expires, current),
        )
        return True


def renew_task_lease(
    task_id: str,
    owner_id: str,
    ttl_seconds: int = DEFAULT_LEASE_SECONDS,
    *,
    now: float | None = None,
) -> bool:
    """Extend a lease only if it is still owned by this worker and unexpired."""
    current = time.time() if now is None else float(now)
    with _connect() as db:
        cursor = db.execute(
            """UPDATE task_execution_leases
               SET expires_at = ?, updated_at = ?
               WHERE task_id = ? AND owner_id = ? AND expires_at > ?""",
            (current + max(10, int(ttl_seconds)), current, task_id, owner_id, current),
        )
        return cursor.rowcount == 1


def release_task_lease(task_id: str, owner_id: str) -> bool:
    """Delete a lease only when the caller still owns it."""
    with _connect() as db:
        cursor = db.execute(
            "DELETE FROM task_execution_leases WHERE task_id = ? AND owner_id = ?",
            (task_id, owner_id),
        )
        return cursor.rowcount == 1
