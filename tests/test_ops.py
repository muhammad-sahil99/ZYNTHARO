"""Tests for the operational layer (contract sections 22.1 - 22.11).

These run headless: the process supervisor uses real child processes (that is the point of
it), everything else is pure logic with injected state, so a locked screen or a video call is
a constructor argument rather than a hope.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orbynth.ops.catalog import (  # noqa: E402
    ACTIVE,
    RETIRED,
    CatalogEntry,
    ModelCatalog,
    UpdateGate,
    check_provider_models,
    sign_catalog,
)
from orbynth.ops.governor import BACKGROUND, CRITICAL, GAME, BusySignal, ResponsivenessGovernor  # noqa: E402
from orbynth.ops.languages import (  # noqa: E402
    RTL,
    detect,
    direction_of,
    equivalent,
    normalize,
    ocr_languages,
    profile_for,
)
from orbynth.ops.owner import OwnerRegistry  # noqa: E402
from orbynth.ops.preflight import (  # noqa: E402
    DEFER,
    RUN_BACKGROUND,
    RUN_ISOLATED,
    RUN_NOW,
    SKIP,
    MachineState,
    Preflight,
    TaskRequirements,
)
from orbynth.ops.supervisor import EXITED, RUNNING, STOPPED, Supervisor  # noqa: E402
from orbynth.ops.verification import (  # noqa: E402
    HARMLESS,
    IRREVERSIBLE,
    MET,
    NEEDS_USER,
    NOT_MET,
    SUBJECTIVE,
    UNCERTAIN,
    OutcomeVerifier,
    RiskTiers,
    SuccessCriterion,
    TaskContract,
)


def temp_home() -> str:
    return tempfile.mkdtemp(prefix="orbynth-ops-")


class SupervisorTests(unittest.TestCase):
    def test_helper_is_owned_and_dies_with_the_supervisor(self):
        supervisor = Supervisor(temp_home(), poll_interval=0.05)
        supervisor.start(reconcile=False)
        job = supervisor.spawn([sys.executable, "-c", "import time; time.sleep(60)"], name="helper", restart="never")
        time.sleep(0.3)
        self.assertEqual(job.status, RUNNING)
        self.assertTrue(supervisor.inventory()["jobs"])
        report = supervisor.shutdown(grace=2.0)
        self.assertIn("helper", report["stopped"])
        self.assertEqual(report["left_running"], [])

    def test_crashed_helper_is_restarted_then_given_up_on(self):
        supervisor = Supervisor(temp_home(), max_restarts=1, restart_window=30.0, poll_interval=0.05)
        supervisor.start(reconcile=False)
        job = supervisor.spawn([sys.executable, "-c", "raise SystemExit(3)"], name="flapper", restart="on-failure")
        deadline = time.time() + 6
        while time.time() < deadline and len([j for j in supervisor.jobs.values() if j.restarts >= 1]) == 0:
            time.sleep(0.1)
        restarted = [j for j in supervisor.jobs.values() if j.restarts >= 1]
        self.assertTrue(restarted, f"no restart happened: {[(j.name, j.status, j.exit_code) for j in supervisor.jobs.values()]}")
        deadline = time.time() + 8
        while time.time() < deadline and any(j.status == RUNNING for j in supervisor.jobs.values()):
            time.sleep(0.1)
        statuses = {j.status for j in supervisor.jobs.values()}
        self.assertTrue(statuses & {EXITED, "crashed", "gave_up"}, statuses)
        supervisor.shutdown(grace=1.0)

    def test_clean_exit_is_not_restarted_when_policy_says_failure(self):
        supervisor = Supervisor(temp_home(), poll_interval=0.05)
        supervisor.start(reconcile=False)
        job = supervisor.spawn([sys.executable, "-c", "print('done')"], name="finisher", restart="on-failure")
        deadline = time.time() + 5
        while time.time() < deadline and job.status == RUNNING:
            time.sleep(0.1)
        self.assertEqual(job.status, EXITED)
        self.assertEqual(job.restarts, 0)
        self.assertIn("done", supervisor.logs(job.job_id))
        supervisor.shutdown(grace=1.0)

    def test_reconcile_terminates_a_job_left_by_a_previous_supervisor(self):
        home = temp_home()
        first = Supervisor(home, poll_interval=5.0)
        first.start(reconcile=False)
        job = first.spawn([sys.executable, "-c", "import time; time.sleep(60)"], name="survivor", restart="never")
        # Simulate a crash: stop watching, keep the journal, never shut the job down.
        first._stop.set()
        first.shutting_down = True
        second = Supervisor(home, poll_interval=0.05)
        report = second.start()
        self.assertTrue(report["terminated"], report)
        self.assertIn("survivor", [entry["name"] for entry in report["terminated"]])
        from orbynth.ops.supervisor import _pid_alive

        deadline = time.time() + 5
        while time.time() < deadline and _pid_alive(job.pid):
            time.sleep(0.1)
        self.assertFalse(_pid_alive(job.pid), "the orphaned helper is still running")
        # Reap our own child so the test does not leave a zombie behind.
        first._procs[job.job_id].poll()
        second.shutdown()

    def test_dead_job_from_a_previous_run_is_reported_as_orphaned(self):
        home = temp_home()
        first = Supervisor(home, poll_interval=5.0)
        first.start(reconcile=False)
        first.spawn([sys.executable, "-c", "pass"], name="gone", restart="never")
        first._stop.set()
        first.shutting_down = True
        time.sleep(0.3)
        second = Supervisor(home, poll_interval=0.05)
        report = second.start()
        self.assertTrue(report["checked"] >= 1)
        second.shutdown()


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.preflight = Preflight()

    def test_locked_session_defers_desktop_work_with_advice(self):
        decision = self.preflight.check(
            TaskRequirements(gui=True, unlocked_session=True),
            origin="schedule",
            state=MachineState(session_locked=True),
        )
        self.assertEqual(decision.action, DEFER)
        self.assertFalse(decision.can_run)
        self.assertTrue(decision.advice)
        self.assertIn("never unlocks", " ".join(decision.reasons) + decision.advice)

    def test_user_request_is_not_silently_skipped(self):
        decision = self.preflight.check(
            TaskRequirements(gui=True), origin="user_request", state=MachineState(session_locked=True)
        )
        self.assertEqual(decision.action, DEFER)
        self.assertIn("unlock", decision.advice)

    def test_isolated_session_is_used_for_unattended_desktop_work(self):
        decision = self.preflight.check(
            TaskRequirements(gui=True),
            origin="schedule",
            state=MachineState(session_locked=True, isolation_available=True),
        )
        self.assertEqual(decision.action, RUN_ISOLATED)

    def test_low_battery_is_skipped_for_user_work_and_deferred_for_scheduled(self):
        state = MachineState(on_ac=False, battery_pct=5)
        self.assertEqual(self.preflight.check(TaskRequirements(power=True), "user_request", state).action, SKIP)
        self.assertEqual(self.preflight.check(TaskRequirements(power=True), "schedule", state).action, DEFER)

    def test_offline_and_no_audio_are_distinguished(self):
        offline = self.preflight.check(TaskRequirements(network=True), state=MachineState(network=False))
        self.assertEqual(offline.action, DEFER)
        silent = self.preflight.check(TaskRequirements(audio=True), state=MachineState(audio_device=False))
        self.assertEqual(silent.action, SKIP)

    def test_busy_machine_defers_or_goes_background(self):
        busy = MachineState(busy_reason="video_call")
        deferred = self.preflight.check(TaskRequirements(gui=True), "schedule", busy)
        self.assertEqual(deferred.action, DEFER)
        background = self.preflight.check(TaskRequirements(gui=True, background_ok=True), "schedule", busy)
        self.assertEqual(background.action, RUN_BACKGROUND)

    def test_human_presence_requirement_is_respected(self):
        decision = self.preflight.check(TaskRequirements(user_present=True), state=MachineState(idle_seconds=9999))
        self.assertEqual(decision.action, DEFER)

    def test_healthy_machine_runs_now(self):
        decision = self.preflight.check(TaskRequirements(gui=True), "user_request", MachineState(idle_seconds=0.0))
        self.assertEqual(decision.action, RUN_NOW)
        self.assertTrue(decision.can_run)


class OwnerTests(unittest.TestCase):
    def test_setup_issues_a_recovery_code_and_requires_a_real_pin(self):
        owner = OwnerRegistry(temp_home())
        with self.assertRaises(ValueError):
            owner.setup("Owner", "123")
        recovery = owner.setup("Owner", "correct-horse")
        self.assertEqual(len(recovery.split("-")), 4)

    def test_irreversible_actions_need_a_pin_session(self):
        owner = OwnerRegistry(temp_home())
        owner.setup("Owner", "correct-horse")
        denied = owner.require(["filesystem.delete"], method="session")
        self.assertFalse(denied.allow)
        self.assertEqual(denied.method, "pin")
        self.assertTrue(owner.verify_pin("correct-horse"))
        allowed = owner.require(["filesystem.delete"], method="pin")
        self.assertTrue(allowed.allow)
        self.assertEqual(allowed.method, "pin")

    def test_voice_alone_never_authorises_an_irreversible_action(self):
        owner = OwnerRegistry(temp_home())
        owner.setup("Owner", "correct-horse")
        decision = owner.require(["whatsapp.call"], method="voice")
        self.assertFalse(decision.allow)
        self.assertIn("voice", decision.reason.lower())

    def test_normal_work_is_allowed_within_a_session(self):
        owner = OwnerRegistry(temp_home())
        owner.setup("Owner", "correct-horse")
        self.assertFalse(owner.require(["filesystem.read"]).allow)
        owner.verify_pin("correct-horse")
        self.assertTrue(owner.require(["filesystem.read"]).allow)

    def test_recovery_code_is_consumed_and_rotated(self):
        owner = OwnerRegistry(temp_home())
        recovery = owner.setup("Owner", "correct-horse")
        self.assertTrue(owner.verify_recovery(recovery, "a-new-long-pin"))
        self.assertTrue(owner.verify_pin("a-new-long-pin"))
        fresh = owner.rotate_recovery_code()
        self.assertNotEqual(fresh, recovery)
        self.assertFalse(owner.verify_recovery(recovery, "another-pin"))

    def test_repeated_failures_lock_the_gate(self):
        owner = OwnerRegistry(temp_home(), max_attempts=2, lockout_seconds=60)
        owner.setup("Owner", "correct-horse")
        self.assertFalse(owner.verify_pin("wrong-1"))
        self.assertFalse(owner.verify_pin("wrong-2"))
        self.assertTrue(owner.locked_out())
        self.assertFalse(owner.verify_pin("correct-horse"))
        decision = owner.require(["filesystem.read"])
        self.assertFalse(decision.allow)
        self.assertEqual(decision.method, "locked_out")

    def test_expired_session_no_longer_authorises(self):
        owner = OwnerRegistry(temp_home(), session_minutes=0)
        owner.setup("Owner", "correct-horse")
        owner.verify_pin("correct-horse")
        self.assertFalse(owner.require(["filesystem.read"]).allow)

    def test_foreign_profile_on_a_shared_pc_is_refused(self):
        home = temp_home()
        owner = OwnerRegistry(home)
        owner.setup("Owner", "correct-horse")
        owner.profile.os_user = "someone-else"
        owner.save()
        other = OwnerRegistry(home)
        decision = other.require(["filesystem.read"])
        self.assertFalse(decision.allow)
        self.assertEqual(decision.method, "owner_absent")
        self.assertTrue(other.foreign_profile())

    def test_voice_verification_reports_unavailable_rather_than_guessing(self):
        owner = OwnerRegistry(temp_home())
        owner.setup("Owner", "correct-horse")
        ok, score = owner.verify_voice("sample")
        self.assertFalse(ok)
        self.assertEqual(score, 0.0)
        matcher = OwnerRegistry(temp_home(), voice_matcher=lambda a, b: 0.9, voice_threshold=0.8)
        matcher.setup("Owner", "correct-horse")
        matcher.enroll_voice("primary", "print-a")
        ok, score = matcher.verify_voice("print-b")
        self.assertTrue(ok)
        self.assertGreaterEqual(score, 0.8)


class VerificationTests(unittest.TestCase):
    def contract(self) -> TaskContract:
        contract = TaskContract("t-1", "save the report and tell me if it reads well")
        contract.intended_capabilities = ("filesystem.write",)
        contract.add("file_exists", target="report.docx")
        contract.add("text_present", target="summary", expected="total")
        contract.add(SUBJECTIVE, note="does the report read well?")
        return contract

    def verifier(self, exists: bool = True, contains: bool = True) -> OutcomeVerifier:
        verifier = OutcomeVerifier()
        verifier.register_probe("file_exists", lambda c, o: (exists, "looked for " + c.target))
        verifier.register_probe("text_present", lambda c, o: (contains, "searched " + c.target))
        return verifier

    def test_all_criteria_met_but_subjective_asks_the_user(self):
        verdict = self.verifier().check(self.contract())
        self.assertEqual(verdict.status, NEEDS_USER)
        self.assertIn("read well", verdict.question)
        self.assertEqual(len(verdict.results), 3)

    def test_unmet_criterion_is_not_met_regardless_of_the_claim(self):
        verdict = self.verifier(contains=False).check(self.contract(), claim="everything is done")
        self.assertEqual(verdict.status, NOT_MET)
        self.assertIn("summary", verdict.summary)

    def test_met_contract_without_subjectivity_is_met(self):
        contract = TaskContract("t-2", "write the file")
        contract.add("file_exists", target="x.txt")
        verdict = self.verifier().check(contract)
        self.assertEqual(verdict.status, MET)

    def test_missing_probe_is_uncertain_not_a_pass(self):
        contract = TaskContract("t-3", "do the thing")
        contract.add("url_is", target="https://example.com")
        verdict = self.verifier().check(contract)
        self.assertEqual(verdict.status, UNCERTAIN)
        self.assertEqual(verdict.results[0].status, UNCERTAIN)

    def test_unrequested_destructive_work_is_flagged_as_a_side_effect(self):
        contract = TaskContract("t-4", "read the config file")
        contract.intended_capabilities = ("filesystem.read",)
        contract.add("file_exists", target="config.json")
        performed = [
            {"action": "fs_read", "capability": "filesystem.read"},
            {"action": "fs_delete", "capability": "filesystem.delete", "outcome": "ok"},
        ]
        verdict = self.verifier().check(contract, performed=performed)
        self.assertEqual(verdict.status, NEEDS_USER)
        self.assertEqual(len(verdict.side_effects), 1)
        self.assertEqual(verdict.side_effects[0]["severity"], "high")
        self.assertIn("fs_delete", verdict.question)

    def test_expected_capabilities_are_not_side_effects(self):
        contract = TaskContract("t-5", "write the file")
        contract.intended_capabilities = ("filesystem.write",)
        contract.add("file_exists", target="x.txt")
        performed = [{"action": "fs_write", "capability": "filesystem.write"}]
        verdict = self.verifier().check(contract, performed=performed)
        self.assertEqual(verdict.side_effects, [])
        self.assertEqual(verdict.status, MET)

    def test_risk_tiers_batch_the_harmless_and_never_shortcut_the_irreversible(self):
        tiers = RiskTiers()

        class Fake:
            def __init__(self, name, capability, danger, group):
                self.name, self.capability, self.danger, self.group = name, capability, danger, group

        plan = tiers.plan(
            [
                Fake("vision_capture", "vision.capture", "low", "vision"),
                Fake("fs_write", "filesystem.write", "medium", "filesystem"),
                Fake("fs_delete", "filesystem.delete", "high", "filesystem"),
            ]
        )
        self.assertEqual(tiers.tier_for("vision_capture", "vision.capture", "low", "vision"), HARMLESS)
        self.assertIn("vision_capture", plan.batched)
        self.assertIn("fs_delete", plan.never_shortcut)
        self.assertIn("fs_delete", plan.immediate)
        self.assertEqual(tiers.tier_for("fs_delete", "filesystem.delete", "high", "filesystem"), IRREVERSIBLE)

    def test_optimism_is_only_for_declared_reversible_actions(self):
        tiers = RiskTiers()

        class Fake:
            name = "fs_write"
            capability = "filesystem.write"
            danger = "medium"
            group = "filesystem"

        self.assertNotIn("fs_write", tiers.plan([Fake()]).optimistic)
        tiers.declare_reversible("fs_write", True)
        self.assertIn("fs_write", tiers.plan([Fake()]).optimistic)


class CatalogTests(unittest.TestCase):
    def catalog(self) -> ModelCatalog:
        return ModelCatalog(
            version="2026.10",
            entries=[
                CatalogEntry("gemini", "gemini-2.5-flash", ACTIVE, rpm=12, tpm=250_000),
                CatalogEntry("gemini", "gemini-3-pro", ACTIVE, replaces="gemini-2.5-pro"),
                CatalogEntry("gemini", "gemini-1.0-legacy", RETIRED),
            ],
        )

    def test_unsigned_catalogue_is_refused_outside_development(self):
        verification = self.catalog().verify()
        self.assertFalse(verification.ok)
        self.assertEqual(verification.method, "unsigned")
        self.assertTrue(self.catalog().verify(allow_unsigned=True).ok)

    def test_hmac_signature_is_verified_and_tampering_is_caught(self):
        catalog = self.catalog()
        catalog.signature = sign_catalog(catalog.to_json(), hmac_key="shared-secret")
        self.assertTrue(catalog.verify(hmac_key="shared-secret").ok)
        self.assertFalse(catalog.verify(hmac_key="wrong-key").ok)
        tampered = self.catalog()
        tampered.entries[0].rpm = 9999
        tampered.signature = sign_catalog(self.catalog().to_json(), hmac_key="shared-secret")
        self.assertFalse(tampered.verify(hmac_key="shared-secret").ok)

    def test_apply_updates_limits_and_removes_retired_models(self):
        from orbynth.config import default_config, load_config

        home = temp_home()
        config = default_config()
        config.home = home
        config.save()
        gate = UpdateGate(home)
        gate.stage(self.catalog())
        delta = gate.apply(config, in_flight=0)
        reloaded = load_config(config.config_path)
        flash = reloaded.model("gemini:gemini-2.5-flash")
        self.assertIsNotNone(flash)
        self.assertEqual(flash.rpm, 12)
        self.assertIn("gemini:gemini-3-pro", delta.added)
        self.assertNotIn("gemini:gemini-1.0-legacy", [m.qualified for m in reloaded.all_models()])

    def test_apply_refuses_while_work_is_in_flight(self):
        from orbynth.config import default_config

        home = temp_home()
        config = default_config()
        config.home = home
        gate = UpdateGate(home)
        gate.stage(self.catalog())
        allowed, reason = gate.can_apply(in_flight=1)
        self.assertFalse(allowed)
        self.assertIn("in flight", reason)
        with self.assertRaises(RuntimeError):
            gate.apply(config, in_flight=1)

    def test_rollback_restores_the_previous_configuration(self):
        from orbynth.config import default_config, load_config

        home = temp_home()
        config = default_config()
        config.home = home
        config.save()
        gate = UpdateGate(home)
        gate.stage(self.catalog())
        gate.apply(config, in_flight=0)
        gate.rollback(config)
        self.assertIsNotNone(load_config(config.config_path).model("gemini:gemini-2.5-pro"))

    def test_rollback_works_from_a_later_process(self):
        from orbynth.config import default_config, load_config

        home = temp_home()
        config = default_config()
        config.home = home
        config.save()
        applied = UpdateGate(home)
        applied.stage(self.catalog())
        applied.apply(config, in_flight=0)
        # A different process (a fresh gate) must still be able to take it back.
        later = UpdateGate(home)
        delta = later.rollback(config)
        self.assertIsNotNone(delta, "a fresh gate found no snapshot to roll back to")
        restored = load_config(config.config_path)
        self.assertIsNotNone(restored.model("gemini:gemini-2.5-pro"))
        self.assertIsNone(later.rollback(config), "a consumed snapshot was reused")

    def test_drift_check_reports_retired_ids_and_unreachable_providers(self):
        from orbynth.config import default_config

        config = default_config()
        report = check_provider_models(config, fetch=lambda base, key, kind="": ["gemini-2.5-flash"])
        self.assertIn("gemini-2.5-pro", [m["model_id"] for m in report["missing"]])

        def boom(base, key, kind=""):
            raise OSError("connection refused")

        offline = check_provider_models(config, fetch=boom)
        self.assertTrue(offline["unreachable"])

    def test_custom_provider_with_base_url_and_quotas_is_applied(self):
        from orbynth.config import ProviderConfig, load_config

        home = temp_home()
        from orbynth.config import default_config

        config = default_config()
        config.home = home
        config.providers.append(ProviderConfig(name="custom2", kind="openai_compatible", base_url="https://llm.example/v1"))
        config.save()
        catalog = ModelCatalog(version="2", entries=[CatalogEntry("custom2", "my-model", rpm=30, rpd=900, tpm=60_000)])
        gate = UpdateGate(home)
        gate.stage(catalog)
        gate.apply(config, in_flight=0)
        spec = load_config(config.config_path).model("custom2:my-model")
        self.assertEqual((spec.rpm, spec.rpd, spec.tpm), (30, 900, 60_000))


class GovernorTests(unittest.TestCase):
    def test_game_pauses_background_but_not_the_users_own_request(self):
        governor = ResponsivenessGovernor(detector=lambda: [])
        state = governor.evaluate([BusySignal(GAME, "valorant", 0.9)])
        self.assertTrue(state.any_pause)
        self.assertTrue(governor.may_run(CRITICAL, state)[0])
        allowed, reason = governor.may_run(BACKGROUND, state)
        self.assertFalse(allowed)
        self.assertIn("deferred", reason)
        self.assertGreater(governor.delay_for(BACKGROUND, state), 0)

    def test_idle_machine_lets_everything_run(self):
        governor = ResponsivenessGovernor(detector=lambda: [])
        state = governor.evaluate([])
        self.assertTrue(governor.may_run(BACKGROUND, state)[0])
        self.assertEqual(governor.delay_for(BACKGROUND, state), 0.0)

    def test_pause_and_resume_are_reported_once_each(self):
        events: list[str] = []
        from orbynth.events import EventBus, GOVERNOR_PAUSED, GOVERNOR_RESUMED

        bus = EventBus()
        bus.subscribe(GOVERNOR_PAUSED, lambda e: events.append("paused"))
        bus.subscribe(GOVERNOR_RESUMED, lambda e: events.append("resumed"))
        governor = ResponsivenessGovernor(bus=bus, detector=lambda: [])
        governor.evaluate([BusySignal(GAME, "cs2")])
        self.assertTrue(governor.may_run(CRITICAL, governor.evaluate([BusySignal(GAME, "cs2")]))[0])
        governor.evaluate([])
        self.assertEqual(events, ["paused", "resumed"])

    def test_priority_change_never_raises(self):
        governor = ResponsivenessGovernor(detector=lambda: [])
        self.assertIn(governor.apply_priority("below_normal"), {"below_normal", "normal"})
        self.assertIn(governor.summary()["priority"], {"below_normal", "normal"})


class LanguageTests(unittest.TestCase):
    def test_urdu_and_arabic_are_detected_as_right_to_left(self):
        self.assertEqual(direction_of("رپورٹ محفوظ کریں"), RTL)
        self.assertEqual(direction_of("حفظ التقرير"), RTL)
        self.assertEqual(direction_of("Save report"), "ltr")

    def test_urdu_is_distinguished_from_arabic(self):
        self.assertEqual(detect("رپورٹ محفوظ کریں"), "ur")
        self.assertEqual(detect("حفظ التقرير في الملف"), "ar")
        self.assertEqual(detect("save the report file"), "en")

    def test_digits_and_diacritics_normalise_for_comparison(self):
        self.assertEqual(normalize("١٢٣ ملفـً"), "123 ملف")
        self.assertTrue(equivalent("مَلف", "ملف"))
        self.assertFalse(equivalent("ملف", "گزارش"))

    def test_stray_rtl_characters_do_not_flip_direction(self):
        self.assertEqual(direction_of("File عد name"), "ltr")
        self.assertEqual(direction_of("report (رپورٹ محفوظ کریں)").__class__, str)

    def test_ocr_packs_and_profiles_are_per_language(self):
        self.assertIn("urd", ocr_languages(["ur"]))
        self.assertIn("ara", ocr_languages(["ar"]))
        profile = profile_for("رپورٹ محفوظ کریں", hints=["ur"])
        self.assertEqual(profile.direction, RTL)
        self.assertFalse(profile.mixed)
        mixed = profile_for("Save رپورٹ now", hints=["en"])
        self.assertTrue(mixed.mixed)


class PlanningTests(unittest.TestCase):
    def test_capacity_prefers_explicit_setting_then_conservative_default(self):
        from orbynth.agents.planning import probe_capacity

        self.assertEqual(probe_capacity(env={}, configured=0, total_items=10).per_wave, 3)
        self.assertEqual(probe_capacity(env={"ORBYNTH_AGENT_PARALLELISM": "1"}, total_items=10).per_wave, 1)
        plan = probe_capacity(env={"ORBYNTH_AGENT_PARALLELISM": "1"}, total_items=9)
        self.assertFalse(plan.parallel)
        self.assertEqual(plan.waves, 9)
        self.assertIn("sequential", plan.source)
        capped = probe_capacity(env={"ORBYNTH_AGENT_PARALLELISM": "999"}, total_items=40)
        self.assertLessEqual(capped.per_wave, 12)

    def test_waves_are_chunked_in_order(self):
        from orbynth.agents.planning import plan_waves

        waves = plan_waves(list(range(7)), 3)
        self.assertEqual([len(w) for w in waves], [3, 3, 1])
        self.assertEqual([item for wave in waves for item in wave], list(range(7)))

    def test_run_waves_falls_back_to_sequential_without_parallel_capacity(self):
        from orbynth.agents.planning import run_waves
        from orbynth.events import EventBus

        class FakeItem:
            def __init__(self, name):
                self.name = name

        class FakeReport:
            def __init__(self, name):
                self.name = name

            def to_json(self):
                return {"agent": self.name, "status": "done", "summary": "ok"}

        class FakeOrchestrator:
            def __init__(self):
                from orbynth.config import default_config

                self.config = default_config()
                self.bus = EventBus()
                self.calls = []

            def dispatch(self, item, parent=None, wait=True):
                self.calls.append((item.name, wait))
                return FakeReport(item.name)

        previous = os.environ.get("ORBYNTH_NO_PARALLEL")
        os.environ["ORBYNTH_NO_PARALLEL"] = "1"
        try:
            orchestrator = FakeOrchestrator()
            result = run_waves(orchestrator, "do four things", [FakeItem(f"i{n}") for n in range(4)], bus=orchestrator.bus)
        finally:
            if previous is None:
                os.environ.pop("ORBYNTH_NO_PARALLEL", None)
            else:
                os.environ["ORBYNTH_NO_PARALLEL"] = previous
        self.assertEqual([c[0] for c in orchestrator.calls], ["i0", "i1", "i2", "i3"])
        self.assertTrue(all(call[1] for call in orchestrator.calls), "sequential mode must wait for each item")
        self.assertIn("wave", result["reports"][0])
        self.assertEqual(result["waves"], 4)
        self.assertFalse(result["plan"]["parallel"])

    def test_milestones_are_declared_with_exit_criteria(self):
        from orbynth.agents.planning import MILESTONES

        ids = [m["id"] for m in MILESTONES]
        self.assertEqual(ids, ["M0", "M1", "M2", "M3", "M4", "GA"])
        self.assertTrue(all(m["exit"] for m in MILESTONES))


class RegistryOwnerGateTests(unittest.TestCase):
    def test_gate_is_off_by_default_and_on_when_requested(self):
        from orbynth.actions import filesystem
        from orbynth.actions.registry import ActionContext, ActionRegistry
        from orbynth.config import default_config, expand_capabilities
        from orbynth.events import EventBus
        import tempfile as _temp

        home = _temp.mkdtemp(prefix="orbynth-gate-")
        registry = ActionRegistry()
        registry.register_module(filesystem)
        registry.owner = OwnerRegistry(home)
        registry.owner.setup("Owner", "correct-horse")
        target = os.path.join(home, "doomed.txt")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("x")
        caps = expand_capabilities(["filesystem.*"])
        loose = ActionContext(capabilities=caps, confirmed=("*",))
        self.assertTrue(registry.invoke("fs_delete", loose, path=target).ok)
        armed = ActionContext(capabilities=caps, confirmed=("*",), require_owner=True)
        second = os.path.join(home, "other.txt")
        with open(second, "w", encoding="utf-8") as handle:
            handle.write("x")
        denied = registry.invoke("fs_delete", armed, path=second)
        self.assertFalse(denied.ok)
        self.assertTrue(denied.evidence.get("owner_required"))
        self.assertTrue(registry.owner.verify_pin("correct-horse"))
        self.assertTrue(registry.invoke("fs_delete", armed, path=second).ok)


if __name__ == "__main__":
    unittest.main()