"""Connected accounts as tools the fleet can call.

The gating is the interesting part. One generic "call an endpoint" tool would be a hole straight
through the capability system: a model holding ``connector.read`` could reach a send endpoint by
naming it. So the surface is split by the operation's own declared capability:

* ``connector_read`` can only reach operations declared ``connector.read``;
* ``connector_write`` can only reach ``connector.write`` (and refuses sends);
* ``connector_send`` is the only path to mail/message delivery — owner-only, confirmation-gated
  and irreversible, because a delivered email cannot be recalled.

Each action re-checks the operation's capability against the *caller's* granted set, so a
sub-agent with read access cannot escalate by picking a different tool name.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .registry import ActionContext, ActionFailed, DriverUnavailable, action

READ = "connector.read"
WRITE = "connector.write"
SEND = "connector.send"
ADMIN = "connector.admin"


@dataclass(slots=True)
class ConnectorServices:
    connectors: Any


_SERVICES: ConnectorServices | None = None


def set_services(services: ConnectorServices | None) -> None:
    global _SERVICES
    _SERVICES = services


def _svc() -> ConnectorServices:
    if _SERVICES is None:
        raise DriverUnavailable(
            "connectors",
            "the connector registry",
            "This stack was assembled without connectors; start the app normally (zyntharo hud) or "
            "pass services to build_registry.",
        )
    return _SERVICES


def _connect(name: str) -> Any:
    try:
        return _svc().connectors.get(name)
    except KeyError as exc:
        raise ActionFailed(str(exc)) from exc
    except Exception as exc:  # Unsupported and friends carry the business gate as text
        raise ActionFailed(str(exc)) from exc


def _split(operation: str, args: dict[str, Any] | None) -> tuple[str, dict[str, Any]]:
    """Allow ``operation`` to carry its own arguments as ``operation: {json}``-style extras."""
    return str(operation).strip(), dict(args or {})


@action(
    name="connector_list",
    description=(
        "Every outside account ZYNTHARO can connect (mail, drives, calendar, chat, CRM, code, "
        "payments), which are connected now, and what each can do. Call this before promising a "
        "customer that a task is possible."
    ),
    capability=READ,
    group="connectors",
    parameters={"type": "object", "properties": {}},
)
def connector_list(ctx: ActionContext) -> Any:
    registry = _svc().connectors
    rows = registry.to_json()
    return {
        "connected": registry.ready(),
        "total": len(rows),
        "connectors": rows,
    }


@action(
    name="connector_operations",
    description="The exact operations one connected account offers, each with the permission it needs.",
    capability=READ,
    group="connectors",
    parameters={
        "type": "object",
        "properties": {"connector": {"type": "string", "description": "e.g. google, microsoft, mail, slack"}},
        "required": ["connector"],
    },
)
def connector_operations(ctx: ActionContext, connector: str) -> Any:
    target = _connect(connector)
    return {
        "connector": target.name,
        "title": target.title,
        "state": target.status().to_json(),
        "operations": [op.to_json() for op in target.operations()],
    }


def _guarded_call(connector: str, operation: str, args: dict[str, Any], ctx: ActionContext, allowed: str) -> Any:
    target = _connect(connector)
    op = target._operation(operation)
    if op.capability != allowed:
        raise ActionFailed(
            f"{target.title}.{operation} needs {op.capability}, not {allowed}. "
            f"Use the tool that matches it."
        )
    if not ctx.can(op.capability):
        raise ActionFailed(
            f"capability denied: {op.capability} is required for {target.title}.{operation}"
        )
    status = target.status()
    if status.state != "ready":
        raise ActionFailed(f"{target.title} is {status.state}: {status.detail}")
    return {**{"connector": target.name, "operation": operation}, "result": target.call(operation, **args)}


@action(
    name="connector_read",
    description=(
        "Read from a connected account: list mail, read a message, list Drive/OneDrive files, "
        "calendar events, spreadsheet rows, CRM records, issues. Never changes anything."
    ),
    capability=READ,
    group="connectors",
    parameters={
        "type": "object",
        "properties": {
            "connector": {"type": "string"},
            "operation": {"type": "string"},
            "args": {"type": "object", "description": "That operation's own arguments."},
        },
        "required": ["connector", "operation"],
    },
)
def connector_read(ctx: ActionContext, connector: str, operation: str, args: dict[str, Any] | None = None) -> Any:
    name, values = _split(operation, args)
    return _guarded_call(connector, name, values, ctx, READ)


@action(
    name="connector_write",
    description=(
        "Change something in a connected account without sending it anywhere: create a calendar "
        "event, append spreadsheet rows, move a message, update a CRM record, upload a file."
    ),
    capability=WRITE,
    group="connectors",
    danger="medium",
    parameters={
        "type": "object",
        "properties": {
            "connector": {"type": "string"},
            "operation": {"type": "string"},
            "args": {"type": "object"},
        },
        "required": ["connector", "operation"],
    },
)
def connector_write(ctx: ActionContext, connector: str, operation: str, args: dict[str, Any] | None = None) -> Any:
    name, values = _split(operation, args)
    return _guarded_call(connector, name, values, ctx, WRITE)


@action(
    name="connector_send",
    description=(
        "Send mail or a message from a connected account. Owner-only and confirmation-gated: a "
        "delivered message cannot be recalled. Prefer preparing a draft for approval instead."
    ),
    capability=SEND,
    group="connectors",
    requires_confirm=True,
    danger="high",
    parameters={
        "type": "object",
        "properties": {
            "connector": {"type": "string"},
            "operation": {"type": "string"},
            "args": {"type": "object"},
        },
        "required": ["connector", "operation"],
    },
)
def connector_send(ctx: ActionContext, connector: str, operation: str, args: dict[str, Any] | None = None) -> Any:
    name, values = _split(operation, args)
    return _guarded_call(connector, name, values, ctx, SEND)


@action(
    name="connector_connect",
    description=(
        "Store a customer's credential for one connector and immediately verify it against the "
        "real service. Owner action: the values are written to the local vault and never logged."
    ),
    capability=ADMIN,
    group="connectors",
    requires_confirm=True,
    danger="medium",
    parameters={
        "type": "object",
        "properties": {
            "connector": {"type": "string"},
            "values": {"type": "object", "description": "e.g. {token}, {key, token}, {address, password}"},
            "meta": {"type": "object", "description": "Optional: provider, account label."},
        },
        "required": ["connector", "values"],
    },
)
def connector_connect(ctx: ActionContext, connector: str, values: dict[str, str], meta: dict[str, str] | None = None) -> Any:
    registry = _svc().connectors
    status = registry.connect(connector, values or {}, meta or {})
    return {"connector": status.name, "state": status.state, "detail": status.detail, "account": status.account}


@action(
    name="connector_verify",
    description="Prove a stored credential still works, and record when it was last proven.",
    capability=ADMIN,
    group="connectors",
    parameters={
        "type": "object",
        "properties": {"connector": {"type": "string"}},
        "required": ["connector"],
    },
)
def connector_verify(ctx: ActionContext, connector: str) -> Any:
    status = _svc().connectors.verify(connector)
    return status.to_json()


@action(
    name="connector_disconnect",
    description="Remove a stored credential from this machine. The remote token stays valid until you revoke it there too.",
    capability=ADMIN,
    group="connectors",
    requires_confirm=True,
    danger="medium",
    parameters={
        "type": "object",
        "properties": {"connector": {"type": "string"}},
        "required": ["connector"],
    },
)
def connector_disconnect(ctx: ActionContext, connector: str) -> Any:
    removed = _svc().connectors.disconnect(connector)
    if not removed:
        raise ActionFailed(f"{connector} had no stored credential on this machine")
    return {
        "connector": connector,
        "disconnected": True,
        "note": "The credential is gone from this machine. Revoke it at the provider if the token was also used elsewhere.",
    }


def all_actions() -> list[Any]:
    return [
        connector_list,
        connector_operations,
        connector_read,
        connector_write,
        connector_send,
        connector_connect,
        connector_verify,
        connector_disconnect,
    ]
