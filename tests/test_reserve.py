"""The model bench: limits intake, reserve activation, and honest refusal.

The Steward's arithmetic is only as good as the numbers it is given, so the tests here are
about *refusing* the numbers that would quietly break it:

* a negative cap, or a weekly cap below the daily one, is rejected with the reason;
* a new provider without a base URL or a key *name* is rejected — pointing the system at a
  URL and hoping is how a key ends up in a config file;
* an existing provider is never silently repointed at a new host;
* a reserve is registered and quota-tracked but assigned to nobody until activated, and its
  headroom is read from the same quota manager everything else obeys.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from orbynth.config import AppConfig, ProviderConfig, ModelSpec
from orbynth.events import MODEL_LIMITS, MODEL_RESERVE, EventBus
from orbynth.llm.quotas import QuotaManager
from orbynth.llm.reserve import ModelLimits, ReservePool


def _config(home: str) -> AppConfig:
    return AppConfig(
        home=home,
        providers=[
            ProviderConfig(
                name="echo",
                kind="echo",
                priority=999,
                models=[ModelSpec(model_id="offline-echo", rpm=1000, rpd=100_000, tags=["offline"])],
            )
        ],
    )


class LimitValidationTests(unittest.TestCase):
    def test_negative_and_zero_caps_are_refused(self):
        for bad in ({"rpm": -1}, {"rpd": 0}, {"tpw": -5}):
            with self.assertRaises(ValueError):
                ModelLimits(**bad).validate()

    def test_weekly_cap_below_daily_cap_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            ModelLimits(rpd=1000, rpw=500).validate()
        self.assertIn("smaller than the daily cap", str(caught.exception))

    def test_describe_lists_only_declared_windows(self):
        self.assertEqual(ModelLimits(rpm=60, tpd=90_000).describe(), "60 req/min, 90000 tok/day")
        self.assertEqual(ModelLimits().describe(), "no limits declared")


class ReservePoolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name
        self.config = _config(self.home)
        self.config.save()
        self.bus = EventBus()
        self.events: list[str] = []
        self.bus.subscribe("*", lambda event: self.events.append(event.topic))
        self.quotas = QuotaManager(self.config, self.bus, persist_path=os.path.join(self.home, "quota.json"))
        self.pool = ReservePool(
            self.config,
            self.quotas,
            self.bus,
            path=os.path.join(self.home, "reserves.json"),
            persist=False,
        )

    def add(self, **overrides):
        payload = dict(
            provider="groq",
            model_id="llama-test",
            limits=ModelLimits(rpm=30, rpd=14_400, rpw=100_000, tpm=6_000, tpd=500_000, tpw=3_000_000),
            kind="openai_compatible",
            base_url="https://api.groq.test/v1",
            api_key_env="GROQ_API_KEY",
            tags=("fast", "general"),
            reserve=True,
        )
        payload.update(overrides)
        return self.pool.add_model(**payload)

    def test_adding_a_model_records_limits_and_starts_as_a_reserve(self):
        result = self.add()
        self.assertEqual(result["model"], "groq:llama-test")
        self.assertTrue(result["reserve"])
        self.assertIn("reserve", result["note"])
        spec = self.config.model("groq:llama-test")
        self.assertIsNotNone(spec)
        self.assertEqual((spec.rpm, spec.rpd, spec.rpw), (30, 14_400, 100_000))
        self.assertEqual((spec.tpm, spec.tpd, spec.tpw), (6_000, 500_000, 3_000_000))
        # Quota-tracked from the first call: the manager knows the envelope immediately.
        self.assertIn("groq:llama-test", self.quotas.models)
        self.assertIn(MODEL_LIMITS, self.events)

    def test_a_reserve_is_not_offered_but_is_callable_by_activation(self):
        self.add()
        self.assertEqual([r["model"] for r in self.pool.reserves()], ["groq:llama-test"])
        activated = self.pool.activate("groq:llama-test", by="steward", reason="primary exhausted")
        self.assertTrue(activated["activated"])
        self.assertEqual(self.pool.reserves(), [])
        self.assertIn(MODEL_RESERVE, self.events)
        self.assertEqual(self.pool.activate("groq:llama-test")["activated"], False)

    def test_retiring_puts_it_back_on_the_bench(self):
        self.add(reserve=False)
        self.assertEqual(self.pool.reserves(), [])
        self.pool.retire("groq:llama-test", reason="quota cooling")
        self.assertEqual([r["model"] for r in self.pool.reserves()], ["groq:llama-test"])

    def test_new_provider_needs_base_url_and_a_key_name(self):
        with self.assertRaises(ValueError) as caught:
            self.pool.add_model("fresh", "m1", ModelLimits(rpm=5))
        self.assertIn("base_url is required", str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            self.pool.add_model("fresh", "m1", ModelLimits(rpm=5), base_url="https://x.test/v1")
        self.assertIn("api_key_env is required", str(caught.exception))

    def test_existing_provider_is_never_silently_repointed(self):
        self.add()
        with self.assertRaises(ValueError) as caught:
            self.add(model_id="other", base_url="https://elsewhere.test/v1")
        self.assertIn("refusing to redirect", str(caught.exception))
        self.assertEqual(self.config.provider("groq").base_url, "https://api.groq.test/v1")

    def test_bad_limits_are_refused_before_anything_is_written(self):
        before = len(self.config.providers)
        with self.assertRaises(ValueError):
            self.add(limits=ModelLimits(rpm=0))
        self.assertEqual(len(self.config.providers), before)

    def test_unknown_model_activation_lists_what_exists(self):
        with self.assertRaises(ValueError) as caught:
            self.pool.activate("ghost:model")
        self.assertIn("unknown model", str(caught.exception))

    def test_headroom_reflects_real_quota_spend(self):
        # A single 500k-token call against a 6k/min envelope is *exhausted*, not half-spent:
        # that is the minute window doing its job. To test a partial daily spend, the
        # envelope has to be one where the daily figure is the binding constraint.
        self.add(limits=ModelLimits(rpm=100, rpd=100, tpm=2_000_000, tpd=1_000_000, tpw=5_000_000))
        self.quotas.record("groq:llama-test", prompt_tokens=500_000, completion_tokens=0)
        row = next(r for r in self.pool.rows() if r["model"] == "groq:llama-test")
        self.assertGreater(row["headroom"], 0.0)
        self.assertLess(row["headroom"], 0.6)

    def test_a_call_that_blows_the_minute_window_leaves_no_headroom(self):
        self.add()
        self.quotas.record("groq:llama-test", prompt_tokens=300_000, completion_tokens=0)
        row = next(r for r in self.pool.rows() if r["model"] == "groq:llama-test")
        # 300k tokens against tpm=6_000 is spent for the current minute, and honesty means
        # saying so rather than averaging it away against the weekly budget.
        self.assertEqual(row["headroom"], 0.0)

    def test_exhausted_reserve_is_not_recommended(self):
        self.add()
        manager = self.quotas.models["groq:llama-test"]
        manager.rpd = 1
        self.quotas.record("groq:llama-test", prompt_tokens=10, completion_tokens=0)
        self.assertIsNone(self.pool.best_reserve())

    def test_reserve_state_survives_a_restart(self):
        path = os.path.join(self.home, "reserves.json")
        pool = ReservePool(self.config, self.quotas, self.bus, path=path, persist=True)
        pool.add_model(
            "groq",
            "llama-test",
            ModelLimits(rpm=30, rpd=14_400),
            base_url="https://api.groq.test/v1",
            api_key_env="GROQ_API_KEY",
            reserve=True,
        )
        reopened = ReservePool(self.config, self.quotas, self.bus, path=path, persist=True)
        self.assertIn("groq:llama-test", reopened._held)
        payload = json.loads(open(path, encoding="utf-8").read())
        self.assertIn("added", payload)


if __name__ == "__main__":
    unittest.main()
