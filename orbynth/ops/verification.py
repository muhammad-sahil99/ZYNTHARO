"""Outcome verification and risk-tiered effort (contract 22.4, 22.8).

Two separate problems live here.

**Does the work match the request?** Receipts prove each action ran; they cannot prove the
*goal* was reached. So a task carries a contract written *before* it starts: the user's own
words plus success criteria. When the agent says it is finished, the verifier checks the
finished state against those criteria, reports which ones are unmet, and flags unintended
side effects (actions taken that the request never implied). A subjective criterion is never
guessed at: it goes to the user as a question.

**How much verification does an action deserve?** Verifying every keystroke would make the
product slow, which is the opposite of the requirement. Effort therefore scales with risk:

===============  ==========================  =====================================
tier             policy                      example
===============  ==========================  =====================================
harmless         batched, verified in bulk   scroll, read a file, list windows
low              batched, immediate on error screenshot, tab switch, focus window
moderate         immediate, optimistic ok    type into a form, save a document
irreversible     immediate, never shortcut   delete, send, call, shell, install
===============  ==========================  =====================================

"Optimistic" means the next action may start before this one is confirmed, but only for
actions that declared themselves reversible; if verification then fails, the rollback path
runs. Irreversible actions are never batched and never started early.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Mapping, Sequence

from ..events import EventBus, TASK_DISPUTED, TASK_VERIFIED
from .risk import IRREVERSIBLE_CAPABILITIES

HARMLESS = "harmless"
LOW = "low"
MODERATE = "moderate"
IRREVERSIBLE = "irreversible"

TIER_ORDER = (HARMLESS, LOW, MODERATE, IRREVERSIBLE)

BATCHED = "batched"
IMMEDIATE = "immediate"
NEVER_SHORTCUT = "never_shortcut"

#: Capabilities whose effects cannot be undone by ORBYNTH, whatever the action claims.
#: Imported from ``ops/risk.py``: the same list the owner gate demands a PIN for, so the
#: verification tier and the authorisation gate can never disagree about what is dangerous.

#: Actions that are pure observation: cheap to batch and never worth waiting on.
OBSERVATION_GROUPS: frozenset[str] = frozenset({"vision", "windows", "memory", "orchestration", "hud"})


@dataclass
class TierPolicy:
    tier: str
    verify: str
    batch_window: float = 0.0
    optimistic: bool = False
    verify_on_error_only: bool = False

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


DEFAULT_POLICIES: dict[str, TierPolicy] = {
    HARMLESS: TierPolicy(HARMLESS, BATCHED, batch_window=5.0, verify_on_error_only=True),
    LOW: TierPolicy(LOW, BATCHED, batch_window=2.0, verify_on_error_only=True),
    MODERATE: TierPolicy(MODERATE, IMMEDIATE, optimistic=True),
    IRREVERSIBLE: TierPolicy(IRREVERSIBLE, NEVER_SHORTCUT),
}


class RiskTiers:
    """Classifies actions and hands out the verification policy for each."""

    def __init__(self, policies: Mapping[str, TierPolicy] | None = None):
        self.policies = dict(policies or DEFAULT_POLICIES)
        #: action name -> reversible. Set by drivers that support undo.
        self.reversible: dict[str, bool] = {}

    def declare_reversible(self, action_name: str, reversible: bool = True) -> None:
        self.reversible[action_name] = reversible

    def tier_for(self, action_name: str, capability: str = "", danger: str = "low", group: str = "") -> str:
        if capability in IRREVERSIBLE_CAPABILITIES or danger in {"high", "critical"}:
            return IRREVERSIBLE
        if danger == "medium":
            return MODERATE
        if group in OBSERVATION_GROUPS and danger == "low":
            return HARMLESS
        return LOW

    def policy_for(self, tier: str) -> TierPolicy:
        return self.policies.get(tier, DEFAULT_POLICIES[LOW])

    def plan(self, actions: Sequence[Any]) -> "VerificationPlan":
        """Group a batch of actions into what is verified now and what is verified in bulk."""
        immediate: list[str] = []
        batched: list[str] = []
        optimistic: list[str] = []
        never_shortcut: list[str] = []
        for act in actions:
            name = getattr(act, "name", str(act))
            capability = getattr(act, "capability", "")
            danger = getattr(act, "danger", "low")
            group = getattr(act, "group", "")
            tier = self.tier_for(name, capability, danger, group)
            policy = self.policy_for(tier)
            if policy.verify == NEVER_SHORTCUT:
                never_shortcut.append(name)
                immediate.append(name)
            elif policy.optimistic and self.reversible.get(name, False):
                optimistic.append(name)
                immediate.append(name)
            elif policy.verify == IMMEDIATE:
                immediate.append(name)
            else:
                batched.append(name)
        return VerificationPlan(immediate=immediate, batched=batched, optimistic=optimistic, never_shortcut=never_shortcut)


@dataclass
class VerificationPlan:
    immediate: list[str] = field(default_factory=list)
    batched: list[str] = field(default_factory=list)
    optimistic: list[str] = field(default_factory=list)
    never_shortcut: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "immediate": self.immediate,
            "batched": self.batched,
            "optimistic": self.optimistic,
            "never_shortcut": self.never_shortcut,
            "summary": (
                f"{len(self.immediate)} verified immediately, {len(self.batched)} batched, "
                f"{len(self.optimistic)} optimistic (rolled back on failure), "
                f"{len(self.never_shortcut)} never shortcut"
            ),
        }


# --------------------------------------------------------------------------------------
# Outcome verification: did the finished work match the request?
# --------------------------------------------------------------------------------------

MET = "met"
NOT_MET = "not_met"
UNCERTAIN = "uncertain"
NEEDS_USER = "needs_user"

FILE_EXISTS = "file_exists"
TEXT_PRESENT = "text_present"
URL_IS = "url_is"
VALUE_EQUALS = "value_equals"
COUNT_AT_LEAST = "count_at_least"
CUSTOM = "custom"
SUBJECTIVE = "subjective"

DETERMINISTIC_KINDS = {FILE_EXISTS, TEXT_PRESENT, URL_IS, VALUE_EQUALS, COUNT_AT_LEAST, CUSTOM}


@dataclass
class SuccessCriterion:
    """One checkable statement about the finished state.

    ``SUBJECTIVE`` is a first-class kind, not a failure mode: "the document reads well" is a
    real criterion and belongs to the user, not to a regex.
    """

    kind: str
    target: str = ""
    expected: Any = None
    note: str = ""
    weight: float = 1.0

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "SuccessCriterion":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class TaskContract:
    """Written before the task starts: the request in the user's words, plus criteria."""

    task_id: str
    request: str
    criteria: list[SuccessCriterion] = field(default_factory=list)
    intended_capabilities: tuple[str, ...] = ()
    requested_by: str = "owner"
    created_at: float = field(default_factory=time.time)

    def add(self, kind: str, target: str = "", expected: Any = None, note: str = "") -> "TaskContract":
        self.criteria.append(SuccessCriterion(kind, target, expected, note))
        return self

    def to_json(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "request": self.request,
            "criteria": [c.to_json() for c in self.criteria],
            "intended_capabilities": list(self.intended_capabilities),
            "requested_by": self.requested_by,
            "created_at": self.created_at,
        }


#: Phrases in a request that imply the capabilities it obviously expects to touch.
#: Deliberately narrow: an over-broad inference would mark every capability "intended" and
#: make the side-effect check meaningless, which is worse than not checking at all.
REQUEST_CAPABILITY_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("delete", "remove", "erase", "uninstall", "trash", "clean up"), "filesystem.delete"),
    (("write", "create", "save", "export", "rename", "zip", "extract", "edit", "append"), "filesystem.write"),
    (("read", "open", "inspect", "list", "summarise", "summarize", "check", "verify", "find"), "filesystem.read"),
    (("type", "click", "press", "keyboard", "mouse", "focus", "scroll"), "input.control"),
    (("browse", "tab", "website", "web page", "url", "portal", "form", "log in", "login"), "browser.*"),
    (("word", "excel", "spreadsheet", "office", "powerpoint", "presentation", "access"), "office.*"),
    (("whatsapp", "message", "chat", "voice note", "call"), "whatsapp.*"),
    (("email", "gmail", "mail", "http", "api"), "network.http"),
    (("shell", "command line", "terminal", "script"), "shell.exec"),
)


def capabilities_from_request(text: str) -> tuple[str, ...]:
    """What a request in plain words expects to touch, for the side-effect check.

    Without this a contract built from a user objective has no intended capabilities at all,
    and ``detect_side_effects`` can judge nothing -- every destructive extra would pass.
    """
    lowered = text.lower()
    found: list[str] = []
    for words, capability in REQUEST_CAPABILITY_HINTS:
        if any(word in lowered for word in words):
            found.append(capability)
    return tuple(found)


def contract_for_request(task_id: str, request: str, expected_outputs: Sequence[str] = ()) -> TaskContract:
    """Write the contract before the work starts. Criteria may legitimately be empty.

    An empty criterion list is not a failure: the verifier reports ``uncertain`` rather than
    certifying the work, criteria derived from what the task actually did are added later, and
    anything only a human can judge is asked as a question.
    """
    contract = TaskContract(task_id=task_id, request=request, intended_capabilities=capabilities_from_request(request))
    for output in expected_outputs:
        contract.add(FILE_EXISTS, target=str(output), note="output the request promised")
    return contract


@dataclass
class CriterionResult:
    criterion: SuccessCriterion
    status: str
    evidence: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"criterion": self.criterion.to_json(), "status": self.status, "evidence": self.evidence[:500]}


@dataclass
class Verdict:
    task_id: str
    status: str  # met | not_met | uncertain | needs_user
    results: list[CriterionResult] = field(default_factory=list)
    side_effects: list[dict[str, Any]] = field(default_factory=list)
    question: str = ""
    summary: str = ""
    checked_at: float = field(default_factory=time.time)

    def to_json(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "results": [r.to_json() for r in self.results],
            "side_effects": self.side_effects,
            "question": self.question,
            "summary": self.summary,
            "checked_at": self.checked_at,
        }


class OutcomeVerifier:
    """Checks a finished task against the contract it was given.

    Deterministic checks run against a ``probes`` mapping supplied by the caller (in tests,
    by the caller's own fakes; in production, by the real drivers). Subjective criteria are
    routed to the user. A model may be consulted for natural-language criteria, but only as
    an explanation of an already-decided outcome, never as the sole authority for a claim
    that work is complete.
    """

    def __init__(self, bus: EventBus | None = None, probes: Mapping[str, Callable[..., Any]] | None = None):
        self.bus = bus or EventBus()
        self.probes = dict(probes or {})

    def register_probe(self, kind: str, fn: Callable[..., Any]) -> None:
        self.probes[kind] = fn

    def _run_criterion(self, criterion: SuccessCriterion, observations: Mapping[str, Any]) -> CriterionResult:
        if criterion.kind == SUBJECTIVE:
            return CriterionResult(criterion, NEEDS_USER, criterion.note or "the user decides this one")
        probe = self.probes.get(criterion.kind) or observations.get(criterion.kind)
        if probe is None:
            return CriterionResult(criterion, UNCERTAIN, f"no probe registered for {criterion.kind!r}")
        try:
            raw = probe(criterion, observations) if callable(probe) else probe
        except Exception as exc:
            return CriterionResult(criterion, NOT_MET, f"probe raised: {exc!r}")
        if isinstance(raw, tuple):
            ok, evidence = bool(raw[0]), str(raw[1] if len(raw) > 1 else "")
        elif isinstance(raw, bool):
            ok, evidence = raw, ""
        else:
            ok, evidence = bool(raw), str(raw)
        return CriterionResult(criterion, MET if ok else NOT_MET, evidence)

    def check(
        self,
        contract: TaskContract,
        observations: Mapping[str, Any] | None = None,
        performed: Sequence[Mapping[str, Any]] = (),
        claim: str = "",
    ) -> Verdict:
        """Compare the finished state with the request. ``performed`` is the audit trail."""
        observations = observations or {}
        results = [self._run_criterion(c, observations) for c in contract.criteria]
        side_effects = self.detect_side_effects(contract, performed)

        failed = [r for r in results if r.status == NOT_MET]
        unsure = [r for r in results if r.status == UNCERTAIN]
        subjective = [r for r in results if r.status == NEEDS_USER]

        if not results:
            # A contract with nothing to check cannot be "met". The first version fell through
            # to MET here, so a task with no success criteria was silently certified as
            # complete -- the exact hole this module exists to close.
            status = UNCERTAIN
        elif failed:
            status = NOT_MET
        elif subjective:
            status = NEEDS_USER
        elif unsure:
            status = UNCERTAIN
        else:
            status = MET
        if side_effects and status == MET:
            status = NEEDS_USER

        question = ""
        if status == NEEDS_USER and subjective:
            questions = [c.criterion.note or c.criterion.target for c in subjective]
            question = "Please confirm the parts only you can judge: " + "; ".join(questions[:5])
        elif status == NEEDS_USER and side_effects:
            question = "Side effects were detected that the request did not mention: " + "; ".join(
                s["action"] for s in side_effects[:5]
            )

        unmet = ", ".join(c.criterion.target or c.criterion.kind for c in failed) or "none"
        if results:
            criteria_line = f"{len(results) - len(failed)}/{len(results)} criteria met (unmet: {unmet})"
        else:
            criteria_line = "no checkable success criteria were written for this task"
        # "0 side effects" must not read as "we looked and found none" when the contract
        # declared no capabilities at all: then nothing can be judged, and saying so is the
        # difference between an honest verdict and a reassuring one.
        if contract.intended_capabilities:
            side_line = f"{len(side_effects)} unintended side effect(s)"
        else:
            side_line = f"side effects not judged ({len(side_effects)} recorded: the contract declared no capabilities)"
        summary = f"{criteria_line}; {side_line}; claim was: {claim[:200] or 'not stated'}"
        verdict = Verdict(contract.task_id, status, results, side_effects, question, summary)
        self.bus.emit(
            TASK_VERIFIED if status == MET else TASK_DISPUTED,
            source="verifier",
            severity="info" if status == MET else "warning",
            task_id=contract.task_id,
            status=status,
            side_effects=len(side_effects),
            summary=summary,
        )
        return verdict

    @staticmethod
    def detect_side_effects(contract: TaskContract, performed: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """Find work that was done but never asked for, weighted by how destructive it was.

        A ``capability``-level comparison, so a task that legitimately needed
        ``filesystem.write`` is not flagged for writing, while a task that only asked to read
        something and then deleted a file certainly is.
        """
        intended = set(contract.intended_capabilities)
        if not intended:
            return []
        out: list[dict[str, Any]] = []
        for record in performed:
            capability = str(record.get("capability") or "")
            action = str(record.get("action") or "")
            if not capability or capability in intended:
                continue
            out.append(
                {
                    "action": action,
                    "capability": capability,
                    "outcome": record.get("outcome", ""),
                    "severity": "high" if capability in IRREVERSIBLE_CAPABILITIES else "medium",
                }
            )
        return out
