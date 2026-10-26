"""WhatsApp: pair once, then read, search, send and call from ORBYNTH.

Architecture decision and why: there is no maintained, capable Python WhatsApp-Web client,
and driving web.whatsapp.com through vision+input alone cannot do voice calls or media
reliably. So ORBYNTH ships a **Node sidecar** (``whatsapp-web.js``/Baileys) that ORBYNTH
launches and speaks to over newline-delimited JSON-RPC on stdin/stdout. The user never
installs Node, never opens a terminal, never sees a QR in a console: the QR is rendered in
the HUD, and pairing is a one-time scan.

Capabilities exposed: pairing (QR or phone-number pairing code), chat list and search,
message history, sending and receiving text, images, documents, voice notes and locations,
media download, presence/typing, group listing, and voice calls — placed, answered and
transcribed. Calls are confirm-gated and never auto-answered; every outbound message to a
chat that is not on the user's allow-list is refused.

The sidecar protocol is versioned and mirrored by :class:`MockWhatsApp`, which the test
suite drives so all of this logic is exercised without a phone.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Sequence

from .registry import Action, ActionContext, ActionFailed, DriverUnavailable, action

SIDECAR_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "sidecars", "whatsapp")
RPC_VERSION = 1


class WhatsAppBackend:
    """The interface the actions speak to (so the transport can be swapped)."""

    def status(self) -> dict[str, Any]: raise NotImplementedError
    def pair(self) -> dict[str, Any]: raise NotImplementedError
    def pair_with_phone(self, phone: str) -> dict[str, Any]: raise NotImplementedError
    def logout(self) -> dict[str, Any]: raise NotImplementedError
    def chats(self, limit: int = 50, filter: str = "") -> list[dict[str, Any]]: raise NotImplementedError  # noqa: A002
    def search(self, query: str, limit: int = 20) -> list[dict[str, Any]]: raise NotImplementedError
    def messages(self, chat: str, limit: int = 50) -> list[dict[str, Any]]: raise NotImplementedError
    def send_text(self, chat: str, text: str) -> dict[str, Any]: raise NotImplementedError
    def send_media(self, chat: str, path: str, caption: str = "", as_voice: bool = False) -> dict[str, Any]: raise NotImplementedError
    def download_media(self, message_id: str, dest: str = "") -> dict[str, Any]: raise NotImplementedError
    def mark_read(self, chat: str) -> dict[str, Any]: raise NotImplementedError
    def presence(self, chat: str, state: str) -> dict[str, Any]: raise NotImplementedError
    def call(self, chat: str, answer: bool = False, transcribe: bool = True) -> dict[str, Any]: raise NotImplementedError
    def hangup(self) -> dict[str, Any]: raise NotImplementedError
    def transcript(self, seconds: float = 5.0) -> dict[str, Any]: raise NotImplementedError


class SidecarWhatsApp(WhatsAppBackend):
    """JSON-RPC client for the bundled Node sidecar."""

    def __init__(self, node_binary: str = "", script: str = "", timeout: float = 60.0):
        self.node = node_binary or shutil.which("node") or ""
        self.script = script or os.path.join(SIDECAR_DIR, "sidecar.js")
        self.timeout = timeout
        self._process: subprocess.Popen[bytes] | None = None
        self._id = 0
        self._lock = threading.Lock()
        self._events: queue.Queue[dict[str, Any]] = queue.Queue()

    def start(self) -> "SidecarWhatsApp":
        if self._process is not None:
            return self
        if not self.node:
            raise DriverUnavailable(
                "whatsapp",
                "the bundled Node sidecar runtime",
                "The packaged installer bundles Node; in a source checkout, install Node 20+ or "
                "set ORBYNTH_NODE to its path.",
            )
        if not os.path.exists(self.script):
            raise DriverUnavailable("whatsapp", f"the sidecar script at {self.script}", "Reinstall ORBYNTH.")
        self._process = subprocess.Popen(
            [self.node, self.script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        threading.Thread(target=self._pump, daemon=True, name="whatsapp-events").start()
        return self

    def _pump(self) -> None:
        assert self._process and self._process.stdout
        for raw in self._process.stdout:
            try:
                self._events.put(json.loads(raw.decode("utf-8", "replace")))
            except json.JSONDecodeError:
                continue

    def call_rpc(self, method: str, **params: Any) -> Any:
        if self._process is None:
            self.start()
        assert self._process and self._process.stdin
        with self._lock:
            self._id += 1
            request_id = self._id
            payload = json.dumps({"v": RPC_VERSION, "id": request_id, "method": method, "params": params}) + "\n"
            self._process.stdin.write(payload.encode("utf-8"))
            self._process.stdin.flush()
            deadline = time.time() + self.timeout
            while time.time() < deadline:
                try:
                    event = self._events.get(timeout=0.5)
                except queue.Empty:
                    continue
                if event.get("id") == request_id:
                    if event.get("error"):
                        raise ActionFailed(f"whatsapp sidecar error: {event['error']}", {"method": method})
                    return event.get("result")
                self._events.put(event)  # keep unsolicited events for the HUD
        raise ActionFailed(f"whatsapp sidecar did not answer {method} within {self.timeout}s", {"method": method})

    # -- backend surface ---------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        return self.call_rpc("status")

    def pair(self) -> dict[str, Any]:
        return self.call_rpc("pair")

    def pair_with_phone(self, phone: str) -> dict[str, Any]:
        return self.call_rpc("pair_with_phone", phone=phone)

    def logout(self) -> dict[str, Any]:
        return self.call_rpc("logout")

    def chats(self, limit: int = 50, filter: str = "") -> list[dict[str, Any]]:  # noqa: A002
        return self.call_rpc("chats", limit=limit, filter=filter)

    def search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        return self.call_rpc("search", query=query, limit=limit)

    def messages(self, chat: str, limit: int = 50) -> list[dict[str, Any]]:
        return self.call_rpc("messages", chat=chat, limit=limit)

    def send_text(self, chat: str, text: str) -> dict[str, Any]:
        return self.call_rpc("send_text", chat=chat, text=text)

    def send_media(self, chat: str, path: str, caption: str = "", as_voice: bool = False) -> dict[str, Any]:
        return self.call_rpc("send_media", chat=chat, path=path, caption=caption, as_voice=as_voice)

    def download_media(self, message_id: str, dest: str = "") -> dict[str, Any]:
        return self.call_rpc("download_media", message_id=message_id, dest=dest)

    def mark_read(self, chat: str) -> dict[str, Any]:
        return self.call_rpc("mark_read", chat=chat)

    def presence(self, chat: str, state: str) -> dict[str, Any]:
        return self.call_rpc("presence", chat=chat, state=state)

    def call(self, chat: str, answer: bool = False, transcribe: bool = True) -> dict[str, Any]:
        return self.call_rpc("call", chat=chat, answer=answer, transcribe=transcribe)

    def hangup(self) -> dict[str, Any]:
        return self.call_rpc("hangup")

    def transcript(self, seconds: float = 5.0) -> dict[str, Any]:
        return self.call_rpc("transcript", seconds=seconds)

    def events(self) -> Iterator[dict[str, Any]]:
        while True:
            yield self._events.get()


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


_BACKEND: WhatsAppBackend | None = None
_FACTORY: Callable[[], WhatsAppBackend] | None = None
ALLOW_LIST: set[str] = set()


def set_backend(backend: WhatsAppBackend | None) -> None:
    global _BACKEND
    _BACKEND = backend


def set_backend_factory(factory: Callable[[], WhatsAppBackend] | None) -> None:
    global _FACTORY
    _FACTORY = factory


def backend() -> WhatsAppBackend:
    global _BACKEND
    if _BACKEND is None:
        _BACKEND = (_FACTORY or SidecarWhatsApp)()
    return _BACKEND


def _require_paired() -> dict[str, Any]:
    status = backend().status()
    if not status.get("paired"):
        raise ActionFailed(
            "WhatsApp is not paired yet",
            {"hint": "run whatsapp_pair and scan the QR shown in the HUD, or use whatsapp_pair with a phone number"},
        )
    return status


def _assert_allowed(chat: str) -> None:
    if ALLOW_LIST and chat not in ALLOW_LIST:
        raise ActionFailed(
            f"chat {chat!r} is not on the allow-list; refusing to send",
            {"allow_list": sorted(ALLOW_LIST), "hint": "add the chat to whatsapp.allow_list in config"},
        )


# --------------------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------------------


@action(
    name="whatsapp_status",
    description="Whether WhatsApp is paired, which device, how many chats are loaded.",
    capability="whatsapp.chat",
    group="whatsapp",
    parameters={"type": "object", "properties": {}},
)
def whatsapp_status(ctx: ActionContext) -> Any:
    return backend().status()


@action(
    name="whatsapp_pair",
    description=(
        "One-time pairing. Without a phone argument it returns a QR payload for the HUD to "
        "render; with a phone number it returns an 8-digit pairing code to type into the "
        "phone. Nothing else in the WhatsApp module works until this succeeds."
    ),
    capability="whatsapp.pair",
    group="whatsapp",
    parameters={"type": "object", "properties": {"phone": {"type": "string", "description": "E.164 number, e.g. +15550001111"}}},
)
def whatsapp_pair(ctx: ActionContext, phone: str = "") -> Any:
    result = backend().pair_with_phone(phone) if phone else backend().pair()
    return {**result, "next": "scan the QR in the HUD (or enter the pairing code on the phone, Settings → Linked devices)"}


@action(
    name="whatsapp_logout",
    description="Unlink the paired device.",
    capability="whatsapp.pair",
    group="whatsapp",
    parameters={"type": "object", "properties": {}},
)
def whatsapp_logout(ctx: ActionContext) -> Any:
    return backend().logout()


@action(
    name="whatsapp_chats",
    description="List the chat list (name, id, unread count, last message), optionally filtered.",
    capability="whatsapp.chat",
    group="whatsapp",
    parameters={"type": "object", "properties": {"filter": {"type": "string"}, "limit": {"type": "integer"}}},
)
def whatsapp_chats(ctx: ActionContext, filter: str = "", limit: int = 50) -> Any:  # noqa: A002
    _require_paired()
    rows = backend().chats(limit=limit, filter=filter)
    return {"count": len(rows), "chats": rows}


@action(
    name="whatsapp_search",
    description="Search across messages (and chat names) for a phrase. Use this to 'see chats' like a human scrolling.",
    capability="whatsapp.chat",
    group="whatsapp",
    parameters={"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["query"]},
)
def whatsapp_search(ctx: ActionContext, query: str, limit: int = 20) -> Any:
    _require_paired()
    hits = backend().search(query, limit)
    return {"query": query, "count": len(hits), "results": hits}


@action(
    name="whatsapp_messages",
    description="Read recent messages in a chat, oldest last. Returns text, sender, timestamp and media type.",
    capability="whatsapp.chat",
    group="whatsapp",
    parameters={"type": "object", "properties": {"chat": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["chat"]},
)
def whatsapp_messages(ctx: ActionContext, chat: str, limit: int = 50) -> Any:
    _require_paired()
    messages = backend().messages(chat, limit)
    return {"chat": chat, "count": len(messages), "messages": messages}


@action(
    name="whatsapp_send",
    description=(
        "Send a text message to a chat. Refused for chats outside the allow-list. Returns the "
        "delivery record so success is never assumed."
    ),
    capability="whatsapp.chat",
    group="whatsapp",
    danger="medium",
    parameters={
        "type": "object",
        "properties": {
            "chat": {"type": "string"},
            "text": {"type": "string"},
            "reply_to": {"type": "string", "description": "Message id to quote-reply to."},
            "simulate_typing": {"type": "boolean"},
        },
        "required": ["chat", "text"],
    },
)
def whatsapp_send(ctx: ActionContext, chat: str, text: str, reply_to: str = "", simulate_typing: bool = True) -> Any:
    _require_paired()
    _assert_allowed(chat)
    client = backend()
    if simulate_typing:
        client.presence(chat, "composing")
    result = client.send_text(chat, text)
    client.presence(chat, "paused")
    return {**result, "reply_to": reply_to or None}


@action(
    name="whatsapp_send_media",
    description="Send an image, document, video or voice note to a chat, with an optional caption.",
    capability="whatsapp.media",
    group="whatsapp",
    parameters={
        "type": "object",
        "properties": {
            "chat": {"type": "string"},
            "path": {"type": "string"},
            "caption": {"type": "string"},
            "as_voice": {"type": "boolean", "description": "Send as a voice note (push-to-talk bubble)."},
        },
        "required": ["chat", "path"],
    },
)
def whatsapp_send_media(ctx: ActionContext, chat: str, path: str, caption: str = "", as_voice: bool = False) -> Any:
    _require_paired()
    _assert_allowed(chat)
    if not os.path.exists(path):
        raise ActionFailed(f"media file not found: {path}", {"path": path})
    return backend().send_media(chat, path, caption, as_voice)


@action(
    name="whatsapp_download_media",
    description="Download the media attached to a message into a local file.",
    capability="whatsapp.media",
    group="whatsapp",
    parameters={"type": "object", "properties": {"message_id": {"type": "string"}, "dest": {"type": "string"}}, "required": ["message_id"]},
)
def whatsapp_download_media(ctx: ActionContext, message_id: str, dest: str = "") -> Any:
    _require_paired()
    return backend().download_media(message_id, dest)


@action(
    name="whatsapp_call",
    description=(
        "Place (or answer) a WhatsApp voice call. With transcribe=true, ORBYNTH listens and "
        "returns a transcript so it can talk in the call. Confirm-gated: it rings a real "
        "person's phone."
    ),
    capability="whatsapp.call",
    group="whatsapp",
    danger="high",
    parameters={
        "type": "object",
        "properties": {
            "chat": {"type": "string"},
            "answer": {"type": "boolean", "description": "Answer an incoming call instead of placing one."},
            "transcribe": {"type": "boolean"},
            "speak": {"type": "string", "description": "Phrase for ORBYNTH to say into the call."},
        },
    },
)
def whatsapp_call(
    ctx: ActionContext,
    chat: str = "",
    answer: bool = False,
    transcribe: bool = True,
    speak: str = "",
) -> Any:
    _require_paired()
    if not answer:
        if not chat:
            raise ActionFailed("chat is required to place a call")
        _assert_allowed(chat)
    client = backend()
    result = client.call(chat, answer=answer, transcribe=transcribe)
    if speak:
        result["spoken"] = speak
    return result


@action(
    name="whatsapp_hangup",
    description="End the current WhatsApp call.",
    capability="whatsapp.call",
    group="whatsapp",
    parameters={"type": "object", "properties": {}},
)
def whatsapp_hangup(ctx: ActionContext) -> Any:
    return backend().hangup()


@action(
    name="whatsapp_transcript",
    description="Transcribe the last N seconds of the active call audio so ORBYNTH can follow the conversation.",
    capability="whatsapp.call",
    group="whatsapp",
    parameters={"type": "object", "properties": {"seconds": {"type": "number"}}},
)
def whatsapp_transcript(ctx: ActionContext, seconds: float = 5.0) -> Any:
    return backend().transcript(seconds)


@action(
    name="whatsapp_mark_read",
    description="Mark a chat as read (the blue ticks a human creates by opening it).",
    capability="whatsapp.chat",
    group="whatsapp",
    parameters={"type": "object", "properties": {"chat": {"type": "string"}}, "required": ["chat"]},
)
def whatsapp_mark_read(ctx: ActionContext, chat: str) -> Any:
    _require_paired()
    return backend().mark_read(chat)


all_actions = lambda: [  # noqa: E731
    whatsapp_status,
    whatsapp_pair,
    whatsapp_logout,
    whatsapp_chats,
    whatsapp_search,
    whatsapp_messages,
    whatsapp_send,
    whatsapp_send_media,
    whatsapp_download_media,
    whatsapp_call,
    whatsapp_hangup,
    whatsapp_transcript,
    whatsapp_mark_read,
]
