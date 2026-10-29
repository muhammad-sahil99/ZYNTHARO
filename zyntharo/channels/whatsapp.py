"""WhatsApp through the bundled sidecar, exposed as a channel.

The transport already exists in :mod:`zyntharo.actions.whatsapp`; this adapter adds no new
capability. It exists so workflows can treat every platform identically while WhatsApp keeps its
own honest states: unpaired is ``needs_setup``, a missing Node runtime is ``needs_setup`` with
that exact instruction, and an unpaired session refuses instead of queueing messages forever.
"""

from __future__ import annotations

from typing import Any, Callable

from .base import Channel, ChannelError, ChannelMessage, ChannelStatus, KIND_LOCAL_SIDECAR


class WhatsAppChannel(Channel):
    name = "whatsapp"
    kind = KIND_LOCAL_SIDECAR

    def __init__(self, backend: Any = None, factory: Callable[[], Any] | None = None):
        self._backend = backend
        self._factory = factory

    def _get(self) -> Any:
        if self._backend is None:
            from ..actions.whatsapp import SidecarWhatsApp

            self._backend = (self._factory or SidecarWhatsApp)()
        return self._backend

    def status(self) -> ChannelStatus:
        try:
            backend = self._get()
        except Exception as exc:  # noqa: BLE001 - a missing runtime is a setup instruction
            return self._needs_setup(f"the bundled WhatsApp sidecar is not usable yet: {exc}")
        try:
            state = backend.status()
        except Exception as exc:  # noqa: BLE001
            return self._needs_setup(f"the WhatsApp sidecar did not answer: {exc}")
        if not state.get("paired"):
            return self._needs_setup(
                "pair WhatsApp once by scanning the QR code in the app (Settings → Channels); "
                "ZYNTHARO stores the session locally and never asks for your password."
            )
        return self._ready("paired session")

    def send(self, chat: str, text: str) -> dict[str, Any]:
        if not str(text).strip():
            raise ChannelError("refusing to send an empty message")
        result = self._get().send_text(chat, text)
        if not result.get("sent"):
            raise ChannelError(f"WhatsApp did not confirm delivery to {chat!r}")
        return {"channel": self.name, "chat": chat, "message_id": str(result.get("id", "")), "chars": len(text)}

    def inbox(self, limit: int = 20, chat: str = "") -> list[ChannelMessage]:
        if not chat:
            raise ChannelError("WhatsApp inbox needs a chat name; list chats first")
        rows = self._get().messages(chat, limit=limit)
        return [
            ChannelMessage(
                channel=self.name,
                chat=chat,
                text=row.get("text", ""),
                message_id=str(row.get("id", "")),
                sender=str(row.get("from", "")),
                ts=float(row.get("timestamp", 0) or 0),
            )
            for row in rows or []
            if row.get("text")
        ]