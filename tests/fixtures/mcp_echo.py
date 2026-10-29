"""A deliberately awkward MCP server, used to test the client against a real subprocess.

It is awkward on purpose, because the failures that matter are the impolite ones:

* it prints a log line on stdout before the handshake (servers do this), which must not be
  mistaken for a JSON-RPC frame;
* ``mutate`` carries no ``readOnlyHint``, so a client that treats a missing annotation as safe
  is caught;
* ``boom`` answers with ``isError: true`` and its own text;
* ``slow`` never answers, so a client without a deadline hangs;
* ``exit_now`` closes the stream, so a client must report the exit instead of an empty answer.
"""

from __future__ import annotations

import json
import sys
import time

TOOLS = [
    {
        "name": "echo",
        "description": "Repeat the given text back.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "mutate",
        "description": "Pretend to change something. Carries no annotation on purpose.",
        "inputSchema": {"type": "object", "properties": {"value": {"type": "string"}}},
    },
    {
        "name": "boom",
        "description": "Always reports an error.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "slow",
        "description": "Never answers.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True},
    },
]


def answer(message_id, value):
    return {"jsonrpc": "2.0", "id": message_id, "result": value}


def fail(message_id, code, message):
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}


def handle(payload):
    method = payload.get("method")
    message_id = payload.get("id")
    if method == "initialize":
        return answer(
            message_id,
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fixture", "version": "1.2.3"},
                "instructions": "fixture server",
            },
        )
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return answer(message_id, {})
    if method == "tools/list":
        return answer(message_id, {"tools": TOOLS})
    if method == "tools/call":
        params = payload.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name == "echo":
            return answer(message_id, {"content": [{"type": "text", "text": str(arguments.get("text", ""))}]})
        if name == "mutate":
            return answer(message_id, {"content": [{"type": "text", "text": "changed"}]})
        if name == "boom":
            return answer(
                message_id,
                {"content": [{"type": "text", "text": "that did not work"}], "isError": True},
            )
        if name == "slow":
            time.sleep(30)
            return answer(message_id, {"content": [{"type": "text", "text": "late"}]})
        if name == "exit_now":
            sys.stdout.flush()
            raise SystemExit(3)
        return fail(message_id, -32602, f"unknown tool {name!r}")
    return fail(message_id, -32601, f"unknown method {method!r}")


def main() -> int:
    # A real server logs to stdout by accident. The client must survive this.
    print("fixture: starting", flush=True)
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            print(json.dumps(fail(None, -32700, "not JSON")), flush=True)
            continue
        response = handle(payload)
        if response is not None:
            print(json.dumps(response), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
