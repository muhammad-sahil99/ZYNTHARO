"""The ``orbynth`` command line: the entry point for everything the system does.

Subcommands are thin: each one assembles the same stack (config → bus → memory → providers →
quotas → runtime → actions → plugins → orchestrator → HUD/voice) and then does one thing.
``build_stack`` is that assembly, shared by the CLI, the tests and any embedding program.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from . import __alternate_brand__, __product__, __tagline__, __version__
from .actions import build_registry, driver_report, orchestration
from .agents.orchestrator import Orchestrator, WorkItem
from .config import AppConfig, Secrets, default_config, load_config
from .events import LOG, EventBus
from .llm.providers import ProviderRegistry
from .llm.quotas import QuotaManager
from .llm.runtime import ModelRuntime
from .memory import MemoryStore
from .ops.languages import ocr_languages
from .ops.preflight import TaskRequirements
from .plugins.forge import Forge
from .plugins.loader import PluginHost
from .resources import resource_path
from .voice.session import TextOnlyVoice, VoiceSession

# User installations resolve the bundled HUD from PyInstaller's payload; source runs resolve it
# from the repository.  Nothing relies on the current working directory.
HUD_DIR = resource_path("hud")


@dataclass
class Stack:
    """Everything wired together — the assembled system."""

    config: AppConfig
    bus: EventBus
    memory: MemoryStore
    registry: Any
    providers: ProviderRegistry
    quotas: QuotaManager
    runtime: ModelRuntime
    plugins: PluginHost
    forge: Forge
    orchestrator: Orchestrator
    voice: Any = None
    hud: Any = field(default=None)
    # -- operational layer (contract sections 22.1 - 22.11) ---------------------------
    supervisor: Any = None
    owner: Any = None
    billing: Any = None
    governor: Any = None
    preflight: Any = None
    verifier: Any = None
    sensors: Any = None
    index: Any = None
    #: Speaker recognition, the consent-gated camera, and the etiquette layer that fuses them.
    speakers: Any = None
    camera: Any = None
    presence: Any = None
    #: Failure memory and work queued for later.
    lessons: Any = None
    scheduler: Any = None
    #: The sellable surface: connected platforms and the three business workflows.
    channels: Any = None
    workflows: dict[str, Any] = field(default_factory=dict)
    #: The customer's own outside accounts (mail, Drive, calendar, chat, CRM, code, payments).
    connectors: Any = None
    reply_desk: Any = None

    def shutdown(self) -> None:
        if self.index is not None:
            try:
                self.index.close()
            except Exception:
                pass
        if self.sensors is not None:
            try:
                self.sensors.stop()
            except Exception:
                pass
        if self.voice is not None:
            try:
                self.voice.stop()
            except Exception:
                pass
        if self.scheduler is not None:
            try:
                self.scheduler.stop()
            except Exception:
                pass
        self.orchestrator.shutdown()
        if self.hud is not None:
            try:
                self.hud.bridge.stop()
            except Exception:
                pass
        # The automation browser is the largest thing this app can leave behind: release it
        # before the supervisor so a Playwright Chromium (driver + browser + tabs) does not
        # keep several hundred megabytes alive after the task is over.
        try:
            from .actions import browser as browser_actions

            browser_actions.release()
        except Exception:
            pass
        # Last: everything we started dies with us (sidecar, automation browser, workers).
        if self.supervisor is not None:
            try:
                self.supervisor.shutdown(grace=3.0)
                from .ops.spawn import set_supervisor

                set_supervisor(None)
            except Exception:
                pass
        self.memory.close()


def build_stack(
    config: AppConfig,
    start_agents: bool = True,
    load_plugins: bool = True,
    with_voice: bool = False,
    with_hud: bool = False,
) -> Stack:
    """Assemble the whole system from configuration.

    The operational layer is created here and not somewhere else, because "assembled but not
    wired" is a failure mode this project already had once: a supervisor that nothing starts
    processes through, an owner gate nothing asks, a preflight nothing consults. Every system
    built in `ops/` is given to the parts that must use it, on this path.
    """
    from .ops.billing import BillingGate, GatewayPolicy
    from .ops.catalog import ModelCatalog
    from .ops.governor import ResponsivenessGovernor
    from .ops.journal import Journal
    from .ops.owner import OwnerRegistry
    from .ops.preflight import Preflight
    from .ops.spawn import set_supervisor
    from .ops.supervisor import Supervisor
    from .ops.verification import OutcomeVerifier

    bus = EventBus()
    memory = MemoryStore(config.db_path)
    secrets = Secrets(config.home_path)
    registry_actions = build_registry(config, bus, memory)
    # The action journal is the write-ahead log behind exactly-once behaviour and "undo last
    # task" (spec 4.5). It has to exist before the first tool call, not after it: an intention
    # that was never recorded cannot be reconciled. Startup also replays whatever the previous
    # session left open and reports it, rather than assuming the machine is in the state the
    # last run intended.
    journal = Journal(config.home_path / "journal.db", bus)
    registry_actions.journal = journal
    reconciliation = journal.reconcile() if config.ops.reconcile_on_start else {"checked": 0}
    if reconciliation.get("checked"):
        print(f"[journal] {reconciliation['summary']}", file=sys.stderr)
    providers = ProviderRegistry(config.providers, secrets, bus)
    quotas = QuotaManager(config, bus, persist_path=config.home_path / "quota.json")

    billing = BillingGate(
        config.billing.mode,
        GatewayPolicy(
            endpoint=config.billing.gateway_endpoint,
            global_daily_requests=config.billing.global_daily_requests,
            per_user_daily_requests=config.billing.per_user_daily_requests,
            per_user_daily_tokens=config.billing.per_user_daily_tokens,
            global_daily_spend_usd=config.billing.global_daily_spend_usd,
            kill_switch=config.billing.kill_switch,
            resale_acknowledged=dict(config.billing.resale_acknowledged),
            legal_review_reference=config.billing.legal_review_reference,
        ),
        local_only=any("localhost" in (p.base_url or "") or "127.0.0.1" in (p.base_url or "") for p in config.providers),
    )
    runtime = ModelRuntime(config, providers, quotas, memory, bus)
    runtime.billing = billing  # type: ignore[attr-defined] - enforced on every model call

    # Read the pinned catalogue if one is configured: model IDs go stale without a release.
    if config.catalog.path:
        try:
            catalog = ModelCatalog.load(config.catalog.path)
            key_pem = ""
            if config.catalog.public_key_path and Path(config.catalog.public_key_path).exists():
                key_pem = Path(config.catalog.public_key_path).read_text(encoding="utf-8")
            hmac_key = os.environ.get(config.catalog.hmac_key_env, "") if config.catalog.hmac_key_env else ""
            verification = catalog.verify(
                public_key_pem=key_pem, hmac_key=hmac_key, allow_unsigned=config.catalog.allow_unsigned
            )
            if verification.ok:
                catalog.apply_to_config(config)
        except Exception as exc:  # a bad catalogue must not stop the app from starting
            bus.emit(LOG, source="catalog", severity="warning", message=f"catalogue not applied: {exc!r}")

    owner = OwnerRegistry(config.home_path, bus)
    registry_actions.owner = owner
    governor = (
        ResponsivenessGovernor(bus, max_workers=config.safety.max_parallel_agents)
        if config.ops.governor_enabled
        else None
    )
    # The preflight asks the governor what is busy: without that, its "machine busy" branch could
    # only ever be reached from a test, never from a real game or call (contract 22.11).
    preflight = Preflight(
        bus,
        busy_provider=(governor.busy_reason if governor is not None else None),
        isolation_available=config.ops.isolated_session_available,
    )
    verifier = _build_verifier(bus, config)

    supervisor = Supervisor(config.home_path, bus) if config.ops.supervisor_enabled else None
    if supervisor is not None:
        set_supervisor(supervisor, bus)
        supervisor.start(reconcile=config.ops.reconcile_on_start)
    if governor is not None and config.ops.low_priority:
        governor.apply_priority("below_normal")

    plugins = PluginHost(config.home_path, registry_actions, bus, memory=memory)
    forge = Forge(plugins, runtime, registry_actions, bus, memory, workdir=config.home_path / "forge")
    if load_plugins:
        plugins.load_all()

    orchestrator = Orchestrator(
        config,
        runtime,
        registry_actions,
        memory,
        bus,
        governor=governor,
        verifier=verifier,
        preflight=preflight,
    )
    orchestration.set_services(orchestration.Services(orchestrator, runtime, memory, bus, config))

    # Channels and workflows are the product the customer buys: the platforms they already use,
    # and the three processes that remove recurring work. Both are created here so the CLI, the
    # HUD and the fleet's tools share one registry and one outbox.
    from .actions import channels as channel_actions
    from .channels import default_registry
    from .workflows import DailyReport, InvoiceIntake, ReplyDesk

    channel_registry = default_registry(str(config.home_path))
    reply_desk = ReplyDesk(config.home_path / "reply_desk.db")
    workflow_map: dict[str, Any] = {
        "invoice_intake": InvoiceIntake(),
        "daily_report": DailyReport(),
        "reply_desk": reply_desk,
    }
    # (wired below, once the connector registry exists: the reply desk reads a mailbox through it)

    # Connected accounts (Gmail/Outlook/Drive/Sheets/Slack/Notion/…). Same reasoning as channels:
    # one registry, created here, reachable from the CLI, the HUD and every agent's tools. It is
    # built from the local vault on each call, so connecting an account needs no restart.
    from .actions import connectors as connector_actions
    from .connectors import default_registry as default_connector_registry

    connector_registry = default_connector_registry(str(config.home_path))
    connector_actions.set_services(connector_actions.ConnectorServices(connector_registry))
    # The channel actions get the connector registry too, so an unconnected mailbox fails with the
    # connector's own reason rather than a KeyError.
    channel_actions.set_services(
        channel_actions.ChannelServices(channel_registry, workflow_map, reply_desk, connector_registry)
    )
    if start_agents:
        orchestrator.bootstrap_roster()

    voice: Any = None
    if with_voice:
        voice = VoiceSession(config.voice, bus) if config.voice.enabled else TextOnlyVoice(bus)

    # The WhatsApp call path needs the config (for voice tiers) and the session's STT (so the
    # caller's words arrive as text). Both are wired once, here, at assembly.
    from .actions import whatsapp as whatsapp_actions

    whatsapp_actions.set_config(config)
    transcriber = getattr(voice, "transcribe_pcm", None) if voice is not None else None
    if callable(transcriber):
        whatsapp_actions.set_transcriber(transcriber)

    # The local index (spec 14.2) has no sources until the owner adds one: indexing a user's
    # documents without being asked is exactly the kind of helpfulness that is not helpful.
    from .actions import localsearch as localsearch_actions
    from .ops.index import SearchIndex

    search_index = SearchIndex(config.home_path / "index.db", bus)
    localsearch_actions.set_index(search_index)

    if config.ops.sensors_enabled:
        sensors = _build_sensors(bus, config, governor, voice)
        from .actions import hardware as hardware_actions

        hardware_actions.set_monitor(sensors)
        sensors.start()

    # Inter-agent comms and the model bench. Both need the roster and the quotas, so they are
    # created after the orchestrator exists — but before the HUD, which shows them.
    from .actions import fleet as fleet_actions
    from .agents.comms import AgentComms
    from .llm.reserve import ReservePool

    comms = AgentComms(
        config.home_path / "comms.db",
        bus,
        roster=lambda: [
            {"agent_id": a.id, "name": a.name, "role": a.identity.role, "rank": a.identity.rank}
            for a in orchestrator.agents.values()
        ],
        memory=memory,
    )
    reserves = ReservePool(config, quotas, bus, path=config.home_path / "reserves.json")
    fleet_actions.set_services(
        fleet_actions.FleetServices(
            comms=comms,
            reserves=reserves,
            orchestrator=orchestrator,
            runtime=runtime,
            config=config,
            memory=memory,
            bus=bus,
        )
    )

    stack = Stack(
        config=config,
        bus=bus,
        memory=memory,
        registry=registry_actions,
        providers=providers,
        quotas=quotas,
        runtime=runtime,
        plugins=plugins,
        forge=forge,
        orchestrator=orchestrator,
        voice=voice,
        supervisor=supervisor,
        owner=owner,
        billing=billing,
        governor=governor,
        preflight=preflight,
        verifier=verifier,
        sensors=sensors,
        index=search_index,
        channels=channel_registry,
        workflows=workflow_map,
        reply_desk=reply_desk,
        connectors=connector_registry,
    )

    # Presence: who is talking, who is in the room, and whether speaking aloud is appropriate.
    # Created here (not inside the HUD) because the agent tools and the interface must see the
    # same instance; a second CameraService would mean two consent records and two cameras open.
    from .actions import presence as presence_actions
    from .ops.presence import PresenceController
    from .vision.camera import CameraService
    from .voice.speakers import SpeakerRegistry

    speaker_registry = SpeakerRegistry(config.home_path, bus)
    camera_service = CameraService(config.home_path, bus)
    presence_controller = PresenceController(speaker_registry, camera_service, bus)
    presence_actions.set_services(
        presence_actions.PresenceServices(
            speakers=speaker_registry,
            camera=camera_service,
            presence=presence_controller,
            voice=voice,
            config=config,
            bus=bus,
        )
    )
    stack.speakers = speaker_registry
    stack.camera = camera_service
    stack.presence = presence_controller

    # Live utterances are identified from the same captured buffer that gets transcribed: one
    # microphone, no second poller. Without this, "who is talking" would only work on files.
    if voice is not None and hasattr(voice, "on_audio"):
        from .voice.speakers import analyse_pcm

        def _identify_speakers(pcm: bytes, rate: int) -> None:
            presence_controller.observe_voice(analyse_pcm(pcm, rate, registry=speaker_registry))

        voice.on_audio = _identify_speakers

    # Scheduled work, and the memory of failures that should not repeat. The gate is the
    # preflight layer: GUI work is deferred while the session is locked, and a window missed by
    # more than the grace period is skipped with a reason instead of replayed in a burst.
    from .actions import schedule as schedule_actions
    from .ops.lessons import LessonBook
    from .ops.schedule import Scheduler

    lesson_book = LessonBook(config.home_path, bus)
    # Attached to the registry, which is what puts the known fix onto the next failure of the
    # same kind and what agents read in their system prompt before choosing a tool.
    registry_actions.lessons = lesson_book

    def _scheduled_gate(entry: Any) -> tuple[bool, str]:
        if not getattr(entry, "needs_gui", False):
            return True, ""
        try:
            from .ops.preflight import TaskRequirements

            decision = orchestrator.preflight_task(
                TaskRequirements(gui=True, unlocked_session=True, background_ok=True), origin="schedule"
            )
        except Exception as exc:  # noqa: BLE001 - an unavailable gate is a refusal, never a pass
            return False, f"preflight unavailable: {exc}"
        if decision is None:
            return True, ""
        return bool(getattr(decision, "can_run", False)), "; ".join(getattr(decision, "reasons", []) or [])

    def _run_scheduled(entry: Any) -> Any:
        agent = orchestrator.spawn(role="operative", name=f"Scheduled-{entry.id[:4]}")
        return agent.run_task(
            f"scheduled: {entry.title}",
            entry.objective,
            context="Queued through the scheduler; nobody is watching this run, so report what happened.",
            max_steps=20,
        )

    scheduler = Scheduler(bus, runner=_run_scheduled, gate=_scheduled_gate, path=config.home_path / "schedule.json")
    schedule_actions.set_services(
        schedule_actions.ScheduleServices(scheduler=scheduler, lessons=lesson_book, bus=bus, config=config)
    )
    if start_agents:
        scheduler.start()
    stack.lessons = lesson_book
    stack.scheduler = scheduler
    if with_hud:
        from .hud.service import HudService

        hud = HudService(
            config=config,
            bus=bus,
            memory=memory,
            registry=registry_actions,
            runtime=runtime,
            quotas=quotas,
            orchestrator=orchestrator,
            plugins=plugins,
            voice=voice,
            static_dir=HUD_DIR,
            supervisor=supervisor,
            owner=owner,
            billing=billing,
            governor=governor,
            verifier=verifier,
            sensors=stack.sensors,
            index=stack.index,
            comms=comms,
            reserves=reserves,
            speakers=speaker_registry,
            camera=camera_service,
            presence=presence_controller,
            lessons=lesson_book,
            scheduler=scheduler,
            connectors=connector_registry,
            replies=reply_desk,
            channels=channel_registry,
        )
        hud.forge = forge  # type: ignore[attr-defined]
        if voice is not None:
            voice.on_utterance = lambda text: hud._run_sovereign(text)  # noqa: SLF001 - single wiring point
        stack.hud = hud
    stack.journal = journal
    stack.comms = comms
    stack.reserves = reserves
    return stack


def _build_sensors(bus: EventBus, config: AppConfig, governor: Any, voice: Any) -> Any:
    """The hardware monitor with its protective hooks wired to this app's real parts.

    The monitor decides *what* a reading means; these callables decide what the app can do
    about it. Keeping them apart is what lets a test drive a rule without a machine on fire.
    """
    from .ops.sensors import ProtectionPolicy, SensorMonitor

    def notify(alert: Any) -> None:
        bus.emit(
            HUD_NOTICE,
            source="sensors",
            severity="warning",
            message=f"{alert.severity.upper()}: {alert.message}",
            alert=alert.to_json(),
        )
        speaker = getattr(voice, "say", None)
        if callable(speaker) and alert.severity in ("danger", "critical"):
            speaker(f"{alert.message}. {', '.join(alert.actions[:2])}.", block=False)

    def reduce_load(alert: Any) -> None:
        if governor is not None:
            governor.apply_priority("below_normal")
        bus.emit(HUD_NOTICE, source="sensors", message="background work slowed to protect the machine")

    def safe_state(alert: Any) -> None:
        bus.emit(
            HUD_NOTICE,
            source="sensors",
            severity="error",
            message=f"critical sensor state: {alert.message}; stop new work and save anything open",
        )

    def power_down(alert: Any) -> None:
        # Only reachable when the owner turned ``allow_power_down`` on: it suspends the machine.
        if os.name == "nt":
            subprocess.run(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"], check=False)
        else:
            subprocess.run(["systemctl", "suspend"], check=False)

    policy = ProtectionPolicy(
        notify=notify,
        reduce_load=reduce_load,
        safe_state=safe_state,
        power_down=power_down,
        allow_power_down=config.ops.allow_power_down,
    )
    return SensorMonitor(bus=bus, policy=policy, interval=config.ops.sensor_interval)



def _build_verifier(bus: EventBus, config: AppConfig) -> Any:
    """Outcome checks whose probes are the real filesystem, not a stub.

    Only criteria this machine can answer are registered. ``url_is`` and ``value_equals`` are
    deliberately left out: a missing probe reports ``uncertain``, which is honest, whereas a
    stub that says "looks fine" would turn a claim into a pass.
    """
    from .ops.languages import equivalent, normalize
    from .ops.verification import OutcomeVerifier

    verifier = OutcomeVerifier(bus)

    def file_exists(criterion: Any, observations: dict[str, Any]) -> tuple[bool, str]:
        raw = observations.get("resolved", {}).get(criterion.target, criterion.target)
        path = Path(str(raw)).expanduser()
        return path.exists(), f"{'found' if path.exists() else 'missing'}: {path}"

    def text_present(criterion: Any, observations: dict[str, Any]) -> tuple[bool, str]:
        raw = observations.get("resolved", {}).get(criterion.target, criterion.target)
        path = Path(str(raw)).expanduser()
        if not path.exists():
            return False, f"cannot check {path}: it does not exist"
        try:
            body = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return False, f"cannot read {path}: {exc}"
        needle = normalize(str(criterion.expected or ""))
        hit = equivalent(body, body) and needle in normalize(body)
        return hit, f"read {path}, looked for {needle!r}"

    def count_at_least(criterion: Any, observations: dict[str, Any]) -> tuple[bool, str]:
        raw = observations.get("resolved", {}).get(criterion.target, criterion.target)
        path = Path(str(raw)).expanduser()
        wanted = int(criterion.expected or 1)
        if path.is_dir():
            found = len(list(path.iterdir()))
        elif path.exists():
            found = len(path.read_text(encoding="utf-8", errors="replace").splitlines())
        else:
            return False, f"{path} does not exist"
        return found >= wanted, f"{path}: {found} of {wanted} required"

    verifier.register_probe("file_exists", file_exists)
    verifier.register_probe("text_present", text_present)
    verifier.register_probe("count_at_least", count_at_least)
    return verifier


def sovereign(stack: Stack, objective: str, context: str = "", expected_outputs: Sequence[str] = ()) -> dict[str, Any]:
    """Send a request to the Sovereign agent (creating one if the roster is empty).

    The request gets a written contract first (contract 22.4). It used to be handed straight
    to the agent with no criteria at all, which meant the *primary* entry point skipped outcome
    verification entirely: every step could run and nobody checked the goal.
    """
    from .agents.orchestrator import Agent
    from .ops.verification import contract_for_request

    agent = next((a for a in stack.orchestrator.agents.values() if a.identity.role == "sovereign"), None)
    if agent is None:
        agent = stack.orchestrator.spawn(role="sovereign", name="Sovereign", capabilities=["*"])
    contract = contract_for_request("user request", objective, expected_outputs) if stack.verifier else None
    report = agent.run_task("user request", objective, context=context, contract=contract)
    return report.to_json()


# --------------------------------------------------------------------------------------
# Subcommands
# --------------------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    config = load_config(args.config) if args.config else default_config()
    if args.config is None:
        config.home = str(Path(args.home).expanduser()) if args.home else config.home
    path = config.save()
    print(f"ORBYNTH home: {config.home_path}")
    print(f"config written: {path}")
    print(f"database: {config.db_path}")
    print("\nNext: add a provider key (or keep the offline 'echo' provider for a test drive), then run:")
    print("  orbynth doctor")
    print("  orbynth hud")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    secrets = Secrets(config.home_path)
    providers = ProviderRegistry(config.providers, secrets)
    stack = build_stack(config, start_agents=False, load_plugins=True)
    print(f"{__product__} {__version__} — {__tagline__}")
    print(f"alternate brand name: {__alternate_brand__}")
    print(f"home: {config.home_path}")
    print(f"python: {sys.version.split()[0]} on {sys.platform}")
    print("\nModels:")
    for row in providers.inventory():
        state = "ready" if row["ready"] else "no key"
        print(f"  {row['qualified']:<40} {state:<8} rpm={row['rpm']} rpd={row['rpd']} tpm={row['tpm']} tags={row['tags']}")
    print("\nDrivers:")
    for row in driver_report():
        mark = "ok" if row["available"] else "--"
        hint = f"  ({row['hint']})" if not row["available"] and row["hint"] else ""
        print(f"  [{mark}] {row['driver']:<10} {row['module']}{hint}")
    print(f"\nActions registered: {len(stack.registry)}")
    for group, names in stack.registry.groups().items():
        print(f"  {group:<16} {len(names):>3} actions")
    print(f"\nPlugins: {len(stack.plugins.installed())} installed")
    for plugin in stack.plugins.installed():
        print(f"  {plugin['name']:<24} {plugin.get('status', 'unknown'):<10} actions={plugin.get('actions', [])}")
    print(f"\nMemory: {json.dumps(stack.memory.stats())}")
    print("\nOperations:")
    owner = stack.owner
    if owner is None:
        print("  [--] owner identity: not wired")
    elif not owner.profile.configured:
        print("  [!!] owner identity: NOT configured — run `orbynth owner setup`; irreversible")
        print("       actions are refused until the owner exists")
    else:
        state = "session open" if owner.session_valid() else "locked"
        print(f"  [ok] owner identity: {owner.profile.owner_name} ({owner.profile.machine_kind}, {state})")
        if owner.foreign_profile():
            print(f"       profile belongs to OS user {owner.profile.os_user!r} — shared PC isolation")
    print(f"  [{'ok' if stack.supervisor else '--'}] supervisor: " + ("owning helper processes; jobs die with this app" if stack.supervisor else "disabled (config.ops.supervisor_enabled=false)"))
    if stack.supervisor is not None:
        inventory = stack.supervisor.inventory()
        print(f"       journal: {inventory['journal']}  jobs now: {inventory['running']}")
    if stack.governor is not None:
        state = stack.governor.summary()
        paused = state["state"]["paused"]
        print(f"  [ok] governor: priority={state['priority']}, background paused={paused.get('background')}")
    else:
        print("  [--] governor: disabled")
    issues = stack.billing.audit(config) if stack.billing is not None else []
    blocking = [i for i in issues if i.severity == "blocking"]
    mode = stack.billing.mode if stack.billing is not None else "n/a"
    print(f"  [{'ok' if not blocking else '!!'}] billing: mode={mode}" + (f", {len(blocking)} blocking issue(s) — run `orbynth billing`" if blocking else ""))
    languages = [config.voice.language, *config.voice.secondary_languages]
    print(f"  [ok] languages: {', '.join(languages)} (OCR packs: {ocr_languages(languages)})")
    print(f"  [ok] verification: {len(stack.verifier.probes) if stack.verifier else 0} deterministic probe(s); subjective results go to the owner")
    warnings: list[str] = []
    if not any(row["ready"] for row in providers.inventory()):
        warnings.append(
            "No provider has credentials. ORBYNTH still runs end to end on the offline 'echo' provider, "
            "but real work needs a key: set e.g. GEMINI_API_KEY, or add a custom provider in config.json."
        )
    absent = [row["driver"] for row in driver_report() if not row["available"]]
    if absent:
        warnings.append("Some desktop drivers are not installed here: " + ", ".join(sorted(set(absent))))
    for warning in warnings:
        print(f"\nwarning: {warning}")
    stack.shutdown()
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    stack = build_stack(config, with_voice=args.voice)
    print(f"[{__product__}] objective: {args.objective}")
    requirements = TaskRequirements(
        gui=getattr(args, "needs_gui", False),
        unlocked_session=getattr(args, "needs_gui", False),
        user_present=getattr(args, "needs_user", False),
        power=getattr(args, "needs_power", False),
        network=config.safety.allow_network,
        background_ok=True,
    )
    decision = stack.preflight.check(requirements, origin="user_request") if stack.preflight else None
    if decision is not None:
        print(f"preflight: {decision.one_line()}")
        if not decision.can_run:
            # Deferred, not dropped, and not faked: record why and how to make it runnable.
            stack.memory.remember(
                "owner",
                f"deferred objective ({decision.action}): {args.objective[:200]} — {'; '.join(decision.reasons)}",
                kind="deferred",
                tags=["deferred", decision.action],
                importance=0.6,
            )
            print(f"not started: {'; '.join(decision.reasons)}")
            if decision.advice:
                print(f"advice: {decision.advice}")
            stack.shutdown()
            return 3
    started = time.time()
    result = sovereign(stack, args.objective, context=args.context or "", expected_outputs=tuple(args.expect or ()))
    print(f"\nstatus: {result['status']}  ({time.time() - started:.1f}s)")
    print(f"summary: {result['summary']}")
    verdict = result.get("verdict")
    if verdict:
        print(f"outcome check: {verdict['status']} — {verdict['summary']}")
        if verdict.get("question"):
            print(f"needs you: {verdict['question']}")
    if result.get("evidence"):
        print("evidence:")
        for item in result["evidence"]:
            print(f"  - {item}")
    if result.get("blockers"):
        print("blockers:")
        for item in result["blockers"]:
            print(f"  - {item}")
    if result.get("next_step"):
        print(f"next: {result['next_step']}")
    print(f"actions used: {result['actions']}   tokens: {result['tokens']}")
    stack.shutdown()
    return 0 if result["status"] in {"done", "partial"} else 1


def cmd_team(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    stack = build_stack(config)
    raw = json.loads(args.items) if args.items else [{"title": args.objective[:60], "objective": args.objective}]
    items = [
        WorkItem(
            title=i.get("title", ""),
            objective=i.get("objective", ""),
            role=i.get("role", "operative"),
            capabilities=i.get("capabilities", []),
            criteria=i.get("criteria", []) or [],
            expected_outputs=i.get("expected_outputs", []) or [],
            work_class=i.get("work_class", "interactive"),
        )
        for i in raw
    ]
    # How wide this host may go is reported, not assumed (contract 22.5).
    plan = stack.orchestrator.capacity_plan(total_items=len(items))
    print(f"capacity plan: {plan['one_line'] if 'one_line' in plan else plan}")
    result = stack.orchestrator.run_team(
        args.objective,
        items,
        parallel=not args.sequential and bool(plan["parallel"]),
    )
    verdicts = [r.get("verdict", {}).get("status") for r in result["reports"] if r.get("verdict")]
    print(
        json.dumps(
            {
                "team": result["team"],
                "summary": result["summary"],
                "outcome_checks": verdicts,
                "reports": result["reports"],
            },
            indent=2,
        )
    )
    disputed = [r for r in result["reports"] if r.get("status") == "disputed"]
    stack.shutdown()
    return 0 if not disputed else 1


def cmd_channels(args: argparse.Namespace) -> int:
    """What each platform can really do here, and the honest offer for the rest."""
    from .channels import declared_platforms

    config = load_config(args.config)
    stack = build_stack(config, start_agents=False, load_plugins=False)
    try:
        if args.action == "send":
            channel = stack.channels.get(args.channel)
            print(json.dumps(channel.send(args.chat, args.text), indent=2))
            return 0
        if args.action == "inbox":
            channel = stack.channels.get(args.channel)
            rows = channel.inbox(limit=args.limit, chat=args.chat)
            print(json.dumps([row.to_json() for row in rows], indent=2))
            return 0
        payload = {"channels": stack.channels.to_json(), "catalogue": declared_platforms()}
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            for row in payload["channels"]:
                print(f"{row['state']:<12} {row['name']:<10} {row['detail']}")
        return 0
    finally:
        stack.shutdown()


def cmd_connectors(args: argparse.Namespace) -> int:
    """The customer's outside accounts: what is connected, what is missing, what cannot be reached."""
    config = load_config(args.config)
    stack = build_stack(config, start_agents=False, load_plugins=False)
    try:
        registry = stack.connectors
        if args.action == "verify":
            status = registry.verify(args.connector)
            print(json.dumps(status.to_json(), indent=2))
            return 0 if status.state == "ready" else 1
        if args.action == "disconnect":
            removed = registry.disconnect(args.connector)
            print(json.dumps({"connector": args.connector, "disconnected": removed}, indent=2))
            return 0 if removed else 1
        if args.action == "connect":
            try:
                values = json.loads(args.values or "{}")
            except ValueError as exc:
                print(f"error: --values must be a JSON object: {exc}", file=sys.stderr)
                return 2
            if not isinstance(values, dict) or not values:
                print("error: --values must be a non-empty JSON object of that connector's fields", file=sys.stderr)
                return 2
            meta = json.loads(args.meta) if args.meta else {}
            status = registry.connect(args.connector, {k: str(v) for k, v in values.items()}, meta)
            print(json.dumps(status.to_json(), indent=2))
            return 0 if status.state == "ready" else 1
        if args.action == "call":
            try:
                payload = json.loads(args.args or "{}")
            except ValueError as exc:
                print(f"error: --args must be a JSON object: {exc}", file=sys.stderr)
                return 2
            print(json.dumps(registry.call(args.connector, args.operation, **payload), indent=2))
            return 0
        catalogue = registry.catalogue()
        if args.json:
            print(json.dumps(catalogue, indent=2))
            return 0
        for category, rows in catalogue["categories"].items():
            print(f"\n{category}")
            for row in rows:
                print(f"  {row['state']:<12} {row['name']:<12} {row['operations']:>2} ops  {row['detail']}")
        if catalogue["unavailable"]:
            print("\nnot available in this build")
            for name, gate in catalogue["unavailable"].items():
                print(f"  {name}: {gate}")
        print(f"\nvault: {catalogue['vault']['path']} — {catalogue['vault']['note']}")
        return 0
    finally:
        stack.shutdown()


def cmd_workflows(args: argparse.Namespace) -> int:
    """The three business processes, runnable against the customer's own folders."""
    config = load_config(args.config)
    stack = build_stack(config, start_agents=False, load_plugins=False)
    try:
        if args.action == "list" or not args.name:
            from .workflows import catalogue

            rows = catalogue()
            if args.json:
                print(json.dumps(rows, indent=2))
            else:
                for row in rows:
                    print(f"{row['name']:<16} {row['title']}")
                    print(f"  {row['description']}")
                    print(f"  needs: {row['needs']}")
            return 0
        params = json.loads(args.params) if args.params else {}
        workflow = stack.workflows.get(args.name)
        if workflow is None:
            print(f"error: unknown workflow {args.name!r}; known: {', '.join(sorted(stack.workflows))}", file=sys.stderr)
            return 2
        if args.name == "reply_desk":
            channel_name = str(params.pop("channel", "telegram"))
            if channel_name.strip().lower() in {"mail", "mailbox", "email"}:
                from .workflows.mailbox import source_for

                try:
                    channel = source_for(
                        stack.connectors,
                        folder=str(params.pop("folder", "INBOX")),
                        unseen_only=bool(params.pop("unseen_only", True)),
                    )
                except RuntimeError as exc:
                    print(f"error: {exc}", file=sys.stderr)
                    return 2
            else:
                channel = stack.channels.get(channel_name)
            result = workflow.run(channel, **params)
        else:
            result = workflow.run(**params)
        payload = result.to_json()
        print(json.dumps(payload, indent=2) if args.json else _format_workflow(payload))
        return 0 if payload["status"] in {"done", "partial"} else 1
    finally:
        stack.shutdown()


def _format_workflow(payload: dict[str, Any]) -> str:
    lines = [f"{payload['workflow']}: {payload['status']} — {payload['summary']}"]
    for step in payload.get("steps", []):
        lines.append(f"  [{step['status']:<7}] {step['name']:<28} {step['detail']}")
    for problem in payload.get("problems", []):
        lines.append(f"  problem: {problem}")
    return "\n".join(lines)


def cmd_agents(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    stack = build_stack(config)
    roster = stack.orchestrator.roster()
    if args.json:
        print(json.dumps(roster, indent=2))
    else:
        print(f"{len(roster)} agents")
        for row in roster:
            print(
                f"  rank-{row['rank']} {row['name']:<18} {row['role']:<12} {row['model']:<24} "
                f"{row['status']:<8} mem={row['memory']['episodes']} caps={len(row['capabilities'])}"
            )
    print("\nQuota:")
    for row in stack.quotas.snapshot():
        print(
            f"  {row['model']:<34} {row['status']:<10} rpm {row['rpm']['used']}/{row['rpm']['limit']} "
            f"rpd {row['rpd']['used']}/{row['rpd']['limit']} tpm {row['tpm']['used']}/{row['tpm']['limit']}"
        )
    stack.shutdown()
    return 0


def cmd_hud(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if args.port:
        config.hud.port = args.port
    if args.host:
        config.hud.host = args.host
    stack = build_stack(config, with_voice=not args.no_voice, with_hud=True)
    assert stack.hud is not None
    stack.hud.start()
    print(f"{__product__} HUD: {stack.hud.bridge.url}")
    print(f"agents: {[a.name for a in stack.orchestrator.agents.values()]}")
    print(f"actions: {len(stack.registry)}   plugins: {len(stack.plugins.installed())}")
    if args.no_webview:
        print("serving headless; open the URL in any browser. Ctrl+C to stop.")
        try:
            while True:
                time.sleep(1.0)
        except KeyboardInterrupt:
            pass
        stack.shutdown()
        return 0
    stack.hud.run(webview=True)
    stack.shutdown()
    return 0


def cmd_plugins(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    stack = build_stack(config, start_agents=False)
    args.name = args.name or getattr(args, "name_flag", "") or ""
    if args.action == "list":
        plugins = stack.plugins.installed()
        if args.json:
            print(json.dumps(plugins, indent=2))
        else:
            print(f"{len(plugins)} plugins installed in {stack.plugins.plugins_dir}")
            for plugin in plugins:
                print(
                    f"  {plugin['name']:<26} v{plugin.get('version', '?'):<8} {plugin.get('status', ''):<10} "
                    f"caps={plugin.get('capabilities', [])} actions={plugin.get('actions', [])}"
                )
    elif args.action == "forge":
        result = stack.forge.forge(args.need, name=args.name or "", capabilities=args.capabilities, install=not args.dry_run)
        print(json.dumps(result, indent=2))
    elif args.action == "uninstall":
        print(json.dumps(stack.plugins.uninstall(args.name), indent=2))
    elif args.action == "opportunities":
        print(json.dumps(stack.forge.opportunities(), indent=2))
    elif args.action == "validate":
        from .plugins.loader import MANIFEST_NAME

        path = Path(args.name)
        print(json.dumps(stack.plugins.validate(path).to_json(), indent=2) if path.exists() else f"no such path: {path}")
        del MANIFEST_NAME
    stack.shutdown()
    return 0


def cmd_voice(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    stack = build_stack(config, with_voice=True)
    session = stack.voice
    started = session.start()
    print(json.dumps({"started": started, "availability": getattr(session, "available", {})}, indent=2))
    if not started.get("started"):
        print("voice cannot start on this machine; the HUD and CLI text mode still work.")
        stack.shutdown()
        return 1
    print("listening… wake word:", config.voice.wake_words, "— Ctrl+C to stop")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    stack.shutdown()
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    stack = build_stack(config, start_agents=False, load_plugins=False)
    rows = stack.memory.audit_log(limit=args.limit)
    for row in rows:
        stamp = time.strftime("%H:%M:%S", time.localtime(row["ts"]))
        print(f"{stamp} {row['agent_id']:<14} {row['action']:<24} {row['outcome']:<8} {row['capability']:<22} {row['args']}")
    print(f"\n{len(rows)} entries (newest first)")
    stack.shutdown()
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Run the HUD bridge without a webview — useful on a headless box or behind a tunnel."""
    config = load_config(args.config)
    if args.port:
        config.hud.port = args.port
    if args.host:
        config.hud.host = args.host
    stack = build_stack(config, with_voice=False, with_hud=True)
    assert stack.hud is not None
    stack.hud.start()
    print(f"HUD bridge on {stack.hud.bridge.url} (websocket: {stack.hud.bridge.url.replace('http', 'ws')}ws)")
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    stack.shutdown()
    return 0


# --------------------------------------------------------------------------------------
# Operational subcommands (contract sections 22.1 - 22.11)
# --------------------------------------------------------------------------------------


def cmd_catalog(args: argparse.Namespace) -> int:
    """Signed model catalogue: verify, apply between actions, roll back, check for drift."""
    from .ops.catalog import ModelCatalog, UpdateGate, Verification, check_provider_models

    config = load_config(args.config)
    action = args.action
    if action == "check":
        report = check_provider_models(config, timeout=args.timeout)
        if args.json:
            print(json.dumps(report, indent=2, default=str))
            return 1 if report["missing"] else 0
        print(f"provider model drift check at {time.strftime('%Y-%m-%d %H:%M')}")
        for name, summary in report["providers"].items():
            print(f"  {name:<16} {summary}")
        for entry in report["missing"]:
            print(f"  retired: {entry['provider']}:{entry['model_id']} is no longer served")
        for entry in report["unreachable"]:
            print(f"  unreachable: {entry['provider']} ({entry['error'][:80]})")
        return 1 if report["missing"] else 0

    if action == "rollback":
        # Rolling back needs no catalogue: it restores the configuration captured before the
        # last apply, which is the whole point of having snapshot it.
        gate = UpdateGate(config.home_path, EventBus())
        delta = gate.rollback(config)
        if delta is None:
            print("nothing to roll back (no apply has been recorded in this home)")
            return 1
        print(f"rolled back: restored {len(delta.added)} model(s), removed {len(delta.retired)}")
        return 0

    if not args.file:
        print("error: --file is required for verify/delta/apply", file=sys.stderr)
        return 2
    catalog = ModelCatalog.load(args.file)
    verification = catalog.verify(public_key_pem=args.public_key, hmac_key=args.hmac_key, allow_unsigned=args.allow_unsigned)
    if action == "verify":
        print(json.dumps({"version": catalog.version, "entries": len(catalog.entries), **verification.to_json()}, indent=2))
        return 0 if verification.ok else 1
    if not verification.ok:
        print(f"refusing to apply: {verification.reason}", file=sys.stderr)
        return 1

    gate = UpdateGate(config.home_path, EventBus())
    gate.stage(catalog, verification)
    stack = build_stack(config, start_agents=False)
    try:
        in_flight = stack.orchestrator.in_flight()
        if action == "delta":
            print(json.dumps(catalog.delta_against(config).to_json(), indent=2))
            return 0
        delta = gate.apply(config, in_flight=in_flight, verification=verification)
        if args.json:
            print(json.dumps({"applied": catalog.version, "method": verification.method, "delta": delta.to_json()}, indent=2))
        else:
            print(f"applied catalog {catalog.version} via {verification.method}: {delta.one_line()}")
        return 0
    except RuntimeError as exc:
        print(f"not applied: {exc}", file=sys.stderr)
        return 1
    finally:
        stack.shutdown()


def cmd_owner(args: argparse.Namespace) -> int:
    """First-run owner setup, status, PIN change and recovery."""
    from .ops.owner import OwnerRegistry

    config = load_config(args.config)
    owner = OwnerRegistry(config.home_path)
    action = args.action
    if action == "status":
        profile = owner.profile
        payload = {
            "configured": profile.configured,
            "owner": profile.owner_name,
            "machine_kind": profile.machine_kind,
            "os_user": profile.os_user,
            "foreign_profile": owner.foreign_profile(),
            "voice_prints": len(profile.voice_prints),
            "recovery_available": bool(profile.recovery_hash),
            "session_valid": owner.session_valid(),
            "locked_out": owner.locked_out(),
        }
        print(json.dumps(payload, indent=2))
        if owner.foreign_profile():
            print(
                f"note: this profile belongs to OS user {profile.os_user!r}; on a shared PC each user"
                " has their own owner, memory and permissions."
            )
        return 0
    if action == "setup":
        import getpass

        name = args.owner_name or input("Owner name: ").strip() or "Owner"
        pin = getpass.getpass("Choose a PIN (6+ characters): ")
        again = getpass.getpass("Repeat PIN: ")
        if pin != again:
            print("error: PINs do not match", file=sys.stderr)
            return 2
        recovery = owner.setup(name, pin, machine_kind=args.machine_kind)
        print("Owner configured. Write this recovery code down now - it is shown once and")
        print("it is the only way back in if the PIN is lost:")
        print(f"\n    {recovery}\n")
        return 0
    if action == "recover":
        import getpass

        code = input("Recovery code: ").strip()
        pin = getpass.getpass("New PIN (6+ characters): ")
        if not owner.verify_recovery(code, pin):
            print("error: recovery code rejected", file=sys.stderr)
            return 1
        # verify_recovery already consumed the old code and issued the replacement;
        # rotating again here would only throw the printed code away.
        print(f"PIN replaced. New recovery code (shown once): {owner.last_recovery_code}")
        return 0
    if action == "rotate":
        print(owner.rotate_recovery_code())
        return 0
    print(f"error: unknown owner action {action!r}", file=sys.stderr)
    return 2


def cmd_billing(args: argparse.Namespace) -> int:
    """Which launch mode is configured, and would it arm?"""
    from .ops.billing import BYOK, BillingGate, GatewayPolicy, launch_mode_summary

    config = load_config(args.config)
    gateway = GatewayPolicy(
        endpoint=args.gateway_endpoint,
        legal_review_reference=args.legal_reference,
    )
    gate = BillingGate(args.mode or BYOK, gateway)
    issues = gate.audit(config)
    if args.json:
        print(json.dumps({"mode": gate.mode, "armable": gate.armable(config), "issues": [i.to_json() for i in issues]}, indent=2))
        return 0 if gate.armable(config) else 1
    print(launch_mode_summary(config, gate.mode, gateway))
    print("\nproviders configured:")
    for provider in config.providers:
        state = "enabled" if provider.enabled else "disabled"
        keyed = "key source set" if (provider.api_key_env or provider.api_key_ref) else "no key source"
        print(f"  {provider.name:<12} {provider.kind:<18} {state:<9} {keyed}")
    if gate.mode != BYOK:
        print(f"\nthis build defaults to BYOK; {gate.mode} is opt-in and needs the items above resolved.")
    return 0 if gate.armable(config) else 1


def cmd_supervise(args: argparse.Namespace) -> int:
    """Inspect or clean up the processes a session started (contract 22.1)."""
    from .ops.supervisor import Supervisor

    config = load_config(args.config)
    supervisor = Supervisor(config.home_path, EventBus(), adopt_orphans=args.adopt)
    if args.action == "clean":
        report = supervisor.reconcile()
        print(json.dumps(report, indent=2, default=str))
        return 0
    if args.action == "status":
        print(json.dumps(supervisor.inventory(), indent=2, default=str))
        return 0
    print(f"error: unknown supervise action {args.action!r}", file=sys.stderr)
    return 2


def cmd_undo(args: argparse.Namespace) -> int:
    """Journal operations: plan an undo, do it, inspect history, reconcile a crashed session."""
    from .ops.journal import Journal

    config = load_config(args.config)
    journal = Journal(config.home_path / "journal.db")
    try:
        if args.reconcile:
            report = journal.reconcile()
        elif args.history:
            report = {
                "task_id": args.task,
                "summary": f"{len(journal.history(args.task, limit=args.limit))} journaled action(s)",
                "entries": [entry.to_json() for entry in journal.history(args.task, limit=args.limit)],
            }
        elif args.yes:
            report = journal.undo_task(args.task) if args.task else journal.undo_last_task()
        else:
            report = journal.plan_undo(args.task, limit=args.limit)
            report["note"] = "this is the plan; pass --yes to reverse the reversible actions"
    finally:
        journal.close()
    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return 0
    print(report.pop("summary", ""))
    for key in ("reversible", "undone", "failed", "impossible", "refused", "needs_decision", "abandoned"):
        for row in report.get(key) or []:
            detail = row.get("why") or (row.get("undo") or {}).get("detail") or ""
            print(f"  {key:<16} {str(row.get('action', row.get('entry_id', ''))):<22} {row.get('target', '')} {detail}")
    if report.get("note"):
        print(report["note"])
    return 0


def cmd_capabilities(args: argparse.Namespace) -> int:
    """What this machine can really do, what is unproven, and what is unavailable (spec 4.7)."""
    from .actions import build_registry
    from .ops.capability import AuditRunStats, CapabilityMatrix, ChainedRunStats, JournalRunStats
    from .ops.journal import Journal

    config = load_config(args.config)
    registry = build_registry(config=config)
    sources: list[object] = []
    journal = None
    journal_path = config.home_path / "journal.db"
    if journal_path.exists():
        journal = Journal(journal_path)
        sources.append(JournalRunStats(journal))
    memory = None
    try:
        from .memory import MemoryStore

        memory = MemoryStore(config.db_path)
        sources.append(AuditRunStats(memory))
    except Exception:
        memory = None
    try:
        matrix = CapabilityMatrix(registry, stats=ChainedRunStats(tuple(sources)))
    finally:
        if journal is not None:
            journal.close()
        if memory is not None:
            memory.close()
    if args.tool:
        record = matrix.record(args.tool)
        if args.json:
            print(json.dumps(record.to_json(), indent=2))
        else:
            print(f"{record.tool}: {record.status} ({record.local})")
            print(f"  capability: {record.capability}  danger: {record.danger}")
            print(f"  platforms:  {', '.join(record.platforms) or 'none declared'}")
            print(f"  runs:       {record.runs} ({record.ok} ok)")
            print(f"  verified:   {record.last_verified or 'never recorded on a real app'}")
            print(f"  recording:  {record.recording or '-'}")
            print(f"  limits:     {record.limits or '-'}")
            if record.evidence_problem:
                print(f"  evidence:   {record.evidence_problem}")
            if not record.available_here:
                print(f"  not here:   {record.unavailability}")
        return 0

    rows = matrix.records() if args.all else [r for r in matrix.records() if r.available_here]
    data = matrix.to_json()
    if args.json:
        print(json.dumps({"summary": data["summary"], "tools": [r.to_json() for r in rows]}, indent=2))
        return 0
    for row in rows:
        print(
            f"{row.status:<11} {row.local:<15} {row.tool:<26} {row.capability:<18} "
            f"runs={row.runs:<4} verified={row.last_verified or '-'}"
        )
    summary = data["summary"]
    print()
    print(
        f"{summary['ready_here']} of {summary['tools']} tool(s) usable on {summary['platform']}; "
        f"{summary['green']} green overall, {summary['provisional']} unproven, "
        f"{summary['unavailable']} with no driver profile"
    )
    if not args.all:
        print("tools this machine cannot run are hidden from agents; --all lists them")
    for problem in summary["invalid_evidence"]:
        print(f"[!!] unusable evidence: {problem['tool']}: {problem['problem']}")
    for tool in summary["phantom_evidence"]:
        print(f"[!!] recording for a tool that does not exist: {tool}")
    return 0


def cmd_gate(args: argparse.Namespace) -> int:
    """Run the master release gate (contract 22.3)."""
    tools_dir = resource_path("tools")
    if str(tools_dir) not in sys.path:
        sys.path.insert(0, str(tools_dir))
    import release_gate  # type: ignore[import-not-found]

    if args.list:
        return release_gate.main(["--list"])
    only = (args.only or "").split(",") if args.only else []
    report = release_gate.run_gate(milestone=args.milestone, only=[o.strip() for o in only if o.strip()])
    print(json.dumps(report, indent=2) if args.json else release_gate.format_report(report))
    return 0 if report["verdict"] == "READY" else 1


# --------------------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="orbynth", description=f"{__product__} — {__tagline__}")
    parser.add_argument("--config", help="path to config.json (defaults to the ORBYNTH home)")
    parser.add_argument("--home", help="ORBYNTH home directory")
    parser.add_argument("--version", action="version", version=f"{__product__} {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the config and database").set_defaults(func=cmd_init)
    sub.add_parser("doctor", help="report platform, drivers, models, plugins and memory").set_defaults(func=cmd_doctor)

    run = sub.add_parser("run", help="give the Sovereign one objective")
    run.add_argument("objective")
    run.add_argument("--context", default="")
    run.add_argument(
        "--expect",
        action="append",
        default=[],
        metavar="PATH",
        help="a file this request promises to produce, checked against the finished state",
    )
    run.add_argument("--voice", action="store_true", help="enable the voice session too")
    run.add_argument("--needs-gui", action="store_true", help="the task must use the desktop")
    run.add_argument("--needs-user", action="store_true", help="a human must be present to approve")
    run.add_argument("--needs-power", action="store_true", help="do not run on low battery")
    run.set_defaults(func=cmd_run)

    team = sub.add_parser("team", help="run an objective as a team of agents")
    team.add_argument("objective")
    team.add_argument("--items", help="JSON array of work items")
    team.add_argument("--sequential", action="store_true")
    team.set_defaults(func=cmd_team)

    channels = sub.add_parser("channels", help="connected platforms, their real state, and the rest with reasons")
    channels.add_argument("action", nargs="?", default="status", choices=["status", "send", "inbox"])
    channels.add_argument("--channel", default="telegram")
    channels.add_argument("--chat", default="")
    channels.add_argument("--text", default="")
    channels.add_argument("--limit", type=int, default=20)
    channels.add_argument("--json", action="store_true")
    channels.set_defaults(func=cmd_channels)

    connectors = sub.add_parser(
        "connectors", help="connected accounts (mail, Drive, calendar, chat, CRM, code, payments)"
    )
    connectors.add_argument(
        "action", nargs="?", default="list", choices=["list", "connect", "verify", "disconnect", "call"]
    )
    connectors.add_argument("--connector", default="")
    connectors.add_argument("--values", default="", help="JSON object of that connector's fields")
    connectors.add_argument("--meta", default="", help="JSON object of account labels")
    connectors.add_argument("--operation", default="")
    connectors.add_argument("--args", default="", help="JSON object of the operation's arguments")
    connectors.add_argument("--json", action="store_true")
    connectors.set_defaults(func=cmd_connectors)

    workflows = sub.add_parser("workflows", help="the business workflows this build ships")
    workflows.add_argument("action", nargs="?", default="list", choices=["list", "run"])
    workflows.add_argument("--name", default="")
    workflows.add_argument("--params", default="", help="JSON object of the workflow's own parameters")
    workflows.add_argument("--json", action="store_true")
    workflows.set_defaults(func=cmd_workflows)

    agents = sub.add_parser("agents", help="roster and quota report")
    agents.add_argument("--json", action="store_true")
    agents.set_defaults(func=cmd_agents)

    hud = sub.add_parser("hud", help="start the interface")
    hud.add_argument("--port", type=int)
    hud.add_argument("--host")
    hud.add_argument("--no-webview", action="store_true", help="serve only; open the URL yourself")
    hud.add_argument("--no-voice", action="store_true")
    hud.set_defaults(func=cmd_hud)

    serve = sub.add_parser("serve", help="run the bridge headless (no webview, no voice)")
    serve.add_argument("--port", type=int)
    serve.add_argument("--host")
    serve.set_defaults(func=cmd_serve)

    plugins = sub.add_parser("plugins", help="plugin management and self-extension")
    plugins.add_argument("action", choices=["list", "forge", "uninstall", "opportunities", "validate"])
    plugins.add_argument("name", nargs="?", default="", help="plugin name, or a path for validate")
    plugins.add_argument("--name", dest="name_flag", default="", help="plugin name (flag form)")
    plugins.add_argument("--need", default="", help="what the plugin should do (for forge)")
    plugins.add_argument("--capabilities", nargs="*", default=None)
    plugins.add_argument("--dry-run", action="store_true", help="generate and validate but do not install")
    plugins.add_argument("--json", action="store_true")
    plugins.set_defaults(func=cmd_plugins)

    sub.add_parser("voice", help="start the voice session").set_defaults(func=cmd_voice)

    audit = sub.add_parser("audit", help="recent action audit trail")
    audit.add_argument("--limit", type=int, default=50)
    audit.set_defaults(func=cmd_audit)

    catalog = sub.add_parser("catalog", help="signed model catalogue: verify, apply, roll back, drift check")
    catalog.add_argument("action", choices=["verify", "delta", "apply", "rollback", "check"])
    catalog.add_argument("--file", default="", help="catalogue JSON")
    catalog.add_argument("--public-key", default="", help="pinned Ed25519 public key (PEM)")
    catalog.add_argument("--hmac-key", default="", help="shared HMAC key (development only)")
    catalog.add_argument("--allow-unsigned", action="store_true", help="development: accept an unsigned catalogue")
    catalog.add_argument("--timeout", type=float, default=20.0)
    catalog.add_argument("--json", action="store_true")
    catalog.set_defaults(func=cmd_catalog)

    owner = sub.add_parser("owner", help="owner identity: setup, status, recovery (contract 22.7)")
    owner.add_argument("action", choices=["status", "setup", "recover", "rotate"])
    owner.add_argument("--name", dest="owner_name", default="")
    owner.add_argument("--machine-kind", default="personal", choices=["personal", "shared", "dedicated"])
    owner.set_defaults(func=cmd_owner)

    billing = sub.add_parser("billing", help="launch mode and whether it would arm (contract 22.6)")
    billing.add_argument("--mode", choices=["byok", "gateway", "local_only"], default="byok")
    billing.add_argument("--gateway-endpoint", default="")
    billing.add_argument("--legal-reference", default="")
    billing.add_argument("--json", action="store_true")
    billing.set_defaults(func=cmd_billing)

    supervise = sub.add_parser("supervise", help="inspect or clean supervised processes (contract 22.1)")
    supervise.add_argument("action", choices=["status", "clean"])
    supervise.add_argument("--adopt", action="store_true", help="adopt still-running jobs instead of terminating them")
    supervise.set_defaults(func=cmd_supervise)

    undo = sub.add_parser("undo", help="action journal: plan an undo, reverse the last task, inspect history (spec 4.5)")
    undo.add_argument("--task", default="", help="task id (default: the last task that changed something)")
    undo.add_argument("--yes", action="store_true", help="actually reverse; without it you get the plan")
    undo.add_argument("--reconcile", action="store_true", help="report what an interrupted session left behind")
    undo.add_argument("--history", action="store_true", help="list journaled actions")
    undo.add_argument("--limit", type=int, default=50)
    undo.add_argument("--json", action="store_true")
    undo.set_defaults(func=cmd_undo)

    caps = sub.add_parser("capabilities", help="what works here, what is unproven, what is unavailable (spec 4.7)")
    caps.add_argument("--tool", default="", help="show one tool's row")
    caps.add_argument("--all", action="store_true", help="include tools this machine cannot run")
    caps.add_argument("--json", action="store_true")
    caps.set_defaults(func=cmd_capabilities)

    gate = sub.add_parser("gate", help="master release gate: every acceptance check (contract 22.3)")
    gate.add_argument("--milestone", default="GA", help="M0..M4 or GA")
    gate.add_argument("--only", default="", help="comma-separated check ids")
    gate.add_argument("--list", action="store_true")
    gate.add_argument("--json", action="store_true")
    gate.set_defaults(func=cmd_gate)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "home", None):
        os.environ["ORBYNTH_HOME"] = str(Path(args.home).expanduser())
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130
    except Exception as exc:  # noqa: BLE001 - a CLI must print a usable error
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
