"""HUD bridge tests: the hand-written WebSocket, event fanout, command routing and static host.

These talk to a real listening socket with real RFC6455 frames — no mocking of the transport,
because the transport is exactly what could be wrong.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import time
import unittest
import urllib.request

from tests.helpers import Stack
from orbynth.events import EventBus
from orbynth.hud.bridge import HudBridge, decode_frames, encode_frame, handshake_response


def client_frame(payload: bytes, opcode: int = 0x1) -> bytes:
    """Client → server frame (masked, as the spec requires)."""
    mask = b"\x11\x22\x33\x44"
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    length = len(payload)
    header = bytearray([0x80 | opcode])
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header += struct.pack("!H", length)
    else:
        header.append(0x80 | 127)
        header += struct.pack("!Q", length)
    return bytes(header) + mask + masked


class FrameCodecTests(unittest.TestCase):
    def test_handshake_accept_key_matches_rfc6455_example(self):
        response = handshake_response("dGhlIHNhbXBsZSBub25jZQ==")
        self.assertIn("Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=", response)

    def test_frames_round_trip_and_handle_partial_input(self):
        frame = client_frame(b"hello")
        frames, remainder = decode_frames(frame)
        self.assertEqual(frames, [(0x1, b"hello")])
        self.assertEqual(remainder, b"")
        frames, remainder = decode_frames(frame[:3])
        self.assertEqual(frames, [])
        self.assertEqual(remainder, frame[:3])

    def test_large_frames_use_the_64_bit_length_form(self):
        payload = b"x" * 70000
        frames, _ = decode_frames(client_frame(payload))
        self.assertEqual(frames[0][1], payload)
        encoded = encode_frame(b"y" * 200)
        self.assertEqual(encoded[1], 126)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.bus = EventBus()
        self.commands: list[tuple[str, dict]] = []

        known = {"chat", "task.submit", "voice.mute", "confirm", "state.request"}

        def router(kind, message, client):
            if kind not in known:
                raise ValueError(f"unknown HUD command: {kind}")
            self.commands.append((kind, message))
            return {"handled": kind}

        self.bridge = HudBridge(
            bus=self.bus,
            host="127.0.0.1",
            port=0,
            static_dir=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hud"),
            snapshot_provider=lambda: {"product": "ORBYNTH", "agents": [{"name": "Sovereign", "rank": 0}]},
            command_router=router,
        ).start()
        self.socket = self._connect()

    def tearDown(self):
        try:
            self.socket.close()
        except OSError:
            pass
        self.bridge.stop()

    def _connect(self, path="/ws"):
        sock = socket.create_connection((self.bridge.host, self.bridge.port), timeout=5)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\nHost: {self.bridge.host}:{self.bridge.port}\r\n"
            f"Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(request.encode("ascii"))
        buffer = b""
        while b"\r\n\r\n" not in buffer:
            buffer += sock.recv(4096)
        header, _, rest = buffer.partition(b"\r\n\r\n")
        self.assertIn(b"101 Switching Protocols", header)
        sock.setblocking(True)
        sock.settimeout(5)
        return sock

    def read_frame(self) -> dict:
        """Read one server frame (unmasked, as servers send)."""
        buffer = b""
        while True:
            buffer += self.socket.recv(65536)
            frames, remainder = decode_frames(buffer)
            buffer = remainder
            for _opcode, payload in frames:
                if payload:
                    return json.loads(payload.decode("utf-8"))
            if not frames:
                continue

    def test_hello_carries_the_system_snapshot(self):
        hello = self.read_frame()
        self.assertEqual(hello["type"], "hello")
        self.assertEqual(hello["snapshot"]["product"], "ORBYNTH")
        self.assertTrue(hello["client_id"])

    def test_bus_events_reach_the_client(self):
        self.read_frame()  # hello
        self.bus.emit("agent.spawned", name="Operative", rank=5)
        frame = self.read_frame()
        self.assertEqual(frame["type"], "event")
        self.assertEqual(frame["topic"], "agent.spawned")
        self.assertEqual(frame["event"]["payload"]["name"], "Operative")

    def test_notices_and_scenes_get_their_own_frames(self):
        self.read_frame()
        self.bus.emit("hud.notice", text="blocked on Excel", severity="warning", speak=False)
        notice = self.read_frame()
        self.assertEqual(notice["type"], "notice")
        self.assertEqual(notice["text"], "blocked on Excel")
        self.bus.emit("hud.scene", kind="chart", title="Invoices", payload={"series": []})
        scene = self.read_frame()
        self.assertEqual(scene["type"], "scene")
        self.assertEqual(scene["title"], "Invoices")

    def test_client_commands_are_routed_and_acknowledged(self):
        self.read_frame()
        self.socket.sendall(client_frame(json.dumps({"type": "chat", "text": "status?"}).encode("utf-8")))
        ack = self.read_frame()
        self.assertEqual(ack["type"], "ack")
        self.assertEqual(ack["command"], "chat")
        self.assertEqual(self.commands[0][0], "chat")

    def test_ping_gets_a_pong_and_unknown_commands_an_error(self):
        self.read_frame()
        self.socket.sendall(client_frame(json.dumps({"type": "ping"}).encode("utf-8")))
        self.assertEqual(self.read_frame()["type"], "pong")
        self.socket.sendall(client_frame(json.dumps({"type": "not.a.command"}).encode("utf-8")))
        frame = self.read_frame()
        self.assertEqual(frame["type"], "error")
        self.assertIn("unknown HUD command", frame["error"])

    def test_http_endpoints_and_static_files(self):
        base = f"http://{self.bridge.host}:{self.bridge.port}"
        with urllib.request.urlopen(f"{base}/api/health", timeout=5) as response:
            health = json.loads(response.read())
        self.assertTrue(health["ok"])
        self.assertIn("clients", health)

        with urllib.request.urlopen(f"{base}/api/state", timeout=5) as response:
            state = json.loads(response.read())
        self.assertEqual(state["product"], "ORBYNTH")

        with urllib.request.urlopen(f"{base}/", timeout=5) as response:
            html = response.read().decode("utf-8")
            content_type = response.headers["Content-Type"]
        self.assertIn("ORBYNTH", html)
        self.assertIn("text/html", content_type)

        with urllib.request.urlopen(f"{base}/app.js", timeout=5) as response:
            self.assertIn("application/javascript", response.headers["Content-Type"])

    def test_path_traversal_is_refused(self):
        base = f"http://{self.bridge.host}:{self.bridge.port}"
        try:
            urllib.request.urlopen(f"{base}/../orbynth/config.py", timeout=5).read()
            self.fail("traversal should have been refused")
        except urllib.error.HTTPError as error:
            self.assertIn(error.code, (403, 404))

    def test_slow_client_does_not_block_the_core(self):
        self.read_frame()
        for index in range(600):
            self.bus.emit("action.invoked", action=f"fs_read_{index}")
        self.assertLessEqual(len(self.bridge.clients), 1)
        bus_still_alive = self.bus.emit("agent.status", status="running")
        self.assertTrue(bus_still_alive.id)


class HudServiceTests(unittest.TestCase):
    def test_snapshot_exposes_the_whole_system(self):
        from orbynth.hud.service import HudService

        stack = Stack()
        try:
            stack.orchestrator.bootstrap_roster()
            service = HudService(
                config=stack.config,
                bus=stack.bus,
                memory=stack.memory,
                registry=stack.actions,
                runtime=stack.runtime,
                quotas=stack.quotas,
                orchestrator=stack.orchestrator,
                plugins=stack.plugins,
            )
            snapshot = service.snapshot()
            self.assertEqual(snapshot["product"], "ORBYNTH")
            self.assertGreater(snapshot["actions"]["count"], 80)
            self.assertTrue(snapshot["agents"])
            self.assertTrue(snapshot["models"])
            self.assertTrue(snapshot["drivers"])
            self.assertIn("quotas", snapshot)
        finally:
            stack.close()

    def test_hud_commands_require_a_live_fleet(self):
        from orbynth.hud.service import HudService

        stack = Stack()
        try:
            service = HudService(
                config=stack.config,
                bus=stack.bus,
                memory=stack.memory,
                registry=stack.actions,
                runtime=stack.runtime,
                quotas=stack.quotas,
                orchestrator=stack.orchestrator,
                plugins=stack.plugins,
            )
            confirmation = service.route("confirm", {"action": "fs_delete", "approved": True}, None)
            self.assertTrue(confirmation["approved"])
            self.assertEqual(stack.actions.approvals, {"fs_delete"})
            self.assertTrue(service.route("plugin.list", {}, None) == [])
            with self.assertRaises(ValueError):
                service.route("nonsense", {}, None)
        finally:
            stack.close()


if __name__ == "__main__":
    unittest.main()
