"""Orchestration actions: the fleet-management tools a model can actually call.

These are what make "the main AI can assign sub-agents, limit their permissions and
resources, ask what they did, and choose which model runs any role" a *tool* rather than a
paragraph in a prompt. They are capability-gated exactly like mouse and keyboard actions:
only ranks that hold ``agent.spawn`` / ``agent.control`` / ``model.assign`` can touch the
fleet.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Sequence

from ..config import expand_capabilities
from ..events import AGENT_STATUS, HUD_NOTICE, HUD_SCENE, MEMORY_RECALL, MEMORY_WRITE
from .registry import Action, ActionContext, ActionFailed, action


@dataclass
class Services:
    """Live system handles injected into the orchestration actions."""

    orchestrator: Any
    runtime: Any
    memory: Any
    bus: Any
    config: Any
    hud: Any = None


SERVICES: Services | None = None


def set_services(services: Services | None) -> None:
    global SERVICES
    SERVICES = services


def _services() -> Services:
    if SERVICES is None:
        raise ActionFailed(
            "orchestration is not wired yet",
            {"hint": "call actions.orchestration.set_services(...) during bootstrap"},
        )
    return SERVICES


@action(
    name="agent_spawn",
    description=(
        "Create a sub-agent with a specific role, model, capability grant and budget. Use for "
        "parallel or specialized work. Ranks: foreman(2) leads a task, operative(5) executes, "
        "probe(7) explores cheaply and read-only."
    ),
    capability="agent.spawn",
    group="orchestration",
    parameters={
        "type": "object",
        "properties": {
            "role": {"type": "string", "description": "sovereign|steward|foreman|operative|artificer|archivist|warden|cartographer|courier|scrivener|probe"},
            "name": {"type": "string"},
            "model": {"type": "string", "description": "provider:model; omit to auto-select"},
            "capabilities": {"type": "array", "items": {"type": "string"}, "description": "exact grant, supports wildcards like filesystem.*"},
            "max_actions": {"type": "integer"},
            "max_tokens": {"type": "integer"},
            "max_wall_seconds": {"type": "integer"},
            "team_id": {"type": "string"},
            "notes": {"type": "string"},
        },
        "required": ["role"],
    },
)
def agent_spawn(
    ctx: ActionContext,
    role: str,
    name: str = "",
    model: str = "",
    capabilities: Sequence[str] | None = None,
    max_actions: int = 200,
    max_tokens: int = 2_000_000,
    max_wall_seconds: int = 1800,
    team_id: str = "",
    notes: str = "",
) -> Any:
    services = _services()
    parent = ctx.identity.agent_id if ctx.identity is not None else None
    agent = services.orchestrator.spawn(
        role=role,
        name=name,
        model=model,
        capabilities=capabilities,
        parent_id=parent,
        team_id=team_id or None,
        budget={"max_actions": max_actions, "max_tokens": max_tokens, "max_wall_seconds": max_wall_seconds},
        notes=notes,
    )
    return {
        "agent_id": agent.id,
        "name": agent.name,
        "role": agent.identity.role,
        "rank": agent.identity.rank,
        "model": agent.model,
        "capabilities": sorted(agent.identity.capabilities),
        "budget": {"max_actions": max_actions, "max_tokens": max_tokens, "max_wall_seconds": max_wall_seconds},
    }


@action(
    name="agent_task",
    description=(
        "Hand a self-contained task to an agent (existing by name/id, or a fresh one of the "
        "given role) and wait for its short report: status, one-line summary, evidence, "
        "blockers, next step."
    ),
    capability="agent.control",
    group="orchestration",
    parameters={
        "type": "object",
        "properties": {
            "agent": {"type": "string", "description": "Existing agent name or id."},
            "role": {"type": "string", "description": "Used when spawning a new agent."},
            "title": {"type": "string"},
            "objective": {"type": "string"},
            "context": {"type": "string"},
            "capabilities": {"type": "array", "items": {"type": "string"}},
            "model": {"type": "string"},
        },
        "required": ["title", "objective"],
    },
)
def agent_task(
    ctx: ActionContext,
    title: str,
    objective: str,
    agent: str = "",
    role: str = "operative",
    context: str = "",
    capabilities: Sequence[str] | None = None,
    model: str = "",
) -> Any:
    services = _services()
    existing = services.orchestrator.find(agent) if agent else None
    caller = ctx.identity
    if existing is not None:
        report = existing.run_task(title, objective, context, max_steps=24, parent_task=None)
        return report.to_json()
    from ..agents.orchestrator import WorkItem

    item = WorkItem(title=title, objective=objective, role=role, capabilities=list(capabilities or []), context=context, model=model)
    parent = services.orchestrator.find(caller.name) if caller is not None else None
    report = services.orchestrator.dispatch(item, parent=parent)
    return report.to_json() if hasattr(report, "to_json") else {"status": "dispatched"}


@action(
    name="agent_team",
    description=(
        "Run a long objective as a team: a Foreman plus one Operative per work item, executed "
        "in parallel, consolidated into a single report. Use this for anything bigger than a "
        "handful of steps."
    ),
    capability="agent.control",
    group="orchestration",
    parameters={
        "type": "object",
        "properties": {
            "objective": {"type": "string"},
            "items": {
                "type": "array",
                "description": "Work items",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "objective": {"type": "string"},
                        "role": {"type": "string"},
                        "capabilities": {"type": "array", "items": {"type": "string"}},
                        "context": {"type": "string"},
                    },
                    "required": ["title", "objective"],
                },
            },
            "parallel": {"type": "boolean"},
        },
        "required": ["objective", "items"],
    },
)
def agent_team(
    ctx: ActionContext,
    objective: str,
    items: Sequence[dict[str, Any]],
    parallel: bool = True,
) -> Any:
    services = _services()
    from ..agents.orchestrator import WorkItem

    work = [
        WorkItem(
            title=str(item.get("title", "")),
            objective=str(item.get("objective", "")),
            role=str(item.get("role", "operative")),
            capabilities=list(item.get("capabilities", []) or []),
            context=str(item.get("context", "")),
        )
        for item in items
    ]
    if not work:
        raise ActionFailed("items must contain at least one work item")
    parent = services.orchestrator.find(ctx.identity.name) if ctx.identity is not None else None
    result = services.orchestrator.run_team(objective, work, parent=parent, parallel=parallel)
    return result


@action(
    name="agent_message",
    description="Ask a specific agent a short question and get a short answer (no task handover).",
    capability="agent.control",
    group="orchestration",
    parameters={
        "type": "object",
        "properties": {"agent": {"type": "string"}, "message": {"type": "string"}},
        "required": ["agent", "message"],
    },
)
def agent_message(ctx: ActionContext, agent: str, message: str) -> Any:
    services = _services()
    target = services.orchestrator.find(agent)
    if target is None:
        raise ActionFailed(f"no such agent: {agent}", {"roster": [a["name"] for a in services.orchestrator.roster()]})
    sender = ctx.identity.name if ctx.identity is not None else "system"
    answer = target.handle_message(sender, message)
    return {"agent": target.name, "answer": answer}


@action(
    name="agent_list",
    description="The fleet roster: every agent with rank, role, model, status, task, memory size and budget.",
    capability="agent.control",
    group="orchestration",
    parameters={"type": "object", "properties": {}},
)
def agent_list(ctx: ActionContext) -> Any:
    services = _services()
    roster = services.orchestrator.roster()
    return {"count": len(roster), "agents": roster}


@action(
    name="agent_terminate",
    description="Stop and remove an agent. Its memory stays in the database (nothing is lost).",
    capability="agent.control",
    group="orchestration",
    parameters={"type": "object", "properties": {"agent": {"type": "string"}, "reason": {"type": "string"}}, "required": ["agent"]},
)
def agent_terminate(ctx: ActionContext, agent: str, reason: str = "requested") -> Any:
    services = _services()
    target = services.orchestrator.find(agent)
    if target is None:
        raise ActionFailed(f"no such agent: {agent}")
    ok = services.orchestrator.terminate(target.id, reason)
    return {"terminated": ok, "agent": target.name, "reason": reason}


@action(
    name="model_assign",
    description=(
        "Assign a model to any agent (or role) at any time; the agent keeps its identity, "
        "memory and transcript. This is how a quota-exhausted or failing model is swapped for "
        "a fallback without losing work."
    ),
    capability="model.assign",
    group="orchestration",
    parameters={
        "type": "object",
        "properties": {
            "agent": {"type": "string", "description": "Agent name or id; omit to assign by role."},
            "role": {"type": "string"},
            "model": {"type": "string", "description": "provider:model"},
            "reason": {"type": "string"},
        },
        "required": ["model"],
    },
)
def model_assign(ctx: ActionContext, model: str, agent: str = "", role: str = "", reason: str = "assignment") -> Any:
    services = _services()
    if agent:
        target = services.orchestrator.find(agent)
        if target is None:
            raise ActionFailed(f"no such agent: {agent}")
        return services.orchestrator.assign_model(target.id, model, reason)
    if not role:
        raise ActionFailed("provide either agent or role")
    moved: list[dict[str, Any]] = []
    for candidate in services.orchestrator.agents.values():
        if candidate.identity.role == role:
            moved.append(services.orchestrator.assign_model(candidate.id, model, reason))
    if not moved:
        raise ActionFailed(f"no agent currently has role {role!r}")
    return {"assigned": moved}


@action(
    name="agent_set_limits",
    description=(
        "Raise or lower an agent's resources and permissions: action count, wall-clock seconds, "
        "token budget, and its capability set. The Steward uses this to keep a runaway worker on "
        "a leash, and Sovereign uses it to grant more power. Changes apply immediately and are "
        "written to the agent's memory so they survive a model swap."
    ),
    capability="agent.control",
    group="orchestration",
    parameters={
        "type": "object",
        "properties": {
            "agent": {"type": "string", "description": "Agent name or id."},
            "max_actions": {"type": "integer"},
            "max_wall_seconds": {"type": "integer"},
            "max_tokens": {"type": "integer"},
            "grant_capabilities": {"type": "array", "items": {"type": "string"}},
            "revoke_capabilities": {"type": "array", "items": {"type": "string"}},
            "reason": {"type": "string"},
        },
        "required": ["agent"],
    },
)
def agent_set_limits(
    ctx: ActionContext,
    agent: str,
    max_actions: int = 0,
    max_wall_seconds: int = 0,
    max_tokens: int = 0,
    grant_capabilities: list[str] | None = None,
    revoke_capabilities: list[str] | None = None,
    reason: str = "requested",
) -> Any:
    """Enforce limits on another agent. Refuses to widen the caller's own authority."""
    services = _services()
    target = services.orchestrator.find(agent)
    if target is None:
        raise ActionFailed(f"no such agent: {agent}")

    granted_specs = list(grant_capabilities or [])
    caller_grants = expand_capabilities(list(ctx.capabilities))
    escalations = [spec for spec in granted_specs if not _within(caller_grants, spec)]
    if escalations:
        raise ActionFailed(
            f"cannot grant capabilities the caller does not hold: {', '.join(escalations)}",
            {"caller_capabilities": sorted(caller_grants)[:20], "refused": escalations},
        )

    # Limits live in the identity's budget dict: an AgentIdentity uses __slots__, so inventing
    # attributes on it would raise, and the budget dict is what the runtime actually enforces.
    before = dict(target.identity.budget or {})
    limits = dict(before)
    if max_actions:
        limits["max_actions"] = int(max_actions)
    if max_wall_seconds:
        limits["max_wall_seconds"] = int(max_wall_seconds)
    if max_tokens:
        limits["max_tokens"] = int(max_tokens)
    target.identity.budget = limits

    caps = set(target.identity.capabilities)
    caps.update(expand_capabilities(granted_specs))
    for spec in revoke_capabilities or []:
        caps.difference_update(expand_capabilities([spec]))
    if granted_specs or revoke_capabilities:
        target.identity.capabilities = frozenset(caps)

    # The runtime budget object is what actually refuses work, so rebuild it around the new
    # limits rather than leaving a stale one in place that would keep enforcing the old grant.
    previous_budget = getattr(services.runtime, "budgets", {}).pop(target.id, None)
    budget = services.runtime.budget_for(target.id, target.identity)
    for field_name in ("actions", "tokens", "spent_actions", "charged_tokens"):
        if previous_budget is not None and hasattr(previous_budget, field_name):
            try:
                setattr(budget, field_name, getattr(previous_budget, field_name))
            except Exception:
                continue

    services.memory.set_agent_limits(
        target.id,
        max_actions=budget.max_actions,
        max_wall_seconds=budget.max_wall_seconds,
        max_tokens=budget.max_tokens,
        capabilities=sorted(target.identity.capabilities),
    )
    target.remember(
        f"limits changed by {ctx.agent_id}: actions<={budget.max_actions}, wall<={budget.max_wall_seconds:.0f}s, "
        f"tokens<={budget.max_tokens}, capabilities={len(target.identity.capabilities)} ({reason})",
        kind="governance",
        importance=0.8,
        tags=["limits"],
    )
    services.bus.emit(
        AGENT_STATUS,
        source=ctx.agent_id,
        agent=target.name,
        agent_id=target.id,
        status="limits_changed",
        reason=reason,
    )
    return {
        "agent": target.name,
        "budget": {
            "max_actions": budget.max_actions,
            "max_wall_seconds": budget.max_wall_seconds,
            "max_tokens": budget.max_tokens,
        },
        "capabilities": sorted(target.identity.capabilities),
        "before": before,
        "reason": reason,
    }


def _within(caller: frozenset[str], spec: str) -> bool:
    """Does the caller already hold every capability this spec would grant?"""
    wanted = expand_capabilities([spec])
    return wanted <= caller if spec.endswith(".*") or spec == "*" else bool(wanted & caller)


@action(
    name="model_health",
    description=(
        "Quota and readiness report for every configured model: rpm/rpd/tpm/tpd usage, "
        "cooldowns, recent errors, which models are ready. The Steward's dashboard."
    ),
    capability="model.assign",
    group="orchestration",
    parameters={"type": "object", "properties": {}},
)
def model_health(ctx: ActionContext) -> Any:
    return _services().runtime.health_report()


@action(
    name="fleet_watch",
    description="Run a health pass now: detect throttled/failing agents and fail them over to healthy models, context intact.",
    capability="model.assign",
    group="orchestration",
    parameters={"type": "object", "properties": {}},
)
def fleet_watch(ctx: ActionContext) -> Any:
    return _services().orchestrator.steward_watch()


@action(
    name="memory_remember",
    description="Write something into your own durable memory (with importance and tags).",
    capability="memory.admin",
    group="orchestration",
    parameters={
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "kind": {"type": "string"},
            "importance": {"type": "number"},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["text"],
    },
)
def memory_remember(
    ctx: ActionContext, text: str, kind: str = "note", importance: float = 0.5, tags: Sequence[str] = ()
) -> Any:
    services = _services()
    agent_id = ctx.agent_id
    episode = services.memory.remember(agent_id, text, kind=kind, importance=importance, tags=list(tags))
    services.bus.emit(MEMORY_WRITE, source=agent_id, kind=kind, episode=episode)
    return {"stored": True, "episode": episode, "agent_id": agent_id}


@action(
    name="memory_recall",
    description="Search your own memory (and your team's board) for related work, facts and lessons.",
    capability="memory.admin",
    group="orchestration",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "k": {"type": "integer"},
            "agent_id": {"type": "string", "description": "Recall another agent's memory (memory.admin only)."},
        },
        "required": ["query"],
    },
)
def memory_recall(ctx: ActionContext, query: str, k: int = 8, agent_id: str = "") -> Any:
    services = _services()
    record = services.memory.get_agent(agent_id or ctx.agent_id)
    team = [record.team_id] if record and record.team_id else []
    hits = services.memory.recall(agent_id or ctx.agent_id, query, k=k, include_teams=team)
    services.bus.emit(MEMORY_RECALL, source=ctx.agent_id, query=query[:100], hits=len(hits))
    return {"query": query, "count": len(hits), "hits": [hit.to_json() for hit in hits]}


@action(
    name="memory_board",
    description="Post to (or read) the team board — the shared scratchpad for a long objective.",
    capability="memory.admin",
    group="orchestration",
    parameters={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["post", "read"]},
            "team_id": {"type": "string"},
            "topic": {"type": "string"},
            "body": {"type": "string"},
            "limit": {"type": "integer"},
        },
        "required": ["action"],
    },
)
def memory_board(
    ctx: ActionContext,
    action: str,  # noqa: A002 - matches the tool schema
    team_id: str = "",
    topic: str = "",
    body: str = "",
    limit: int = 30,
) -> Any:
    services = _services()
    record = services.memory.get_agent(ctx.agent_id)
    team = team_id or (record.team_id if record else "") or "fleet"
    if action == "post":
        if not (topic and body):
            raise ActionFailed("topic and body are required to post")
        entry = services.memory.post_board(team, topic, body, author=record.name if record else ctx.agent_id)
        return {"posted": True, "id": entry, "team_id": team}
    return {"team_id": team, "entries": services.memory.read_board(team, limit=limit)}


@action(
    name="notify_user",
    description="Say something to the user through the HUD (spoken by default, shown in the transcript).",
    capability="agent.control",
    group="orchestration",
    parameters={
        "type": "object",
        "properties": {"text": {"type": "string"}, "speak": {"type": "boolean"}, "severity": {"type": "string"}},
        "required": ["text"],
    },
)
def notify_user(ctx: ActionContext, text: str, speak: bool = True, severity: str = "info") -> Any:
    services = _services()
    services.bus.emit(
        HUD_NOTICE,
        source=ctx.agent_id,
        severity=severity,
        text=text[:2000],
        speak=speak,
        agent=(ctx.identity.name if ctx.identity is not None else ctx.agent_id),
    )
    if services.hud is not None:  # direct push when a HUD session is attached
        try:
            services.hud.notify(text, severity=severity, speak=speak)
        except Exception:
            pass
    return {"delivered": True, "spoken": speak}


@action(
    name="hud_scene",
    description=(
        "Open a visual explanation in the HUD: a 3D scene, diagram, chart or annotated "
        "screenshot, rendered in the scene viewer window. Use drawings over prose."
    ),
    capability="agent.control",
    group="orchestration",
    parameters={
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["scene3d", "diagram", "chart", "image", "table"]},
            "title": {"type": "string"},
            "payload": {"type": "object", "description": "Scene graph, nodes/edges, chart series, image path or table rows."},
        },
        "required": ["kind", "title", "payload"],
    },
)
def hud_scene(ctx: ActionContext, kind: str, title: str, payload: dict[str, Any]) -> Any:
    services = _services()
    services.bus.emit(HUD_SCENE, source=ctx.agent_id, kind=kind, title=title[:200], payload=payload)
    return {"opened": True, "kind": kind, "title": title}


@action(
    name="task_list",
    description="List tasks with status, owner and report — the fleet's work ledger.",
    capability="agent.control",
    group="orchestration",
    parameters={"type": "object", "properties": {"team_id": {"type": "string"}, "status": {"type": "string"}}},
)
def task_list(ctx: ActionContext, team_id: str = "", status: str = "") -> Any:
    services = _services()
    rows = services.memory.tasks(team_id or None, status or None)
    return {"count": len(rows), "tasks": rows[-100:]}


all_actions = lambda: [  # noqa: E731
    agent_spawn,
    agent_task,
    agent_team,
    agent_message,
    agent_list,
    agent_terminate,
    agent_set_limits,
    model_assign,
    model_health,
    fleet_watch,
    memory_remember,
    memory_recall,
    memory_board,
    notify_user,
    hud_scene,
    task_list,
]
