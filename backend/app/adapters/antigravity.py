import asyncio
import json
import os
import shutil
import subprocess
import time
from collections.abc import AsyncIterator
from pathlib import Path

from app.adapters.base import BaseAgentAdapter
from app.models.schemas import AgentCapabilities, AgentEvent, AvailabilityState, EventType, TaskCreateRequest


class AntigravityAdapter(BaseAgentAdapter):
    """Adapter for Google's documented, official Antigravity CLI (agy).

    The undocumented IDE agentapi/RPC integration is deliberately not used.
    Authentication stays inside the official CLI and its own credential store.
    """

    def __init__(self, executable: str | None = None):
        self._configured_executable = executable or os.environ.get("ANTIGRAVITY_CLI")
        self._active: dict[str, asyncio.subprocess.Process] = {}
        self._cancel: dict[str, asyncio.Event] = {}

    @property
    def agent_id(self) -> str:
        return "antigravity"

    @property
    def name(self) -> str:
        return "Google Antigravity CLI"

    @property
    def description(self) -> str:
        return "Official agy CLI using your own local Google sign-in. Supports headless task execution."

    def find_cli(self) -> str | None:
        if self._configured_executable:
            configured = Path(self._configured_executable).expanduser()
            if configured.exists():
                return str(configured)
            located = shutil.which(self._configured_executable)
            if located:
                return located
        return shutil.which("agy") or shutil.which("agy.exe")

    async def check_availability(self) -> tuple[AvailabilityState, str]:
        executable = self.find_cli()
        if not executable:
            return AvailabilityState.MISSING_CLI, (
                "Official Antigravity CLI 'agy' was not found on PATH. Install Google's CLI, "
                "sign in with an interactive agy session, then restart Code Conductor. "
                "Set ANTIGRAVITY_CLI to its full path if it is not on PATH."
            )
        try:
            proc = await asyncio.create_subprocess_exec(
                executable, "--version",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
            if proc.returncode != 0:
                detail = (stderr or stdout).decode("utf-8", errors="replace")[:220]
                return AvailabilityState.ERROR, f"agy CLI probe failed: {detail}"
            version = (stdout or stderr).decode("utf-8", errors="replace").strip()
            return AvailabilityState.READY, f"Official agy CLI found{': ' + version if version else ''}. Sign-in is checked when a task runs."
        except asyncio.TimeoutError:
            return AvailabilityState.ERROR, "agy --version timed out."
        except Exception as exc:
            return AvailabilityState.ERROR, f"Could not start agy: {exc}"

    async def get_capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(
            supports_streaming=True,
            supports_code_edits=True,
            supports_terminal_execution=True,
            supports_subagents=True,
            supported_models=["Use agy models to list models supported by your installed CLI"],
        )

    async def stream_task(self, task_id: str, request: TaskCreateRequest) -> AsyncIterator[AgentEvent]:
        executable = self.find_cli()
        if not executable:
            yield AgentEvent(task_id=task_id, event_type=EventType.ERROR, text="Official Antigravity CLI 'agy' not found.", timestamp=time.time())
            return

        workspace = Path(request.workspace_path or os.getcwd()).expanduser().resolve()
        if not workspace.is_dir():
            yield AgentEvent(task_id=task_id, event_type=EventType.ERROR, text=f"Workspace is not a directory: {workspace}", timestamp=time.time())
            return

        mode = getattr(request, "permission_mode", "safe")
        confirmed = bool(getattr(request, "god_mode_confirmed", False))
        if mode == "god" and not confirmed:
            yield AgentEvent(task_id=task_id, event_type=EventType.ERROR, text="God Mode was selected but its explicit confirmation is missing. Re-submit after confirming the high-risk mode.", timestamp=time.time())
            return

        cmd = [executable, "-p", request.prompt, "--output-format", "stream-json", "--print-timeout", "20m"]
        if request.model and request.model not in {"auto", "antigravity-default"}:
            cmd.extend(["--model", request.model])
        if mode == "safe":
            cmd.append("--sandbox")
        elif mode == "god" and confirmed:
            # Explicitly requested high-risk behavior; never enabled by default.
            cmd.append("--dangerously-skip-permissions")

        cancel_event = asyncio.Event()
        self._cancel[task_id] = cancel_event
        yield AgentEvent(
            task_id=task_id, event_type=EventType.STARTED,
            text=f"Started official agy CLI in {workspace} (permission mode: {mode}).",
            timestamp=time.time(),
            data={"permission_mode": mode, "workspace": str(workspace)},
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=str(workspace),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            self._active[task_id] = proc
            assert proc.stdout is not None
            stderr_task = asyncio.create_task(proc.stderr.read()) if proc.stderr else None
            final_status: str | None = None
            final_error = ""
            last_text = ""
            while True:
                if cancel_event.is_set():
                    await self._terminate(proc)
                    yield AgentEvent(task_id=task_id, event_type=EventType.CANCELLED, text="Antigravity CLI task cancelled.", timestamp=time.time())
                    return
                try:
                    line = await asyncio.wait_for(proc.stdout.readline(), timeout=0.25)
                except asyncio.TimeoutError:
                    if proc.returncode is not None:
                        break
                    continue
                if not line:
                    break
                decoded = line.decode("utf-8", errors="replace").strip()
                if not decoded:
                    continue
                try:
                    event = json.loads(decoded)
                except json.JSONDecodeError:
                    yield AgentEvent(task_id=task_id, event_type=EventType.LOG, text=decoded[:1000], timestamp=time.time())
                    continue

                kind = event.get("event")
                if kind == "step_update":
                    step = event.get("step_update") or {}
                    delta = step.get("text_delta")
                    if delta:
                        last_text += delta
                        yield AgentEvent(task_id=task_id, event_type=EventType.TOKEN, text=delta, timestamp=time.time())
                    tool = step.get("tool_name")
                    if step.get("step_type") == "tool" and tool:
                        yield AgentEvent(task_id=task_id, event_type=EventType.TOOL_CALL, text=f"Antigravity tool: {tool}", timestamp=time.time(), data={"tool": tool})
                elif kind == "result":
                    result = event.get("result") or {}
                    final_status = str(result.get("status", ""))
                    final_error = str(result.get("error", ""))
                    response = result.get("response")
                    if response and not last_text:
                        yield AgentEvent(task_id=task_id, event_type=EventType.TOKEN, text=str(response), timestamp=time.time())
                elif kind == "init":
                    init = event.get("init") or {}
                    yield AgentEvent(task_id=task_id, event_type=EventType.LOG, text=f"Antigravity CLI permission mode: {init.get('permission_mode', 'configured by CLI settings')}", timestamp=time.time())

            return_code = await proc.wait()
            stderr_bytes = await stderr_task if stderr_task else b""
            stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
            if stderr_text and any(word in stderr_text.lower() for word in ("permission", "approval", "soft-denied", "ask to allow")):
                yield AgentEvent(task_id=task_id, event_type=EventType.LOG, text=("Antigravity permission notice: " + stderr_text[:1200]), timestamp=time.time())
            if final_status == "SUCCESS" and return_code == 0:
                yield AgentEvent(task_id=task_id, event_type=EventType.COMPLETED, text="Antigravity task completed successfully.", timestamp=time.time(), data={"status": final_status})
            else:
                detail = final_error or stderr_text or f"agy exited with code {return_code} (status: {final_status or 'unknown'})."
                yield AgentEvent(task_id=task_id, event_type=EventType.ERROR, text=f"Antigravity did not complete successfully: {detail[:1800]}", timestamp=time.time(), data={"status": final_status, "exit_code": return_code})
        except Exception as exc:
            yield AgentEvent(task_id=task_id, event_type=EventType.ERROR, text=f"Failed to execute official agy CLI: {type(exc).__name__}: {exc}", timestamp=time.time())
        finally:
            self._active.pop(task_id, None)
            self._cancel.pop(task_id, None)

    async def _terminate(self, proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        try:
            if os.name == "nt" and proc.pid:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=5, check=False)
            else:
                proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=3)
        except Exception:
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    async def cancel_task(self, task_id: str) -> bool:
        event = self._cancel.get(task_id)
        proc = self._active.get(task_id)
        if not event or not proc:
            return False
        event.set()
        await self._terminate(proc)
        return True
