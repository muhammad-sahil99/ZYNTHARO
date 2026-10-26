"""In-process acceptance operations for the release gate.

These run the real objects -- the same ones the test suite builds -- rather than a shell
script that reimplements them, so a behaviour change breaks the gate instead of a copy of it.
Each returns ``(ok, detail)``; detail is shown verbatim, including honest failures.
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _temp_home() -> str:
    return tempfile.mkdtemp(prefix="orbynth-gate-")


def _stack(home: str) -> Any:
    """The same assembled system the test suite uses, so the gate tests the product."""
    try:
        from tests.helpers import Stack
    except ImportError as exc:  # pragma: no cover - packaging concern, reported not hidden
        raise RuntimeError(f"the in-process gate needs the test fixtures on the path: {exc}") from exc
    return Stack(home=home)


def op_m0_slice(root: Path) -> tuple[bool, str]:
    """M0: one real task through every layer -- registry, driver, evidence, memory, bus."""
    home = _temp_home()
    stack = _stack(home)
    try:
        target = str(Path(home) / "m0.txt")
        write = stack.tool("fs_write", path=target, content="ORBYNTH M0")
        if not write.ok:
            return False, f"write failed: {write.error}"
        read = stack.tool("fs_read", path=target)
        if not read.ok or "ORBYNTH M0" not in str(read.output):
            return False, f"read-back mismatch: {read.output!r}"
        topics = {event.topic for event in stack.events}
        if "action.result" not in topics:
            return False, f"no action.result reached the bus; HUD would show nothing ({sorted(topics)[:6]})"
        if not stack.memory.audit_log(limit=5):
            return False, "no audit row was written for the action"
        denied = stack.tool("fs_delete", capabilities=["filesystem.read"], path=target)
        if denied.ok:
            return False, "capability gate did not deny a delete without the capability"
        return True, f"task through gate->driver->evidence->memory->bus; {len(topics)} topics; delete correctly denied ({denied.error[:40]})"
    finally:
        stack.close()


def op_plugin_lifecycle(root: Path) -> tuple[bool, str]:
    """The Forge authors, validates and installs a real plugin, then it can be removed."""
    home = _temp_home()
    stack = _stack(home)
    before: set[str] = set()
    try:
        before = set(stack.actions.names())
        result = stack.forge.forge(
            "download invoices then rename them by date",
            name="invoice-download",
            capabilities=["filesystem.read", "filesystem.write"],
        )
        validation = result.get("validation", {})
        if not validation.get("ok"):
            return False, f"validation refused the authored plugin: {validation.get('errors')}"
        if not result.get("installed"):
            return False, f"forge did not install: {result.get('error') or result}"
        # Compare before/after rather than assuming a naming prefix: what matters is that a
        # new, capability-carrying action appeared and that removing it takes it away again.
        added = sorted(set(stack.actions.names()) - before)
        if not added:
            return False, f"the authored plugin registered no action: {stack.actions.names()[:8]}"
        action = stack.actions.get(added[0])
        if action is None or not action.capability:
            return False, f"the new action {added[0]!r} declares no capability, so the gate cannot hold it"
        if not stack.plugins.installed():
            return False, "the plugin was not recorded as installed"
        stack.plugins.uninstall("invoice-download")
        after_removal = sorted(set(stack.actions.names()) - before)
        if after_removal:
            return False, f"uninstall left actions registered: {after_removal}"
        return True, f"authored+installed {added[0]!r} (capability {action.capability}); uninstall removed it"
    finally:
        stack.close()


def op_supervisor_cleanup(root: Path) -> tuple[bool, str]:
    """A supervised helper dies with its supervisor, and the journal reconciles afterwards."""
    from orbynth.ops.supervisor import RUNNING, Supervisor

    home = _temp_home()
    first = Supervisor(home, poll_interval=0.05)
    first.start(reconcile=False)
    job = first.spawn([sys.executable, "-c", "import time; time.sleep(60)"], name="gate-helper", restart="never")
    time.sleep(0.4)
    if job.status != RUNNING:
        return False, "the helper did not start"
    report = first.shutdown(grace=2.0)
    time.sleep(0.3)
    if "gate-helper" not in report["stopped"]:
        return False, f"the helper was not stopped with the supervisor: {report}"
    second = Supervisor(home, poll_interval=0.05)
    reconciled = second.start()
    second.shutdown()
    if reconciled["checked"] == 0:
        return False, "the journal was empty on restart, so orphan cleanup had nothing to work from"
    return True, f"helper stopped with its supervisor; reconcile revisited {reconciled['checked']} journalled job(s)"


def op_owner_gate(root: Path) -> tuple[bool, str]:
    """Risky work needs the owner: no PIN, no permission; a voice session is not enough."""
    from orbynth.ops.owner import OwnerRegistry

    home = _temp_home()
    owner = OwnerRegistry(home)
    recovery = owner.setup("Owner", "correct-horse", machine_kind="shared")
    if "-" not in recovery:
        return False, "first-run setup issued no recovery code"
    without = owner.require(["filesystem.delete"], method="session")
    if without.allow:
        return False, "an irreversible action was authorised with no PIN session"
    if not owner.verify_pin("correct-horse"):
        return False, "the correct PIN was rejected"
    with_pin = owner.require(["filesystem.delete"], method="pin")
    if not with_pin.allow:
        return False, f"a PIN session did not authorise the delete: {with_pin.reason}"
    voice_only = OwnerRegistry(home).require(["whatsapp.call"], method="voice")
    if voice_only.allow:
        return False, "voice alone authorised an irreversible action"
    recovered = OwnerRegistry(home)
    if recovered.verify_recovery(recovery, "another-good-pin"):
        rotated = recovered.rotate_recovery_code() if recovered.profile.configured else ""
    else:
        return False, "the recovery code did not restore access"
    if recovered.verify_recovery(recovery, "third-pin") :
        return False, "the consumed recovery code still worked"
    return True, (
        f"denied without PIN ({without.method}), allowed with it ({with_pin.method}), "
        f"voice denied, recovery consumed and rotated ({len(rotated)} chars)"
    )


def op_catalog_rollback(root: Path) -> tuple[bool, str]:
    """A catalog applies only when idle, retires dead model IDs, and rolls back cleanly."""
    from orbynth.config import default_config, load_config
    from orbynth.ops.catalog import ACTIVE, RETIRED, CatalogEntry, ModelCatalog, UpdateGate

    home = _temp_home()
    config = default_config()
    config.home = home
    config.save()
    catalog = ModelCatalog(
        version="gate-1",
        entries=[
            CatalogEntry("gemini", "gemini-2.5-flash", ACTIVE, rpm=7, rpd=200, tpm=250_000),
            CatalogEntry("gemini", "gemini-2.5-pro", RETIRED),
        ],
    )
    verification = catalog.verify(hmac_key="", allow_unsigned=True)
    if not verification.ok:
        return False, "an unsigned catalogue was refused even in development mode"
    gate = UpdateGate(home)
    gate.stage(catalog, verification)
    allowed, reason = gate.can_apply(in_flight=2)
    if allowed:
        return False, "an update was applied while work was in flight"
    delta = gate.apply(config, in_flight=0, verification=verification)
    if load_config(config.config_path).model("gemini:gemini-2.5-pro") is not None:
        return False, "the retired model survived the apply"
    applied = load_config(config.config_path).model("gemini:gemini-2.5-flash")
    if applied is None or applied.rpm != 7:
        return False, "the active catalogue entry was not applied"
    gate.rollback(config)
    if load_config(config.config_path).model("gemini:gemini-2.5-pro") is None:
        return False, "rollback did not restore the retired model"
    return True, f"blocked while busy ({reason[:38]}), applied -{len(delta.retired)} model(s), rolled back"


def op_language_rtl(root: Path) -> tuple[bool, str]:
    """Urdu and Arabic interfaces: direction, detection, digits and OCR packs."""
    from orbynth.ops.languages import detect, direction_of, equivalent, normalize, ocr_languages, profile_for

    urdu = "رپورٹ محفوظ کریں"
    if direction_of(urdu) != "rtl" or direction_of("حفظ التقرير") != "rtl":
        return False, "Urdu/Arabic text was not detected as right-to-left"
    if detect(urdu) != "ur":
        return False, f"Urdu was detected as {detect(urdu)!r}"
    if normalize("١٢٣ ملف\u0640\u064b") != "123 ملف":
        return False, f"digit/diacritic normalisation failed: {normalize('١٢٣ ملف\u0640\u064b')!r}"
    if not equivalent("مَلف", "ملف"):
        return False, "diacritic-insensitive comparison failed"
    profile = profile_for(urdu, hints=["ur"])
    if profile.direction != "rtl":
        return False, f"profile direction was {profile.direction!r}"
    packs = ocr_languages([profile.primary])
    if "urd" not in packs:
        return False, f"no Urdu OCR pack was produced: {packs}"
    return True, f"ur/ar rtl ok, digits normalised, profile={profile.to_json()['direction']}, ocr={ocr_languages(['ur', 'ar'])}"


def op_risk_tiers(root: Path) -> tuple[bool, str]:
    """Harmless work is batched; irreversible work is never shortcut."""
    from orbynth.ops.verification import NEVER_SHORTCUT

    home = _temp_home()
    stack = _stack(home)
    try:
        plan = stack.actions.verification_plan()
        if "fs_delete" in plan["batched"]:
            return False, "fs_delete is batched, which would let a delete race ahead of its check"
        if not plan["immediate"]:
            return False, "nothing is verified immediately"
        if not plan["batched"]:
            return False, "nothing is batched: verifying every action would make the product slow"
        policies = {tier: p.verify for tier, p in stack.actions.risk_tiers.policies.items()}
        if NEVER_SHORTCUT not in policies.values():
            return False, "no tier uses the never-shortcut policy"
        return True, plan["summary"]
    finally:
        stack.close()


def op_preflight(root: Path) -> tuple[bool, str]:
    """A locked screen defers desktop work instead of pretending to run it."""
    from orbynth.ops.preflight import DEFER, RUN_ISOLATED, SKIP, MachineState, Preflight, TaskRequirements

    preflight = Preflight()
    gui_task = TaskRequirements(gui=True, unlocked_session=True, isolation_ok=True)
    locked = preflight.check(gui_task, origin="schedule", state=MachineState(session_locked=True))
    if locked.action != DEFER:
        return False, f"a locked screen produced {locked.action}, not defer"
    isolated = preflight.check(gui_task, origin="schedule", state=MachineState(session_locked=True, isolation_available=True))
    if isolated.action != RUN_ISOLATED:
        return False, "an available isolated session was not used"
    busy = preflight.check(gui_task, origin="schedule", state=MachineState(busy_reason="video_call"))
    if busy.action == "run_now":
        return False, "desktop work was scheduled during a video call"
    audio = preflight.check(TaskRequirements(audio=True), state=MachineState(audio_device=False))
    if audio.action != SKIP:
        return False, "a voice task with no audio device was not skipped"
    now = preflight.check(gui_task, origin="user_request", state=MachineState(idle_seconds=0.0))
    if not now.can_run:
        return False, "a task the user asked for right now was not allowed to run"
    return True, f"locked->{locked.action}, isolated->{isolated.action}, busy->{busy.action}, no-audio->{audio.action}, user-request->{now.action}"


def op_billing_modes(root: Path) -> tuple[bool, str]:
    """A hosted gateway refuses to arm itself without legal review and abuse caps."""
    from orbynth.config import default_config
    from orbynth.ops.billing import BYOK, GATEWAY, BillingGate, GatewayPolicy

    config = default_config()
    bare = BillingGate(GATEWAY, GatewayPolicy(endpoint="https://gateway.example/v1"))
    codes = [i.code for i in bare.audit(config) if i.severity == "blocking"]
    if not {"gateway_no_legal_review", "gateway_resale_unconfirmed"} <= set(codes):
        return False, f"an unguarded gateway passed the audit: {codes}"
    armed = BillingGate(
        GATEWAY,
        GatewayPolicy(
            endpoint="https://gateway.example/v1",
            legal_review_reference="LEGAL-2026-014",
            resale_acknowledged={"gemini": True, "custom": True},
        ),
    )
    remaining = [i.code for i in armed.audit(config) if i.severity == "blocking"]
    if remaining:
        return False, f"a fully configured gateway was still blocked: {remaining}"
    throttle = armed.authorize("user-1", est_tokens=10)
    if not throttle.allow:
        return False, f"a legitimate call was refused: {throttle.reason}"
    armed.gateway.per_user_daily_requests = 0
    capped = armed.authorize("user-1", est_tokens=10)
    if capped.allow:
        return False, "a zero per-user cap did not stop calls"
    byok = BillingGate(BYOK)
    return True, f"unguarded blocked ({codes[:2]}), armed allows ({throttle.reason}), caps enforced, byok issues={len(byok.audit(config))}"


def op_governor(root: Path) -> tuple[bool, str]:
    """Background work yields during a game; work the user asked for never does."""
    from orbynth.ops.governor import BACKGROUND, CRITICAL, GAME, BusySignal, ResponsivenessGovernor

    governor = ResponsivenessGovernor(detector=lambda: [])
    state = governor.evaluate([BusySignal(GAME, "valorant", 0.9)])
    if not governor.may_run(CRITICAL, state)[0]:
        return False, "the governor paused work the user explicitly asked for"
    allowed, reason = governor.may_run(BACKGROUND, state)
    if allowed:
        return False, "background work continued during a game"
    if governor.delay_for(BACKGROUND, state) <= 0:
        return False, "no back-off was applied to background work"
    quiet = governor.evaluate([])
    if not governor.may_run(BACKGROUND, quiet)[0]:
        return False, "background work stayed paused when the machine was idle"
    return True, f"critical allowed, background deferred during a game ({reason}), resumed when idle"


OPS: dict[str, Callable[[Path], tuple[bool, str]]] = {
    "m0_slice": op_m0_slice,
    "plugin_lifecycle": op_plugin_lifecycle,
    "supervisor_cleanup": op_supervisor_cleanup,
    "owner_gate": op_owner_gate,
    "catalog_rollback": op_catalog_rollback,
    "language_rtl": op_language_rtl,
    "risk_tiers": op_risk_tiers,
    "preflight": op_preflight,
    "billing_modes": op_billing_modes,
    "governor": op_governor,
}
