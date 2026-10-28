"""Typed event bus.

Every meaningful thing the system does is published here first. The HUD's wire graph,
the audit trail and the transcript are all downstream consumers of this bus — which is
why the HUD can show truthful state instead of animation playing at nothing.

Guarantees:
  * ``publish`` is synchronous and never blocks on subscribers (handlers are queued).
  * An exception in one subscriber never affects another or the publisher.
  * The most recent ``history_size`` events are retained and replayable for a HUD that
    attaches late.
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable

# --------------------------------------------------------------------------------------
# Topic constants — the HUD maps these to visual effects (see ARCHITECTURE.md §6).
# --------------------------------------------------------------------------------------

AGENT_SPAWNED = "agent.spawned"
AGENT_TERMINATED = "agent.terminated"
AGENT_STATUS = "agent.status"
AGENT_MESSAGE = "agent.message"
AGENT_REPORT = "agent.report"
AGENT_ERROR = "agent.error"
#: Inter-agent comms: memos, help requests, answers and file handoffs between agents. The
#: fleet talks to itself, and every message is a stored fact, not a whisper.
AGENT_COMMS = "agent.comms"
AGENT_HELP_REQUEST = "agent.help_request"
AGENT_HELP_ANSWERED = "agent.help_answered"

#: Reserve model activated or retired, and limits recorded/refused (Control Manager).
MODEL_RESERVE = "model.reserve"
MODEL_LIMITS = "model.limits"

TASK_CREATED = "task.created"
TASK_ASSIGNED = "task.assigned"
TASK_DONE = "task.done"

ACTION_INVOKED = "action.invoked"
ACTION_RESULT = "action.result"
ACTION_DENIED = "action.denied"
ACTION_CONFIRM = "action.confirm"

MODEL_CALL = "model.call"
MODEL_FALLBACK = "model.fallback"
QUOTA_WARNING = "quota.warning"
QUOTA_EXHAUSTED = "quota.exhausted"
PROVIDER_ERROR = "provider.error"

BILLING_BLOCKED = "billing.blocked"

MEMORY_WRITE = "memory.write"
MEMORY_RECALL = "memory.recall"

SUPERVISOR_STARTED = "supervisor.started"
SUPERVISOR_RECONCILED = "supervisor.reconciled"
SUPERVISOR_STOPPED = "supervisor.stopped"
JOB_STARTED = "job.started"
JOB_EXITED = "job.exited"
JOB_RESTARTED = "job.restarted"
JOB_ORPHANED = "job.orphaned"

#: The action journal (operator contract 4.5): what an interrupted session left behind, and
#: what an undo actually reversed. Both are user-visible facts, not internal bookkeeping.
JOURNAL_RECONCILED = "journal.reconciled"
JOURNAL_UNDONE = "journal.undone"

TASK_PREFLIGHT = "task.preflight"
TASK_VERIFIED = "task.verified"
TASK_DISPUTED = "task.disputed"

OWNER_SETUP = "owner.setup"
OWNER_VERIFIED = "owner.verified"
OWNER_DENIED = "owner.denied"

UPDATE_STAGED = "update.staged"
UPDATE_APPLIED = "update.applied"
UPDATE_BLOCKED = "update.blocked"

GOVERNOR_PAUSED = "governor.paused"
GOVERNOR_RESUMED = "governor.resumed"

PLUGIN_AUTHORED = "plugin.authored"
PLUGIN_INSTALLED = "plugin.installed"
PLUGIN_FAILED = "plugin.failed"
PLUGIN_UNINSTALLED = "plugin.uninstalled"

VOICE_LISTENING = "voice.listening"
VOICE_TRANSCRIPT = "voice.transcript"
VOICE_SPEAKING = "voice.speaking"
VOICE_WAKE = "voice.wake"
#: Spoken/heard language differs from the configured one (multi-language interface, 22.11).
VOICE_LANGUAGE = "voice.language"

#: Hardware monitor (spec 16): a deterministic service, not an agent.
HARDWARE_ALERT = "hardware.alert"
HARDWARE_CLEARED = "hardware.cleared"
HARDWARE_TUNED = "hardware.tuned"

#: Local search index (spec 14.2): what a scan actually did, never what it hoped to do.
LOCAL_INDEX = "index.scan"

HUD_SCENE = "hud.scene"
HUD_NOTICE = "hud.notice"
LOG = "system.log"


@dataclass(slots=True)
class Event:
    topic: str
    payload: dict[str, Any] = field(default_factory=dict)
    source: str = "system"
    severity: str = "info"  # debug | info | notice | warning | error | critical
    ts: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["ts_iso"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.ts))
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Event":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


Handler = Callable[[Event], None]


class Subscription:
    """Handle for a live subscription; use as a context manager or call :meth:`close`."""

    def __init__(self, bus: "EventBus", pattern: str, handler: Handler):
        self._bus = bus
        self.pattern = pattern
        self.handler = handler
        self.closed = False

    def close(self) -> None:
        if not self.closed:
            self._bus.unsubscribe(self)
            self.closed = True

    def __enter__(self) -> "Subscription":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class EventBus:
    """In-process pub/sub with topic wildcards and a replayable ring buffer."""

    def __init__(self, history_size: int = 500, loop: asyncio.AbstractEventLoop | None = None):
        self._subs: list[Subscription] = []
        self._history: deque[Event] = deque(maxlen=history_size)
        self._loop = loop
        self._counters: dict[str, int] = {}

    # -- subscription ------------------------------------------------------------------
    def subscribe(self, pattern: str, handler: Handler) -> Subscription:
        sub = Subscription(self, pattern, handler)
        self._subs.append(sub)
        # Catch the new subscriber up on retained history it cares about.
        for event in list(self._history):
            if fnmatch.fnmatch(event.topic, pattern):
                self._safe_call(handler, event)
        return sub

    def subscribe_many(self, patterns: Iterable[str], handler: Handler) -> list[Subscription]:
        return [self.subscribe(p, handler) for p in patterns]

    def unsubscribe(self, sub: Subscription) -> None:
        with contextlib.suppress(ValueError):
            self._subs.remove(sub)

    def unsubscribe_all(self) -> None:
        self._subs.clear()

    # -- publishing --------------------------------------------------------------------
    def publish(self, event: Event | str, **payload: Any) -> Event:
        if isinstance(event, str):
            event = Event(topic=event, payload=payload)
        elif payload:
            event.payload.update(payload)
        self._history.append(event)
        self._counters[event.topic] = self._counters.get(event.topic, 0) + 1
        for sub in list(self._subs):
            if sub.closed:
                continue
            if fnmatch.fnmatch(event.topic, sub.pattern):
                self._safe_call(sub.handler, event)
        return event

    def emit(self, topic: str, source: str = "system", severity: str = "info", **payload: Any) -> Event:
        """Convenience: publish with source/severity set from keyword arguments."""
        return self.publish(Event(topic=topic, source=source, severity=severity, payload=payload))

    def _safe_call(self, handler: Handler, event: Event) -> None:
        try:
            handler(event)
        except Exception as exc:  # a broken subscriber must never break the system
            self._counters["_handler_errors"] = self._counters.get("_handler_errors", 0) + 1
            if self._loop is not None:
                with contextlib.suppress(RuntimeError):
                    self._loop.call_soon_threadsafe(
                        lambda: None
                    )
            # Best-effort: record the failure without recursing through the bus.
            if event.topic != LOG:
                self.publish(
                    Event(
                        topic=LOG,
                        severity="error",
                        source="event_bus",
                        payload={
                            "message": f"subscriber failed for {event.topic}: {exc!r}",
                            "handler": getattr(handler, "__qualname__", repr(handler)),
                        },
                    )
                )

    # -- async helpers -----------------------------------------------------------------
    async def apublish(self, event: Event | str, **payload: Any) -> Event:
        """Publish from async code, yielding to the loop so streaming stays responsive."""
        result = self.publish(event, **payload)
        await asyncio.sleep(0)
        return result

    # -- introspection -----------------------------------------------------------------
    def history(self, pattern: str = "*", limit: int = 100) -> list[Event]:
        hits = [e for e in self._history if fnmatch.fnmatch(e.topic, pattern)]
        return hits[-limit:]

    def stats(self) -> dict[str, int]:
        return dict(self._counters)

    def replay_to(self, handler: Handler, pattern: str = "*", limit: int = 200) -> None:
        for event in self.history(pattern, limit):
            handler(event)
