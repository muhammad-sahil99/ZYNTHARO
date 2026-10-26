"""Core tests: configuration, capability expansion, event bus and memory."""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest

from tests.helpers import Stack, make_config
from orbynth.config import CAPABILITIES, AppConfig, ProviderConfig, ModelSpec, expand_capabilities
from orbynth.events import Event, EventBus, LOG
from orbynth.memory import MemoryStore


class ConfigTests(unittest.TestCase):
    def test_capability_expansion(self):
        self.assertIn("filesystem.read", expand_capabilities(["filesystem.*"]))
        self.assertEqual(len(expand_capabilities(["*"])), len(CAPABILITIES))
        self.assertIn("custom.thing", expand_capabilities(["custom.thing"]))

    def test_round_trip_preserves_providers_and_roster(self):
        with tempfile.TemporaryDirectory() as home:
            config = make_config(home)
            config.roster.append(__import__("orbynth.config", fromlist=["AgentConfig"]).AgentConfig(name="X", role="operative"))
            path = config.save()
            reloaded = AppConfig.from_json(json.loads(open(path).read()))
            self.assertEqual(reloaded.providers[0].name, "echo")
            self.assertEqual(reloaded.roster[-1].name, "X")
            self.assertEqual(reloaded.model("echo:offline-echo").qualified, "echo:offline-echo")

    def test_model_inherits_provider_name(self):
        provider = ProviderConfig(name="groq", kind="openai_compatible", models=[ModelSpec(model_id="llama")])
        self.assertEqual(provider.model("llama").qualified, "groq:llama")

    def test_config_rejects_nothing_but_reports_missing_models(self):
        config = make_config(tempfile.mkdtemp())
        self.assertIsNone(config.model("nope:missing"))


class EventBusTests(unittest.TestCase):
    def test_wildcard_and_specific_subscriptions(self):
        bus = EventBus()
        seen: list[str] = []
        bus.subscribe("agent.*", lambda event: seen.append("agent"))
        bus.subscribe("action.result", lambda event: seen.append("action"))
        bus.emit("agent.spawned", name="A")
        bus.emit("action.result", action="fs_read")
        bus.emit("unrelated.topic")
        self.assertEqual(seen, ["agent", "action"])

    def test_history_replay_after_late_subscription(self):
        bus = EventBus()
        bus.emit("quota.warning", model="x")
        later: list[Event] = []
        bus.subscribe("quota.*", later.append)
        self.assertEqual(len(later), 1)

    def test_failing_subscriber_does_not_break_publishers(self):
        bus = EventBus()
        bus.subscribe("x.*", lambda event: (_ for _ in ()).throw(RuntimeError("boom")))
        good: list[str] = []
        bus.subscribe("x.*", lambda event: good.append(event.topic))
        bus.emit("x.one")
        self.assertEqual(good, ["x.one"])
        self.assertTrue(any(event.topic == LOG for event in bus.history()))

    def test_unsubscribe_stops_delivery(self):
        bus = EventBus()
        hits: list[str] = []
        subscription = bus.subscribe("a.b", lambda event: hits.append(event.topic))
        bus.emit("a.b")
        subscription.close()
        bus.emit("a.b")
        self.assertEqual(len(hits), 1)


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.memory = MemoryStore(":memory:")
        self.agent = self.memory.register_agent("Sovereign", rank=0, role="sovereign", capabilities=["*"])

    def tearDown(self):
        self.memory.close()

    def test_namespaces_are_separate(self):
        other = self.memory.register_agent("Operative", rank=5, role="operative")
        self.memory.remember(self.agent.agent_id, "the supplier portal needs 2FA", importance=0.9)
        self.memory.remember(other.agent_id, "renamed invoice_0412.pdf", importance=0.4)
        sovereign_hits = self.memory.recall(self.agent.agent_id, "supplier portal")
        operative_hits = self.memory.recall(other.agent_id, "supplier portal")
        self.assertTrue(sovereign_hits)
        self.assertFalse(operative_hits)

    def test_recall_ranks_by_relevance_then_importance(self):
        self.memory.remember(self.agent.agent_id, "invoice portal login uses a one-time code", importance=0.3)
        self.memory.remember(self.agent.agent_id, "lunch order was noodles", importance=0.9)
        hits = self.memory.recall(self.agent.agent_id, "invoice portal", k=2)
        self.assertIn("invoice portal", hits[0].text)

    def test_team_board_is_shared_across_the_team(self):
        self.memory.post_board("team-1", "plan", "split download and reconcile", author="Foreman")
        entries = self.memory.read_board("team-1")
        self.assertEqual(entries[0]["author"], "Foreman")

    def test_facts_and_transcript(self):
        self.memory.set_fact(self.agent.agent_id, "portal_url", "https://supplier.example")
        self.assertEqual(self.memory.get_fact(self.agent.agent_id, "portal_url"), "https://supplier.example")
        self.memory.append_message(self.agent.agent_id, "user", "hi")
        self.assertEqual(self.memory.transcript_len(self.agent.agent_id), 1)

    def test_tasks_and_reports(self):
        task = self.memory.create_task("team-1", owner=self.agent.agent_id, title="fetch invoices")
        self.memory.update_task(task, status="done", report={"summary": "41 files"})
        self.assertEqual(self.memory.task(task)["report"]["summary"], "41 files")
        self.assertEqual(len(self.memory.tasks(status="done")), 1)

    def test_audit_redacts_secrets(self):
        self.memory.audit(
            self.agent.agent_id,
            "http_post",
            "network.http",
            "ok",
            args={"api_key": "sk-live-123", "url": "https://x", "nested": {"password": "hunter2"}},
        )
        row = self.memory.audit_log(1)[0]
        self.assertEqual(row["args"]["api_key"], "***")
        self.assertEqual(row["args"]["nested"]["password"], "***")
        self.assertEqual(row["args"]["url"], "https://x")

    def test_prune_audit_respects_age(self):
        self.memory.audit(self.agent.agent_id, "old", "shell.exec", "ok")
        with self.memory._conn:
            self.memory._conn.execute("UPDATE audit SET ts = ?", (time.time() - 200 * 86400,))
        self.assertEqual(self.memory.prune_audit(90), 1)


class StackSmokeTests(unittest.TestCase):
    def test_stack_builds_every_subsystem(self):
        stack = Stack()
        try:
            self.assertGreater(len(stack.actions), 80)
            self.assertTrue(stack.providers.ready("echo"))
            self.assertEqual(stack.runtime.candidates(), ["echo:offline-echo"])
        finally:
            stack.close()


if __name__ == "__main__":
    unittest.main()
