"""The scheduler: queued work, the missed-window policy, the gate and the per-tick bound.

Time is injected everywhere, because a scheduler tested against the wall clock passes on a fast
machine and fails in the field. The three behaviours that matter are all about what happens when
the machine was *not* running: a stale run is skipped rather than replayed, a gated run is
deferred with a reason, and one failed task does not stop the others.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from orbynth.ops.schedule import Scheduler


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ran: list[str] = []
        self.scheduler = Scheduler(
            path=os.path.join(self.tmp.name, "schedule.json"),
            runner=lambda task: self.ran.append(task.id) or {"status": "done"},
        )

    # -- authoring ---------------------------------------------------------------------
    def test_a_schedule_with_no_future_time_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            self.scheduler.schedule("file the invoices", in_seconds=-30)
        self.assertIn("past", str(caught.exception))
        self.assertEqual(self.scheduler.snapshot()["count"], 0)

    def test_a_recurring_interval_under_a_minute_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            self.scheduler.schedule("poll the inbox every second", in_seconds=5, every_seconds=5)
        self.assertIn("60", str(caught.exception))

    def test_a_vague_objective_is_refused_rather_than_queued(self):
        with self.assertRaises(ValueError) as caught:
            self.scheduler.schedule("do it", in_seconds=60)
        self.assertIn("too short", str(caught.exception))

    def test_a_valid_one_shot_is_queued_and_disabled_after_it_runs(self):
        entry = self.scheduler.schedule("download yesterday's invoices", in_seconds=60, now=1000.0)
        self.assertTrue(entry.enabled)
        summary = self.scheduler.tick(now=1060.0)
        self.assertEqual(len(summary["ran"]), 1)
        self.assertEqual(self.ran, [entry.id])
        self.assertFalse(self.scheduler.entries[entry.id].enabled)
        self.assertEqual(self.scheduler.entries[entry.id].last_outcome, "ran: done")

    # -- the interesting failures ------------------------------------------------------
    def test_a_run_missed_past_the_grace_window_is_skipped_with_a_reason(self):
        entry = self.scheduler.schedule("open the portal and download the PDFs", in_seconds=0.5, now=1000.0)
        # The machine slept for two hours: grace is 30 minutes.
        summary = self.scheduler.tick(now=1000.0 + 7200)
        self.assertEqual(summary["ran"], [])
        self.assertIn("missed its window", summary["skipped"][0]["reason"])
        self.assertNotIn(entry.id, [task.id for task in self.scheduler.due(now=1e9)])

    def test_a_deferred_run_stays_queued_instead_of_running_on_a_locked_screen(self):
        gate_calls: list[str] = []

        def gate(task):
            gate_calls.append(task.id)
            return False, "the session is locked"

        scheduler = Scheduler(
            path=os.path.join(self.tmp.name, "gated.json"),
            runner=lambda task: {"status": "done"},
            gate=gate,
        )
        entry = scheduler.schedule("rename the downloads", in_seconds=10, needs_gui=True, now=1000.0)
        summary = scheduler.tick(now=1010.0)
        self.assertEqual(summary["ran"], [])
        self.assertIn("deferred", summary["skipped"][0]["reason"])
        # Still armed: the work is not lost, it waits for a machine it can actually use.
        self.assertTrue(scheduler.entries[entry.id].enabled)

    def test_a_raising_gate_is_a_refusal_not_a_pass(self):
        def gate(task):
            raise RuntimeError("preflight crashed")

        scheduler = Scheduler(
            path=os.path.join(self.tmp.name, "boom.json"),
            runner=lambda task: {"status": "done"},
            gate=gate,
        )
        scheduler.schedule("open the spreadsheet", in_seconds=10, needs_gui=True, now=1000.0)
        summary = scheduler.tick(now=1010.0)
        self.assertEqual(summary["ran"], [])
        self.assertIn("gate failed", summary["skipped"][0]["reason"])

    def test_one_failing_task_does_not_stop_the_next(self):
        calls: list[str] = []

        def runner(task):
            calls.append(task.id)
            if len(calls) == 1:
                raise RuntimeError("the portal changed its layout")
            return {"status": "done"}

        scheduler = Scheduler(path=os.path.join(self.tmp.name, "mixed.json"), runner=runner)
        scheduler.schedule("first task that explodes", in_seconds=1, now=1000.0)
        scheduler.schedule("second task that works", in_seconds=1, now=1000.0)
        summary = scheduler.tick(now=1002.0)
        self.assertEqual(len(summary["ran"]), 2)
        self.assertTrue(summary["ran"][0]["outcome"].startswith("failed: RuntimeError"))
        self.assertEqual(summary["ran"][1]["outcome"], "ran: done")

    def test_a_tick_is_bounded_so_a_backlog_cannot_stampede(self):
        scheduler = Scheduler(path=os.path.join(self.tmp.name, "many.json"), runner=lambda task: None, max_per_tick=2)
        for index in range(6):
            scheduler.schedule(f"queued job number {index}", in_seconds=1, now=1000.0)
        summary = scheduler.tick(now=1005.0)
        self.assertEqual(len(summary["ran"]), 2)

    # -- recurring ---------------------------------------------------------------------
    def test_a_recurring_task_advances_to_the_next_future_window(self):
        entry = self.scheduler.schedule("check the inbox", in_seconds=10, every_seconds=300, now=1000.0)
        self.scheduler.tick(now=1010.0)
        self.assertTrue(self.scheduler.entries[entry.id].enabled)
        self.assertGreater(self.scheduler.entries[entry.id].next_run, 1010.0)
        # Three intervals later the machine is up again: one run, no burst of three.
        self.scheduler.tick(now=1010.0 + 950)
        self.assertEqual(self.scheduler.entries[entry.id].runs, 2)

    def test_cancelling_works_by_id_and_by_title(self):
        entry = self.scheduler.schedule("file the bank statements", title="statements", in_seconds=60, now=1000.0)
        self.assertTrue(self.scheduler.cancel("statements"))
        self.assertFalse(self.scheduler.cancel(entry.id))
        self.assertEqual(self.scheduler.snapshot()["count"], 0)

    def test_the_schedule_survives_a_restart(self):
        self.scheduler.schedule("send the weekly summary", in_seconds=600, now=1000.0)
        reopened = Scheduler(path=os.path.join(self.tmp.name, "schedule.json"))
        self.assertEqual(reopened.snapshot()["count"], 1)
        self.assertIn("weekly summary", reopened.snapshot()["tasks"][0]["objective"])

    def test_the_loop_refuses_to_start_without_a_runner(self):
        scheduler = Scheduler(path=os.path.join(self.tmp.name, "norunner.json"))
        result = scheduler.start()
        self.assertFalse(result["started"])
        self.assertIn("no runner", result["reason"])


class ScheduleToolTests(unittest.TestCase):
    def setUp(self):
        from orbynth.actions import schedule as tools
        from orbynth.ops.lessons import LessonBook
        from tests.helpers import Stack

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stack = Stack()
        self.addCleanup(self.stack.close)
        self.scheduler = Scheduler(path=os.path.join(self.tmp.name, "s.json"), runner=lambda task: {"status": "done"})
        self.lessons = LessonBook(os.path.join(self.tmp.name, "lessons"))
        tools.set_services(
            tools.ScheduleServices(scheduler=self.scheduler, lessons=self.lessons, bus=self.stack.bus, config=self.stack.config)
        )
        self.addCleanup(tools.set_services, None)

    def test_scheduling_through_the_gate_records_a_task(self):
        result = self.stack.tool(
            "task_schedule", capabilities=["task.schedule"], objective="download the invoices at midnight", in_seconds=60
        )
        self.assertTrue(result.ok, result.error)
        self.assertEqual(self.scheduler.snapshot()["count"], 1)
        listed = self.stack.tool("task_schedules", capabilities=["task.schedule"])
        self.assertTrue(listed.ok, listed.error)
        self.assertEqual(listed.output["count"], 1)

    def test_scheduling_without_the_capability_is_denied(self):
        result = self.stack.tool("task_schedule", capabilities=["filesystem.read"], objective="do something later", in_seconds=60)
        self.assertFalse(result.ok)
        self.assertIn("capability denied", result.error)

    def test_a_refused_schedule_is_a_readable_failure(self):
        result = self.stack.tool("task_schedule", capabilities=["task.schedule"], objective="run it in the past", in_seconds=-10)
        self.assertFalse(result.ok)
        self.assertIn("past", result.error)

    def test_lessons_note_and_recall_round_trip(self):
        noted = self.stack.tool(
            "lessons_note",
            capabilities=["memory.admin"],
            action="browser_click",
            cause="shadow DOM swallowed the click",
            fix="pass pierce=true",
        )
        self.assertTrue(noted.ok, noted.error)
        recalled = self.stack.tool("lessons_recall", capabilities=["memory.admin"], action="browser_click")
        self.assertTrue(recalled.ok, recalled.error)
        self.assertEqual(recalled.output["lessons"][0]["fix"], "pass pierce=true")

    def test_a_lesson_without_a_fix_is_refused(self):
        refused = self.stack.tool("lessons_note", capabilities=["memory.admin"], action="x", cause="it broke", fix="  ")
        self.assertFalse(refused.ok)
        self.assertIn("teaches nothing", refused.error)


if __name__ == "__main__":
    unittest.main()
