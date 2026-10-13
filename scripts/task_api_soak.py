#!/usr/bin/env python3
"""Concurrent live-API smoke/soak test using the deterministic mock provider."""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any


def api_json(base_url: str, token: str, path: str, payload: dict | None = None) -> Any:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=data,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST" if payload is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[-1000:]
        raise RuntimeError(f"{path} returned HTTP {exc.code}: {body}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--count", type=int, default=12)
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--timeout-seconds", type=int, default=90)
    parser.add_argument("--output", type=Path, default=Path("task-api-soak-results.json"))
    args = parser.parse_args()
    token = os.environ.get("AIHUB_AUTH_TOKEN", "").strip()
    if len(token) < 16:
        parser.error("AIHUB_AUTH_TOKEN must contain at least 16 characters")
    if args.count < 1 or args.concurrency < 1 or args.timeout_seconds < 10:
        parser.error("count/concurrency must be positive and timeout-seconds must be >= 10")

    args.workspace.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    tasks: list[dict[str, Any]] = []
    for index in range(args.count):
        key = f"nightly-soak-{int(time.time())}-{index}"
        payload = {
            "prompt": f"Deterministic reliability smoke task {index}: report completion.",
            "agent_id": "mock",
            "workspace_path": str(args.workspace.resolve()),
            "github_research": False,
            "auto_verify": False,
            "permission_mode": "safe",
            "idempotency_key": key,
        }
        created = api_json(args.base_url, token, "/tasks", payload)
        tasks.append({"task_id": created["task_id"], "idempotency_key": key, "index": index})

    # Repeating an identical idempotency key must return the same durable task,
    # not start a duplicate provider pipeline.
    first = tasks[0]
    duplicate = api_json(args.base_url, token, "/tasks", {
        "prompt": "Deterministic reliability smoke task 0: report completion.",
        "agent_id": "mock",
        "workspace_path": str(args.workspace.resolve()),
        "github_research": False,
        "auto_verify": False,
        "permission_mode": "safe",
        "idempotency_key": first["idempotency_key"],
    })
    if duplicate.get("task_id") != first["task_id"]:
        raise RuntimeError("Idempotent duplicate request returned a different task ID")

    deadline = time.monotonic() + args.timeout_seconds

    def wait_for_task(task: dict[str, Any]) -> dict[str, Any]:
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last = api_json(args.base_url, token, f"/tasks/{task['task_id']}")
            if last.get("status") in {"completed", "failed", "cancelled"}:
                evidence = api_json(args.base_url, token, f"/tasks/{task['task_id']}/evidence")
                kinds = {
                    (item.get("kind"), item.get("status"))
                    for item in evidence.get("evidence", [])
                }
                gate_ok = ("completion_gate", "passed") in kinds
                return {
                    **task,
                    "status": last.get("status"),
                    "verification_status": evidence.get("verification_status"),
                    "completion_gate_passed": gate_ok,
                    "evidence_kinds": sorted({str(item.get("kind")) for item in evidence.get("evidence", [])}),
                    "error": last.get("error") or "",
                }
            time.sleep(0.25)
        return {**task, "status": "timeout", "error": "Task did not reach a terminal state before deadline"}

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(wait_for_task, task) for task in tasks]
        results = [future.result() for future in as_completed(futures)]

    results.sort(key=lambda item: item["index"])
    passed = all(
        item["status"] == "completed" and item["completion_gate_passed"]
        for item in results
    )
    summary = {
        "cases": len(results),
        "passed": sum(1 for item in results if item["status"] == "completed" and item["completion_gate_passed"]),
        "failed": sum(1 for item in results if not (item["status"] == "completed" and item["completion_gate_passed"])),
        "concurrency": args.concurrency,
        "duplicate_idempotency_key_reused_same_task": True,
        "duration_seconds": round(time.monotonic() - started, 3),
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in summary.items() if key != "results"}, indent=2))
    for item in results:
        print(json.dumps(item, ensure_ascii=False))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
