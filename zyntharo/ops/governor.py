"""Keeping the machine responsive (contract 22.11).

A desktop agent that pins the CPU while the user is in a video call has failed, however
clever its plan was. Two mechanisms:

* **Low priority.** ZYNTHARO's own work runs below the user's interactive work: ``nice`` on
  POSIX, ``BELOW_NORMAL_PRIORITY_CLASS`` on Windows. Controlled work (keystrokes, mouse,
  screenshots) still happens on time; only background reasoning is de-prioritised.
* **Pausing.** Games, video calls and presentations get the machine. Detected busy signals
  pause background and maintenance work (a task sweep, a catalog check, an index rebuild)
  and *defer* -- never silently drop -- work that needs the desktop. Work the user asked
  for right now is unaffected: it is their machine and their request.

Detection is driver-dependent and therefore honest: the detectors here report what they can
see and say "unknown" otherwise. Everything is injectable so the policy can be tested
without a desktop -- **including the load reading**. That last one is not a nicety: the load
used to be read straight from the host inside :meth:`ResponsivenessGovernor.evaluate`, so a
test that injected its own busy signals still paused background work whenever the machine
running the tests happened to be loaded, and the release gate failed on a busy CI box for
reasons that had nothing to do with the code.
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
    # The cap is applied before workers are submitted. Pausing later is too late: Chromium,
    # OCR or a local model can already have allocated memory by then.
    worker_cap: int = 1
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
            "worker_cap": self.worker_cap,
            "reason": self.reason,
        }


def machine_load() -> float:
    """Load average per CPU, or 0.0 where the OS has no such reading (notably Windows).

    Injectable so tests and simulations are deterministic instead of inheriting whatever the
    host is doing at the time.
    """
    try:
        load = os.getloadavg()[0]
    except (OSError, AttributeError):
        return 0.0
    return load / (os.cpu_count() or 1)


def _windows_process_names() -> set[str]:  # pragma: no cover - windows only
    """Image names of running processes, for matching against the known-game/call lists.

    The previous Windows branch enumerated every pid and then threw the list away without ever
    asking for a single name, so ``KNOWN_GAMES`` and ``KNOWN_CALLS`` could never match on the
    one platform this product is mainly built for: games and calls went undetected and ZYNTHARO
    happily worked through them.
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    psapi = ctypes.windll.psapi  # type: ignore[attr-defined]
    process_query_limited_information = 0x1000
    buf = (wintypes.DWORD * 4096)()
    needed = wintypes.DWORD()
    if not psapi.EnumProcesses(ctypes.byref(buf), ctypes.sizeof(buf), ctypes.byref(needed)):
        return set()
    names: set[str] = set()
    size = wintypes.DWORD(1024)
    path = ctypes.create_unicode_buffer(1024)
    for index in range(needed.value // ctypes.sizeof(wintypes.DWORD)):
        pid = int(buf[index])
        if pid <= 0:
            continue
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            continue
        try:
            if kernel32.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
                names.add(path.value.rsplit("\\", 1)[-1].lower())
        finally:
            kernel32.CloseHandle(handle)
        size.value = 1024
    return names


def detect_busy() -> list[BusySignal]:
    """Best-effort, read-only probe. Reports nothing rather than inventing a signal."""
    signals: list[BusySignal] = []
    try:  # pragma: no cover - depends on the platform's process listing
        if os.name == "nt":
            names = _windows_process_names()
        else:
            import glob

            names = set()
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
    load_per_cpu = machine_load()
    if load_per_cpu > 0.85:
        cpus = os.cpu_count() or 1
        signals.append(BusySignal(HEAVY_LOAD, f"load {load_per_cpu * cpus:.1f} on {cpus} cpus", 0.8))
    return signals


class ResponsivenessGovernor:
    """Decides what may run while the machine is busy, and how politely."""

    def __init__(
        self,
        bus: EventBus | None = None,
        detector: Callable[[], list[BusySignal]] | None = None,
        load_ceiling: float = 0.85,
        load_provider: Callable[[], float] | None = None,
        max_workers: int = 2,
    ):
        self.bus = bus or EventBus()
        self.detector = detector or detect_busy
        #: Load per CPU. Injected in tests so policy is decided by the signals under test,
        #: never by how busy the machine running the tests happens to be.
        self.load_provider = load_provider or machine_load
        self.load_ceiling = load_ceiling
        self.max_workers = max(1, int(max_workers))
        self._paused = False
        self._priority = PRIORITY_NORMAL

    def busy_reason(self) -> str:
        """What is keeping this machine busy, as one line, or "" when it is free.

        ``ops/preflight.py`` asks this before a task starts so a game, call or presentation
        defers desktop work or routes it down the headless path. Without a caller the busy
        branch of that decision was unreachable in production.
        """
        state = self.evaluate()
        kinds = sorted({signal.kind for signal in state.signals})
        return ", ".join(kinds)

    def evaluate(self, signals: Sequence[BusySignal] | None = None) -> GovernorState:
        signals = list(signals if signals is not None else self.detector())
        try:
            load = float(self.load_provider())
        except Exception:
            load = 0.0  # an unreadable load reading must not stop a task from running
        kinds = {s.kind for s in signals}
        paused = {
            BACKGROUND: bool(kinds & {GAME, VIDEO_CALL, PRESENTATION, FULLSCREEN}) or load > self.load_ceiling,
            MAINTENANCE: bool(kinds) or load > self.load_ceiling * 0.75,
        }
        # Keep one worker alive under mild pressure; zero is reserved for explicit courtesy
        # pauses. Interactive requests are separately exempt in ``may_run``.
        if paused[BACKGROUND]:
            worker_cap = 1
        elif load > self.load_ceiling * 0.6:
            worker_cap = max(1, self.max_workers // 2)
        else:
            worker_cap = self.max_workers
        state = GovernorState(
            paused=paused, signals=signals, load_per_cpu=load, priority=self._priority, worker_cap=worker_cap
        )
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

    def worker_limit(self, requested: int, state: GovernorState, work_class: str = BACKGROUND) -> int:
        """Bound admissions before expensive helpers can be started.

        This is deliberately a *limit*, not a measurement of CPU percent. Python cannot safely
        promise a percentage for child browsers or native models; limiting parallel starts is
        the reliable cross-platform control the supervisor and orchestrator can enforce.
        """
        requested = max(1, int(requested))
        if work_class in {CRITICAL, INTERACTIVE}:
            return min(requested, self.max_workers)
        return min(requested, state.worker_cap)

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

    #: Absolute nice values. ``os.nice()`` is relative, which is what made the old version
    #: wrong: calling it twice with 10 reached nice 20, so "below normal" quietly became "as
    #: low as the OS allows" and there was no way back without privileges.
    _NICE = {PRIORITY_IDLE: 19, PRIORITY_BELOW: 10, PRIORITY_NORMAL: 0}
    _WINDOWS_CLASS = {PRIORITY_IDLE: 0x00000040, PRIORITY_BELOW: 0x00004000, PRIORITY_NORMAL: 0x00000020}

    def apply_priority(self, level: str = PRIORITY_BELOW) -> str:
        """Move this process below interactive work. Absolute, idempotent, never a crash risk."""
        self._priority = level
        try:
            if os.name == "nt":  # pragma: no cover - windows only
                import ctypes

                value = self._WINDOWS_CLASS.get(level, 0x00004000)
                ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), value)  # type: ignore[attr-defined]
            else:
                os.setpriority(os.PRIO_PROCESS, 0, self._NICE.get(level, 10))
        except (OSError, AttributeError, ValueError):
            # Being unable to *lower* priority (or raise it back without privileges) is not
            # fatal. Say so instead of pretending it worked.
            self._priority = PRIORITY_NORMAL
            return PRIORITY_NORMAL
        return level

    def summary(self) -> dict[str, Any]:
        state = self.evaluate()
        return {
            "state": state.to_json(),
            "priority": self._priority,
            "configured_worker_cap": self.max_workers,
            "platform": sys.platform,
            "paused": self._paused,
        }
