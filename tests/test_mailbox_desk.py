"""The reply desk on a real mailbox: threading, dedupe, and the approval gate.

What would break a customer's trust here:

* a reply that arrives as a **new** email instead of a thread, because the subject was dropped;
* the same message answered twice, because the IMAP uid was not namespaced by folder;
* an email sent without the owner's approval — the whole reason the desk exists;
* an upgrade that silently loses drafts waiting for approval.

The connector is faked at its own boundary (``call``), so these tests assert the adapter and the
desk, not HTTP.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from zyntharo.channels.base import ChannelMessage
from zyntharo.workflows.base import DONE
from zyntharo.workflows.mailbox import MailboxSource, address_of, source_for, timestamp_of
from zyntharo.workflows.reply_desk import PENDING, SENT, ReplyDesk, WorkflowError


class FakeMailbox:
    """A mailbox at the operation boundary: the two operations the desk actually uses."""

    def __init__(self, messages=None, fail_read=()):
        self.messages = list(messages or [])
        self.fail_read = set(fail_read)
        self.sent: list[dict] = []
        self.searches: list[dict] = []

    def call(self, operation: str, **args):
        if operation == "search":
            self.searches.append(args)
            return {"folder": args.get("folder"), "count": len(self.messages), "messages": self.messages}
        if operation == "read":
            message_id = str(args.get("message_id"))
            if message_id in self.fail_read:
                raise RuntimeError("the message was deleted between the listing and the fetch")
            message = next(m for m in self.messages if str(m["id"]) == message_id)
            return {**message, "body": message.get("body", ""), "attachments": []}
        if operation == "send":
            self.sent.append(args)
            return {"to": args.get("to"), "subject": args.get("subject"), "chars": len(args.get("body", ""))}
        raise AssertionError(f"unexpected operation {operation}")


MESSAGES = [
    {
        "id": "4123",
        "from": "Dana Reed <dana@example.com>",
        "subject": "Invoice 2026-03",
        "date": "Mon, 26 Oct 2026 09:12:00 +0000",
        "body": "Hello, invoice 2026-03 is still unpaid. Could you confirm?",
    },
    {
        "id": "4124",
        "from": "sam@example.org",
        "subject": "Change of address",
        "date": "not a date",
        "body": "Please update the address on our account.",
    },
]


class MailboxAdapterTests(unittest.TestCase):
    def test_messages_become_channel_messages_with_a_bare_address_and_subject(self):
        source = MailboxSource(FakeMailbox(MESSAGES))
        messages = source.inbox(limit=10)
        self.assertEqual(len(messages), 2)
        first = messages[0]
        self.assertIsInstance(first, ChannelMessage)
        self.assertEqual(first.channel, "mail")
        self.assertEqual(first.chat, "dana@example.com")
        self.assertEqual(first.subject, "Invoice 2026-03")
        # The uid is namespaced by folder: two folders can hand out the same number.
        self.assertEqual(first.message_id, "INBOX:4123")
        # A real date parses to a real epoch second (the exact value is the RFC 5322 parser's
        # business, not this test's); what matters is that it is not zero and not "now".
        self.assertGreater(first.ts, 1_700_000_000)

    def test_an_unparseable_date_is_zero_not_now(self):
        """"Now" would make a week-old message look like it arrived in this batch."""
        self.assertEqual(timestamp_of("not a date"), 0.0)
        self.assertEqual(timestamp_of(""), 0.0)

    def test_a_body_is_truncated_so_one_newsletter_cannot_fill_the_context(self):
        big = [{"id": "1", "from": "a@b.co", "subject": "Long", "date": "", "body": "x" * 5000}]
        source = MailboxSource(FakeMailbox(big), body_chars=300)
        self.assertEqual(len(source.inbox(limit=1)[0].text), 300)

    def test_one_unreadable_message_does_not_stop_the_batch(self):
        source = MailboxSource(FakeMailbox(MESSAGES, fail_read={"4123"}))
        messages = source.inbox(limit=10)
        self.assertEqual([m.message_id for m in messages], ["INBOX:4124"])

    def test_send_reply_threads_the_stored_subject(self):
        mailbox = FakeMailbox(MESSAGES)
        source = MailboxSource(mailbox)
        delivered = source.send_reply({"chat": "dana@example.com", "subject": "Invoice 2026-03", "reply": "Confirmed."})
        self.assertEqual(mailbox.sent[0]["to"], "dana@example.com")
        self.assertEqual(mailbox.sent[0]["subject"], "Re: Invoice 2026-03")
        self.assertEqual(delivered["subject"], "Re: Invoice 2026-03")

    def test_a_draft_without_a_subject_says_so_instead_of_inventing_a_thread(self):
        mailbox = FakeMailbox(MESSAGES)
        MailboxSource(mailbox).send_reply({"chat": "x@y.z", "subject": "", "reply": "Hi"})
        self.assertEqual(mailbox.sent[0]["subject"], "Re: your message")

    def test_address_of_handles_a_display_name_and_a_bare_address(self):
        self.assertEqual(address_of("Dana Reed <dana@example.com>"), "dana@example.com")
        self.assertEqual(address_of("dana@example.com"), "dana@example.com")
        self.assertEqual(address_of('"Reed, Dana" <dana@example.com>'), "dana@example.com")

    def test_an_unconnected_mailbox_is_refused_with_the_connectors_own_reason(self):
        class Status:
            state = "needs_setup"
            detail = "No mailbox connected. Connect one in Settings → Connectors; it needs only an address and an app password."

        class Connector:
            title = "Mailbox (IMAP/SMTP)"

            def status(self):
                return Status()

        class Registry:
            def get(self, name):
                return Connector()

        with self.assertRaises(RuntimeError) as caught:
            source_for(Registry())
        self.assertIn("needs_setup", str(caught.exception))
        self.assertIn("app password", str(caught.exception))


class ReplyDeskOnMailTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.desk = ReplyDesk(Path(self.dir) / "reply.db")
        self.mailbox = FakeMailbox(MESSAGES)
        self.source = MailboxSource(self.mailbox)

    def _drafter(self, row):
        return f"Thanks — re: {row['subject']}, we are on it."

    def test_collect_then_draft_then_approve_sends_exactly_one_threaded_reply(self):
        result = self.desk.run(self.source, drafter=self._drafter, limit=10)
        self.assertEqual(result.status, DONE)
        self.assertEqual(result.counts["collected"], 2)
        self.assertEqual(result.counts["drafted"], 2)
        self.assertIn("waiting for your approval", result.summary)
        # Nothing has left the building yet.
        self.assertEqual(self.mailbox.sent, [])

        pending = self.desk.pending()
        self.assertEqual([d["status"] for d in pending], [PENDING, PENDING])
        self.assertEqual(pending[0]["subject"], "Invoice 2026-03")

        approved = self.desk.approve(pending[0]["id"], MailboxSource(self.mailbox))
        self.assertEqual(approved["status"], SENT)
        self.assertEqual(len(self.mailbox.sent), 1)
        self.assertEqual(self.mailbox.sent[0]["subject"], "Re: Invoice 2026-03")
        self.assertEqual(self.mailbox.sent[0]["to"], "dana@example.com")

        # Approving the same draft twice must not send a second copy.
        with self.assertRaises(WorkflowError) as caught:
            self.desk.approve(pending[0]["id"], MailboxSource(self.mailbox))
        self.assertIn("not waiting for approval", str(caught.exception))
        self.assertEqual(len(self.mailbox.sent), 1)

    def test_an_unreadable_message_is_reported_not_hidden(self):
        """"Collected 1" while a second message was unreadable is a half-truth."""
        source = MailboxSource(FakeMailbox(MESSAGES, fail_read={"4123"}))
        result = self.desk.run(source, drafter=self._drafter, limit=10)
        self.assertEqual(result.counts["collected"], 1)
        self.assertTrue(result.problems)
        self.assertIn("unreadable", result.problems[0])
        self.assertTrue(any(step.name == "collect.unreadable" for step in result.steps))

    def test_collecting_twice_does_not_answer_the_same_message_twice(self):
        self.desk.run(self.source, drafter=self._drafter, limit=10)
        again = self.desk.run(self.source, drafter=self._drafter, limit=10)
        self.assertEqual(again.counts["collected"], 0)
        self.assertEqual(len(self.desk.pending()), 2)
        self.assertEqual(self.mailbox.sent, [])

    def test_a_draft_may_only_be_sent_through_its_own_source(self):
        self.desk.run(self.source, drafter=self._drafter, limit=10)
        draft = self.desk.pending()[0]

        class OtherChannel:
            name = "telegram"

            def send(self, chat, text):  # pragma: no cover - must never be reached
                raise AssertionError("a mail draft must not leave through another platform")

        with self.assertRaises(WorkflowError) as caught:
            self.desk.approve(draft["id"], OtherChannel())
        self.assertIn("wrong place", str(caught.exception))
        self.assertEqual(self.mailbox.sent, [])

    def test_an_upgrade_keeps_pending_drafts_and_adds_the_subject_column(self):
        """A customer's desk already has drafts in it; a column must not cost them that."""
        path = Path(tempfile.mkdtemp()) / "old.db"
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE inbound (
                id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT NOT NULL, chat TEXT NOT NULL,
                message_id TEXT NOT NULL, sender TEXT DEFAULT '', text TEXT NOT NULL,
                ts REAL DEFAULT 0, collected_at REAL DEFAULT 0, UNIQUE(channel, message_id)
            );
            CREATE TABLE drafts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, inbound_id INTEGER NOT NULL UNIQUE,
                channel TEXT NOT NULL, chat TEXT NOT NULL, reply TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending_approval', created_at REAL DEFAULT 0,
                decided_at REAL DEFAULT 0, error TEXT DEFAULT ''
            );
            INSERT INTO inbound (channel, chat, message_id, sender, text, ts, collected_at)
                VALUES ('telegram', '42', 'm1', 'dana', 'old message', 0, 0);
            INSERT INTO drafts (inbound_id, channel, chat, reply, status, created_at)
                VALUES (1, 'telegram', '42', 'old draft', 'pending_approval', 0);
            """
        )
        conn.commit()
        conn.close()

        desk = ReplyDesk(path)
        self.assertEqual(len(desk.pending()), 1)
        columns = {row[1] for row in sqlite3.connect(path).execute("PRAGMA table_info(drafts)")}
        self.assertIn("subject", columns)
        self.assertEqual(desk.pending()[0]["subject"], "")


if __name__ == "__main__":
    unittest.main()