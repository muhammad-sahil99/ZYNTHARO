"""Scheduled-task tools, and the mistake-memory tools that keep failures from repeating.

Two small groups, both about the same complaint: the system forgets. It forgot to run the thing
you asked for tonight, and it forgot that this exact click failed an hour ago. ``task_schedule``
and its siblings fix the first; ``lessons_recall`` and ``lessons_note`` expose the second.

Scheduling is capability-gated (``task.schedule``) because an agent that can queue work for a
locked, unattended machine is an agent that can act while nobody is watching — precisely the
authority that should be granted deliberately. Reading the lesson book is cheap and is allowed
to anyone holding ``memory.admin``-free access: the hints are about tools, not about people.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .registry import ActionContext, ActionFailed, action


@dataclass
class ScheduleServices:
    scheduler: Any = None
    lessons: Any = None
    bus: Any = None
    config: Any = None


SERVICES: ScheduleServices | None = None


def set_services(services: ScheduleServices | None) -> None:
    global SERVICES
    SERVICES = services


def _services() -> ScheduleServices:
    if SERVICES is None:
        raise ActionFailed(
            "the scheduler is not wired yet",
            {"hint": "call actions.schedule.set_services(...) during bootstrap"},
        )
    return SERVICES


def _scheduler() -> Any:
    scheduler = _services().scheduler
    if scheduler is None:
        raise ActionFailed("no scheduler is attached to this build")
    return scheduler


# --------------------------------------------------------------------------------------
# Scheduling
# --------------------------------------------------------------------------------------


@action(
    name="task_schedule",
    description=(
        "Queue a task for later, once or on an interval. Use for work the user asked to happen "
        "at a time they are not watching. A task that misses its window is skipped with a reason "
        "instead of firing late, and GUI work is deferred while the screen is locked."
    ),
    capability="task.schedule",
    group="schedule",
    parameters={
        "type": "object",
        "properties": {
            "objective": {"type": "string", "description": "What should happen, precisely."},
            "title": {"type": "string", "description": "One-line label for the schedule panel."},
            "in_seconds": {"type": "number", "description": "Delay before the first run."},
            "every_seconds": {"type": "number", "description": "Repeat interval; omit for one-shot. Minimum 60."},
            "needs_gui": {"type": "boolean", "description": "True if it needs a visible, unlocked desktop."},
        },
        "required": ["objective"],
    },
)
def task_schedule(
    ctx: ActionContext,
    objective: str,
    title: str = "",
    in_seconds: float | None = None,
    every_seconds: float = 0.0,
    needs_gui: bool = False,
) -> Any:
    scheduler = _scheduler()
    try:
        entry = scheduler.schedule(
            objective,
            title=title,
            in_seconds=in_seconds,
            every_seconds=every_seconds,
            needs_gui=needs_gui,
            created_by=ctx.agent_id or "agent",
        )
    except ValueError as exc:
        # A refused schedule is information: the model can fix the numbers and retry.
        raise ActionFailed(str(exc), {"minimum_interval_seconds": 60}) from exc
    return {"scheduled": entry.to_json(), "note": "one-shot tasks disable themselves after they run"}


@action(
    name="task_schedules",
    description="List scheduled work with its next run, last outcome and whether the loop is running.",
    capability="task.schedule",
    group="schedule",
    parameters={"type": "object", "properties": {}},
)
def task_schedules(ctx: ActionContext) -> Any:
    return _scheduler().snapshot()


@action(
    name="task_cancel",
    description="Cancel a scheduled task by id or exact title.",
    capability="task.schedule",
    group="schedule",
    parameters={"type": "object", "properties": {"task": {"type": "string"}}, "required": ["task"]},
)
def task_cancel(ctx: ActionContext, task: str) -> Any:
    scheduler = _scheduler()
    if not scheduler.cancel(task):
        raise ActionFailed(f"no scheduled task matches {task!r}", {"known": [t["id"] for t in scheduler.snapshot()["tasks"]]})
    return {"cancelled": task}


@action(
    name="task_run_due",
    description="Run whatever is due right now and report what ran, what was deferred and why.",
    capability="task.schedule",
    group="schedule",
    parameters={"type": "object", "properties": {}},
)
def task_run_due(ctx: ActionContext) -> Any:
    return _scheduler().tick()


# --------------------------------------------------------------------------------------
# Mistake memory
# --------------------------------------------------------------------------------------


@action(
    name="lessons_recall",
    description=(
        "Read the failure patterns learned on this machine: which tool failed, why, how often, "
        "and the fix that worked. Check this before retrying something that just failed."
    ),
    capability="memory.admin",
    group="schedule",
    parameters={
        "type": "object",
        "properties": {
            "action": {"type": "string", "description": "Only lessons about this tool."},
            "error": {"type": "string", "description": "Find lessons similar to this error text."},
            "limit": {"type": "integer"},
        },
    },
)
def lessons_recall(ctx: ActionContext, action: str = "", error: str = "", limit: int = 8) -> Any:
    book = _services().lessons
    if book is None:
        raise ActionFailed("no lesson book is attached to this build")
    rows = book.recall(action, error, k=limit) if (action or error) else book._capped()[:limit]
    return {
        "count": len(rows),
        "lessons": [
            {"action": row.action, "cause": row.cause, "fix": row.fix, "hits": row.hits, "last_seen": row.last_seen}
            for row in rows
        ],
        "stats": book.stats(),
    }


@action(
    name="lessons_note",
    description=(
        "Record what went wrong and what fixed it, so the next attempt — yours, a sibling's or "
        "tomorrow's — starts with the fix instead of repeating the mistake."
    ),
    capability="memory.admin",
    group="schedule",
    parameters={
        "type": "object",
        "properties": {
            "action": {"type": "string", "description": "Tool name this lesson is about."},
            "cause": {"type": "string", "description": "What failed, in one line."},
            "fix": {"type": "string", "description": "What actually worked instead."},
        },
        "required": ["action", "cause", "fix"],
    },
)
def lessons_note(ctx: ActionContext, action: str, cause: str, fix: str) -> Any:
    book = _services().lessons
    if book is None:
        raise ActionFailed("no lesson book is attached to this build")
    if not (fix or "").strip():
        raise ActionFailed("a lesson without a fix teaches nothing; pass the fix that worked")
    # ``count=False``: writing a fix down is not another failure of that tool.
    lesson = book.record(action, cause, fix=fix.strip(), count=False)
    return {"lesson": lesson.to_json(), "stats": book.stats()}
