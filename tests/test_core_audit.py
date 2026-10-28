"""Audit tests for the arithmetic and retrieval layers.

These three modules decide things silently: the quota manager decides whether a call may run,
the memory store decides what an agent remembers, and the budget decides when an agent stops.
A wrong sign or a mis-bound parameter in any of them produces plausible-looking behaviour, which
is why each test below is written against the *failure* it prevents rather than a happy path.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from orbynth.config import ModelSpec
from orbynth.events import QUOTA_WARNING, EventBus
from orbynth.llm.quotas import QuotaManager
from orbynth.memory import MemoryStore
from orbynth.llm.runtime import Budget, BudgetExceeded


class Clock:
    def __init__(self, start: float = 1_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def manager(clock: Clock, path: str | None = None, **limits) -> QuotaManager:
    bus = EventBus()
    quota = QuotaManager(bus=bus, clock=clock, persist_path=path)
    spec = ModelSpec(model_id="m", provider="p", **limits)
    quota.register_model(spec)
    return quota


class QuotaPersistenceTests(unittest.TestCase):
    def test_an_auth_cooldown_survives_a_restart(self):
        # An auth failure locks the model for an hour. Forgetting that on restart meant every
        # restart walked straight back into the same 401 and burned another attempt.
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "quota.json")
            clock = Clock()
            first = manager(clock, path, rpm=10, rpd=100)
            first.note_error("p:m", "auth", detail="invalid key")
            self.assertFalse(first.check("p:m").allowed)
            second = manager(clock, path, rpm=10, rpd=100)
            decision = second.check("p:m")
            self.assertFalse(decision.allowed, "the cooldown was forgotten across the restart")
            self.assertEqual(decision.reason, "cooldown")

    def test_an_expired_cooldown_is_not_restored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "quota.json")
            clock = Clock()
            first = manager(clock, path, rpm=10)
            first.note_error("p:m", "quota", retry_after=5.0)
            clock.advance(10.0)
            second = manager(clock, path, rpm=10)
            self.assertTrue(second.check("p:m").allowed)

    def test_spend_counts_survive_a_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "quota.json")
            clock = Clock()
            first = manager(clock, path, rpm=10, rpd=100)
            for _ in range(3):
                first.record("p:m", prompt_tokens=10, completion_tokens=5)
            second = manager(clock, path, rpm=10, rpd=100)
            row = second.snapshot()[0]
            self.assertEqual(row["rpd"]["used"], 3)
            self.assertEqual(row["tpd"]["used"], 45)


class QuotaWarningTests(unittest.TestCase):
    def test_a_warning_fires_once_per_minute_not_once_per_call(self):
        clock = Clock()
        quota = manager(clock, rpm=10)
        seen: list[str] = []
        quota.bus.subscribe(QUOTA_WARNING, lambda event: seen.append(event.payload.get("window", "")))
        for _ in range(8):
            quota.record("p:m")
            quota.check("p:m")
        self.assertEqual(seen, ["rpm"], f"a warning on every call is noise, got {seen}")
        # A fresh minute with the same pressure earns exactly one fresh warning.
        clock.advance(61.0)
        for _ in range(8):
            quota.record("p:m")
            quota.check("p:m")
        self.assertEqual(len(seen), 2, f"one warning per minute, got {seen}")

    def test_headroom_ignores_spend_from_a_window_that_has_passed(self):
        clock = Clock()
        quota = manager(clock, rpm=2, rpd=100)
        quota.record("p:m")
        quota.record("p:m")
        self.assertEqual(quota.ranked_by_headroom(["p:m"])[0][1], 0.0, "both per-minute slots are spent")
        clock.advance(61.0)
        ranked = quota.ranked_by_headroom(["p:m"])
        # The two requests still count against the *day*, so headroom is 0.98 — but the spent
        # per-minute window must no longer block the model, which is what "0.0" would mean.
        self.assertGreater(ranked[0][1], 0.9, ranked)


class BudgetTests(unittest.TestCase):
    def test_the_budget_stops_at_the_granted_limit(self):
        budget = Budget(max_actions=2, max_tokens=100, max_wall_seconds=60)
        budget.start(0.0)
        budget.charge_action(0.0)
        budget.charge_action(0.0)
        with self.assertRaises(BudgetExceeded):
            budget.charge_action(0.0)

    def test_tokens_and_wall_clock_are_both_enforced(self):
        budget = Budget(max_actions=100, max_tokens=10, max_wall_seconds=5)
        budget.start(0.0)
        with self.assertRaises(BudgetExceeded):
            budget.charge_tokens(11, 0.0)
        wall = Budget(max_actions=100, max_tokens=1000, max_wall_seconds=5)
        wall.start(0.0)
        with self.assertRaises(BudgetExceeded):
            wall.check(6.0)
        self.assertEqual(wall.to_json(6.0)["elapsed_s"], 6.0)


class MemoryRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = MemoryStore(os.path.join(self.tmp.name, "mem.db"))
        self.addCleanup(self.store.close)
        self.store.remember("a1", "the invoice portal login failed with a timeout", kind="note")
        self.store.remember("a1", "invoices are filed under 2026/Q3", kind="fact")
        self.store.remember("a2", "a different agent's private note about invoices", kind="note")

    def test_a_kind_filtered_search_returns_the_right_rows(self):
        # The old fallback built its placeholder list from the combined parameters, so the kind
        # names were bound where agent ids belonged: correct row count, wrong rows, no error.
        self.store.has_fts = False
        hits = self.store.recall("a1", "invoices", k=10, kinds=["fact"])
        self.assertTrue(hits, "the kind-filtered search returned nothing")
        self.assertTrue(all(hit.kind == "fact" for hit in hits), [h.kind for h in hits])
        self.assertTrue(all(hit.agent_id == "a1" for hit in hits), [h.agent_id for h in hits])

    def test_another_agents_memory_is_not_returned(self):
        self.store.has_fts = False
        hits = self.store.recall("a1", "invoices", k=10)
        self.assertTrue(hits)
        self.assertNotIn("a2", {hit.agent_id for hit in hits})

    def test_a_query_with_no_usable_terms_does_not_raise(self):
        # "to be or" tokenises to nothing; the old FTS path sent a bare quote to MATCH.
        for query in ("to be or", "a", ""):
            hits = self.store.recall("a1", query, k=5)
            self.assertIsInstance(hits, list)

    def test_a_kind_filtered_search_is_correct_on_both_paths(self):
        # The FTS path has its own parameter list, and it had the same shape of bug available to
        # it: bind the filter values where the scopes belong. Both paths are checked here.
        for has_fts in (True, False):
            self.store.has_fts = has_fts
            hits = self.store.recall("a1", "invoices", k=10, kinds=["fact"])
            self.assertTrue(hits, f"no hits with has_fts={has_fts}")
            self.assertTrue(all(hit.kind == "fact" for hit in hits), [h.kind for h in hits])
            self.assertTrue(all(hit.agent_id == "a1" for hit in hits), [h.agent_id for h in hits])

    def test_both_paths_find_the_same_exact_term(self):
        self.store.has_fts = True
        fts_hits = {hit.id for hit in self.store.recall("a1", "portal", k=5)}
        self.store.has_fts = False
        fallback_hits = {hit.id for hit in self.store.recall("a1", "portal", k=5)}
        self.assertTrue(fts_hits, "the FTS path found nothing for an exact term")
        # Substring LIKE also matches "invoices" for "invoice", so the fallback may be a superset;
        # it must never miss something the index found.
        self.assertTrue(fts_hits <= fallback_hits, (fts_hits, fallback_hits))


if __name__ == "__main__":
    unittest.main()
