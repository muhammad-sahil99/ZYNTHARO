"""Roster tests: the caps the owner supplied, the job chains, and the honest live-transport gap.

The behaviour that would break if these failed: a tool loop being started on a 20-requests-a-day
model, a background indexer spending the only 20/day thinker, a voice chain that puts the
interpreter ahead of the speech-to-speech model, or ``doctor`` claiming the live socket exists.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from zyntharo.config import Secrets, default_config
from zyntharo.llm import roster
from zyntharo.llm.providers import ProviderRegistry
from zyntharo.llm.quotas import QuotaManager
from zyntharo.llm.runtime import ModelRuntime


class CatalogueTests(unittest.TestCase):
    def test_every_model_the_owner_listed_is_present(self):
        listed = {
            "gemini-3.8-flash",
            "antigravity",
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
            "gemini-3-flash-preview",
            "gemini-3.1-flash-lite",
            "gemini-3.6-flash",
            "gemini-3.5-flash-lite",
            "gemini-robotics-er2-preview",
            "gemma-4-26b-it",
            "gemma-4-31b-it",
            "gemini-2.5-flash-preview-tts",
            "gemini-3.1-flash-tts-preview",
            "gemini-3.5-transcribe",
            "gemini-3.8-flash-lite-tts",
            "gemini-3.8-flash-tts",
            "gemini-embedding-001",
            "gemini-embedding-2",
            "gemini-3-flash-live-preview",
        }
        missing = sorted(listed - set(roster.BY_ID))
        self.assertEqual([], missing)

    def test_ids_are_unique(self):
        ids = [entry.model_id for entry in roster.GEMINI]
        self.assertEqual(len(ids), len(set(ids)))

    def test_published_caps_are_recorded_verbatim(self):
        # The owner's list: 3.8 Flash is 5 rpm / 250K tpm / 20 rpd, Gemma 4 31B is 30 / 16K / 14,400.
        flash = roster.BY_ID["gemini-3.8-flash"]
        self.assertEqual((5, 250_000, 20), (flash.rpm, flash.tpm, flash.rpd))
        gemma = roster.BY_ID["gemma-4-31b-it"]
        self.assertEqual((30, 16_000, 14_400), (gemma.rpm, gemma.tpm, gemma.rpd))
        tts = roster.BY_ID["gemini-3.8-flash-tts"]
        self.assertEqual((3, 10_000, 10), (tts.rpm, tts.tpm, tts.rpd))
        embedding = roster.BY_ID["gemini-embedding-2"]
        self.assertEqual((100, 30_000, 1_000), (embedding.rpm, embedding.tpm, embedding.rpd))

    def test_live_models_are_flagged_and_only_those(self):
        live = {e.model_id for e in roster.GEMINI if e.audio_live}
        self.assertEqual(5, len(live))
        self.assertIn("gemini-2.5-flash-preview-native-audio-dialog", live)
        # A text model must never be offered as a live voice model.
        self.assertNotIn("gemini-3.8-flash", live)

    def test_non_chat_models_declare_no_tools(self):
        for entry in roster.GEMINI:
            if "tts" in entry.tags or "embedding" in entry.tags or "transcribe" in entry.tags:
                self.assertFalse(entry.tools, entry.model_id)
                self.assertFalse(entry.vision, entry.model_id)

    def test_inferred_ids_are_marked_unconfirmed(self):
        # The Live API rows came as display names; saying otherwise would be a claim we cannot back.
        self.assertFalse(roster.BY_ID["gemini-3.8-live-preview"].id_confirmed)
        self.assertTrue(roster.BY_ID["gemini-3.8-flash"].id_confirmed)
        self.assertIn("gemini:gemini-3.8-live-preview", roster.summary()["unconfirmed_ids"])


class ProviderExpansionTests(unittest.TestCase):
    """The reference sheet's six providers: caps recorded, activation by key, no invented limits."""

    def test_every_provider_from_the_sheet_is_declared(self):
        for name in ("gemini", "openrouter", "groq", "cerebras", "mistral", "cloudflare"):
            self.assertIn(name, roster.PROVIDER_BY_NAME)

    def test_free_tier_caps_are_recorded_verbatim(self):
        self.assertEqual((10, 40_000), (roster.BY_QUALIFIED["openrouter:deepseek/deepseek-r1:free"].rpm, roster.BY_QUALIFIED["openrouter:deepseek/deepseek-r1:free"].tpm))
        groq = roster.BY_QUALIFIED["groq:llama-3.3-70b-versatile"]
        self.assertEqual((30, 6_000, 1_000), (groq.rpm, groq.tpm, groq.rpd))
        cerebras = roster.BY_QUALIFIED["cerebras:llama3.1-8b"]
        self.assertEqual((30, 60_000), (cerebras.rpm, cerebras.tpm))
        self.assertEqual(1, roster.BY_QUALIFIED["mistral:mistral-large-latest"].rpm)

    def test_cloudflare_records_no_invented_request_rate(self):
        for entry in roster.CLOUDFLARE:
            self.assertIsNone(entry.rpm)
            self.assertIsNone(entry.rpd)
            self.assertIn("neuron", entry.note)

    def test_qualified_ids_survive_colons_inside_model_ids(self):
        # "openrouter:deepseek/deepseek-r1:free" splits once, so the colon stays in the model id.
        provider, model_id = "openrouter:deepseek/deepseek-r1:free".split(":", 1)
        self.assertEqual("openrouter", provider)
        self.assertEqual("deepseek/deepseek-r1:free", model_id)
        self.assertIn("openrouter:deepseek/deepseek-r1:free", roster.BY_QUALIFIED)

    def test_cloudflare_is_disabled_without_an_account_id(self):
        import os
        from unittest import mock

        clean = {k: v for k, v in os.environ.items() if k != "CF_ACCOUNT_ID"}
        with mock.patch.dict(os.environ, clean, clear=True):
            cloudflare = roster.PROVIDER_BY_NAME["cloudflare"]
            self.assertFalse(cloudflare.enabled)
            self.assertEqual("", cloudflare.base_url)
            self.assertEqual("CF_ACCOUNT_ID", cloudflare.needs_extra)

    def test_cloudflare_builds_its_account_url_when_the_id_is_present(self):
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {"CF_ACCOUNT_ID": "acct123"}):
            # providers() is rebuilt from the environment, so the id is picked up here.
            rebuilt = {p.name: p for p in roster.providers()}["cloudflare"]
        self.assertTrue(rebuilt.enabled)
        self.assertIn("accounts/acct123", rebuilt.base_url)
        self.assertEqual("CLOUDFLARE_API_TOKEN", rebuilt.api_key_env)

    def test_a_key_activates_its_provider_with_no_config_change(self):
        config = default_config()

        class Keys:
            def __init__(self, present):
                self.present = set(present)

            def get(self, env_name: str = "", ref: str = "") -> str:
                return "key" if env_name in self.present else ""

        groq_only = ProviderRegistry(config.providers, Keys({"GROQ_API_KEY"}))
        self.assertTrue(groq_only.ready("groq"))
        self.assertFalse(groq_only.ready("mistral"))
        # and the same config with no keys at all still runs, on the offline provider
        nothing = ProviderRegistry(config.providers, Keys(set()))
        self.assertTrue(nothing.ready("echo"))
        self.assertFalse(nothing.ready("openrouter"))

    def test_chains_name_real_models_from_every_provider(self):
        for purpose in roster.PURPOSES.values():
            for qualified in purpose.chain:
                self.assertIn(qualified, roster.BY_QUALIFIED, f"{purpose.name} -> {qualified}")

    def test_free_tiers_serve_the_volume_jobs(self):
        # If a key is added, it must have somewhere to go; bulk/background name them.
        self.assertIn("groq:llama-3.1-8b-instant", roster.chain("bulk"))
        self.assertIn("cerebras:llama3.1-70b", roster.chain("background"))
        self.assertIn("openrouter:deepseek/deepseek-r1:free", roster.chain("reasoning"))

    def test_a_spent_model_is_demoted_not_dropped(self):
        config = default_config()

        class Keys:
            def get(self, env_name: str = "", ref: str = "") -> str:
                return "key"

        registry = ProviderRegistry(config.providers, Keys())
        runtime = ModelRuntime(config, registry, QuotaManager(config))
        first = roster.chain("bulk")[0]
        quota = runtime.quotas.models[first]
        quota.requests_day.extend([runtime.quotas.clock()] * (quota.rpd or 1))
        ordered = runtime.for_purpose("bulk")
        self.assertNotEqual(first, ordered[0], "a spent model was still chosen first")
        self.assertIn(first, ordered, "a spent model was dropped instead of demoted")
        # Demoted behind the next rung of its own chain, and still present as a last resort.
        # (It can still precede out-of-band models from other providers, which are fallbacks of
        # last resort by rank; what matters is that the chain's second choice now goes first.)
        second = roster.chain("bulk")[1]
        self.assertLess(ordered.index(second), ordered.index(first))


class PurposeTests(unittest.TestCase):
    def test_every_chain_names_real_models(self):
        for purpose in roster.PURPOSES.values():
            for qualified in purpose.chain:
                self.assertIn(qualified, roster.BY_QUALIFIED, f"{purpose.name} -> {qualified}")

    def test_every_distribution_role_maps_to_a_known_purpose(self):
        for role, purpose in roster.ROLE_PURPOSE.items():
            self.assertIn(purpose, roster.PURPOSES, role)

    def test_unknown_purpose_raises_instead_of_returning_nothing(self):
        with self.assertRaises(KeyError):
            roster.chain("telepathy")

    def test_voice_chain_starts_with_true_speech_to_speech(self):
        first = roster.chain("voice_live")[0]
        self.assertEqual("gemini:gemini-2.5-flash-preview-native-audio-dialog", first)
        # and the interpreter is last, because it does not converse
        self.assertEqual("gemini:gemini-3.5-live-translate-preview", roster.chain("voice_live")[-1])

    def test_voice_transport_is_declared_unimplemented(self):
        # The models exist; this build has no BidiGenerateContent socket. Both facts must show.
        self.assertFalse(roster.PURPOSES["voice_live"].transport.implemented)
        self.assertEqual(5, len(roster.chain("voice_live")))
        self.assertFalse(roster.summary()["live_transport_implemented"])

    def test_speech_chain_is_newest_first(self):
        self.assertEqual("gemini:gemini-3.8-flash-tts", roster.chain("speech")[0])

    def test_tool_loop_starts_on_a_high_throughput_model(self):
        first = roster.chain("tool_loop")[0].split(":", 1)[1]
        self.assertGreaterEqual(roster.BY_ID[first].rpm or 0, 15)

    def test_background_and_bulk_never_touch_a_scarce_model(self):
        # A published daily cap must be generous; no published cap at all (Cerebras, Cloudflare)
        # is not scarcity — the Steward paces on what is published and nothing here invented a
        # limit the vendor never stated.
        for purpose in ("background", "bulk"):
            for qualified in roster.chain(purpose):
                model = roster.BY_QUALIFIED[qualified]
                if model.rpd is None:
                    continue
                self.assertGreaterEqual(model.rpd, 500, f"{purpose} spent {qualified}")

    def test_no_chain_repeats_a_model(self):
        for purpose in roster.PURPOSES.values():
            self.assertEqual(len(purpose.chain), len(set(purpose.chain)), purpose.name)

    def test_aggressive_fallbacks_are_distinct_models(self):
        # "then next, then another" only means something if the rungs are different models.
        for purpose in roster.PURPOSES.values():
            if len(purpose.chain) > 1:
                self.assertNotEqual(purpose.chain[0], purpose.chain[1], purpose.name)


class ConfigWiringTests(unittest.TestCase):
    def test_default_config_carries_the_whole_roster(self):
        config = default_config()
        provider = config.provider("gemini")
        self.assertIsNotNone(provider)
        self.assertEqual(len(roster.GEMINI), len(provider.models))
        self.assertEqual("GEMINI_API_KEY", provider.api_key_env)
        # Priority 10 keeps Gemini ahead of the placeholder "custom" provider (50) and behind
        # nothing else, while the offline echo stub stays last at 999.
        self.assertEqual(10, provider.priority)

    def test_agents_start_on_their_own_jobs_first_model(self):
        config = default_config()
        by_role = {agent.role: agent.model for agent in config.roster}
        self.assertEqual(roster.chain("conversation")[0], by_role["sovereign"])
        self.assertEqual(roster.chain("management")[0], by_role["steward"])
        self.assertEqual(roster.chain("coding")[0], by_role["artificer"])

    def test_registry_is_not_ready_without_the_key(self):
        config = default_config()
        with tempfile.TemporaryDirectory() as home:
            for env in ("GEMINI_API_KEY",):
                import os

                os.environ.pop(env, None)
            registry = ProviderRegistry(config.providers, Secrets(Path(home)))
            self.assertFalse(registry.ready("gemini"))
            self.assertTrue(registry.ready("echo"))

    def test_runtime_prefers_the_purpose_chain_then_the_rest(self):
        config = default_config()

        class Key:
            def get(self, env_name: str = "", ref: str = "") -> str:
                return "test-key"

        registry = ProviderRegistry(config.providers, Key())
        runtime = ModelRuntime(config, registry, QuotaManager(config))
        chain = runtime.for_purpose("coding")
        self.assertEqual(roster.chain("coding")[0], chain[0])
        # every roster model that can serve the job appears exactly once
        self.assertEqual(len(chain), len(set(chain)))
        self.assertIn(roster.chain("coding")[1], chain)


class ProbeTests(unittest.TestCase):
    def test_probe_without_a_key_says_so_and_calls_nothing(self):
        result = roster.probe(api_key="")
        self.assertFalse(result["ok"])
        self.assertIn("GEMINI_API_KEY", result["detail"])

    def test_probe_diffs_the_account_against_the_roster(self):
        body = json.dumps(
            {
                "models": [
                    {"name": "models/gemini-3.8-flash"},
                    {"name": "models/brand-new-model"},
                ]
            }
        ).encode()

        def fetch(url, headers, timeout):
            self.assertIn("key=abc", url)
            return 200, body

        result = roster.probe(api_key="abc", fetch=fetch)
        self.assertTrue(result["ok"])
        self.assertIn("brand-new-model", result["unlisted"])
        self.assertIn("gemma-4-31b-it", result["missing"])
        self.assertNotIn("gemini-3.8-flash", result["missing"])

    def test_probe_reports_a_rejected_key_instead_of_raising(self):
        def fetch(url, headers, timeout):
            return 403, b'{"error": "denied"}'

        result = roster.probe(api_key="bad", fetch=fetch)
        self.assertFalse(result["ok"])
        self.assertIn("403", result["detail"])


class SpeechChainTests(unittest.TestCase):
    """The Gemini TTS rung: 24 kHz in, 16 kHz frames out, and a refusal that says why."""

    def test_decodes_inline_audio_and_its_rate(self):
        import base64

        from zyntharo.voice.tts import _decode_gemini_audio

        pcm = b"\x01\x00" * 240
        payload = json.dumps(
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "inlineData": {
                                        "data": base64.b64encode(pcm).decode(),
                                        "mimeType": "audio/L16;rate=24000",
                                    }
                                }
                            ]
                        }
                    }
                ]
            }
        ).encode()
        audio, rate = _decode_gemini_audio(payload)
        self.assertEqual(pcm, audio)
        self.assertEqual(24_000, rate)

    def test_a_blocked_response_raises_with_the_reason(self):
        from zyntharo.voice.tts import _decode_gemini_audio

        with self.assertRaises(RuntimeError) as caught:
            _decode_gemini_audio(json.dumps({"promptFeedback": {"blockReason": "SAFETY"}}).encode())
        self.assertIn("SAFETY", str(caught.exception))

    def test_resampling_preserves_duration_not_sample_count(self):
        import array

        from zyntharo.voice.tts import _pcm_resample

        source = array.array("h", [100] * 2400)  # 100 ms at 24 kHz
        out = _pcm_resample(source.tobytes(), 24_000, 16_000)
        samples = array.array("h")
        samples.frombytes(out)
        self.assertEqual(1600, len(samples))
        # a constant signal stays constant: no clipping, no silence, no swap of byte order
        self.assertEqual({100}, set(samples))

    def test_the_engine_names_the_missing_key_instead_of_pretending(self):
        import os

        from zyntharo.voice.tts import GeminiTTS

        saved = os.environ.pop("GEMINI_API_KEY", None)
        try:
            usable, detail = GeminiTTS(object()).ready()
        finally:
            if saved:
                os.environ["GEMINI_API_KEY"] = saved
        self.assertFalse(usable)
        self.assertIn("GEMINI_API_KEY", detail)


class ManagementChainTests(unittest.TestCase):
    """The manager's own fallback: never a single point of failure, honest about its limits."""

    def test_the_steward_runs_the_management_chain(self):
        self.assertEqual("management", roster.ROLE_PURPOSE["steward"])

    def test_the_plan_lists_every_rung_even_with_no_keys(self):
        config = default_config()
        # No key at all: the ladder must still be visible, with each rung marked unready, and the
        # chosen rung must honestly be the stub rather than a model that cannot be called.
        runtime = ModelRuntime(config, ProviderRegistry(config.providers), QuotaManager(config))
        plan = runtime.manager_plan()
        self.assertEqual(len(roster.chain("management")), len(plan["chain"]))
        self.assertEqual("echo:offline-echo", plan["first"])
        self.assertFalse(any(row["ready"] for row in plan["limits"] if not row["model"].startswith("echo:")))

    def test_the_chain_spans_providers_and_ends_keyless(self):
        chain = roster.chain("management")
        self.assertGreaterEqual(len({q.split(":", 1)[0] for q in chain}), 5)
        self.assertEqual("echo:offline-echo", chain[-1])
        self.assertIn("echo:offline-echo", roster.BY_QUALIFIED)

    def test_the_chain_never_rests_on_a_scarce_model(self):
        for qualified in roster.chain("management"):
            self.assertGreaterEqual(roster.BY_QUALIFIED[qualified].rpd or 1_000, 100, qualified)

    def test_manager_plan_reports_its_own_headroom_and_order(self):
        config = default_config()

        class Keys:
            def get(self, env_name: str = "", ref: str = "") -> str:
                return "key"

        runtime = ModelRuntime(config, ProviderRegistry(config.providers, Keys()), QuotaManager(config))
        plan = runtime.manager_plan()
        self.assertEqual("management", plan["purpose"])
        self.assertTrue(plan["last_resort_is_keyless"])
        self.assertEqual(plan["chain"][0], plan["first"])  # with every key present, rung one runs
        self.assertEqual(len(plan["chain"]), len(plan["limits"]))
        self.assertTrue(all("headroom" in row for row in plan["limits"]))
        self.assertGreaterEqual(len(plan["chain"]), 8)
        self.assertEqual("echo:offline-echo", plan["chain"][-1])

    def test_a_drained_manager_model_moves_to_the_back_but_stays(self):
        config = default_config()

        class Keys:
            def get(self, env_name: str = "", ref: str = "") -> str:
                return "key"

        runtime = ModelRuntime(config, ProviderRegistry(config.providers, Keys()), QuotaManager(config))
        first = roster.chain("management")[0]
        quota = runtime.quotas.models[first]
        quota.requests_day.extend([runtime.quotas.clock()] * (quota.rpd or 1))
        plan = runtime.manager_plan()
        self.assertNotEqual(first, plan["first"], "a drained manager model was still chosen first")
        self.assertIn(first, plan["chain"], "a drained manager model was dropped instead of demoted")
        self.assertEqual("echo:offline-echo", plan["terminal"])


class RankTests(unittest.TestCase):
    """Size ranks: derived from published sizes, declared where they are not, and used to assign."""

    def test_rank_follows_the_published_parameter_count(self):
        self.assertEqual(1, roster.BY_QUALIFIED["groq:llama-3.1-8b-instant"].rank)   # 8B
        self.assertEqual(2, roster.BY_QUALIFIED["gemini:gemma-4-31b-it"].rank)      # 31B
        self.assertEqual(3, roster.BY_QUALIFIED["groq:llama-3.3-70b-versatile"].rank)  # 70B
        self.assertEqual(4, roster.BY_QUALIFIED["openrouter:nvidia/nemotron-4-340b-instruct:free"].rank)

    def test_an_unpublished_size_is_a_declared_class_and_says_so(self):
        flash = roster.BY_QUALIFIED["gemini:gemini-3.8-flash"]
        self.assertTrue(flash.rank_is_declared)
        self.assertIsNone(flash.size_b)
        self.assertEqual(3, flash.rank)
        # a published size is a datum, not a declaration
        self.assertFalse(roster.BY_QUALIFIED["gemini:gemma-4-31b-it"].rank_is_declared)

    def test_rank_boundaries(self):
        self.assertEqual(0, roster.rank_for_size(3.9))
        self.assertEqual(1, roster.rank_for_size(4))
        self.assertEqual(1, roster.rank_for_size(9))
        self.assertEqual(2, roster.rank_for_size(9.1))
        self.assertEqual(3, roster.rank_for_size(99))
        self.assertEqual(4, roster.rank_for_size(100))
        self.assertIsNone(roster.rank_for_size(None))

    def test_every_job_declares_a_band(self):
        for purpose in roster.PURPOSES.values():
            low, high = purpose.rank_band
            self.assertGreaterEqual(low, 0)
            self.assertLessEqual(high, 4)
            self.assertLessEqual(low, high, purpose.name)

    def test_bulk_work_cannot_start_on_a_frontier_model(self):
        for qualified in roster.ranked_chain("bulk"):
            self.assertLessEqual(roster.BY_QUALIFIED[qualified].rank, 2, qualified)

    def test_reasoning_puts_in_band_models_before_small_ones(self):
        chain = roster.ranked_chain("reasoning")
        r1 = chain.index("openrouter:deepseek/deepseek-r1:free")       # 671B, rank 4
        small = chain.index("gemini:gemini-3-flash-preview")           # declared rank 2
        self.assertLess(r1, small)
        self.assertTrue(all(roster.BY_QUALIFIED[q].rank >= 3 for q in chain[:4]))

    def test_the_manager_band_covers_its_own_first_rung(self):
        first = roster.chain("management")[0]
        self.assertTrue(roster.PURPOSES["management"].rank_fits(roster.BY_QUALIFIED[first].rank))

    def test_runtime_orders_by_band_then_quota_and_keeps_the_stub_last(self):
        config = default_config()

        class Keys:
            def get(self, env_name: str = "", ref: str = "") -> str:
                return "key"

        runtime = ModelRuntime(config, ProviderRegistry(config.providers, Keys()), QuotaManager(config))
        reasoning = runtime.for_purpose("reasoning")
        self.assertEqual("gemini:gemini-3.8-flash", reasoning[0])
        self.assertLess(
            reasoning.index("openrouter:deepseek/deepseek-r1:free"),
            reasoning.index("gemini:gemini-3-flash-preview"),
            "an out-of-band rung was tried before an in-band one",
        )
        management = runtime.for_purpose("management")
        self.assertEqual("echo:offline-echo", management[-1], "the stub is a floor, not a contender")
        self.assertEqual("gemini:antigravity", management[0])


class SecondSheetProviderTests(unittest.TestCase):
    """The second sheet: recorded caps, and env-only endpoints named instead of guessed."""

    def test_every_provider_is_declared(self):
        for name in ("tokenharbor", "sambanova", "llm7", "teamorouter", "ovh", "deepseek"):
            self.assertIn(name, roster.PROVIDER_BY_NAME)

    def test_caps_are_recorded_verbatim(self):
        self.assertEqual(60, roster.BY_QUALIFIED["tokenharbor:deepseek-v4.1-flash:free"].rpm)
        samba = roster.BY_QUALIFIED["sambanova:DeepSeek-V3.1"]
        self.assertEqual((20, 20), (samba.rpm, samba.rpd))
        self.assertEqual(30, roster.BY_QUALIFIED["llm7:deepseek-r1"].rpm)
        self.assertEqual(200, roster.BY_QUALIFIED["teamorouter:deepseek-v4-flash-free"].rpd)
        self.assertEqual(12, roster.BY_QUALIFIED["ovh:deepseek-v3"].rpm)

    def test_an_unpublished_rate_is_left_unpublished(self):
        for entry in roster.DEEPSEEK:
            self.assertIsNone(entry.rpm)
            self.assertIn("no rate limit published", entry.note)

    def test_env_only_endpoints_are_disabled_with_the_variable_named(self):
        from unittest import mock
        with mock.patch.dict("os.environ", {}, clear=True):
            rebuilt = {p.name: p for p in roster.providers()}
        self.assertFalse(rebuilt["tokenharbor"].usable)
        self.assertEqual("TOKEN_HARBOR_BASE_URL", rebuilt["tokenharbor"].base_url_env)
        self.assertFalse(rebuilt["teamorouter"].usable)
        self.assertFalse(rebuilt["ovh"].usable)
        self.assertEqual("", rebuilt["tokenharbor"].resolved_base_url)

    def test_an_env_endpoint_makes_the_provider_usable(self):
        from unittest import mock
        with mock.patch.dict("os.environ", {"OVH_AI_BASE_URL": "https://example.invalid/v1"}):
            ovh = {p.name: p for p in roster.providers()}["ovh"]
            self.assertTrue(ovh.usable)
            self.assertEqual("", ovh.api_key_env)  # the anonymous tier declares no key, by design
            self.assertEqual("https://example.invalid/v1", ovh.config().base_url)


if __name__ == "__main__":
    unittest.main()
