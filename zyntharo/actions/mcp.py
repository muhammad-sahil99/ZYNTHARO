"""MCP tools (spec 15.1) — the fleet can use outside servers, and outside clients can use us.

The split that matters here is **read against act**. An MCP server describes its tools with
hints, and the only hint that changes our behaviour is ``readOnlyHint``. A tool that claims to
be read-only is callable by an agent holding ``mcp.read``; everything else — including a tool
that says nothing at all — needs ``mcp.call`` *and* the owner's confirmation, because the hint
is the server's own claim about somebody else's program. Treating a missing hint as safe would
mean an unannotated ``rm -rf`` wrapper ran without a question.

Each call starts its server, uses it and stops it. That is deliberate: a cached process is a
process that can hang, hold a file lock, or outlive the task that spawned it, and the whole
point of the supervisor rules in this project is that nothing runs unwatched. The cost is a
process start per call, and it is stated in LIMITATIONS rather than hidden.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..mcp.client import MCPError, MCPToolError
from ..mcp.servers import ServerSpec, ServerStore, ServerStoreError
from .registry import ActionContext, ActionFailed, action


@dataclass
class McpServices:
    """Wired by ``cli.build_stack``. ``bus`` is used for the two failure events."""

    bus: Any = None
    config: Any = None
    servers: ServerStore | None = None


SERVICES: McpServices | None = None


def set_services(services: McpServices | None) -> None:
    global SERVICES
    SERVICES = services


def _store(ctx: ActionContext | None = None) -> ServerStore:
    services = SERVICES
    if services is not None and services.servers is not None:
        return services.servers
    config = (ctx.config if ctx is not None else None) or (services.config if services else None)
    home = getattr(config, "home_path", None)
    return ServerStore(home if home is not None else "")


def _bus(ctx: ActionContext) -> Any:
    if SERVICES is not None and SERVICES.bus is not None:
        return SERVICES.bus
    return ctx.bus


def _config(ctx: ActionContext) -> Any:
    if ctx.config is not None:
        return ctx.config
    return SERVICES.config if SERVICES is not None else None


def _spec(name: str) -> ServerSpec:
    try:
        return _store().get(name)
    except ServerStoreError as exc:
        raise ActionFailed(str(exc), {"configured": [s.name for s in _store().list()]}) from exc


def _with_server(name: str, work: Any) -> Any:
    """Run ``work(client)`` against a started server, always stopping it afterwards."""
    spec = _spec(name)
    client = spec.client()
    try:
        client.start()
        return work(client)
    except MCPError as exc:
        raise ActionFailed(str(exc), {"server": name, "command": list(spec.command)}) from exc
    finally:
        client.close()


# --------------------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------------------


@action(
    name="mcp_servers",
    description=(
        "List the MCP servers this machine is configured to use. Says which are configured and "
        "which command each runs; environment values are never shown, only their names."
    ),
    capability="mcp.read",
    group="mcp",
    parameters={"type": "object", "properties": {}},
)
def mcp_servers(ctx: ActionContext, **_: Any) -> dict[str, Any]:
    store = _store(ctx)
    servers = store.list()
    return {
        "count": len(servers),
        "config_path": str(store.path),
        "problem": store.problem,
        "servers": [
            {
                "name": spec.name,
                "command": list(spec.command),
                "env_names": sorted(spec.env),
                "note": spec.note,
                "timeout_s": spec.timeout,
            }
            for spec in servers
        ],
    }


@action(
    name="mcp_tools",
    description=(
        "Start one configured MCP server, list the tools it offers, and stop it. Each tool is "
        "reported with whether the server claims it is read-only; unclaimed tools need the owner's "
        "confirmation to call."
    ),
    capability="mcp.read",
    group="mcp",
    parameters={
        "type": "object",
        "properties": {"server": {"type": "string", "description": "Configured server name."}},
        "required": ["server"],
    },
)
def mcp_tools(ctx: ActionContext, server: str, **_: Any) -> dict[str, Any]:
    def work(client: Any) -> dict[str, Any]:
        tools = client.list_tools()
        return {
            "server": server,
            "server_version": client.info.version,
            "protocol": client.negotiated,
            "negotiated_different_from_ours": bool(
                client.negotiated and client.negotiated.split("-")[0] != _our_protocol_root()
            ),
            "instructions": client.instructions[:400],
            "count": len(tools),
            "tools": [
                {
                    "name": tool["name"],
                    "description": tool["description"][:300],
                    "read_only": bool((tool.get("annotations") or {}).get("readOnlyHint") is True),
                }
                for tool in tools
            ],
        }

    return _with_server(server, work)


def _our_protocol_root() -> str:
    from ..mcp.protocol import PROTOCOL_VERSION

    return PROTOCOL_VERSION.split("-")[0]


# --------------------------------------------------------------------------------------
# Calling
# --------------------------------------------------------------------------------------


def _announce_failure(ctx: ActionContext, server: str, tool: str, reason: str) -> None:
    from ..events import MCP_FAILED

    bus = _bus(ctx)
    if bus is not None:
        bus.emit(MCP_FAILED, source=ctx.agent_id, severity="warning", server=server, tool=tool, reason=reason[:300])


def _connect_event(ctx: ActionContext, server: str, tools: int, protocol: str) -> None:
    from ..events import MCP_CONNECTED

    bus = _bus(ctx)
    if bus is not None:
        bus.emit(
            MCP_CONNECTED,
            source=ctx.agent_id,
            severity="info",
            server=server,
            tools=tools,
            protocol=protocol,
        )


@action(
    name="mcp_read",
    description=(
        "Call a tool on a configured MCP server that declares itself read-only. A tool that does "
        "not declare read-only is refused here by name — use mcp_call, which asks the owner first."
    ),
    capability="mcp.read",
    group="mcp",
    parameters={
        "type": "object",
        "properties": {
            "server": {"type": "string", "description": "Configured server name."},
            "tool": {"type": "string", "description": "Tool name as the server reports it."},
            "arguments": {"type": "object", "description": "Arguments object for the tool."},
        },
        "required": ["server", "tool"],
    },
)
def mcp_read(
    ctx: ActionContext,
    server: str,
    tool: str,
    arguments: dict[str, Any] | None = None,
    **_: Any,
) -> dict[str, Any]:
    def work(client: Any) -> dict[str, Any]:
        listing = {t["name"]: t for t in client.list_tools()}
        described = listing.get(tool)
        if described is None:
            raise ActionFailed(
                f"server {server!r} has no tool called {tool!r}",
                {"available": sorted(listing)[:40]},
            )
        if (described.get("annotations") or {}).get("readOnlyHint") is not True:
            raise ActionFailed(
                f"{tool!r} on {server!r} does not declare itself read-only, so it is not a read: "
                "call mcp_call, which requires the owner's confirmation",
                {"read_only": False, "server": server, "tool": tool},
            )
        _connect_event(ctx, server, len(listing), client.negotiated)
        text = client.call_tool(tool, arguments or {})
        return {"server": server, "tool": tool, "read_only": True, "text": text}

    try:
        return _with_server(server, work)
    except MCPToolError as exc:
        _announce_failure(ctx, server, tool, str(exc))
        raise ActionFailed(f"{tool!r} on {server!r} reported an error: {exc}", {"server": server, "tool": tool}) from exc
    except ActionFailed as exc:
        if exc.evidence.get("read_only") is False:
            _announce_failure(ctx, server, tool, "not a read-only tool")
        raise


@action(
    name="mcp_call",
    description=(
        "Call any tool on a configured MCP server, including one that changes things. Requires the "
        "owner's confirmation, because the server's own annotation is the only claim about what the "
        "tool does. The server's answer text is returned verbatim."
    ),
    capability="mcp.call",
    group="mcp",
    requires_confirm=True,
    danger="medium",
    parameters={
        "type": "object",
        "properties": {
            "server": {"type": "string", "description": "Configured server name."},
            "tool": {"type": "string", "description": "Tool name as the server reports it."},
            "arguments": {"type": "object", "description": "Arguments object for the tool."},
        },
        "required": ["server", "tool"],
    },
)
def mcp_call(
    ctx: ActionContext,
    server: str,
    tool: str,
    arguments: dict[str, Any] | None = None,
    **_: Any,
) -> dict[str, Any]:
    def work(client: Any) -> dict[str, Any]:
        listing = {t["name"]: t for t in client.list_tools()}
        if tool not in listing:
            raise ActionFailed(
                f"server {server!r} has no tool called {tool!r}",
                {"available": sorted(listing)[:40]},
            )
        read_only = (listing[tool].get("annotations") or {}).get("readOnlyHint") is True
        _connect_event(ctx, server, len(listing), client.negotiated)
        text = client.call_tool(tool, arguments or {})
        return {"server": server, "tool": tool, "read_only": read_only, "text": text}

    try:
        return _with_server(server, work)
    except MCPToolError as exc:
        _announce_failure(ctx, server, tool, str(exc))
        raise ActionFailed(f"{tool!r} on {server!r} reported an error: {exc}", {"server": server, "tool": tool}) from exc


# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------


@action(
    name="mcp_add",
    description=(
        "Register an MCP server by command line, e.g. a filesystem or GitHub server. The command "
        "is stored, never started now: it runs only when one of its tools is called. Environment "
        "values are stored 0600 and are never echoed back."
    ),
    capability="mcp.admin",
    group="mcp",
    requires_confirm=True,
    danger="medium",
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Short name, used as the tool prefix."},
            "command": {"type": "array", "items": {"type": "string"}, "description": "Executable and arguments."},
            "env": {"type": "object", "description": "Environment variables for the server, e.g. a token."},
            "note": {"type": "string", "description": "Why it is configured, for the panel."},
            "timeout_s": {"type": "number", "description": "Per-request timeout, 1–600 seconds."},
        },
        "required": ["name", "command"],
    },
)
def mcp_add(
    ctx: ActionContext,
    name: str,
    command: list[str],
    env: dict[str, str] | None = None,
    note: str = "",
    timeout_s: float = 25.0,
    **_: Any,
) -> dict[str, Any]:
    from ..mcp.client import DEFAULT_TIMEOUT

    spec = ServerSpec(
        name=name,
        command=tuple(command),
        env={str(k): str(v) for k, v in (env or {}).items()},
        note=note,
        timeout=float(timeout_s or DEFAULT_TIMEOUT),
    )
    try:
        _store(ctx).add(spec, replace=False)
    except ServerStoreError as exc:
        raise ActionFailed(str(exc)) from exc
    return {"added": spec.name, "command": list(spec.command), "env_names": sorted(spec.env)}


@action(
    name="mcp_remove",
    description="Remove a configured MCP server. The stored command and its environment values are deleted.",
    capability="mcp.admin",
    group="mcp",
    parameters={
        "type": "object",
        "properties": {"name": {"type": "string", "description": "Configured server name."}},
        "required": ["name"],
    },
)
def mcp_remove(ctx: ActionContext, name: str, **_: Any) -> dict[str, Any]:
    try:
        spec = _store(ctx).remove(name)
    except ServerStoreError as exc:
        raise ActionFailed(str(exc)) from exc
    return {"removed": spec.name, "removed_env_names": sorted(spec.env)}
