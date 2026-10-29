"""A connected mailbox, presented as a channel the reply desk already understands.

The reply desk was written against a messaging channel: something that can list recent inbound
messages and send a reply to a conversation. A mailbox has exactly those two abilities, plus one
extra that matters — a **subject line**, because an email reply that does not thread is a new
conversation in the customer's inbox, not an answer.

So this module is a thin, honest adapter rather than a second desk:

* ``inbox()`` reads recent messages through the mail connector's declared operations (``search``
  then ``read``), and maps each one onto :class:`ChannelMessage`. The body is truncated: a drafter
  needs enough to answer, not an entire newsletter, and a 400 KB email would blow a model's context
  for no gain.
* ``chat`` is the sender's *address*, because that is where a reply goes. The original subject is
  carried alongside so :meth:`send_reply` can build ``Re: <subject>``.
* Nothing here sends on its own. The only caller is an approved draft (``reply_approve``, which is
  owner-only), so the "an agent prepares, the owner sends" rule stays in one place.

Message ids are namespaced by folder (``INBOX:4123``), because two IMAP folders can hand out the
same numeric uid and the reply desk's uniqueness constraint is what prevents a customer being
answered twice.
"""

from __future__ import annotations

from email.utils import parsedate_to_datetime
from typing import Any

from ..channels.base import ChannelMessage

#: Provider error text is already customer-facing; these failures just say which step it was.
SUBJECT_MAX = 200
BODY_CHARS = 1500


class MailboxSource:
    """One connected mailbox, viewed through the reply desk's channel contract."""

    name = "mail"

    def __init__(
        self,
        connector: Any,
        folder: str = "INBOX",
        unseen_only: bool = True,
        body_chars: int = BODY_CHARS,
    ):
        self.connector = connector
        self.folder = folder or "INBOX"
        self.unseen_only = bool(unseen_only)
        self.body_chars = max(200, int(body_chars))
        #: Messages that were listed but could not be read, with the reason. Reported by the
        #: workflow, because "we collected 4" while 2 were unreadable is a half-truth.
        self.skipped: list[dict[str, str]] = []

    # -- reading ----------------------------------------------------------------------
    def inbox(self, limit: int = 20, chat: str = "") -> list[ChannelMessage]:
        """Recent inbound messages. ``chat`` filters to one sender (useful for a thread)."""
        listing = self.connector.call(
            "search",
            folder=self.folder,
            unseen_only=self.unseen_only,
            sender=chat,
            limit=limit,
        )
        messages: list[ChannelMessage] = []
        self.skipped = []
        for row in listing.get("messages", []) or []:
            message_id = str(row.get("id", "")).strip()
            if not message_id:
                continue
            try:
                detail = self.connector.call("read", message_id=message_id, folder=self.folder)
            except Exception as exc:  # noqa: BLE001 - one bad message must not stop the batch
                # Not silent: the message is counted with the provider's reason, and the workflow
                # reports it. Turning it into an empty draft would be worse — an empty draft can
                # be approved, and then the customer is answered with nothing.
                self.skipped.append({"message_id": f"{self.folder}:{message_id}", "reason": str(exc)[:300]})
                continue
            body = str(detail.get("body", "") or "").strip()
            messages.append(
                ChannelMessage(
                    channel=self.name,
                    chat=address_of(row.get("from", "")),
                    text=body[: self.body_chars],
                    message_id=f"{self.folder}:{message_id}",
                    sender=str(row.get("from", "")),
                    ts=timestamp_of(row.get("date", "")),
                    subject=str(detail.get("subject", "") or row.get("subject", ""))[:SUBJECT_MAX],
                )
            )
        return messages

    # -- sending ----------------------------------------------------------------------
    def send(self, chat: str, text: str, subject: str = "") -> dict[str, Any]:
        """Plain send, used when a caller has no thread to reply into."""
        return self.connector.call(
            "send",
            to=chat,
            subject=subject or "Message from ZYNTHARO",
            body=text,
        )

    def send_reply(self, draft: dict[str, Any]) -> dict[str, Any]:
        """Reply into the original thread, using the stored subject.

        A draft row carries ``chat`` (the recipient) and ``subject`` (the original). Without the
        subject we say so in the subject line rather than inventing a thread that does not exist.
        """
        subject = str(draft.get("subject", "") or "").strip()
        reply_subject = f"Re: {subject}" if subject else "Re: your message"
        return self.connector.call(
            "send",
            to=str(draft.get("chat", "")),
            subject=reply_subject[:SUBJECT_MAX],
            body=str(draft.get("reply", "")),
        )


def address_of(raw: str) -> str:
    """``"Dana Reed <dana@example.com>"`` → ``dana@example.com``.

    The reply desk sends to ``chat``, and a display name in that field produces an invalid
    recipient; a bare address is what SMTP actually needs.
    """
    text = str(raw or "").strip()
    if "<" in text and ">" in text:
        inner = text[text.rfind("<") + 1 : text.rfind(">")].strip()
        if inner:
            return inner
    return text


def timestamp_of(raw: str) -> float:
    """Parse an RFC 5322 date. An unparseable date becomes 0 rather than "now".

    "Now" would be a fabricated fact: a message from last week would look like it arrived during
    this batch, and any ordering or "answered within an hour" claim built on it would be wrong.
    """
    try:
        parsed = parsedate_to_datetime(str(raw))
    except (TypeError, ValueError, IndexError):
        return 0.0
    if parsed is None:
        return 0.0
    try:
        return parsed.timestamp()
    except (OverflowError, OSError, ValueError):
        return 0.0


def source_for(connectors: Any, name: str = "mail", **options: Any) -> MailboxSource:
    """Build the adapter from a connector registry, refusing with the connector's own reason.

    The registry's ``get`` raises for an unknown connector and the connector's ``status`` explains
    a missing credential, so a customer without a mailbox connected gets that sentence here
    instead of a workflow that "collected 0 messages" and looked successful.
    """
    if connectors is None:
        raise RuntimeError(
            "no connector registry is attached, so there is no mailbox to read. Start the app "
            "normally (zyntharo hud) or connect a mailbox first."
        )
    connector = connectors.get(name)
    status = connector.status()
    if status.state != "ready":
        raise RuntimeError(f"{connector.title} is not ready ({status.state}): {status.detail}")
    return MailboxSource(connector, **options)
