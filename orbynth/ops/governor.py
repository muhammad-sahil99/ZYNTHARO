"""Keeping the machine responsive (contract 22.11).

A desktop agent that pins the CPU while the user is in a video call has failed, however
clever its plan was. Two mechanisms:

* **Low priority.** ORBYNTH's own work runs below the user's interactive work: ``nice`` on
  POSIX, ``BELOW_NORMAL_PRIORITY_CLASS`` on Windows. Controlled work (keystrokes, mouse,
  screenshots) still happens on time; only background reasoning is de-prioritised.
* **Pausing.** Games, video calls and presentations get the machine. Detected busy signals
  pause background and maintenance work (a task sweep, a catalog check, an index rebuild)
  and *defer* -- never silently drop -- work that needs the desktop. Work the user asked
  for right now is unaffected: it is their machine and their request.

Detection is driver-dependent and therefore honest: the detectors here report what they can
see and say "unknown" otherwise. Everything is injectable so the policy can be tested
without a desktop.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Sequence

from ..events import EventBus, GOVERNOR_PAUSED, GOVERNOR_RESUMED

CRITICAL = "critical"
INTERACTIVE = "interactive"
BACKGROUND = "background"
MAINTENANCE = "maintenance"

PRIORITY_NORMAL = "normal"
PRIORITY_BELOW = "below_normal"
PRIORITY_IDLE = "idle"

GAME = "game"
VIDEO_CALL = "video_call"
PRESENTATION = "presentation"
FULLSCREEN = "fullscreen"
HEAVY_LOAD = "heavy_load"

KNOWN_GAMES = ("valorant", "cs2", "fortnite", "genshinimpact", "leagueclient", "dota2", "steam")
KNOWN_CALLS = ("zoom", "teams", "webex", "discord", "slack", "meet", "skype")


@dataclass
class BusySignal:
    kind: str
    detail: str = ""
    confidence: float = 0.5


@dataclass
class GovernorState:
    paused: dict[str, bool] = field(default_factory=dict)
    signals: list[BusySignal] = field(default_factory=list)
    load_per_cpu: float = 0.0
    priority: str = PRIORITY_BELOW
    reason: str = ""

    @property
    def any_pause(self) -> bool:
        return any(self.paused.values())

    def to_json(self) -> dict[str, Any]:
        return {
            "paused": self.paused,
            "signals": [asdict(s) for s in self.signals],
            "load_per_cpu": round(self.load_per_cpu, 3),
            "priority": self.priority,
            "reason": self.reason,
        }


def detect_busy() -> list[BusySignal]:
    """Best-effort, read-only probe. Reports nothing rather than inventing a signal."""
    signals: list[BusySignal] = []
    try:  # pragma: no cover - depends on the platform's process listing
        names: set[str] = set()
        if os.name == "nt":
            import ctypes

            psapi = ctypes.windll.psapi  # type: ignore[attr-defined]
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.GetCurrentProcess()
            count = 1024
            buf = (ctypes.c_ulong * count)()
            needed = ctypes.c_ulong()
            if psapi.EnumProcesses(ctypes.byref(buf), ctypes.sizeof(buf), ctypes.byref(needed)):
                for pid in buf[: needed.value // ctypes.sizeof(ctypes.c_ulong)]:
                    if not pid:
                        continue
                    proc = kernel32.OpenProcess(0x1000, False, pid)
                    if proc:
                        kernel32.CloseHandle(proc)
            names = set()
        else:
            import glob

            for path in glob.glob("/proc/[0-9]*/comm"):
                try:
                    names.add(open(path, "r", encoding="utf-8", errors="replace").read().strip().lower())
                except OSError:
                    continue
        for name in names:
            if any(game in name for game in KNOWN_GAMES):
                signals.append(BusySignal(GAME, name, 0.7))
            if any(call in name for call in KNOWN_CALLS):
                signals.append(BusySignal(VIDEO_CALL, name, 0.4))
    except Exception:
        return signals
    load = os.getloadavg()[0] if hasattr(os, "getloadavg") else 0.0
    cpus = os.cpu_count() or 1
    if load / cpus > 0.85:
        signals.append(BusySignal(HEAVY_LOAD, f"load {load:.1f} on {cpus} cpus", 0.8))
    return signals


class ResponsivenessGovernor:
    """Decides what may run while the machine is busy, and how politely."""

    def __init__(self, bus: EventBus | None = None, detector: Callable[[], list[BusySignal]] | None = None, load_ceiling: float = 0.85):
        self.bus = bus or EventBus()
        self.detector = detector or detect_busy
        self.load_ceiling = load_ceiling
        self._paused = False
        self._priority = PRIORITY_NORMAL

    def evaluate(self, signals: Sequence[BusySignal] | None = None) -> GovernorState:
        signals = list(signals if signals is not None else self.detector())
        load = os.getloadavg()[0] / (os.cpu_count() or 1) if hasattr(os, "getloadavg") else 0.0
        kinds = {s.kind for s in signals}
        paused = {
            BACKGROUND: bool(kinds & {GAME, VIDEO_CALL, PRESENTATION, FULLSCREEN}) or load > self.load_ceiling,
            MAINTENANCE: bool(kinds) or load > self.load_ceiling * 0.75,
        }
        state = GovernorState(paused=paused, signals=signals, load_per_cpu=load, priority=self._priority)
        if paused[BACKGROUND]:
            state.reason = "busy: " + ", ".join(sorted(kinds | ({HEAVY_LOAD} if load > self.load_ceiling else set())))
        if paused[BACKGROUND] != self._paused:
            self._paused = paused[BACKGROUND]
            self.bus.emit(
                GOVERNOR_PAUSED if self._paused else GOVERNOR_RESUMED,
                source="governor",
                severity="notice",
                reasons=state.reason,
                tasks_affected=["background", "maintenance"] if self._paused else [],
            )
        return state

    @staticmethod
    def may_run(work_class: str, state: GovernorState) -> tuple[bool, str]:
        """The policy in one place, so no caller has to reinterpret it."""
        if work_class in {CRITICAL, INTERACTIVE}:
            return True, ""
        if work_class == BACKGROUND and state.paused.get(BACKGROUND):
            return False, f"background work deferred: {state.reason or 'machine busy'}"
        if work_class == MAINTENANCE and state.paused.get(MAINTENANCE):
            return False, f"maintenance deferred: {state.reason or 'machine busy'}"
        return True, ""

    def delay_for(self, work_class: str, state: GovernorState) -> float:
        """How long to wait before the next unit of this class of work."""
        if work_class == MAINTENANCE and state.paused.get(MAINTENANCE):
            return 30.0
        if work_class == BACKGROUND:
            if state.paused.get(BACKGROUND):
                return 10.0
            if state.load_per_cpu > self.load_ceiling * 0.6:
                return 1.0
        return 0.0

    def apply_priority(self, level: str = PRIORITY_BELOW) -> str:
        """Move this process below interactive work. Reversible, and never a crash risk."""
        self._priority = level
        if level == PRIORITY_NORMAL:
            return level
        try:
            if os.name == "nt":  # pragma: no cover - windows only
                import ctypes

                # BELOW_NORMAL_PRIORITY_CLASS = 0x00004000, IDLE = 0x00000040
                value = 0x00000040 if level == PRIORITY_IDLE else 0x00004000
                ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), value)  # type: ignore[attr-defined]
                return level
            os.nice(19 if level == PRIORITY_IDLE else 10)
            return level
        except (OSError, AttributeError):
            # Being unable to lower priority is not fatal; say so instead of pretending.
            self._priority = PRIORITY_NORMAL
            return PRIORITY_NORMAL

    def summary(self) -> dict[str, Any]:
        state = self.evaluate()
        return {"state": state.to_json(), "priority": self._priority, "platform": sys.platform, "paused": self._paused}
