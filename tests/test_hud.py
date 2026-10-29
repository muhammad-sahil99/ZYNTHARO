"""HUD bridge tests: the hand-written WebSocket, event fanout, command routing and static host.

These talk to a real listening socket with real RFC6455 frames — no mocking of the transport,
because the transport is exactly what could be wrong.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import time
import unittest
import urllib.request

from tests.helpers import Stack
from zyntharo import __product__
from zyntharo.events import EventBus
from zyntharo.hud.bridge import HudBridge, decode_frames, encode_frame, handshake_response


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


class AnimationPowerTests(unittest.TestCase):
    """The plate must not cost a core when nobody is looking at it.

    This is a source-level check on purpose: the browser loop cannot run in the headless suite,
    but removing the guard would silently reintroduce a full-rate render behind a minimised
    window, which is exactly the sort of regression a user notices as "it heats my laptop".
    """

    def setUp(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.source = open(os.path.join(root, "hud", "app.js"), encoding="utf-8").read()

    def test_hidden_window_skips_drawing(self):
        self.assertIn("document.hidden", self.source)
        guard = self.source.index("if (document.hidden)")
        window = self.source[guard : guard + 400]
        self.assertIn("requestAnimationFrame", window, "the loop must stay alive to resume")
        self.assertIn("return", window)

    def test_idle_plate_is_paced_and_active_work_is_not(self):
        self.assertIn("IDLE_FRAME_MS", self.source)
        self.assertIn('lastSituation !== "idle"', self.source)
        self.assertIn("now - lastDraw < IDLE_FRAME_MS", self.source)


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
            snapshot_provider=lambda: {"product": "ZYNTHARO Beta", "agents": [{"name": "Sovereign", "rank": 0}]},
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
        self.assertEqual(hello["snapshot"]["product"], "ZYNTHARO Beta")
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
        # The brand comes from the package, not a literal in the test: a rename that misses the
        # wordmark must fail here rather than be papered over by an updated string.
        self.assertEqual(state["product"], __product__)
        self.assertIn("Beta", state["product"])

        with urllib.request.urlopen(f"{base}/", timeout=5) as response:
            html = response.read().decode("utf-8")
            content_type = response.headers["Content-Type"]
        self.assertIn("ZYNTHARO", html)
        self.assertIn("text/html", content_type)

        with urllib.request.urlopen(f"{base}/app.js", timeout=5) as response:
            self.assertIn("application/javascript", response.headers["Content-Type"])

    def test_path_traversal_is_refused(self):
        base = f"http://{self.bridge.host}:{self.bridge.port}"
        try:
            urllib.request.urlopen(f"{base}/../zyntharo/config.py", timeout=5).read()
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


class HudCoverageTests(unittest.TestCase):
    """The interface must have somewhere to put every event the core can emit.

    hud/app.js promises this check; before it existed, the whole operational layer shipped
    while the interface ignored its topics, so the app looked calm while processes were being
    terminated and model catalogues applied.
    """

    def test_every_emitted_topic_reaches_the_interface(self):
        from tools.hud_coverage import COVERAGE, report

        data = report()
        self.assertEqual(data["unhandled"], [], f"no HUD effect for: {data['unhandled']}")
        self.assertEqual(
            data["literal_topics"], [],
            "topics must be declared in zyntharo/events.py, not written as bare strings: "
            f"{data['literal_topics']}",
        )
        self.assertTrue(data["topics"], "no topics found: the parser is broken, not the code")
        self.assertTrue(COVERAGE.exists(), "hud/coverage.json is missing; run tools/hud_coverage.py")
        committed = json.loads(COVERAGE.read_text(encoding="utf-8"))
        self.assertEqual(committed, data, "hud/coverage.json is stale; run tools/hud_coverage.py")

    def test_demo_data_covers_the_panels_the_interface_renders(self):
        # The hosted preview has no desktop to automate, so it runs on demo data. If the demo
        # snapshot drifts from what the panels read, the preview quietly shows empty panels.
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hud", "mock.js")
        source = open(path, encoding="utf-8").read()
        for key in (
            "jobs", "verdicts", "preflight", "security", "updates", "machine",
            "agents", "quotas", "plugins", "drivers", "memory", "models",
        ):
            self.assertIn(f"{key}:", source, f"demo snapshot has no {key}")


class HudVisualTests(unittest.TestCase):
    """The visual layer cannot be screenshotted here, so it is executed headlessly instead.

    ``tools/hud_visual_check.js`` runs the real particle core and chrome modules against a
    stubbed WebGL context and a recording canvas 2D context. That catches the failures a blank
    screen hides: a uniform the JavaScript looks up but the shader never declares, an attribute
    fetched but never bound, a band of the instrument that comes out empty, a projection that is
    not actually 3D, and an element id app.js reaches for that index.html does not define.
    """

    @unittest.skipUnless(shutil.which("node"), "node is required to execute the HUD renderers")
    def test_the_renderers_build_valid_geometry_and_draw(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        result = subprocess.run(
            [shutil.which("node"), os.path.join(root, "tools", "hud_visual_check.js")],
            capture_output=True,
            text=True,
            timeout=180,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("HUD visual layer verified", result.stdout)

    def test_the_core_projects_in_three_dimensions_and_the_cage_is_drawn(self):
        """Static guard: the core must not fall back to scaling x/y by a fake perspective."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "hud", "particle-core.js"), encoding="utf-8") as handle:
            core = handle.read()
        self.assertIn("uCamZ - v.z", core, "the core has no camera depth term")
        self.assertIn("gl_Position = vec4(v.x * f / aspect", core)
        with open(os.path.join(root, "hud", "index.html"), encoding="utf-8") as handle:
            html = handle.read()
        self.assertIn('id="chrome"', html, "the instrument cage has no canvas")
        self.assertIn("chrome.js", html)

    def test_the_console_carries_the_grid_backdrop_and_the_widget_instruments(self):
        """Static guard: the dense instrument layers must stay part of the document.

        The housing is what makes the console read as an instrument, so a refactor that drops the
        backdrop, the widget canvases or their wiring should fail here rather than quietly
        returning the interface to a dashboard with a globe in the middle.
        """
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        html = open(os.path.join(root, "hud", "index.html"), encoding="utf-8").read()
        app = open(os.path.join(root, "hud", "app.js"), encoding="utf-8").read()
        self.assertIn('id="grid"', html, "the system grid has no canvas")
        self.assertIn("grid.js", html)
        self.assertIn("widgets.js", html)
        self.assertGreaterEqual(html.count('data-widget="'), 12, "too few instrument widgets are mounted")
        self.assertGreaterEqual(html.count('data-gauge="'), 3, "the dial bank is gone")
        self.assertIn('data-stack="fleet"', html)
        self.assertIn('data-stack="console"', html)
        self.assertIn("OrbythGrid", app)
        self.assertIn("OrbythWidgets", app)
        # the effects director: situations change the whole plate, and events must reach it
        self.assertIn("effects.js", html)
        self.assertIn("OrbythEffects", app)
        self.assertIn("fx.route", app)
        self.assertIn("applyEffects", app)
        self.assertIn("bindPulses", app)

    def test_a_3d_explanation_window_stops_rendering_when_it_closes(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "hud", "app.js"), encoding="utf-8") as handle:
            app = handle.read()
        self.assertIn("scene3D", app)
        self.assertIn("state.sceneFrame = null", app)
        self.assertIn("closeScene", app)


class HudServiceTests(unittest.TestCase):
    def test_snapshot_exposes_the_whole_system(self):
        from zyntharo.hud.service import HudService

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
            self.assertEqual(snapshot["product"], __product__)
            self.assertGreater(snapshot["actions"]["count"], 80)
            self.assertTrue(snapshot["agents"])
            self.assertTrue(snapshot["models"])
            self.assertTrue(snapshot["drivers"])
            self.assertIn("quotas", snapshot)
        finally:
            stack.close()

    def test_the_interface_is_told_which_way_the_language_reads(self):
        """An RTL language is a layout, not a translation (contract 22.11).

        The language layer knew the direction; the HUD never asked, so an Urdu or Arabic user
        got left-anchored chrome with right-to-left text inside it.
        """
        from zyntharo.hud.service import HudService

        stack = Stack()
        try:
            stack.config.voice.language = "ur"
            stack.config.voice.secondary_languages = ["en"]
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
            machine = service.snapshot()["machine"]
            self.assertEqual(machine["language_dir"], "rtl")
            self.assertIn("urd", machine["languages"])
        finally:
            stack.close()

    def test_the_interface_applies_the_direction_and_styles_a_disputed_report(self):
        """Static check: the core can report a direction the chrome never uses."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        app = open(os.path.join(root, "hud", "app.js"), encoding="utf-8").read()
        css = open(os.path.join(root, "hud", "styles.css"), encoding="utf-8").read()
        self.assertIn("applyDirection", app)
        self.assertIn('document.documentElement.dir', app)
        self.assertIn("status-disputed", css)

    def test_hud_commands_require_a_live_fleet(self):
        from zyntharo.hud.service import HudService

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
