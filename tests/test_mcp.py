"""MCP (spec 15.1): the wire format, the client against a real subprocess, the server, the actions.

The client tests run an actual child process rather than faking the transport, because the whole
point of this subsystem is that somebody else's program behaves badly: it logs on stdout, ignores
a request, exits mid-session. A fake would agree with us and prove nothing.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest

from tests.helpers import Stack
from zyntharo.mcp import protocol
from zyntharo.mcp.client import MCPError, MCPToolError, StdioMCPClient, flatten, is_read_only
from zyntharo.mcp.servers import ServerSpec, ServerStore, ServerStoreError
from zyntharo.mcp.server import READ_ONLY_CAPABILITIES, MCPServer

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "mcp_echo.py")


def fixture_command() -> tuple[str, ...]:
    return (sys.executable, FIXTURE)


class ProtocolTests(unittest.TestCase):
    def test_rejects_a_line_that_is_not_json(self):
        with self.assertRaises(protocol.ProtocolError) as caught:
            protocol.decode("root: not json")
        self.assertEqual(caught.exception.code, protocol.PARSE_ERROR)

    def test_refuses_content_length_framing_with_the_reason(self):
        with self.assertRaises(protocol.ProtocolError) as caught:
            protocol.decode("Content-Length: 42\r\n\r\n{}")
        self.assertIn("newline-delimited", str(caught.exception))

    def test_requires_jsonrpc_2(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.decode('{"id": 1, "method": "ping"}')

    def test_distinguishes_request_response_and_notification(self):
        request = protocol.decode(protocol.encode(protocol.request(7, "tools/list")))
        self.assertTrue(request.is_request)
        self.assertFalse(request.is_notification)
        response = protocol.decode(protocol.encode(protocol.result(7, {"tools": []})))
        self.assertTrue(response.is_response)
        note = protocol.decode(protocol.encode(protocol.notification(protocol.INITIALIZED, {})))
        self.assertTrue(note.is_notification)
        self.assertFalse(note.is_request)

    def test_params_must_be_an_object(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.decode('{"jsonrpc": "2.0", "id": 1, "method": "x", "params": [1, 2]}')

    def test_read_text_flattens_content_and_refuses_nonsense(self):
        self.assertEqual(
            protocol.read_text(protocol.text_result("first")),
            "first",
        )
        with self.assertRaises(protocol.ProtocolError):
            protocol.read_text("a string, not a result")

    def test_missing_read_only_hint_is_not_read_only(self):
        self.assertFalse(is_read_only({}))
        self.assertFalse(is_read_only({"readOnlyHint": "yes"}))
        self.assertTrue(is_read_only({"readOnlyHint": True}))

    def test_flatten_keeps_the_schema_and_the_hint(self):
        listings = flatten(
            "fixture",
            [{"name": "read", "description": "d", "input_schema": {"type": "object"}, "annotations": {"readOnlyHint": True}}],
        )
        self.assertEqual(listings[0].server, "fixture")
        self.assertTrue(listings[0].read_only)
        self.assertEqual(listings[0].input_schema, {"type": "object"})


class ClientTests(unittest.TestCase):
    def client(self, timeout: float = 10.0) -> StdioMCPClient:
        return StdioMCPClient("fixture", fixture_command(), timeout=timeout)

    def test_handshake_reports_the_server_it_actually_reached(self):
        with self.client() as client:
            self.assertEqual(client.info.name, "fixture")
            self.assertEqual(client.info.version, "1.2.3")
            self.assertEqual(client.negotiated, protocol.PROTOCOL_VERSION)
            self.assertEqual(client.instructions, "fixture server")

    def test_a_stdout_log_line_is_dropped_not_parsed(self):
        with self.client() as client:
            self.assertEqual(client.call_tool("echo", {"text": "hi"}), "hi")
            # The fixture prints "fixture: starting" before the handshake.
            self.assertGreaterEqual(client.dropped, 1)

    def test_lists_tools_with_their_annotations(self):
        with self.client() as client:
            tools = {tool["name"]: tool for tool in client.list_tools()}
        self.assertEqual(set(tools), {"echo", "mutate", "boom", "slow"})
        self.assertTrue(is_read_only(tools["echo"]["annotations"]))
        self.assertFalse(is_read_only(tools["mutate"]["annotations"]))

    def test_tool_error_text_comes_back_verbatim(self):
        with self.client() as client:
            with self.assertRaises(MCPToolError) as caught:
                client.call_tool("boom")
        self.assertEqual(str(caught.exception), "that did not work")
        self.assertEqual(caught.exception.tool, "boom")

    def test_a_server_that_never_answers_is_stopped_and_named(self):
        client = self.client(timeout=1.5)
        client.start()
        try:
            with self.assertRaises(MCPError) as caught:
                client.call_tool("slow")
            message = str(caught.exception)
            self.assertIn("did not answer tools/call", message)
            self.assertIn("fixture", message)
        finally:
            client.close()
        self.assertFalse(client.describe()["running"])

    def test_a_server_that_exits_mid_session_is_reported_as_exited(self):
        with self.client() as client:
            with self.assertRaises(MCPError) as caught:
                client.call_tool("exit_now")
            self.assertIn("exited before answering", str(caught.exception))

    def test_a_missing_executable_says_what_to_install(self):
        client = StdioMCPClient("ghost", ("definitely-not-a-real-binary-xyz",), timeout=5)
        with self.assertRaises(MCPError) as caught:
            client.start()
        self.assertIn("could not be started", str(caught.exception))
        self.assertIn("mcp remove ghost", str(caught.exception))

    def test_close_is_idempotent_and_leaves_no_process(self):
        client = self.client()
        client.start()
        client.close()
        client.close()
        self.assertFalse(client.describe()["running"])


class ServerTests(unittest.TestCase):
    """The server over our own registry: the gate must be the same gate."""

    def setUp(self):
        self.stack = Stack()
        self.server = MCPServer(
            registry=self.stack.actions,
            # One read capability and one act capability, so the listing can be checked in both
            # directions: granted reads appear as read-only, granted writes appear as not.
            capabilities={"mcp.read", "filesystem.read", "filesystem.move", "vision.capture"},
            agent_id="remote-client",
            config=self.stack.config,
            bus=self.stack.bus,
        )

    def tearDown(self):
        self.stack.close()

    def rpc(self, method: str, params: dict | None = None, message_id: int = 1):
        return self.server.handle({"jsonrpc": "2.0", "id": message_id, "method": method, "params": params or {}})

    def test_initialize_answers_with_our_identity(self):
        payload = self.rpc("initialize")
        result = payload["result"]
        self.assertEqual(result["protocolVersion"], protocol.PROTOCOL_VERSION)
        self.assertEqual(result["serverInfo"]["name"], "zyntharo")
        self.assertTrue(result["serverInfo"]["version"])

    def test_tool_list_is_limited_to_granted_capabilities(self):
        tools = {tool["name"]: tool for tool in self.rpc("tools/list")["result"]["tools"]}
        self.assertIn("mcp_servers", tools)          # mcp.read granted
        self.assertIn("fs_read", tools)              # filesystem.read granted
        self.assertNotIn("mcp_call", tools)          # mcp.call not granted
        self.assertNotIn("fs_delete", tools)         # filesystem.delete not granted

    def test_tool_list_marks_read_only_by_capability_not_by_name(self):
        tools = {tool["name"]: tool for tool in self.rpc("tools/list")["result"]["tools"]}
        self.assertTrue(tools["mcp_servers"]["annotations"]["readOnlyHint"])
        self.assertFalse(tools["fs_move"]["annotations"]["readOnlyHint"])
        self.assertIn("inputSchema", tools["mcp_servers"])

    def test_a_granted_read_tool_runs_and_returns_text(self):
        payload = self.rpc("tools/call", {"name": "mcp_servers", "arguments": {}})
        self.assertFalse(payload["result"]["isError"])
        text = payload["result"]["content"][0]["text"]
        self.assertIn("count", text)

    def test_a_tool_outside_the_grant_is_refused_not_hidden_only(self):
        payload = self.rpc("tools/call", {"name": "fs_delete", "arguments": {"path": "/tmp/x"}})
        self.assertTrue(payload["result"]["isError"])
        self.assertIn("capability denied: filesystem.delete", payload["result"]["content"][0]["text"])

    def test_a_destructive_tool_needs_confirmation_even_when_granted(self):
        server = MCPServer(
            registry=self.stack.actions,
            capabilities={"mcp.read", "mcp.call"},
            agent_id="remote-client",
            config=self.stack.config,
            bus=self.stack.bus,
        )
        payload = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 9,
                "method": "tools/call",
                "params": {"name": "mcp_call", "arguments": {"server": "x", "tool": "y"}},
            }
        )
        self.assertTrue(payload["result"]["isError"])
        self.assertIn("confirmation required", payload["result"]["content"][0]["text"])

    def test_unknown_tool_and_unknown_method_are_jsonrpc_errors(self):
        tool = self.rpc("tools/call", {"name": "nope", "arguments": {}})
        self.assertEqual(tool["error"]["code"], protocol.INVALID_PARAMS)
        self.assertIn("unknown tool", tool["error"]["message"])
        method = self.rpc("tools/delete")
        self.assertEqual(method["error"]["code"], protocol.METHOD_NOT_FOUND)

    def test_a_notification_gets_no_answer(self):
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}))

    def test_serve_stdio_speaks_only_json_on_stdout(self):
        stdin = io.StringIO(
            "\n".join(
                [
                    json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
                    json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
                    "a stray log line",
                ]
            )
            + "\n"
        )
        stdout = io.StringIO()
        self.server.serve_stdio(stdin=stdin, stdout=stdout)
        lines = [json.loads(line) for line in stdout.getvalue().strip().splitlines()]
        self.assertEqual(lines[0]["id"], 1)
        self.assertEqual(lines[1]["id"], 2)
        self.assertEqual(lines[2]["error"]["code"], protocol.PARSE_ERROR)


class ActionTests(unittest.TestCase):
    """``mcp_*`` actions: the store, the read/act split, and the confirm gate."""

    def setUp(self):
        from zyntharo.actions import mcp as mcp_actions

        self.mcp_actions = mcp_actions
        self.stack = Stack()
        self.store = ServerStore(self.stack.home)
        mcp_actions.set_services(
            mcp_actions.McpServices(bus=self.stack.bus, config=self.stack.config, servers=self.store)
        )
        self.stack.tool("mcp_add", name="fixture", command=list(fixture_command()), note="tests")

    def tearDown(self):
        self.mcp_actions.set_services(None)
        self.stack.close()

    def assertFailed(self, result, needle: str):
        self.assertFalse(result.ok, result.error)
        self.assertIn(needle, result.error)

    def test_add_is_stored_and_environment_values_are_not_echoed(self):
        result = self.stack.tool(
            "mcp_add", name="withtoken", command=[sys.executable, FIXTURE], env={"TOKEN": "s3cret"}
        )
        self.assertTrue(result.ok, result.error)
        listing = self.stack.tool("mcp_servers")
        self.assertTrue(listing.ok)
        row = {entry["name"]: entry for entry in listing.output["servers"]}["withtoken"]
        self.assertEqual(row["env_names"], ["TOKEN"])
        self.assertNotIn("s3cret", json.dumps(row))

    def test_a_duplicate_name_is_refused(self):
        result = self.stack.tool("mcp_add", name="fixture", command=list(fixture_command()))
        self.assertFailed(result, "already exists")

    def test_an_invalid_name_is_refused(self):
        result = self.stack.tool("mcp_add", name="Bad Name!", command=["cmd"])
        self.assertFailed(result, "invalid server name")

    def test_remove_reports_what_it_deleted(self):
        result = self.stack.tool("mcp_remove", name="fixture")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.output["removed"], "fixture")
        self.assertEqual(self.store.list(), [])

    def test_tools_lists_the_server_with_its_read_only_flags(self):
        result = self.stack.tool("mcp_tools", server="fixture")
        self.assertTrue(result.ok, result.error)
        tools = {tool["name"]: tool for tool in result.output["tools"]}
        self.assertTrue(tools["echo"]["read_only"])
        self.assertFalse(tools["mutate"]["read_only"])

    def test_a_read_only_tool_is_callable_without_confirmation(self):
        result = self.stack.tool("mcp_read", server="fixture", tool="echo", arguments={"text": "hello"})
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.output["text"], "hello")

    def test_an_unannotated_tool_is_refused_by_the_read_path(self):
        result = self.stack.tool("mcp_read", server="fixture", tool="mutate", arguments={"value": "x"})
        self.assertFailed(result, "does not declare itself read-only")

    def test_a_tool_error_surfaces_the_server_text(self):
        result = self.stack.tool("mcp_read", server="fixture", tool="boom")
        self.assertFailed(result, "that did not work")

    def test_the_act_path_needs_confirmation(self):
        refused = self.stack.tool(
            "mcp_call", confirmed=(), server="fixture", tool="mutate", arguments={"value": "x"}
        )
        self.assertFailed(refused, "confirmation required")
        allowed = self.stack.tool(
            "mcp_call", confirmed=("mcp_call",), server="fixture", tool="mutate", arguments={"value": "x"}
        )
        self.assertTrue(allowed.ok, allowed.error)
        self.assertEqual(allowed.output["text"], "changed")

    def test_an_unknown_server_explains_what_is_configured(self):
        result = self.stack.tool("mcp_tools", server="ghost")
        self.assertFailed(result, "no MCP server called 'ghost'")

    def test_a_capability_without_mcp_read_cannot_list_servers(self):
        result = self.stack.tool("mcp_servers", capabilities=("filesystem.read",))
        self.assertFailed(result, "capability denied: mcp.read")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="zyntharo-mcp-")

    def test_the_file_is_private_and_round_trips(self):
        store = ServerStore(self.home)
        store.add(ServerSpec(name="one", command=("npx", "-y", "server"), env={"KEY": "value"}))
        self.assertTrue(store.path.is_file())
        if os.name != "nt":
            self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)
        loaded = store.get("one")
        self.assertEqual(loaded.command, ("npx", "-y", "server"))
        self.assertEqual(loaded.env, {"KEY": "value"})

    def test_a_corrupt_file_is_reported_not_overwritten(self):
        store = ServerStore(self.home)
        store.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(store.load(), {})
        self.assertIn("could not be read", store.problem)
        self.assertEqual(store.path.read_text(encoding="utf-8"), "{not json")

    def test_a_bad_entry_does_not_hide_the_good_ones(self):
        store = ServerStore(self.home)
        store.path.write_text(
            json.dumps(
                {
                    "servers": [
                        {"name": "good", "command": ["x"]},
                        {"name": "BAD!", "command": ["y"]},
                        {"command": []},
                    ]
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual([spec.name for spec in store.list()], ["good"])

    def test_removing_something_absent_lists_what_exists(self):
        store = ServerStore(self.home)
        with self.assertRaises(ServerStoreError) as caught:
            store.remove("nope")
        self.assertIn("configured: none", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
