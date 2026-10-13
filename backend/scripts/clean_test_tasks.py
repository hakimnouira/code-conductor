import json
import os
import sqlite3
from pathlib import Path

db_path = Path.home() / ".code-conductor" / "tasks.sqlite3"
if db_path.exists():
    conn = sqlite3.connect(db_path)
    test_prefixes = [
        "Hello world from test",
        "Task to be cancelled",
        "Check status endpoint",
        "Replay task events",
        "Task history listing",
        "Long running simulation",
        "Persisted event history",
        "verify local app",
        "fix local app",
        "Inspect workspace",
        "E2E stream test prompt",
        "Mutable legacy request",
        "Still running",
        "artifact route test",
        "Idempotent scheduled task",
    ]
    rows = conn.execute("SELECT task_id, request_json FROM tasks").fetchall()
    deleted = 0
    for tid, req_str in rows:
        try:
            req = json.loads(req_str) if req_str else {}
            prompt = req.get("prompt", "")
        except Exception:
            prompt = ""
        is_test = any(prompt.startswith(tp) for tp in test_prefixes) or tid.startswith(
            ("task-browser-", "task-artifact-", "task-legacy-", "task-retry-")
        )
        if is_test:
            conn.execute("DELETE FROM task_events WHERE task_id = ?", (tid,))
            conn.execute("DELETE FROM tasks WHERE task_id = ?", (tid,))
            deleted += 1
    conn.commit()
    print(f"Cleaned {deleted} test tasks.")
    remaining = conn.execute("SELECT task_id, updated_at, request_json FROM tasks ORDER BY updated_at DESC").fetchall()
    print(f"Remaining real tasks: {len(remaining)}")
    for r in remaining:
        prompt = json.loads(r[2]).get("prompt", "") if r[2] else ""
        print(f"  [{r[0]}] {prompt[:80]}")
    conn.close()
