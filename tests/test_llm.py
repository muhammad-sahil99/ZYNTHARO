"""LLM layer tests: quota accounting, provider failover and context-preserving transplants."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from tests.helpers import Stack
from orbynth.config import AppConfig, ModelSpec, ProviderConfig, Secrets
from orbynth.events import MODEL_FALLBACK, PROVIDER_ERROR, QUOTA_EXHAUSTED, EventBus
from orbynth.llm.providers import (
    LLMRequest,
    Message,
    ProviderError,
    ProviderRegistry,
    TransportResponse,
    classify_status,
)
from orbynth.llm.quotas import QuotaManager
from orbynth.llm.runtime import AllModelsUnavailable, ModelRuntime, Requirements
from orbynth.memory import MemoryStore


class FakeTransport:
    """Routes requests by URL so each provider can be scripted independently."""

    def __init__(self, routes: dict[str, tuple[int, str]]):
        self.routes = routes
        self.requests: list[dict] = []

    def send(self, url, headers, body, timeout=120.0):
        self.requests.append({"url": url, "body": body.decode("utf-8", "replace")})
        for fragment, (status, payload) in self.routes.items():
            if fragment in url:
                return TransportResponse(status, {}, payload.encode("utf-8"))
        return TransportResponse(404, {}, b'{"error":{"message":"no route"}}')

    def stream(self, url, headers, body, timeout=300.0):
        self.requests.append({"url": url, "body": body.decode("utf-8", "replace")})
        for fragment, (status, payload) in self.routes.items():
            if fragment in url:
                return status, {}, iter([payload.encode("utf-8")])
        return 404, {}, iter([b'{"error":{"message":"no route"}}'])


def openai_ok(content="done", model="spare-model"):
    return json.dumps(
        {
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
            "model": model,
        }
    )


class QuotaTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.bus = EventBus()
        self.events = []
        self.bus.subscribe("*", self.events.append)

    def test_request_per_minute_is_enforced(self):
        config = AppConfig(home=self.home, providers=[ProviderConfig(name="p", kind="echo", models=[ModelSpec(model_id="m", rpm=2)])])
        quotas = QuotaManager(config, self.bus)
        quotas.record("p:m")
        quotas.record("p:m")
        decision = quotas.check("p:m")
        self.assertFalse(decision.allowed)
        self.assertIn("rpm", decision.reason)
        self.assertTrue(any(event.topic == QUOTA_EXHAUSTED for event in self.events))

    def test_token_per_minute_counts_against_requests(self):
        config = AppConfig(home=self.home, providers=[ProviderConfig(name="p", kind="echo", models=[ModelSpec(model_id="m", rpm=100, tpm=1000)])])
        quotas = QuotaManager(config, self.bus)
        quotas.record("p:m", prompt_tokens=800, completion_tokens=300)
        self.assertFalse(quotas.check("p:m").allowed)

    def test_daily_limit_persists_across_restart(self):
        path = os.path.join(self.home, "quota.json")
        config = AppConfig(home=self.home, providers=[ProviderConfig(name="p", kind="echo", models=[ModelSpec(model_id="m", rpd=3)])])
        first = QuotaManager(config, self.bus, persist_path=path)
        for _ in range(3):
            first.record("p:m")
        second = QuotaManager(config, self.bus, persist_path=path)
        self.assertEqual(second.snapshot()[0]["rpd"]["used"], 3)
        self.assertFalse(second.check("p:m").allowed)

    def test_rate_limit_error_sets_cooldown_and_honours_retry_after(self):
        config = AppConfig(home=self.home, providers=[ProviderConfig(name="p", kind="echo", models=[ModelSpec(model_id="m", rpm=100)])])
        quotas = QuotaManager(config, self.bus)
        cooldown = quotas.note_error("p:m", "rate_limit", retry_after=45.0)
        self.assertAlmostEqual(cooldown, 45.0, delta=1.0)
        self.assertEqual(quotas.check("p:m").reason, "cooldown")
        self.assertEqual(quotas.snapshot()[0]["status"], "cooling")

    def test_escalating_backoff_without_retry_after(self):
        config = AppConfig(home=self.home, providers=[ProviderConfig(name="p", kind="echo", models=[ModelSpec(model_id="m")])])
        quotas = QuotaManager(config, self.bus)
        first = quotas.note_error("p:m", "rate_limit")
        second = quotas.note_error("p:m", "rate_limit")
        self.assertGreater(second, first)

    def test_headroom_ranking_prefers_the_unused_model(self):
        config = AppConfig(
            home=self.home,
            providers=[
                ProviderConfig(name="busy", kind="echo", models=[ModelSpec(model_id="m", rpm=10)]),
                ProviderConfig(name="idle", kind="echo", models=[ModelSpec(model_id="m", rpm=10)]),
            ],
        )
        quotas = QuotaManager(config, self.bus)
        for _ in range(9):
            quotas.record("busy:m")
        ranked = quotas.ranked_by_headroom(["busy:m", "idle:m"])
        self.assertEqual(ranked[0][0], "idle:m")

    def test_error_classification(self):
        self.assertEqual(classify_status(429, "rate limit exceeded").kind, "rate_limit")
        self.assertEqual(classify_status(429, '{"error":{"message":"quota exhausted"}}').kind, "quota")
        self.assertEqual(classify_status(401, "bad key").kind, "auth")
        self.assertEqual(classify_status(503, "upstream").kind, "server")
        self.assertTrue(classify_status(503, "").retryable)


class RuntimeFailoverTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.bus = EventBus()
        self.events = []
        self.bus.subscribe("*", self.events.append)
        self.memory = MemoryStore(":memory:")

    def _runtime(self, routes):
        config = AppConfig(
            home=self.home,
            providers=[
                ProviderConfig(
                    name="primary",
                    kind="openai_compatible",
                    base_url="https://primary.local/v1",
                    priority=1,
                    models=[ModelSpec(model_id="p-model", tags=["fast"], rpm=60)],
                ),
                ProviderConfig(
                    name="backup",
                    kind="openai_compatible",
                    base_url="https://backup.local/v1",
                    priority=2,
                    models=[ModelSpec(model_id="b-model", tags=["fast"], rpd=100)],
                ),
            ],
        )
        transport = FakeTransport(routes)
        registry = ProviderRegistry(config.providers, Secrets(self.home), self.bus, transport=transport)
        quotas = QuotaManager(config, self.bus)
        runtime = ModelRuntime(config, registry, quotas, self.memory, self.bus)
        return runtime, quotas, registry

    def test_transport_failure_moves_to_the_fallback_model(self):
        runtime, quotas, _ = self._runtime(
            {
                "primary.local": (429, '{"error":{"message":"quota exceeded for this project"}}'),
                "backup.local": (200, openai_ok("fallback reply")),
            }
        )
        result = runtime.run(LLMRequest(messages=[Message(role="user", content="hi")], model=""), agent_id="a1")
        self.assertEqual(result.model, "backup:b-model")
        self.assertEqual(result.response.text, "fallback reply")
        self.assertTrue(result.fell_back)
        self.assertTrue(any(event.topic == MODEL_FALLBACK for event in self.events))
        self.assertTrue(any(event.topic == PROVIDER_ERROR for event in self.events))
        # The failing provider is now cooling down rather than being hammered.
        self.assertEqual(quotas.check("primary:p-model").reason, "cooldown")

    def test_quota_exhaustion_skips_without_calling_the_provider(self):
        runtime, quotas, registry = self._runtime({"backup.local": (200, openai_ok())})
        for _ in range(60):
            quotas.record("primary:p-model")
        result = runtime.run(LLMRequest(messages=[Message(role="user", content="hi")], model=""), agent_id="a1")
        self.assertEqual(result.model, "backup:b-model")
        attempted = [call["url"] for call in registry.transport.requests]  # type: ignore[attr-defined]
        self.assertTrue(all("primary.local" not in url for url in attempted))

    def test_all_models_unavailable_is_reported_with_attempts(self):
        runtime, _, _ = self._runtime({"primary.local": (500, "boom"), "backup.local": (500, "boom")})
        with self.assertRaises(AllModelsUnavailable) as caught:
            runtime.run(LLMRequest(messages=[Message(role="user", content="hi")], model=""), agent_id="a1")
        self.assertEqual(len(caught.exception.attempts), 2)

    def test_requirements_filter_candidates(self):
        runtime, _, _ = self._runtime({})
        self.assertEqual(runtime.candidates(requirements=Requirements(tags=("vision",))), [])

    def test_transplant_keeps_identity_memory_and_transcript(self):
        runtime, _, _ = self._runtime({"backup.local": (200, openai_ok())})
        record = self.memory.register_agent("Operative", rank=5, role="operative", model="primary:p-model", capabilities=["filesystem.read"], team_id="team-1")
        self.memory.remember(record.agent_id, "portal needs 2FA on Mondays", importance=0.9)
        self.memory.append_message(record.agent_id, "user", "start the download")
        episodes_before = self.memory.stats(record.agent_id)["episodes"]

        moved = runtime.transplant(record.agent_id, "backup:b-model", reason="primary exhausted")

        self.assertEqual(moved["from"], "primary:p-model")
        self.assertEqual(moved["to"], "backup:b-model")
        self.assertTrue(moved["memory_preserved"])
        self.assertEqual(self.memory.get_agent(record.agent_id).model, "backup:b-model")
        self.assertEqual(self.memory.stats(record.agent_id)["episodes"], episodes_before + 1)
        self.assertEqual(self.memory.transcript_len(record.agent_id), 1)
        self.assertTrue(self.memory.read_board("team-1"))
        self.assertTrue(any(event.topic == MODEL_FALLBACK for event in self.events))

    def test_health_report_exposes_quota_and_readiness(self):
        runtime, quotas, _ = self._runtime({"backup.local": (200, openai_ok())})
        quotas.note_error("primary:p-model", "auth", detail="bad key")
        report = runtime.health_report()
        self.assertIn("primary:p-model", report["exhausted"])
        self.assertIn("backup:b-model", report["ready"])


class TransportUnavailableTests(unittest.TestCase):
    def test_provider_without_key_is_not_ready(self):
        home = tempfile.mkdtemp()
        config = AppConfig(
            home=home,
            providers=[
                ProviderConfig(
                    name="openai",
                    kind="openai_compatible",
                    base_url="https://api.openai.com/v1",
                    api_key_env="ORBYNTH_TEST_UNSET_KEY",
                    models=[ModelSpec(model_id="gpt-x")],
                )
            ],
        )
        registry = ProviderRegistry(config.providers, Secrets(home))
        self.assertFalse(registry.ready("openai"))
        runtime = ModelRuntime(config, registry, QuotaManager(config), None, EventBus())
        self.assertEqual(runtime.candidates(), [])

    def test_stack_without_any_model_reports_instead_of_crashing(self):
        stack = Stack()
        try:
            stack.providers.providers["echo"].enabled = False
            self.assertEqual(stack.runtime.candidates(), [])
        finally:
            stack.close()


if __name__ == "__main__":
    unittest.main()
