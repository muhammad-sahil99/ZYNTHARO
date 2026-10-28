"""Fleet comms and model-bench actions.

Two jobs, both of which the fleet needs to be a *fleet* rather than a crowd:

* **Talk to each other.** ``agent_ask`` opens a request to a sibling, ``agent_reply``
  answers one, ``agent_send_file`` hands over an artefact, ``agent_inbox`` and
  ``agent_thread`` read what arrived. Help requests are addressed by name, by id or by
  ``role:<role>`` so an Operative can ask "whoever is the browser specialist" without
  knowing which agent is behind that role today.
* **Manage the bench.** ``model_add`` records a model *with its limits*, optionally as a
  reserve; ``model_activate`` / ``model_retire`` call reserves up and stand them down;
  ``model_reserves`` shows the bench with real headroom.

Every one of these is capability-gated like a mouse click: ``agent.messaging`` and
``model.manage`` are held by the Steward and above, so an Operative cannot quietly add a
provider or mail the Sovereign without having been granted the right to.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from ..llm.reserve import ModelLimits
from .registry import ActionContext, ActionFailed, action


@dataclass
class FleetServices:
    """Live handles the fleet tools need."""

    comms: Any = None
    reserves: Any = None
    orchestrator: Any = None
    runtime: Any = None
    config: Any = None
    memory: Any = None
    bus: Any = None


SERVICES: FleetServices | None = None


def set_services(services: FleetServices | None) -> None:
    global SERVICES
    SERVICES = services


def _services() -> FleetServices:
    if SERVICES is None:
        raise ActionFailed(
            "fleet comms are not wired yet",
            {"hint": "call actions.fleet.set_services(...) during bootstrap"},
        )
    return SERVICES


def _comms() -> Any:
    comms = _services().comms
    if comms is None:
        raise ActionFailed("no comms store is attached to this build")
    return comms


def _reserves() -> Any:
    reserves = _services().reserves
    if reserves is None:
        raise ActionFailed("no reserve pool is attached to this build")
    return reserves


def _me(ctx: ActionContext) -> tuple[str, str]:
    identity = ctx.identity
    if identity is None:
        return (ctx.agent_id or "system", "system")
    return (identity.agent_id, identity.name)


# --------------------------------------------------------------------------------------
# Talking to each other
# --------------------------------------------------------------------------------------


@action(
    name="agent_ask",
    description=(
        "Ask another agent for help and open a tracked request. Address it by agent id, by "
        "name, by role:<role> for the standing specialist, or '*' for the whole fleet. The "
        "request stays open until answered, so a manager can see what is still waiting."
    ),
    capability="agent.messaging",
    group="fleet",
    parameters={
        "type": "object",
        "properties": {
            "to": {"type": "string", "description": "agent id, name, role:<role>, or *"},
            "question": {"type": "string", "description": "The question, precise enough to answer without the full task."},
            "subject": {"type": "string", "description": "One-line title for the thread."},
            "context": {"type": "string", "description": "What they need to know: paths, urls, what you already tried."},
            "attachments": {"type": "array", "items": {"type": "string"}, "description": "Files to hand over (copied, not referenced)."},
            "thread_id": {"type": "string", "description": "Continue an existing thread."},
        },
        "required": ["to", "question"],
    },
)
def agent_ask(
    ctx: ActionContext,
    to: str,
    question: str,
    subject: str = "",
    context: str = "",
    attachments: Sequence[str] | None = None,
    thread_id: str = "",
) -> Any:
    comms = _comms()
    sender_id, sender_name = _me(ctx)
    staged = comms.stage_files(list(attachments or []), note="question") if attachments else []
    body = question.strip()
    if context:
        body = f"{body}\n\nCONTEXT:\n{context.strip()}"
    message = comms.send(
        sender_id=sender_id,
        sender_name=sender_name,
        recipient=to,
        subject=subject or question.strip().splitlines()[0][:120],
        body=body,
        kind="request",
        attachments=staged,
        thread_id=thread_id,
    )
    return {
        "msg_id": message.msg_id,
        "thread_id": message.thread_id,
        "to": message.recipient,
        "kind": message.kind,
        "attachments": [a["name"] for a in staged],
        "status": "open until answered",
        "next": f"agent_inbox to check for the answer, or agent_thread with thread_id {message.thread_id}",
    }


@action(
    name="agent_inbox",
    description="Read your own inbox: messages from other agents, newest first, with unread and open-request state.",
    capability="agent.messaging",
    group="fleet",
    parameters={
        "type": "object",
        "properties": {
            "unread_only": {"type": "boolean"},
            "limit": {"type": "integer"},
            "mark_read": {"type": "boolean", "description": "Mark what is returned as read."},
        },
    },
)
def agent_inbox(ctx: ActionContext, unread_only: bool = False, limit: int = 20, mark_read: bool = True) -> Any:
    comms = _comms()
    sender_id, _ = _me(ctx)
    messages = comms.inbox(sender_id, limit=limit, unread_only=unread_only)
    if mark_read and messages:
        comms.mark_read(sender_id, [m.msg_id for m in messages if not m.read])
    return {
        "agent_id": sender_id,
        "count": len(messages),
        "messages": [m.to_json() for m in messages],
        "stats": comms.stats(sender_id),
    }


@action(
    name="agent_reply",
    description=(
        "Answer an open help request by message id. The answer lands in the same thread and "
        "closes the request; answering an already-answered request is refused."
    ),
    capability="agent.messaging",
    group="fleet",
    parameters={
        "type": "object",
        "properties": {
            "msg_id": {"type": "string"},
            "answer": {"type": "string"},
            "attachments": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["msg_id", "answer"],
    },
)
def agent_reply(ctx: ActionContext, msg_id: str, answer: str, attachments: Sequence[str] | None = None) -> Any:
    comms = _comms()
    sender_id, sender_name = _me(ctx)
    staged = comms.stage_files(list(attachments or []), note="answer") if attachments else []
    message = comms.answer(msg_id, sender_id, sender_name, answer, staged)
    return {
        "msg_id": message.msg_id,
        "thread_id": message.thread_id,
        "answered_request": msg_id,
        "to": message.sender_name,
        "attachments": [a["name"] for a in staged],
    }


@action(
    name="agent_send_file",
    description=(
        "Hand one or more files to another agent. Files are copied into the comms store with "
        "a sha256, so the handover survives the sender's cleanup."
    ),
    capability="agent.messaging",
    group="fleet",
    parameters={
        "type": "object",
        "properties": {
            "to": {"type": "string"},
            "paths": {"type": "array", "items": {"type": "string"}},
            "note": {"type": "string", "description": "What the recipient should do with it."},
            "subject": {"type": "string"},
        },
        "required": ["to", "paths"],
    },
)
def agent_send_file(ctx: ActionContext, to: str, paths: Sequence[str], note: str = "", subject: str = "") -> Any:
    comms = _comms()
    sender_id, sender_name = _me(ctx)
    if not paths:
        raise ActionFailed("paths is empty: name at least one file to hand over")
    staged = comms.stage_files(list(paths), note=note)
    message = comms.send(
        sender_id=sender_id,
        sender_name=sender_name,
        recipient=to,
        subject=subject or f"{len(staged)} file(s)",
        body=note or f"Files: {', '.join(a['name'] for a in staged)}",
        kind="file",
        attachments=staged,
    )
    return {
        "msg_id": message.msg_id,
        "thread_id": message.thread_id,
        "to": message.recipient,
        "files": [{"name": a["name"], "bytes": a["bytes"], "sha256": a["sha256"], "stored_at": a["path"]} for a in staged],
    }


@action(
    name="agent_thread",
    description="Read a whole conversation thread in order, including its open or answered request state.",
    capability="agent.messaging",
    group="fleet",
    parameters={
        "type": "object",
        "properties": {"thread_id": {"type": "string"}, "limit": {"type": "integer"}},
        "required": ["thread_id"],
    },
)
def agent_thread(ctx: ActionContext, thread_id: str, limit: int = 50) -> Any:
    comms = _comms()
    messages = comms.thread(thread_id, limit=limit)
    if not messages:
        raise ActionFailed(f"no thread {thread_id!r} on this machine", {"known_stats": comms.stats()})
    return {"thread_id": thread_id, "count": len(messages), "messages": [m.to_json() for m in messages]}


@action(
    name="agent_requests",
    description="List help requests that nobody has answered yet — the manager's chase list.",
    capability="agent.messaging",
    group="fleet",
    parameters={"type": "object", "properties": {"mine_only": {"type": "boolean"}}},
)
def agent_requests(ctx: ActionContext, mine_only: bool = False) -> Any:
    comms = _comms()
    sender_id, _ = _me(ctx)
    rows = comms.open_requests(sender_id if mine_only else "")
    return {"count": len(rows), "open": [m.to_json() for m in rows]}


# --------------------------------------------------------------------------------------
# The model bench
# --------------------------------------------------------------------------------------


@action(
    name="model_add",
    description=(
        "Add a model (and, if needed, an OpenAI-compatible provider) with its real limits: "
        "requests per minute/day/week and tokens per minute/day/week. Add it as a reserve to "
        "keep it out of assignment until model_activate is called."
    ),
    capability="model.manage",
    group="fleet",
    parameters={
        "type": "object",
        "properties": {
            "provider": {"type": "string", "description": "Provider name, e.g. groq or my-local"},
            "model_id": {"type": "string"},
            "rpm": {"type": "integer", "description": "requests per minute"},
            "rpd": {"type": "integer", "description": "requests per day"},
            "rpw": {"type": "integer", "description": "requests per week"},
            "tpm": {"type": "integer", "description": "tokens per minute"},
            "tpd": {"type": "integer", "description": "tokens per day"},
            "tpw": {"type": "integer", "description": "tokens per week"},
            "context_window": {"type": "integer"},
            "base_url": {"type": "string", "description": "Required for a new provider."},
            "api_key_env": {"type": "string", "description": "Environment variable holding the key — never the key itself."},
            "kind": {"type": "string", "description": "openai_compatible|gemini|anthropic|echo"},
            "tags": {"type": "array", "items": {"type": "string"}, "description": "reasoning, fast, cheap, coding, vision…"},
            "reserve": {"type": "boolean", "description": "true keeps it on the bench until activated."},
            "audio_live": {"type": "boolean", "description": "Full-duplex live audio model (Sovereign class)."},
        },
        "required": ["provider", "model_id"],
    },
)
def model_add(
    ctx: ActionContext,
    provider: str,
    model_id: str,
    rpm: int | None = None,
    rpd: int | None = None,
    rpw: int | None = None,
    tpm: int | None = None,
    tpd: int | None = None,
    tpw: int | None = None,
    context_window: int = 128_000,
    base_url: str = "",
    api_key_env: str = "",
    kind: str = "openai_compatible",
    tags: Sequence[str] | None = None,
    reserve: bool = True,
    audio_live: bool = False,
) -> Any:
    reserves = _reserves()
    limits = ModelLimits(
        rpm=rpm, rpd=rpd, rpw=rpw, tpm=tpm, tpd=tpd, tpw=tpw, context_window=context_window
    )
    try:
        result = reserves.add_model(
            provider=provider,
            model_id=model_id,
            limits=limits,
            kind=kind,
            base_url=base_url,
            api_key_env=api_key_env,
            tags=tuple(tags or ()),
            reserve=reserve,
            supports_audio_live=audio_live,
        )
    except ValueError as exc:
        # A refused envelope is information, not a crash: hand the model the reason so it can
        # correct the numbers instead of retrying the same nonsense.
        raise ActionFailed(str(exc), {"provider": provider, "model": model_id, "limits": limits.to_json()}) from exc
    return result


@action(
    name="model_activate",
    description="Call a reserve model up onto the active bench. The Steward's authority; every activation is published.",
    capability="model.manage",
    group="fleet",
    parameters={
        "type": "object",
        "properties": {
            "model": {"type": "string", "description": "provider:model"},
            "reason": {"type": "string"},
        },
        "required": ["model"],
    },
)
def model_activate(ctx: ActionContext, model: str, reason: str = "") -> Any:
    sender_id, _ = _me(ctx)
    try:
        return _reserves().activate(model, by=sender_id, reason=reason)
    except ValueError as exc:
        raise ActionFailed(str(exc), {"model": model, "known": [r["model"] for r in _reserves().rows()]}) from exc


@action(
    name="model_retire",
    description="Stand a model back down onto the reserve bench, with a reason.",
    capability="model.manage",
    group="fleet",
    parameters={
        "type": "object",
        "properties": {"model": {"type": "string"}, "reason": {"type": "string"}},
        "required": ["model"],
    },
)
def model_retire(ctx: ActionContext, model: str, reason: str = "") -> Any:
    sender_id, _ = _me(ctx)
    try:
        return _reserves().retire(model, by=sender_id, reason=reason)
    except ValueError as exc:
        raise ActionFailed(str(exc), {"model": model}) from exc


@action(
    name="model_reserves",
    description="Show the model bench: every model, its declared limits, whether it is a reserve, and its real headroom.",
    capability="model.manage",
    group="fleet",
    parameters={"type": "object", "properties": {"only_reserves": {"type": "boolean"}}},
)
def model_reserves(ctx: ActionContext, only_reserves: bool = False) -> Any:
    reserves = _reserves()
    rows = reserves.reserves() if only_reserves else reserves.rows()
    return {
        "count": len(rows),
        "models": rows,
        "best_reserve": reserves.best_reserve(),
        "note": "headroom 0.0 means at least one window is spent; a reserve is not assigned until activated",
    }
