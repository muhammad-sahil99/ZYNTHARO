"""MCP (Model Context Protocol) — both directions, hand-written over JSON-RPC on stdio.

``protocol`` is the wire format, ``client`` drives an outside server (filesystem, GitHub, a
database), ``server`` offers ZYNTHARO's own capability-gated tools to an outside client, and
``servers`` stores which servers the owner has configured. Nothing here is imported by the core:
the CLI and the actions module reach for it lazily, so a machine with no MCP servers configured
pays nothing for it.
"""

from __future__ import annotations

from .client import MCPError, MCPToolError, ServerInfo, StdioMCPClient, ToolListing, is_read_only
from .protocol import PROTOCOL_VERSION, ProtocolError, RpcError

__all__ = [
    "MCPError",
    "MCPToolError",
    "PROTOCOL_VERSION",
    "ProtocolError",
    "RpcError",
    "ServerInfo",
    "StdioMCPClient",
    "ToolListing",
    "is_read_only",
]
