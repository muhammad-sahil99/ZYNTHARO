"""The sellable surface: three workflows and the platforms behind them.

These are integration tests on purpose. The invoice workflow touches real files and a real xlsx
package; the Telegram adapter talks to a real local HTTP server over a real socket. Mocking our own
transport would prove nothing about whether a customer's machine can send a message.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from zyntharo.channels import ChannelError, ChannelRegistry, default_registry
from zyntharo.channels.base import Channel, ChannelMessage, ChannelStatus, UNAVAILABLE
from zyntharo.channels.telegram import TelegramChannel
from zyntharo.workflows import DailyReport, InvoiceIntake, ReplyDesk, WorkflowError
from zyntharo.ops import docengine


def write(path: str, text: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


class InvoiceIntakeTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="zyntharo-inv-")
        self.source = os.path.join(self.home, "inbox")
        self.dest = os.path.join(self.home, "filed")
        self.ledger = os.path.join(self.home, "ledger.xlsx")
        os.makedirs(self.source, exist_ok=True)
        write(os.path.join(self.source, "acme.txt"), "ACME Supplies\nInvoice 22\nDate 2026-02-14\nTotal: $1,234.50\n")
        write(os.path.join(self.source, "northwind.md"), "Northwind Ltd\nDate 05/11/2025\nAmount due EUR 320,00\n")

    def test_files_are_filed_by_quarter_and_recorded_once(self):
        result = InvoiceIntake().run(source=self.source, dest=self.dest, ledger=self.ledger)
        self.assertEqual(result.status, "done", result.to_json())
        self.assertEqual(result.counts["filed"], 2, result.to_json())
        filed = sorted(
            os.path.relpath(os.path.join(root, name), self.dest)
            for root, _, names in os.walk(self.dest)
            for name in names
        )
        self.assertTrue(any(path.startswith("2026/Q1") for path in filed), filed)
        self.assertTrue(any(path.startswith("2025/Q4") for path in filed), filed)
        rows = docengine.xlsx_read(self.ledger)["rows"]
        self.assertEqual(rows[0][0], "sha256")
        self.assertEqual(len(rows), 3, rows)

    def test_a_second_run_is_idempotent(self):
        InvoiceIntake().run(source=self.source, dest=self.dest, ledger=self.ledger)
        again = InvoiceIntake().run(source=self.source, dest=self.dest, ledger=self.ledger)
        self.assertEqual(again.counts["filed"], 0, again.to_json())
        self.assertEqual(len(docengine.xlsx_read(self.ledger)["rows"]), 3)

    def test_an_unreadable_file_is_skipped_with_its_reason(self):
        open(os.path.join(self.source, "scan.png"), "wb").write(b"\x89PNG\r\n\x1a\n")
        result = InvoiceIntake().run(source=self.source, dest=self.dest, ledger=self.ledger)
        skipped = [step for step in result.steps if step.status == "skipped" and step.name == "scan.png"]
        self.assertEqual(len(skipped), 1)
        self.assertIn("OCR", skipped[0].detail)

    def test_a_foreign_ledger_is_refused_not_appended_to(self):
        docengine.xlsx_write(self.ledger, {"ledger": [["something", "else"], [1, 2]]})
        with self.assertRaises(WorkflowError):
            InvoiceIntake().run(source=self.source, dest=self.dest, ledger=self.ledger)

    def test_dry_run_touches_nothing(self):
        result = InvoiceIntake().run(source=self.source, dest=self.dest, ledger=self.ledger, dry_run=True)
        self.assertEqual(result.status, "done")
        self.assertFalse(os.path.exists(self.dest) and os.listdir(self.dest))
        self.assertFalse(os.path.exists(self.ledger))


class DailyReportTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="zyntharo-rep-")
        self.ledger = os.path.join(self.home, "ledger.xlsx")
        rows = [
            ["sha256", "source", "filed_to", "vendor", "amount", "currency", "invoice_date", "filed_at"],
            ["a", "s1", "f1", "acme", 100.0, "USD", "2026-02-14", "2026-02-14T10:00:00Z"],
            ["b", "s2", "f2", "acme", 50.5, "USD", "2026-02-15", "2026-02-15T10:00:00Z"],
            ["c", "s3", "f3", "northwind", 320.0, "EUR", "2025-11-05", "2025-11-05T10:00:00Z"],
        ]
        docengine.xlsx_write(self.ledger, {"ledger": rows})

    def test_a_verified_word_and_excel_report_is_produced(self):
        out = os.path.join(self.home, "reports")
        result = DailyReport().run(ledger=self.ledger, out_dir=out, title="Invoice summary")
        self.assertEqual(result.status, "done", result.to_json())
        artifacts = result.artifacts
        self.assertTrue(any(path.endswith(".xlsx") for path in artifacts), artifacts)
        docx = next(path for path in artifacts if path.endswith(".docx"))
        text = docengine.docx_text(docx)
        self.assertIn("Invoice summary", text)
        self.assertIn("USD 150.50", text)
        summary = next(path for path in artifacts if path.endswith(".xlsx"))
        sheets = docengine.xlsx_read(summary, "by_vendor")["rows"]
        self.assertIn(["acme", 2], [[row[0], row[1]] for row in sheets], sheets)

    def test_a_ledger_without_amounts_is_refused(self):
        broken = os.path.join(self.home, "broken.xlsx")
        docengine.xlsx_write(broken, {"ledger": [["vendor"], ["acme"]]})
        with self.assertRaises(WorkflowError):
            DailyReport().run(ledger=broken, out_dir=os.path.join(self.home, "r"))


class FakeChannel(Channel):
    name = "fake"

    def __init__(self, messages=None):
        self.messages = list(messages or [])
        self.sent: list[tuple[str, str]] = []

    def status(self):
        return ChannelStatus(self.name, "ready", "test channel")

    def send(self, chat: str, text: str) -> dict[str, Any]:
        self.sent.append((chat, text))
        return {"channel": self.name, "chat": chat, "message_id": str(len(self.sent))}

    def inbox(self, limit: int = 20, chat: str = ""):
        return self.messages[:limit]


class ReplyDeskTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="zyntharo-desk-")
        self.desk = ReplyDesk(os.path.join(self.home, "desk.db"))
        self.channel = FakeChannel([ChannelMessage("fake", "cust-1", "where is my order?", "m-1", "Sam")])

    def test_collecting_without_a_drafter_never_sends(self):
        result = self.desk.run(self.channel)
        self.assertEqual(result.status, "done", result.to_json())
        self.assertEqual(self.channel.sent, [])
        self.assertEqual(self.desk.pending(), [])

    def test_a_draft_waits_for_approval_and_sends_exactly_once(self):
        result = self.desk.run(self.channel, drafter=lambda row: f"Re: {row['text']}")
        self.assertIn("waiting for your approval", result.summary)
        self.assertEqual(self.channel.sent, [], "a draft must never be sent automatically")
        pending = self.desk.pending()
        self.assertEqual(len(pending), 1)
        delivered = self.desk.approve(pending[0]["id"], self.channel)
        self.assertEqual(delivered["status"], "sent")
        self.assertEqual(len(self.channel.sent), 1)
        with self.assertRaises(WorkflowError):
            self.desk.approve(pending[0]["id"], self.channel)

    def test_a_draft_cannot_be_sent_through_the_wrong_platform(self):
        self.desk.run(self.channel, drafter=lambda row: "hello")
        draft_id = self.desk.pending()[0]["id"]
        other = FakeChannel()
        other.name = "telegram"
        with self.assertRaises(WorkflowError):
            self.desk.approve(draft_id, other)
        self.assertEqual(other.sent, [])

    def test_an_inbound_message_is_never_drafted_twice(self):
        self.desk.collect(self.channel)
        self.desk.collect(self.channel)
        self.assertEqual(len(self.desk.undrafted()), 1)
        self.desk.draft(lambda row: "hi")
        self.assertEqual(self.desk.undrafted(), [])

    def test_a_rejected_draft_is_not_sendable(self):
        self.desk.run(self.channel, drafter=lambda row: "hi")
        draft_id = self.desk.pending()[0]["id"]
        self.desk.reject(draft_id, "customer asked for a call")
        self.assertEqual(self.desk.pending(), [])
        with self.assertRaises(WorkflowError):
            self.desk.approve(draft_id, self.channel)


class _BotAPI(BaseHTTPRequestHandler):
    bodies: list[tuple[str, dict]] = []

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler contract
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).bodies.append((self.path, body))
        if self.path.endswith("/botBAD/sendMessage"):
            self._reply(400, {"ok": False, "description": "chat not found"})
        elif self.path.endswith("/getUpdates"):
            self._reply(200, {"ok": True, "result": [{"update_id": 11, "message": {"message_id": 3, "text": "hello", "chat": {"id": 99}, "from": {"username": "sam"}, "date": 1_700_000_000}}]})
        elif self.path.endswith("/sendMessage"):
            self._reply(200, {"ok": True, "result": {"message_id": 7, "chat": {"id": body.get("chat_id")}}})
        else:
            self._reply(404, {"ok": False, "description": "unknown method"})

    def _reply(self, code: int, payload: dict):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # silence the server's own logging
        return


class TelegramChannelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _BotAPI)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        _BotAPI.bodies.clear()
        self.offset = os.path.join(tempfile.mkdtemp(prefix="zyntharo-tg-"), "offset.json")

    def test_without_a_token_it_asks_for_one_instead_of_failing_later(self):
        channel = TelegramChannel(token="")
        status = channel.status()
        self.assertEqual(status.state, "needs_setup")
        self.assertIn("BotFather", status.detail)
        with self.assertRaises(ChannelError):
            channel.send("99", "hi")

    def test_sending_reports_the_platforms_own_message_id(self):
        channel = TelegramChannel(token="TEST", api_base=self.base, offset_path=self.offset)
        result = channel.send("99", "your order shipped")
        self.assertEqual(result["message_id"], "7")
        self.assertEqual(result["chat"], "99")
        self.assertEqual(channel.status().state, "ready")

    def test_a_refusal_keeps_the_platforms_description(self):
        channel = TelegramChannel(token="BAD", api_base=self.base)
        with self.assertRaises(ChannelError) as caught:
            channel.send("99", "hi")
        self.assertIn("chat not found", str(caught.exception))

    def test_inbox_reads_messages_and_remembers_its_offset(self):
        channel = TelegramChannel(token="TEST", api_base=self.base, offset_path=self.offset)
        messages = channel.inbox(limit=5)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].text, "hello")
        self.assertEqual(messages[0].sender, "sam")
        with open(self.offset, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["offset"], 12)
        channel.inbox(limit=5)
        last_body = _BotAPI.bodies[-1][1]
        self.assertEqual(last_body["offset"], 12, "the second poll must not re-deliver update 11")

    def test_an_empty_message_is_refused_before_it_reaches_the_platform(self):
        channel = TelegramChannel(token="TEST", api_base=self.base)
        with self.assertRaises(ChannelError):
            channel.send("99", "   ")


class RegistryHonestyTests(unittest.TestCase):
    def test_platforms_that_need_a_business_approval_are_declared_not_faked(self):
        registry = default_registry(tempfile.mkdtemp())
        states = {row.name: row.state for row in registry.statuses()}
        for platform in ("instagram", "messenger", "twitter", "linkedin", "tiktok", "snapchat"):
            self.assertEqual(states[platform], UNAVAILABLE, platform)
        self.assertIn("telegram", states)
        self.assertIn("whatsapp", states)

    def test_an_unavailable_platform_refuses_with_its_reason(self):
        registry = default_registry(tempfile.mkdtemp())
        with self.assertRaises(ChannelError) as caught:
            registry.get("instagram").send("x", "hi")
        self.assertIn("review", str(caught.exception))

    def test_an_unknown_channel_lists_the_known_ones(self):
        registry = ChannelRegistry([])
        with self.assertRaises(KeyError) as caught:
            registry.get("myspace")
        self.assertIn("known channels", str(caught.exception))


if __name__ == "__main__":
    unittest.main()