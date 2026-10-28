"""The orchestrator: who does what, with which model, and what they report back.

Three levels of management, exactly as specified:

* **Sovereign** (rank 0) — the main agent. Owns the fleet and the user relationship. Picks a
  model for any role at any time; spawns, re-tasks and terminates anyone.
* **Steward** (rank 1) — the manager. Never does the task itself. Watches quotas, provider
  errors and stalled agents; when an agent is throttled or failing, it transplants that
  agent onto another model — same agent id, same memory, same transcript, work continues —
  and escalates to the Sovereign only when its own authority is not enough.
* **Foreman + Operatives** — a Foreman owns one long objective, splits it, spawns the
  Operatives it needs, runs the independent parts **in parallel** through a thread pool,
  verifies their reports and returns one consolidated report upward.

Everything is recorded: tasks in SQLite, reports on the team board, memos in each agent's
own memory. A parent that wants detail asks memory; the report itself stays five lines.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

from ..actions.registry import ActionContext, ActionRegistry
from ..config import AgentConfig, AppConfig, expand_capabilities
from ..events import (
    AGENT_MESSAGE,
    AGENT_SPAWNED,
    AGENT_TERMINATED,
    LOG,
    MODEL_FALLBACK,
    QUOTA_EXHAUSTED,
    QUOTA_WARNING,
    TASK_ASSIGNED,
    TASK_CREATED,
    TASK_DONE,
    EventBus,
)
from ..llm.runtime import ModelRuntime, Requirements
from ..memory import MemoryStore
from .base import Agent, AgentReport
from .identity import AgentIdentity
from .roles import role_template


@dataclass
class Team:
    team_id: str
    objective: str
    foreman_id: str
    member_ids: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def to_json(self) -> dict[str, Any]:
        return {
            "team_id": self.team_id,
            "objective": self.objective[:300],
            "foreman": self.foreman_id,
            "members": self.member_ids,
            "age_s": round(time.time() - self.created_at, 1),
        }


@dataclass
class WorkItem:
    """One unit of work handed to an Operative."""

    title: str
    objective: str
    capabilities: list[str] = field(default_factory=list)
    role: str = "operative"
    name: str = ""
    context: str = ""
    model: str = ""
    depends_on: list[str] = field(default_factory=list)
    #: Success criteria written *before* the work starts (contract 22.4). Either explicit
    #: ``{"kind", "target", "expected", "note"}`` dicts, or paths the task must produce.
    criteria: list[dict[str, Any]] = field(default_factory=list)
    expected_outputs: list[str] = field(default_factory=list)
    #: "interactive" work is never held back by the governor; "background" work yields to
    #: games, calls and presentations (contract 22.11).
    work_class: str = "interactive"


class Orchestrator:
    """Spawns agents, runs teams, and keeps the fleet alive."""

    def __init__(
        self,
        config: AppConfig,
        runtime: ModelRuntime,
        actions: ActionRegistry,
        memory: MemoryStore,
        bus: EventBus,
        max_parallel: int | None = None,
        governor: Any = None,
        verifier: Any = None,
        preflight: Any = None,
    ):
        self.config = config
        self.runtime = runtime
        self.actions = actions
        self.memory = memory
        self.bus = bus
        #: Operational layer, optional so a bare Orchestrator still works in tests and as an
        #: embedded library. When present, tasks get contracts, verdicts and courtesy.
        self.governor = governor
        self.verifier = verifier
        self.preflight = preflight
        self.max_parallel = max_parallel or config.safety.max_parallel_agents
        self.executor = ThreadPoolExecutor(max_workers=self.max_parallel, thread_name_prefix="orbynth-agent")
        self.agents: dict[str, Agent] = {}
        self.teams: dict[str, Team] = {}
        self._lock = threading.RLock()
        self._steward: Agent | None = None

    # ----------------------------------------------------------------------------------
    # Spawning
    # ----------------------------------------------------------------------------------
    def bootstrap_roster(self) -> list[Agent]:
        """Start the standing cast from config (Sovereign, Steward, Artificer, …)."""
        spawned: list[Agent] = []
        for entry in self.config.roster:
            template = role_template(entry.role)
            model = entry.model or self.choose_model(entry.role)
            agent = self.spawn(
                role=entry.role,
                name=entry.name,
                model=model,
                capabilities=entry.capabilities or list(template.capabilities),
                budget={
                    "max_actions": entry.max_actions,
                    "max_wall_seconds": entry.max_wall_seconds,
                    "max_tokens": entry.max_tokens,
                },
            )
            spawned.append(agent)
            if entry.role == "steward":
                self._steward = agent
        return spawned

    def spawn(
        self,
        role: str,
        name: str = "",
        model: str = "",
        capabilities: Sequence[str] | None = None,
        parent_id: str | None = None,
        team_id: str | None = None,
        budget: dict[str, Any] | None = None,
        notes: str = "",
    ) -> Agent:
        template = role_template(role)
        with self._lock:
            if len(self.agents) >= self.max_parallel * 4:
                raise RuntimeError(
                    f"fleet limit reached ({len(self.agents)} agents); terminate finished agents first"
                )
            agent_config = AgentConfig(
                name=name or self._unique_name(template.title.split(" —")[0]),
                role=role,
                model=self._usable_model(model, role),
                capabilities=list(capabilities if capabilities is not None else template.capabilities),
            )
            if budget:
                agent_config.max_actions = int(budget.get("max_actions", agent_config.max_actions))
                agent_config.max_wall_seconds = int(budget.get("max_wall_seconds", agent_config.max_wall_seconds))
                agent_config.max_tokens = int(budget.get("max_tokens", agent_config.max_tokens))
            identity = AgentIdentity.from_config(agent_config, agent_id=f"{role[:4]}-{uuid.uuid4().hex[:8]}", team_id=team_id, parent_id=parent_id)
            identity.notes = notes
            self.memory.register_agent(
                name=identity.name,
                rank=identity.rank,
                role=identity.role,
                parent_id=parent_id,
                team_id=team_id,
                model=agent_config.model,
                capabilities=identity.capabilities,
                agent_id=identity.agent_id,
            )
            agent = Agent(identity, self.config, self.runtime, self.actions, self.memory, self.bus, orchestrator=self, executor=self.executor)
            self.agents[identity.agent_id] = agent
            if team_id and team_id in self.teams:
                self.teams[team_id].member_ids.append(identity.agent_id)
            self.bus.emit(
                AGENT_SPAWNED,
                source=parent_id or "orchestrator",
                **{
                    "agent_id": identity.agent_id,
                    "name": identity.name,
                    "role": identity.role,
                    "rank": identity.rank,
                    "title": identity.title,
                    "insignia": identity.insignia,
                    "color": identity.color,
                    "model": agent_config.model,
                    "capabilities": sorted(identity.capabilities),
                    "team_id": team_id,
                    "parent_id": parent_id,
                },
            )
            return agent

    def _usable_model(self, requested: str, role: str) -> str:
        """Honour a requested model only if it can actually be called right now.

        Assigning an agent a model whose provider has no credentials is a silent trap: the
        agent would fail at its first thought. Fall back to a ready model instead, and let
        the Steward move it later if quota demands it.
        """
        if requested:
            provider_name = requested.split(":", 1)[0]
            if self.runtime.registry.ready(provider_name):
                return requested
        return self.choose_model(role)

    def _unique_name(self, base: str) -> str:
        existing = {a.name for a in self.agents.values()}
        if base not in existing:
            return base
        index = 2
        while f"{base}-{index}" in existing:
            index += 1
        return f"{base}-{index}"

    # ----------------------------------------------------------------------------------
    # Model assignment
    # ----------------------------------------------------------------------------------
    def choose_model(self, role: str, requirements: Requirements | None = None, exclude: Sequence[str] = ()) -> str:
        """Pick the best available model for a role from what the catalog allows."""
        template = role_template(role)
        tags = tuple(template.preferred_tags)
        req = requirements or Requirements(tools=True, tags=tags, exclude=tuple(exclude), vision=role in {"cartographer", "operative", "foreman", "video"})
        chain = self.runtime.candidates(requirements=req)
        if not chain:
            req = Requirements(tools=True, exclude=tuple(exclude))
            chain = self.runtime.candidates(requirements=req)
        if not chain:
            # No model is available at all. Return empty rather than raising: an agent with no
            # model must be able to report "blocked" upward, not crash the spawn.
            return ""
        ranked = self.runtime.quotas.ranked_by_headroom(chain)
        return ranked[0][0] if ranked else chain[0]

    def assign_model(self, agent_id: str, model: str, reason: str = "manual assignment") -> dict[str, Any]:
        """Sovereign/Steward power: move an agent onto a different model, context intact."""
        return self.runtime.transplant(agent_id, model, reason)

    # ----------------------------------------------------------------------------------
    # Single task dispatch
    # ----------------------------------------------------------------------------------
    def dispatch(
        self,
        task: WorkItem,
        parent: Agent | None = None,
        team_id: str | None = None,
        wait: bool = True,
    ) -> AgentReport | Future[AgentReport]:
        agent = self.spawn(
            role=task.role,
            name=task.name,
            model=task.model,
            capabilities=task.capabilities or None,
            parent_id=parent.id if parent else None,
            team_id=team_id,
            notes=task.context[:500],
        )
        self.bus.emit(
            TASK_ASSIGNED,
            source=parent.id if parent else "orchestrator",
            task=task.title,
            agent=agent.name,
            agent_id=agent.id,
            model=agent.model,
            rank=agent.identity.rank,
        )
        context = task.context
        if task.depends_on:
            context += "\n\nUpstream results:\n" + "\n".join(
                f"- {dep}: {(self.memory.task(dep) or {}).get('report', {}).get('summary', 'pending')}" for dep in task.depends_on
            )
        contract = self._contract_for(task, agent, parent)
        future = self.executor.submit(
            agent.run_task,
            task.title,
            task.objective,
            context,
            24,
            None,
            team_id,
            contract=contract,
            work_class=task.work_class,
        )
        if not wait:
            return future
        report = future.result()
        if parent is not None:
            self.bus.emit(
                AGENT_MESSAGE,
                source=agent.id,
                to=parent.id,
                direction="out",
                preview=report.one_line()[:200],
                kind="report",
            )
        return report

    def _contract_for(self, task: WorkItem, agent: Agent, parent: Agent | None) -> Any:
        """Write down what "done" means before the work starts (contract 22.4).

        This is the difference between "every step ran" and "the goal was reached": criteria
        exist before the agent can rationalise its result, and a separate verifier checks them
        afterwards. Returns None when verification is not wired in.
        """
        from ..ops.verification import FILE_EXISTS, SuccessCriterion, TaskContract

        if self.verifier is None:
            return None
        contract = TaskContract(
            task_id="",
            request=task.objective or task.title,
            intended_capabilities=tuple(task.capabilities),
            requested_by=parent.name if parent is not None else "owner",
        )
        for entry in task.criteria:
            contract.criteria.append(SuccessCriterion.from_json(entry))
        for output in task.expected_outputs:
            contract.add(FILE_EXISTS, target=output, note="output this task promised")
        return contract

    def preflight_task(self, requirements: Any, origin: str = "user_request", state: Any = None) -> Any:
        """Ask whether this machine can do the work now, before spending a model call on it."""
        if self.preflight is None:
            return None
        return self.preflight.check(requirements, origin=origin, state=state)

    # ----------------------------------------------------------------------------------
    # Teams and long tasks
    # ----------------------------------------------------------------------------------
    def create_team(
        self,
        objective: str,
        items: Sequence[WorkItem],
        parent: Agent | None = None,
        foreman_model: str = "",
    ) -> Team:
        team_id = f"team-{uuid.uuid4().hex[:8]}"
        foreman = self.spawn(
            role="foreman",
            name=f"Foreman-{team_id[-4:]}",
            model=foreman_model or self.choose_model("foreman"),
            parent_id=parent.id if parent else None,
            team_id=team_id,
            notes=objective[:300],
        )
        team = Team(team_id=team_id, objective=objective, foreman_id=foreman.id, member_ids=[])
        self.teams[team_id] = team
        self.bus.emit(TASK_CREATED, source=foreman.id, team_id=team_id, objective=objective[:300], items=len(items))
        return team

    def _worker_batch_size(self, remaining: int, parallel: bool) -> int:
        """How many workers may be *admitted* right now, before any of them start.

        Submitting every future at once is the resource bug this replaces: a nine-item team
        queued nine agents, each holding a transcript and a model context, and the governor's
        "pause background work" decision arrived when the memory was already allocated.
        The cap is recomputed between batches, so pressure that appears mid-run shrinks the
        next batch instead of being discovered afterwards.
        """
        if not parallel:
            return 1
        requested = max(1, min(self.max_parallel, remaining))
        if self.governor is None:
            return requested
        from ..ops.governor import BACKGROUND

        try:
            state = self.governor.evaluate()
            return max(1, min(requested, self.governor.worker_limit(self.max_parallel, state, BACKGROUND)))
        except Exception:  # a governor that cannot answer must not stop real work
            return requested

    def run_team(
        self,
        objective: str,
        items: Sequence[WorkItem],
        parent: Agent | None = None,
        parallel: bool = True,
    ) -> dict[str, Any]:
        """Run a long objective as a team: bounded parallel batches, then one report."""
        team = self.create_team(objective, items, parent=parent)
        foreman = self.agents[team.foreman_id]
        foreman.remember(f"Leading team {team.team_id} for: {objective}", kind="task", importance=0.8, tags=["team", "lead"])

        reports: list[AgentReport] = []
        started = time.time()
        pending = list(items)
        batches: list[int] = []
        for item in pending:
            # A long objective is background work: it yields to the user's machine (22.11).
            if item.work_class == "interactive":
                item.work_class = "background"
        while pending:
            size = self._worker_batch_size(len(pending), parallel)
            batch, pending = pending[:size], pending[size:]
            batches.append(len(batch))
            futures: dict[Future[AgentReport], WorkItem] = {}
            for item in batch:
                future = self.dispatch(item, parent=foreman, team_id=team.team_id, wait=False)
                assert isinstance(future, Future)
                futures[future] = item
            for future in as_completed(futures) if len(futures) > 1 else futures:
                item = futures[future]
                try:
                    reports.append(future.result())
                except Exception as exc:  # a crashed worker must not kill the team
                    reports.append(
                        AgentReport("", item.name or item.title, "failed", f"worker crashed: {exc}", [], [str(exc)[:200]])
                    )

        # Consolidation is the Foreman's own model call: it summarizes, judges, and decides.
        consolidated = self._consolidate(foreman, objective, reports)
        self.memory.post_board(
            team.team_id,
            "team-report",
            json.dumps({"objective": objective[:200], "reports": [r.to_json() for r in reports], "summary": consolidated}, ensure_ascii=False)[:6000],
            author="Foreman",
        )
        for agent_id in [team.foreman_id, *team.member_ids]:
            self.bus.emit(TASK_DONE, source=agent_id, team_id=team.team_id, objective=objective[:200])
        return {
            "team": team.to_json(),
            "reports": [r.to_json() for r in reports],
            "summary": consolidated,
            "elapsed_s": round(time.time() - started, 1),
            "parallel": parallel,
            "batches": batches,
        }

    def _consolidate(self, foreman: Agent, objective: str, reports: Sequence[AgentReport]) -> str:
        from ..llm.providers import LLMRequest, Message

        brief = "\n".join(f"- {r.agent} [{r.status}] {r.summary}" + (f" | blockers: {'; '.join(r.blockers)}" if r.blockers else "") for r in reports)
        prompt = (
            f"Objective: {objective}\n\nWorker reports:\n{brief}\n\n"
            "Write the report for your parent: at most 5 lines. State the outcome, the evidence, "
            "any blocker, and what should happen next. Do not repeat the worker reports."
        )
        try:
            result = self.runtime.run(
                LLMRequest(messages=[Message(role="user", content=prompt)], model=foreman.model, tools=[], system=foreman.system_prompt(objective)),
                agent_id=foreman.id,
                prefer=[foreman.model],
                requirements=Requirements(tools=False),
            )
            summary = result.response.text.strip()
        except Exception as exc:  # deterministic fallback so a team never ends without a summary
            summary = f"(consolidation unavailable: {exc}) " + " | ".join(r.one_line() for r in reports[:5])
        foreman.remember(f"Team result for {objective[:120]}: {summary[:1500]}", kind="report", importance=0.75, tags=["team", "result"])
        return summary[:4000]

    # ----------------------------------------------------------------------------------
    # Steward: keeping the fleet alive
    # ----------------------------------------------------------------------------------
    # ----------------------------------------------------------------------------------
    # Capacity and build order (contract 22.5)
    # ----------------------------------------------------------------------------------
    def capacity_plan(self, total_items: int = 0) -> dict[str, Any]:
        """How wide this host may go. Waves of a few, or one at a time if it cannot mix."""
        from .planning import probe_capacity

        plan = probe_capacity(configured=self.config.safety.max_parallel_agents, total_items=total_items)
        return plan.to_json()

    def run_waves(
        self,
        objective: str,
        items: Sequence[WorkItem],
        parent: Agent | None = None,
        per_wave: int = 0,
        stop_on_failure: bool = True,
    ) -> dict[str, Any]:
        """Run a long objective in waves, sized to what this machine can actually run."""
        from .planning import run_waves

        return run_waves(self, objective, items, parent=parent, per_wave=per_wave, stop_on_failure=stop_on_failure, bus=self.bus)

    def in_flight(self) -> int:
        """Agents currently working. Used to refuse updates mid-task (contract 22.10)."""
        count = 0
        for agent in self.agents.values():
            record = self.memory.get_agent(agent.id)
            if record is not None and record.status in {"working", "busy", "running"}:
                count += 1
        return count

    def steward_watch(self) -> dict[str, Any]:
        """Fleet health pass: quota pressure, provider errors, stalled agents, failovers."""
        health = self.runtime.health_report()
        actions_taken: list[dict[str, Any]] = []
        for agent in list(self.agents.values()):
            record = self.memory.get_agent(agent.id)
            if record is None or record.status in {"idle", "stopped", "done"}:
                continue
            model = record.model or agent.model
            decision = self.runtime.quotas.check(model)
            if decision.allowed and decision.reason not in {"cooldown"}:
                continue
            replacement = self.choose_model(agent.identity.role, exclude=[model])
            if replacement == model:
                self._escalate(f"no alternative model available for {agent.name} (currently {model})", health)
                continue
            moved = self.runtime.transplant(
                agent.id,
                replacement,
                reason=f"{decision.reason} on {model} (retry_after={decision.retry_after:.0f}s)",
            )
            actions_taken.append(moved)
            self.bus.emit(
                MODEL_FALLBACK,
                source="steward",
                severity="notice",
                agent=agent.id,
                intended=model,
                actual=replacement,
                context_preserved=True,
            )
        summary = {
            "checked_agents": len(self.agents),
            "failovers": len(actions_taken),
            "actions": actions_taken,
            "exhausted_models": health.get("exhausted", []),
            "ready_models": health.get("ready", []),
        }
        if actions_taken:
            self.memory.remember(
                self._steward.id if self._steward else "system",
                f"steward failovers: {json.dumps(summary)[:1500]}",
                kind="steward",
                importance=0.6,
                tags=["quota", "failover"],
            )
        self.bus.emit(LOG, source="steward", severity="info", **{"message": "fleet health pass", **{k: v for k, v in summary.items() if k != "actions"}})
        return summary

    def _escalate(self, message: str, health: dict[str, Any]) -> None:
        sovereign = next((a for a in self.agents.values() if a.identity.role == "sovereign"), None)
        payload = {"message": message, "health": {k: v for k, v in health.items() if k != "models"}}
        if sovereign is not None:
            self.memory.remember(sovereign.id, f"escalation: {message}", kind="escalation", importance=0.9, tags=["steward"])
            self.bus.emit(AGENT_MESSAGE, source=self._steward.id if self._steward else "steward", to=sovereign.id, direction="out", preview=message[:200], kind="escalation")
        self.bus.emit(LOG, source="steward", severity="warning", **payload)

    def monitor_loop(self, interval: float = 20.0, stop_event: threading.Event | None = None) -> None:
        """Optional background health loop (started by the daemon/HUD, not by tests)."""
        while not (stop_event and stop_event.is_set()):
            try:
                self.steward_watch()
            except Exception as exc:  # pragma: no cover - defensive
                self.bus.emit(LOG, source="steward", severity="error", message=f"health pass failed: {exc}")
            time.sleep(interval)

    # ----------------------------------------------------------------------------------
    # Introspection & lifecycle
    # ----------------------------------------------------------------------------------
    def roster(self) -> list[dict[str, Any]]:
        rows = []
        for agent in sorted(self.agents.values(), key=lambda a: (a.identity.rank, a.name)):
            rows.append(
                {
                    "agent_id": agent.id,
                    "name": agent.name,
                    "role": agent.identity.role,
                    "title": agent.identity.title,
                    "rank": agent.identity.rank,
                    "rank_name": agent.identity.rank_name,
                    "insignia": agent.identity.insignia,
                    "color": agent.identity.color,
                    "model": agent.model,
                    "status": (self.memory.get_agent(agent.id) or type("X", (), {"status": "unknown"})()).status,
                    "task": agent.current_task,
                    "team_id": agent.identity.team_id,
                    "parent_id": agent.identity.parent_id,
                    "capabilities": sorted(agent.identity.capabilities),
                    "memory": self.memory.stats(agent.id),
                }
            )
        return rows

    def find(self, name_or_id: str) -> Agent | None:
        if name_or_id in self.agents:
            return self.agents[name_or_id]
        for agent in self.agents.values():
            if agent.name.lower() == name_or_id.lower():
                return agent
        return None

    def terminate(self, agent_id: str, reason: str = "task complete") -> bool:
        agent = self.agents.pop(agent_id, None)
        if agent is None:
            return False
        agent.stop()
        self.memory.remember(agent.id, f"terminated: {reason}", kind="lifecycle", importance=0.4)
        self.bus.emit(AGENT_TERMINATED, source="orchestrator", agent_id=agent_id, agent=agent.name, reason=reason)
        return True

    def shutdown(self) -> None:
        for agent_id in list(self.agents):
            self.terminate(agent_id, "shutdown")
        self.executor.shutdown(wait=False, cancel_futures=True)

    def registry_snapshot(self) -> dict[str, Any]:
        return {
            "agents": self.roster(),
            "teams": [t.to_json() for t in self.teams.values()],
            "tasks": self.memory.tasks()[-50:],
            "health": self.runtime.health_report(),
        }
