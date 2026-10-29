"""The agent: identity + memory + tools + budget, in one loop.

One agent is: a role (what it is for), a rank (what it may command), a capability set (what
it may touch), a budget (how much it may spend), its own memory namespace, and the loop that
turns an objective into actions and then into a *short* report.

The loop is deliberately plain — plan, act, observe, repeat, report — because that is what
survives contact with a real desktop:

1. Recall relevant memories into the prompt (the agent's own past experience).
2. Ask the model what to do next, with the tool list filtered to its real capabilities.
3. Execute the returned tool calls through the capability-gated registry.
4. Feed every result back as a tool message, including failures and their evidence.
5. Finish by calling the ``agent_report`` tool — never by returning prose. A report is
   structured data: status, one-line summary, evidence, blockers, next step, confidence.

The report is short by contract. The full narrative is written into the agent's own memory
and the team board; the parent receives five lines and can query memory if it wants detail.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from ..actions.registry import ActionContext, ActionResult, ActionRegistry, DriverUnavailable
from ..config import AppConfig
from ..events import (
    AGENT_ERROR,
    AGENT_MESSAGE,
    AGENT_REPORT,
    AGENT_SPAWNED,
    AGENT_STATUS,
    AGENT_TERMINATED,
    ACTION_INVOKED,
    EventBus,
    MEMORY_RECALL,
    MEMORY_WRITE,
)
from ..llm.providers import LLMRequest, LLMResponse, Message, ToolSpec
from ..llm.runtime import AllModelsUnavailable, BudgetExceeded, ModelRuntime, Requirements
from ..memory import MemoryStore
from .identity import AgentIdentity
from .roles import COMMON_LAW, role_template

REPORT_TOOL = "agent_report"
MAX_REPORT_SUMMARY = 400


@dataclass(slots=True)
class AgentReport:
    """The five-line contract between an agent and its parent.

    ``status`` is one of ``done``, ``partial``, ``blocked``, ``failed`` or ``disputed``.
    An agent may claim the first four itself; ``disputed`` is written by the outcome verifier
    when the finished state does not satisfy the task's own success criteria. It exists in
    this vocabulary on purpose: without it a verified failure looked like an ordinary claim of
    success to every consumer (HUD status pill, team board, parent's consolidation prompt).
    """

    task_id: str = ""
    agent: str = ""
    status: str = "done"  # done | failed | blocked | partial | disputed
    summary: str = ""
    evidence: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    next_step: str = ""
    confidence: float = 0.5
    actions: int = 0
    tokens: int = 0
    seconds: float = 0.0
    #: The verifier's verdict on the finished state against the task's success criteria
    #: (contract 22.4). ``None`` means the task carried no contract, not that it passed.
    verdict: dict[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "agent": self.agent,
            "status": self.status,
            "summary": self.summary,
            "evidence": self.evidence[:8],
            "blockers": self.blockers[:5],
            "next_step": self.next_step,
            "confidence": round(self.confidence, 2),
            "actions": self.actions,
            "tokens": self.tokens,
            "seconds": round(self.seconds, 1),
            "verdict": self.verdict,
        }

    def one_line(self) -> str:
        tail = f" — {self.blockers[0]}" if self.blockers else (f" → {self.next_step}" if self.next_step else "")
        return f"[{self.status}] {self.agent}: {self.summary}{tail}"


class Agent:
    """A single living agent."""

    def __init__(
        self,
        identity: AgentIdentity,
        config: AppConfig,
        runtime: ModelRuntime,
        actions: ActionRegistry,
        memory: MemoryStore,
        bus: EventBus,
        orchestrator: Any = None,
        executor: ThreadPoolExecutor | None = None,
    ):
        self.identity = identity
        self.config = config
        self.runtime = runtime
        self.actions = actions
        self.memory = memory
        self.bus = bus
        self.orchestrator = orchestrator
        self.executor = executor
        self.template = role_template(identity.role)
        self.stopped = False
        self.current_task: str = ""
        self.reports: list[AgentReport] = []

    # -- identity helpers --------------------------------------------------------------
    @property
    def id(self) -> str:
        return self.identity.agent_id

    @property
    def name(self) -> str:
        return self.identity.name

    @property
    def model(self) -> str:
        record = self.memory.get_agent(self.id)
        return (record.model if record and record.model else self.identity.model) or ""

    def budget(self) -> Any:
        return self.runtime.budget_for(self.id, self.identity.budget)

    def can(self, capability: str) -> bool:
        return self.identity.can(capability)

    # -- prompt ------------------------------------------------------------------------
    def system_prompt(self, objective: str = "", team_note: str = "") -> str:
        caps = ", ".join(sorted(self.identity.capabilities)) or "(none)"
        budget = self.budget()
        memory_digest = self._memory_digest(objective)
        roster_note = ""
        if self.orchestrator is not None:
            roster = self.orchestrator.roster()
            roster_note = "Fleet: " + "; ".join(f"{r['name']}({r['role']},rank-{r['rank']})" for r in roster[:12])
        return "\n\n".join(
            part
            for part in (
                self.template.system_prompt,
                COMMON_LAW if self.template.system_prompt != COMMON_LAW else "",
                f"Identities: you are {self.name} ({self.template.title}), rank {self.identity.rank} "
                f"({self.identity.rank_name}), id {self.id}. Model: {self.model or 'unassigned'}.",
                f"Granted capabilities: {caps}. Anything outside this list is denied by the runtime — "
                "report it upward instead of trying to work around it.",
                f"Budget: {budget.actions_used}/{budget.max_actions} actions, "
                f"{budget.tokens_used}/{budget.max_tokens} tokens, "
                f"{budget.max_wall_seconds:.0f}s wall clock.",
                roster_note,
                memory_digest,
                self._lesson_digest(),
                team_note,
                f"Finish every task by calling the `{REPORT_TOOL}` tool. Prose is not a completion. "
                "Summary must be one line. Put detail in memory, not in the report.",
            )
            if part
        )

    def _lesson_digest(self) -> str:
        """Known failure patterns for this machine, so an agent does not repeat them.

        Read from the registry's LessonBook (see ``ops/lessons.py``). Empty when there is
        nothing learned yet: an invented hint would be worse than none.
        """
        holder = getattr(self, "registry", None)
        if holder is None:
            holder = getattr(self, "actions", None)
        book = getattr(holder, "lessons", None)
        if book is None:
            return ""
        try:
            rows = book.top(5)
        except Exception:  # noqa: BLE001 - the prompt must still build
            return ""
        if not rows:
            return ""
        lines = [f"- {row['action']} failed {row['hits']}x: {row['cause']} → {row['fix']}" for row in rows]
        return (
            "Failure patterns already observed on this machine. Read them before choosing a tool; "
            "repeating one is your mistake, not the machine's:\n" + "\n".join(lines)
        )

    def _memory_digest(self, query: str) -> str:
        hits = self.memory.recall(self.id, query, k=6, include_teams=[self.identity.team_id or ""], include_global=False)
        facts = self.memory.facts(self.id)
        rows = [f"- ({hit.kind}) {hit.text[:220]}" for hit in hits]
        if facts:
            rows.append("- facts: " + json.dumps({k: v for k, v in list(facts.items())[:12]}, default=str)[:600])
        return "Your memory of related work:\n" + ("\n".join(rows) if rows else "(nothing yet)")

    def tools(self) -> list[ToolSpec]:
        specs = self.actions.tool_specs(self.identity.capabilities)
        specs.append(
            ToolSpec(
                name=REPORT_TOOL,
                description=(
                    "Report the outcome of the assigned task and finish. Call this exactly once, "
                    "when the task is done, impossible, or blocked."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "status": {"type": "string", "enum": ["done", "partial", "blocked", "failed"]},
                        "summary": {"type": "string", "description": "One line. No paragraphs."},
                        "evidence": {"type": "array", "items": {"type": "string"}, "description": "File paths, URLs, ids, observed values."},
                        "blockers": {"type": "array", "items": {"type": "string"}},
                        "next_step": {"type": "string"},
                        "confidence": {"type": "number"},
                        "detail": {"type": "string", "description": "Long-form detail for your own memory and the team board."},
                        "remember": {"type": "array", "items": {"type": "string"}, "description": "Durable facts to keep for next time."},
                    },
                    "required": ["status", "summary"],
                },
            )
        )
        return specs

    # -- the loop ----------------------------------------------------------------------
    def run_task(
        self,
        title: str,
        objective: str,
        context: str = "",
        max_steps: int = 24,
        parent_task: str | None = None,
        team_id: str | None = None,
        contract: Any = None,
        work_class: str = "interactive",
    ) -> AgentReport:
        started = time.time()
        self.work_class = work_class
        if contract is not None and not getattr(contract, "task_id", ""):
            contract.task_id = "pending"
        team = team_id or self.identity.team_id or "fleet"
        task_id = self.memory.create_task(
            team,
            owner=self.id,
            title=title,
            parent_task=parent_task,
            detail={"objective": objective, "context": context[:2000]},
        )
        self.current_task = title
        self.memory.set_agent_status(self.id, "running")
        self.bus.emit(
            AGENT_STATUS,
            source=self.id,
            agent=self.name,
            role=self.identity.role,
            rank=self.identity.rank,
            status="running",
            task=title,
            model=self.model,
        )
        self.memory.append_message(self.id, "user", f"OBJECTIVE: {title}\n{objective}\n\nCONTEXT:\n{context}"[:20_000])

        messages = self._conversation()
        requirements = Requirements(tools=True, tags=self.template.preferred_tags)
        steps = 0
        total_tokens = 0
        action_count = 0
        prose_rounds = 0
        report: AgentReport | None = None

        while steps < max_steps and not self.stopped:
            steps += 1
            budget = self.budget()
            try:
                budget.check(time.time())
            except BudgetExceeded as exc:
                report = AgentReport(task_id, self.name, "failed", f"budget exhausted: {exc}", [], [str(exc)], "", 0.2, action_count, total_tokens, time.time() - started)
                break

            if contract is not None and contract.task_id == "pending":
                contract.task_id = task_id
            deferred = self._honour_governor(deadline_deferrals=self._deferral_budget())
            if deferred is None:
                # The machine has been busy for the whole allowed window: report it honestly
                # instead of burning the budget waiting for a desktop that is not free.
                report = AgentReport(
                    task_id, self.name, "blocked", "deferred: the machine is busy (game, call or presentation)",
                    [], ["responsiveness governor held this task back"], "rerun when the machine is free",
                    0.3, action_count, total_tokens, time.time() - started,
                )
                break

            request = LLMRequest(
                messages=messages,
                model=self.model,
                temperature=self.template.temperature,
                tools=self.tools(),
                system=self.system_prompt(objective, team_note=self._team_note()),
            )
            try:
                result = self.runtime.run(request, agent_id=self.id, prefer=[self.model], requirements=requirements)
            except AllModelsUnavailable as exc:
                report = AgentReport(
                    task_id, self.name, "blocked", "no model available to continue",
                    [], [f"all models unavailable: {len(exc.attempts)} attempts"], "escalate to Steward/Sovereign",
                    0.3, action_count, total_tokens, time.time() - started,
                )
                break
            except Exception as exc:  # noqa: BLE001 - surfaced as a report, never a crash
                self.bus.emit(AGENT_ERROR, source=self.id, severity="error", message=str(exc)[:400])
                report = AgentReport(task_id, self.name, "failed", f"runtime error: {exc}", [], [str(exc)[:200]], "", 0.2, action_count, total_tokens, time.time() - started)
                break

            response = result.response
            if result.fell_back:
                messages.append(
                    Message(
                        role="system",
                        content=f"(continued on {result.model} after failover; your identity, memory and history are unchanged)",
                    )
                )
            total_tokens += response.usage.total_tokens
            budget.charge_tokens(response.usage.total_tokens, time.time())
            self.memory.append_message(
                self.id,
                "assistant",
                response.text or "",
                meta={"model": result.model, "tool_calls": [c.get("function", {}).get("name") for c in response.tool_calls]},
            )

            if not response.tool_calls:
                prose_rounds += 1
                if response.text.strip() and prose_rounds <= 3:
                    # A model answering with prose is normal — but it is not a completion, and
                    # looping on it forever would burn the whole budget on nothing. Nudge twice,
                    # then take the text as an unverified partial and report it honestly.
                    messages.append(Message(role="assistant", content=response.text))
                    messages.append(
                        Message(
                            role="user",
                            content=(
                                f"That was prose, not action. Either call a tool to make progress, or call "
                                f"`{REPORT_TOOL}` if the objective is genuinely complete or impossible."
                            ),
                        )
                    )
                    if steps < max_steps:
                        continue
                report = AgentReport(
                    task_id, self.name, "partial", (response.text or "no output").strip().splitlines()[0][:MAX_REPORT_SUMMARY],
                    [], ["model returned no tool call, so completion is unverified"], "re-run or inspect transcript",
                    0.4, action_count, total_tokens, time.time() - started,
                )
                break

            finished = False
            try:
                for call in response.tool_calls:
                    name = call.get("function", {}).get("name", "")
                    raw_args = call.get("function", {}).get("arguments", "{}")
                    args = _parse_args(raw_args)
                    messages.append(Message(role="assistant", content=response.text or "", tool_calls=[call]))
                    if name == REPORT_TOOL:
                        report = self._finish(task_id, args, action_count, total_tokens, started)
                        finished = True
                        break
                    outcome = self.act(name, **args)
                    action_count += 1
                    budget.charge_action(time.time())
                    messages.append(
                        Message(
                            role="tool",
                            tool_call_id=call.get("id", ""),
                            name=name,
                            content=_tool_result_text(name, outcome),
                        )
                    )
            except BudgetExceeded as exc:
                # Hitting a budget is a normal outcome, not a crash: report it upward.
                report = AgentReport(
                    task_id,
                    self.name,
                    "failed",
                    f"resource budget exhausted: {exc}",
                    [],
                    [str(exc)],
                    "split the task or raise the grant",
                    0.2,
                    action_count,
                    total_tokens,
                    time.time() - started,
                )
            if finished or report is not None:
                break

        if report is None:
            report = AgentReport(
                task_id, self.name, "partial", f"stopped after {steps} steps without a report",
                [], ["step limit reached"], "re-scope the task or raise max_steps",
                0.3, action_count, total_tokens, time.time() - started,
            )
        report = self._verify_outcome(contract, report, task_id)
        self.reports.append(report)
        self.memory.update_task(task_id, status=report.status, report=report.to_json())
        self.memory.set_agent_status(self.id, "idle")
        self.bus.emit(AGENT_REPORT, source=self.id, **report.to_json())
        return report

    # -- outcome verification (contract 22.4) ------------------------------------------
    def _deferral_budget(self) -> float:
        return float(getattr(getattr(self.config, "ops", None), "governor_max_defer_s", 90) or 90)

    def _honour_governor(self, deadline_deferrals: float) -> float | None:
        """Yield the machine while a game, call or presentation is running.

        Returns None when the machine has been busy longer than the allowed window, so the
        caller can report a deferral instead of waiting forever. Interactive work the user
        asked for is never held back (see ``ops/governor.py``).
        """
        governor = getattr(getattr(self, "orchestrator", None), "governor", None)
        if governor is None:
            return 0.0
        state = governor.evaluate()
        allowed, why = governor.may_run(getattr(self, "work_class", "interactive"), state)
        if allowed:
            self._deferred_total = getattr(self, "_deferred_total", 0.0)
            return 0.0
        delay = min(governor.delay_for(getattr(self, "work_class", "interactive"), state) or 5.0, 15.0)
        self._deferred_total = getattr(self, "_deferred_total", 0.0) + delay
        if self._deferred_total > deadline_deferrals:
            return None
        self.bus.emit(
            AGENT_STATUS,
            source=self.id,
            severity="notice",
            agent=self.name,
            status="deferred",
            reason=why,
            seconds=round(delay, 1),
        )
        time.sleep(delay)
        return delay

    def _verify_outcome(self, contract: Any, report: AgentReport, task_id: str) -> AgentReport:
        """Check what was asked against what is, rather than trusting the agent's own claim."""
        verifier = getattr(getattr(self, "orchestrator", None), "verifier", None)
        if contract is None or verifier is None:
            return report
        contract.task_id = task_id
        # Evidence the task produced becomes checkable criteria: if the agent wrote files or
        # visited URLs while working, those are things the finished state can be tested against.
        performed: list[dict[str, Any]] = []
        try:
            for row in self.memory.audit_log(limit=400, agent_id=self.id):
                performed.append(row)
        except Exception:
            performed = []
        self._derive_criteria(contract, performed)
        observations = {"resolved": {}}
        verdict = verifier.check(contract, observations, performed=performed, claim=report.summary)
        report.verdict = verdict.to_json()
        if verdict.status == "not_met":
            report.status = "disputed"
            report.blockers = [*report.blockers, f"outcome check failed: {verdict.summary}"][:5]
            report.next_step = report.next_step or "fix the unmet criteria before calling this done"
        elif verdict.status == "needs_user" and verdict.question:
            report.next_step = report.next_step or verdict.question
            self.context_note = verdict.question
        elif verdict.status == "uncertain":
            # Not a failure, but not a pass either: say what would have to be checkable.
            report.next_step = report.next_step or (
                "outcome check was inconclusive (nothing checkable was written for this task); "
                "state what proves the goal is met"
            )
        self.remember(
            f"Outcome check for {task_id}: {verdict.status} - {verdict.summary}",
            kind="verification",
            importance=0.7,
            tags=["verification", verdict.status],
        )
        return report

    @staticmethod
    def _derive_criteria(contract: Any, performed: list[dict[str, Any]]) -> None:
        """Turn real side effects into criteria, so 'done' gets tested against reality."""
        from ..ops.verification import FILE_EXISTS, SuccessCriterion

        known = {c.target for c in contract.criteria}
        for row in performed:
            args = row.get("args") or {}
            if not isinstance(args, dict):
                continue
            action = str(row.get("action") or "")
            outcome = str(row.get("outcome") or "")
            if action == "fs_write" and outcome == "ok" and args.get("path"):
                target = str(args["path"])
                if target not in known:
                    contract.criteria.append(SuccessCriterion(FILE_EXISTS, target=target, note="file this task wrote"))
                    known.add(target)

    def _conversation(self) -> list[Message]:
        rows = self.memory.transcript(self.id, limit=30)
        messages: list[Message] = []
        for row in rows:
            role = row["role"] if row["role"] in {"user", "assistant", "system"} else "user"
            messages.append(Message(role=role, content=row["content"]))
        return messages

    def _team_note(self) -> str:
        if self.identity.team_id is None:
            return ""
        board = self.memory.read_board(self.identity.team_id, limit=8)
        if not board:
            return ""
        rows = [f"- {entry['author']}: {entry['topic']} — {entry['body'][:160]}" for entry in board[-6:]]
        siblings = [a for a in self.memory.list_agents(team_id=self.identity.team_id) if a.agent_id != self.id]
        if siblings:
            rows.append("Teammates: " + ", ".join(f"{a.name}({a.role},{a.status})" for a in siblings[:10]))
        return "Team board:\n" + "\n".join(rows)

    # -- acting ------------------------------------------------------------------------
    def act(self, name: str, **kwargs: Any) -> ActionResult:
        ctx = ActionContext(
            agent_id=self.id,
            capabilities=self.identity.capabilities,
            bus=self.bus,
            memory=self.memory,
            config=self.config,
            # No blanket pre-approval, for any rank. This used to be
            # ``tuple(config.safety.confirm_capabilities) if rank <= 1 else ()`` -- the same
            # list that *requires* confirmation also marked it pre-approved, so the gate
            # cancelled itself and the Sovereign and Steward never showed an approval card
            # for a single dangerous action. Pre-approval now comes only from a real, explicit
            # approval (the HUD's confirm path calls ``registry.approve``), which is what the
            # user actually clicked.
            confirmed=(),
            # The owner gate (contract 22.7): irreversible work needs the owner's PIN, however
            # this action was reached. The registry emits action.invoked itself, so this method
            # deliberately does not emit a second copy (it used to, doubling HUD effects).
            require_owner=bool(getattr(getattr(self.config, "ops", None), "require_owner_for_irreversible", False)),
        )
        result = self.actions.invoke(name, ctx, **kwargs)
        if result.ok and self._worth_remembering(name):
            self.memory.remember(
                self.id,
                f"{name} -> ok ({str(result.output)[:200]})",
                kind="action",
                tags=[name.split("_")[0]],
                importance=0.35,
            )
        return result

    @staticmethod
    def _worth_remembering(name: str) -> bool:
        return name.split("_")[0] in {"browser", "office", "whatsapp", "plugin", "fs", "windows", "agent"}

    # -- finishing ---------------------------------------------------------------------
    def _finish(self, task_id: str, args: dict[str, Any], actions: int, tokens: int, started: float) -> AgentReport:
        summary = str(args.get("summary", "")).strip().splitlines()[0][:MAX_REPORT_SUMMARY] if args.get("summary") else "no summary"
        report = AgentReport(
            task_id=task_id,
            agent=self.name,
            status=str(args.get("status", "done")),
            summary=summary,
            evidence=[str(e)[:300] for e in (args.get("evidence") or [])][:8],
            blockers=[str(b)[:200] for b in (args.get("blockers") or [])][:5],
            next_step=str(args.get("next_step", ""))[:300],
            confidence=float(args.get("confidence", 0.6) or 0.6),
            actions=actions,
            tokens=tokens,
            seconds=time.time() - started,
        )
        detail = str(args.get("detail", ""))
        if detail:
            self.memory.remember(self.id, detail[:8000], kind="report", tags=["report", task_id], importance=0.7)
        for fact in args.get("remember") or []:
            self.memory.set_fact(self.id, f"learned:{hash(str(fact)) % 10**8}", str(fact)[:500])
        if self.identity.team_id:
            self.memory.post_board(
                self.identity.team_id,
                f"report:{task_id}",
                f"{self.name}: [{report.status}] {report.summary} | evidence: {', '.join(report.evidence[:4]) or 'none'}",
                author=self.name,
            )
        self.bus.emit(MEMORY_WRITE, source=self.id, kind="report", task=task_id, bytes=len(detail))
        return report

    # -- messaging ---------------------------------------------------------------------
    def handle_message(self, sender: str, message: str, reply_budget_steps: int = 4) -> str:
        """Answer a short question from another agent without leaving the current task."""
        self.bus.emit(AGENT_MESSAGE, source=sender, to=self.id, agent=self.name, direction="in", preview=message[:200])
        self.memory.append_message(self.id, "user", f"[from {sender}] {message}"[:4000])
        request = LLMRequest(
            messages=[
                Message(role="user", content=f"{sender} asks: {message}\nAnswer in at most 3 lines with the facts you have."),
            ],
            model=self.model,
            temperature=self.template.temperature,
            tools=[],
            system=self.system_prompt(team_note=self._team_note()),
        )
        try:
            result = self.runtime.run(request, agent_id=self.id, prefer=[self.model], requirements=Requirements(tools=False))
            answer = result.response.text.strip()[:1200] or "(no answer)"
        except AllModelsUnavailable as exc:
            answer = f"(unavailable: all models exhausted — {exc})"
        self.memory.append_message(self.id, "assistant", answer)
        self.bus.emit(AGENT_MESSAGE, source=self.id, to=sender, agent=self.name, direction="out", preview=answer[:200])
        return answer

    def remember(self, text: str, kind: str = "note", importance: float = 0.5, tags: Sequence[str] = ()) -> int:
        episode = self.memory.remember(self.id, text, kind=kind, importance=importance, tags=list(tags))
        self.bus.emit(MEMORY_WRITE, source=self.id, kind=kind, episode=episode)
        return episode

    def recall(self, query: str, k: int = 8) -> list[dict[str, Any]]:
        hits = self.memory.recall(self.id, query, k=k, include_teams=[self.identity.team_id or ""])
        self.bus.emit(MEMORY_RECALL, source=self.id, query=query[:120], hits=len(hits))
        return [hit.to_json() for hit in hits]

    def stop(self) -> None:
        self.stopped = True
        self.memory.set_agent_status(self.id, "stopped")
        self.bus.emit(AGENT_TERMINATED, source=self.id, agent=self.name, role=self.identity.role)

    def to_json(self) -> dict[str, Any]:
        record = self.memory.get_agent(self.id)
        return {
            **self.identity.to_json(),
            "status": record.status if record else "unknown",
            "model": self.model,
            "task": self.current_task,
            "memory": self.memory.stats(self.id),
            "budget": self.budget().to_json(time.time()),
            "reports": [r.to_json() for r in self.reports[-3:]],
        }


def _parse_args(raw: Any) -> dict[str, Any]:
    """Parse tool-call arguments defensively.

    Real models emit more than clean JSON: ```json fences, a sentence of commentary
    before or after the object, trailing prose. Every one of those must still produce a
    usable call, because failing here wastes a whole turn and looks like the agent broke.
    """
    if isinstance(raw, dict):
        return raw
    if raw is None:
        return {}
    text = str(raw).strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except json.JSONDecodeError:
        pass
    candidate = _first_json_object(text)
    if candidate is not None:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return {"_raw": text[:500]}


def _first_json_object(text: str) -> str | None:
    """Extract the first balanced ``{...}`` block, ignoring braces inside strings."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _tool_result_text(name: str, result: ActionResult) -> str:
    if isinstance(result, DriverUnavailable):
        return f"{name}: UNAVAILABLE — {result}"
    payload = result.to_json()
    text = json.dumps(payload, default=str)
    return text if len(text) <= 12_000 else text[:12_000] + f'…(+{len(text) - 12_000} chars truncated)'
