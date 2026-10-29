"""The three workflows ZYNTHARO sells, each one runnable end to end and verifiable by its output.

Keeping this list short is deliberate. A vertical product is trusted because it does a small number
of things correctly on a real customer's machine, not because it offers forty half-finished
automations. New workflows are added when a paying customer needs one, and they arrive with the
same shape: explicit inputs, an artifact, and a read-back check.
"""

from __future__ import annotations

from .base import DONE, FAILED, PARTIAL, Step, Workflow, WorkflowError, WorkflowResult
from .daily_report import DailyReport
from .invoice_intake import InvoiceIntake
from .reply_desk import ReplyDesk

WORKFLOWS: dict[str, type[Workflow]] = {
    InvoiceIntake.name: InvoiceIntake,
    ReplyDesk.name: ReplyDesk,
    DailyReport.name: DailyReport,
}

__all__ = [
    "DONE",
    "FAILED",
    "PARTIAL",
    "Step",
    "Workflow",
    "WorkflowError",
    "WorkflowResult",
    "WORKFLOWS",
    "InvoiceIntake",
    "ReplyDesk",
    "DailyReport",
]


def catalogue() -> list[dict[str, str]]:
    """What the sales page and the app's Workflows panel show, without embellishment."""
    return [
        {
            "name": name,
            "title": workflow.title,
            "description": workflow.description,
            "needs": workflow.needs,
        }
        for name, workflow in WORKFLOWS.items()
    ]