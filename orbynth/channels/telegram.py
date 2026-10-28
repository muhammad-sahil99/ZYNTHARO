"""Telegram over the official Bot API.

Why this platform first: it is the only major messaging network that gives a normal business a
free, documented, terms-compliant API for both sending and receiving messages without an app
review, a phone farm or a web session. That makes it the honest second channel after WhatsApp,
and the one a customer can switch on in two minutes.

Nothing here fabricates a result. A missing token is ``needs_setup``; a refused call carries
Telegram's own description; a webhook conflict is named rather than retried forever.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from .base import CONFIGURED, Channel, ChannelError, ChannelMessage, ChannelStatus


class TelegramChannel(Channel):
    name = "telegram"

    def __init__(
        self,
        token: str = "",
        api_base: str = "https://api.telegram.org",
        timeout: float = 15.0,
        offset_path: str = "",
    ):
        self.token = token or os.environ.get("ORBYNTH_TELEGRAM_TOKEN", "") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout
        self.offset_path = offset_path
        self._offset = self._load_offset()
        self._last_ok = False
        self._last_error = ""

    # -- transport -------------------------------------------------------------------
    def _call(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        if not self.token:
            raise ChannelError("no Telegram bot token is configured")
        url = f"{self.api_base}/bot{self.token}/{method}"
        body = json.dumps(payload or {}).encode("utf-8")
        request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            # Telegram explains the problem in the body; losing it would turn "chat not found"
            # into an anonymous HTTP 400.
            try:
                data = json.loads(exc.read().decode("utf-8") or "{}")
            except (ValueError, OSError):
                data = {}
            description = data.get("description") or f"HTTP {exc.code}"
            self._last_error = description
            raise ChannelError(f"telegram {method} failed: {description}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            self._last_error = str(exc)
            raise ChannelError(f"telegram {method} could not reach {self.api_base}: {exc}") from exc
        if not data.get("ok", False):
            description = data.get("description") or "the API refused the request"
            self._last_error = description
            raise ChannelError(f"telegram {method} refused: {description}")
        self._last_ok = True
        self._last_error = ""
        return data.get("result")

    # -- contract --------------------------------------------------------------------
    def status(self) -> ChannelStatus:
        if not self.token:
            return self._needs_setup(
                "Create a bot with @BotFather, then set ORBYNTH_TELEGRAM_TOKEN (Sites/Secrets or "
                "the app's settings). No phone number or QR code is required."
            )
        if self._last_ok:
            return self._ready("last Bot API call succeeded")
        detail = "token configured; not exercised yet"
        if self._last_error:
            detail = f"token configured; last error: {self._last_error}"
        return ChannelStatus(self.name, CONFIGURED, detail, self.kind)

    def send(self, chat: str, text: str) -> dict[str, Any]:
        if not str(chat).strip():
            raise ChannelError("a Telegram chat id or @channel name is required")
        if not str(text).strip():
            raise ChannelError("refusing to send an empty message")
        result = self._call("sendMessage", {"chat_id": chat, "text": text})
        return {
            "channel": self.name,
            "chat": str(result.get("chat", {}).get("id", chat)),
            "message_id": str(result.get("message_id", "")),
            "chars": len(text),
        }

    def inbox(self, limit: int = 20, chat: str = "") -> list[ChannelMessage]:
        """Recent messages by polling. Telegram delivers each update once per offset."""
        if not self.token:
            raise ChannelError("no Telegram bot token is configured")
        result = self._call("getUpdates", {"timeout": 0, "limit": max(1, min(int(limit), 100)), "offset": self._offset or None})
        messages: list[ChannelMessage] = []
        for update in result or []:
            self._offset = max(self._offset, int(update.get("update_id", 0)) + 1)
            message = update.get("message") or update.get("edited_message") or {}
            text = message.get("text") or message.get("caption") or ""
            if not text:
                continue
            sender = message.get("from") or {}
            chat_id = str((message.get("chat") or {}).get("id", ""))
            if chat and str(chat) != chat_id:
                continue
            messages.append(
                ChannelMessage(
                    channel=self.name,
                    chat=chat_id,
                    text=text,
                    message_id=str(message.get("message_id", "")),
                    sender=str(sender.get("username") or sender.get("first_name") or sender.get("id") or ""),
                    ts=float(message.get("date", 0) or 0),
                )
            )
        self._save_offset()
        return messages

    # -- offset persistence ----------------------------------------------------------
    def _load_offset(self) -> int:
        if not self.offset_path or not os.path.exists(self.offset_path):
            return 0
        try:
            with open(self.offset_path, "r", encoding="utf-8") as handle:
                return int(json.load(handle).get("offset", 0))
        except (OSError, ValueError, TypeError):
            return 0

    def _save_offset(self) -> None:
        if not self.offset_path:
            return
        try:
            os.makedirs(os.path.dirname(self.offset_path), exist_ok=True)
            tmp = f"{self.offset_path}.tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump({"offset": self._offset}, handle)
            os.replace(tmp, self.offset_path)
        except OSError:
            # Losing the offset can duplicate a message in the inbox; it must never lose a
            # reply that was already sent, so this is reported rather than raised.
            self._last_error = "could not persist the Telegram offset"