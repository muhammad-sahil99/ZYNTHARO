"""Agent identity: names, ranks, roles, capability grants and budgets.

Rank is not decoration — it is authority. A rank-0 agent may re-task a rank-1 agent and
reassign models fleet-wide; a rank-5 worker may not touch another agent at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config import AgentConfig, expand_capabilities

#: Rank ladder. Lower number = more authority.
RANKS: dict[str, int] = {
    "sovereign": 0,  # the main agent (Gemini Live class: audio, all power)
    "steward": 1,  # manager: fleet health, quotas, failover, context transplant
    "foreman": 2,  # head of a long task; owns a team of workers
    "specialist": 3,  # archivist, warden, artificer, eyes, courier
    "operative": 5,  # worker: executes actions
    "probe": 7,  # disposable, heavily budgeted, read-only explorer
}

RANK_NAMES = {v: k for k, v in RANKS.items()}


@dataclass(slots=True)
class AgentIdentity:
    """Everything that says *who* an agent is, independent of which model runs it."""

    agent_id: str
    name: str
    role: str
    rank: int
    capabilities: frozenset[str]
    team_id: str | None = None
    parent_id: str | None = None
    model: str = ""
    title: str = ""
    insignia: str = ""
    color: str = "#5ad1ff"
    budget: dict[str, Any] = field(default_factory=dict)
    notes: str = ""

    def can(self, capability: str) -> bool:
        return capability in self.capabilities

    def can_any(self, *capabilities: str) -> bool:
        return any(self.can(c) for c in capabilities)

    @property
    def rank_name(self) -> str:
        return RANK_NAMES.get(self.rank, f"rank-{self.rank}")

    def outranks(self, other: "AgentIdentity") -> bool:
        return self.rank < other.rank

    def to_json(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "role": self.role,
            "title": self.title,
            "rank": self.rank,
            "rank_name": self.rank_name,
            "insignia": self.insignia,
            "color": self.color,
            "capabilities": sorted(self.capabilities),
            "team_id": self.team_id,
            "parent_id": self.parent_id,
            "model": self.model,
            "budget": self.budget,
        }

    @classmethod
    def from_config(
        cls,
        config: AgentConfig,
        agent_id: str,
        team_id: str | None = None,
        parent_id: str | None = None,
    ) -> "AgentIdentity":
        from .roles import role_template

        template = role_template(config.role)
        granted = config.capabilities or template.capabilities
        return cls(
            agent_id=agent_id,
            name=config.name,
            role=config.role,
            rank=template.rank,
            capabilities=expand_capabilities(list(granted)),
            team_id=team_id,
            parent_id=parent_id,
            model=config.model,
            title=template.title,
            insignia=template.insignia,
            color=template.color,
            budget={
                "max_actions": config.max_actions,
                "max_wall_seconds": config.max_wall_seconds,
                "max_tokens": config.max_tokens,
            },
            notes=config.notes,
        )
