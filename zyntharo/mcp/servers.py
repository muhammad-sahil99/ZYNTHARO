"""Which MCP servers the owner configured, and how they are started.

A server entry is a command line plus optional environment. That is powerful and therefore
sensitive: a ``GITHUB_TOKEN`` in an environment block is a credential, so the file is written
``0600``, values are never returned by the listing commands or logged, and a stored entry is
never auto-run at startup — it runs only when a task asks for one of its tools. A config file
that silently spawns eight processes at boot is the opposite of the lightweight promise.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .client import DEFAULT_TIMEOUT, StdioMCPClient

_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
FILE_NAME = "mcp_servers.json"


class ServerStoreError(RuntimeError):
    """The entry is invalid or the store cannot be read. The message is for the user."""


@dataclass(slots=True)
class ServerSpec:
    name: str
    command: tuple[str, ...]
    env: dict[str, str] = field(default_factory=dict)
    note: str = ""
    timeout: float = DEFAULT_TIMEOUT
    added: float = field(default_factory=time.time)

    def to_json(self, redact: bool = True) -> dict[str, Any]:
        data = asdict(self)
        data["command"] = list(self.command)
        if redact and self.env:
            # Names are useful in a listing ("it reads GITHUB_TOKEN"); values are not.
            data["env"] = {key: "***" for key in self.env}
        return data

    def client(self) -> StdioMCPClient:
        return StdioMCPClient(self.name, self.command, extra_env=self.env, timeout=self.timeout)


def validate(spec: ServerSpec) -> ServerSpec:
    if not _NAME.match(spec.name):
        raise ServerStoreError(
            f"invalid server name {spec.name!r}: use lowercase letters, digits, hyphen or underscore "
            "(max 32 characters), so it is safe to use as a tool prefix"
        )
    if not spec.command or not all(isinstance(part, str) and part.strip() for part in spec.command):
        raise ServerStoreError(f"server {spec.name!r} needs a non-empty command")
    if spec.timeout <= 0 or spec.timeout > 600:
        raise ServerStoreError(f"server {spec.name!r} timeout must be between 0 and 600 seconds")
    for key, value in spec.env.items():
        if not isinstance(key, str) or not key or not isinstance(value, str):
            raise ServerStoreError(f"server {spec.name!r} has a malformed environment entry")
    return spec


class ServerStore:
    """Reads and writes ``<home>/mcp_servers.json``; one owner, one file, no daemon."""

    def __init__(self, home: str | Path):
        self.home = Path(home) if home else Path(os.environ.get("ZYNTHARO_HOME", ".")).expanduser()
        self.path = self.home / FILE_NAME
        self.problem = ""

    # -- io ----------------------------------------------------------------------------
    def load(self) -> dict[str, ServerSpec]:
        if not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # A broken file must not be silently replaced: it may be the only copy of the owner's
            # commands. Report it and behave as if empty, so the app still starts.
            self.problem = f"{self.path} could not be read: {exc}"
            return {}
        servers: dict[str, ServerSpec] = {}
        for entry in data.get("servers", []) if isinstance(data, dict) else []:
            try:
                spec = ServerSpec(
                    name=str(entry["name"]),
                    command=tuple(str(p) for p in entry["command"]),
                    env={str(k): str(v) for k, v in (entry.get("env") or {}).items()},
                    note=str(entry.get("note") or ""),
                    timeout=float(entry.get("timeout") or DEFAULT_TIMEOUT),
                    added=float(entry.get("added") or time.time()),
                )
                validate(spec)
            except (KeyError, TypeError, ValueError, ServerStoreError):
                continue  # one bad entry does not hide the others
            servers[spec.name] = spec
        self.problem = ""
        return servers

    def save(self, servers: dict[str, ServerSpec]) -> Path:
        self.home.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "servers": [spec.to_json(redact=False) for spec in servers.values()]}
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        try:
            os.chmod(temp, 0o600)
        except OSError:  # Windows and some filesystems: best effort, documented in LIMITATIONS
            pass
        temp.replace(self.path)
        return self.path

    # -- mutations ---------------------------------------------------------------------
    def add(self, spec: ServerSpec, replace: bool = False) -> ServerSpec:
        validate(spec)
        servers = self.load()
        if spec.name in servers and not replace:
            raise ServerStoreError(
                f"an MCP server called {spec.name!r} already exists; remove it first or use --replace"
            )
        servers[spec.name] = spec
        self.save(servers)
        return spec

    def remove(self, name: str) -> ServerSpec:
        servers = self.load()
        spec = servers.pop(name, None)
        if spec is None:
            raise ServerStoreError(f"no MCP server called {name!r}; configured: {sorted(servers) or 'none'}")
        self.save(servers)
        return spec

    def get(self, name: str) -> ServerSpec:
        servers = self.load()
        spec = servers.get(name)
        if spec is None:
            raise ServerStoreError(f"no MCP server called {name!r}; configured: {sorted(servers) or 'none'}")
        return spec

    def list(self) -> list[ServerSpec]:
        return [servers for _, servers in sorted(self.load().items())]
