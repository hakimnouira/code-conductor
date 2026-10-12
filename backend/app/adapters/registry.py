from app.adapters.antigravity import AntigravityAdapter
from app.adapters.base import BaseAgentAdapter
from app.adapters.codex import CodexAdapter
from app.adapters.copilot import CopilotAdapter
from app.adapters.custom_provider import CustomProviderAdapter
from app.core.provider_store import list_providers
from app.adapters.echo import EchoMockAdapter
from app.adapters.multimodel import MultiModelAdapter
from app.adapters.ollama import OllamaAdapter
from app.adapters.orchestrator import OrchestratorAdapter
from app.models.schemas import AgentInfo, AvailabilityState


class AgentRegistry:
    def __init__(self):
        self._adapters: dict[str, BaseAgentAdapter] = {}

        # 1. Register individual real worker adapters first
        self.register(CodexAdapter())
        self.register(CopilotAdapter())
        self.register(OllamaAdapter())
        self.register(AntigravityAdapter())
        self.register(MultiModelAdapter("groq"))
        self.register(MultiModelAdapter("codecraft"))
        self.register(MultiModelAdapter("apmix"))
        self.register(MultiModelAdapter("experientiallabs"))
        self.register(MultiModelAdapter("tokenharbor"))
        self.register(MultiModelAdapter("auto"))
        # User-added OpenAI-compatible endpoints; secrets remain in the OS credential store.
        for provider in list_providers():
            self.register(CustomProviderAdapter(provider))

        # Register Web Model adapters (CloakBrowser)
        try:
            from app.adapters.web_model import WebBrowserModelAdapter
            from app.web_models_store import load_web_models
            for wm in load_web_models():
                self.register(WebBrowserModelAdapter(wm))
        except Exception:
            pass

        # 2. Register multi-agent orchestrator coordinator
        self.register(OrchestratorAdapter())

        # 3. Register mock testing adapter last
        self.register(EchoMockAdapter())

    def register(self, adapter: BaseAgentAdapter) -> None:
        self._adapters[adapter.agent_id] = adapter

    def get(self, agent_id: str) -> BaseAgentAdapter | None:
        return self._adapters.get(agent_id)

    def unregister(self, agent_id: str) -> None:
        self._adapters.pop(agent_id, None)

    def reload_web_models(self) -> None:
        """Reloads all web models into the agent registry."""
        try:
            from app.adapters.web_model import WebBrowserModelAdapter
            from app.web_models_store import load_web_models
            for wm in load_web_models():
                self.register(WebBrowserModelAdapter(wm))
        except Exception:
            pass

    async def list_agents(self) -> list[AgentInfo]:
        import asyncio
        self.reload_web_models()
        return await asyncio.gather(*[adapter.get_info() for adapter in self._adapters.values()])

    async def get_ready_workers(self) -> list[BaseAgentAdapter]:
        """Returns all currently ready real worker adapters (excluding orchestrator and mock)."""
        worker_ids = [
            "codex",
            "copilot",
            "ollama",
            "antigravity",
            "groq",
            "tokenharbor",
            "codecraft",
            "apmix",
            "experientiallabs",
            "multimodel",
        ]
        ready_workers: list[BaseAgentAdapter] = []
        for wid in worker_ids:
            adapter = self._adapters.get(wid)
            if adapter:
                state, _ = await adapter.check_availability()
                if state == AvailabilityState.READY:
                    ready_workers.append(adapter)
        return ready_workers

    async def resolve_agent(self, agent_id: str, prompt: str = "") -> BaseAgentAdapter | None:
        """
        Capability-based agent routing:
        - Specific agent requested: returns that adapter directly.
        - 'auto':
            - Discovers real worker availability first.
            - If prompt implies multi-step decomposition ('refactor and test', 'plan and execute')
              AND at least one real worker is ready: routes to orchestrator.
            - Otherwise routes to the best available ready worker.
            - If NO real provider is ready: returns None (never silently disguises mock as real).
        """
        if agent_id != "auto":
            return self._adapters.get(agent_id)

        ready_workers = await self.get_ready_workers()

        # Check if prompt requires multi-agent orchestration
        prompt_lower = prompt.lower()
        needs_decomposition = any(
            phrase in prompt_lower
            for phrase in ["decompose", "subtasks", "orchestrate", "refactor and test", "plan and execute"]
        )

        if needs_decomposition and ready_workers:
            orchestrator = self._adapters.get("orchestrator")
            if orchestrator:
                return orchestrator

        # Select highest-priority ready worker
        if ready_workers:
            return ready_workers[0]

        # No real provider is ready
        return None


_registry: AgentRegistry | None = None


def get_agent_registry() -> AgentRegistry:
    global _registry
    if _registry is None:
        _registry = AgentRegistry()
    return _registry
