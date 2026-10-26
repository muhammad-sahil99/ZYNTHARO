"""HUD bridge: a dependency-free WebSocket + static server for the interface.

Why hand-write RFC6455 instead of adding a dependency: the installer ships a bundled runtime
on machines with no coding environment, so every third-party package is weight the user pays
for. The protocol surface needed here (text frames, ping/pong, close, per-client queues) is
about two hundred lines, and it means the HUD has exactly one local port serving both the
interface and its live event stream.

Outbound frames follow a small, stable vocabulary the front-end renders:

* ``hello``   — full system snapshot: version, models, quotas, agents, plugins, actions.
* ``event``   — every event bus message (agent.*, action.*, quota.*, voice.*, plugin.*).
* ``state``   — periodic digest (agent tree, quota table) so a late-joining HUD is correct.
* ``notice``  — something the user should read/hear, with optional speech.
* ``scene``   — a visual explanation to open in the scene viewer.
* ``transcript`` — live voice transcript lines.

Inbound frames carry user intent from the HUD: chat input, voice mute, task submission,
agent control, plugin management. Each is dispatched to a router callable, so the bridge
itself stays transport-only and testable without a browser.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlparse

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


# --------------------------------------------------------------------------------------
# WebSocket framing
# --------------------------------------------------------------------------------------


def handshake_response(key: str) -> str:
    digest = hashlib.sha1((key + GUID).encode("ascii")).digest()
    accept = base64.b64encode(digest).decode("ascii")
    return (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
    )


def encode_frame(payload: bytes, opcode: int = 0x1) -> bytes:
    """Server → client frame (unmasked, per spec)."""
    header = bytearray([0x80 | opcode])
    length = len(payload)
    if length < 126:
        header.append(length)
    elif length < 65536:
        header.append(126)
        header += struct.pack("!H", length)
    else:
        header.append(127)
        header += struct.pack("!Q", length)
    return bytes(header) + payload


def decode_frames(buffer: bytes) -> tuple[list[tuple[int, bytes]], bytes]:
    """Parse as many complete client frames as ``buffer`` holds. Returns (frames, remainder)."""
    frames: list[tuple[int, bytes]] = []
    offset = 0
    while True:
        if len(buffer) - offset < 2:
            break
        first, second = buffer[offset], buffer[offset + 1]
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        index = offset + 2
        if length == 126:
            if len(buffer) - index < 2:
                break
            length = struct.unpack("!H", buffer[index : index + 2])[0]
            index += 2
        elif length == 127:
            if len(buffer) - index < 8:
                break
            length = struct.unpack("!Q", buffer[index : index + 8])[0]
            index += 8
        mask = b""
        if masked:
            if len(buffer) - index < 4:
                break
            mask = buffer[index : index + 4]
            index += 4
        if len(buffer) - index < length:
            break
        payload = buffer[index : index + length]
        if masked and mask:
            payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        frames.append((opcode, payload))
        offset = index + length
    return frames, buffer[offset:]


@dataclass
class Client:
    """One attached HUD. Writes are queued and flushed by a dedicated thread."""

    connection: socket.socket
    address: tuple[str, int]
    id: str = field(default_factory=lambda: f"hud-{int(time.time() * 1000) % 100000}")
    queue: "list[dict[str, Any]] | None" = None
    alive: bool = True
    user_agent: str = ""
    sent: int = 0
    subscribed: str = "*"


class HudBridge:
    """Threaded WebSocket hub with an event-bus subscription and a command router."""

    def __init__(
        self,
        bus: Any = None,
        host: str = "127.0.0.1",
        port: int = 8760,
        static_dir: str | os.PathLike[str] | None = None,
        snapshot_provider: Callable[[], dict[str, Any]] | None = None,
        command_router: Callable[[str, dict[str, Any], Client], Any] | None = None,
        max_queue: int = 400,
    ):
        self.bus = bus
        self.host = host
        self.port = port
        self.static_dir = Path(static_dir) if static_dir else Path(__file__).resolve().parent.parent.parent / "hud"
        self.snapshot_provider = snapshot_provider
        self.command_router = command_router
        self.max_queue = max_queue
        self.clients: dict[str, Client] = {}
        self._lock = threading.RLock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.started_at = time.time()
        self.counters: dict[str, int] = {"events_out": 0, "commands_in": 0, "connections": 0}
        self._subscription = None

    # ----------------------------------------------------------------------------------
    # Lifecycle
    # ----------------------------------------------------------------------------------
    def start(self, background: bool = True) -> "HudBridge":
        handler = self._build_handler()
        self._server = ThreadingHTTPServer((self.host, self.port), handler)
        self._server.daemon_threads = True
        self.host, self.port = self._server.server_address[0], self._server.server_address[1]
        if self.bus is not None:
            self._subscription = self.bus.subscribe("*", self._on_event)
        if background:
            self._thread = threading.Thread(target=self._server.serve_forever, name="orbynth-hud", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        if self._subscription is not None:
            self._subscription.close()
        with self._lock:
            for client in list(self.clients.values()):
                self._drop(client)
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    @property
    def url(self) -> str:
        host = "127.0.0.1" if self.host in {"0.0.0.0", "::"} else self.host
        return f"http://{host}:{self.port}/"

    # ----------------------------------------------------------------------------------
    # Broadcasting
    # ----------------------------------------------------------------------------------
    def _on_event(self, event: Any) -> None:
        payload = event.to_json() if hasattr(event, "to_json") else {"topic": str(event)}
        broadcast = {"type": "event", "topic": payload.get("topic", ""), "event": payload, "ts": time.time()}
        # Translate high-level topics into HUD-specific frames the front-end reacts to.
        topic = payload.get("topic", "")
        if topic not in {"hud.scene", "hud.notice", "voice.transcript"}:
            # Those three have a dedicated frame below; sending both would double-render them.
            self.send(broadcast)
        if topic == "hud.scene":
            self.send({"type": "scene", **{k: v for k, v in payload.get("payload", {}).items()}, "title": payload.get("payload", {}).get("title", "Scene"), "ts": time.time()})
        elif topic == "hud.notice":
            self.send({"type": "notice", "text": payload.get("payload", {}).get("text", ""), "severity": payload.get("severity", "info"), "speak": payload.get("payload", {}).get("speak", True), "ts": time.time()})
        elif topic == "voice.transcript":
            self.send({"type": "transcript", "text": payload.get("payload", {}).get("text", ""), "role": payload.get("payload", {}).get("role", "user"), "ts": time.time()})

    def send(self, message: dict[str, Any], topic_filter: str = "*") -> int:
        """Queue a message for every matching client. Returns how many were queued."""
        data = json.dumps(message, default=str)
        queued = 0
        with self._lock:
            for client in list(self.clients.values()):
                if not client.alive or client.queue is None:
                    continue
                if len(client.queue) >= self.max_queue:
                    client.queue.pop(0)  # drop the oldest: a slow HUD must never block the core
                client.queue.append({"text": data, "ts": time.time()})
                queued += 1
        self.counters["events_out"] += queued
        return queued

    def notify(self, text: str, severity: str = "info", speak: bool = True) -> int:
        return self.send({"type": "notice", "text": text, "severity": severity, "speak": speak, "ts": time.time()})

    def push_state(self, state: dict[str, Any] | None = None) -> int:
        payload = state if state is not None else (self.snapshot_provider() if self.snapshot_provider else {})
        return self.send({"type": "state", "state": payload, "ts": time.time()})

    def snapshot(self) -> dict[str, Any]:
        if self.snapshot_provider is None:
            return {"clients": len(self.clients), "uptime_s": round(time.time() - self.started_at, 1)}
        try:
            data = self.snapshot_provider()
        except Exception as exc:  # noqa: BLE001 - a broken provider must not kill the HUD
            data = {"error": f"snapshot failed: {exc}"}
        data.setdefault("clients", len(self.clients))
        data.setdefault("uptime_s", round(time.time() - self.started_at, 1))
        return data

    def _drop(self, client: Client) -> None:
        client.alive = False
        self.clients.pop(client.id, None)
        try:
            client.connection.close()
        except OSError:
            pass

    # ----------------------------------------------------------------------------------
    # HTTP + WebSocket handler
    # ----------------------------------------------------------------------------------
    def _build_handler(self) -> type[BaseHTTPRequestHandler]:
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "ORBYNTH-HUD/0.1"

            def log_message(self, fmt: str, *args: Any) -> None:  # keep the console clean
                return

            def do_GET(self) -> None:  # noqa: N802 - stdlib naming
                path = urlparse(self.path).path
                if path in {"/ws", "/socket"} and self.headers.get("Upgrade", "").lower() == "websocket":
                    self._websocket()
                    return
                if path == "/api/state":
                    body = json.dumps(bridge.snapshot(), default=str).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if path == "/api/health":
                    body = json.dumps(
                        {
                            "ok": True,
                            "clients": len(bridge.clients),
                            "uptime_s": round(time.time() - bridge.started_at, 1),
                            **bridge.counters,
                        }
                    ).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self._static(path)

            def _static(self, path: str) -> None:
                relative = unquote(path).lstrip("/") or "index.html"
                candidate = (bridge.static_dir / relative).resolve()
                root = bridge.static_dir.resolve()
                if root not in candidate.parents and candidate != root:
                    self.send_error(403, "outside the HUD directory")
                    return
                if candidate.is_dir():
                    candidate = candidate / "index.html"
                if not candidate.exists():
                    self.send_error(404, f"not found: {relative}")
                    return
                content_types = {
                    ".html": "text/html; charset=utf-8",
                    ".js": "application/javascript; charset=utf-8",
                    ".mjs": "application/javascript; charset=utf-8",
                    ".css": "text/css; charset=utf-8",
                    ".json": "application/json",
                    ".svg": "image/svg+xml",
                    ".png": "image/png",
                    ".woff2": "font/woff2",
                    ".ico": "image/x-icon",
                }
                body = candidate.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", content_types.get(candidate.suffix.lower(), "application/octet-stream"))
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _websocket(self) -> None:
                client = Client(
                    connection=self.connection,
                    address=self.client_address,
                    user_agent=self.headers.get("User-Agent", "")[:120],
                )
                client.queue = []
                self.connection.sendall(handshake_response(self.headers.get("Sec-WebSocket-Key", "")).encode("ascii"))
                with bridge._lock:
                    bridge.clients[client.id] = client
                bridge.counters["connections"] += 1
                writer = threading.Thread(target=bridge._writer, args=(client,), name=f"{client.id}-writer", daemon=True)
                writer.start()
                bridge.send(
                    {
                        "type": "hello",
                        "client_id": client.id,
                        "version": _version(),
                        "snapshot": bridge.snapshot(),
                        "ts": time.time(),
                    },
                    topic_filter=client.id,
                )
                try:
                    self.connection.settimeout(None)
                    buffer = b""
                    while client.alive:
                        chunk = self.connection.recv(65536)
                        if not chunk:
                            break
                        buffer += chunk
                        frames, buffer = decode_frames(buffer)
                        for opcode, payload in frames:
                            if opcode == 0x8:  # close
                                client.alive = False
                                break
                            if opcode == 0x9:  # ping → pong
                                self.connection.sendall(encode_frame(payload, 0xA))
                                continue
                            if opcode == 0xA:
                                continue
                            bridge._handle_message(client, payload)
                except (OSError, ConnectionError):
                    pass
                finally:
                    import contextlib

                    with contextlib.suppress(OSError):
                        self.connection.sendall(encode_frame(b"", 0x8))
                    with bridge._lock:
                        bridge._drop(client)

        return Handler

    def _writer(self, client: Client) -> None:
        while client.alive:
            queue = client.queue
            if not queue:
                time.sleep(0.02)
                continue
            item = queue.pop(0)
            try:
                client.connection.sendall(encode_frame(item["text"].encode("utf-8"), 0x1))
                client.sent += 1
            except OSError:
                client.alive = False
                break

    def _handle_message(self, client: Client, payload: bytes) -> None:
        try:
            message = json.loads(payload.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            self.send({"type": "error", "error": "malformed frame"}, topic_filter=client.id)
            return
        kind = message.get("type", "")
        self.counters["commands_in"] += 1
        if kind == "ping":
            self._enqueue(client, {"type": "pong", "ts": time.time()})
            return
        if kind == "subscribe":
            client.subscribed = str(message.get("topic", "*"))
            self._enqueue(client, {"type": "subscribed", "topic": client.subscribed})
            return
        if kind == "state.request":
            self._enqueue(client, {"type": "state", "state": self.snapshot(), "ts": time.time()})
            return
        routed = False
        if self.command_router is not None:
            try:
                result = self.command_router(kind, message, client)
                routed = result is not None
                self._enqueue(client, {"type": "ack", "command": kind, "result": result, "ts": time.time()})
            except Exception as exc:  # noqa: BLE001 - report, never crash the socket
                self._enqueue(client, {"type": "error", "command": kind, "error": f"{type(exc).__name__}: {exc}"})
                routed = True
        if not routed:
            self._enqueue(client, {"type": "error", "error": f"unhandled message type: {kind}"})

    def _enqueue(self, client: Client, message: dict[str, Any]) -> None:
        if client.queue is None or not client.alive:
            return
        client.queue.append({"text": json.dumps(message, default=str), "ts": time.time()})


def _version() -> str:
    try:
        from .. import __version__

        return __version__
    except Exception:  # pragma: no cover
        return "0.1.0"


# --------------------------------------------------------------------------------------
# Webview launcher
# --------------------------------------------------------------------------------------


def open_webview(url: str, title: str = "ORBYNTH", width: int = 1480, height: int = 940) -> Any:
    """Render the HUD in the OS webview (lightweight: no bundled Chromium)."""
    try:
        import webview  # type: ignore

        window = webview.create_window(title, url, width=width, height=height, frameless=False, easy_drag=False)
        webview.start()
        return window
    except Exception:
        import webbrowser

        webbrowser.open(url)
        return None
