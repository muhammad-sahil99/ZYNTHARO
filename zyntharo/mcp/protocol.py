"""The Model Context Protocol wire format: JSON-RPC 2.0, one message per line, on stdio.

Why this is hand-written rather than pulled from an SDK: the transport is the part that can be
wrong, and a 200-line encoder is cheaper to reason about than a dependency whose failure modes
appear inside a task the customer is watching. It also keeps the rule that ZYNTHARO's core is
stdlib-only, so a packaged install needs nothing.

Two decisions worth knowing:

* **Newline-delimited JSON, not `Content-Length` framing.** MCP's stdio transport uses LSP-style
  headers in some revisions and plain lines in others; the servers ZYNTHARO talks to use lines.
  A header-framed message is detected and refused with the reason instead of being parsed as
  garbage, because a half-understood stream produces confidently wrong tool calls.
* **A malformed line never kills the session.** It produces a JSON-RPC parse error with a null
  id, and the reader continues: one noisy server log line must not take the fleet down.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

#: The revision this client and server implement. Sent in ``initialize``; the peer answers with
#: the revision it will actually speak, and the client reports a mismatch rather than pretending.
PROTOCOL_VERSION = "2025-06-18"

# Methods
INITIALIZE = "initialize"
INITIALIZED = "notifications/initialized"
PING = "ping"
TOOLS_LIST = "tools/list"
TOOLS_CALL = "tools/call"
#: Server -> client notifications we accept and ignore, listed so they are not "unknown".
NOTIFICATIONS = frozenset({"notifications/message", "notifications/cancelled", "notifications/progress"})

# JSON-RPC error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class ProtocolError(ValueError):
    """A message could not be understood. Carries the JSON-RPC code to answer with."""

    def __init__(self, message: str, code: int = INVALID_REQUEST):
        super().__init__(message)
        self.code = code


class RpcError(RuntimeError):
    """The peer answered with a JSON-RPC error object."""

    def __init__(self, message: str, code: int = INTERNAL_ERROR, data: Any = None):
        super().__init__(message)
        self.code = code
        self.data = data


@dataclass(slots=True)
class Message:
    """One JSON-RPC message, validated far enough to route safely."""

    id: int | str | None = None
    method: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    result: Any = None
    error: dict[str, Any] | None = None
    is_notification: bool = False

    @property
    def is_request(self) -> bool:
        return bool(self.method) and self.id is not None

    @property
    def is_response(self) -> bool:
        return not self.method and self.id is not None


def encode(message: dict[str, Any]) -> bytes:
    """Encode one message as a single line. ``ensure_ascii`` keeps byte offsets predictable."""
    if not isinstance(message, dict):
        raise ProtocolError("a JSON-RPC message must be an object", INVALID_REQUEST)
    return (json.dumps(message, ensure_ascii=True, separators=(",", ":")) + "\n").encode("utf-8")


def decode(line: str | bytes) -> Message:
    """Parse one line into a :class:`Message` or raise :class:`ProtocolError`.

    A line that is empty is a transport artefact (a stray newline), not an error.
    """
    raw = line.decode("utf-8", "replace") if isinstance(line, bytes) else line
    text = raw.strip()
    if not text:
        raise ProtocolError("empty line", PARSE_ERROR)
    if text.lower().startswith("content-length:"):
        raise ProtocolError(
            "the peer is using LSP/Content-Length framing, which this transport does not speak; "
            "configure the server for newline-delimited JSON",
            INVALID_REQUEST,
        )
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"not JSON: {exc.msg} at column {exc.colno}", PARSE_ERROR) from exc
    if not isinstance(payload, dict):
        raise ProtocolError("a JSON-RPC message must be an object", INVALID_REQUEST)
    if payload.get("jsonrpc") != "2.0":
        raise ProtocolError("missing or wrong jsonrpc version (must be \"2.0\")", INVALID_REQUEST)

    error = payload.get("error")
    if error is not None and not isinstance(error, dict):
        raise ProtocolError("error must be an object", INVALID_REQUEST)
    params = payload.get("params") or {}
    if not isinstance(params, dict):
        raise ProtocolError("params must be an object", INVALID_PARAMS)
    method = payload.get("method") or ""
    if not isinstance(method, str):
        raise ProtocolError("method must be a string", INVALID_REQUEST)
    return Message(
        id=payload.get("id"),
        method=method,
        params=params,
        result=payload.get("result"),
        error=error,
        is_notification=bool(method) and "id" not in payload,
    )


def request(message_id: int | str, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "method": method, "params": params or {}}


def notification(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "method": method, "params": params or {}}


def result(message_id: int | str | None, value: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "result": value}


def error(message_id: int | str | None, code: int, message: str, data: Any = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        payload["data"] = data
    return {"jsonrpc": "2.0", "id": message_id, "error": payload}


def text_result(text: str, is_error: bool = False) -> dict[str, Any]:
    """The MCP tool result shape: a content list, plus the error flag a caller must honour."""
    return {"content": [{"type": "text", "text": text}], "isError": bool(is_error)}


def read_text(result: Any) -> str:
    """Flatten a tool result's content list to text, refusing shapes we cannot read.

    Returning ``str(result)`` here would be the friendly-looking bug: a caller would parse a
    Python repr as if it were the tool's answer.
    """
    if not isinstance(result, dict):
        raise ProtocolError("tool result must be an object", INVALID_PARAMS)
    content = result.get("content")
    if content is None:
        # Some servers answer with a bare structured payload; that is legal and useful.
        return json.dumps(result.get("structuredContent", result), ensure_ascii=True)
    if not isinstance(content, list):
        raise ProtocolError("tool result content must be a list", INVALID_PARAMS)
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text" and isinstance(item.get("text"), str):
            parts.append(item["text"])
        elif item.get("type") == "resource" and isinstance(item.get("resource"), dict):
            parts.append(json.dumps(item["resource"], ensure_ascii=True))
    return "\n".join(parts)
