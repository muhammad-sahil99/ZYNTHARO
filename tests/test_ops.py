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

from zyntharo.ops.catalog import (  # noqa: E402
    ACTIVE,
    RETIRED,
    CatalogEntry,
    ModelCatalog,
    UpdateGate,
    check_provider_models,
    sign_catalog,
)
from zyntharo.ops.governor import (  # noqa: E402
    BACKGROUND,
    CRITICAL,
    GAME,
    HEAVY_LOAD,
    VIDEO_CALL,
    BusySignal,
    ResponsivenessGovernor,
)
from zyntharo.ops.languages import (  # noqa: E402
    RTL,
    detect,
    direction_of,
    equivalent,
    normalize,
    ocr_languages,
    profile_for,
)
from zyntharo.ops.owner import OwnerRegistry  # noqa: E402
from zyntharo.ops.preflight import (  # noqa: E402
    DEFER,
    RUN_BACKGROUND,
    RUN_ISOLATED,
    RUN_NOW,
    SKIP,
    MachineState,
    Preflight,
    TaskRequirements,
)
from zyntharo.ops.supervisor import EXITED, RUNNING, STOPPED, Supervisor  # noqa: E402
from zyntharo.ops.verification import (  # noqa: E402
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
    return tempfile.mkdtemp(prefix="zyntharo-ops-")


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
        from zyntharo.ops.supervisor import _pid_alive

        deadline = time.time() + 5
        while time.time() < deadline and _pid_alive(job.pid):
            time.sleep(0.1)
        self.assertFalse(_pid_alive(job.pid), "the orphaned helper is still running")
        # Reap our own child so the test does not leave a zombie behind.
        first._procs[job.job_id].poll()
        second.shutdown()

    def test_adopted_jobs_survive_reconciliation(self):
        # Adoption used to be dead code: self.jobs.clear() at the end of reconcile() wiped
        # every job _adopt() had just added, so the report claimed nothing was adopted and
        # the still-running helper was never watched.
        home = temp_home()
        first = Supervisor(home, poll_interval=5.0)
        first.start(reconcile=False)
        job = first.spawn([sys.executable, "-c", "import time; time.sleep(30)"], name="adoptee", restart="never")
        first._stop.set()
        first.shutting_down = True
        second = Supervisor(home, poll_interval=0.05, adopt_orphans=True)
        report = second.start()
        self.assertIn("adoptee", report["adopted"], report)
        self.assertIn(job.job_id, second.jobs)
        self.assertEqual(report["terminated"], [])
        second.shutdown(grace=2.0)
        first._procs[job.job_id].poll()

    def test_interrupted_workspaces_are_cleaned_at_startup(self):
        # A crashed run's scratch directory used to survive forever: the cleanup loop only
        # covered jobs it had just terminated, which skipped the process-is-gone case.
        home = temp_home()
        scratch = os.path.join(home, "scratch", "interrupted")
        os.makedirs(scratch, exist_ok=True)
        first = Supervisor(home, poll_interval=5.0)
        first.start(reconcile=False)
        job = first.spawn(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            name="interrupted",
            restart="never",
            workspace=scratch,
        )
        first._stop.set()
        first.shutting_down = True
        # The supervisor died; the helper did not, so kill it to leave a "process gone" entry.
        from zyntharo.ops.supervisor import _terminate_tree

        _terminate_tree(job.pid, job.pgid, force=True)
        first._procs[job.job_id].wait(timeout=5)
        second = Supervisor(home, poll_interval=0.05)
        report = second.start()
        self.assertIn("interrupted", [entry["name"] for entry in report["orphaned"]], report)
        self.assertIn(scratch, report["workspaces_cleaned"])
        self.assertFalse(os.path.exists(scratch))
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

    def test_windows_audio_probe_is_not_a_display_probe(self):
        # The bug this pins down: audio_device used to be filled from the screen-width metric,
        # so a machine with no sound hardware looked ready for voice work. The probe must ask
        # an audio question; off Windows it is simply not consulted.
        from zyntharo.ops.preflight import _windows_audio_available

        self.assertIsInstance(_windows_audio_available(), bool)
        state = MachineState.detect()
        self.assertIsInstance(state.audio_device, bool)

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

    def test_using_the_recovery_code_rotates_it_without_asking_the_caller(self):
        # The rotation used to live in the CLI command only, so every other caller of a
        # library API left the code it had just used valid forever.
        owner = OwnerRegistry(temp_home())
        first_code = owner.setup("Owner", "correct-horse")
        self.assertTrue(owner.verify_recovery(first_code, "a-new-long-pin"))
        self.assertTrue(owner.last_recovery_code)
        self.assertNotEqual(owner.last_recovery_code, first_code)
        self.assertFalse(owner.verify_recovery(first_code, "yet-another-pin"))
        self.assertTrue(owner.verify_recovery(owner.last_recovery_code, "yet-another-pin"))

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

    def test_a_request_writes_down_what_it_expects_to_touch(self):
        from zyntharo.ops.verification import capabilities_from_request, contract_for_request

        caps = capabilities_from_request("download the invoice PDFs, rename them and delete the duplicates")
        self.assertIn("filesystem.write", caps)
        self.assertIn("filesystem.delete", caps)
        self.assertNotIn("shell.exec", caps)
        empty = capabilities_from_request("say hello")
        self.assertEqual(empty, ())
        contract = contract_for_request("t-9", "save a summary to report.txt", expected_outputs=["report.txt"])
        self.assertEqual(contract.request, "save a summary to report.txt")
        self.assertEqual([c.target for c in contract.criteria], ["report.txt"])

    def test_a_contract_with_nothing_to_check_is_never_met(self):
        verdict = self.verifier().check(TaskContract("t-6", "do something vague"))
        self.assertEqual(verdict.status, UNCERTAIN)
        self.assertIn("no checkable success criteria", verdict.summary)

    def test_side_effects_line_is_honest_when_no_capabilities_were_declared(self):
        contract = TaskContract("t-7", "read the config")
        contract.add("file_exists", target="config.json")
        verdict = self.verifier().check(contract, performed=[{"action": "fs_delete", "capability": "filesystem.delete"}])
        self.assertIn("side effects not judged", verdict.summary)
        declared = TaskContract("t-8", "read the config")
        declared.intended_capabilities = ("filesystem.read",)
        declared.add("file_exists", target="config.json")
        clean = self.verifier().check(declared, performed=[{"action": "fs_read", "capability": "filesystem.read"}])
        self.assertIn("0 unintended side effect(s)", clean.summary)

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
        from zyntharo.config import default_config, load_config

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
        from zyntharo.config import default_config

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
        from zyntharo.config import default_config, load_config

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
        from zyntharo.config import default_config, load_config

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
        from zyntharo.config import default_config

        config = default_config()
        report = check_provider_models(config, fetch=lambda base, key, kind="": ["gemini-2.5-flash"])
        self.assertIn("gemini-2.5-pro", [m["model_id"] for m in report["missing"]])

        def boom(base, key, kind=""):
            raise OSError("connection refused")

        offline = check_provider_models(config, fetch=boom)
        self.assertTrue(offline["unreachable"])

    def test_custom_provider_with_base_url_and_quotas_is_applied(self):
        from zyntharo.config import ProviderConfig, load_config

        home = temp_home()
        from zyntharo.config import default_config

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


def _quiet_governor(**kwargs):
    """A governor that cannot see this host: no detector, and load pinned to zero.

    Both injections matter. These tests used to inject the signals but not the load, so on a
    busy machine (a CI box, a laptop compiling) ``evaluate([])`` still paused background work
    and the release gate failed for a reason that had nothing to do with the code under test.
    """
    kwargs.setdefault("detector", lambda: [])
    kwargs.setdefault("load_provider", lambda: 0.0)
    return ResponsivenessGovernor(**kwargs)


class GovernorTests(unittest.TestCase):
    def test_game_pauses_background_but_not_the_users_own_request(self):
        governor = _quiet_governor()
        state = governor.evaluate([BusySignal(GAME, "valorant", 0.9)])
        self.assertTrue(state.any_pause)
        self.assertTrue(governor.may_run(CRITICAL, state)[0])
        allowed, reason = governor.may_run(BACKGROUND, state)
        self.assertFalse(allowed)
        self.assertIn("deferred", reason)
        self.assertGreater(governor.delay_for(BACKGROUND, state), 0)

    def test_idle_machine_lets_everything_run(self):
        governor = _quiet_governor()
        state = governor.evaluate([])
        self.assertTrue(governor.may_run(BACKGROUND, state)[0])
        self.assertEqual(governor.delay_for(BACKGROUND, state), 0.0)

    def test_a_loaded_machine_pauses_background_work(self):
        """The load reading is part of the policy, so it has to be injectable and real."""
        governor = ResponsivenessGovernor(detector=lambda: [], load_provider=lambda: 1.4)
        state = governor.evaluate([])
        self.assertFalse(governor.may_run(BACKGROUND, state)[0])
        self.assertTrue(governor.may_run(CRITICAL, state)[0])

    def test_busy_reason_names_what_is_holding_the_machine(self):
        self.assertEqual(_quiet_governor().busy_reason(), "")
        busy = _quiet_governor()
        busy.detector = lambda: [BusySignal(GAME, "valorant"), BusySignal(VIDEO_CALL, "teams")]
        reason = busy.busy_reason()
        self.assertIn("game", reason)
        self.assertIn("video_call", reason)

    def test_pause_and_resume_are_reported_once_each(self):
        events: list[str] = []
        from zyntharo.events import EventBus, GOVERNOR_PAUSED, GOVERNOR_RESUMED

        bus = EventBus()
        bus.subscribe(GOVERNOR_PAUSED, lambda e: events.append("paused"))
        bus.subscribe(GOVERNOR_RESUMED, lambda e: events.append("resumed"))
        governor = _quiet_governor(bus=bus)
        governor.evaluate([BusySignal(GAME, "cs2")])
        self.assertTrue(governor.may_run(CRITICAL, governor.evaluate([BusySignal(GAME, "cs2")]))[0])
        governor.evaluate([])
        self.assertEqual(events, ["paused", "resumed"])

    def test_worker_cap_shrinks_before_expensive_work_is_admitted(self):
        """The cap must exist at admission time, not after Chromium or OCR already started."""
        quiet = _quiet_governor(max_workers=4)
        self.assertEqual(quiet.worker_limit(4, quiet.evaluate([])), 4)
        mild = _quiet_governor(max_workers=4, load_provider=lambda: 0.7)
        state = mild.evaluate([])
        self.assertFalse(state.paused[BACKGROUND], "mild pressure must not stop all work")
        self.assertEqual(mild.worker_limit(4, state), 2, "mild pressure halves admission")
        heavy = ResponsivenessGovernor(detector=lambda: [], load_provider=lambda: 1.2, max_workers=4)
        state = heavy.evaluate([])
        self.assertTrue(state.paused[BACKGROUND])
        self.assertEqual(heavy.worker_limit(4, state), 1)
        self.assertEqual(heavy.worker_limit(4, state, CRITICAL), 4, "the user's own request is not courtesy-capped")

    def test_a_cap_never_reaches_zero_workers(self):
        # A zero cap would look like "paused" but could deadlock a caller waiting for progress.
        governor = ResponsivenessGovernor(detector=lambda: [], load_provider=lambda: 9.0, max_workers=1)
        self.assertEqual(governor.worker_limit(8, governor.evaluate([])), 1)
        self.assertEqual(governor.worker_limit(0, governor.evaluate([])), 1)

    def test_priority_change_never_raises(self):
        governor = _quiet_governor()
        self.assertIn(governor.apply_priority("below_normal"), {"below_normal", "normal"})
        self.assertIn(governor.summary()["priority"], {"below_normal", "normal"})

    @unittest.skipUnless(sys.platform.startswith("linux"), "posix nice semantics")
    def test_priority_is_absolute_not_cumulative(self):
        """Asking twice for the same level must not keep lowering the process.

        ``os.nice()`` is relative: the old implementation added 10 on every call and reached
        the floor after two, so "below normal" was silently "as low as possible".
        """
        governor = _quiet_governor()
        original = os.getpriority(os.PRIO_PROCESS, 0)
        try:
            self.assertEqual(governor.apply_priority("below_normal"), "below_normal")
            first = os.getpriority(os.PRIO_PROCESS, 0)
            governor.apply_priority("below_normal")
            second = os.getpriority(os.PRIO_PROCESS, 0)
            self.assertEqual(first, second)
            self.assertEqual(second, 10)
        finally:
            try:
                os.setpriority(os.PRIO_PROCESS, 0, original)
            except OSError:  # pragma: no cover - would need privileges already given up
                pass


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

    def test_persian_is_not_swallowed_by_urdu(self):
        # پ چ ژ گ ک ی are shared by Urdu and Persian, and testing the combined set first made
        # every Persian sentence score as Urdu; Persian only ever won on a letter it does not
        # actually use.
        self.assertEqual(detect("گزارش مالی"), "fa")
        self.assertEqual(detect("رپورٹ محفوظ کریں"), "ur")
        self.assertEqual(detect("حفظ التقرير في الملف"), "ar")
        self.assertEqual(detect("سلام"), "ar")  # indistinguishable without a dictionary

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
        from zyntharo.agents.planning import probe_capacity

        self.assertEqual(probe_capacity(env={}, configured=0, total_items=10).per_wave, 3)
        self.assertEqual(probe_capacity(env={"ZYNTHARO_AGENT_PARALLELISM": "1"}, total_items=10).per_wave, 1)
        plan = probe_capacity(env={"ZYNTHARO_AGENT_PARALLELISM": "1"}, total_items=9)
        self.assertFalse(plan.parallel)
        self.assertEqual(plan.waves, 9)
        self.assertIn("sequential", plan.source)
        capped = probe_capacity(env={"ZYNTHARO_AGENT_PARALLELISM": "999"}, total_items=40)
        self.assertLessEqual(capped.per_wave, 12)

    def test_waves_are_chunked_in_order(self):
        from zyntharo.agents.planning import plan_waves

        waves = plan_waves(list(range(7)), 3)
        self.assertEqual([len(w) for w in waves], [3, 3, 1])
        self.assertEqual([item for wave in waves for item in wave], list(range(7)))

    def test_run_waves_falls_back_to_sequential_without_parallel_capacity(self):
        from zyntharo.agents.planning import run_waves
        from zyntharo.events import EventBus

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
                from zyntharo.config import default_config

                self.config = default_config()
                self.bus = EventBus()
                self.calls = []

            def dispatch(self, item, parent=None, wait=True):
                self.calls.append((item.name, wait))
                return FakeReport(item.name)

        previous = os.environ.get("ZYNTHARO_NO_PARALLEL")
        os.environ["ZYNTHARO_NO_PARALLEL"] = "1"
        try:
            orchestrator = FakeOrchestrator()
            result = run_waves(orchestrator, "do four things", [FakeItem(f"i{n}") for n in range(4)], bus=orchestrator.bus)
        finally:
            if previous is None:
                os.environ.pop("ZYNTHARO_NO_PARALLEL", None)
            else:
                os.environ["ZYNTHARO_NO_PARALLEL"] = previous
        self.assertEqual([c[0] for c in orchestrator.calls], ["i0", "i1", "i2", "i3"])
        self.assertTrue(all(call[1] for call in orchestrator.calls), "sequential mode must wait for each item")
        self.assertIn("wave", result["reports"][0])
        self.assertEqual(result["waves"], 4)
        self.assertFalse(result["plan"]["parallel"])

    def test_milestones_are_declared_with_exit_criteria(self):
        from zyntharo.agents.planning import MILESTONES

        ids = [m["id"] for m in MILESTONES]
        self.assertEqual(ids, ["M0", "M1", "M2", "M3", "M4", "GA"])
        self.assertTrue(all(m["exit"] for m in MILESTONES))


class SpawnTests(unittest.TestCase):
    def test_a_launched_helper_remembers_the_supervisor_that_owns_it(self):
        # terminate() used to look the supervisor up in a module global, so a second
        # supervisor -- a restart, or a test -- meant stopping the wrong process book.
        from zyntharo.ops.spawn import Launched, managed_popen, set_supervisor

        supervisor = Supervisor(temp_home(), poll_interval=5.0)
        supervisor.start(reconcile=False)
        set_supervisor(supervisor)
        try:
            launched = managed_popen([sys.executable, "-c", "import time; time.sleep(30)"], name="probe")
            self.assertTrue(launched.supervised)
            self.assertIs(launched.supervisor, supervisor)
            self.assertIsInstance(launched, Launched)
            replaced = Supervisor(temp_home(), poll_interval=5.0)
            set_supervisor(replaced)
            launched.terminate(force=True)
            self.assertEqual(launched.job.status, STOPPED)
            self.assertTrue(any(j.status == STOPPED for j in supervisor.jobs.values()))
            replaced.shutdown()
        finally:
            set_supervisor(None)
            supervisor.shutdown(grace=2.0)


class RegistryOwnerGateTests(unittest.TestCase):
    def test_gate_is_off_by_default_and_on_when_requested(self):
        from zyntharo.actions import filesystem
        from zyntharo.actions.registry import ActionContext, ActionRegistry
        from zyntharo.config import default_config, expand_capabilities
        from zyntharo.events import EventBus
        import tempfile as _temp

        home = _temp.mkdtemp(prefix="zyntharo-gate-")
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


class AuditFixTests(unittest.TestCase):
    """Regressions for faults found by reading the seams rather than by breaking them."""

    def test_one_definition_of_irreversible_governs_every_consumer(self):
        """The owner gate, the verification tier and the registry must not disagree.

        They used to: ``model.assign`` and ``memory.admin`` demanded a PIN but were tiered as
        routine work, so the two systems held different beliefs about the same action.
        """
        from zyntharo.actions.registry import OWNER_IRREVERSIBLE
        from zyntharo.ops import verification
        from zyntharo.ops.owner import IRREVERSIBLE_CAPABILITIES as owner_list
        from zyntharo.ops.risk import IRREVERSIBLE_CAPABILITIES as canonical

        self.assertEqual(canonical, frozenset(owner_list))
        self.assertEqual(canonical, frozenset(OWNER_IRREVERSIBLE))
        self.assertEqual(canonical, frozenset(verification.IRREVERSIBLE_CAPABILITIES))
        self.assertIn("model.assign", canonical)
        self.assertIn("memory.admin", canonical)

    def test_preflight_defers_desktop_work_when_the_governor_says_busy(self):
        """``busy_reason`` used to be declared and never filled, so this branch was dead."""
        preflight = Preflight(
            state_provider=lambda: MachineState(),
            busy_provider=lambda: "video_call",
        )
        decision = preflight.check(TaskRequirements(gui=True))
        self.assertEqual(decision.action, DEFER)
        self.assertIn("video_call", "; ".join(decision.reasons))

    def test_preflight_uses_the_headless_path_on_a_busy_machine(self):
        preflight = Preflight(state_provider=lambda: MachineState(), busy_provider=lambda: "game")
        decision = preflight.check(TaskRequirements(gui=True, background_ok=True), origin="schedule")
        self.assertEqual(decision.action, RUN_BACKGROUND)

    def test_an_unreadable_busy_probe_never_blocks_a_task(self):
        def explode() -> str:
            raise RuntimeError("probe broke")

        preflight = Preflight(state_provider=lambda: MachineState(), busy_provider=explode)
        self.assertEqual(preflight.check(TaskRequirements()).action, RUN_NOW)

    def test_isolation_is_only_offered_when_it_is_configured(self):
        locked = MachineState(session_locked=True)
        without = Preflight(state_provider=lambda: locked, isolation_available=False)
        self.assertEqual(without.check(TaskRequirements(gui=True)).action, DEFER)
        with_iso = Preflight(state_provider=lambda: locked, isolation_available=True)
        self.assertEqual(with_iso.check(TaskRequirements(gui=True)).action, RUN_ISOLATED)

    def test_idle_time_comes_from_the_probe_and_drives_user_presence(self):
        """``idle_seconds`` was never detected, so ``user_active`` was always True."""
        present = MachineState(idle_seconds=4)
        absent = MachineState(idle_seconds=900)
        self.assertTrue(present.user_active)
        self.assertFalse(absent.user_active)
        check = Preflight(state_provider=lambda: absent)
        self.assertEqual(check.check(TaskRequirements(user_present=True)).action, DEFER)
        # A headless run on an empty desktop is allowed to use the non-GUI path.
        decision = Preflight().check(TaskRequirements(gui=True, background_ok=True), origin="schedule", state=absent)
        self.assertEqual(decision.action, RUN_BACKGROUND)

    def test_supervisor_journal_never_records_helper_environment_values(self):
        """A helper's environment can hold a provider key; the journal is plain text."""
        home = tempfile.mkdtemp(prefix="zyntharo-journal-")
        supervisor = Supervisor(home, poll_interval=5.0)
        try:
            job = supervisor.spawn(
                [sys.executable, "-c", "import time; time.sleep(5)"],
                name="leaky",
                env={"OPENAI_API_KEY": "sk-not-a-real-key"},
            )
            supervisor._save()  # noqa: SLF001 - the journal write is what is under test
            raw = (supervisor.home / "jobs.json").read_text(encoding="utf-8")
            self.assertNotIn("sk-not-a-real-key", raw)
            self.assertIn("OPENAI_API_KEY", raw)
            self.assertIn(job.job_id, raw)
        finally:
            supervisor.shutdown(grace=1.0)


if __name__ == "__main__":
    unittest.main()