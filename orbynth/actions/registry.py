"""Action registry: the only door through which anything touches the computer.

Every mouse move, keystroke, file write, browser click and Office call goes through
:meth:`ActionRegistry.invoke`. That gives the system four properties it cannot have
otherwise:

1. **Capability enforcement.** The caller's granted capabilities are checked before any
   side effect. An Operative without ``filesystem.delete`` cannot delete anything, no
   matter what a model asks for.
2. **Confirmation gating.** Confirm-level capabilities (delete, macro, eval, shell, call,
   plugin install) require an explicit grant — from the user, or from the Warden when the
   user has pre-authorised a class of work.
3. **Audit.** Append-only record of who did what, with redacted arguments and evidence.
4. **Observability.** ``action.invoked`` / ``action.result`` / ``action.denied`` events
   drive the HUD's shear effects and the live wire activity.

Actions are declared with a JSON-Schema parameter block, so the same registry produces the
tool definitions handed to the model. One declaration, three consumers: the model's tool
list, the validator, and the documentation.
"""

from __future__ import annotations

import inspect
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from ..config import AppConfig
from ..events import (
    ACTION_CONFIRM,
    ACTION_DENIED,
    ACTION_INVOKED,
    ACTION_RESULT,
    EventBus,
)
from ..llm.providers import ToolSpec
from ..ops.journal import COMMITTED, intent_for
from ..ops.receipt import Receipt
from ..ops.verification import RiskTiers
from ..ops.owner import IRREVERSIBLE_CAPABILITIES as OWNER_IRREVERSIBLE


class CapabilityError(PermissionError):
    """The caller does not hold the capability the action requires."""


class ConfirmationRequired(PermissionError):
    """The action is destructive and no confirmation was supplied."""

    def __init__(self, action: str, capability: str, detail: str):
        super().__init__(f"{action} requires confirmation ({capability}): {detail}")
        self.action = action
        self.capability = capability
        self.detail = detail


class ActionFailed(RuntimeError):
    """The action ran and failed; carries structured evidence for the agent loop."""

    def __init__(self, message: str, evidence: dict[str, Any] | None = None):
        super().__init__(message)
        self.evidence = evidence or {}


class DriverUnavailable(ActionFailed):
    """The native dependency for this action is not installed on this machine."""

    def __init__(self, driver: str, needs: str, hint: str = ""):
        message = (
            f"driver {driver!r} is not available on this machine: needs {needs}. "
            f"{hint or 'Install the desktop extra: pip install \"orbynth[desktop]\"'} "
            "(ORBYNTH runs headless here, so the core still works without it.)"
        )
        super().__init__(message, {"driver": driver, "needs": needs, "unavailable": True})


@dataclass(slots=True)
class ActionResult:
    ok: bool
    output: Any = None
    error: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    #: "text" means the result already carries everything vision would have shown.
    detail: str = "text"
    duration: float = 0.0
    verified: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "output": self.output if _jsonable(self.output) else repr(self.output),
            "error": self.error,
            "evidence": self.evidence,
            "detail": self.detail,
            "duration": round(self.duration, 3),
            "verified": self.verified,
        }

    @classmethod
    def success(cls, output: Any = None, evidence: dict[str, Any] | None = None, detail: str = "text") -> "ActionResult":
        return cls(ok=True, output=output, evidence=evidence or {}, detail=detail, verified=True)

    @classmethod
    def failure(cls, error: str, evidence: dict[str, Any] | None = None) -> "ActionResult":
        return cls(ok=False, error=error, evidence=evidence or {})


def _jsonable(value: Any) -> bool:
    return isinstance(value, (str, int, float, bool, type(None), list, dict))


@dataclass(slots=True)
class ActionContext:
    """Everything an action may need, injected — actions never reach for globals."""

    agent_id: str = "system"
    capabilities: frozenset[str] = frozenset()
    bus: EventBus = field(default_factory=EventBus)
    memory: Any = None
    config: AppConfig | None = None
    confirmed: tuple[str, ...] = ()  # action names or capabilities the caller pre-approved
    #: When set, irreversible capabilities must also pass the owner gate (contract 22.7).
    #: Off by default so headless/test contexts behave exactly as before.
    require_owner: bool = False
    #: Empty means "use however the owner's current session was opened" -- the registry asks
    #: the owner registry rather than trusting a claimed authentication method.
    owner_method: str = ""
    identity: Any = None
    dry_run: bool = False
    #: The task this action belongs to. The journal groups entries by it, which is what makes
    #: "undo last task" one command instead of a guess (4.5).
    task_id: str = ""
    #: Set when an action must run *at most once*, even across a crash, a retry or a model
    #: failover. Empty means repeatable: two clicks on the same button are a legitimate human
    #: intention, not a duplication.
    idempotency_key: str = ""
    #: Execution mode for this action (10.1): background | isolated | direct.
    mode: str = "background"
    #: Set by the registry on every invoke so plugin/sub-action code can reach the tool
    #: surface while remaining inside the same capability gate.
    registry: Any = None

    def can(self, capability: str) -> bool:
        return capability in self.capabilities


@dataclass(slots=True)
class Action:
    """A single capability-gated operation."""

    name: str
    description: str
    capability: str
    handler: Callable[..., Any]
    parameters: dict[str, Any] = field(default_factory=dict)
    requires_confirm: bool = False
    danger: str = "low"  # low | medium | high
    group: str = "general"
    returns: str = "text"

    def invoke(self, ctx: ActionContext, **kwargs: Any) -> Any:
        return self.handler(ctx, **kwargs)

    def tool_spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=self.description,
            parameters=self.parameters or {"type": "object", "properties": {}},
            capability=self.capability,
            requires_confirm=self.requires_confirm,
        )


def action(
    name: str,
    description: str,
    capability: str,
    parameters: dict[str, Any],
    requires_confirm: bool = False,
    danger: str = "low",
    group: str = "general",
    returns: str = "text",
) -> Callable[[Callable[..., Any]], Action]:
    """Decorator used by driver modules to declare an action."""

    def wrapper(fn: Callable[..., Any]) -> Action:
        return Action(
            name=name,
            description=description,
            capability=capability,
            handler=fn,
            parameters=parameters,
            requires_confirm=requires_confirm,
            danger=danger,
            group=group,
            returns=returns,
        )

    return wrapper


class ActionRegistry:
    """Holds every action and is the single choke point for execution."""

    def __init__(self, bus: EventBus | None = None, memory: Any = None, config: AppConfig | None = None):
        self.bus = bus or EventBus()
        self.memory = memory
        self.config = config
        self._actions: dict[str, Action] = {}
        self._groups: dict[str, list[str]] = {}
        #: Actions the user explicitly approved in the HUD. Confirmation is a user decision,
        #: so it lives here as a revocable grant rather than being passed around in a context.
        self.approvals: set[str] = set()
        self.pending: dict[str, dict[str, Any]] = {}
        #: Risk-tiered verification effort (contract 22.8).
        self.risk_tiers = RiskTiers()
        #: Optional owner registry; enforced only when a context opts in (contract 22.7).
        self.owner: Any = None
        #: Optional LessonBook (see ``ops/lessons.py``): a failure that repeats gets the known fix
        #: attached to its error text, so the next attempt starts corrected instead of blind.
        self.lessons: Any = None
        #: Optional Capability Matrix (4.7). When present, a tool with no driver on this
        #: machine is never offered to a model: it is reported as unavailable instead, which
        #: is the difference between "cannot" and "fails halfway through a task".
        self.capabilities: Any = None
        #: Evidence receipts (4.2). Built by the host for every executed call, stored when a
        #: memory store is attached, and attached to the result so a report can cite them.
        self.receipts_enabled: bool = True
        self.last_receipt: Receipt | None = None

    def _learned(self, action_name: str, error: str) -> str:
        """Attach the best known fix to a repeated failure, and record this one.

        Deliberately silent when the book is missing or broken: an action that failed must
        report its own failure, never a secondary error raised by the memory add-on.
        """
        book = self.lessons
        if book is None:
            return error
        try:
            return book.annotate(action_name, error)
        except Exception:  # noqa: BLE001 - see docstring
            return error

    def approve(self, action_name: str, approved: bool = True) -> None:
        if approved:
            self.approvals.add(action_name)
            self.pending.pop(action_name, None)
        else:
            self.approvals.discard(action_name)
            self.pending.pop(action_name, None)

    def is_approved(self, action_name: str) -> bool:
        return action_name in self.approvals

    # -- registration ------------------------------------------------------------------
    def register(self, act: Action, replace: bool = True) -> Action:
        if act.name in self._actions and not replace:
            raise ValueError(f"action already registered: {act.name}")
        self._actions[act.name] = act
        self._groups.setdefault(act.group, [])
        if act.name not in self._groups[act.group]:
            self._groups[act.group].append(act.name)
        return act

    def register_all(self, actions: Iterable[Action]) -> None:
        for act in actions:
            self.register(act)

    def register_module(self, module: Any) -> int:
        """Register every :class:`Action` a module defines at import time."""
        count = 0
        for value in vars(module).values():
            if isinstance(value, Action):
                self.register(value)
                count += 1
        return count

    def get(self, name: str) -> Action | None:
        return self._actions.get(name)

    def names(self, group: str | None = None) -> list[str]:
        return sorted(self._groups.get(group, [])) if group else sorted(self._actions)

    def groups(self) -> dict[str, list[str]]:
        return {g: sorted(names) for g, names in sorted(self._groups.items())}

    def __len__(self) -> int:
        return len(self._actions)

    # -- discovery for models ----------------------------------------------------------
    def tool_specs(
        self,
        capabilities: Iterable[str] | None = None,
        limit: int = 0,
        available_only: bool = True,
    ) -> list[ToolSpec]:
        """Tool definitions for the model, filtered to the caller's real capabilities.

        With a Capability Matrix attached, tools the machine cannot run are omitted entirely
        (4.7), and tools that have not yet been proven on a real app carry an explicit
        ``[unproven]`` marker so a model cannot mistake an untested path for a known one. The
        marker is one word on purpose: the detail lives in the matrix, not in every prompt.
        """
        caps = frozenset(capabilities) if capabilities is not None else None
        specs: list[ToolSpec] = []
        for name in sorted(self._actions):
            act = self._actions[name]
            if caps is not None and act.capability not in caps:
                continue
            record = None
            if self.capabilities is not None:
                record = self.capabilities.record(name)
                if available_only and not record.available_here:
                    continue
            spec = act.tool_spec()
            if record is not None and not record.green:
                spec.description = f"{spec.description} [unproven]"
            specs.append(spec)
        return specs[:limit] if limit else specs

    def unavailable_tools(self) -> list[dict[str, Any]]:
        """What this machine cannot do, so an agent asks instead of improvising (4.7)."""
        if self.capabilities is None:
            return []
        return [hidden.to_json() for hidden in self.capabilities.hidden()]

    def risk_tier(self, action_name: str) -> str:
        act = self._actions.get(action_name)
        if act is None:
            return "low"
        return self.risk_tiers.tier_for(act.name, act.capability, act.danger, act.group)

    def verification_plan(self, names: Iterable[str] | None = None) -> dict[str, Any]:
        """Which of these actions are verified immediately, batched, or never shortcut."""
        selected = [self._actions[n] for n in (names or self.names()) if n in self._actions]
        return self.risk_tiers.plan(selected).to_json()

    def describe(self) -> list[dict[str, Any]]:
        return [
            {
                "name": a.name,
                "group": a.group,
                "capability": a.capability,
                "danger": a.danger,
                "requires_confirm": a.requires_confirm,
                "description": a.description,
                # The parameter schema belongs in the catalogue: it is what the model is
                # given and what the call is validated against, so a catalogue without it
                # would describe a tool that cannot be called.
                "parameters": a.parameters or {"type": "object", "properties": {}},
            }
            for a in (self._actions[n] for n in sorted(self._actions))
        ]

    # -- execution ---------------------------------------------------------------------
    def invoke(self, action_name: str, ctx: ActionContext | None = None, **kwargs: Any) -> ActionResult:
        # The first parameter is deliberately not called ``name``: actions take a ``name``
        # argument themselves (proc_which, browser_type, windows_click_element…), and a
        # positional collision there would be an infuriating, invisible bug.
        name = action_name
        ctx = ctx or ActionContext()
        ctx.registry = self
        act = self._actions.get(name)
        started = time.time()
        if act is None:
            result = ActionResult.failure(f"unknown action: {name}", {"available": self.names()[:40]})
            self._record(ctx, name or "<unknown>", "<none>", "error", kwargs, result, started)
            return result

        # 1. capability gate
        if not ctx.can(act.capability):
            result = ActionResult.failure(
                f"capability denied: {act.capability}",
                {"required": act.capability, "granted": sorted(ctx.capabilities)},
            )
            self.bus.emit(
                ACTION_DENIED,
                source=ctx.agent_id,
                severity="warning",
                action=name,
                capability=act.capability,
                reason="missing capability",
            )
            self._record(ctx, name, act.capability, "denied", kwargs, result, started)
            return result

        # 2. confirmation gate
        confirm_required = act.requires_confirm or (
            self.config is not None and act.capability in self.config.safety.confirm_capabilities
        )
        if confirm_required and not self._is_confirmed(ctx, act):
            detail = f"{act.danger}-danger action '{name}' on { _brief(kwargs) }"
            self.pending[name] = {"action": name, "capability": act.capability, "detail": detail, "agent": ctx.agent_id}
            self.bus.emit(
                ACTION_CONFIRM,
                source=ctx.agent_id,
                severity="notice",
                action=name,
                capability=act.capability,
                detail=detail,
                args=_brief(kwargs),
            )
            result = ActionResult.failure(
                f"confirmation required for {name}",
                {"confirmation_required": True, "action": name, "detail": detail},
            )
            self._record(ctx, name, act.capability, "denied", kwargs, result, started)
            return result

        # 2b. owner gate (contract 22.7): irreversible work needs the owner, not just a session
        if ctx.require_owner and self.owner is not None and act.capability in OWNER_IRREVERSIBLE:
            decision = self.owner.require([act.capability], method=ctx.owner_method)
            if not decision.allow:
                result = ActionResult.failure(
                    f"owner authorization required for {name}: {decision.reason}",
                    {"owner_required": True, "method": decision.method, "capability": act.capability},
                )
                self.bus.emit(
                    ACTION_DENIED,
                    source=ctx.agent_id,
                    severity="notice",
                    action=name,
                    capability=act.capability,
                    reason=decision.reason,
                )
                self._record(ctx, name, act.capability, "denied", kwargs, result, started)
                return result

        # 3. dry run (used by the doctor command and by safety previews)
        if ctx.dry_run:
            result = ActionResult.success(
                {"dry_run": True, "action": name, "args": _brief(kwargs)}, evidence={"dry_run": True}
            )
            self._record(ctx, name, act.capability, "ok", kwargs, result, started)
            return result

        # 3b. journal: the intention is on disk before the side effect happens (4.5). A crash
        # between these two lines is exactly the case reconciliation exists for.
        journal = getattr(self, "journal", None)
        entry_id = 0
        if journal is not None:
            intent = intent_for(name, act.capability, kwargs, ctx, getattr(act, "danger", ""))
            if intent.idempotency_key:
                previous = journal.lookup(intent.idempotency_key)
                if previous is not None and previous.state == COMMITTED:
                    # Exactly-once: this action already ran, so its recorded outcome is the
                    # answer. Executing again would duplicate a message, a payment or a write.
                    replay = ActionResult.success(
                        previous.outcome,
                        evidence={**previous.evidence, "replayed": True, "journal_entry": previous.entry_id},
                    )
                    replay.duration = time.time() - started
                    self._record(ctx, name, act.capability, "ok", kwargs, replay, started)
                    self.bus.emit(
                        ACTION_RESULT,
                        source=ctx.agent_id,
                        severity="info",
                        action=name,
                        capability=act.capability,
                        ok=True,
                        replayed=True,
                        detail="already executed; returned the journaled outcome",
                    )
                    return replay
            entry_id = journal.begin(intent)
            journal.mark_running(entry_id)

        # 4. execute
        self.bus.emit(
            ACTION_INVOKED,
            source=ctx.agent_id,
            action=name,
            capability=act.capability,
            args=_brief(kwargs),
            danger=act.danger,
        )
        try:
            raw = act.invoke(ctx, **kwargs)
            result = raw if isinstance(raw, ActionResult) else ActionResult.success(raw)
        except (ConfirmationRequired, CapabilityError) as exc:
            result = ActionResult.failure(str(exc))
        except DriverUnavailable as exc:
            # A missing driver is the most repeatable failure there is: the known fix (which
            # install command actually helps) is exactly what the next attempt needs.
            result = ActionResult.failure(self._learned(name, str(exc)), exc.evidence)
        except ActionFailed as exc:
            result = ActionResult.failure(self._learned(name, str(exc)), exc.evidence)
        except TypeError as exc:
            # A model passing wrong argument names is a common, recoverable event.
            result = ActionResult.failure(
                f"bad arguments for {name}: {exc}",
                {"signature": self._signature(act), "given": sorted(kwargs)},
            )
        except Exception as exc:  # never let a driver crash the agent loop
            result = ActionResult.failure(
                f"{type(exc).__name__}: {exc}",
                {"traceback": traceback.format_exc(limit=3)},
            )
        result.duration = time.time() - started
        if journal is not None and entry_id:
            journal.finish(entry_id, result.ok, result.output, result.evidence, result.error)
        receipt = self._receipt(name, act, ctx, kwargs, result, started)
        self._record(ctx, name, act.capability, "ok" if result.ok else "error", kwargs, result, started)
        self.bus.emit(
            ACTION_RESULT,
            source=ctx.agent_id,
            severity="info" if result.ok else "warning",
            action=name,
            capability=act.capability,
            ok=result.ok,
            duration=round(result.duration, 3),
            detail=result.detail,
            evidence=_brief(result.evidence),
            output=_brief({"v": result.output}),
            receipt_id=receipt.receipt_id if receipt else "",
            outcome=receipt.outcome if receipt else "",
            rung=receipt.rung_name if receipt else "",
        )
        return result

    def invoke_many(self, calls: Sequence[Mapping[str, Any]], ctx: ActionContext | None = None) -> list[ActionResult]:
        """Run a batch of ``{"action": name, ...args}`` requests in order."""
        return [self.invoke(str(c.get("action", "")), ctx, **{k: v for k, v in c.items() if k != "action"}) for c in calls]

    # -- helpers -----------------------------------------------------------------------
    def _is_confirmed(self, ctx: ActionContext, act: Action) -> bool:
        return (
            act.name in ctx.confirmed
            or act.capability in ctx.confirmed
            or "*" in ctx.confirmed
            or act.name in self.approvals
        )

    def _receipt(
        self,
        name: str,
        act: Action,
        ctx: ActionContext,
        kwargs: dict[str, Any],
        result: ActionResult,
        started: float,
    ) -> Receipt | None:
        """Build, attach and store the evidence receipt for one executed call (4.2).

        A call that never reached a driver (capability denied, confirmation missing, dry run)
        deliberately produces no receipt: there is no evidence because nothing happened, and
        the denial event is the record of it.
        """
        if not self.receipts_enabled or ctx.dry_run:
            return None
        try:
            receipt = Receipt.build(
                tool=name,
                act=act,
                ctx=ctx,
                args=kwargs,
                result=result,
                started_at=started,
                risk_tier=self.risk_tier(name),
            )
        except Exception:  # a receipt must never break the action it describes
            return None
        result.evidence = {**result.evidence, "receipt": receipt.to_json()}
        self.last_receipt = receipt
        if self.memory is not None:
            try:
                self.memory.store_receipt(receipt.to_json())
            except Exception:
                pass
        return receipt

    def _signature(self, act: Action) -> list[str]:
        try:
            sig = inspect.signature(act.handler)
            return [p.name for p in sig.parameters.values() if p.name != "ctx"]
        except (TypeError, ValueError):  # pragma: no cover - builtins
            return []

    def _record(
        self,
        ctx: ActionContext,
        name: str,
        capability: str,
        outcome: str,
        kwargs: dict[str, Any],
        result: ActionResult,
        started: float,
    ) -> None:
        if self.memory is None:
            return
        try:
            self.memory.audit(
                agent_id=ctx.agent_id,
                action=name,
                capability=capability,
                outcome=outcome,
                args=kwargs,
                duration=time.time() - started,
                evidence=result.evidence if isinstance(result.evidence, dict) else {},
                detail=result.error[:400] if result.error else "",
            )
        except Exception:  # auditing must never break execution
            pass


def _brief(value: Any, max_len: int = 220) -> Any:
    """Shrink arguments/evidence for events and audit rows without losing meaning."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in list(value.items())[:12]:
            if isinstance(v, str) and len(v) > max_len:
                out[k] = v[:max_len] + f"…(+{len(v) - max_len})"
            elif isinstance(v, (dict, list)):
                out[k] = _brief(v, max_len // 2)
            else:
                out[k] = v
        return out
    if isinstance(value, list):
        return [_brief(v, max_len // 2) for v in value[:12]]
    if isinstance(value, str) and len(value) > max_len:
        return value[:max_len] + f"…(+{len(value) - max_len})"
    return value
