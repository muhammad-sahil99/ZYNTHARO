"""Channels and workflows as tools the fleet can actually call.

The rules these actions encode are the product's promises:

* ``channel_status`` may always answer; it is how a customer learns what is missing.
* ``channel_send`` requires ``channel.send``, which is owner-only and confirmation-gated: an agent
  can prepare a message, never post it on its own.
* ``workflow_run`` runs a named process with the customer's own paths and returns its evidence.
* ``reply_approve`` is the single path from a prepared draft to a delivered message, and it checks
  that the draft's platform matches the channel it is being sent through.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .registry import ActionContext, ActionFailed, DriverUnavailable, action


@dataclass(slots=True)
class ChannelServices:
    channels: Any
    workflows: dict[str, Any]
    reply_desk: Any = None


_SERVICES: ChannelServices | None = None


def set_services(services: ChannelServices | None) -> None:
    global _SERVICES
    _SERVICES = services


def _svc() -> ChannelServices:
    if _SERVICES is None:
        raise DriverUnavailable(
            "channels",
            "the channel registry",
            "This stack was assembled without channels; start the app normally (orbynth hud) or "
            "pass services to build_registry.",
        )
    return _SERVICES


def _channel(name: str) -> Any:
    try:
        return _svc().channels.get(name)
    except KeyError as exc:
        raise ActionFailed(str(exc)) from exc


@action(
    name="channel_status",
    description=(
        "Which messaging platforms are usable right now, which need credentials, and which are not "
        "offered at all — each with the reason. Call this before promising a customer anything."
    ),
    capability="channel.read",
    group="channels",
    parameters={"type": "object", "properties": {}},
)
def channel_status(ctx: ActionContext) -> Any:
    services = _svc()
    return {"offered": services.channels.names(), "channels": services.channels.to_json()}


@action(
    name="channel_send",
    description=(
        "Send a message through a connected platform. Owner-only and confirmation-gated: a sent "
        "message cannot be recalled."
    ),
    capability="channel.send",
    group="channels",
    parameters={
        "type": "object",
        "properties": {
            "channel": {"type": "string", "description": "telegram | whatsapp"},
            "chat": {"type": "string"},
            "text": {"type": "string"},
        },
        "required": ["channel", "chat", "text"],
    },
)
def channel_send(ctx: ActionContext, channel: str, chat: str, text: str) -> Any:
    return _channel(channel).send(chat, text)


@action(
    name="channel_inbox",
    description="Read recent inbound messages from a platform, without replying.",
    capability="channel.read",
    group="channels",
    parameters={
        "type": "object",
        "properties": {
            "channel": {"type": "string"},
            "chat": {"type": "string"},
            "limit": {"type": "integer"},
        },
        "required": ["channel"],
    },
)
def channel_inbox(ctx: ActionContext, channel: str, chat: str = "", limit: int = 20) -> Any:
    messages = _channel(channel).inbox(limit=limit, chat=chat)
    return {"count": len(messages), "messages": [message.to_json() for message in messages]}


@action(
    name="workflow_list",
    description="The business workflows this build ships, with what each one needs to run.",
    capability="workflow.run",
    group="workflows",
    parameters={"type": "object", "properties": {}},
)
def workflow_list(ctx: ActionContext) -> Any:
    from ..workflows import catalogue

    return {"workflows": catalogue()}


@action(
    name="workflow_run",
    description=(
        "Run a named workflow (invoice_intake, daily_report, reply_desk) with its parameters. "
        "Returns steps, artifacts and whether the outcome was verified."
    ),
    capability="workflow.run",
    group="workflows",
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "params": {"type": "object", "description": "The workflow's own parameters, e.g. source/dest/ledger."},
        },
        "required": ["name"],
    },
)
def workflow_run(ctx: ActionContext, name: str, params: dict[str, Any] | None = None) -> Any:
    services = _svc()
    workflow = services.workflows.get(name)
    if workflow is None:
        raise ActionFailed(f"no workflow named {name!r}; known: {', '.join(sorted(services.workflows))}")
    if services.reply_desk is not None and name == "reply_desk":
        from ..workflows.base import WorkflowError

        channel_name = str((params or {}).get("channel", ""))
        channel = _channel(channel_name) if channel_name else None
        if channel is None:
            raise ActionFailed("reply_desk needs a 'channel' parameter naming a connected platform")
        try:
            result = services.reply_desk.run(channel, chat=(params or {}).get("chat", ""), limit=int((params or {}).get("limit", 20)))
        except WorkflowError as exc:
            raise ActionFailed(str(exc)) from exc
        return result.to_json()
    from ..workflows.base import WorkflowError

    try:
        result = workflow.run(**(params or {}))
    except WorkflowError as exc:
        raise ActionFailed(str(exc)) from exc
    return result.to_json()


@action(
    name="reply_pending",
    description="Drafts waiting for the owner's approval. Nothing here has been sent.",
    capability="channel.read",
    group="workflows",
    parameters={"type": "object", "properties": {"limit": {"type": "integer"}}},
)
def reply_pending(ctx: ActionContext, limit: int = 50) -> Any:
    desk = _svc().reply_desk
    if desk is None:
        raise ActionFailed("the reply desk is not available in this stack")
    drafts = desk.pending(limit=limit)
    return {"count": len(drafts), "drafts": drafts}


@action(
    name="reply_approve",
    description=(
        "Send one prepared draft through its own platform. Owner action: confirmation-gated, and "
        "refused if the draft belongs to a different channel."
    ),
    capability="channel.send",
    group="workflows",
    parameters={
        "type": "object",
        "properties": {"draft_id": {"type": "integer"}},
        "required": ["draft_id"],
    },
)
def reply_approve(ctx: ActionContext, draft_id: int) -> Any:
    services = _svc()
    desk = services.reply_desk
    if desk is None:
        raise ActionFailed("the reply desk is not available in this stack")
    row = next((draft for draft in desk.pending(limit=200) if int(draft["id"]) == int(draft_id)), None)
    if row is None:
        raise ActionFailed(f"draft {draft_id} is not waiting for approval")
    from ..workflows.base import WorkflowError

    try:
        return desk.approve(int(draft_id), _channel(str(row["channel"])))
    except WorkflowError as exc:
        raise ActionFailed(str(exc)) from exc


@action(
    name="reply_reject",
    description="Discard a prepared draft without sending it.",
    capability="channel.read",
    group="workflows",
    parameters={
        "type": "object",
        "properties": {"draft_id": {"type": "integer"}, "reason": {"type": "string"}},
        "required": ["draft_id"],
    },
)
def reply_reject(ctx: ActionContext, draft_id: int, reason: str = "") -> Any:
    desk = _svc().reply_desk
    if desk is None:
        raise ActionFailed("the reply desk is not available in this stack")
    from ..workflows.base import WorkflowError

    try:
        return desk.reject(int(draft_id), reason)
    except WorkflowError as exc:
        raise ActionFailed(str(exc)) from exc


def all_actions() -> list[Any]:
    return [
        channel_status,
        channel_send,
        channel_inbox,
        workflow_list,
        workflow_run,
        reply_pending,
        reply_approve,
        reply_reject,
    ]