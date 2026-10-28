"""The Action Journal: a write-ahead log of every action the system intends to take.

Specification 4.5 asks for exactly-once behaviour across crashes, retries and model failover,
and for "undo last task". Both need the same thing: a durable record written *before* the side
effect, not after. The audit log answers "what happened"; the journal answers "what was about
to happen, and did it". Those are different questions, and only the second one can be answered
after a power cut.

What the journal guarantees:

* **Write-ahead.** An entry reaches disk in state ``intended`` before the action executes and
  is moved to ``running`` immediately before the side effect. A crash therefore leaves a
  record of work that may or may not have landed, which is precisely the case that needs
  reconciliation.
* **Exactly once, when asked.** An action that carries an idempotency key runs at most once:
  a replay of the same key returns the recorded outcome instead of executing again. Actions
  without a key stay repeatable, because two clicks on the same button are a legitimate user
  intention, not a duplication.
* **Reconciliation, not guessing.** At startup every open entry is checked against *real*
  state. The result is one of three honest answers: the effect is there (the action committed),
  the effect is absent (it never happened, so it is abandoned), or it cannot be determined at
  all — which is surfaced as "needs a decision" instead of being auto-completed or auto-undone.
* **Undo where undo exists.** Compensating actions are registered per tool. A tool with no
  compensation reports that plainly rather than pretending. Irreversible capabilities (see
  ``ops/risk.py``, the one list) are never undone automatically, ever.

Storage is SQLite in WAL mode: one file next to the memory database, no dependencies, safe to
read while the system is running.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .risk import is_irreversible

#: Entry states. ``intended``/``running`` are the two an interrupted session can leave behind.
INTENDED = "intended"
RUNNING = "running"
COMMITTED = "committed"
FAILED = "failed"
ABANDONED = "abandoned"
COMPENSATED = "compensated"

OPEN_STATES = (INTENDED, RUNNING)

#: Secret-looking argument names are redacted before an argument is written to disk.
_SECRET_MARKERS = ("key", "token", "password", "secret", "credential", "passphrase")

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS journal (
    entry_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id        TEXT NOT NULL DEFAULT '',
    agent_id       TEXT NOT NULL DEFAULT '',
    action         TEXT NOT NULL,
    capability     TEXT NOT NULL DEFAULT '',
    args_json      TEXT NOT NULL DEFAULT '{}',
    args_hash      TEXT NOT NULL DEFAULT '',
    target         TEXT NOT NULL DEFAULT '',
    risk           INTEGER NOT NULL DEFAULT 1,
    mode           TEXT NOT NULL DEFAULT 'background',
    reversible     INTEGER NOT NULL DEFAULT 0,
    idempotency_key TEXT NOT NULL DEFAULT '',
    state          TEXT NOT NULL,
    outcome_json   TEXT NOT NULL DEFAULT '',
    evidence_json  TEXT NOT NULL DEFAULT '{}',
    error          TEXT NOT NULL DEFAULT '',
    notes          TEXT NOT NULL DEFAULT '',
    started_at     REAL NOT NULL,
    ended_at       REAL
);
CREATE INDEX IF NOT EXISTS journal_task ON journal(task_id, entry_id);
CREATE INDEX IF NOT EXISTS journal_state ON journal(state, entry_id);
CREATE UNIQUE INDEX IF NOT EXISTS journal_key ON journal(idempotency_key) WHERE idempotency_key != '';
"""


def _redact(value: Any, depth: int = 0) -> Any:
    """Copy arguments with secret-looking values masked, so the journal is safe to keep."""
    if depth > 4:
        return "…"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in list(value.items())[:40]:
            if isinstance(key, str) and any(marker in key.lower() for marker in _SECRET_MARKERS):
                out[key] = "***"
            else:
                out[key] = _redact(item, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_redact(item, depth + 1) for item in list(value)[:40]]
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    return repr(value)


#: Public name for the redactor: receipts and reports need the same masking the journal uses,
#: and a second copy of "which argument names are secret" would drift into a leak.
redact = _redact


def arguments_hash(action: str, args: dict[str, Any]) -> str:
    """A stable hash of an action and its arguments: the identity of one intended action."""
    payload = json.dumps({"action": action, "args": _redact(args)}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def risk_of(capability: str, danger: str = "") -> int:
    """The risk level of a call: irreversible capabilities are L3 whatever the tool says."""
    if is_irreversible(capability):
        return 3
    return {"high": 3, "medium": 2, "low": 1, "notice": 0, "none": 0}.get(str(danger or "").lower(), 1)


@dataclass(slots=True)
class Intent:
    """One action the system is about to take. Written to disk before anything happens."""

    action: str
    capability: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    target: str = ""
    task_id: str = ""
    agent_id: str = "system"
    risk: int = 1
    mode: str = "background"
    idempotency_key: str = ""

    @property
    def args_hash(self) -> str:
        return arguments_hash(self.action, self.args)

    @property
    def reversible(self) -> bool:
        return not is_irreversible(self.capability)


@dataclass(slots=True)
class Entry:
    """A journal row: what was intended, what happened, and what can still be undone."""

    entry_id: int
    task_id: str
    agent_id: str
    action: str
    capability: str
    args: dict[str, Any]
    args_hash: str
    target: str
    risk: int
    mode: str
    reversible: bool
    idempotency_key: str
    state: str
    outcome: Any
    evidence: dict[str, Any]
    error: str
    notes: str
    started_at: float
    ended_at: float | None

    def to_json(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "action": self.action,
            "capability": self.capability,
            "target": self.target,
            "risk": self.risk,
            "mode": self.mode,
            "reversible": self.reversible,
            "idempotency_key": self.idempotency_key,
            "state": self.state,
            "outcome": self.outcome,
            "error": self.error,
            "notes": self.notes,
            "started_at": round(self.started_at, 3),
            "ended_at": round(self.ended_at, 3) if self.ended_at else None,
            "evidence": self.evidence,
        }


# --------------------------------------------------------------------------------------
# Compensations and reconciliation probes: registered per tool, never inferred
# --------------------------------------------------------------------------------------

#: action name -> function(entry) -> {"undone": bool, "detail": str, ...}
_COMPENSATIONS: dict[str, Callable[[Entry], dict[str, Any]]] = {}
#: action name -> function(args) -> "committed" | "absent" | "unknown"
_PROBES: dict[str, Callable[[dict[str, Any]], str]] = {}


def register_compensation(action: str, handler: Callable[[Entry], dict[str, Any]]) -> None:
    """Teach the journal how to undo one tool. Without this, undo reports it cannot."""
    _COMPENSATIONS[action] = handler


def register_probe(action: str, handler: Callable[[dict[str, Any]], str]) -> None:
    """Teach the journal how to check whether one tool's effect is really on disk."""
    _PROBES[action] = handler


def compensation_for(action: str) -> Callable[[Entry], dict[str, Any]] | None:
    return _COMPENSATIONS.get(action)


def probe_for(action: str) -> Callable[[dict[str, Any]], str] | None:
    return _PROBES.get(action)


def _path_of(args: dict[str, Any], *names: str) -> str:
    for name in names:
        value = args.get(name)
        if isinstance(value, str) and value:
            return os.path.abspath(os.path.expanduser(value))
    return ""


def _probe_exists(args: dict[str, Any]) -> str:
    """A path the action claims to have written either exists with content or it does not."""
    path = _path_of(args, "path", "dest", "dst", "target")
    if not path:
        return "unknown"
    if not os.path.exists(path):
        return "absent"
    try:
        return "committed" if os.path.getsize(path) > 0 else "unknown"
    except OSError:
        return "unknown"


def _probe_absent(args: dict[str, Any]) -> str:
    """For a delete or a move: the source must be gone for the action to have taken effect."""
    path = _path_of(args, "path", "src", "source")
    if not path:
        return "unknown"
    return "absent" if os.path.exists(path) else "committed"


def _undo_delete_created(entry: Entry) -> dict[str, Any]:
    """Undo a write by deleting what it created, or restoring the backup it left."""
    path = _path_of(entry.args, "path", "dest", "target")
    if not path:
        return {"undone": False, "detail": "the entry does not record a path"}
    backup = path + ".orbynth-bak"
    if os.path.exists(backup):
        try:
            os.replace(backup, path)
            return {"undone": True, "detail": f"restored {path} from its pre-write backup"}
        except OSError as exc:
            return {"undone": False, "detail": f"could not restore the backup: {exc}"}
    if os.path.exists(path):
        try:
            os.remove(path)
            return {"undone": True, "detail": f"removed {path}, which this action created"}
        except OSError as exc:
            return {"undone": False, "detail": f"could not remove {path}: {exc}"}
    return {"undone": True, "detail": f"{path} is already gone"}


def _undo_move(entry: Entry) -> dict[str, Any]:
    src = _path_of(entry.args, "src", "source")
    dst = _path_of(entry.args, "dst", "dest")
    if not (src and dst) or not os.path.exists(dst):
        return {"undone": False, "detail": "the moved file is no longer where the move put it"}
    try:
        os.replace(dst, src)
        return {"undone": True, "detail": f"moved {dst} back to {src}"}
    except OSError as exc:
        return {"undone": False, "detail": f"could not move it back: {exc}"}


def _undo_copy(entry: Entry) -> dict[str, Any]:
    dst = _path_of(entry.args, "dst", "dest")
    if not dst:
        return {"undone": False, "detail": "the entry does not record the copy's destination"}
    if not os.path.exists(dst):
        return {"undone": True, "detail": f"{dst} is already gone"}
    try:
        os.remove(dst)
        return {"undone": True, "detail": f"removed the copy at {dst}"}
    except OSError as exc:
        return {"undone": False, "detail": f"could not remove the copy: {exc}"}


def _undo_delete_honest(entry: Entry) -> dict[str, Any]:
    """A delete is restored from the trash or reported as not undone. Never silently skipped."""
    path = _path_of(entry.args, "path", "src")
    if not path:
        return {"undone": False, "detail": "the entry does not record a path"}
    if os.path.exists(path):
        return {"undone": True, "detail": f"{path} is back in place"}
    return {
        "undone": False,
        "detail": (
            f"{path} was deleted; restore it from the recycle bin / trash. Automated restore needs a "
            "recorded trash location, which this build does not keep."
        ),
    }


register_probe("fs_write", _probe_exists)
register_probe("fs_create", _probe_exists)
register_probe("fs_edit", _probe_exists)
register_probe("fs_copy", _probe_exists)
register_probe("fs_zip", _probe_exists)
register_probe("fs_move", _probe_absent)
register_probe("fs_delete", _probe_absent)

register_compensation("fs_write", _undo_delete_created)
register_compensation("fs_create", _undo_delete_created)
register_compensation("fs_edit", _undo_delete_created)
register_compensation("fs_move", _undo_move)
register_compensation("fs_copy", _undo_copy)
register_compensation("fs_delete", _undo_delete_honest)
register_compensation("fs_zip", _undo_delete_created)


# --------------------------------------------------------------------------------------
# The journal
# --------------------------------------------------------------------------------------


def _decode(value: str, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _row_to_entry(row: sqlite3.Row) -> Entry:
    return Entry(
        entry_id=int(row["entry_id"]),
        task_id=str(row["task_id"]),
        agent_id=str(row["agent_id"]),
        action=str(row["action"]),
        capability=str(row["capability"]),
        args=_decode(row["args_json"], {}),
        args_hash=str(row["args_hash"]),
        target=str(row["target"]),
        risk=int(row["risk"]),
        mode=str(row["mode"]),
        reversible=bool(row["reversible"]),
        idempotency_key=str(row["idempotency_key"]),
        state=str(row["state"]),
        outcome=_decode(row["outcome_json"], None) if row["outcome_json"] else None,
        evidence=_decode(row["evidence_json"], {}),
        error=str(row["error"]),
        notes=str(row["notes"]),
        started_at=float(row["started_at"]),
        ended_at=float(row["ended_at"]) if row["ended_at"] is not None else None,
    )


class Journal:
    """The durable action log. Safe to read and write from the running system."""

    def __init__(self, path: str | os.PathLike[str], bus: Any = None) -> None:
        self.path = Path(path)
        self.bus = bus
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # -- writing -----------------------------------------------------------------
    def begin(self, intent: Intent) -> int:
        """Write the intention to disk *before* the action runs. Returns the entry id.

        A second attempt at the same idempotency key reuses its row rather than inserting a
        duplicate: a retry after a *failure* is legitimate (the action did not take effect),
        while a replay after a *commit* is refused by the caller. Keeping one row per intention
        is also what makes "what happened to this action?" a single lookup.
        """
        with self._lock:
            if intent.idempotency_key:
                existing = self.lookup(intent.idempotency_key)
                if existing is not None:
                    self._conn.execute(
                        "UPDATE journal SET state=?, started_at=?, ended_at=NULL, error='', outcome_json='',"
                        " notes=?, args_json=?, args_hash=?, risk=?, mode=?, reversible=? WHERE entry_id=?",
                        (
                            INTENDED,
                            time.time(),
                            "retry of an earlier attempt at this key",
                            json.dumps(_redact(intent.args), default=str),
                            intent.args_hash,
                            intent.risk,
                            intent.mode,
                            1 if intent.reversible else 0,
                            existing.entry_id,
                        ),
                    )
                    self._conn.commit()
                    return int(existing.entry_id)
            cursor = self._conn.execute(
                "INSERT INTO journal (task_id, agent_id, action, capability, args_json, args_hash,"
                " target, risk, mode, reversible, idempotency_key, state, started_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    intent.task_id,
                    intent.agent_id,
                    intent.action,
                    intent.capability,
                    json.dumps(_redact(intent.args), default=str),
                    intent.args_hash,
                    intent.target,
                    intent.risk,
                    intent.mode,
                    1 if intent.reversible else 0,
                    intent.idempotency_key,
                    INTENDED,
                    time.time(),
                ),
            )
            self._conn.commit()
            return int(cursor.lastrowid or 0)

    def mark_running(self, entry_id: int) -> None:
        """Called immediately before the side effect, so a crash leaves no completed-looking row."""
        with self._lock:
            self._conn.execute("UPDATE journal SET state=? WHERE entry_id=?", (RUNNING, int(entry_id)))
            self._conn.commit()

    def lookup(self, idempotency_key: str) -> Entry | None:
        """The entry already recorded for this key, if the action has been attempted before."""
        if not idempotency_key:
            return None
        row = self._conn.execute(
            "SELECT * FROM journal WHERE idempotency_key=? ORDER BY entry_id DESC LIMIT 1", (idempotency_key,)
        ).fetchone()
        return _row_to_entry(row) if row else None

    def entry(self, entry_id: int) -> Entry | None:
        row = self._conn.execute("SELECT * FROM journal WHERE entry_id=?", (int(entry_id),)).fetchone()
        return _row_to_entry(row) if row else None

    def history(self, task_id: str = "", limit: int = 100) -> list[Entry]:
        if task_id:
            rows = self._conn.execute(
                "SELECT * FROM journal WHERE task_id=? ORDER BY entry_id DESC LIMIT ?", (task_id, limit)
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM journal ORDER BY entry_id DESC LIMIT ?", (limit,)).fetchall()
        return [_row_to_entry(row) for row in rows]

    def open_entries(self) -> list[Entry]:
        placeholders = ",".join("?" for _ in OPEN_STATES)
        rows = self._conn.execute(
            f"SELECT * FROM journal WHERE state IN ({placeholders}) ORDER BY entry_id", list(OPEN_STATES)
        ).fetchall()
        return [_row_to_entry(row) for row in rows]

    def last_task_id(self) -> str:
        row = self._conn.execute(
            "SELECT task_id FROM journal WHERE task_id != '' AND state IN (?,?)"
            " ORDER BY entry_id DESC LIMIT 1",
            (COMMITTED, FAILED),
        ).fetchone()
        return str(row["task_id"]) if row else ""

    def finish(
        self,
        entry_id: int,
        ok: bool,
        outcome: Any = None,
        evidence: dict[str, Any] | None = None,
        error: str = "",
        notes: str = "",
    ) -> None:
        """Record the real outcome. A failure is recorded as a failure, never as an attempt."""
        if not entry_id:
            return
        with self._lock:
            self._conn.execute(
                "UPDATE journal SET state=?, outcome_json=?, evidence_json=?, error=?, notes=?, ended_at=?"
                " WHERE entry_id=?",
                (
                    COMMITTED if ok else FAILED,
                    json.dumps(outcome, default=str) if outcome is not None else "",
                    json.dumps(_redact(evidence or {}), default=str),
                    (error or "")[:1000],
                    notes,
                    time.time(),
                    int(entry_id),
                ),
            )
            self._conn.commit()

    def note(self, entry_id: int, notes: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE journal SET notes=? WHERE entry_id=?", (notes[:500], int(entry_id)))
            self._conn.commit()

    # -- reconciliation ---------------------------------------------------------------
    def reconcile(self, left_open_for: float = 0.0) -> dict[str, Any]:
        """Check every interrupted entry against real state and say what happened to it.

        Three answers are possible and all three are reported: the effect is present (the
        action committed before the interruption), absent (it never happened, so it is
        abandoned), or undeterminable — which is surfaced for a decision rather than guessed
        at. Nothing is auto-undone here: undoing is a deliberate act (``undo_task``).
        """
        checked = 0
        committed: list[dict[str, Any]] = []
        abandoned: list[dict[str, Any]] = []
        unknown: list[dict[str, Any]] = []
        now = time.time()
        for entry in self.open_entries():
            if left_open_for and (now - entry.started_at) < left_open_for:
                unknown.append({**entry.to_json(), "why": "still running in this session"})
                continue
            checked += 1
            probe = probe_for(entry.action)
            verdict = probe(entry.args) if probe else "unknown"
            if verdict == "committed":
                self.finish(entry.entry_id, True, entry.outcome, entry.evidence, "", "reconciled: effect found on disk")
                committed.append(entry.to_json())
            elif verdict == "absent":
                with self._lock:
                    self._conn.execute(
                        "UPDATE journal SET state=?, ended_at=?, notes=? WHERE entry_id=?",
                        (ABANDONED, time.time(), "reconciled: no effect on disk, never happened", entry.entry_id),
                    )
                    self._conn.commit()
                abandoned.append(entry.to_json())
            else:
                reason = (
                    f"no reconciliation probe exists for {entry.action}"
                    if probe is None
                    else f"the effect of {entry.action} cannot be determined from outside"
                )
                unknown.append({**entry.to_json(), "why": reason})
        report = {
            "checked": checked,
            "reconciled_committed": committed,
            "abandoned": abandoned,
            "needs_decision": unknown,
            "summary": (
                f"{len(committed)} interrupted action(s) had already taken effect, {len(abandoned)} never ran, "
                f"{len(unknown)} need a decision"
            ),
        }
        if self.bus is not None and checked:
            try:
                from ..events import JOURNAL_RECONCILED

                self.bus.emit(
                    JOURNAL_RECONCILED,
                    source="journal",
                    severity="notice" if unknown else "info",
                    checked=checked,
                    committed=len(committed),
                    abandoned=len(abandoned),
                    needs_decision=len(unknown),
                )
            except Exception:
                pass
        return report

    # -- undo ---------------------------------------------------------------------------
    def undo_task(self, task_id: str, allow_irreversible: bool = False, limit: int = 200) -> dict[str, Any]:
        """Reverse a finished task, newest action first, and say exactly what could not be undone.

        Irreversible capabilities are refused unless the caller passes ``allow_irreversible``
        (which the confirmation layer only does after the owner approves), and a tool with no
        registered compensation is reported as not undone rather than quietly counted as a
        success. "Undone" always means the compensating action itself reported a real effect.
        """
        entries = [entry for entry in self.history(task_id, limit) if entry.state == COMMITTED]
        undone: list[dict[str, Any]] = []
        refused: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        for entry in reversed(entries):
            if is_irreversible(entry.capability) and not allow_irreversible:
                refused.append({**entry.to_json(), "reason": "irreversible: never undone automatically"})
                continue
            compensator = compensation_for(entry.action)
            if compensator is None:
                failed.append({**entry.to_json(), "reason": f"no undo is implemented for {entry.action}"})
                continue
            try:
                outcome = compensator(entry)
            except Exception as exc:
                outcome = {"undone": False, "detail": f"undo raised {type(exc).__name__}: {exc}"}
            record = {**entry.to_json(), "undo": outcome}
            if outcome.get("undone"):
                with self._lock:
                    self._conn.execute(
                        "UPDATE journal SET state=?, notes=?, ended_at=? WHERE entry_id=?",
                        (COMPENSATED, str(outcome.get("detail", ""))[:400], time.time(), entry.entry_id),
                    )
                    self._conn.commit()
                undone.append(record)
            else:
                with self._lock:
                    self._conn.execute(
                        "UPDATE journal SET notes=? WHERE entry_id=?",
                        (f"undo failed: {outcome.get('detail', '')}"[:400], entry.entry_id),
                    )
                    self._conn.commit()
                failed.append(record)
        report = {
            "task_id": task_id,
            "attempted": len(entries),
            "undone": undone,
            "refused": refused,
            "failed": failed,
            "summary": (
                f"{len(undone)} action(s) reversed, {len(failed)} could not be, {len(refused)} refused as irreversible"
            ),
        }
        if self.bus is not None and entries:
            try:
                from ..events import JOURNAL_UNDONE

                self.bus.emit(
                    JOURNAL_UNDONE,
                    source="journal",
                    severity="warning" if (failed or refused) else "info",
                    task_id=task_id,
                    undone=len(undone),
                    failed=len(failed),
                    refused=len(refused),
                )
            except Exception:
                pass
        return report

    def plan_undo(self, task_id: str = "", limit: int = 200) -> dict[str, Any]:
        """What an undo *would* do, without doing any of it.

        An undo that surprises the user is worse than no undo, so the plan is a first-class
        answer: which actions can be reversed, which are irreversible, and which have no
        compensating action implemented at all.
        """
        resolved = task_id or self.last_task_id()
        entries = [entry for entry in self.history(resolved, limit) if entry.state == COMMITTED] if resolved else []
        reversible: list[dict[str, Any]] = []
        refused: list[dict[str, Any]] = []
        impossible: list[dict[str, Any]] = []
        for entry in entries:
            row = {"entry_id": entry.entry_id, "action": entry.action, "target": entry.target, "risk": entry.risk}
            if is_irreversible(entry.capability):
                refused.append({**row, "why": "irreversible: this is never undone automatically"})
            elif compensation_for(entry.action) is None:
                impossible.append({**row, "why": f"no undo is implemented for {entry.action}"})
            else:
                reversible.append(row)
        return {
            "task_id": resolved,
            "planned": len(entries),
            "reversible": reversible,
            "refused": refused,
            "impossible": impossible,
            "summary": (
                f"{len(reversible)} of {len(entries)} action(s) can be reversed; "
                f"{len(refused)} refused as irreversible, {len(impossible)} have no undo"
            ),
        }

    def undo_last_task(self, allow_irreversible: bool = False) -> dict[str, Any]:
        """The one-command undo the specification asks for.

        Picks the most recent task that actually changed something and reverses it. With no
        such task the answer is an honest empty report, not a success.
        """
        task_id = self.last_task_id()
        if not task_id:
            return {"task_id": "", "attempted": 0, "undone": [], "refused": [], "failed": [], "summary": "nothing to undo: no action has been journaled yet"}
        return self.undo_task(task_id, allow_irreversible=allow_irreversible)

    # -- reporting ------------------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        rows = self._conn.execute("SELECT state, COUNT(*) AS n FROM journal GROUP BY state").fetchall()
        counts = {str(row["state"]): int(row["n"]) for row in rows}
        reversible = self._conn.execute("SELECT COUNT(*) AS n FROM journal WHERE reversible=1").fetchone()
        latest = self._conn.execute("SELECT MAX(ended_at) AS t FROM journal").fetchone()
        return {
            "path": str(self.path),
            "entries": sum(counts.values()),
            "by_state": counts,
            "reversible": int(reversible["n"]) if reversible else 0,
            "open": sum(counts.get(state, 0) for state in OPEN_STATES),
            "last_activity": float(latest["t"]) if latest and latest["t"] else None,
        }

    def action_stats(self) -> dict[str, dict[str, Any]]:
        """Real per-action history for the Capability Matrix (spec 4.7).

        Counts come from this log, never from a fixture: ``runs`` is every journalled
        attempt, ``committed`` those that reached their outcome, and ``last`` the most recent
        activity. Open and failed entries are reported too, because a tool that only ever
        fails must not look healthy in the matrix.
        """
        rows = self._conn.execute(
            "SELECT action,"
            " COUNT(*) AS runs,"
            " SUM(CASE WHEN state='committed' THEN 1 ELSE 0 END) AS committed,"
            " SUM(CASE WHEN state IN ('failed','rejected','abandoned') THEN 1 ELSE 0 END) AS failed,"
            " SUM(CASE WHEN ended_at IS NULL THEN 1 ELSE 0 END) AS open,"
            " MAX(COALESCE(ended_at, started_at)) AS last"
            " FROM journal GROUP BY action"
        ).fetchall()
        return {
            str(row["action"]): {
                "runs": int(row["runs"] or 0),
                "committed": int(row["committed"] or 0),
                "failed": int(row["failed"] or 0),
                "open": int(row["open"] or 0),
                "last": float(row["last"]) if row["last"] else None,
            }
            for row in rows
        }

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.commit()
            finally:
                self._conn.close()


def intent_for(action: str, capability: str, args: dict[str, Any], ctx: Any, danger: str = "") -> Intent:
    """Build an :class:`Intent` from a tool call, deriving the target the way the receipt does."""
    target = ""
    for name in ("path", "url", "tab_id", "chat", "title_contains", "target", "cell", "query", "session"):
        value = args.get(name)
        if isinstance(value, str) and value:
            target = value
            break
    return Intent(
        action=action,
        capability=capability,
        args=args,
        target=target,
        task_id=str(getattr(ctx, "task_id", "") or ""),
        agent_id=str(getattr(ctx, "agent_id", "system") or "system"),
        risk=risk_of(capability, danger),
        mode=str(getattr(ctx, "mode", "background") or "background"),
        idempotency_key=str(getattr(ctx, "idempotency_key", "") or ""),
    )
