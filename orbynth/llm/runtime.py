"""Model runtime: model selection, budgets, and failure-tolerant execution.

Two guarantees this module exists to provide:

1. **A task never dies because one model ran out of quota.** Every request carries a
   candidate chain built from the agent's preferred model outwards to every other usable
   model. A quota error, an auth error or a flaky gateway moves the call down the chain
   and a ``model.fallback`` event tells the HUD and the Steward what happened.
2. **A model swap never loses context.** Because conversation and memory live against the
   *agent id* (see :mod:`orbynth.memory`), swapping models is a label change on the agent
   record plus an event. ``transplant()`` performs exactly that and nothing else.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Sequence

from ..config import AppConfig, ModelSpec
from ..events import MODEL_FALLBACK, PROVIDER_ERROR, EventBus
from ..memory import MemoryStore
from .providers import LLMRequest, LLMResponse, ProviderError, ProviderRegistry, Usage
from .quotas import QuotaManager


@dataclass(slots=True)
class Requirements:
    """What a task needs from a model."""

    tools: bool = True
    vision: bool = False
    audio_live: bool = False
    tags: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    min_context: int = 0

    def matches(self, spec: ModelSpec) -> bool:
        if self.tools and not spec.supports_tools:
            return False
        if self.vision and not spec.supports_vision:
            return False
        if self.audio_live and not spec.supports_audio_live:
            return False
        if self.min_context and spec.context_window < self.min_context:
            return False
        if self.tags and not set(self.tags) & set(spec.tags):
            return False
        if spec.qualified in self.exclude:
            return False
        return True


@dataclass(slots=True)
class Budget:
    max_actions: int = 200
    max_wall_seconds: float = 1800.0
    max_tokens: int = 2_000_000
    actions_used: int = 0
    tokens_used: int = 0
    started_at: float = 0.0

    def start(self, now: float) -> None:
        self.started_at = now

    def charge_action(self, now: float) -> None:
        self.actions_used += 1
        self.check(now)

    def charge_tokens(self, tokens: int, now: float) -> None:
        self.tokens_used += max(0, tokens)
        self.check(now)

    def check(self, now: float) -> None:
        if self.actions_used > self.max_actions:
            raise BudgetExceeded(f"action budget exceeded ({self.actions_used}/{self.max_actions})")
        if self.tokens_used > self.max_tokens:
            raise BudgetExceeded(f"token budget exceeded ({self.tokens_used}/{self.max_tokens})")
        if self.started_at and now - self.started_at > self.max_wall_seconds:
            raise BudgetExceeded(f"wall-clock budget exceeded ({self.max_wall_seconds:.0f}s)")

    def to_json(self, now: float) -> dict[str, Any]:
        return {
            "actions": f"{self.actions_used}/{self.max_actions}",
            "tokens": f"{self.tokens_used}/{self.max_tokens}",
            "elapsed_s": round(max(0.0, now - self.started_at), 1) if self.started_at else 0.0,
            "wall_limit_s": self.max_wall_seconds,
        }


class BudgetExceeded(RuntimeError):
    """Raised when an agent burns through the resources its parent granted it."""


class AllModelsUnavailable(RuntimeError):
    """Raised when every candidate model refused or was exhausted."""

    def __init__(self, message: str, attempts: list[dict[str, Any]]):
        super().__init__(message)
        self.attempts = attempts


@dataclass(slots=True)
class RunResult:
    response: LLMResponse
    model: str
    attempts: list[dict[str, Any]] = field(default_factory=list)

    @property
    def fell_back(self) -> bool:
        """True when at least one candidate was skipped or failed before this one served."""
        return bool(self.attempts)


class ModelRuntime:
    """Turns "run this prompt as this agent" into a concrete, resilient model call."""

    def __init__(
        self,
        config: AppConfig,
        registry: ProviderRegistry,
        quotas: QuotaManager,
        memory: MemoryStore | None = None,
        bus: EventBus | None = None,
        clock: Callable[[], float] | None = None,
    ):
        self.config = config
        self.registry = registry
        self.quotas = quotas
        self.memory = memory
        self.bus = bus or registry.bus
        import time as _time

        self.clock = clock or _time.time
        self.budgets: dict[str, Budget] = {}

    # ----------------------------------------------------------------------------------
    # Model selection
    # ----------------------------------------------------------------------------------
    def candidates(self, prefer: Sequence[str] = (), requirements: Requirements | None = None) -> list[str]:
        """Ordered candidate list: preferred first, then best-fit available models."""
        requirements = requirements or Requirements()
        specs = [s for s in self.config.all_models() if requirements.matches(s) and self.registry.ready(s.provider)]
        by_name = {s.qualified: s for s in specs}
        ordered: list[str] = [q for q in prefer if q in by_name]
        # Unknown/unsupported preferences are dropped, not fatal.
        rest = [s for s in specs if s.qualified not in ordered]
        rest.sort(
            key=lambda s: (
                self.config.provider(s.provider).priority if self.config.provider(s.provider) else 99,
                -self.quotas.headroom(self.quotas.models[s.qualified])
                if s.qualified in self.quotas.models
                else 0.0,
            )
        )
        ordered.extend(s.qualified for s in rest)
        return ordered

    def budget_for(self, agent_id: str, grant: Any = None) -> Budget:
        """Create (once) the resource budget for an agent.

        ``grant`` is either an :class:`~orbynth.config.AgentConfig`, an
        :class:`~orbynth.agents.identity.AgentIdentity`, or the plain dict the identity carries —
        all three are supported so a budget declared anywhere is actually enforced.
        """
        budget = self.budgets.get(agent_id)
        if budget is None:
            if isinstance(grant, dict):
                source = grant.get("budget", grant)
                limits = source if isinstance(source, dict) else {}
            else:
                limits = {
                    "max_actions": getattr(grant, "max_actions", None),
                    "max_wall_seconds": getattr(grant, "max_wall_seconds", None),
                    "max_tokens": getattr(grant, "max_tokens", None),
                }
                nested = getattr(grant, "budget", None)
                if isinstance(nested, dict):
                    limits = {**nested, **{k: v for k, v in limits.items() if v is not None}}
            budget = Budget(
                max_actions=int(limits["max_actions"]) if limits.get("max_actions") is not None else 200,
                max_wall_seconds=float(limits["max_wall_seconds"]) if limits.get("max_wall_seconds") is not None else 1800.0,
                max_tokens=int(limits["max_tokens"]) if limits.get("max_tokens") is not None else 2_000_000,
            )
            budget.start(self.clock())
            self.budgets[agent_id] = budget
        return budget

    # ----------------------------------------------------------------------------------
    # Execution
    # ----------------------------------------------------------------------------------
    def run(
        self,
        request: LLMRequest,
        agent_id: str = "sovereign",
        prefer: Sequence[str] = (),
        requirements: Requirements | None = None,
        charge_budget: bool = True,
        max_attempts: int = 6,
    ) -> RunResult:
        chain = self.candidates(prefer, requirements)[:max_attempts]
        if not chain:
            raise AllModelsUnavailable(
                "no usable model: configure a provider key or add the built-in echo provider",
                [],
            )
        attempts: list[dict[str, Any]] = []
        budget = self.budget_for(agent_id) if charge_budget else None
        if budget is not None:
            budget.check(self.clock())
        for index, qualified in enumerate(chain):
            decision = self.quotas.check(qualified)
            if not decision.allowed:
                attempts.append({"model": qualified, "skipped": decision.reason, "retry_after": decision.retry_after})
                continue
            try:
                response = self.registry.complete(qualified, request, agent_id)
            except ProviderError as exc:
                cooldown = self.quotas.note_error(qualified, exc.kind, exc.retry_after, str(exc))
                self.bus.emit(
                    PROVIDER_ERROR,
                    source=agent_id,
                    severity="warning",
                    model=qualified,
                    kind=exc.kind,
                    status=exc.status,
                    cooldown_s=round(cooldown, 1),
                    message=str(exc)[:300],
                )
                attempts.append({"model": qualified, "error": exc.kind, "message": str(exc)[:200]})
                continue
            except Exception as exc:  # adapter bug or transport surprise — still fail over
                self.quotas.note_error(qualified, "unknown", 0.0, repr(exc))
                attempts.append({"model": qualified, "error": "unknown", "message": repr(exc)[:200]})
                continue

            self.quotas.record(qualified, response.usage.prompt_tokens, response.usage.completion_tokens)
            if budget is not None:
                budget.charge_tokens(response.usage.total_tokens or 0, self.clock())
            if index > 0:
                self._announce_fallback(agent_id, chain[0], qualified, attempts)
            return RunResult(response=response, model=qualified, attempts=attempts)
        raise AllModelsUnavailable(
            f"all {len(chain)} candidate models were unavailable for {agent_id}", attempts
        )

    def stream(
        self,
        request: LLMRequest,
        agent_id: str = "sovereign",
        prefer: Sequence[str] = (),
        requirements: Requirements | None = None,
        max_attempts: int = 4,
    ) -> Iterator[str]:
        """Yield text chunks, transparently failing over before the first chunk arrives."""
        chain = self.candidates(prefer, requirements)[:max_attempts]
        attempts: list[dict[str, Any]] = []
        for index, qualified in enumerate(chain):
            if not self.quotas.check(qualified).allowed:
                continue
            produced = False
            try:
                for chunk in self.registry.stream(qualified, request, agent_id):
                    produced = True
                    yield chunk
                if produced:
                    if index > 0:
                        self._announce_fallback(agent_id, chain[0], qualified, attempts)
                    return
                raise ProviderError("stream produced no content", kind="server", provider=qualified)
            except ProviderError as exc:
                if produced:  # partial output already delivered: surface it, do not restart
                    raise
                self.quotas.note_error(qualified, exc.kind, exc.retry_after, str(exc))
                attempts.append({"model": qualified, "error": exc.kind})
                continue

    def _announce_fallback(self, agent_id: str, intended: str, actual: str, attempts: list[dict[str, Any]]) -> None:
        self.bus.emit(
            MODEL_FALLBACK,
            source="runtime",
            severity="notice",
            agent=agent_id,
            intended=intended,
            actual=actual,
            attempts=attempts,
            context_preserved=True,
        )
        if self.memory is not None:
            self.memory.remember(
                agent_id,
                f"model fallback: {intended} -> {actual} (context preserved, same agent identity)",
                kind="runtime",
                tags=["fallback", "quota"],
                importance=0.7,
            )
            record = self.memory.get_agent(agent_id)
            if record is not None:
                self.memory.set_agent_status(agent_id, record.status, model=actual)

    # ----------------------------------------------------------------------------------
    # Steward API
    # ----------------------------------------------------------------------------------
    def transplant(self, agent_id: str, new_model: str, reason: str = "") -> dict[str, Any]:
        """Move an agent onto another model while keeping identity, memory and transcript."""
        if self.memory is None:
            raise RuntimeError("transplant requires a memory store")
        record = self.memory.get_agent(agent_id)
        if record is None:
            raise KeyError(f"unknown agent: {agent_id}")
        previous = record.model
        self.memory.set_agent_status(agent_id, "running", model=new_model)
        self.memory.remember(
            agent_id,
            f"transplanted {previous} -> {new_model}; reason: {reason or 'quota/failure failover'}",
            kind="runtime",
            tags=["transplant"],
            importance=0.9,
        )
        self.memory.post_board(
            record.team_id or "fleet",
            "transplant",
            f"{record.name} moved from {previous} to {new_model} ({reason or 'failover'}). "
            "Memory and task context retained under the same agent id; work continues.",
            author="Steward",
        )
        event = self.bus.emit(
            MODEL_FALLBACK,
            source="steward",
            severity="notice",
            agent=agent_id,
            agent_name=record.name,
            intended=previous,
            actual=new_model,
            reason=reason,
            context_preserved=True,
        )
        return {
            "agent": record.name,
            "agent_id": agent_id,
            "from": previous,
            "to": new_model,
            "reason": reason,
            "memory_preserved": True,
            "event": event.id,
        }

    def health_report(self) -> dict[str, Any]:
        """Fleet-level view the Steward reasons over: quota + readiness per model."""
        rows = self.quotas.snapshot()
        return {
            "models": rows,
            "ready": [r["model"] for r in rows if self.registry.ready(r["model"].split(":", 1)[0])],
            "exhausted": [r["model"] for r in rows if r["status"] in {"exhausted", "cooling"}],
            "budgets": {a: b.to_json(self.clock()) for a, b in self.budgets.items()},
        }

    def usage_ledger(self) -> dict[str, Any]:
        totals = Usage()
        for quota in self.quotas.models.values():
            totals.prompt_tokens += quota.total_tokens
        return {"total_tokens": totals.prompt_tokens, "per_model": self.quotas.snapshot()}

    @staticmethod
    def dumps(obj: Any) -> str:
        return json.dumps(obj, ensure_ascii=False, default=str)
