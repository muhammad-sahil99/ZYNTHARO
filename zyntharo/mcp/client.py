"""MCP over stdio — the client half: a real subprocess, a real handshake, honest failures.

What goes wrong with naive MCP clients, and what this one does instead:

* **A hang looks like a slow tool.** Every read has a deadline; when it expires the child is
  killed and the error names the server, the method and the last stderr lines, so a task fails
  with a cause instead of freezing the fleet.
* **A killed process leaves a live pipe.** ``close`` closes stdin, waits, then terminates and
  finally kills, and every thread it started is joined. Nothing outlives the caller.
* **A server that answers the wrong request.** Responses are matched to their id; an id that
  was never sent is dropped (with a counter) rather than being handed back as the answer to
  whatever the caller asked.
* **A chatty server fills the pipe and deadlocks.** stderr is drained continuously into a
  bounded ring buffer, which is also what ends up in error messages.
"""

from __future__ import annotations

import collections
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from . import protocol
from .protocol import PROTOCOL_VERSION, ProtocolError, RpcError

#: A server's answer larger than this is killed rather than buffered: a runaway tool returning a
#: gigabyte of text would take the whole machine down with it.
MAX_MESSAGE_BYTES = 4 * 1024 * 1024
#: Default for any single request. Generous, because a real tool (a browser, a database) may take
#: seconds, but finite, because "no answer ever" is the failure this number exists to expose.
DEFAULT_TIMEOUT = 25.0


class MCPError(RuntimeError):
    """The server could not be started, stopped answering, or exited."""


class MCPToolError(RuntimeError):
    """The tool ran and reported failure. ``message`` is the server's own text, verbatim."""

    def __init__(self, tool: str, message: str, payload: Any = None):
        super().__init__(message)
        self.tool = tool
        self.payload = payload


@dataclass(slots=True)
class ServerInfo:
    name: str
    version: str = ""
    protocol: str = ""
    instructions: str = ""


class StdioMCPClient:
    """One MCP server, started on demand and stopped deterministically."""

    def __init__(
        self,
        name: str,
        command: Sequence[str],
        extra_env: dict[str, str] | None = None,
        cwd: str = "",
        timeout: float = DEFAULT_TIMEOUT,
    ):
        if not command:
            raise ValueError("an MCP server needs a command")
        self.name = name
        self.command = list(command)
        self.extra_env = dict(extra_env or {})
        self.cwd = cwd or None
        self.timeout = float(timeout)
        self.info = ServerInfo(name=name)
        self.negotiated = ""
        self.instructions = ""
        self._proc: subprocess.Popen[bytes] | None = None
        self._inbox: queue.Queue[bytes | None] = queue.Queue()
        self._stderr: collections.deque[str] = collections.deque(maxlen=40)
        self._next_id = 0
        self._threads: list[threading.Thread] = []
        self.dropped = 0
        self.started = False

    # -- lifecycle ---------------------------------------------------------------------
    def start(self) -> "StdioMCPClient":
        if self._proc is not None and self._proc.poll() is None:
            return self
        env = os.environ.copy()
        env.update(self.extra_env)
        try:
            self._proc = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self.cwd,
                env=env,
                bufsize=0,
            )
        except (OSError, FileNotFoundError) as exc:
            raise MCPError(
                f"MCP server {self.name!r} could not be started: {exc}. "
                f"Install it, or remove it with `zyntharo mcp remove {self.name}`."
            ) from exc
        self._pump(self._proc.stdout, self._inbox)
        self._pump(self._proc.stderr, self._stderr)
        self.started = True
        self._handshake()
        return self

    def _pump(self, stream: Any, sink: Any) -> None:
        def run() -> None:
            try:
                for line in iter(stream.readline, b""):
                    if isinstance(sink, queue.Queue):
                        sink.put(line)
                    else:
                        text = line.decode("utf-8", "replace").rstrip()
                        if text:
                            sink.append(text)
            except (ValueError, OSError):  # pipe closed under us during shutdown
                pass
            finally:
                if isinstance(sink, queue.Queue):
                    sink.put(None)
                try:
                    stream.close()
                except Exception:  # noqa: BLE001 - closing a dead pipe must not raise
                    pass

        thread = threading.Thread(target=run, name=f"mcp-{self.name}-pump", daemon=True)
        thread.start()
        self._threads.append(thread)

    def _handshake(self) -> None:
        result = self._call(
            protocol.INITIALIZE,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "clientInfo": {"name": "zyntharo", "version": _version()},
            },
        )
        if not isinstance(result, dict):
            raise ProtocolError("initialize must answer with an object", protocol.INTERNAL_ERROR)
        server = result.get("serverInfo") or {}
        self.info = ServerInfo(
            name=str(server.get("name") or self.name),
            version=str(server.get("version") or ""),
            protocol=str(result.get("protocolVersion") or ""),
        )
        self.negotiated = self.info.protocol
        self.instructions = str(result.get("instructions") or "")
        # The spec says a client confirms; a server that does not want it will ignore or log it,
        # and neither is our problem.
        self._send(protocol.notification(protocol.INITIALIZED, {}))

    def close(self) -> None:
        proc = self._proc
        self._proc = None
        self.started = False
        if proc is None:
            return
        for stream in (proc.stdin,):
            try:
                if stream is not None:
                    stream.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:  # pragma: no cover - a wedged kernel process
                    pass
        for thread in self._threads:
            thread.join(timeout=1)
        self._threads.clear()

    def __enter__(self) -> "StdioMCPClient":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- transport ---------------------------------------------------------------------
    def _send(self, message: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise MCPError(f"MCP server {self.name!r} is not running")
        try:
            proc.stdin.write(protocol.encode(message))
            proc.stdin.flush()
        except (BrokenPipeError, ValueError, OSError) as exc:
            raise MCPError(f"MCP server {self.name!r} closed its input: {self._why(exc)}") from exc

    def _why(self, exc: Exception | None = None) -> str:
        tail = " | ".join(self._stderr) if self._stderr else "no stderr output"
        proc = self._proc
        code = proc.poll() if proc is not None else None
        exit_note = f"exit code {code}; " if code is not None else ""
        base = f"{exit_note}{tail}"
        return f"{base} ({exc})" if exc else base

    def _call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self._next_id += 1
        message_id = self._next_id
        self._send(protocol.request(message_id, method, params or {}))
        deadline = time.time() + self.timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                self.close()
                raise MCPError(
                    f"MCP server {self.name!r} did not answer {method} within {self.timeout:.0f}s; "
                    f"it was stopped. {self._why()}"
                )
            try:
                line = self._inbox.get(timeout=remaining)
            except queue.Empty:
                continue
            if line is None:
                # The reader hit EOF: the process is gone. Say which, not "empty response".
                proc = self._proc
                code = proc.poll() if proc is not None else None
                raise MCPError(
                    f"MCP server {self.name!r} exited before answering {method} "
                    f"(exit code {code if code is not None else 'unknown'}); {self._why()}"
                )
            if len(line) > MAX_MESSAGE_BYTES:
                self.close()
                raise MCPError(
                    f"MCP server {self.name!r} sent {len(line)} bytes for {method}, above the "
                    f"{MAX_MESSAGE_BYTES} byte ceiling; it was stopped"
                )
            try:
                message = protocol.decode(line)
            except ProtocolError:
                # A log line on stdout happens; it is not our answer, so keep reading.
                self.dropped += 1
                continue
            if message.id is not None and message.id != message_id:
                self.dropped += 1
                continue
            if message.error is not None:
                raise RpcError(
                    str(message.error.get("message") or "the server returned an error"),
                    int(message.error.get("code") or protocol.INTERNAL_ERROR),
                    message.error.get("data"),
                )
            return message.result

    # -- the two operations a caller needs ---------------------------------------------
    def list_tools(self) -> list[dict[str, Any]]:
        result = self._call(protocol.TOOLS_LIST, {})
        tools = (result or {}).get("tools") if isinstance(result, dict) else None
        if not isinstance(tools, list):
            raise ProtocolError("tools/list must answer with a tools list", protocol.INTERNAL_ERROR)
        cleaned: list[dict[str, Any]] = []
        for item in tools:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                continue
            schema = item.get("inputSchema")
            cleaned.append(
                {
                    "name": item["name"],
                    "description": str(item.get("description") or ""),
                    "input_schema": schema if isinstance(schema, dict) else {"type": "object", "properties": {}},
                    "annotations": item.get("annotations") if isinstance(item.get("annotations"), dict) else {},
                }
            )
        return cleaned

    def call_tool(self, tool: str, arguments: dict[str, Any] | None = None) -> str:
        result = self._call(protocol.TOOLS_CALL, {"name": tool, "arguments": dict(arguments or {})})
        text = protocol.read_text(result)
        if isinstance(result, dict) and result.get("isError"):
            raise MCPToolError(tool, text or "the tool reported an error with no text", result)
        return text

    # -- introspection -----------------------------------------------------------------
    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "command": self.command,
            "server": self.info.name,
            "server_version": self.info.version,
            "protocol": self.negotiated,
            "running": bool(self._proc is not None and self._proc.poll() is None),
            "dropped_messages": self.dropped,
        }


def _version() -> str:
    from .. import __version__

    return __version__


@dataclass(slots=True)
class ToolListing:
    """A tool from a server, flattened with the server it came from."""

    server: str
    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})
    read_only: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "server": self.server,
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "read_only": self.read_only,
        }


def is_read_only(annotations: dict[str, Any]) -> bool:
    """Whether a server *claims* a tool is read-only.

    The claim is the server's, and it is used only to decide whether a call needs the owner's
    confirmation — never to decide that a call is safe to batch or to run without a receipt.
    A missing or non-boolean hint is read as "may change things", because guessing the safe
    direction is how a read-only label ends up on a delete.
    """
    hint = annotations.get("readOnlyHint")
    return hint is True


def annotate(tool: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Split a raw tools/list entry into (annotations, read_only)."""
    annotations = tool.get("annotations") or {}
    return annotations, is_read_only(annotations)


def flatten(server: str, tools: Iterable[dict[str, Any]]) -> list[ToolListing]:
    listings: list[ToolListing] = []
    for tool in tools:
        annotations = tool.get("annotations") or {}
        listings.append(
            ToolListing(
                server=server,
                name=str(tool.get("name") or ""),
                description=str(tool.get("description") or ""),
                input_schema=tool.get("input_schema") or {"type": "object", "properties": {}},
                read_only=is_read_only(annotations),
            )
        )
    return listings
