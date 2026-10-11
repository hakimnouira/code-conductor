from app.adapters.base import BaseAgentAdapter
from app.adapters.codex import CodexAdapter
from app.adapters.echo import EchoMockAdapter
from app.models.schemas import AgentInfo, AvailabilityState

class AgentRegistry:
    def __init__(self):
        self._adapters: dict[str, BaseAgentAdapter] = {}
        # Register adapters
        self.register(EchoMockAdapter())
        self.register(CodexAdapter())

    def register(self, adapter: BaseAgentAdapter) -> None:
        self._adapters[adapter.agent_id] = adapter

    def get(self, agent_id: str) -> BaseAgentAdapter | None:
        return self._adapters.get(agent_id)

    async def list_agents(self) -> list[AgentInfo]:
        infos: list[AgentInfo] = []
        for adapter in self._adapters.values():
            info = await adapter.get_info()
            infos.append(info)
        return infos

    async def resolve_agent(self, agent_id: str) -> BaseAgentAdapter | None:
        if agent_id == "auto":
            # Select the first ready non-mock adapter if available, otherwise mock
            for adapter in self._adapters.values():
                if adapter.agent_id != "mock":
                    state, _ = await adapter.check_availability()
                    if state == AvailabilityState.READY:
                        return adapter
            return self._adapters.get("mock")
        return self._adapters.get(agent_id)

_registry: AgentRegistry | None = None

def get_agent_registry() -> AgentRegistry:
    global _registry
    if _registry is None:
        _registry = AgentRegistry()
    return _registry
