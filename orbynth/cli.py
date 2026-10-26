"""The ``orbynth`` command line: the entry point for everything the system does.

Subcommands are thin: each one assembles the same stack (config → bus → memory → providers →
quotas → runtime → actions → plugins → orchestrator → HUD/voice) and then does one thing.
``build_stack`` is that assembly, shared by the CLI, the tests and any embedding program.
"""

from __future__ import annotations

import argparse
import json
import os
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
from .plugins.forge import Forge
from .plugins.loader import PluginHost
from .voice.session import TextOnlyVoice, VoiceSession

HUD_DIR = Path(__file__).resolve().parent.parent / "hud"


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

    def shutdown(self) -> None:
        if self.hud is not None:
            try:
                self.hud.bridge.stop()
            except Exception:
                pass
        if self.voice is not None:
            try:
                self.voice.stop()
            except Exception:
                pass
        self.orchestrator.shutdown()
        self.memory.close()


def build_stack(
    config: AppConfig,
    start_agents: bool = True,
    load_plugins: bool = True,
    with_voice: bool = False,
    with_hud: bool = False,
) -> Stack:
    """Assemble the whole system from configuration."""
    bus = EventBus()
    memory = MemoryStore(config.db_path)
    secrets = Secrets(config.home_path)
    registry_actions = build_registry(config, bus, memory)
    providers = ProviderRegistry(config.providers, secrets, bus)
    quotas = QuotaManager(config, bus, persist_path=config.home_path / "quota.json")
    runtime = ModelRuntime(config, providers, quotas, memory, bus)

    plugins = PluginHost(config.home_path, registry_actions, bus, memory=memory)
    forge = Forge(plugins, runtime, registry_actions, bus, memory, workdir=config.home_path / "forge")
    if load_plugins:
        plugins.load_all()

    orchestrator = Orchestrator(config, runtime, registry_actions, memory, bus)
    orchestration.set_services(orchestration.Services(orchestrator, runtime, memory, bus, config))
    if start_agents:
        orchestrator.bootstrap_roster()

    voice: Any = None
    if with_voice:
        voice = VoiceSession(config.voice, bus) if config.voice.enabled else TextOnlyVoice(bus)

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
    )
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
        )
        hud.forge = forge  # type: ignore[attr-defined]
        if voice is not None:
            voice.on_utterance = lambda text: hud._run_sovereign(text)  # noqa: SLF001 - single wiring point
        stack.hud = hud
    return stack


def sovereign(stack: Stack, objective: str, context: str = "") -> dict[str, Any]:
    """Send a request to the Sovereign agent (creating one if the roster is empty)."""
    from .agents.orchestrator import Agent

    agent = next((a for a in stack.orchestrator.agents.values() if a.identity.role == "sovereign"), None)
    if agent is None:
        agent = stack.orchestrator.spawn(role="sovereign", name="Sovereign", capabilities=["*"])
    report = agent.run_task("user request", objective, context=context)
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
    started = time.time()
    result = sovereign(stack, args.objective, context=args.context or "")
    print(f"\nstatus: {result['status']}  ({time.time() - started:.1f}s)")
    print(f"summary: {result['summary']}")
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
    items = json.loads(args.items) if args.items else [{"title": args.objective[:60], "objective": args.objective}]
    result = stack.orchestrator.run_team(
        args.objective,
        [WorkItem(title=i.get("title", ""), objective=i.get("objective", ""), role=i.get("role", "operative"), capabilities=i.get("capabilities", [])) for i in items],
        parallel=not args.sequential,
    )
    print(json.dumps({"team": result["team"], "reports": result["reports"], "summary": result["summary"]}, indent=2))
    stack.shutdown()
    return 0


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
        fresh = owner.rotate_recovery_code()
        print(f"PIN replaced. New recovery code (shown once): {fresh}")
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


def cmd_gate(args: argparse.Namespace) -> int:
    """Run the master release gate (contract 22.3)."""
    tools_dir = Path(__file__).resolve().parent.parent / "tools"
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
    run.add_argument("--voice", action="store_true", help="enable the voice session too")
    run.set_defaults(func=cmd_run)

    team = sub.add_parser("team", help="run an objective as a team of agents")
    team.add_argument("objective")
    team.add_argument("--items", help="JSON array of work items")
    team.add_argument("--sequential", action="store_true")
    team.set_defaults(func=cmd_team)

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
