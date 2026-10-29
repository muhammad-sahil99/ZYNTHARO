"""HUD service: the process that owns the interface.

It builds the snapshot the HUD renders on connect, routes the user's intent back into the
fleet (chat, task submission, confirmations, agent and plugin control, voice), and can run
the whole thing inside an OS webview or as a plain local server.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .. import __alternate_brand__, __product__, __tagline__, __version__
from ..actions.registry import ActionRegistry
from ..actions import driver_report
from ..config import AppConfig
from ..events import HUD_NOTICE, LOG, VOICE_SPEAKING, VOICE_TRANSCRIPT, EventBus
from ..memory import MemoryStore
from .bridge import HudBridge, Client, open_webview


class HudService:
    """Owns the bridge and translates between the fleet and the interface."""

    def __init__(
        self,
        config: AppConfig,
        bus: EventBus,
        memory: MemoryStore,
        registry: ActionRegistry,
        runtime: Any = None,
        quotas: Any = None,
        orchestrator: Any = None,
        plugins: Any = None,
        voice: Any = None,
        connectors: Any = None,
        static_dir: str | Path | None = None,
        supervisor: Any = None,
        owner: Any = None,
        billing: Any = None,
        governor: Any = None,
        verifier: Any = None,
        sensors: Any = None,
        index: Any = None,
        comms: Any = None,
        reserves: Any = None,
        speakers: Any = None,
        camera: Any = None,
        presence: Any = None,
        lessons: Any = None,
        scheduler: Any = None,
    ):
        self.config = config
        self.bus = bus
        self.memory = memory
        self.registry = registry
        self.runtime = runtime
        self.quotas = quotas
        self.orchestrator = orchestrator
        self.plugins = plugins
        self.voice = voice
        #: The customer's own outside accounts (mail, drives, calendar, chat, CRM, code, payments).
        #: The settings screen is the only place a credential is typed; it goes straight into the
        #: local vault and is proven against the real service before the row turns green.
        self.connectors = connectors
        # The operational layer, so the interface can show it instead of hiding it (22.1-22.11).
        self.supervisor = supervisor
        self.owner = owner
        self.billing = billing
        self.governor = governor
        self.verifier = verifier
        #: Hardware monitor and local index: shown as measurements, never as decoration.
        self.sensors = sensors
        self.index = index
        #: Inter-agent comms and the model bench (reserve models with declared limits).
        self.comms = comms
        self.reserves = reserves
        #: Who is talking and who is in the room: presence is shown as evidence, including the
        #: camera's consent state, because a blocked camera must look blocked, not empty.
        self.speakers = speakers
        self.camera = camera
        self.presence = presence
        #: What the system has learned from its own failures, and the work queued for later.
        self.lessons = lessons
        self.scheduler = scheduler
        #: Recent outcome checks and preflight decisions, newest first.
        self.verdicts: list[dict[str, Any]] = []
        self.preflight_log: list[dict[str, Any]] = []
        self.bus.subscribe("task.verified", self._on_verdict)
        self.bus.subscribe("task.disputed", self._on_verdict)
        self.bus.subscribe("task.preflight", self._on_preflight)
        if static_dir:
            self.static_dir = Path(static_dir)
        else:
            from ..resources import resource_path

            self.static_dir = resource_path("hud")
        self.bridge = HudBridge(
            bus=bus,
            host=config.hud.host,
            port=config.hud.port,
            static_dir=self.static_dir,
            snapshot_provider=self.snapshot,
            command_router=self.route,
        )
        self.transcript: list[dict[str, Any]] = []
        self.bus.subscribe(VOICE_TRANSCRIPT, self._on_voice_transcript)

    # ----------------------------------------------------------------------------------
    # Snapshot: everything the HUD needs to be truthful on first paint
    # ----------------------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        agents = self.orchestrator.roster() if self.orchestrator else []
        teams = [t.to_json() for t in self.orchestrator.teams.values()] if self.orchestrator else []
        plugins = self.plugins.installed() if self.plugins else []
        quotas = self.quotas.snapshot() if self.quotas else []
        models = self.runtime.registry.inventory() if self.runtime else []
        return {
            "product": __product__,
            "alternate_brand": __alternate_brand__,
            "tagline": __tagline__,
            "version": __version__,
            "server_time": time.time(),
            "config": {
                "home": str(self.config.home_path),
                "theme": self.config.hud.theme,
                "particle_budget": self.config.hud.particle_budget,
                "wires": self.config.hud.wires,
                "voice": {
                    "enabled": self.config.voice.enabled,
                    "wake_words": self.config.voice.wake_words,
                    "stt": self.config.voice.stt,
                    "tts": self.config.voice.tts,
                    "tts_voice": self.config.voice.tts_voice,
                    "barge_in": self.config.voice.barge_in,
                },
                "safety": {
                    "confirm_capabilities": self.config.safety.confirm_capabilities,
                    "max_parallel_agents": self.config.safety.max_parallel_agents,
                    "allow_network": self.config.safety.allow_network,
                },
            },
            "models": models,
            "quotas": quotas,
            "agents": agents,
            "teams": teams,
            "tasks": self.memory.tasks()[-40:],
            "plugins": plugins,
            "actions": {
                "count": len(self.registry),
                "groups": self.registry.groups(),
            },
            "drivers": driver_report(),
            "memory": self.memory.stats(),
            "pending_confirmations": list(self.registry.pending.values()),
            "transcript": self.transcript[-60:],
            "jobs": (self.supervisor.inventory()["jobs"] if self.supervisor else []),
            "verdicts": self.verdicts[:40],
            "preflight": self.preflight_log[:20],
            "security": self._security_snapshot(),
            "updates": self._updates_snapshot(),
            "machine": self._machine_snapshot(),
            "hardware": self._hardware_snapshot(),
            "capabilities": self._capability_snapshot(),
            "search": self._search_snapshot(),
            "comms": self._comms_snapshot(),
            "reserves": self._reserves_snapshot(),
            "connectors": self._connectors_snapshot(),
            "presence": self._presence_snapshot(),
            "lessons": self.lessons.stats() | {"top": self.lessons.top(6)} if self.lessons is not None else {"lessons": 0, "note": "lesson book not attached"},
            "schedule": self.scheduler.snapshot() if self.scheduler is not None else {"count": 0, "note": "scheduler not attached"},
        }

    def _presence_snapshot(self) -> dict[str, Any]:
        """Who is present, what each sense actually knows, and the last etiquette verdict."""
        camera = self.camera
        payload: dict[str, Any] = {
            "camera": camera.snapshot() if camera is not None else {"note": "camera not attached"},
            "people": self.speakers.rows() if self.speakers is not None else [],
            "owner_voice": self.speakers.owner() if self.speakers is not None else "",
        }
        if self.presence is not None:
            try:
                payload["snapshot"] = self.presence.snapshot_json()
            except Exception as exc:  # the panel shows the failure instead of an empty room
                payload["error"] = repr(exc)
        else:
            payload["note"] = "presence controller not attached"
        return payload

    def _comms_snapshot(self) -> dict[str, Any]:
        if self.comms is None:
            return {"stats": {}, "recent": [], "open_requests": [], "note": "comms not attached"}
        try:
            return self.comms.snapshot()
        except Exception as exc:  # the panel must show the failure, not an empty calm list
            return {"stats": {}, "recent": [], "open_requests": [], "error": repr(exc)}

    def _reserves_snapshot(self) -> dict[str, Any]:
        if self.reserves is None:
            return {"models": [], "note": "reserve pool not attached"}
        try:
            rows = self.reserves.rows()
        except Exception as exc:
            return {"models": [], "error": repr(exc)}
        return {
            "models": rows,
            "reserve_count": sum(1 for r in rows if r.get("reserve")),
            "active_count": sum(1 for r in rows if not r.get("reserve")),
        }

    def _connectors_snapshot(self) -> dict[str, Any]:
        """The connected-accounts screen, painted from the vault and nothing else.

        Deliberately no network call happens while building this: statuses are local facts about
        stored credentials. A screen refresh must never be able to lock an account or make the
        interface slow, and the one call that *does* touch the service (``verify``) is a button
        the customer presses.

        The per-connector operation lists are left out on purpose — this payload is rebuilt on
        every state push, and the full list is fetched per account by ``connector.spec`` when a
        row is opened.
        """
        if self.connectors is None:
            return {
                "attached": False,
                "rows": [],
                "note": "no connector registry is attached to this build",
            }
        try:
            catalogue = self.connectors.catalogue()
        except Exception as exc:  # a broken registry must look broken, not empty
            return {
                "attached": False,
                "rows": [],
                "note": f"the connector registry failed: {type(exc).__name__}: {exc}",
            }
        rows: list[dict[str, Any]] = []
        for entries in (catalogue.get("categories") or {}).values():
            for entry in entries:
                rows.append(
                    {
                        key: entry.get(key)
                        for key in (
                            "name",
                            "title",
                            "category",
                            "state",
                            "detail",
                            "auth",
                            "account",
                            "verified_at",
                            "operations",
                            "needs",
                            "where",
                            "docs",
                        )
                    }
                )
        rows.sort(key=lambda row: (row.get("state") != "ready", row.get("category") or "", row.get("name") or ""))
        return {
            "attached": True,
            "rows": rows,
            "total": catalogue.get("total", len(rows)),
            "ready": catalogue.get("ready", 0),
            "needs_setup": catalogue.get("needs_setup", 0),
            "unavailable": catalogue.get("unavailable") or {},
            "mail_providers": catalogue.get("mail_providers") or [],
            "vault": catalogue.get("vault") or {},
            "connected": catalogue.get("connected") or [],
            "signin_profiles": catalogue.get("signin_profiles") or {},
            "signin": self._signin_sessions(),
        }

    def _signin_sessions(self) -> list[dict[str, Any]]:
        """Live consent sessions. Empty when nothing is being signed in, which is the normal case."""
        manager = getattr(self.connectors, "signin", None) if self.connectors is not None else None
        if manager is None:
            return []
        try:
            return manager.sessions()
        except Exception:  # noqa: BLE001 - a broken session list must not blank the screen
            return []

    def _security_snapshot(self) -> dict[str, Any]:
        owner = self.owner
        if owner is None:
            return {"configured": False, "owner": "", "note": "owner gate not attached"}
        profile = owner.profile
        return {
            "configured": bool(profile.configured),
            "owner": profile.owner_name if profile.configured else "",
            "machine_kind": profile.machine_kind,
            "os_user": profile.os_user,
            "foreign_profile": owner.foreign_profile(),
            "voice_prints": len(profile.voice_prints),
            "recovery_available": bool(profile.recovery_hash),
            "session_valid": owner.session_valid(),
            "session_remaining_s": round(owner.session_remaining(), 1),
            "locked_out": owner.locked_out(),
            "irreversible_needs": "pin",
        }

    def _updates_snapshot(self) -> dict[str, Any]:
        catalogue = self.config.catalog
        snapshot: dict[str, Any] = {
            "mode": self.billing.mode if self.billing else self.config.billing.mode,
            "catalog_path": catalogue.path,
            "catalog_version": "loaded" if catalogue.path else "none loaded",
        }
        if self.billing is not None:
            issues = self.billing.audit(self.config)
            snapshot["billing_issues"] = [i.code for i in issues]
            snapshot["armable"] = self.billing.armable(self.config)
            snapshot["spend_today_usd"] = round(self.billing.global_usage.spend_usd, 4)
            snapshot["requests_today"] = self.billing.global_usage.requests
        return snapshot

    def _machine_snapshot(self) -> dict[str, Any]:
        from ..ops.languages import LANGUAGES, LTR, ocr_languages
        from ..ops.preflight import MachineState

        state = MachineState.detect()
        snapshot = state.to_json()
        snapshot["priority"] = getattr(self.governor, "_priority", "normal") if self.governor else "normal"
        if self.governor is not None:
            snapshot["paused"] = self.governor.evaluate().paused
            # Give the interface the reason the machine is being left alone, so "deferred"
            # finally has a cause to show instead of an unexplained pause.
            snapshot["busy_reason"] = snapshot.get("busy_reason") or self.governor.busy_reason()
            # The admission cap is a number the user can act on: it is the real limit on how
            # much work starts at once, not an estimate of CPU percent.
            snapshot["worker_cap"] = self.governor.evaluate().worker_cap
            snapshot["worker_ceiling"] = self.governor.max_workers
        languages = [self.config.voice.language, *self.config.voice.secondary_languages]
        snapshot["languages"] = ", ".join(languages) + " (ocr: " + ocr_languages(languages) + ")"
        # Direction of the interface itself (contract 22.11). A right-to-left language is not
        # a translation: the chrome has to lay out mirrored, or the HUD is wrong for Urdu and
        # Arabic users while pretending to support them.
        snapshot["language_dir"] = LANGUAGES.get(languages[0], LANGUAGES["en"]).direction if languages else LTR
        return snapshot

    def _hardware_snapshot(self) -> dict[str, Any]:
        if self.sensors is None:
            return {"attached": False, "note": "the sensor monitor is not running on this machine"}
        try:
            summary = self.sensors.summary()
            summary["attached"] = True
            summary["report"] = self.sensors.sensor_report()
            return summary
        except Exception as exc:  # noqa: BLE001 - a broken panel must not break the HUD
            return {"attached": False, "note": f"sensor monitor error: {type(exc).__name__}: {exc}"}

    def _capability_snapshot(self) -> dict[str, Any]:
        """The Capability Matrix, so the interface can say what is proven and what is not."""
        matrix = getattr(self.registry, "capabilities", None)
        if matrix is None:
            return {"attached": False, "rows": []}
        try:
            data = matrix.to_json()
        except Exception as exc:  # noqa: BLE001
            return {"attached": False, "error": f"{type(exc).__name__}: {exc}", "rows": []}
        rows = data.get("tools", [])
        return {
            "attached": True,
            "summary": data.get("summary", {}),
            "rows": rows[:400],
            "warnings": data.get("warnings", []),
        }

    def _search_snapshot(self) -> dict[str, Any]:
        if self.index is None:
            return {"attached": False, "note": "the local index is not attached"}
        try:
            status = self.index.status()
            status["attached"] = True
            return status
        except Exception as exc:  # noqa: BLE001
            return {"attached": False, "note": f"index error: {type(exc).__name__}: {exc}"}

    def _on_verdict(self, event: Any) -> None:
        payload = dict(event.payload or {})
        payload["at"] = event.ts
        self.verdicts.insert(0, payload)
        self.verdicts = self.verdicts[:60]

    def _on_preflight(self, event: Any) -> None:
        payload = dict(event.payload or {})
        payload["at"] = event.ts
        self.preflight_log.insert(0, payload)
        self.preflight_log = self.preflight_log[:40]

    # ----------------------------------------------------------------------------------
    # Routing user intent from the HUD
    # ----------------------------------------------------------------------------------
    def route(self, kind: str, message: dict[str, Any], client: Client) -> Any:
        handler: Callable[[dict[str, Any]], Any] | None = {
            "chat": self._chat,
            "task.submit": self._task,
            "voice.mute": self._voice_mute,
            "voice.ptt": self._voice_ptt,
            "presence": self._presence_command,
            "schedule.run": self._schedule_run,
            "speak": self._speak,
            "confirm": self._confirm,
            "plugin.list": lambda _m: self.plugins.installed() if self.plugins else [],
            "plugin.forge": self._forge,
            "plugin.uninstall": self._uninstall,
            "agent.spawn": self._spawn_agent,
            "agent.terminate": self._terminate_agent,
            "model.assign": self._assign_model,
            "state.request": lambda _m: self.snapshot(),
            "owner.unlock": self._owner_unlock,
            "owner.status": lambda _m: self._security_snapshot(),
            "jobs.stop": self._jobs_stop,
            "memory.search": self._memory_search,
            "hw.query": self._hw_query,
            "hw.alerts": self._hw_alerts,
            "index.search": self._index_search,
            "index.update": self._index_update,
            "index.add": self._index_add,
            "index.sources": lambda _m: self.index.sources() if self.index else [],
            "capability.matrix": lambda _m: self._capability_snapshot(),
            "audit": self._audit,
            "connector.spec": self._connector_spec,
            "connector.connect": self._connector_connect,
            "connector.verify": self._connector_verify,
            "connector.disconnect": self._connector_disconnect,
            "connector.call": self._connector_call,
            "connector.signin": self._connector_signin,
        }.get(kind)
        if handler is None:
            raise ValueError(f"unknown HUD command: {kind}")
        return handler(message)

    def _chat(self, message: dict[str, Any]) -> dict[str, Any]:
        text = str(message.get("text", "")).strip()
        if not text:
            raise ValueError("chat message is empty")
        self.transcript.append({"role": "user", "text": text, "ts": time.time()})
        self.bus.emit(VOICE_TRANSCRIPT, source="user", text=text, role="user")
        self._run_sovereign(text)
        return {"accepted": True, "queued": True}

    def _run_sovereign(self, text: str) -> None:
        sovereign = None
        if self.orchestrator is not None:
            sovereign = next((a for a in self.orchestrator.agents.values() if a.identity.role == "sovereign"), None)
        if sovereign is None:
            self.bridge.notify("No Sovereign agent is running — start the fleet first.", severity="warning", speak=False)
            return

        def run() -> None:
            try:
                report = sovereign.run_task("user request", text, context="Direct request from the user through the HUD.")
                self.transcript.append({"role": "assistant", "text": report.summary, "ts": time.time(), "report": report.to_json()})
                self.bus.emit(VOICE_TRANSCRIPT, source=sovereign.id, text=report.summary, role="assistant")
                severity = "info" if report.status == "done" else "warning"
                decision = self._speech_decision(report.summary, severity)
                self.bridge.notify(report.summary, severity=severity, speak=bool(decision.get("audible", True)) if decision else True)
                if decision and not decision.get("audible", True):
                    self.bus.emit(
                        HUD_NOTICE,
                        source="presence",
                        text=f"reply kept on screen ({decision.get('mode', '')}): {decision.get('reason', '')}",
                        speak=False,
                    )
                if report.blockers:
                    self.bridge.notify("blocked: " + "; ".join(report.blockers[:2]), severity="warning", speak=False)
            except Exception as exc:  # noqa: BLE001
                self.bridge.notify(f"Sovereign failed: {exc}", severity="error", speak=False)
            finally:
                self.bridge.push_state()

        threading.Thread(target=run, name="sovereign-turn", daemon=True).start()

    def _task(self, message: dict[str, Any]) -> dict[str, Any]:
        if self.orchestrator is None:
            raise ValueError("no orchestrator attached")
        objective = str(message.get("objective", "")).strip()
        items = message.get("items") or []
        if not objective:
            raise ValueError("objective is required")
        # Preflight first: a task submitted to a locked desktop is deferred with a reason the
        # user can read, not queued silently or run against a screen nobody can see (22.2).
        if self.orchestrator is not None and getattr(self.orchestrator, "preflight", None) is not None:
            from ..ops.preflight import TaskRequirements

            requirements = TaskRequirements(
                gui=bool(message.get("needs_gui", False)),
                unlocked_session=bool(message.get("needs_gui", False)),
                user_present=bool(message.get("needs_user", False)),
                power=bool(message.get("needs_power", False)),
                network=self.config.safety.allow_network,
                background_ok=bool(message.get("background_ok", True)),
            )
            decision = self.orchestrator.preflight_task(requirements, origin=str(message.get("origin", "user_request")))
            if decision is not None and not decision.can_run:
                self.bridge.notify(
                    f"held back ({decision.action}): {'; '.join(decision.reasons)}", severity="warning", speak=False
                )
                return {
                    "accepted": False,
                    "action": decision.action,
                    "reasons": decision.reasons,
                    "advice": decision.advice,
                }
        if items:
            result = self.orchestrator.run_team(
                objective,
                [
                    __import__("orbynth.agents.orchestrator", fromlist=["WorkItem"]).WorkItem(
                        title=str(item.get("title", "")),
                        objective=str(item.get("objective", "")),
                        role=str(item.get("role", "operative")),
                        capabilities=list(item.get("capabilities", []) or []),
                    )
                    for item in items
                ],
                parallel=bool(message.get("parallel", True)),
            )
            return {"accepted": True, "team": result["team"], "summary": result["summary"]}

        def run() -> None:
            from ..agents.orchestrator import WorkItem

            report = self.orchestrator.dispatch(
                WorkItem(title=objective[:80], objective=objective, role=str(message.get("role", "operative"))),
                wait=True,
            )
            self.bridge.notify(getattr(report, "summary", "task finished"), severity="info", speak=True)
            self.bridge.push_state()

        threading.Thread(target=run, name="task-run", daemon=True).start()
        return {"accepted": True, "mode": "single-agent"}

    def _voice_mute(self, message: dict[str, Any]) -> dict[str, Any]:
        muted = bool(message.get("muted", True))
        if self.voice is not None:
            self.voice.set_muted(muted)
        return {"muted": muted}

    def _schedule_run(self, message: dict[str, Any]) -> dict[str, Any]:
        if self.scheduler is None:
            raise ValueError("no scheduler is attached to this build")
        summary = self.scheduler.tick()
        self.bridge.push_state()
        return summary

    def _presence_command(self, message: dict[str, Any]) -> dict[str, Any]:
        """The interface's presence controls: one look, or the owner's camera decision.

        Consent can be given here because the interface is the owner's own screen. Agents still
        cannot do it without ``camera.enroll``: a model must never be able to switch a camera on.
        """
        action = str(message.get("action", "look"))
        if self.camera is None or self.presence is None:
            raise ValueError("presence is not attached to this build")
        if action == "consent":
            decision = str(message.get("decision", "grant"))
            if decision == "grant":
                return self.camera.grant(decided_by="owner", note="granted from the interface")
            if decision == "revoke":
                return self.camera.revoke("revoked from the interface")
            return self.camera.deny("declined from the interface")
        view = self.presence.look()
        self.bridge.push_state()
        return {
            "camera": view.to_json(),
            "people": self.speakers.rows() if self.speakers is not None else [],
        }

    # ----------------------------------------------------------------------------------
    # Connected accounts. The interface may connect them because it is the owner's own screen;
    # an agent still cannot (`connector.admin`), because a credential typed into a chat window is
    # not a consent gesture. No value is ever echoed back, logged or put in an event payload.
    # ----------------------------------------------------------------------------------
    def _connector_registry(self) -> Any:
        if self.connectors is None:
            raise ValueError("no connector registry is attached to this build")
        return self.connectors

    def _connector_name(self, message: dict[str, Any]) -> str:
        name = str(message.get("connector", "")).strip().lower()
        if not name:
            raise ValueError("which account? pass connector=<name>")
        return name

    def _connector_spec(self, message: dict[str, Any]) -> dict[str, Any]:
        """One account's full surface, fetched only when a row is opened.

        This is where the customer reads the two lists that matter: what this account can do, and
        which of those operations sends something (and therefore needs their PIN).
        """
        from ..connectors.base import ConnectorError

        name = self._connector_name(message)
        provider = str(message.get("provider", "")).strip().lower()
        try:
            target = self._connector_registry().get(name, provider)
        except KeyError as exc:
            raise ValueError(str(exc)) from exc
        except ConnectorError as exc:  # Unsupported carries the business gate as its text
            return {
                "connector": name,
                "state": "unavailable",
                "detail": str(exc),
                "operations": [],
            }
        manager = getattr(self.connectors, "signin", None)
        profiles = manager.profiles(target.name) if manager is not None else []
        return {
            "connector": target.name,
            "title": target.title,
            "category": target.category,
            "auth": target.auth,
            "needs": target.needs,
            "where": target.where,
            "docs": target.docs,
            "status": target.status().to_json(),
            "fields": [f.to_json() for f in target.fields()],
            "signin_profiles": profiles,
            "operations": [op.to_json() for op in target.operations()],
        }

    def _connector_connect(self, message: dict[str, Any]) -> dict[str, Any]:
        """Store what the customer typed, then make the service itself prove it.

        The row only turns green if the provider agrees — a stored key is ``configured``, and the
        interface keeps saying so until ``verify`` succeeds. On failure the credential is kept, so
        fixing a typo does not mean retyping every field.
        """
        from ..connectors.base import ConnectorError

        name = self._connector_name(message)
        values = message.get("values") or {}
        if not isinstance(values, dict) or not any(str(v).strip() for v in values.values()):
            raise ValueError("at least one field value is required")
        meta = message.get("meta") if isinstance(message.get("meta"), dict) else {}
        try:
            status = self._connector_registry().connect(
                name, {str(k): str(v) for k, v in values.items()}, {str(k): str(v) for k, v in meta.items()}
            )
        except KeyError as exc:
            raise ValueError(str(exc)) from exc
        except ConnectorError as exc:
            self.bus.emit(
                HUD_NOTICE, source="connectors", severity="warning", message=f"{name}: {exc}", speak=False
            )
            raise ValueError(str(exc)) from exc
        data = status.to_json()
        self._announce_connector(status)
        return data

    def _connector_verify(self, message: dict[str, Any]) -> dict[str, Any]:
        from ..connectors.base import ConnectorError

        name = self._connector_name(message)
        try:
            status = self._connector_registry().verify(name)
        except KeyError as exc:
            raise ValueError(str(exc)) from exc
        except ConnectorError as exc:
            # Reported as a row state rather than a protocol error: the customer needs the
            # provider's own words next to the field they have to fix, not a red toast.
            self.bus.emit(
                HUD_NOTICE, source="connectors", severity="warning", message=f"{name} failed verification: {exc}", speak=False
            )
            return {"name": name, "state": "needs_setup", "detail": str(exc), "auth": "", "account": "", "operations": 0}
        data = status.to_json()
        self._announce_connector(status)
        return data

    def _connector_disconnect(self, message: dict[str, Any]) -> dict[str, Any]:
        name = self._connector_name(message)
        removed = bool(self._connector_registry().disconnect(name))
        self.bus.emit(
            HUD_NOTICE,
            source="connectors",
            severity="info" if removed else "notice",
            message=(
                f"disconnected {name}: the credential is gone from the vault"
                if removed
                else f"nothing to disconnect for {name}"
            ),
            speak=False,
        )
        self.bridge.push_state()
        return {"connector": name, "disconnected": removed}

    def _connector_signin(self, message: dict[str, Any]) -> dict[str, Any]:
        """Start, poll or cancel an interactive consent for a connector that needs one.

        Returns immediately in every case: the flow runs in its own thread and the panel polls
        ``connector.signin`` with ``action=status``. A browser consent that blocked the interface
        thread would freeze the console for up to four minutes, which is not a trade this project
        makes.
        """
        from ..connectors.base import ConnectorError

        name = self._connector_name(message)
        manager = getattr(self._connector_registry(), "signin", None)
        if manager is None:
            raise ValueError("interactive sign-in is not available in this build")
        action = str(message.get("action", "status")).strip() or "status"
        try:
            if action == "start":
                session = manager.start(name, str(message.get("profile", "")), float(message.get("timeout", 240.0)))
                self.bus.emit(
                    HUD_NOTICE,
                    source="connectors",
                    severity="info",
                    message=f"{name}: sign-in started — {session.instruction}",
                    speak=False,
                )
                self.bridge.push_state()
                return session.to_json()
            if action == "cancel":
                cancelled = manager.cancel(name)
                self.bridge.push_state()
                return {"connector": name, "cancelled": cancelled}
            session = manager.status(name)
            return session.to_json() if session is not None else {"connector": name, "stage": "idle"}
        except ConnectorError as exc:
            raise ValueError(str(exc)) from exc

    def _connector_call(self, message: dict[str, Any]) -> dict[str, Any]:
        """Run one operation from the owner's own screen — the "test it now" button.

        Agents reach the same operations through the capability-gated actions, where the
        operation's declared permission is checked against the caller's grant. Here the caller is
        the owner at their own keyboard, so the check is that the operation exists at all.
        """
        from ..connectors.base import ConnectorError

        name = self._connector_name(message)
        operation = str(message.get("operation", "")).strip()
        if not operation:
            raise ValueError("an operation name is required")
        args = message.get("args") if isinstance(message.get("args"), dict) else {}
        try:
            result = self._connector_registry().call(name, operation, **args)
        except KeyError as exc:
            raise ValueError(str(exc)) from exc
        except ConnectorError as exc:
            raise ValueError(f"{name}.{operation} failed: {exc}") from exc
        return {"connector": name, "operation": operation, "result": result}

    def _announce_connector(self, status: Any) -> None:
        severity = "info" if getattr(status, "state", "") == "ready" else "warning"
        self.bus.emit(
            HUD_NOTICE,
            source="connectors",
            severity=severity,
            message=f"{status.name}: {status.state} — {status.detail}",
            speak=False,
        )
        self.bridge.push_state()

    def _voice_ptt(self, message: dict[str, Any]) -> dict[str, Any]:
        if self.voice is None:
            raise ValueError("voice subsystem is not attached")
        return self.voice.push_to_talk(bool(message.get("down", True)))

    def _speech_decision(self, text: str, severity: str = "info") -> dict[str, Any]:
        """Ask the etiquette layer whether this may be said out loud.

        Without a presence controller the answer is "nothing to weigh" and the caller speaks,
        which is the old behaviour. With one attached, the *spoken* path is the one that has to
        pass the check — the chat panel already shows the text, so a held-back reply is never
        lost, only made quiet.
        """
        if self.presence is None:
            return {}
        from ..ops.presence import looks_private

        enabled = bool(getattr(getattr(self.voice, "state", None), "enabled", True))
        urgency = {"error": "danger", "critical": "critical"}.get(severity, "normal")
        try:
            decision = self.presence.decide(text=text, addressed=True, urgency=urgency, voice_enabled=enabled)
        except Exception as exc:  # noqa: BLE001 - report, then take the private-content-safe side
            self.bus.emit(LOG, source="presence", severity="warning", message=f"etiquette check failed: {exc}")
            return {"mode": "text_only", "audible": False, "reason": f"etiquette check failed: {exc}"} if looks_private(text) else {}
        return decision.to_json()

    def _speak(self, message: dict[str, Any]) -> dict[str, Any]:
        text = str(message.get("text", "")).strip()
        decision = self._speech_decision(text)
        if decision and not decision.get("audible", True):
            reason = decision.get("reason", "")
            self.bridge.notify(f"held back ({decision.get('mode', 'silent')}): {reason}", severity="notice", speak=False)
            self.bus.emit(VOICE_SPEAKING, source="hud", text=text, muted=True, reason=reason)
            return {"spoken": "", "held": reason, "mode": decision.get("mode", "silent")}
        if self.voice is not None:
            self.voice.say(text)
        self.bus.emit(VOICE_SPEAKING, source="hud", text=text)
        return {"spoken": text[:200], "mode": decision.get("mode", "") if decision else "unchecked"}

    def _confirm(self, message: dict[str, Any]) -> dict[str, Any]:
        action = str(message.get("action", ""))
        if not action:
            raise ValueError("action name is required")
        approved = bool(message.get("approved", True))
        self.registry.approve(action, approved)
        self.bus.emit(
            HUD_NOTICE,
            source="user",
            text=f"{'approved' if approved else 'denied'} confirmation for {action}",
            speak=False,
        )
        return {"action": action, "approved": approved}

    def _forge(self, message: dict[str, Any]) -> dict[str, Any]:
        if self.plugins is None or getattr(self, "forge", None) is None:
            raise ValueError("the Forge is not attached")
        need = str(message.get("need", "")).strip()
        if not need:
            raise ValueError("describe the need to forge a plugin")
        result = self.forge.forge(need, name=str(message.get("name", "")), capabilities=message.get("capabilities"))
        self.bridge.push_state()
        return result

    def _uninstall(self, message: dict[str, Any]) -> dict[str, Any]:
        if self.plugins is None:
            raise ValueError("plugin host is not attached")
        return self.plugins.uninstall(str(message.get("name", "")))

    def _spawn_agent(self, message: dict[str, Any]) -> dict[str, Any]:
        if self.orchestrator is None:
            raise ValueError("no orchestrator attached")
        role = str(message.get("role", "operative"))
        agent = self.orchestrator.spawn(
            role=role,
            name=str(message.get("name", "")),
            model=str(message.get("model", "")),
            capabilities=message.get("capabilities"),
            team_id=message.get("team_id"),
        )
        self.bridge.push_state()
        return {"agent_id": agent.id, "name": agent.name, "role": role, "model": agent.model}

    def _terminate_agent(self, message: dict[str, Any]) -> dict[str, Any]:
        if self.orchestrator is None:
            raise ValueError("no orchestrator attached")
        target = self.orchestrator.find(str(message.get("agent", "")))
        if target is None:
            raise ValueError("agent not found")
        return {"terminated": self.orchestrator.terminate(target.id, "HUD request")}

    def _assign_model(self, message: dict[str, Any]) -> dict[str, Any]:
        if self.orchestrator is None:
            raise ValueError("no orchestrator attached")
        target = self.orchestrator.find(str(message.get("agent", "")))
        if target is None:
            raise ValueError("agent not found")
        return self.orchestrator.assign_model(target.id, str(message.get("model", "")), "HUD request")

    def _owner_unlock(self, message: dict[str, Any]) -> dict[str, Any]:
        """Unlock an owner session with the PIN so irreversible work can be approved.

        Without this the owner gate would make the GUI unable to do anything risky at all,
        which is not security, it is a dead end.
        """
        if self.owner is None:
            raise ValueError("owner gate is not attached")
        if not self.owner.profile.configured:
            raise ValueError("no owner yet: run `orbynth owner setup` in a terminal")
        pin = str(message.get("pin", ""))
        if not self.owner.verify_pin(pin):
            self.bridge.notify("PIN rejected.", severity="warning", speak=False)
            return {"unlocked": False, "locked_out": self.owner.locked_out()}
        self.bridge.notify("Owner session open.", severity="info", speak=False)
        self.bridge.push_state()
        return {"unlocked": True, "seconds": round(self.owner.session_remaining(), 1)}

    def _jobs_stop(self, message: dict[str, Any]) -> dict[str, Any]:
        if self.supervisor is None:
            raise ValueError("no supervisor attached")
        target = str(message.get("job_id", "")) or str(message.get("name", ""))
        job = next((j for j in self.supervisor.jobs.values() if j.job_id == target or j.name == target), None)
        if job is None:
            raise ValueError(f"no such job: {target}")
        stopped = self.supervisor.stop(job.job_id, force=bool(message.get("force", False)), reason="HUD request")
        self.bridge.push_state()
        return {"stopped": stopped, "job": job.name}

    def _memory_search(self, message: dict[str, Any]) -> dict[str, Any]:
        query = str(message.get("query", ""))
        agent_id = str(message.get("agent_id", "")) or None
        hits = self.memory.recall(agent_id, query, k=int(message.get("k", 10))) if agent_id else []
        if not agent_id:
            merged: list[dict[str, Any]] = []
            for record in self.memory.list_agents()[:12]:
                merged.extend(hit.to_json() for hit in self.memory.recall(record.agent_id, query, k=5))
            merged.sort(key=lambda row: row["score"], reverse=True)
            hits = merged[: int(message.get("k", 10))]
        return {"query": query, "hits": [hit.to_json() if hasattr(hit, "to_json") else hit for hit in hits]}

    def _hw_query(self, message: dict[str, Any]) -> dict[str, Any]:
        if self.sensors is None:
            raise ValueError("the sensor monitor is not running")
        sensors = [str(item) for item in (message.get("sensors") or [])] or None
        return self.sensors.query(sensors, float(message.get("window", 60.0)))

    def _hw_alerts(self, message: dict[str, Any]) -> dict[str, Any]:
        if self.sensors is None:
            raise ValueError("the sensor monitor is not running")
        return {"alerts": self.sensors.recent_alerts(int(message.get("limit", 20)))}

    def _index_search(self, message: dict[str, Any]) -> dict[str, Any]:
        if self.index is None:
            raise ValueError("the local index is not attached")
        query = str(message.get("query", "")).strip()
        if not query:
            raise ValueError("a search needs a query")
        return self.index.search(query, limit=int(message.get("limit", 20)))

    def _index_update(self, message: dict[str, Any]) -> dict[str, Any]:
        if self.index is None:
            raise ValueError("the local index is not attached")
        return self.index.update(
            source=str(message.get("source") or "") or None,
            limit=int(message.get("limit", 2_000)),
            batch=200,
            sleep=0.01,
        )

    def _index_add(self, message: dict[str, Any]) -> dict[str, Any]:
        """Adding a search source is an owner action: the HUD asks for the PIN, not a checkbox."""
        if self.index is None:
            raise ValueError("the local index is not attached")
        kind = str(message.get("kind", "files"))
        if kind in ("email", "whatsapp", "browser_history", "clipboard"):
            if self.owner is not None and not self.owner.session_valid():
                raise ValueError(f"{kind} is personal data: unlock with your PIN before adding it")
        return self.index.add_source(
            str(message.get("name", "docs")),
            str(message.get("root", "")),
            kind=kind,
            opt_in=bool(message.get("opt_in", False)),
        )

    def _audit(self, message: dict[str, Any]) -> dict[str, Any]:
        return {"entries": self.memory.audit_log(limit=int(message.get("limit", 100)))}

    # ----------------------------------------------------------------------------------
    # Voice passthrough
    # ----------------------------------------------------------------------------------
    def _on_voice_transcript(self, event: Any) -> None:
        self.transcript.append({"role": event.payload.get("role", "user"), "text": event.payload.get("text", ""), "ts": event.ts})

    # ----------------------------------------------------------------------------------
    # Run
    # ----------------------------------------------------------------------------------
    def start(self) -> "HudService":
        self.bridge.start(background=True)
        self.bus.emit(
            LOG,
            source="hud",
            severity="info",
            message=f"HUD bridge listening on {self.bridge.url}",
            actions=len(self.registry),
        )
        return self

    def run(self, webview: bool | None = None, open_browser: bool = False) -> None:
        self.start()
        if self.voice is not None and self.config.voice.enabled:
            try:
                self.voice.start()
            except Exception as exc:  # noqa: BLE001 - the HUD must come up even with no mic
                self.bus.emit(LOG, source="voice", severity="warning", message=f"voice unavailable: {exc}")
        use_webview = self.config.hud.webview if webview is None else webview
        if open_browser:
            import webbrowser

            webbrowser.open(self.bridge.url)
            while True:
                time.sleep(1.0)
        if use_webview:
            open_webview(self.bridge.url, title=f"{__product__} — {__tagline__}")
        else:  # headless/service mode: keep serving
            try:
                while True:
                    time.sleep(1.0)
            except KeyboardInterrupt:
                pass
        self.bridge.stop()
