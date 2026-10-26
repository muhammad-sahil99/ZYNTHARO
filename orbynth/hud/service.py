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
from ..events import LOG, VOICE_SPEAKING, VOICE_TRANSCRIPT, EventBus
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
        static_dir: str | Path | None = None,
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
        self.static_dir = Path(static_dir) if static_dir else Path(__file__).resolve().parent.parent.parent / "hud"
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
        }

    # ----------------------------------------------------------------------------------
    # Routing user intent from the HUD
    # ----------------------------------------------------------------------------------
    def route(self, kind: str, message: dict[str, Any], client: Client) -> Any:
        handler: Callable[[dict[str, Any]], Any] | None = {
            "chat": self._chat,
            "task.submit": self._task,
            "voice.mute": self._voice_mute,
            "voice.ptt": self._voice_ptt,
            "speak": self._speak,
            "confirm": self._confirm,
            "plugin.list": lambda _m: self.plugins.installed() if self.plugins else [],
            "plugin.forge": self._forge,
            "plugin.uninstall": self._uninstall,
            "agent.spawn": self._spawn_agent,
            "agent.terminate": self._terminate_agent,
            "model.assign": self._assign_model,
            "state.request": lambda _m: self.snapshot(),
            "memory.search": self._memory_search,
            "audit": self._audit,
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
                self.bridge.notify(report.summary, severity="info" if report.status == "done" else "warning", speak=True)
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

    def _voice_ptt(self, message: dict[str, Any]) -> dict[str, Any]:
        if self.voice is None:
            raise ValueError("voice subsystem is not attached")
        return self.voice.push_to_talk(bool(message.get("down", True)))

    def _speak(self, message: dict[str, Any]) -> dict[str, Any]:
        text = str(message.get("text", "")).strip()
        if self.voice is not None:
            self.voice.say(text)
        self.bus.emit(VOICE_SPEAKING, source="hud", text=text)
        return {"spoken": text[:200]}

    def _confirm(self, message: dict[str, Any]) -> dict[str, Any]:
        action = str(message.get("action", ""))
        if not action:
            raise ValueError("action name is required")
        approved = bool(message.get("approved", True))
        self.registry.approve(action, approved)
        self.bus.emit(
            "hud.notice",
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
