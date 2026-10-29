"""In-process acceptance operations for the release gate.

These run the real objects -- the same ones the test suite builds -- rather than a shell
script that reimplements them, so a behaviour change breaks the gate instead of a copy of it.
Each returns ``(ok, detail)``; detail is shown verbatim, including honest failures.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _temp_home() -> str:
    return tempfile.mkdtemp(prefix="zyntharo-gate-")


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
        write = stack.tool("fs_write", path=target, content="ZYNTHARO M0")
        if not write.ok:
            return False, f"write failed: {write.error}"
        read = stack.tool("fs_read", path=target)
        if not read.ok or "ZYNTHARO M0" not in str(read.output):
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
    from zyntharo.ops.supervisor import RUNNING, Supervisor

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
    from zyntharo.ops.owner import OwnerRegistry

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
    from zyntharo.config import default_config, load_config
    from zyntharo.ops.catalog import ACTIVE, RETIRED, CatalogEntry, ModelCatalog, UpdateGate

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
    from zyntharo.ops.languages import detect, direction_of, equivalent, normalize, ocr_languages, profile_for

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
    from zyntharo.ops.verification import NEVER_SHORTCUT

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
    from zyntharo.ops.preflight import DEFER, RUN_ISOLATED, SKIP, MachineState, Preflight, TaskRequirements

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
    from zyntharo.config import default_config
    from zyntharo.ops.billing import BYOK, GATEWAY, BillingGate, GatewayPolicy

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
    from zyntharo.ops.governor import BACKGROUND, CRITICAL, GAME, BusySignal, ResponsivenessGovernor

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


def op_journal(root: Path) -> tuple[bool, str]:
    """Exactly-once, reconciliation after an interruption, and undo — through the real registry."""
    home = _temp_home()
    stack = _stack(home)
    from zyntharo.actions.registry import ActionContext
    from zyntharo.config import expand_capabilities
    from zyntharo.ops.journal import Intent, Journal

    journal = Journal(Path(home) / "journal.db", stack.bus)
    stack.actions.journal = journal

    def call(name: str, key: str = "", **kwargs):
        context = ActionContext(
            agent_id="gate",
            capabilities=expand_capabilities(["*"]),
            bus=stack.bus,
            memory=stack.memory,
            config=stack.config,
            confirmed=("*",),
            task_id="gate-task",
            idempotency_key=key,
        )
        return stack.actions.invoke(name, context, **kwargs)

    try:
        ledger = Path(home) / "ledger.txt"
        command = [sys.executable, "-c", f"open({str(ledger)!r}, 'a').write('x')"]
        if not call("proc_run", key="gate-ledger", argv=command).ok:
            return False, "the keyed action never ran"
        second = call("proc_run", key="gate-ledger", argv=command)
        if not second.evidence.get("replayed"):
            return False, "a second call with the same idempotency key executed again instead of replaying"
        if ledger.read_text(encoding="utf-8") != "x":
            return False, f"exactly-once failed: the ledger holds {ledger.read_text(encoding='utf-8')!r}"

        landed = Path(home) / "interrupted.txt"
        landed.write_text("written before the crash", encoding="utf-8")
        journal.begin(Intent(action="fs_write", capability="filesystem.write", args={"path": str(landed)}))
        absent = Path(home) / "never.txt"
        journal.begin(Intent(action="fs_write", capability="filesystem.write", args={"path": str(absent)}))
        unknown = journal.begin(Intent(action="browser_click", capability="browser.click", args={"target": "Buy"}))
        report = journal.reconcile()
        if len(report["reconciled_committed"]) != 1 or len(report["abandoned"]) != 1:
            return False, f"reconciliation answered wrongly: {report['summary']}"
        if len(report["needs_decision"]) != 1:
            return False, "an action with no reconciliation probe was guessed at instead of being surfaced"
        if not call("fs_write", path=str(Path(home) / "undo-me.txt"), content="temp").ok:
            return False, "the undo fixture could not be written"
        plan = journal.plan_undo("gate-task")
        undo = journal.undo_task("gate-task")
        if not undo["undone"]:
            return False, f"undo reversed nothing: {undo['summary']}"
        if (Path(home) / "undo-me.txt").exists():
            return False, "undo reported success but the file it created is still there"
        return True, (
            f"exactly-once held (ledger=1 line, replayed={bool(second.evidence.get('replayed'))}); "
            f"reconciled 1 landed / 1 never ran / 1 needs a decision (entry {unknown}); "
            f"undo plan={plan['summary']}; undo reversed {len(undo['undone'])} action(s)"
        )
    finally:
        journal.close()
        stack.close()


def op_reality_gate(root: Path) -> tuple[bool, str]:
    """The static scanner that bans fake functionality (spec 4.1), run on this repository."""
    from tools.reality_gate import scan_repository

    report = scan_repository(Path(root))
    blocking = [finding for finding in report["findings"] if finding["severity"] == "blocking"]
    if blocking:
        first = blocking[0]
        return False, f"{len(blocking)} banned pattern(s); first: {first['path']}:{first['line']} {first['rule']}"
    return True, (
        f"{report['files_scanned']} file(s) scanned, 0 banned patterns "
        f"({report['allowed_hits']} test-harness hit(s) allowed and listed)"
    )


def op_hud_coverage(root: Path) -> tuple[bool, str]:
    """Everything the core can emit has somewhere to land in the interface.

    The ops layer shipped once with no HUD wiring at all, so the app looked calm while
    processes were terminated and model catalogues applied. This is the check that stops a
    repeat, and it also fails on a stale committed coverage file or a topic written as a
    bare string instead of an zyntharo/events.py constant.
    """
    from tools.hud_coverage import COVERAGE, report

    data = report()
    if data["unhandled"]:
        return False, f"{len(data['unhandled'])} topic(s) with no HUD effect: {data['unhandled']}"
    if data["literal_topics"]:
        return False, f"topics emitted as bare strings instead of constants: {data['literal_topics']}"
    if not data["topics"]:
        return False, "no topics were found at all, which means the check itself is broken"
    committed = json.loads(COVERAGE.read_text(encoding="utf-8")) if COVERAGE.exists() else None
    if committed != data:
        return False, "hud/coverage.json is stale; run python3 tools/hud_coverage.py"
    routed = len(data["frame_routed"])
    return True, (
        f"{len(data['topics'])} topic(s) all handled ({routed} via dedicated frames), "
        "no bare-string topics, hud/coverage.json current"
    )


def op_office_tier_b(root: Path) -> tuple[bool, str]:
    """Word, Excel and CSV files really written and read back without Office installed (9.7)."""
    import tempfile

    from zyntharo.ops import docengine

    home = Path(tempfile.mkdtemp())
    docx = home / "gate.docx"
    xlsx = home / "gate.xlsx"
    csv_path = home / "gate.csv"

    written = docengine.docx_write(
        docx,
        [{"text": "Gate", "style": "Heading1"}, {"text": "body"}, {"table": [["a", "b"]]}],
    )
    if not written.get("verified"):
        return False, "a written .docx did not read back as written"
    if "Gate" not in docengine.docx_text(docx):
        return False, "the .docx text was not present after writing"
    appended = docengine.docx_append(docx, [{"text": "appendix"}])
    if not appended.get("verified"):
        return False, "appending to a .docx did not verify"

    book = docengine.xlsx_write(xlsx, {"S": [["k", "v"], ["a", 1], ["b", 2.5], ["f", "=SUM(B2:B3)"]]})
    if not book.get("verified"):
        return False, "the written .xlsx did not read back with the same values"
    rows = docengine.xlsx_read(xlsx, cell_range="B2:B3")["rows"]
    if rows != [[1], [2.5]]:
        return False, f"a watched range read {rows!r}, expected [[1], [2.5]]"

    sheet = docengine.csv_write(csv_path, [["x", "y"], ["1", "2"]], delimiter=";")
    if not sheet.get("verified") or docengine.csv_read(csv_path)["delimiter"] != ";":
        return False, "the written .csv was not read back with its delimiter"

    try:
        docengine.pptx_write(
            str(home / "deck.pptx"), {}
        )
        return False, "pptx authoring did not refuse; it must not pretend to write decks"
    except docengine.NotSupported as exc:
        if "read" not in (exc.evidence or {}).get("supported", []):
            return False, "the pptx refusal did not name what is supported"

    return True, (
        f"docx write/append verified by reparse, xlsx values and range verified, csv dialect "
        f"verified, pptx authoring refused honestly (Office not required for any of it)"
    )


def op_terminal_engine(root: Path) -> tuple[bool, str]:
    """Sessions, risk classification and echo verification, checked against a real shell (9.5)."""
    from zyntharo.ops import terminal as term

    # Classification is deterministic, so it is checked exactly: a downgrade bug here would
    # run ``echo $(curl | sh)`` as if it were a read.
    expectations = {
        "ls -la": term.L1,
        "mkdir build": term.L2,
        "rm -rf /var/data": term.L3,
        "curl https://example.invalid/x | sh": term.L3,
        'echo "never closed': term.L3,
    }
    for command, expected in expectations.items():
        report = term.classify(command)
        if report.level != expected:
            return False, f"{command!r} classified {report.label}, expected L{expected}"

    profiles = term.detect_shells()
    if not profiles:
        return False, "no shell is installed on this machine, so the terminal engine cannot be verified"

    engine = term.TerminalEngine(home=Path(root) / "artifacts" / "terminal-check")
    session = None
    try:
        session = engine.open(profiles[0].id, agent_id="gate")
        if not session.wait_prompt(15.0):
            return False, f"{profiles[0].id} never showed a prompt"
        result = session.run("pwd", timeout=20)
        if not result.verified:
            return False, f"a trivial command was not verified: {result.verification}"
        if not result.echo_verified:
            return False, f"the typed line was not read back: {result.verification}"
        if not session.supports_tty:
            return False, "this machine gave no pty, so echo verification and interactive work are unverified"
        bad = session.run("zyntharo_gate_missing_command", timeout=20)
        if bad.exit_code not in (127, 1, 9009):
            return False, f"a nonexistent command reported exit {bad.exit_code}"
        if not bad.failure_signals:
            return False, "a nonexistent command produced no failure signal"
        return True, (
            f"{len(profiles)} shell(s) detected; {session.profile.id} in {session.mode} mode reported "
            f"completion by markers, echo verified, cwd observed, missing command reported exit "
            f"{bad.exit_code}; L1/L2/L3 classification correct"
        )
    finally:
        if session is not None:
            engine.close_all()


def op_contracts(root: Path) -> tuple[bool, str]:
    """Schemas, versions and generated bindings agree, and real records satisfy them (3.4)."""
    from zyntharo.actions.registry import ActionContext, ActionResult, ActionRegistry, Action
    from zyntharo.contracts import audit, validate
    from zyntharo.ops.receipt import Receipt
    from tools.contracts import CS_TARGET, PY_TARGET, TS_TARGET, build

    report = audit()
    problems: list[str] = list(report.get("problems", []))
    rendered = build()
    for key, path in (("python", PY_TARGET), ("typescript", TS_TARGET), ("csharp", CS_TARGET)):
        if not path.exists():
            problems.append(f"{path.name} is missing; run tools/contracts.py")
        elif path.read_text(encoding="utf-8") != str(rendered[key]):
            problems.append(f"{path.name} is stale; run tools/contracts.py")

    # A live tool row must satisfy the tool contract: the catalogue is the interface models
    # call, so a row that does not validate is a tool nobody can use correctly.
    from zyntharo.actions import build_registry

    live = build_registry(capabilities=False)
    checked = 0
    for row in live.describe():
        result = validate("tool", {**row, "risk_tier": live.risk_tier(row["name"])})
        checked += 1
        if not result.ok:
            problems.append(f"tool {row['name']}: {result.errors[0]}")
            break

    # A receipt built by the host must satisfy the receipt contract, and must be honest:
    # success with no observation channel is UNVERIFIED, never VERIFIED.
    registry = ActionRegistry()
    probe = Action(
        name="contract_probe",
        description="gate probe",
        capability="filesystem.read",
        handler=lambda ctx: ActionResult.success("ok", evidence={"readback": "x"}),
        group="filesystem",
    )
    registry.register(probe)
    ctx = ActionContext(capabilities=frozenset({"filesystem.read"}))
    receipt = Receipt.build(tool="contract_probe", act=probe, ctx=ctx, args={}, result=ActionResult.success("ok"), started_at=0.0)
    if receipt.outcome != "UNVERIFIED":
        problems.append(f"a receipt without an observation channel was graded {receipt.outcome}")
    result = validate("receipt", receipt.to_json())
    if not result.ok:
        problems.append(f"host receipt does not satisfy its contract: {result.errors[0]}")

    if problems:
        return False, "; ".join(problems[:4])
    return True, (
        f"{len(report['contracts'])} contract(s) consistent, generated Python/TypeScript/C# "
        f"bindings current, {checked} live tool row(s) validated, unproven receipt graded UNVERIFIED"
    )


def op_capability_matrix(root: Path) -> tuple[bool, str]:
    """CAPABILITY_MATRIX.md matches the registry, and no unverifiable claim is counted (4.7)."""
    from tools.capability_matrix import MATRIX_DOC, MATRIX_JSON, build

    matrix = build(Path(root))
    data = matrix.to_json()
    summary = data["summary"]
    problems: list[str] = []
    if not MATRIX_DOC.exists():
        problems.append("CAPABILITY_MATRIX.md is missing")
    elif MATRIX_DOC.read_text(encoding="utf-8") != matrix.markdown():
        problems.append("CAPABILITY_MATRIX.md is stale")
    if not MATRIX_JSON.exists():
        problems.append("artifacts/capabilities.json is missing")
    else:
        committed = json.loads(MATRIX_JSON.read_text(encoding="utf-8"))
        if committed != data:
            problems.append("artifacts/capabilities.json is stale")
    if summary["invalid_evidence"]:
        problems.append(
            "unusable evidence: "
            + "; ".join(f"{item['tool']}: {item['problem']}" for item in summary["invalid_evidence"])
        )
    if summary["phantom_evidence"]:
        problems.append("recordings for undeclared tools: " + ", ".join(summary["phantom_evidence"]))
    if problems:
        return False, "; ".join(problems) + " (run python3 tools/capability_matrix.py)"
    return True, (
        f"{summary['tools']} declared tool(s): {summary['green']} green, "
        f"{summary['provisional']} unproven, {summary['unavailable']} without a driver profile; "
        f"{summary['with_runs']} with recorded runs; document and JSON current"
    )


def op_hardware_sensors(root: Path) -> tuple[bool, str]:
    """Sensing is real, filtered, and protective only when it should be (spec 16).

    The checks are the ones that would catch a plausible-looking fake: a bad reading must not
    fire an alert, an alert must close when the condition ends, and loosening a threshold must
    need the owner. Anything less and the sensor panel is decoration.
    """
    from zyntharo.ops.sensors import DEFAULT_RULES, ProtectionPolicy, Reading, SensorMonitor

    class Source:
        def __init__(self, readings):
            self.readings = readings
            self.last_duration = 0.0005

        def collect(self):
            return [Reading(**reading) for reading in self.readings]

        def filter(self, readings, previous=None):
            from zyntharo.ops.sensors import Collector

            return Collector(sources=()).filter(readings, previous)

    def reading(value):
        return {"key": "cpu.temp", "label": "cpu.temp", "value": value, "unit": "C", "source": "gate", "ts": time.time()}

    fired: list[Any] = []
    monitor = SensorMonitor(
        collector=Source([reading(5000.0)]),
        policy=ProtectionPolicy(notify=fired.append, reduce_load=lambda alert: fired.append(alert)),
        rules=DEFAULT_RULES,
    )
    monitor.sample()
    if fired:
        return False, "an impossible reading (5000 C) fired an alert; the plausibility filter is not enforced"
    if not monitor.last_snapshot or not monitor.last_snapshot.readings[0].suspect:
        return False, "the impossible reading was not reported as suspect, so the fault is invisible"

    hot = SensorMonitor(
        collector=Source([reading(96.0)]),
        policy=ProtectionPolicy(notify=fired.append, reduce_load=lambda alert: fired.append(alert)),
        rules=tuple(rule for rule in DEFAULT_RULES if rule.rule == "cpu.temp.high"),
    )
    hot.sample()
    if not hot.active:
        return False, "96 C did not fire the CPU temperature rule"
    if len(fired) != 2:
        return False, f"a danger alert notified/reduced load {len(fired)} time(s), expected notify + reduce"
    hot.collector.readings = [reading(60.0)]
    hot.sample()
    hot.sample()
    if hot.active:
        return False, "the alert never cleared after the sensor returned to 60 C"

    if monitor.tune("cpu.temp.high", 200.0).get("tuned"):
        return False, "an agent-loosened threshold was accepted; protective alerts could be silenced"
    if not monitor.tune("cpu.temp.high", 70.0).get("tuned"):
        return False, "a tightened threshold was refused"
    if not monitor.tune("cpu.temp.high", 80.0, owner=True).get("tuned"):
        return False, "the owner could not loosen a threshold"

    overhead = monitor.overhead()
    report = monitor.sensor_report()
    return True, (
        f"impossible reading flagged not fired, 96 C alerted and cleared, threshold tightening "
        f"allowed and loosening owner-only, {len(report['found'])} sensor(s) found, "
        f"{len(report['unavailable'])} reported unavailable, own cost {overhead['collector_cpu_ms_total']} ms over "
        f"{overhead['window_s']} s"
    )


def op_local_index(root: Path) -> tuple[bool, str]:
    """Local search: real extraction, incremental scans, verified hits, honest gaps (14.2)."""
    import os
    import shutil
    import tempfile

    from zyntharo.ops.index import SearchIndex

    home = Path(tempfile.mkdtemp(prefix="zyntharo-index-"))
    corpus = home / "docs"
    (corpus / "node_modules").mkdir(parents=True)
    (corpus / "notes.md").write_text("# Pump Log\n\nthe coolant pump vibrates at low rpm\n", encoding="utf-8")
    (corpus / "node_modules" / "dep.md").write_text("coolant dependency noise", encoding="utf-8")
    (corpus / "scan.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
    index = SearchIndex(home / "index.db")
    try:
        index.add_source("docs", corpus)
        first = index.update()
        if first["updated"] < 1:
            return False, f"nothing was indexed from a corpus of readable files: {first}"
        hits = index.search("coolant")
        found = [hit for hit in hits["hits"] if hit["path"].endswith("notes.md")]
        if not found or not found[0]["verified"]:
            return False, f"the expected hit was missing or unverified: {hits['hits'][:3]}"
        if all("node_modules" not in hit["path"] for hit in hits["hits"]) is False:
            return False, "an excluded directory (node_modules) was indexed"

        second = index.update()
        if second["updated"] != 0:
            return False, f"an unchanged corpus was re-indexed ({second['updated']} file(s))"

        notes = corpus / "notes.md"
        notes.write_text("# Pump Log\n\nthe impeller is scuffed\n", encoding="utf-8")
        os.utime(notes, (time.time() + 5, time.time() + 5))
        changed = index.update()
        if changed["updated"] != 1 or index.search("coolant")["count"] != 0:
            return False, "a changed file was not re-indexed; the old text would still be returned"

        notes.unlink()
        removed = index.update()
        if removed["removed"] != 1 or index.search("impeller")["count"] != 0:
            return False, "a deleted file stayed in the index"

        try:
            index.add_source("mail", corpus, kind="email")
            return False, "a personal source was indexed without an explicit opt-in"
        except PermissionError:
            pass

        status = index.status()
        if status["capabilities"]["semantic"]:
            return False, "the index claims semantic search; this build has no embeddings"
        if status["unextractable"] < 1:
            return False, "an unreadable file was not reported as unextractable"
        return True, (
            f"{status['documents']} document(s) indexed, hit verified on disk, exclusions honoured, "
            f"change and deletion handled, personal source refused, {status['unextractable']} "
            f"unextractable file(s) reported; semantic={status['capabilities']['semantic']}, "
            f"ocr={status['capabilities']['ocr']}, pdf={status['capabilities']['pdf']}"
        )
    finally:
        index.close()
        shutil.rmtree(home, ignore_errors=True)


OPS: dict[str, Callable[[Path], tuple[bool, str]]] = {
    "m0_slice": op_m0_slice,
    "hud_coverage": op_hud_coverage,
    "plugin_lifecycle": op_plugin_lifecycle,
    "supervisor_cleanup": op_supervisor_cleanup,
    "owner_gate": op_owner_gate,
    "catalog_rollback": op_catalog_rollback,
    "language_rtl": op_language_rtl,
    "risk_tiers": op_risk_tiers,
    "preflight": op_preflight,
    "billing_modes": op_billing_modes,
    "governor": op_governor,
    "journal": op_journal,
    "reality_gate": op_reality_gate,
    "capability_matrix": op_capability_matrix,
    "contracts": op_contracts,
    "terminal_engine": op_terminal_engine,
    "office_tier_b": op_office_tier_b,
    "hardware_sensors": op_hardware_sensors,
    "local_index": op_local_index,
}
