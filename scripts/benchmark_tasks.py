#!/usr/bin/env python3
"""Run the deterministic coding-agent benchmark suite against a local Code Conductor API.

Example:
  python scripts/benchmark_tasks.py --workspace-root .code-conductor-benchmark-runs
The harness uses only the Python standard library and reads the local bearer token from
~/.aihub_token unless --token-file is supplied. It never sends API keys to the server.
"""
from __future__ import annotations

import argparse
import json
import shutil
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SUITE = ROOT / "backend" / "benchmarks" / "suite.json"


def request_json(base_url: str, token: str, path: str, payload: dict | None = None) -> Any:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=data,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST" if payload is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[-2000:]
        raise RuntimeError(f"API {path} returned HTTP {exc.code}: {body}") from exc


def run_suite(args: argparse.Namespace) -> int:
    suite = json.loads(args.suite.read_text(encoding="utf-8"))
    token = args.token_file.read_text(encoding="utf-8").strip()
    if not token:
        raise SystemExit(f"Bearer token file is empty: {args.token_file}")
    args.workspace_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_root = args.workspace_root / timestamp
    run_root.mkdir(parents=True, exist_ok=False)
    results: list[dict[str, Any]] = []

    for case in suite["tasks"]:
        fixture = ROOT / "backend" / "benchmarks" / "fixtures" / case["fixture"]
        if not fixture.is_dir():
            raise SystemExit(f"Fixture not found: {fixture}")
        workspace = run_root / case["id"]
        shutil.copytree(fixture, workspace)
        payload = {
            "prompt": case["prompt"],
            "agent_id": args.agent or case.get("agent_id", "auto"),
            "workspace_path": str(workspace.resolve()),
            "auto_verify": bool(case.get("auto_verify", True)),
            "github_research": False,
            "permission_mode": "safe",
        }
        started = time.monotonic()
        task = request_json(args.base_url, token, "/tasks", payload)
        task_id = task["task_id"]
        deadline = time.monotonic() + args.timeout_seconds
        state: dict[str, Any] = {}
        while time.monotonic() < deadline:
            state = request_json(args.base_url, token, f"/tasks/{task_id}")
            if state.get("status") in {"completed", "failed", "cancelled"}:
                break
            time.sleep(args.poll_seconds)
        else:
            state = state | {"status": "timeout", "error": "Benchmark polling deadline exceeded."}

        evidence: dict[str, Any] = {}
        try:
            evidence = request_json(args.base_url, token, f"/tasks/{task_id}/evidence")
        except RuntimeError as exc:
            evidence = {"verification_status": "unavailable", "error": str(exc), "evidence": []}
        expected = case.get("expected_verification_status", "passed")
        evidence_items = evidence.get("evidence", [])
        if not isinstance(evidence_items, list):
            evidence_items = []
        verified = (
            state.get("status") == "completed"
            and evidence.get("verification_status") == expected
        )
        latest_status: dict[str, str] = {}
        test_runs: list[dict[str, Any]] = []
        build_runs: list[dict[str, Any]] = []
        provider_runs: list[dict[str, Any]] = []
        for item in evidence_items:
            kind = str(item.get("kind") or "")
            if kind:
                latest_status[kind] = str(item.get("status") or "unknown")
            details = item.get("details") if isinstance(item.get("details"), dict) else {}
            if kind == "automated_tests":
                test_runs.append(details)
            elif kind == "build":
                build_runs.append(details)
            elif kind == "provider_execution":
                provider_runs.append(details)
        provenance = next(
            (item for item in reversed(evidence_items) if item.get("kind") == "workspace_provenance"),
            {},
        )
        provenance_details = provenance.get("details") if isinstance(provenance.get("details"), dict) else {}
        test_build_duration_ms = sum(
            float(details.get("duration_ms") or 0)
            for details in [*test_runs, *build_runs]
        )
        provider_stream_duration_ms = sum(
            float(details.get("duration_ms") or 0) for details in provider_runs
        )
        result = {
            "suite": suite.get("name", "unnamed"),
            "case_id": case["id"],
            "task_id": task_id,
            "agent_id": payload["agent_id"],
            "task_status": state.get("status", "unknown"),
            "verification_status": evidence.get("verification_status", "unknown"),
            "expected_verification_status": expected,
            "passed": verified,
            "duration_seconds": round(time.monotonic() - started, 3),
            "verification_duration_ms": round(test_build_duration_ms, 3),
            "provider_stream_duration_ms": round(provider_stream_duration_ms, 3),
            "provider_attempts": len(provider_runs),
            "provider_events_received": sum(int(details.get("events_received") or 0) for details in provider_runs),
            "test_attempts": len(test_runs),
            "build_attempts": len(build_runs),
            "browser_verification_status": latest_status.get("browser_verification", "not_requested"),
            "completion_gate_status": latest_status.get("completion_gate", "missing"),
            "verification_checks": latest_status,
            "workspace_snapshot_sha256": provenance_details.get("snapshot_sha256"),
            "workspace_snapshot_complete": provenance_details.get("snapshot_complete", False),
            "estimated_cost_usd": None,
            "cost_reporting": "The local task API does not expose provider cost; no estimate was fabricated.",
            "evidence": [
                {"kind": item.get("kind"), "status": item.get("status")}
                for item in evidence_items
            ],
            "error": state.get("error") or evidence.get("error") or "",
            "workspace": str(workspace.resolve()),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        results.append(result)
        print(json.dumps(result, ensure_ascii=False))

    output = run_root / "results.jsonl"
    output.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in results), encoding="utf-8")
    passed = sum(1 for item in results if item["passed"])
    summary = {
        "suite": suite.get("name", "unnamed"),
        "run_directory": str(run_root.resolve()),
        "cases": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "success_rate": round(passed / len(results), 4) if results else 0,
        "mean_duration_seconds": round(sum(item["duration_seconds"] for item in results) / len(results), 3) if results else 0,
        "results_file": str(output.resolve()),
    }
    (run_root / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0 if passed == len(results) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--token-file", type=Path, default=Path.home() / ".aihub_token")
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    parser.add_argument("--workspace-root", type=Path, default=ROOT / ".code-conductor-benchmark-runs")
    parser.add_argument("--agent", help="Override the provider for every case (for provider comparisons).")
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--poll-seconds", type=float, default=2)
    args = parser.parse_args()
    if args.timeout_seconds < 10 or args.poll_seconds <= 0:
        parser.error("timeout-seconds must be >= 10 and poll-seconds must be > 0")
    return run_suite(args)


if __name__ == "__main__":
    raise SystemExit(main())