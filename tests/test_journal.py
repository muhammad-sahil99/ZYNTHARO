"""Action Journal tests: write-ahead, exactly-once, reconciliation and undo.

These exercise the real registry path with a real journal file, because the properties under
test (a row on disk *before* the side effect, a replay that does not execute again, an undo
that reports what it could not reverse) are exactly the ones a mock would fake.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from orbynth.actions.registry import ActionContext
from orbynth.config import expand_capabilities
from orbynth.ops.journal import (
    ABANDONED,
    COMMITTED,
    FAILED,
    INTENDED,
    RUNNING,
    Intent,
    Journal,
    risk_of,
)
from tests.helpers import Stack


class JournalTestCase(unittest.TestCase):
    def setUp(self):
        self.stack = Stack()
        self.journal = Journal(os.path.join(self.stack.home, "journal.db"), self.stack.bus)
        self.stack.actions.journal = self.journal

    def tearDown(self):
        self.journal.close()
        self.stack.close()

    def invoke(self, name, *, task_id="task-1", key="", **kwargs):
        context = ActionContext(
            agent_id="test",
            capabilities=expand_capabilities(["*"]),
            bus=self.stack.bus,
            memory=self.stack.memory,
            config=self.stack.config,
            confirmed=("*",),
            task_id=task_id,
            idempotency_key=key,
        )
        return self.stack.actions.invoke(name, context, **kwargs)

    def path(self, name: str) -> str:
        return os.path.join(self.stack.home, name)


class WriteAheadTests(JournalTestCase):
    def test_the_intention_is_on_disk_before_the_action_runs(self):
        entry_id = self.journal.begin(Intent(action="fs_write", capability="filesystem.write", args={"path": "/tmp/x"}))
        self.assertGreater(entry_id, 0)
        entry = self.journal.entry(entry_id)
        self.assertEqual(entry.state, INTENDED)
        # An interrupted session is therefore visible: this is what reconciliation reads.
        self.assertEqual([e.entry_id for e in self.journal.open_entries()], [entry_id])

    def test_running_and_finishing_moves_the_entry_out_of_the_open_set(self):
        entry_id = self.journal.begin(Intent(action="fs_write", capability="filesystem.write", args={}))
        self.journal.mark_running(entry_id)
        self.assertEqual(self.journal.entry(entry_id).state, RUNNING)
        self.journal.finish(entry_id, True, {"done": True}, {"verified": True})
        entry = self.journal.entry(entry_id)
        self.assertEqual(entry.state, COMMITTED)
        self.assertEqual(entry.outcome, {"done": True})
        self.assertEqual(self.journal.open_entries(), [])

    def test_a_failure_is_recorded_as_a_failure(self):
        entry_id = self.journal.begin(Intent(action="fs_read", capability="filesystem.read", args={}))
        self.journal.finish(entry_id, False, None, {}, "no such file")
        entry = self.journal.entry(entry_id)
        self.assertEqual(entry.state, FAILED)
        self.assertEqual(entry.error, "no such file")

    def test_secret_looking_arguments_are_redacted_on_disk(self):
        entry_id = self.journal.begin(
            Intent(action="provider_add", capability="config.write", args={"url": "https://x", "api_key": "sk-live-123"})
        )
        entry = self.journal.entry(entry_id)
        self.assertEqual(entry.args["api_key"], "***")
        self.assertEqual(entry.args["url"], "https://x")
        self.assertNotIn("sk-live-123", self.journal.path.read_text(encoding="utf-8", errors="ignore"))

    def test_irreversible_capabilities_are_risk_level_three_whatever_the_tool_claims(self):
        self.assertEqual(risk_of("filesystem.delete", "low"), 3)
        self.assertEqual(risk_of("filesystem.write", "low"), 1)
        self.assertEqual(risk_of("browser.click", "medium"), 2)
        self.assertEqual(risk_of("provider.call", "none"), 0)


class ExactlyOnceTests(JournalTestCase):
    def test_a_keyed_action_executes_once_even_when_called_twice(self):
        """Proof, not assertion-of-intent: the second call must not append a second line."""
        ledger = self.path("ledger.txt")
        command = ["python3", "-c", f"open({ledger!r}, 'a').write('x')"]
        first = self.invoke("proc_run", key="ledger-append", argv=command)
        self.assertTrue(first.ok)
        second = self.invoke("proc_run", key="ledger-append", argv=command)
        self.assertTrue(second.ok)
        self.assertTrue(second.evidence.get("replayed"))
        self.assertEqual(open(ledger).read(), "x")
        self.assertEqual(len([e for e in self.journal.history("task-1") if e.state == COMMITTED]), 1)

    def test_an_action_without_a_key_stays_repeatable(self):
        """Two clicks on the same button are a human intention, not a duplication."""
        ledger = self.path("ledger2.txt")
        command = ["python3", "-c", f"open({ledger!r}, 'a').write('y')"]
        self.assertTrue(self.invoke("proc_run", argv=command).ok)
        self.assertTrue(self.invoke("proc_run", argv=command).ok)
        self.assertEqual(open(ledger).read(), "yy")

    def test_a_failed_keyed_action_is_not_replayed_as_success(self):
        missing = self.path("does-not-exist")
        first = self.invoke("fs_read", key="read-once", path=missing)
        self.assertFalse(first.ok)
        second = self.invoke("fs_read", key="read-once", path=missing)
        self.assertFalse(second.ok)
        self.assertFalse(second.evidence.get("replayed"))

    def test_every_journaled_call_records_the_real_outcome(self):
        target = self.path("written.txt")
        self.assertTrue(self.invoke("fs_write", path=target, content="hello").ok)
        entry = self.journal.history("task-1")[0]
        self.assertEqual(entry.action, "fs_write")
        self.assertEqual(entry.state, COMMITTED)
        self.assertIn("written.txt", entry.target)
        self.assertEqual(entry.risk, 1)
        self.assertTrue(entry.reversible)


class ReconciliationTests(JournalTestCase):
    """What an interrupted session left behind. Three honest answers, never a guess."""

    def test_an_interrupted_write_that_landed_is_reconciled_as_committed(self):
        target = self.path("landed.txt")
        with open(target, "w") as handle:
            handle.write("content")
        self.journal.begin(Intent(action="fs_write", capability="filesystem.write", args={"path": target}))
        report = self.journal.reconcile()
        self.assertEqual(report["checked"], 1)
        self.assertEqual(len(report["reconciled_committed"]), 1)
        self.assertEqual(self.journal.open_entries(), [])
        self.assertIn("had already taken effect", report["summary"])

    def test_an_interrupted_write_that_never_happened_is_abandoned(self):
        self.journal.begin(
            Intent(action="fs_write", capability="filesystem.write", args={"path": self.path("never.txt")})
        )
        report = self.journal.reconcile()
        self.assertEqual(len(report["abandoned"]), 1)
        self.assertEqual(report["needs_decision"], [])
        self.assertIn("never ran", report["summary"])

    def test_an_interrupted_action_with_no_probe_asks_for_a_decision(self):
        self.journal.begin(Intent(action="browser_click", capability="browser.click", args={"target": "Buy"}))
        report = self.journal.reconcile()
        self.assertEqual(len(report["needs_decision"]), 1)
        self.assertIn("no reconciliation probe", report["needs_decision"][0]["why"])
        # Left open on purpose: guessing here is how a system loses track of what it did.
        self.assertEqual(len(self.journal.open_entries()), 1)

    def test_reconciliation_reaches_the_bus_for_the_interface(self):
        self.journal.begin(Intent(action="fs_write", capability="filesystem.write", args={"path": self.path("a.txt")}))
        self.journal.reconcile()
        topics = [event.topic for event in self.stack.events]
        self.assertIn("journal.reconciled", topics)


class UndoTests(JournalTestCase):
    def test_undo_removes_a_file_the_task_created(self):
        target = self.path("created.txt")
        self.assertTrue(self.invoke("fs_write", path=target, content="x").ok)
        self.assertTrue(os.path.exists(target))
        report = self.journal.undo_task("task-1")
        self.assertEqual(len(report["undone"]), 1)
        self.assertFalse(os.path.exists(target))
        self.assertEqual(self.journal.history("task-1")[0].state, "compensated")
        self.assertIn("reversed", report["summary"])

    def test_undo_moves_a_moved_file_back(self):
        src, dst = self.path("src.txt"), self.path("dst.txt")
        self.invoke("fs_write", path=src, content="data")
        self.assertTrue(self.invoke("fs_move", src=src, dst=dst).ok)
        report = self.journal.undo_task("task-1")
        self.assertFalse(os.path.exists(dst))
        self.assertTrue(any(row["action"] == "fs_move" for row in report["undone"]))
        self.assertIn("moved", " ".join(row["undo"]["detail"] for row in report["undone"]))

    def test_undo_refuses_irreversible_work_and_says_so(self):
        target = self.path("doomed.txt")
        with open(target, "w") as handle:
            handle.write("bye")
        self.assertTrue(self.invoke("fs_delete", path=target, to_trash=False).ok)
        report = self.journal.undo_task("task-1")
        self.assertEqual(len(report["refused"]), 1)
        self.assertIn("irreversible", report["refused"][0]["reason"])
        self.assertIn("refused as irreversible", report["summary"])

    def test_undo_reports_a_tool_it_cannot_reverse(self):
        entry_id = self.journal.begin(
            Intent(action="office_word", capability="office.word", args={"path": self.path("d.docx"), "operation": "save"})
        )
        self.journal.finish(entry_id, True, {"saved": True})
        report = self.journal.undo_task("")
        self.assertTrue(report["attempted"] >= 1)
        self.assertTrue(any("no undo is implemented" in row["reason"] for row in report["failed"]))

    def test_undo_emits_a_topic_and_last_task_with_nothing_is_honest(self):
        empty = self.journal.undo_last_task()
        self.assertEqual(empty["attempted"], 0)
        self.assertIn("nothing to undo", empty["summary"])
        target = self.path("later.txt")
        self.invoke("fs_write", task_id="task-2", path=target, content="y")
        report = self.journal.undo_last_task()
        self.assertEqual(report["task_id"], "task-2")
        self.assertFalse(os.path.exists(target))
        self.assertIn("journal.undone", [event.topic for event in self.stack.events])

    def test_stats_report_the_real_state_of_the_log(self):
        target = self.path("counted.txt")
        self.invoke("fs_write", path=target, content="z")
        stats = self.journal.stats()
        self.assertEqual(stats["entries"], 1)
        self.assertEqual(stats["by_state"][COMMITTED], 1)
        self.assertEqual(stats["open"], 0)
        self.assertTrue(stats["last_activity"])


if __name__ == "__main__":
    unittest.main()
