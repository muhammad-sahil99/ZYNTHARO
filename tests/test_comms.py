"""Inter-agent comms (spec: fleet messaging) — threads, help requests, file handoffs.

What these tests protect is the behaviour an agent depends on at 3am:

* a question reaches the right agent even when addressed by role rather than by name;
* a request stays *open* until answered, and cannot be answered twice into two conflicting
  answers;
* a handed-over file is a copy with a real digest, so the sender deleting its working
  directory does not destroy the recipient's evidence;
* an unknown recipient fails loudly and lists who does exist, instead of silently
  delivering to nobody.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from zyntharo.agents.comms import AgentComms
from zyntharo.events import AGENT_HELP_ANSWERED, AGENT_HELP_REQUEST, EventBus


class CommsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bus = EventBus()
        self.events: list[str] = []
        self.bus.subscribe("*", lambda event: self.events.append(event.topic))
        self.roster = [
            {"agent_id": "sove-1", "name": "Sovereign", "role": "sovereign", "rank": 0},
            {"agent_id": "oper-1", "name": "Operative", "role": "operative", "rank": 5},
            {"agent_id": "cart-1", "name": "Cartographer", "role": "cartographer", "rank": 4},
        ]
        self.comms = AgentComms(
            os.path.join(self.tmp.name, "comms.db"),
            self.bus,
            roster=lambda: self.roster,
        )
        self.addCleanup(self.comms.close)

    def test_a_request_reaches_the_standing_specialist_by_role(self):
        message = self.comms.send(
            "oper-1", "Operative", "role:cartographer", "How do I scroll a shadow-DOM list?", kind="request"
        )
        self.assertEqual(message.recipient, "role:cartographer")
        inbox = self.comms.inbox("cart-1")
        self.assertEqual([m.msg_id for m in inbox], [message.msg_id])
        # The role was resolved to the agent actually holding it.
        self.assertEqual(self.comms.stats("cart-1")["unread_for_me"], 1)

    def test_request_stays_open_until_answered_then_closes(self):
        request = self.comms.send("oper-1", "Operative", "Sovereign", "Which model for OCR?", kind="request")
        self.assertEqual(len(self.comms.open_requests()), 1)
        self.comms.answer(request.msg_id, "sove-1", "Sovereign", "Use the vision-tagged one.")
        self.assertEqual(self.comms.open_requests(), [])
        self.assertIn(AGENT_HELP_REQUEST, self.events)
        self.assertIn(AGENT_HELP_ANSWERED, self.events)

    def test_answering_twice_is_refused_not_forked(self):
        request = self.comms.send("oper-1", "Operative", "Sovereign", "Which model?", kind="request")
        self.comms.answer(request.msg_id, "sove-1", "Sovereign", "first answer")
        with self.assertRaises(ValueError) as caught:
            self.comms.answer(request.msg_id, "cart-1", "Cartographer", "second answer")
        self.assertIn("already answered", str(caught.exception))
        # Exactly one answer exists in the thread: no contradictory pair.
        thread = self.comms.thread(request.thread_id)
        self.assertEqual([m.kind for m in thread], ["request", "answer"])

    def test_unknown_recipient_fails_and_names_the_real_agents(self):
        with self.assertRaises(ValueError) as caught:
            self.comms.send("oper-1", "Operative", "nobody", "hello")
        message = str(caught.exception)
        self.assertIn("unknown recipient", message)
        self.assertIn("Cartographer", message)

    def test_broadcast_reaches_every_registered_agent(self):
        self.comms.send("sove-1", "Sovereign", "*", "Fleet: quota is tight, keep reports short.", kind="notice")
        for agent_id in ("oper-1", "cart-1"):
            self.assertEqual(len(self.comms.inbox(agent_id)), 1, f"{agent_id} did not receive the broadcast")

    def test_attachment_is_copied_with_a_verified_digest(self):
        source = os.path.join(self.tmp.name, "page.html")
        with open(source, "w", encoding="utf-8") as handle:
            handle.write("<html><body>evidence</body></html>")
        message = self.comms.send(
            "cart-1",
            "Cartographer",
            "Sovereign",
            "Here is the DOM snapshot.",
            kind="file",
            attachments=self.comms.stage_files([source], note="dom"),
        )
        stored = message.attachments[0]
        self.assertEqual(len(stored["sha256"]), 64)
        # The sender's original can vanish; the handoff must survive it.
        os.unlink(source)
        self.assertTrue(os.path.exists(stored["path"]))
        self.assertEqual(len(self.comms.inbox("sove-1")[0].attachments), 1)

    def test_directories_are_refused_with_a_usable_reason(self):
        with self.assertRaises(ValueError) as caught:
            self.comms.stage_files([self.tmp.name])
        self.assertIn("zip it first", str(caught.exception))

    def test_messages_and_threads_survive_a_restart(self):
        original = self.comms.send("oper-1", "Operative", "Sovereign", "Persist me", kind="request")
        path = self.comms.path
        self.comms.close()
        reopened = AgentComms(path, self.bus, roster=lambda: self.roster)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.thread(original.thread_id)[0].body, "Persist me")
        self.assertEqual(len(reopened.open_requests()), 1)

    def test_mark_read_only_clears_what_it_was_given(self):
        first = self.comms.send("oper-1", "Operative", "Sovereign", "one")
        self.comms.send("oper-1", "Operative", "Sovereign", "two")
        self.assertEqual(self.comms.mark_read("sove-1", [first.msg_id]), 1)
        self.assertEqual(self.comms.stats("sove-1")["unread_for_me"], 1)

    def test_overlong_body_is_refused_instead_of_truncated(self):
        with self.assertRaises(ValueError) as caught:
            self.comms.send("oper-1", "Operative", "Sovereign", "x" * 30_000)
        self.assertIn("limit", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
