"""The channel contract shared by every platform adapter."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

READY = "ready"
CONFIGURED = "configured"
NEEDS_SETUP = "needs_setup"
UNAVAILABLE = "unavailable"

#: Platforms are either an official API, a local sidecar we own, or not offered at all.
KIND_OFFICIAL_API = "official_api"
KIND_LOCAL_SIDECAR = "local_sidecar"
KIND_UNSUPPORTED = "unsupported"


class ChannelError(RuntimeError):
    """A channel call failed. The message is written for a person, not a log parser."""


@dataclass(slots=True)
class ChannelStatus:
    name: str
    state: str
    detail: str
    kind: str = KIND_OFFICIAL_API
    verified_at: float = 0.0

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ChannelMessage:
    channel: str
    chat: str
    text: str
    message_id: str = ""
    sender: str = ""
    ts: float = field(default_factory=time.time)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


class Channel:
    """Base class. Subclasses implement real transport or refuse with a reason."""

    name = ""
    kind = KIND_OFFICIAL_API

    def status(self) -> ChannelStatus:
        raise NotImplementedError

    def send(self, chat: str, text: str) -> dict[str, Any]:
        raise NotImplementedError

    def inbox(self, limit: int = 20, chat: str = "") -> list[ChannelMessage]:
        raise NotImplementedError

    # -- shared helpers ---------------------------------------------------------------
    def _needs_setup(self, detail: str) -> ChannelStatus:
        return ChannelStatus(self.name, NEEDS_SETUP, detail, self.kind)

    def _unavailable(self, detail: str) -> ChannelStatus:
        return ChannelStatus(self.name, UNAVAILABLE, detail, KIND_UNSUPPORTED)

    def _ready(self, detail: str = "") -> ChannelStatus:
        return ChannelStatus(self.name, READY, detail or "last call succeeded", self.kind, time.time())