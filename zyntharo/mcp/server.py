"""MCP over stdio — the server half: ZYNTHARO's own tools, offered to any MCP client.

This is the direction that makes the product composable: Claude Desktop, an IDE, another agent
framework or the customer's own script can drive the same capability-gated action registry the
fleet uses. Three rules make that safe rather than a remote shell:

1. **The capability gate is the same gate.** The server runs with one :class:`ActionContext`,
   built from the capabilities the owner granted that client. A tool the client was not granted
   is not listed *and* is refused if guessed.
2. **Confirmation is not delegated.** A destructive action refuses over MCP exactly as it does
   locally (`requires confirmation`), unless the owner granted it in the HUD. An outside client
   must never be able to talk the system past the gate by calling it from a different door.
3. **The result is the receipt.** Every call goes through ``registry.invoke``, so it is audited,
   journalled, verified and emitted on the bus, whether the caller was the local HUD or a remote
   MCP client.
"""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from ..actions.registry import ActionContext, ActionRegistry
from ..config import AppConfig
from ..events import MCP_SERVER_STARTED, MCP_TOOL_CALLED, EventBus
from . import protocol
from .protocol import ProtocolError

#: Capabilities that only read. Used for one thing: the ``readOnlyHint`` this server publishes, so
#: an outside client knows which of our tools it may call without asking the owner. Deliberately a
#: short allowlist rather than "anything ending in .read": a capability is read-only because
#: someone decided it was, not because of how it is spelled.
READ_ONLY_CAPABILITIES: frozenset[str] = frozenset(
    {
        "vision.capture",
        "windows.list",
        "filesystem.read",
        "process.list",
        "hardware.read",
        "channel.read",
        "connector.read",
        "presence.read",
        "mcp.read",
    }
)


class MCPServer:
    """Serves the action registry over MCP. ``handle`` is pure and testable without a pipe."""

    def __init__(
        self,
        registry: ActionRegistry,
        capabilities: Any,
        agent_id: str = "mcp-client",
        config: AppConfig | None = None,
        bus: EventBus | None = None,
        name: str = "zyntharo",
        version: str = "",
        confirmed: tuple[str, ...] = (),
    ):
        self.registry = registry
        self.capabilities = frozenset(capabilities)
        self.agent_id = agent_id
        self.config = config
        self.bus = bus or getattr(registry, "bus", None) or EventBus()
        self.name = name
        self.version = version or _version()
        self.confirmed = tuple(confirmed)
        self.calls = 0

    # -- context -----------------------------------------------------------------------
    def context(self) -> ActionContext:
        return ActionContext(
            agent_id=self.agent_id,
            capabilities=self.capabilities,
            bus=self.bus,
            config=self.config,
            confirmed=self.confirmed,
        )

    # -- discovery ---------------------------------------------------------------------
    def tool_list(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        specs = self.registry.tool_specs(self.capabilities, available_only=False)
        for spec in specs:
            action = self.registry.get(spec.name)
            capability = action.capability if action is not None else spec.capability
            tools.append(
                {
                    "name": spec.name,
                    "description": spec.description,
                    "inputSchema": spec.parameters or {"type": "object", "properties": {}},
                    "annotations": {
                        "readOnlyHint": capability in READ_ONLY_CAPABILITIES,
                        "destructiveHint": bool(action.requires_confirm) if action is not None else False,
                        "title": action.group if action is not None else "",
                    },
                }
            )
        return tools

    # -- dispatch ----------------------------------------------------------------------
    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """Answer one JSON-RPC message. Returns ``None`` for notifications."""
        try:
            parsed = protocol.decode(protocol.encode(message)) if isinstance(message, dict) else None
        except ProtocolError as exc:
            return protocol.error(None, exc.code, str(exc))
        assert parsed is not None
        if parsed.is_notification:
            return None
        try:
            result = self._route(parsed.method, parsed.params)
        except ProtocolError as exc:
            return protocol.error(parsed.id, exc.code, str(exc))
        except Exception as exc:  # noqa: BLE001 - MCP requires an error answer, not a traceback
            return protocol.error(parsed.id, protocol.INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")
        return protocol.result(parsed.id, result)

    def _route(self, method: str, params: dict[str, Any]) -> Any:
        if method == protocol.INITIALIZE:
            return {
                "protocolVersion": protocol.PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": self.name, "version": self.version},
                "instructions": (
                    "ZYNTHARO's capability-gated tools. Destructive tools refuse unless the owner "
                    "has granted them; the refusal text says which capability was missing."
                ),
            }
        if method == protocol.PING:
            return {}
        if method == protocol.TOOLS_LIST:
            return {"tools": self.tool_list()}
        if method == protocol.TOOLS_CALL:
            return self._call_tool(params)
        if method in protocol.NOTIFICATIONS:
            return {}
        raise ProtocolError(f"unknown method {method!r}", protocol.METHOD_NOT_FOUND)

    def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise ProtocolError("tools/call needs a tool name", protocol.INVALID_PARAMS)
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise ProtocolError("arguments must be an object", protocol.INVALID_PARAMS)
        action = self.registry.get(name)
        if action is None:
            raise ProtocolError(f"unknown tool {name!r}", protocol.INVALID_PARAMS)
        if action.capability not in self.capabilities:
            # Same wording the local gate uses, so a client cannot learn the tool list by guessing.
            return protocol.text_result(
                f"capability denied: {action.capability}", is_error=True
            )
        self.calls += 1
        try:
            result = self.registry.invoke(name, self.context(), **arguments)
        except TypeError as exc:
            # A client that sends an argument the tool does not take gets the tool's signature,
            # not a stack trace: it is a caller bug and the caller can fix it.
            raise ProtocolError(f"bad arguments for {name}: {exc}", protocol.INVALID_PARAMS) from exc
        payload = result.to_json()
        self.bus.emit(
            MCP_TOOL_CALLED,
            source=self.agent_id,
            severity="info" if result.ok else "warning",
            tool=name,
            ok=result.ok,
            verified=result.verified,
            duration=payload.get("duration"),
        )
        if result.ok:
            text = payload["output"] if isinstance(payload.get("output"), str) else json.dumps(payload["output"], ensure_ascii=True)
            return protocol.text_result(text)
        return protocol.text_result(result.error or "the tool failed with no message", is_error=True)

    # -- transport ---------------------------------------------------------------------
    def serve_stdio(self, stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
        """Read request lines until EOF. Returns the number of calls served."""
        source = stdin or sys.stdin
        sink = stdout or sys.stdout
        self.bus.emit(
            MCP_SERVER_STARTED,
            source=self.agent_id,
            severity="notice",
            capabilities=len(self.capabilities),
            tools=len(self.tool_list()),
        )
        for line in source:
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                sink.write(json.dumps(protocol.error(None, protocol.PARSE_ERROR, str(exc))))
                sink.write("\n")
                sink.flush()
                continue
            answer = self.handle(message)
            if answer is not None:
                sink.write(json.dumps(answer, ensure_ascii=True))
                sink.write("\n")
                sink.flush()
        return self.calls


def _version() -> str:
    from .. import __version__

    return __version__


def serve(
    registry: ActionRegistry,
    capabilities: Any,
    config: AppConfig | None = None,
    bus: EventBus | None = None,
    agent_id: str = "mcp-client",
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    confirmed: tuple[str, ...] = (),
) -> int:
    """Convenience entry point used by ``zyntharo mcp serve``."""
    server = MCPServer(
        registry=registry,
        capabilities=capabilities,
        agent_id=agent_id,
        config=config,
        bus=bus,
        confirmed=confirmed,
    )
    return server.serve_stdio(stdin, stdout)


__all__ = ["MCPServer", "READ_ONLY_CAPABILITIES", "serve"]
