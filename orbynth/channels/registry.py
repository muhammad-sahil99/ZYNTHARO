"""Which platforms ORBYNTH offers, which it cannot, and why.

The second list is the important one. Customers ask for "all social media"; the honest answer is
that different platforms have completely different business gates. Declaring those gates up front
turns an endless feature request into a decision the customer can make:

* Telegram — official bot API, free, no review. **Offered.**
* WhatsApp — unofficial sidecar of the web session. **Offered**, with the account-risk warning.
* Discord — official bot API. **Offered** through the same HTTP shape as Telegram once a bot
  token exists; declared here as ``needs_setup`` so it is visible rather than imaginary.
* Instagram / Facebook Messenger — Meta requires a business account, app review and messaging
  permission. **Declared unavailable** until an owner supplies an approved app.
* X / Twitter DMs — paid API tier. **Declared unavailable.**
* LinkedIn, TikTok, Snapchat — no usable direct-message API for third-party software.
  **Declared unavailable.**
"""

from __future__ import annotations

from typing import Any

from .base import Channel, ChannelStatus, KIND_UNSUPPORTED, NEEDS_SETUP, UNAVAILABLE
from .telegram import TelegramChannel
from .whatsapp import WhatsAppChannel

#: platform → the actual business/technical gate. Kept as data so the app can show it verbatim.
DECLARED_GATES: dict[str, str] = {
    "instagram": "Meta requires an Instagram Business account, a reviewed app and the "
    "instagram_manage_messages permission; direct messages cannot be automated without approval.",
    "messenger": "Meta requires a reviewed app with pages_messaging before a page's messages can be read.",
    "twitter": "X direct messages require a paid API tier; there is no free automation path.",
    "linkedin": "LinkedIn does not license direct-message automation to third-party software.",
    "tiktok": "TikTok has no third-party direct-message API.",
    "snapchat": "Snapchat has no third-party messaging API.",
}

#: Platforms that are implemented but need the owner's credentials before they run.
CREDENTIAL_GATES: dict[str, str] = {
    "discord": "Create a bot in the Discord developer portal and set ORBYNTH_DISCORD_TOKEN.",
}


class DeclaredChannel(Channel):
    """A platform that is named honestly and never pretends to work."""

    def __init__(self, name: str, detail: str, state: str = UNAVAILABLE):
        self.name = name
        self._detail = detail
        self._state = state
        self.kind = KIND_UNSUPPORTED

    def status(self) -> ChannelStatus:
        return ChannelStatus(self.name, self._state, self._detail, self.kind)

    def send(self, chat: str, text: str) -> dict[str, Any]:
        from .base import ChannelError

        raise ChannelError(f"{self.name} is not available: {self._detail}")

    def inbox(self, limit: int = 20, chat: str = "") -> list[Any]:
        from .base import ChannelError

        raise ChannelError(f"{self.name} is not available: {self._detail}")


class ChannelRegistry:
    def __init__(self, channels: list[Channel]):
        self.channels = {channel.name: channel for channel in channels}

    def names(self) -> list[str]:
        return sorted(self.channels)

    def get(self, name: str) -> Channel:
        channel = self.channels.get(str(name).strip().lower())
        if channel is None:
            available = ", ".join(self.names()) or "none"
            raise KeyError(f"unknown channel {name!r}; known channels: {available}")
        return channel

    def statuses(self) -> list[ChannelStatus]:
        rows = []
        for channel in self.channels.values():
            try:
                rows.append(channel.status())
            except Exception as exc:  # noqa: BLE001 - a status call must never break the app
                rows.append(ChannelStatus(channel.name, NEEDS_SETUP, f"status check failed: {exc}"))
        return rows

    def ready(self) -> list[str]:
        return [row.name for row in self.statuses() if row.state == "ready"]

    def to_json(self) -> list[dict[str, Any]]:
        return [row.to_json() for row in self.statuses()]


def default_registry(home: str = "") -> ChannelRegistry:
    """Every channel the product knows about, each in its real state on this machine."""
    channels: list[Channel] = [
        TelegramChannel(offset_path=f"{home}/telegram-offset.json" if home else ""),
        WhatsAppChannel(),
    ]
    for name, detail in CREDENTIAL_GATES.items():
        channels.append(DeclaredChannel(name, detail, NEEDS_SETUP))
    for name, detail in DECLARED_GATES.items():
        channels.append(DeclaredChannel(name, detail, UNAVAILABLE))
    return ChannelRegistry(channels)


def declared_platforms() -> dict[str, dict[str, str]]:
    """The honest catalogue for the settings screen and the sales page."""
    return {
        "offered": {
            "telegram": "official Bot API",
            "whatsapp": "bundled sidecar (unofficial web session)",
        },
        "needs_credentials": dict(CREDENTIAL_GATES),
        "not_available": dict(DECLARED_GATES),
    }