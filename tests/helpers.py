"""Shared fixtures for the ORBYNTH test suite.

Everything is built on the offline ``echo`` provider so the whole system — agents, quotas,
tools, plugins, HUD — is exercised without a network, an API key or a desktop session.
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orbynth.actions import build_registry, orchestration  # noqa: E402
from orbynth.actions.registry import ActionContext  # noqa: E402
from orbynth.agents.orchestrator import Orchestrator  # noqa: E402
from orbynth.config import AgentConfig, AppConfig, ModelSpec, ProviderConfig, Secrets, expand_capabilities  # noqa: E402
from orbynth.events import EventBus  # noqa: E402
from orbynth.llm.providers import ProviderRegistry  # noqa: E402
from orbynth.llm.quotas import QuotaManager  # noqa: E402
from orbynth.llm.runtime import ModelRuntime  # noqa: E402
from orbynth.memory import MemoryStore  # noqa: E402
from orbynth.plugins.forge import Forge  # noqa: E402
from orbynth.plugins.loader import PluginHost  # noqa: E402

ECHO_TAGS = ["offline", "fast", "general", "coding", "reasoning", "vision", "cheap"]


def make_config(home: str, quotas: dict[str, dict[str, int]] | None = None) -> AppConfig:
    """A config with one offline provider plus optional extra (failover) providers."""
    providers = [
        ProviderConfig(
            name="echo",
            kind="echo",
            priority=10,
            models=[
                ModelSpec(
                    model_id="offline-echo",
                    tags=ECHO_TAGS,
                    rpm=(quotas or {}).get("echo", {}).get("rpm", 1000),
                    rpd=(quotas or {}).get("echo", {}).get("rpd", 100_000),
                    tpm=(quotas or {}).get("echo", {}).get("tpm", 1_000_000),
                )
            ],
        )
    ]
    for name in ("flaky", "spare"):
        if quotas is not None and name in quotas:
            providers.append(
                ProviderConfig(
                    name=name,
                    kind="echo",
                    priority=20,
                    models=[
                        ModelSpec(
                            model_id=f"{name}-model",
                            tags=ECHO_TAGS,
                            rpm=quotas[name].get("rpm", 100),
                            rpd=quotas[name].get("rpd", 10_000),
                            tpm=quotas[name].get("tpm", 100_000),
                        )
                    ],
                )
            )
    config = AppConfig(home=home, providers=providers)
    # The standing cast, with provider-neutral models so the roster always runs offline.
    from orbynth.config import default_config

    config.roster = [
        AgentConfig(name=entry.name, role=entry.role, model="", capabilities=entry.capabilities) for entry in default_config().roster
    ]
    config.save()
    return config


class Stack:
    """Assembled system under test."""

    def __init__(self, home: str | None = None, quotas: dict[str, dict[str, int]] | None = None, registry_ready: bool = True):
        self.home = home or tempfile.mkdtemp(prefix="orbynth-test-")
        self.config = make_config(self.home, quotas)
        self.bus = EventBus()
        self.memory = MemoryStore(os.path.join(self.home, "test.db"))
        self.actions = build_registry(self.config, self.bus, self.memory)
        self.secrets = Secrets(self.config.home_path)
        self.providers = ProviderRegistry(self.config.providers, self.secrets, self.bus)
        self.quotas = QuotaManager(self.config, self.bus, persist_path=os.path.join(self.home, "quota.json"))
        self.runtime = ModelRuntime(self.config, self.providers, self.quotas, self.memory, self.bus)
        self.plugins = PluginHost(self.home, self.actions, self.bus, granted_capabilities=["*"], memory=self.memory)
        self.forge = Forge(self.plugins, self.runtime, self.actions, self.bus, self.memory, artificer_model="echo:offline-echo")
        self.orchestrator = Orchestrator(self.config, self.runtime, self.actions, self.memory, self.bus, max_parallel=4)
        orchestration.set_services(orchestration.Services(self.orchestrator, self.runtime, self.memory, self.bus, self.config))
        self.events: list = []
        self.bus.subscribe("*", self.events.append)

    def close(self) -> None:
        self.orchestrator.shutdown()
        self.memory.close()
        self.quotas.save()

    def run(self, title: str, objective: str, role: str = "operative", capabilities=None, agent=None):
        from orbynth.agents.orchestrator import WorkItem

        if agent is None:
            agent = self.orchestrator.spawn(role=role, name="TestAgent", capabilities=capabilities)
        return agent.run_task(title, objective)

    def tool(self, action_name: str, capabilities=("*",), confirmed=("*",), **kwargs):
        """Invoke an action with an explicit capability grant."""
        context = ActionContext(
            agent_id="test",
            capabilities=expand_capabilities(list(capabilities)),
            bus=self.bus,
            memory=self.memory,
            config=self.config,
            confirmed=tuple(confirmed),
        )
        return self.actions.invoke(action_name, context, **kwargs)

    def spawn(self, role: str = "operative", capabilities=None, model: str = ""):
        return self.orchestrator.spawn(role=role, name=f"Test-{role}", capabilities=capabilities, model=model)
