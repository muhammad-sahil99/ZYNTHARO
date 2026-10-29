"""Scheduled work: run it later, and be honest when "later" is impossible.

The spec promised tasks that run while the user is away. Two things make that promise either
real or dishonest, and this module is about both:

* **A missed window is not a backlog.** A machine that was asleep for eight hours must not
  wake up and fire eleven queued GUI tasks at once on a locked screen. Each entry declares a
  grace period; past it, the run is *skipped with a reason* and re-armed for the next window.
  Silently replaying stale work is how an automation does something destructive at the wrong
  time.
* **The run gate comes first, every time.** The scheduler asks an injected gate (the preflight
  layer, in the real build) whether the machine can do this *now*: GUI work on a locked or
  sleeping session is deferred rather than run against a screen nobody can see.

One-shot and recurring entries share a shape because the differences are small and the failure
modes are identical. Everything is bounded: a tick handles at most ``max_per_tick`` entries and
the background loop sleeps between ticks, so a scheduler cannot become the thing that keeps the
CPU busy.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

MIN_INTERVAL_SECONDS = 60
DEFAULT_GRACE_SECONDS = 1800


@dataclass(slots=True)
class ScheduledTask:
    id: str
    title: str
    objective: str
    created_by: str = "user"
    next_run: float = 0.0
    every_seconds: float = 0.0  # 0 = one-shot
    needs_gui: bool = False
    enabled: bool = True
    runs: int = 0
    #: Set while a run is in flight. Surviving a restart with this set means the process died
    #: mid-run, which is reported instead of being silently repeated.
    running_since: float = 0.0
    last_run: float = 0.0
    #: How many times this entry was deferred by the gate. Capped: an entry that can never run
    #: must stop being retried, or it becomes a permanent quiet failure.
    deferrals: int = 0
    last_outcome: str = ""
    last_reason: str = ""
    created_at: float = field(default_factory=time.time)

    @property
    def recurring(self) -> bool:
        return self.every_seconds > 0

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["in_seconds"] = max(0, int(self.next_run - time.time())) if self.enabled else None
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "ScheduledTask":
        known = {name for name in cls.__dataclass_fields__}
        return cls(**{key: value for key, value in (data or {}).items() if key in known})


class Scheduler:
    """A bounded, gated scheduler with a policy for missed runs and a background tick."""

    def __init__(
        self,
        bus: Any = None,
        runner: Callable[[ScheduledTask], Any] | None = None,
        gate: Callable[[ScheduledTask], tuple[bool, str]] | None = None,
        home: str | os.PathLike[str] | None = None,
        path: str | os.PathLike[str] | None = None,
        grace_seconds: float = DEFAULT_GRACE_SECONDS,
        defer_seconds: float = 300.0,
        max_deferrals: int = 5,
        max_per_tick: int = 4,
        tick_seconds: float = 20.0,
    ):
        self.bus = bus
        self.runner = runner
        self.gate = gate
        self.grace_seconds = grace_seconds
        #: How long a deferred run waits before being looked at again. Deferral means "not now,
        #: but still wanted", which is the opposite of a missed window.
        self.defer_seconds = max(1.0, min(defer_seconds, grace_seconds))
        self.max_deferrals = max(1, max_deferrals)
        self.max_per_tick = max(1, max_per_tick)
        self.tick_seconds = max(1.0, tick_seconds)
        self._path = Path(path) if path is not None else (Path(home) if home is not None else Path(".")) / "schedule.json"
        self.entries: dict[str, ScheduledTask] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.last_tick: dict[str, Any] = {}
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    # -- storage -----------------------------------------------------------------------
    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        now = time.time()
        for row in data.get("tasks") or []:
            task = ScheduledTask.from_json(row)
            if not task.id:
                continue
            if task.running_since:
                # The process stopped while this task was running. A one-shot is not replayed —
                # it may have half-finished, and repeating a half-finished action is worse than
                # reporting it. A recurring entry simply waits for its next real window.
                task.last_outcome = "interrupted: the process stopped mid-run; not replayed"
                task.last_reason = "interrupted"
                task.running_since = 0.0
                if task.recurring:
                    task.next_run = max(task.next_run, now + task.every_seconds)
                else:
                    task.enabled = False
            self.entries[task.id] = task

    def save(self) -> Path:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        rows = [task.to_json() for task in sorted(self.entries.values(), key=lambda item: item.next_run)]
        tmp.write_text(json.dumps({"version": 1, "tasks": rows}, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)
        return self._path

    def _emit(self, topic: str, **payload: Any) -> None:
        if self.bus is None:
            return
        try:
            self.bus.emit(topic, source="scheduler", **payload)
        except Exception:  # noqa: BLE001 - a broken interface must not stop the schedule
            pass

    # -- authoring ---------------------------------------------------------------------
    def schedule(
        self,
        objective: str,
        title: str = "",
        in_seconds: float | None = None,
        at: float | None = None,
        every_seconds: float = 0.0,
        needs_gui: bool = False,
        created_by: str = "user",
        now: float | None = None,
    ) -> ScheduledTask:
        """Add an entry. Refuses the shapes that quietly do nothing.

        A time in the past, a "recurring" interval under a minute, or an objective shorter than
        a sentence are all refused *here*, because the alternative is an entry that sits in the
        list looking scheduled and never runs — worse than an error.
        """
        now = time.time() if now is None else now
        text = (objective or "").strip()
        if len(text) < 8:
            raise ValueError("objective is too short to run: describe what should happen")
        if at is None and in_seconds is None:
            raise ValueError("give either in_seconds or an absolute at time")
        when = float(at) if at is not None else now + float(in_seconds or 0)
        if when <= now - 1:
            raise ValueError(f"that time is in the past ({int(now - when)}s ago); schedule it again from now")
        every = float(every_seconds or 0.0)
        if every and every < MIN_INTERVAL_SECONDS:
            raise ValueError(f"a recurring task cannot run more often than every {MIN_INTERVAL_SECONDS}s")
        entry = ScheduledTask(
            id=uuid.uuid4().hex[:10],
            title=(title or text.splitlines()[0])[:120],
            objective=text[:8000],
            created_by=created_by,
            next_run=when,
            every_seconds=every,
            needs_gui=bool(needs_gui),
            created_at=now,
        )
        with self._lock:
            self.entries[entry.id] = entry
            self.save()
        self._emit("task.scheduled", task=entry.to_json(), in_seconds=round(when - now, 1))
        return entry

    def cancel(self, id_or_title: str) -> bool:
        with self._lock:
            target = self.entries.get(id_or_title)
            if target is None:
                target = next((task for task in self.entries.values() if task.title == id_or_title), None)
            if target is None:
                return False
            self.entries.pop(target.id, None)
            self.save()
        return True

    # -- running -----------------------------------------------------------------------
    def due(self, now: float | None = None) -> list[ScheduledTask]:
        now = time.time() if now is None else now
        return [task for task in self.entries.values() if task.enabled and task.next_run <= now]

    def tick(self, now: float | None = None) -> dict[str, Any]:
        """Run whatever is due, up to the per-tick bound, and report each decision."""
        now = time.time() if now is None else now
        ready = sorted(self.due(now), key=lambda item: item.next_run)[: self.max_per_tick]
        ran: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for task in ready:
            lateness = now - task.next_run
            if lateness > self.grace_seconds:
                # Either the machine was off, or nobody noticed. Either way the work is stale;
                # firing it now would act on a world that has moved on.
                reason = f"missed its window by {int(lateness)}s (grace {int(self.grace_seconds)}s)"
                self._skip(task, reason, now)
                skipped.append({"id": task.id, "title": task.title, "reason": reason})
                continue
            if self.gate is not None:
                try:
                    allowed, reason = self.gate(task)
                except Exception as exc:  # noqa: BLE001 - an unknown gate is a refusal, not a pass
                    allowed, reason = False, f"gate failed: {exc}"
                if not allowed:
                    message = f"deferred: {reason}"
                    self._skip(task, message, now, defer=True)
                    skipped.append({"id": task.id, "title": task.title, "reason": message})
                    continue
            self._emit("task.due", task=task.to_json(), lateness=round(lateness, 1))
            task.running_since = now
            with self._lock:
                self.save()
            outcome = "ran"
            try:
                result = self.runner(task) if self.runner is not None else None
                if isinstance(result, dict) and result.get("status"):
                    outcome = f"ran: {result['status']}"
            except Exception as exc:  # noqa: BLE001 - one failing task must not stop the rest
                outcome = f"failed: {type(exc).__name__}: {exc}"[:300]
            self._finish(task, outcome, now)
            ran.append({"id": task.id, "title": task.title, "outcome": outcome})
        summary = {"at": now, "ran": ran, "skipped": skipped, "due": len(ready)}
        self.last_tick = summary
        return summary

    def _skip(self, task: ScheduledTask, reason: str, now: float, defer: bool = False) -> None:
        task.last_reason = reason
        task.running_since = 0.0
        if defer:
            # "Not now" is not "never": retry on the next deferred window and keep the entry
            # armed, otherwise a locked screen would silently cancel work the user asked for.
            task.deferrals += 1
            if task.deferrals >= self.max_deferrals:
                task.enabled = False
                task.last_reason = f"{reason} (gave up after {task.deferrals} deferrals)"
                if task.recurring:
                    task.next_run = self._next_window(task, now)
                    task.enabled = True
                    task.deferrals = 0
                    task.last_reason = f"{reason} (deferral limit reset for the next window)"
            else:
                task.next_run = now + self.defer_seconds
                task.enabled = True
        elif task.recurring:
            task.next_run = self._next_window(task, now)
        else:
            task.enabled = False
        with self._lock:
            self.save()
        self._emit("task.skipped", task=task.to_json(), reason=reason)

    def _finish(self, task: ScheduledTask, outcome: str, now: float) -> None:
        task.runs += 1
        task.last_run = now
        task.last_outcome = outcome
        task.last_reason = ""
        task.running_since = 0.0
        task.deferrals = 0
        if task.recurring:
            task.next_run = self._next_window(task, now)
        else:
            task.enabled = False
        with self._lock:
            self.save()
        self._emit("task.schedule_result", task=task.to_json(), outcome=outcome)

    @staticmethod
    def _next_window(task: ScheduledTask, now: float) -> float:
        """Advance to the next future window, skipping periods already gone."""
        step = task.every_seconds or MIN_INTERVAL_SECONDS
        next_run = task.next_run + step
        if next_run <= now:
            missed = int((now - next_run) // step) + 1
            next_run += missed * step
        return next_run

    # -- background loop ---------------------------------------------------------------
    def start(self) -> dict[str, Any]:
        if self._thread is not None and self._thread.is_alive():
            return {"started": True, "already": True}
        if self.runner is None:
            return {"started": False, "reason": "no runner is attached, so nothing would execute"}
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self._thread.start()
        return {"started": True, "entries": len(self.entries), "tick_seconds": self.tick_seconds}

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - a scheduler crash would silently stop all future work
                pass
            self._stop.wait(self.tick_seconds)

    # -- introspection -----------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        tasks = sorted(self.entries.values(), key=lambda item: (not item.enabled, item.next_run))
        return {
            "count": len(tasks),
            "enabled": sum(1 for task in tasks if task.enabled),
            "running": bool(self._thread and self._thread.is_alive()),
            "tasks": [task.to_json() for task in tasks[:40]],
            "last_tick": self.last_tick,
            "policy": {
                "grace_seconds": int(self.grace_seconds),
                "max_per_tick": self.max_per_tick,
                "min_interval_seconds": MIN_INTERVAL_SECONDS,
            },
        }
