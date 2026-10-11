from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from app.models.schemas import AgentCapabilities, AgentEvent, AgentInfo, AvailabilityState, TaskCreateRequest

class BaseAgentAdapter(ABC):
    """Common asynchronous adapter interface for all AI coding agents."""

    @property
    @abstractmethod
    def agent_id(self) -> str:
        """Unique identifier for this agent."""
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        """Display name of the agent."""
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        """Description of the agent and its subscription requirement."""
        pass

    @abstractmethod
    async def check_availability(self) -> tuple[AvailabilityState, str]:
        """
        Check if the agent CLI / SDK is installed and authenticated.
        Returns: (state, diagnostic_message)
        """
        pass

    @abstractmethod
    async def get_capabilities(self) -> AgentCapabilities:
        """Return the actual supported capabilities of the agent."""
        pass

    async def get_info(self) -> AgentInfo:
        """Return aggregated info for UI agent selector."""
        state, diag = await self.check_availability()
        caps = await self.get_capabilities()
        return AgentInfo(
            id=self.agent_id,
            name=self.name,
            description=self.description,
            state=state,
            diagnostic=diag,
            capabilities=caps,
        )

    @abstractmethod
    async def stream_task(self, task_id: str, request: TaskCreateRequest) -> AsyncIterator[AgentEvent]:
        """Execute a task and yield real-time events."""
        pass

    @abstractmethod
    async def cancel_task(self, task_id: str) -> bool:
        """Cancel an in-flight task execution."""
        pass
