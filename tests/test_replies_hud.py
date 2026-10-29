"""The replies panel: the owner's end of the reply desk.

Three behaviours are asserted, each because the naive version is wrong in a way a customer would
feel:

* **A draft is a proposal.** Sending happens only on ``reply.approve``, once, and never through a
  different source than the one the draft came from.
* **An edit is not a send.** Saving changed text must leave the draft pending for approval.
* **Nothing here reads the network while painting.** Opening the panel cannot mark mail as read.

The source is faked at the channel contract (``inbox``/``send``) and the model is the offline echo
provider, so no network and no desktop are involved.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from zyntharo.channels.base import ChannelMessage
from zyntharo.hud.service import HudService
from zyntharo.workflows.reply_desk import PENDING, SENT, ReplyDesk
from tests.helpers import Stack


class FakeSource:
    name = "fake"

    def __init__(self, messages):
        self.messages = list(messages)
        self.sent: list[tuple[str, str]] = []

    def inbox(self, limit: int = 20, chat: str = ""):
        unseen = self.messages[:limit]
        self.messages = self.messages[limit:]
        return unseen

    def send(self, chat: str, text: str):
        self.sent.append((chat, text))
        return {"chat": chat, "chars": len(text)}


def message(index: int, sender: str = "dana@example.com", subject: str = "Invoice") -> ChannelMessage:
    return ChannelMessage(
        channel="fake",
        chat=sender,
        text=f"message {index}",
        message_id=f"m{index}",
        sender=sender,
        subject=subject,
    )


class RepliesPanelTests(unittest.TestCase):
    def setUp(self):
        self.stack = Stack()
        self.desk = ReplyDesk(self.stack.config.home_path / "reply_panel.db")
        self.service = HudService(
            config=self.stack.config,
            bus=self.stack.bus,
            memory=self.stack.memory,
            registry=self.stack.actions,
            runtime=self.stack.runtime,
            quotas=self.stack.quotas,
            orchestrator=self.stack.orchestrator,
            plugins=self.stack.plugins,
            replies=self.desk,
        )
        self.source = FakeSource([message(1), message(2, subject="Second")])
        # The delivery is faked at the seam the panel uses, so these tests assert the desk and the
        # commands rather than a real mailbox.
        self.service._reply_delivery = lambda name: self.source  # noqa: SLF001 - test seam

    def tearDown(self):
        self.stack.close()

    # -- snapshot ----------------------------------------------------------------------
    def test_the_snapshot_lists_pending_drafts_and_their_sources(self):
        snapshot = self.service.snapshot()["replies"]
        self.assertTrue(snapshot["attached"])
        self.assertEqual(snapshot["pending"], [])
        self.assertEqual(snapshot["counts"]["pending"], 0)
        self.assertIsInstance(snapshot["sources"], list)

    def test_a_build_without_a_desk_says_so(self):
        service = HudService(
            config=self.stack.config,
            bus=self.stack.bus,
            memory=self.stack.memory,
            registry=self.stack.actions,
            runtime=self.stack.runtime,
            quotas=self.stack.quotas,
            orchestrator=self.stack.orchestrator,
            plugins=self.stack.plugins,
        )
        snapshot = service.snapshot()["replies"]
        self.assertFalse(snapshot["attached"])
        self.assertIn("not attached", snapshot["note"])
        # Reading is a query, so it answers honestly instead of raising; commands that would act
        # on a draft do raise, because there is nothing to act on.
        self.assertFalse(service.route("reply.pending", {}, None)["attached"])
        with self.assertRaises(ValueError):
            service.route("reply.check", {"channel": "mail"}, None)

    # -- drafting ----------------------------------------------------------------------
    def test_check_drafts_with_the_fleets_model_and_sends_nothing(self):
        result = self.service.route("reply.check", {"channel": "fake", "limit": 5}, None)
        self.assertEqual(result["counts"]["collected"], 2)
        self.assertEqual(result["counts"]["drafted"], 2)
        self.assertIn("model", result["drafted_with"])
        self.assertEqual(self.source.sent, [])
        pending = self.desk.pending()
        self.assertEqual(len(pending), 2)
        self.assertTrue(pending[0]["reply"].strip())
        self.assertEqual(pending[0]["subject"], "Invoice")

    def test_one_press_cannot_draft_the_whole_mailbox(self):
        self.source.messages = [message(i) for i in range(1, 9)]
        result = self.service.route("reply.check", {"channel": "fake", "limit": 3}, None)
        self.assertEqual(result["counts"]["collected"], 3)
        self.assertEqual(len(self.desk.pending()), 3)

    def test_a_refused_source_is_reported_as_a_message_not_a_crash(self):
        from zyntharo.workflows import mailbox

        def refuse(connectors, name="mail", **options):
            raise RuntimeError("Mailbox (IMAP/SMTP) is not ready (needs_setup): No mailbox connected.")

        original = mailbox.source_for
        mailbox.source_for = refuse
        try:
            service = HudService(
                config=self.stack.config,
                bus=self.stack.bus,
                memory=self.stack.memory,
                registry=self.stack.actions,
                runtime=self.stack.runtime,
                quotas=self.stack.quotas,
                orchestrator=self.stack.orchestrator,
                plugins=self.stack.plugins,
                replies=self.desk,
                connectors=object(),
            )
            with self.assertRaises(ValueError) as caught:
                service.route("reply.check", {"channel": "mail"}, None)
            self.assertIn("needs_setup", str(caught.exception))
            self.assertIn("No mailbox connected", str(caught.exception))
        finally:
            mailbox.source_for = original

    # -- the owner's decisions ---------------------------------------------------------
    def test_editing_a_draft_does_not_send_it(self):
        self.service.route("reply.check", {"channel": "fake", "limit": 2}, None)
        draft = self.desk.pending()[0]
        saved = self.service.route("reply.edit", {"draft_id": draft["id"], "reply": "Short and clear."}, None)
        self.assertEqual(saved["status"], PENDING)
        self.assertEqual(saved["reply"], "Short and clear.")
        self.assertEqual(self.desk.pending()[0]["reply"], "Short and clear.")
        self.assertEqual(self.source.sent, [])

    def test_an_empty_edit_is_refused_so_a_blank_reply_cannot_be_sent(self):
        self.service.route("reply.check", {"channel": "fake", "limit": 1}, None)
        draft = self.desk.pending()[0]
        with self.assertRaises(ValueError) as caught:
            self.service.route("reply.edit", {"draft_id": draft["id"], "reply": "   "}, None)
        self.assertIn("cannot be empty", str(caught.exception))
        self.assertEqual(self.desk.pending()[0]["reply"], draft["reply"])

    def test_approving_sends_once_and_then_refuses_a_second_send(self):
        self.service.route("reply.check", {"channel": "fake", "limit": 1}, None)
        draft = self.desk.pending()[0]
        result = self.service.route("reply.approve", {"draft_id": draft["id"]}, None)
        self.assertEqual(result["status"], SENT)
        self.assertEqual(len(self.source.sent), 1)
        self.assertEqual(self.source.sent[0][0], "dana@example.com")
        with self.assertRaises(ValueError) as caught:
            self.service.route("reply.approve", {"draft_id": draft["id"]}, None)
        self.assertIn("not waiting for approval", str(caught.exception))
        self.assertEqual(len(self.source.sent), 1)

    def test_discarding_a_draft_removes_it_from_the_queue(self):
        self.service.route("reply.check", {"channel": "fake", "limit": 2}, None)
        draft = self.desk.pending()[0]
        result = self.service.route("reply.reject", {"draft_id": draft["id"], "reason": "wrong customer"}, None)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual([row["id"] for row in self.desk.pending()], [self.desk.pending()[0]["id"]])
        with self.assertRaises(ValueError):
            self.service.route("reply.approve", {"draft_id": draft["id"]}, None)
        self.assertEqual(self.source.sent, [])

    def test_a_missing_draft_id_is_refused_by_every_decision_command(self):
        for command in ("reply.edit", "reply.approve", "reply.reject"):
            with self.assertRaises(ValueError) as caught:
                self.service.route(command, {}, None)
            self.assertIn("draft id", str(caught.exception))

    def test_an_unknown_draft_id_is_refused_without_touching_the_source(self):
        with self.assertRaises(ValueError) as caught:
            self.service.route("reply.approve", {"draft_id": 999}, None)
        self.assertIn("not waiting", str(caught.exception))
        self.assertEqual(self.source.sent, [])


class RepliesPanelInterfaceTests(unittest.TestCase):
    def test_the_panel_has_its_controls_and_uses_the_bridge(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "hud", "index.html"), encoding="utf-8") as handle:
            html = handle.read()
        with open(os.path.join(root, "hud", "app.js"), encoding="utf-8") as handle:
            app = handle.read()
        for fragment in ('data-tab="replies"', 'data-panel="replies"', 'id="replies"', 'id="reply-source"'):
            self.assertIn(fragment, html)
        for command in ("reply.check", "reply.edit", "reply.approve", "reply.reject"):
            self.assertIn(command, app)
        # The inbound message must stay visible next to the proposal.
        self.assertIn("inbound_text", app)
        self.assertIn("reply-edit", app)


if __name__ == "__main__":
    unittest.main()