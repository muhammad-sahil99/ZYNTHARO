"""Tests for hardware sensing (spec 16).

Everything here is deterministic: readings are injected, the clock is the caller's, and the
protective hooks are recorded calls. A test must never depend on this machine actually being
hot, on a fan existing, or on a battery being present.
"""

from __future__ import annotations

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orbynth.actions import hardware as hardware_actions  # noqa: E402
from orbynth.actions.registry import ActionContext  # noqa: E402
from orbynth.config import expand_capabilities  # noqa: E402
from orbynth.ops.sensors import (  # noqa: E402
    DANGER,
    DEFAULT_RULES,
    Collector,
    ProtectionPolicy,
    Reading,
    SensorMonitor,
    Snapshot,
)


class FakeSource(Collector):
    """A collector whose readings are exactly what the test says they are.

    It subclasses the real collector so the plausibility filter, the snapshot shape and the
    timings are the production ones: only the numbers are injected.
    """

    def __init__(self, readings):
        super().__init__(sources=())
        self.readings = readings

    def collect(self):
        self.last_duration = 0.0005
        return [Reading(**reading) for reading in self.readings]


def reading(key, value, unit="C", source="test", **extra):
    return {"key": key, "label": key, "value": value, "unit": unit, "source": source, "ts": time.time(), **extra}


def monitor_with(readings, **kwargs):
    return SensorMonitor(collector=FakeSource(readings), **kwargs)


class Plausibility(unittest.TestCase):
    def test_an_impossible_reading_is_kept_but_flagged(self):
        collector = Collector()
        readings = collector.filter([Reading(key="cpu.temp", label="cpu", value=5000.0, unit="C", source="test", ts=time.time())])
        self.assertEqual(len(readings), 1, "a broken sensor is information, not a gap")
        self.assertIn("plausible range", readings[0].suspect)

    def test_a_sudden_jump_is_flagged_as_a_sensor_fault(self):
        collector = Collector()
        now = time.time()
        prior = Reading(key="cpu.temp", label="cpu", value=40.0, unit="C", source="test", ts=now)
        reading_now = Reading(key="cpu.temp", label="cpu", value=95.0, unit="C", source="test", ts=now + 0.2)
        out = collector.filter([reading_now], {"cpu.temp": prior})
        self.assertTrue(out[0].suspect, "a 275 C/s change is a broken sensor, not a hot CPU")

    def test_a_suspect_reading_never_fires_an_alert(self):
        # This is the bug the filter exists to prevent: 5000 C became a critical alert and a
        # protective action against a machine that was perfectly fine.
        fired = []
        monitor = monitor_with(
            [reading("cpu.temp", 5000.0)],
            policy=ProtectionPolicy(notify=lambda alert: fired.append(alert)),
            rules=DEFAULT_RULES,
        )
        monitor.rules = tuple(rule for rule in DEFAULT_RULES if rule.rule == "cpu.temp.high")
        monitor.sample()
        self.assertEqual(fired, [])
        self.assertEqual(monitor.alerts, {})
        self.assertTrue(monitor.last_snapshot.readings[0].suspect, "the fault is still reported")

    def test_ingest_refuses_what_the_filter_rejects(self):
        monitor = monitor_with([])
        self.assertIsNone(monitor.ingest("cpu.temp", 5000.0, unit="C"))
        self.assertEqual(monitor.history.window("cpu.temp"), [])
        accepted = monitor.ingest("phone.battery", 42.0, unit="%", source="phone")
        self.assertIsNotNone(accepted)
        self.assertEqual(monitor.history.latest("phone.battery")[1], 42.0)


class Rules(unittest.TestCase):
    def only(self, name):
        return tuple(rule for rule in DEFAULT_RULES if rule.rule == name)

    def test_a_real_high_temperature_fires_with_evidence(self):
        fired = []
        monitor = monitor_with(
            [reading("cpu.temp", 96.0)],
            policy=ProtectionPolicy(notify=lambda alert: fired.append(alert)),
            rules=self.only("cpu.temp.high"),
        )
        monitor.sample()
        self.assertEqual(len(fired), 1)
        alert = fired[0]
        self.assertEqual(alert.severity, DANGER)
        self.assertTrue(alert.causes and alert.actions, "an alert without causes and next steps is noise")
        self.assertTrue(alert.evidence, "an alert without the numbers behind it is an opinion")

    def test_the_same_alert_does_not_spam(self):
        fired = []
        monitor = monitor_with(
            [reading("cpu.temp", 96.0)],
            policy=ProtectionPolicy(notify=lambda alert: fired.append(alert)),
            rules=self.only("cpu.temp.high"),
        )
        for _ in range(5):
            monitor.sample()
        self.assertEqual(len(fired), 1)
        self.assertEqual(list(monitor.alerts.values())[0].count, 5)

    def test_recovery_clears_the_alert(self):
        fired = []
        monitor = monitor_with(
            [reading("cpu.temp", 96.0)],
            policy=ProtectionPolicy(notify=lambda alert: fired.append(alert)),
            rules=self.only("cpu.temp.high"),
        )
        monitor.sample()
        monitor.collector.readings = [reading("cpu.temp", 60.0)]
        # The first reading after a 36 C drop is flagged as an impossible jump, so the monitor
        # keeps the alert open: it cannot verify recovery from a suspect reading. One more
        # steady sample proves the machine really is cool, and the alert closes.
        monitor.sample()
        self.assertTrue(monitor.active, "recovery is not assumed from one impossible sample")
        monitor.sample()
        self.assertEqual(monitor.active, {})

    def test_protective_actions_scale_with_severity(self):
        calls = {"notify": 0, "reduce": 0, "safe": 0, "down": 0}
        policy = ProtectionPolicy(
            notify=lambda alert: calls.__setitem__("notify", calls["notify"] + 1),
            reduce_load=lambda alert: calls.__setitem__("reduce", calls["reduce"] + 1),
            safe_state=lambda alert: calls.__setitem__("safe", calls["safe"] + 1),
            power_down=lambda alert: calls.__setitem__("down", calls["down"] + 1),
            allow_power_down=False,
        )
        monitor = monitor_with([reading("cpu.temp", 106.0)], policy=policy, rules=self.only("cpu.temp.high"))
        monitor.sample()
        self.assertEqual(calls, {"notify": 1, "reduce": 1, "safe": 1, "down": 0}, "power-down stays off by default")

    def test_a_broken_rule_cannot_stop_the_monitor(self):
        class Broken:
            rule = "broken"

            def evaluate(self, snapshot, history, baseline):
                raise RuntimeError("boom")

        monitor = monitor_with([reading("cpu.load", 0.2, unit="ratio")], rules=(Broken(),))
        snapshot = monitor.sample()
        self.assertEqual(len(snapshot.readings), 1)


class Tuning(unittest.TestCase):
    def test_tightening_is_allowed_for_an_agent(self):
        monitor = monitor_with([])
        result = monitor.tune("cpu.temp.high", 70.0)
        self.assertTrue(result["tuned"])
        self.assertEqual(result["direction"], "tighten")

    def test_loosening_needs_the_owner(self):
        monitor = monitor_with([])
        refused = monitor.tune("cpu.temp.high", 95.0)
        self.assertFalse(refused["tuned"])
        self.assertEqual(refused["requires"], "owner")
        self.assertTrue(monitor.tune("cpu.temp.high", 95.0, owner=True)["tuned"])

    def test_severity_lines_stay_in_order(self):
        monitor = monitor_with([])
        monitor.tune("cpu.temp.high", 60.0)
        rule = next(rule for rule in monitor.rules if rule.rule == "cpu.temp.high")
        self.assertLess(rule.watch, rule.warning)
        self.assertLess(rule.warning, rule.danger)
        self.assertLess(rule.danger, rule.critical)
        self.assertGreaterEqual(rule.critical, 60.0)

    def test_unknown_rule_is_reported_not_invented(self):
        monitor = monitor_with([])
        result = monitor.tune("no.such.rule", 1.0)
        self.assertFalse(result["tuned"])
        self.assertIn("known", result)


class Honesty(unittest.TestCase):
    def test_load_by_agent_without_attribution_says_so(self):
        monitor = monitor_with([])
        result = monitor.load_by_agent()
        self.assertFalse(result["attributed"])
        self.assertIn("no agent-to-process map", result["reason"])

    def test_overhead_is_not_claimed_during_startup(self):
        monitor = monitor_with([])
        monitor.sample()
        report = monitor.overhead()
        self.assertTrue(report["warming_up"])
        self.assertIsNone(report["within_target"], "a 20 ms window cannot prove a 1% target")

    def test_sensor_report_lists_what_is_missing(self):
        collector = Collector()
        monitor = SensorMonitor(collector=collector)
        report = monitor.sensor_report()
        self.assertIn("found", report)
        self.assertIn("unavailable", report)
        self.assertIn("note", report)

    def test_start_stop_is_idempotent_and_joined(self):
        monitor = monitor_with([])
        self.assertTrue(monitor.start())
        self.assertFalse(monitor.start(), "a second start must not create a second sampler")
        stopped = monitor.stop()
        self.assertTrue(stopped["stopped"])
        self.assertGreaterEqual(stopped["samples"], 1)

    def test_sampling_speeds_up_when_something_is_wrong(self):
        monitor = monitor_with([])
        self.assertEqual(monitor.next_interval(), monitor.idle_interval)
        monitor.active = {"rule:sensor": object()}
        self.assertEqual(monitor.next_interval(), monitor.burst_interval)


class Actions(unittest.TestCase):
    def setUp(self):
        self.monitor = monitor_with([reading("cpu.temp", 55.0)])
        hardware_actions.set_monitor(self.monitor)
        self.context = ActionContext(agent_id="t", capabilities=expand_capabilities(["hardware.*"]))

    def tearDown(self):
        hardware_actions.set_monitor(None)

    def test_summary_reads_the_live_monitor(self):
        result = hardware_actions.hw_summary.invoke(self.context)
        self.assertEqual(len(result["sensors"]), 1)
        self.assertEqual(result["sensors"][0]["key"], "cpu.temp")

    def test_report_and_summary_agree(self):
        hardware_actions.hw_report.invoke(self.context, sensor="phone.battery", value=55.0, unit="%")
        self.assertIsNotNone(self.monitor.history.latest("phone.battery"))

    def test_a_rejected_report_says_why(self):
        result = hardware_actions.hw_report.invoke(self.context, sensor="cpu.temp", value=9999.0, unit="C")
        self.assertFalse(result["accepted"])
        self.assertIn("plausibility", result["reason"])

    def test_tuning_that_needs_the_owner_fails_loudly(self):
        with self.assertRaises(Exception) as caught:
            hardware_actions.hw_tune.invoke(self.context, rule="cpu.temp.high", value=99.0)
        self.assertIn("owner", str(caught.exception))

    def test_unknown_alert_is_an_error_not_an_empty_answer(self):
        with self.assertRaises(Exception):
            hardware_actions.hw_explain.invoke(self.context, alert_id="nope")


if __name__ == "__main__":
    unittest.main()