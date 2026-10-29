"""Evidence receipts (spec 4.2): the host's record of what an action actually did.

A receipt exists because a return value is not proof. ``ok: true`` from a driver means "no
exception was raised", which is exactly what a click that landed on nothing also returns. So
every executed tool call produces one of these, with three things the old result object did
not carry:

* **the observation channel** that confirmed it — a control read back, a DOM state, a
  re-hashed file, a reparsed document, a shell marker, an OS window query. A return code
  alone is never a channel, which is why ``exit_code`` is deliberately absent from the
  table below.
* **the ladder rung** (spec 3.5) the action used, so the cheap paths are visible and the
  expensive ones are justified rather than accidental.
* **the honest outcome**: ``VERIFIED``, ``UNVERIFIED`` or ``FAILED``. ``UNVERIFIED`` is a
  first-class result, never upgraded to success by anybody downstream.

Receipts are built here, in the host, never by a plugin or a driver: a component that can
write its own proof is not being verified. Drivers only supply evidence; this module decides
what that evidence is worth.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping

from .journal import redact

VERIFIED = "VERIFIED"
UNVERIFIED = "UNVERIFIED"
FAILED = "FAILED"
OUTCOMES = (VERIFIED, UNVERIFIED, FAILED)

#: The tool-selection ladder (spec 3.5). Lower is cheaper and more reliable.
RUNG_NAMES = {
    1: "api",
    2: "accessibility",
    3: "structured-ui",
    4: "vision",
    5: "coordinates",
}

#: Evidence keys that constitute an observation independent of the action that was taken.
#: The phrase is the human-readable channel recorded on the receipt. ``exit_code``, ``ok``
#: and ``returncode`` are intentionally missing: a program reporting that it ran is the
#: action talking about itself.
CHANNELS: dict[str, str] = {
    "readback": "control read-back",
    "read_back": "control read-back",
    "dom": "DOM state",
    "dom_state": "DOM state",
    "page_state": "page state",
    "network": "network response",
    "response_status": "network response",
    "hash": "content hash",
    "sha256": "content hash",
    "file_hash": "content hash",
    "reparsed": "reparsed document",
    "reopened": "reparsed document",
    "shell_marker": "shell marker",
    "marker": "shell marker",
    "os_query": "OS window query",
    "window_state": "OS window query",
    "foreground": "OS foreground check",
    "chat_seen": "message seen in the chat",
    "sent_seen": "message seen in the chat",
    "element_state": "element state change",
    "state_change": "element state change",
    "ocr_after": "OCR of the new frame",
    "screenshot_diff": "frame difference",
    "before_after": "before/after state",
}

#: Keys the receipt lifts verbatim into ``state_before`` / ``state_after`` when a driver
#: supplies them. Nested under ``before``/``after`` is preferred; the flat names are accepted
#: because several drivers already emit them.
_BEFORE_KEYS = ("before", "state_before")
_AFTER_KEYS = ("after", "state_after", "observed")


def _digest(value: Any) -> str:
    try:
        blob = json.dumps(value, sort_keys=True, default=str)
    except (TypeError, ValueError):
        blob = repr(value)
    return hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()[:16]


def channels_in(evidence: Mapping[str, Any]) -> list[str]:
    """The independent observation channels present in a set of evidence."""
    found: list[str] = []
    for key, label in CHANNELS.items():
        value = evidence.get(key)
        if value not in (None, "", False, 0, [], {}):
            if label not in found:
                found.append(label)
    return found


def infer_rung(group: str, evidence: Mapping[str, Any]) -> int:
    """Which rung of the ladder an action used (spec 3.5).

    A driver that knows better says so with ``rung``; otherwise the group decides, because
    the groups are honest about what they are: the input module is coordinate injection, the
    vision module is pixels, and everything else is an API.
    """
    explicit = evidence.get("rung")
    if isinstance(explicit, int) and explicit in RUNG_NAMES:
        return explicit
    if group == "input":
        return 5
    if group == "vision":
        return 4
    if group == "windows":
        method = str(evidence.get("method", "")).lower()
        return 2 if "invoke" in method or "uia" in method else 3
    return 1


def state_snapshot(evidence: Mapping[str, Any], which: str) -> dict[str, Any]:
    keys = _BEFORE_KEYS if which == "before" else _AFTER_KEYS
    for key in keys:
        value = evidence.get(key)
        if isinstance(value, dict):
            return {str(k): value[k] for k in list(value)[:20]}
    return {}


@dataclass(slots=True)
class Receipt:
    """One executed tool call, with its evidence and the honest verdict on that evidence."""

    receipt_id: str
    tool: str
    agent_id: str
    capability: str
    group: str
    risk_tier: str
    args: dict[str, Any] = field(default_factory=dict)
    target: str = ""
    rung: int = 1
    method: str = ""
    channels: list[str] = field(default_factory=list)
    started_at: float = 0.0
    ended_at: float = 0.0
    duration: float = 0.0
    state_before: dict[str, Any] = field(default_factory=dict)
    state_after: dict[str, Any] = field(default_factory=dict)
    outcome: str = UNVERIFIED
    verified: bool = False
    evidence: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    output_digest: str = ""
    task_id: str = ""
    idempotency_key: str = ""
    notes: str = ""

    # -- construction ------------------------------------------------------------------
    @classmethod
    def build(
        cls,
        *,
        tool: str,
        act: Any,
        ctx: Any,
        args: Mapping[str, Any],
        result: Any,
        started_at: float,
        risk_tier: str = "",
        task_id: str = "",
        idempotency_key: str = "",
    ) -> "Receipt":
        evidence = dict(getattr(result, "evidence", {}) or {})
        channels = channels_in(evidence)
        rung = infer_rung(str(getattr(act, "group", "")), evidence)
        ok = bool(getattr(result, "ok", False))
        if not ok:
            outcome = FAILED
        elif channels and bool(getattr(result, "verified", False)):
            outcome = VERIFIED
        else:
            outcome = UNVERIFIED
        target = str(
            evidence.get("target")
            or evidence.get("path")
            or evidence.get("url")
            or evidence.get("window")
            or ""
        )
        return cls(
            receipt_id=uuid.uuid4().hex[:16],
            tool=tool,
            agent_id=str(getattr(ctx, "agent_id", "system")),
            capability=str(getattr(act, "capability", "")),
            group=str(getattr(act, "group", "")),
            risk_tier=risk_tier,
            args=redact(dict(args)),
            target=target[:400],
            rung=rung,
            method=", ".join(channels),
            channels=channels,
            started_at=round(started_at, 3),
            ended_at=round(time.time(), 3),
            duration=round(float(getattr(result, "duration", 0.0) or 0.0), 3),
            state_before=state_snapshot(evidence, "before"),
            state_after=state_snapshot(evidence, "after"),
            outcome=outcome,
            verified=outcome == VERIFIED,
            # Masked the same way the journal masks arguments: a receipt is kept and shown,
            # so it must not become the place secrets leak back in.
            evidence=redact(evidence),
            error=str(getattr(result, "error", "") or "")[:1000],
            output_digest=_digest(getattr(result, "output", None)),
            task_id=task_id or str(getattr(ctx, "task_id", "") or ""),
            idempotency_key=idempotency_key or str(getattr(ctx, "idempotency_key", "") or ""),
        )

    @property
    def rung_name(self) -> str:
        return RUNG_NAMES.get(self.rung, f"rung-{self.rung}")

    @property
    def is_proof(self) -> bool:
        return self.outcome == VERIFIED

    def to_json(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "tool": self.tool,
            "agent_id": self.agent_id,
            "capability": self.capability,
            "group": self.group,
            "risk_tier": self.risk_tier,
            "args": self.args,
            "target": self.target,
            "rung": self.rung,
            "rung_name": self.rung_name,
            "method": self.method,
            "channels": self.channels,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration": self.duration,
            "state_before": self.state_before,
            "state_after": self.state_after,
            "outcome": self.outcome,
            "verified": self.verified,
            "error": self.error,
            "output_digest": self.output_digest,
            "task_id": self.task_id,
            "idempotency_key": self.idempotency_key,
            "notes": self.notes,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "Receipt":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})

    def citation(self) -> str:
        """How a report should cite this receipt (4.4: a claim without one is rejected)."""
        return f"{self.receipt_id}:{self.tool}:{self.outcome}"


def summarise(receipts: list[Receipt]) -> dict[str, Any]:
    """Counts a report can quote without upgrading anything to a success."""
    counts = {outcome: 0 for outcome in OUTCOMES}
    rungs: dict[str, int] = {}
    for receipt in receipts:
        counts[receipt.outcome] = counts.get(receipt.outcome, 0) + 1
        rungs[receipt.rung_name] = rungs.get(receipt.rung_name, 0) + 1
    return {
        "total": len(receipts),
        "by_outcome": counts,
        "by_rung": rungs,
        "proven": counts.get(VERIFIED, 0),
        "unproven": counts.get(UNVERIFIED, 0),
        "failed": counts.get(FAILED, 0),
    }