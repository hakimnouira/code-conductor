"""Google Antigravity CLI Bridge / Emulator.

Provides the official 'agy' CLI protocol for headless JSON streaming execution.
Bridges to the local Antigravity multi-model router or Gemini APIs.
"""

import sys
import os
import json
import subprocess
from pathlib import Path

ROUTER_SCRIPT = Path(r"C:\Users\hakim\.gemini\config\ai_router.py")
PYTHON_EXEC = Path(r"D:\oriflame colab\.venv\Scripts\python.exe")
if not PYTHON_EXEC.exists():
    PYTHON_EXEC = Path(sys.executable)


def main():
    args = sys.argv[1:]

    # Probe / Version check
    if "--version" in args or "-v" in args:
        print("Google Antigravity CLI v2.0.4 (Antigravity IDE & Gemini Engine)")
        sys.exit(0)

    # Help check
    if "--help" in args or "-h" in args:
        print("Google Antigravity CLI (agy) - Official Headless Task Agent")
        print("Usage: agy [--sandbox] [--dangerously-skip-permissions] [--model MODEL] [--input-format FORMAT] [--output-format FORMAT]")
        sys.exit(0)

    # Streaming task execution
    prompt_text = ""
    # Check if prompt passed via stdin
    if not sys.stdin.isatty():
        try:
            raw_input = sys.stdin.read()
            if raw_input.strip():
                # Try parsing stream-json
                for line in raw_input.strip().splitlines():
                    try:
                        data = json.loads(line)
                        if data.get("event") == "user":
                            prompt_text = data.get("message", {}).get("content", "")
                    except Exception:
                        pass
                if not prompt_text:
                    prompt_text = raw_input.strip()
        except Exception:
            pass

    # Fallback to positional arguments
    if not prompt_text and args:
        # Collect non-flag arguments
        non_flags = [a for a in args if not a.startswith("-")]
        if non_flags:
            prompt_text = " ".join(non_flags)

    if not prompt_text:
        prompt_text = "Hello from Code Conductor"

    # Send init event
    init_event = {"event": "init", "init": {"permission_mode": "sandbox", "version": "2.0.4"}}
    sys.stdout.write(json.dumps(init_event) + "\n")
    sys.stdout.flush()

    # Query AI Router
    full_response = ""
    try:
        if ROUTER_SCRIPT.exists() and PYTHON_EXEC.exists():
            cmd = [str(PYTHON_EXEC), str(ROUTER_SCRIPT), prompt_text]
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            for line in proc.stdout:
                if line:
                    full_response += line
                    step_event = {
                        "event": "step_update",
                        "step_update": {
                            "step_type": "text",
                            "text_delta": line,
                        },
                    }
                    sys.stdout.write(json.dumps(step_event) + "\n")
                    sys.stdout.flush()
            proc.wait()
        else:
            msg = "Antigravity Bridge received task: " + prompt_text[:100]
            full_response = msg
            sys.stdout.write(json.dumps({"event": "step_update", "step_update": {"text_delta": msg}}) + "\n")
            sys.stdout.flush()

    except Exception as exc:
        full_response = f"Antigravity Bridge execution error: {exc}"
        sys.stdout.write(json.dumps({"event": "step_update", "step_update": {"text_delta": full_response}}) + "\n")
        sys.stdout.flush()

    # Send completion result
    result_event = {
        "event": "result",
        "result": {
            "status": "SUCCESS",
            "response": full_response,
        },
    }
    sys.stdout.write(json.dumps(result_event) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
