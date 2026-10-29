"""The WhatsApp test double.

Specification 4.1 bans mocks in production code, and this class used to live inside
``zyntharo/actions/whatsapp.py`` — a fake backend shipped in the product, one wiring mistake away
from being handed a real user's chats. It belongs here, in the harness, where doubles are
required and clearly labelled. The shipped module keeps only the interface
(:class:`~zyntharo.actions.whatsapp.WhatsAppBackend`) and the real transport.
"""

from __future__ import annotations

import time
from typing import Any, Sequence

from zyntharo.actions.whatsapp import WhatsAppBackend


class MockWhatsApp(WhatsAppBackend):
    """In-memory backend: the whole action surface exercised with no phone and no Node."""

    def __init__(self, allow_list: Sequence[str] = ("Mom", "+15550001111")):
        self.allow_list = set(allow_list)
        self.paired = False
        self._messages: dict[str, list[dict[str, Any]]] = {
            "Mom": [
                {"id": "m1", "from": "Mom", "text": "call me when you can", "ts": time.time() - 600, "type": "text"},
                {"id": "m2", "from": "me", "text": "will do tonight", "ts": time.time() - 540, "type": "text"},
            ],
            "+15550001111": [
                {"id": "m3", "from": "+15550001111", "text": "invoice attached", "ts": time.time() - 120, "type": "document", "media": "invoice.pdf"},
            ],
        }
        self.calls: list[dict[str, Any]] = []
        self.sent: list[dict[str, Any]] = []
        self.archived_chats: set[str] = set()
        self.settings_changes: list[tuple[Any, ...]] = []

    def status(self) -> dict[str, Any]:
        return {"paired": self.paired, "device": "mock", "chats": len(self._messages)}

    def pair(self) -> dict[str, Any]:
        self.paired = True
        return {"qr": "MOCK-QR-DATA", "paired": True, "expires_in": 60}

    def pair_with_phone(self, phone: str) -> dict[str, Any]:
        self.paired = True
        return {"pairing_code": "1234-5678", "phone": phone, "paired": True}

    def logout(self) -> dict[str, Any]:
        self.paired = False
        return {"logged_out": True}

    def chats(self, limit: int = 50, filter: str = "") -> list[dict[str, Any]]:  # noqa: A002
        rows = [
            {"id": name, "name": name, "unread": 1 if name == "Mom" else 0, "last_message": msgs[-1]["text"] if msgs else ""}
            for name, msgs in self._messages.items()
        ]
        if filter:
            rows = [r for r in rows if filter.lower() in r["name"].lower()]
        return rows[:limit]

    def search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        needle = query.lower()
        hits: list[dict[str, Any]] = []
        for chat, messages in self._messages.items():
            for message in messages:
                if needle in message["text"].lower():
                    hits.append({"chat": chat, **message})
        return hits[:limit]

    def messages(self, chat: str, limit: int = 50) -> list[dict[str, Any]]:
        return self._messages.get(chat, [])[-limit:]

    def send_text(self, chat: str, text: str) -> dict[str, Any]:
        self._messages.setdefault(chat, []).append({"id": f"out{len(self.sent)}", "from": "me", "text": text, "ts": time.time(), "type": "text"})
        record = {"chat": chat, "text": text, "sent": True}
        self.sent.append(record)
        return record

    def send_media(self, chat: str, path: str, caption: str = "", as_voice: bool = False) -> dict[str, Any]:
        record = {"chat": chat, "path": path, "caption": caption, "voice": as_voice, "sent": True}
        self.sent.append(record)
        return record

    def download_media(self, message_id: str, dest: str = "") -> dict[str, Any]:
        return {"message_id": message_id, "path": dest or "/tmp/mock-media.bin", "bytes": 1024}

    def mark_read(self, chat: str) -> dict[str, Any]:
        return {"chat": chat, "read": True}

    def presence(self, chat: str, state: str) -> dict[str, Any]:
        return {"chat": chat, "presence": state}

    def call(self, chat: str, answer: bool = False, transcribe: bool = True) -> dict[str, Any]:
        record = {"chat": chat, "answered": answer, "transcribing": transcribe, "state": "connected"}
        self.calls.append(record)
        return record

    def hangup(self) -> dict[str, Any]:
        return {"state": "ended"}

    def transcript(self, seconds: float = 5.0) -> dict[str, Any]:
        return {"seconds": seconds, "text": "[mock] caller: hello? / them: just calling to confirm dinner"}

    # -- archive and settings ---------------------------------------------------------
    def archived(self, limit: int = 50) -> list[dict[str, Any]]:
        return [{"id": name, "name": name, "archived": True} for name in self.archived_chats][:limit]

    def unread(self, limit: int = 50) -> list[dict[str, Any]]:
        return [row for row in self.chats(limit=limit) if row.get("unread")][:limit]

    def archive(self, chat: str, archived: bool = True) -> dict[str, Any]:
        if archived:
            self.archived_chats.add(chat)
        else:
            self.archived_chats.discard(chat)
        return {"chat": chat, "archived": archived}

    def pin(self, chat: str, pinned: bool = True) -> dict[str, Any]:
        self.settings_changes.append(("pin", chat, pinned))
        return {"chat": chat, "pinned": pinned}

    def mute(self, chat: str, muted: bool = True, seconds: int = 28800) -> dict[str, Any]:
        self.settings_changes.append(("mute", chat, muted))
        return {"chat": chat, "muted": muted, "seconds": seconds if muted else 0}

    def settings(self) -> dict[str, Any]:
        return {
            "session": {"archived": len(self.archived_chats), "unread": len([r for r in self.chats() if r.get("unread")])},
            "changes": [list(row) for row in self.settings_changes],
        }

    def react(self, chat: str, message_id: str, emoji: str = "") -> dict[str, Any]:
        self.settings_changes.append(("react", message_id, emoji))
        return {"message_id": message_id, "emoji": emoji}

    def reply(self, chat: str, message_id: str, text: str) -> dict[str, Any]:
        self.sent.append({"chat": chat, "text": text, "reply_to": message_id, "sent": True})
        return {"chat": chat, "reply_to": message_id, "sent": True}

    def delete_message(self, chat: str, message_id: str, for_everyone: bool = False) -> dict[str, Any]:
        self.settings_changes.append(("delete", chat, message_id))
        return {"message_id": message_id, "for_everyone": for_everyone, "deleted": True}

    def forward(self, from_chat: str, message_id: str, to_chats: Sequence[str]) -> dict[str, Any]:
        self.settings_changes.append(("forward", message_id, tuple(to_chats)))
        return {"message_id": message_id, "forwarded_to": list(to_chats)}

    def groups(self, limit: int = 50) -> list[dict[str, Any]]:
        return [{"id": "group-1", "name": "Suppliers", "is_group": True}][:limit]

    def contacts(self, limit: int = 100) -> list[dict[str, Any]]:
        return [{"id": name, "name": name, "number": ""} for name in self._messages][:limit]

    def export_chat(self, chat: str, limit: int = 500) -> dict[str, Any]:
        rows = self.messages(chat, limit)
        return {"chat": chat, "count": len(rows), "messages": rows}

    def health(self) -> dict[str, Any]:
        return {"ok": True, "paired": self.paired, "ready": self.paired, "clone": "mock"}
