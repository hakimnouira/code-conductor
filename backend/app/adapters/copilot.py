import asyncio
import os
import time
from collections.abc import AsyncIterator
from app.adapters.base import BaseAgentAdapter
from app.models.schemas import (
    AgentCapabilities,
    AgentEvent,
    AvailabilityState,
    EventType,
    TaskCreateRequest,
)

class CopilotAdapter(BaseAgentAdapter):
    """GitHub Copilot adapter reusing official GitHub Copilot SDK and existing subscription."""

    def __init__(self, github_token: str | None = None):
        self._github_token = github_token
        self._active_sessions: dict[str, asyncio.Event] = {}

    @property
    def agent_id(self) -> str:
        return "copilot"

    @property
    def name(self) -> str:
        return "GitHub Copilot SDK"

    @property
    def description(self) -> str:
        return "Official GitHub Copilot agent runtime reusing existing Copilot Individual/Business subscription without separate API keys."

    async def check_availability(self) -> tuple[AvailabilityState, str]:
        try:
            import copilot
            from copilot import CopilotClient
        except ImportError:
            return (
                AvailabilityState.MISSING_CLI,
                "GitHub Copilot SDK package is not installed. Install with: pip install github-copilot-sdk",
            )

        try:
            token = self._github_token or os.environ.get("GITHUB_TOKEN")
            client_kwargs = {"use_logged_in_user": True}
            if token:
                client_kwargs = {"github_token": token, "use_logged_in_user": False}

            async with CopilotClient(**client_kwargs) as client:
                await client.ping("availability-check")
                auth = await client.get_auth_status()

                if getattr(auth, "isAuthenticated", False):
                    login = getattr(auth, "login", "authenticated_user")
                    return (
                        AvailabilityState.READY,
                        f"GitHub Copilot is ready (authenticated as '{login}') using active Copilot subscription.",
                    )
                else:
                    msg = getattr(auth, "statusMessage", "Not authenticated")
                    return (
                        AvailabilityState.UNAUTHENTICATED,
                        f"GitHub Copilot runtime is active, but not signed in ({msg}). "
                        "Please authenticate with GitHub CLI ('gh auth login') or set GITHUB_TOKEN to reuse your subscription.",
                    )
        except Exception as e:
            err_str = str(e)
            if "Not authenticated" in err_str or "-32603" in err_str:
                return (
                    AvailabilityState.UNAUTHENTICATED,
                    "GitHub Copilot runtime is available, but no active authenticated user session was detected. "
                    "Run 'gh auth login' or sign into GitHub CLI/Desktop to link your existing Copilot subscription.",
                )
            return (
                AvailabilityState.ERROR,
                f"GitHub Copilot runtime error: {err_str}",
            )

    async def get_capabilities(self) -> AgentCapabilities:
        models = ["gpt-4o", "gpt-5", "claude-3.7-sonnet", "claude-sonnet-4.5"]
        try:
            from copilot import CopilotClient
            token = self._github_token or os.environ.get("GITHUB_TOKEN")
            client_kwargs = {"use_logged_in_user": True}
            if token:
                client_kwargs = {"github_token": token, "use_logged_in_user": False}

            async with CopilotClient(**client_kwargs) as client:
                m_list = await client.list_models()
                if m_list:
                    models = [getattr(m, "id", str(m)) for m in m_list]
        except Exception:
            pass

        return AgentCapabilities(
            supports_streaming=True,
            supports_code_edits=True,
            supports_terminal_execution=False,
            supports_subagents=False,
            supported_models=models,
        )

    async def stream_task(self, task_id: str, request: TaskCreateRequest) -> AsyncIterator[AgentEvent]:
        state, diag = await self.check_availability()
        if state != AvailabilityState.READY:
            yield AgentEvent(
                task_id=task_id,
                event_type=EventType.ERROR,
                text=f"GitHub Copilot unavailable: {diag}",
                timestamp=time.time(),
            )
            return

        try:
            from copilot import CopilotClient, PermissionHandler
            from copilot.session_events import AssistantMessageData, SessionIdleData
        except ImportError as e:
            yield AgentEvent(
                task_id=task_id,
                event_type=EventType.ERROR,
                text=f"Copilot SDK import error: {e}",
                timestamp=time.time(),
            )
            return

        cancel_event = asyncio.Event()
        self._active_sessions[task_id] = cancel_event

        yield AgentEvent(
            task_id=task_id,
            event_type=EventType.STARTED,
            text=f"GitHub Copilot task started with model '{request.model or 'auto'}'.",
            timestamp=time.time(),
        )

        token = self._github_token or os.environ.get("GITHUB_TOKEN")
        client_kwargs = {"use_logged_in_user": True}
        if token:
            client_kwargs = {"github_token": token, "use_logged_in_user": False}

        try:
            async with CopilotClient(**client_kwargs) as client:
                model_name = request.model if request.model and request.model != "auto" else "gpt-4o"
                async with await client.create_session(
                    on_permission_request=PermissionHandler.approve_all,
                    model=model_name,
                ) as session:
                    done_event = asyncio.Event()
                    token_queue: asyncio.Queue[str | None] = asyncio.Queue()

                    def on_event(event):
                        data = getattr(event, "data", None)
                        if isinstance(data, AssistantMessageData):
                            token_queue.put_nowait(data.content)
                        elif isinstance(data, SessionIdleData):
                            done_event.set()
                            token_queue.put_nowait(None)

                    session.on(on_event)
                    await session.send(request.prompt)

                    while not done_event.is_set():
                        if cancel_event.is_set():
                            yield AgentEvent(
                                task_id=task_id,
                                event_type=EventType.CANCELLED,
                                text="Copilot task cancelled by user request.",
                                timestamp=time.time(),
                            )
                            return

                        try:
                            token_text = await asyncio.wait_for(token_queue.get(), timeout=0.1)
                            if token_text is None:
                                break
                            yield AgentEvent(
                                task_id=task_id,
                                event_type=EventType.TOKEN,
                                text=token_text,
                                timestamp=time.time(),
                            )
                        except asyncio.TimeoutError:
                            continue

                    yield AgentEvent(
                        task_id=task_id,
                        event_type=EventType.COMPLETED,
                        text="GitHub Copilot task completed successfully.",
                        timestamp=time.time(),
                    )
        except Exception as e:
            yield AgentEvent(
                task_id=task_id,
                event_type=EventType.ERROR,
                text=f"Copilot session error: {str(e)}",
                timestamp=time.time(),
            )
        finally:
            self._active_sessions.pop(task_id, None)

    async def cancel_task(self, task_id: str) -> bool:
        event = self._active_sessions.get(task_id)
        if event:
            event.set()
            return True
        return False
