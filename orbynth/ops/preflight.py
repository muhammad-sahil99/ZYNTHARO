"""Unattended runs (contract 22.2).

The scheduler fires tasks while the screen is locked, the PC is asleep, or nobody is logged
in. Windows (and macOS, and Wayland) block input injection and desktop access in exactly
those states, so "just try it anyway" produces the worst possible outcome: an agent that
believes it clicked a button.

So a task declares what it needs *before* it starts, and the preflight answers with one of
five honest outcomes:

``run_now``       the machine can do this, now, interactively.
``run_background`` it can proceed without the desktop (headless browser, files, Office over
                  COM on a service account, API work) while the user keeps their screen.
``run_isolated``  it needs a desktop, but not *this* desktop: run in a disposable session or
                  VM. This is how unattended GUI work is supposed to happen.
``defer``         the machine will be able to do it later; the task is queued with a reason
                  and a retry hint.
``skip``          it cannot be done here at all, with a reason a human can act on.

The system never unlocks a session, never injects input into a locked desktop, and never
pretends a deferred task ran. When the honest answer is "this needs a dedicated machine",
that is what it says.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..events import EventBus, TASK_PREFLIGHT

RUN_NOW = "run_now"
RUN_BACKGROUND = "run_background"
RUN_ISOLATED = "run_isolated"
DEFER = "defer"
SKIP = "skip"


@dataclass
class TaskRequirements:
    """What a task needs from the machine. Written when the task is created, not guessed."""

    gui: bool = False
    unlocked_session: bool = False
    power: bool = False
    network: bool = False
    audio: bool = False
    user_present: bool = False  # a human must be there to approve or watch
    isolation_ok: bool = True  # may be run in a disposable session/VM
    background_ok: bool = False  # may run without the desktop at all
    max_wall_seconds: int = 1800
    note: str = ""

    def to_json(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class MachineState:
    """A snapshot of what the machine can currently be asked to do."""

    session_locked: bool = False
    display_attached: bool = True
    on_ac: bool = True
    battery_pct: int = 100
    network: bool = True
    audio_device: bool = False
    idle_seconds: float = 0.0
    busy_reason: str = ""  # "game" | "video_call" | "presentation" | ""
    headless_process: bool = False  # no desktop session reachable at all (service/SSH)
    isolation_available: bool = False  # a disposable session/VM is configured
    detected_at: float = field(default_factory=time.time)

    @property
    def user_active(self) -> bool:
        """Is a human using this machine right now?"""
        return self.idle_seconds < 120 and not self.session_locked

    def to_json(self) -> dict[str, Any]:
        return {**self.__dict__, "user_active": self.user_active}

    @classmethod
    def detect(cls) -> "MachineState":
        """Best-effort live reading. Every probe degrades to "unknown, assume permissive".

        None of these probes injects input or unlocks anything; they only read state.
        """
        state = cls()
        try:
            if os.name == "nt":  # pragma: no cover - windows only
                import ctypes

                user32 = ctypes.windll.user32  # type: ignore[attr-defined]
                state.session_locked = bool(user32.GetForegroundWindow() == 0)
                state.display_attached = bool(user32.GetSystemMetrics(0) > 0)
                state.audio_device = bool(user32.GetSystemMetrics(0) > 0)
            else:
                state.display_attached = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
                state.headless_process = not state.display_attached
            battery = _battery()
            if battery is not None:
                state.battery_pct, state.on_ac = battery
        except Exception:  # a probe must never break a task
            return cls()
        return state


def _battery() -> tuple[int, bool] | None:
    """Read battery charge without a dependency, or return None on a desktop machine."""
    base = Path("/sys/class/power_supply")
    if base.exists():
        pct: int | None = None
        on_ac = False
        for entry in base.iterdir():
            try:
                kind = (entry / "type").read_text(encoding="utf-8").strip().lower()
                if kind == "battery":
                    pct = int((entry / "capacity").read_text(encoding="utf-8").strip())
                elif kind in {"mains", "usb"}:
                    on_ac = (entry / "online").read_text(encoding="utf-8").strip() == "1"
            except (OSError, ValueError):
                continue
        if pct is not None:
            return pct, on_ac
    return None


@dataclass
class PreflightDecision:
    action: str
    reasons: list[str] = field(default_factory=list)
    retry_after: float = 0.0
    advice: str = ""
    requirements: TaskRequirements | None = None
    state: MachineState | None = None

    @property
    def can_run(self) -> bool:
        return self.action in {RUN_NOW, RUN_BACKGROUND, RUN_ISOLATED}

    def to_json(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "reasons": self.reasons,
            "retry_after": round(self.retry_after, 1),
            "advice": self.advice,
            "can_run": self.can_run,
        }

    def one_line(self) -> str:
        detail = f": {'; '.join(self.reasons)}" if self.reasons else ""
        return f"{self.action}{detail}"


class Preflight:
    """Turns (task requirements, machine state) into a decision. Pure logic, fully testable."""

    def __init__(self, bus: EventBus | None = None, state_provider: Any = None):
        self.bus = bus or EventBus()
        #: Overridable for tests, for a remote-session probe, or for a headless server farm.
        self.state_provider = state_provider or MachineState.detect

    def check(self, requirements: TaskRequirements, origin: str = "user_request", state: MachineState | None = None) -> PreflightDecision:
        state = state or self.state_provider()
        reasons: list[str] = []
        decision: PreflightDecision

        needs_desktop = requirements.gui or requirements.unlocked_session
        desktop_unusable = state.session_locked or not state.display_attached or state.headless_process

        if needs_desktop and desktop_unusable:
            because = "session is locked" if state.session_locked else "no desktop session is reachable"
            if requirements.isolation_ok and state.isolation_available:
                decision = PreflightDecision(
                    RUN_ISOLATED,
                    [f"{because}; a disposable session is available"],
                    advice="running in an isolated session so nothing touches the user's desktop",
                )
            elif origin == "user_request":
                # The human is right there asking: say what to unlock, do not fake it.
                decision = PreflightDecision(
                    DEFER,
                    [because],
                    retry_after=30.0,
                    advice="unlock the screen to run this, or enable an isolated session/VM for unattended GUI work",
                )
            else:
                decision = PreflightDecision(
                    DEFER,
                    [because, "task needs the desktop at a time the desktop is unavailable"],
                    retry_after=300.0,
                    advice="ORBYNTH never unlocks a session or injects input into a locked desktop; "
                    "schedule unattended GUI work on an always-on machine or VM",
                )
        elif requirements.power and not state.on_ac and state.battery_pct < 20:
            decision = PreflightDecision(SKIP if origin == "user_request" else DEFER, [f"on battery at {state.battery_pct}%"], retry_after=600.0)
        elif requirements.network and not state.network:
            decision = PreflightDecision(DEFER, ["no network connection"], retry_after=60.0)
        elif requirements.audio and not state.audio_device:
            decision = PreflightDecision(SKIP, ["no audio device available"], advice="voice tasks need a microphone and speaker")
        elif requirements.user_present and not state.user_active:
            decision = PreflightDecision(DEFER, ["task requires a human present for approval"], retry_after=120.0)
        elif state.busy_reason and not requirements.user_present:
            # A game, call or presentation is running: yield the machine (see governor, 22.11).
            if requirements.background_ok:
                decision = PreflightDecision(RUN_BACKGROUND, [f"machine busy: {state.busy_reason}; proceeding without the desktop"])
            else:
                decision = PreflightDecision(DEFER, [f"machine busy: {state.busy_reason}"], retry_after=120.0, advice="tasks needing the desktop wait while the user is in a call, game or presentation")
        elif requirements.gui and origin != "user_request" and requirements.background_ok and not state.user_active:
            decision = PreflightDecision(RUN_BACKGROUND, ["no user at the desktop; using the headless path"])
        else:
            decision = PreflightDecision(RUN_NOW, ["machine state satisfies the task's requirements"])

        decision.requirements = requirements
        decision.state = state
        self.bus.emit(
            TASK_PREFLIGHT,
            source="preflight",
            severity="info" if decision.can_run else "notice",
            action=decision.action,
            origin=origin,
            reasons=decision.reasons,
            requirements=requirements.to_json(),
        )
        return decision
