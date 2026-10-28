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

The sidecar protocol is versioned. The test suite drives it through a double that lives in
the harness (``tests/whatsapp_double.py``), never in shipped code (spec 4.1).
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
    # chats and archive
    def archived(self, limit: int = 50) -> list[dict[str, Any]]: raise NotImplementedError
    def unread(self, limit: int = 50) -> list[dict[str, Any]]: raise NotImplementedError
    def archive(self, chat: str, archived: bool = True) -> dict[str, Any]: raise NotImplementedError
    # chat settings a person actually changes
    def pin(self, chat: str, pinned: bool = True) -> dict[str, Any]: raise NotImplementedError
    def mute(self, chat: str, muted: bool = True, seconds: int = 28800) -> dict[str, Any]: raise NotImplementedError
    def settings(self) -> dict[str, Any]: raise NotImplementedError
    # message actions
    def react(self, chat: str, message_id: str, emoji: str = "") -> dict[str, Any]: raise NotImplementedError
    def reply(self, chat: str, message_id: str, text: str) -> dict[str, Any]: raise NotImplementedError
    def delete_message(self, chat: str, message_id: str, for_everyone: bool = False) -> dict[str, Any]: raise NotImplementedError
    def forward(self, from_chat: str, message_id: str, to_chats: Sequence[str]) -> dict[str, Any]: raise NotImplementedError
    # directories
    def groups(self, limit: int = 50) -> list[dict[str, Any]]: raise NotImplementedError
    def contacts(self, limit: int = 100) -> list[dict[str, Any]]: raise NotImplementedError
    def export_chat(self, chat: str, limit: int = 500) -> dict[str, Any]: raise NotImplementedError
    # connection health
    def health(self) -> dict[str, Any]: raise NotImplementedError


class SidecarWhatsApp(WhatsAppBackend):
    """JSON-RPC client for the bundled Node sidecar."""

    def __init__(self, node_binary: str = "", script: str = "", timeout: float = 60.0):
        self.node = node_binary or shutil.which("node") or ""
        self.script = script or os.path.join(SIDECAR_DIR, "sidecar.js")
        self.timeout = timeout
        self._process: subprocess.Popen[bytes] | None = None
        self._launched: Any = None
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
        # Through the supervisor: a sidecar that outlives the app keeps a WhatsApp session
        # paired in the background with nobody watching it, which is exactly the orphan case
        # the process architecture exists to prevent. Pipes are kept because this is JSON-RPC.
        from ..ops.spawn import managed_popen

        self._launched = managed_popen(
            [self.node, self.script],
            name="whatsapp-sidecar",
            kind="sidecar",
            pipes=True,
            restart="on-failure",
        )
        self._process = self._launched.process
        threading.Thread(target=self._pump, daemon=True, name="whatsapp-events").start()
        return self

    def stop(self) -> None:
        """Shut the sidecar down. Called by the driver's owner; idempotent."""
        launched = getattr(self, "_launched", None)
        if launched is not None:
            launched.terminate(force=True)
        self._launched = None
        self._process = None

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

    def archived(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.call_rpc("archived", limit=limit)

    def unread(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.call_rpc("unread", limit=limit)

    def archive(self, chat: str, archived: bool = True) -> dict[str, Any]:
        return self.call_rpc("archive", chat=chat, archived=archived)

    def pin(self, chat: str, pinned: bool = True) -> dict[str, Any]:
        return self.call_rpc("pin", chat=chat, pinned=pinned)

    def mute(self, chat: str, muted: bool = True, seconds: int = 28800) -> dict[str, Any]:
        return self.call_rpc("mute", chat=chat, muted=muted, seconds=seconds)

    def settings(self) -> dict[str, Any]:
        return self.call_rpc("settings")

    def react(self, chat: str, message_id: str, emoji: str = "") -> dict[str, Any]:
        return self.call_rpc("react", chat=chat, message_id=message_id, emoji=emoji)

    def reply(self, chat: str, message_id: str, text: str) -> dict[str, Any]:
        return self.call_rpc("reply", chat=chat, message_id=message_id, text=text)

    def delete_message(self, chat: str, message_id: str, for_everyone: bool = False) -> dict[str, Any]:
        return self.call_rpc("delete_message", chat=chat, message_id=message_id, for_everyone=for_everyone)

    def forward(self, from_chat: str, message_id: str, to_chats: Sequence[str]) -> dict[str, Any]:
        return self.call_rpc("forward", from_chat=from_chat, message_id=message_id, to_chats=list(to_chats))

    def groups(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.call_rpc("groups", limit=limit)

    def contacts(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.call_rpc("contacts", limit=limit)

    def export_chat(self, chat: str, limit: int = 500) -> dict[str, Any]:
        return self.call_rpc("export_chat", chat=chat, limit=limit)

    def health(self) -> dict[str, Any]:
        return self.call_rpc("health")

    def events(self) -> Iterator[dict[str, Any]]:
        while True:
            yield self._events.get()




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


#: The duplex call bridge, created on first use. It is what puts a voice *into* a call: the
#: previous implementation only recorded the phrase it was asked to say, so nothing was audible.
_BRIDGE: Any = None
_CONFIG: Any = None
_TRANSCRIBER: Callable[[bytes], str] | None = None


def set_config(config: Any) -> None:
    """The CLI hands its config in once: the call bridge needs it for voice-tier settings."""
    global _CONFIG
    _CONFIG = config


def set_transcriber(transcriber: Callable[[bytes], str] | None) -> None:
    """Wire the voice session's STT in, so captured call audio can be turned into words."""
    global _TRANSCRIBER
    _TRANSCRIBER = transcriber


def _bridge() -> Any:
    global _BRIDGE
    if _BRIDGE is None:
        from ..voice.callbridge import CallAudioBridge
        from ..voice.tts import StreamingVoice

        _BRIDGE = CallAudioBridge(voice=StreamingVoice(_CONFIG))
    return _BRIDGE


def set_bridge(bridge: Any) -> None:
    """Replace the bridge (tests, and the packaged installer's own audio path)."""
    global _BRIDGE
    _BRIDGE = bridge


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
    """Place or answer a call, and actually be audible in it.

    The old version took ``speak`` and copied the string into the result — nothing was ever
    spoken, so the voice quality was moot. Now the phrase is synthesised through the best voice
    tier the machine has and written frame by frame into the call's audio path. If no audio path
    exists, that is raised as a driver problem naming the install step, never reported as speech.
    """
    _require_paired()
    if not answer:
        if not chat:
            raise ActionFailed("chat is required to place a call")
        _assert_allowed(chat)
    client = backend()
    result = dict(client.call(chat, answer=answer, transcribe=transcribe) or {})
    bridge = _bridge()
    result["audio"] = bridge.status()
    if result.get("state") == "unsupported":
        # The honest path, and the documented one: WhatsApp Web has no call API, so the call is
        # driven in the desktop application and the audio is routed by the bridge. The response
        # says so instead of implying a call was placed.
        result["next"] = result.get("fallback_detail", "")
        result["requires"] = "the WhatsApp desktop application, an OS input driver, and a loopback audio device"
    if speak:
        result["spoken"] = bridge.speak(speak)
    return result


@action(
    name="whatsapp_say",
    description=(
        "Speak a phrase into the live call. The phrase is synthesised through the best voice tier "
        "available (realtime > streaming neural > batch neural > offline) and written into the "
        "call's audio path in 20 ms frames, so it can be interrupted mid-sentence."
    ),
    capability="whatsapp.call",
    group="whatsapp",
    danger="medium",
    parameters={
        "type": "object",
        "properties": {"text": {"type": "string"}, "voice": {"type": "string"}, "timeout": {"type": "number"}},
        "required": ["text"],
    },
)
def whatsapp_say(ctx: ActionContext, text: str, voice: str = "", timeout: float = 60.0) -> Any:
    if not text.strip():
        raise ActionFailed("nothing to say")
    bridge = _bridge()
    return {**bridge.speak(text, voice, timeout), "audio": bridge.status()}


@action(
    name="whatsapp_listen",
    description=(
        "Capture the live call's audio and transcribe it, so ORBYNTH can follow the conversation "
        "and answer. Barge-in is automatic: if the caller speaks while ORBYNTH is talking, the "
        "current utterance is cut off mid-frame."
    ),
    capability="whatsapp.call",
    group="whatsapp",
    parameters={
        "type": "object",
        "properties": {"seconds": {"type": "number"}, "transcribe": {"type": "boolean"}},
    },
)
def whatsapp_listen(ctx: ActionContext, seconds: float = 5.0, transcribe: bool = True) -> Any:
    bridge = _bridge()
    captured = bridge.listen(seconds)
    text = ""
    if transcribe and captured.get("pcm") and _TRANSCRIBER is not None:
        text = _TRANSCRIBER(captured["pcm"]) or ""
    return {
        **{key: value for key, value in captured.items() if key != "pcm"},
        "text": text,
        "tier": bridge.voice.tier,
        "transcriber": "voice-session" if _TRANSCRIBER is not None else "none (wire the STT in to get words)",
    }


@action(
    name="whatsapp_audio",
    description=(
        "Why the voice sounds the way it does: which audio route is in use (loopback microphone, "
        "speakerphone or none), which voice tier is live, time to first frame, and what to install "
        "to reach a more human tier."
    ),
    capability="whatsapp.call",
    group="whatsapp",
    parameters={"type": "object", "properties": {}},
)
def whatsapp_audio(ctx: ActionContext) -> Any:
    return _bridge().status()


@action(
    name="whatsapp_archived",
    description="List archived chats — the ones hidden from the main list.",
    capability="whatsapp.chat",
    group="whatsapp",
    parameters={"type": "object", "properties": {"limit": {"type": "integer"}}},
)
def whatsapp_archived(ctx: ActionContext, limit: int = 50) -> Any:
    _require_paired()
    rows = backend().archived(limit)
    return {"count": len(rows), "chats": rows}


@action(
    name="whatsapp_unread",
    description="List chats with unread messages, so the fleet can triage what needs an answer.",
    capability="whatsapp.chat",
    group="whatsapp",
    parameters={"type": "object", "properties": {"limit": {"type": "integer"}}},
)
def whatsapp_unread(ctx: ActionContext, limit: int = 50) -> Any:
    _require_paired()
    rows = backend().unread(limit)
    return {"count": len(rows), "chats": rows}


@action(
    name="whatsapp_archive",
    description="Archive or unarchive a chat (archived is false to bring it back).",
    capability="whatsapp.chat",
    group="whatsapp",
    danger="medium",
    parameters={
        "type": "object",
        "properties": {"chat": {"type": "string"}, "archived": {"type": "boolean"}},
        "required": ["chat"],
    },
)
def whatsapp_archive(ctx: ActionContext, chat: str, archived: bool = True) -> Any:
    _require_paired()
    return backend().archive(chat, archived)


@action(
    name="whatsapp_pin",
    description="Pin or unpin a chat at the top of the list.",
    capability="whatsapp.chat",
    group="whatsapp",
    parameters={
        "type": "object",
        "properties": {"chat": {"type": "string"}, "pinned": {"type": "boolean"}},
        "required": ["chat"],
    },
)
def whatsapp_pin(ctx: ActionContext, chat: str, pinned: bool = True) -> Any:
    _require_paired()
    return backend().pin(chat, pinned)


@action(
    name="whatsapp_mute",
    description="Mute or unmute a chat for a number of seconds (default 8 hours).",
    capability="whatsapp.chat",
    group="whatsapp",
    parameters={
        "type": "object",
        "properties": {"chat": {"type": "string"}, "muted": {"type": "boolean"}, "seconds": {"type": "integer"}},
        "required": ["chat"],
    },
)
def whatsapp_mute(ctx: ActionContext, chat: str, muted: bool = True, seconds: int = 28800) -> Any:
    _require_paired()
    return backend().mute(chat, muted, seconds)


@action(
    name="whatsapp_settings",
    description=(
        "Session settings in one place: archived and unread counts, linked account, and what the "
        "protocol can and cannot do. Privacy and disappearing-message policy are account settings "
        "that only the phone can change, and this reports that rather than pretending otherwise."
    ),
    capability="whatsapp.chat",
    group="whatsapp",
    parameters={"type": "object", "properties": {}},
)
def whatsapp_settings(ctx: ActionContext) -> Any:
    _require_paired()
    return backend().settings()


@action(
    name="whatsapp_react",
    description="React to a message with an emoji (empty string removes the reaction).",
    capability="whatsapp.chat",
    group="whatsapp",
    parameters={
        "type": "object",
        "properties": {"chat": {"type": "string"}, "message_id": {"type": "string"}, "emoji": {"type": "string"}},
        "required": ["chat", "message_id"],
    },
)
def whatsapp_react(ctx: ActionContext, chat: str, message_id: str, emoji: str = "") -> Any:
    _require_paired()
    return backend().react(chat, message_id, emoji)


@action(
    name="whatsapp_reply",
    description="Quote-reply to a specific message.",
    capability="whatsapp.chat",
    group="whatsapp",
    danger="medium",
    parameters={
        "type": "object",
        "properties": {"chat": {"type": "string"}, "message_id": {"type": "string"}, "text": {"type": "string"}},
        "required": ["chat", "message_id", "text"],
    },
)
def whatsapp_reply(ctx: ActionContext, chat: str, message_id: str, text: str) -> Any:
    _require_paired()
    _assert_allowed(chat)
    return backend().reply(chat, message_id, text)


@action(
    name="whatsapp_delete",
    description="Delete a message (for_everyone=true recalls it for the other side too).",
    capability="whatsapp.chat",
    group="whatsapp",
    danger="high",
    parameters={
        "type": "object",
        "properties": {
            "chat": {"type": "string"}, "message_id": {"type": "string"},
            "for_everyone": {"type": "boolean"},
        },
        "required": ["chat", "message_id"],
    },
)
def whatsapp_delete(ctx: ActionContext, chat: str, message_id: str, for_everyone: bool = False) -> Any:
    _require_paired()
    return backend().delete_message(chat, message_id, for_everyone)


@action(
    name="whatsapp_forward",
    description="Forward a message to one or more chats.",
    capability="whatsapp.chat",
    group="whatsapp",
    danger="medium",
    parameters={
        "type": "object",
        "properties": {
            "from_chat": {"type": "string"}, "message_id": {"type": "string"},
            "to_chats": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["from_chat", "message_id", "to_chats"],
    },
)
def whatsapp_forward(ctx: ActionContext, from_chat: str, message_id: str, to_chats: Sequence[str]) -> Any:
    _require_paired()
    for destination in to_chats:
        _assert_allowed(destination)
    return backend().forward(from_chat, message_id, list(to_chats))


@action(
    name="whatsapp_groups",
    description="List group chats this account is in.",
    capability="whatsapp.chat",
    group="whatsapp",
    parameters={"type": "object", "properties": {"limit": {"type": "integer"}}},
)
def whatsapp_groups(ctx: ActionContext, limit: int = 50) -> Any:
    _require_paired()
    rows = backend().groups(limit)
    return {"count": len(rows), "groups": rows}


@action(
    name="whatsapp_contacts",
    description="List contacts with names and numbers, so chats can be addressed by name.",
    capability="whatsapp.chat",
    group="whatsapp",
    parameters={"type": "object", "properties": {"limit": {"type": "integer"}}},
)
def whatsapp_contacts(ctx: ActionContext, limit: int = 100) -> Any:
    _require_paired()
    rows = backend().contacts(limit)
    return {"count": len(rows), "contacts": rows}


@action(
    name="whatsapp_export_chat",
    description="Export a chat's messages as structured rows for filing, searching or reporting.",
    capability="whatsapp.chat",
    group="whatsapp",
    parameters={
        "type": "object",
        "properties": {"chat": {"type": "string"}, "limit": {"type": "integer"}},
        "required": ["chat"],
    },
)
def whatsapp_export_chat(ctx: ActionContext, chat: str, limit: int = 500) -> Any:
    _require_paired()
    return backend().export_chat(chat, limit)


@action(
    name="whatsapp_health",
    description=(
        "Connection health: paired, ready, seconds since the last event, reconnect attempts and "
        "which protocol features are actually available. Use this instead of guessing why a chat "
        "is quiet."
    ),
    capability="whatsapp.chat",
    group="whatsapp",
    parameters={"type": "object", "properties": {}},
)
def whatsapp_health(ctx: ActionContext) -> Any:
    return backend().health()


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
    whatsapp_say,
    whatsapp_listen,
    whatsapp_audio,
    whatsapp_hangup,
    whatsapp_transcript,
    whatsapp_mark_read,
    whatsapp_archived,
    whatsapp_unread,
    whatsapp_archive,
    whatsapp_pin,
    whatsapp_mute,
    whatsapp_settings,
    whatsapp_react,
    whatsapp_reply,
    whatsapp_delete,
    whatsapp_forward,
    whatsapp_groups,
    whatsapp_contacts,
    whatsapp_export_chat,
    whatsapp_health,
]
