"""Agent layer: identities, the agent loop, the orchestrator and the role templates."""

from .base import REPORT_TOOL, Agent, AgentReport
from .identity import RANK_NAMES, RANKS, AgentIdentity
from .orchestrator import Orchestrator, Team, WorkItem
from .roles import TEMPLATES, RoleTemplate, role_names, role_template

__all__ = [
    "REPORT_TOOL",
    "Agent",
    "AgentReport",
    "AgentIdentity",
    "RANKS",
    "RANK_NAMES",
    "Orchestrator",
    "Team",
    "WorkItem",
    "TEMPLATES",
    "RoleTemplate",
    "role_names",
    "role_template",
]
