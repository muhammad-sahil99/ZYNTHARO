"""Terminal actions (spec 9.5): owned shells an agent can actually work in.

These are the tools that make interactive work possible — a REPL, an installer, a pager, an
``ssh`` login — without the agent pretending to see a screen it does not have. Every command
comes back with its exit code, the observed completion reason, the working directory before
and after, the output line range and a hash, so a receipt can cite it and a model can read a
range instead of a whole log.

Safety is not optional here: the command is classified before it is typed, an L3 command
needs an approval bound to that exact command text, and a line whose echo cannot be read back
is cleared rather than executed.
"""

from __future__ import annotations

from typing import Any

from .registry import ActionContext, ActionFailed, ConfirmationRequired, action
from ..ops import terminal as term

#: One engine per process, created on first use and closed by the supervisor's shutdown.
_ENGINE: term.TerminalEngine | None = None


def engine() -> term.TerminalEngine:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = term.TerminalEngine()
    return _ENGINE


def set_engine(value: term.TerminalEngine | None) -> None:
    """Replace the engine (used by the app assembly and by tests)."""
    global _ENGINE
    _ENGINE = value


def _run(ctx: ActionContext, command: str, timeout: float, cadence: float, approve: str, session: str) -> Any:
    eng = engine()
    sess = eng.get(session) if session else None
    if sess is None:
        sessions = [row for row in eng.sessions(ctx.agent_id) if row.get("alive")]
        if not sessions:
            raise ActionFailed(
                "no open terminal session for this agent; call term_open first",
                {"hint": "term_open(shell='bash') returns a session_id", "agent": ctx.agent_id},
            )
        sess = eng.get(sessions[-1]["session_id"])
    risk = term.classify(command)
    if risk.needs_bound_confirmation:
        # The approval must name this exact command: an approval for "a shell command" would
        # let a different command ride on it (spec 5.3).
        token = term.command_token(command)
        if approve != token:
            raise ConfirmationRequired(
                "term_run",
                "shell.exec",
                f"{risk.label} command needs approval bound to this exact text: pass approve='{token}'. "
                f"Reasons: {'; '.join(risk.reasons[:3])}",
            )
    result = sess.run(command, timeout=timeout, cadence=cadence, risk=risk)
    payload = result.to_json()
    evidence = {
        "session_id": result.session_id,
        "command": command,
        "exit_code": result.exit_code,
        "cwd_before": result.cwd_before,
        "cwd_after": result.cwd_after,
        "output_lines": [result.first_line, result.last_line],
        "output_digest": result.output_digest,
        "target": result.session_id,
        "readback": result.verification,
        # Independent channels, so the receipt grades on observation rather than on a return
        # value: the terminal echoed the line, and the shell itself reported completion.
        "shell_marker": bool(result.markers),
        "before": {"cwd": result.cwd_before},
        "after": {"cwd": result.cwd_after, "exit_code": result.exit_code},
        "verified": result.verified,
    }
    return _result(payload, evidence, result.verified)


def _result(payload: dict[str, Any], evidence: dict[str, Any], verified: bool) -> Any:
    from .registry import ActionResult

    result = ActionResult.success(payload, evidence=evidence)
    result.verified = verified
    return result


@action(
    name="term_shells",
    description="List the shells that really exist on this machine, and which one is the default.",
    capability="process.list",
    group="shell",
    parameters={"type": "object", "properties": {}},
)
def term_shells(ctx: ActionContext) -> Any:
    return engine().shell_list()


@action(
    name="term_open",
    description=(
        "Open an owned terminal session (a real PTY where the platform allows one). "
        "Returns a session_id used by every other term_* action."
    ),
    capability="shell.exec",
    group="shell",
    parameters={
        "type": "object",
        "properties": {
            "shell": {"type": "string", "description": "Shell id from term_shells; default is the machine default."},
            "cwd": {"type": "string", "description": "Starting directory."},
            "name": {"type": "string", "description": "Short label for the session."},
            "env": {"type": "object", "description": "Extra environment variables."},
        },
    },
)
def term_open(ctx: ActionContext, shell: str = "", cwd: str = "", name: str = "", env: dict[str, str] | None = None) -> Any:
    session = engine().open(shell, agent_id=ctx.agent_id, cwd=cwd, env=env, name=name)
    ready = session.wait_prompt(timeout=10.0)
    return {
        "session_id": session.session_id,
        "shell": session.profile.id,
        "mode": session.mode,
        "supports_tty": session.supports_tty,
        "markers": session.markers_seen,
        "prompt_ready": ready,
        "cwd": session.cwd_current or session.cwd,
    }


@action(
    name="term_run",
    description=(
        "Type a command into a session, verify the typed line, run it, and return its exit "
        "code, output range, working directory before and after, and how completion was "
        "observed. L3 commands need an approval bound to the exact command text."
    ),
    capability="shell.exec",
    group="shell",
    danger="high",
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "session": {"type": "string", "description": "Session id; defaults to this agent's most recent session."},
            "timeout": {"type": "number", "description": "Seconds before the command is cancelled, default 120."},
            "cadence": {"type": "number", "description": "Seconds between keystrokes; 0 types as fast as possible."},
            "approve": {
                "type": "string",
                "description": "For L3 commands only: the token returned when approval was refused, proving the owner approved this exact command.",
            },
        },
        "required": ["command"],
    },
)
def term_run(
    ctx: ActionContext,
    command: str,
    session: str = "",
    timeout: float = 120.0,
    cadence: float = 0.0,
    approve: str = "",
) -> Any:
    if not command.strip():
        raise ActionFailed("command is empty")
    return _run(ctx, command, timeout, cadence, approve, session)


@action(
    name="term_read",
    description="Read a terminal session's output by line range, by tail, or by search. Keeps huge logs out of context.",
    capability="process.list",
    group="shell",
    parameters={
        "type": "object",
        "properties": {
            "session": {"type": "string"},
            "start": {"type": "integer", "description": "First line (1-based) for a range read."},
            "end": {"type": "integer", "description": "Last line, inclusive."},
            "tail": {"type": "integer", "description": "Read the last N lines instead."},
            "search": {"type": "string", "description": "Regex to search the buffer for."},
        },
    },
)
def term_read(
    ctx: ActionContext,
    session: str = "",
    start: int = 0,
    end: int = 0,
    tail: int = 0,
    search: str = "",
) -> Any:
    eng = engine()
    sess = eng.get(session) if session else None
    if sess is None:
        rows = eng.sessions(ctx.agent_id)
        if not rows:
            raise ActionFailed("no open terminal session for this agent", {"hint": "call term_open first"})
        sess = eng.get(rows[-1]["session_id"])
    if search:
        return sess.buffer.search(search)
    if tail:
        lines = sess.buffer.tail(tail)
        return {
            "session_id": sess.session_id,
            "lines": lines,
            "start": sess.buffer.total_lines - len(lines) + 1,
            "end": sess.buffer.total_lines,
            "total_lines": sess.buffer.total_lines,
        }
    if start:
        return {"session_id": sess.session_id, **sess.buffer.to_json(), **sess.buffer.range(start, end)}
    return {"session_id": sess.session_id, **sess.buffer.to_json(), "lines": sess.buffer.tail(40)}


@action(
    name="term_send",
    description="Send text or a named key to a session without running it (answer a prompt, drive a REPL).",
    capability="shell.exec",
    group="shell",
    danger="medium",
    parameters={
        "type": "object",
        "properties": {
            "session": {"type": "string"},
            "text": {"type": "string", "description": "Literal text to type."},
            "key": {
                "type": "string",
                "description": "One of enter, tab, escape, backspace, up, down, left, right, ctrl-c, ctrl-d, ctrl-l.",
            },
        },
    },
)
def term_send(ctx: ActionContext, session: str = "", text: str = "", key: str = "") -> Any:
    eng = engine()
    sess = eng.get(session) if session else None
    if sess is None:
        rows = eng.sessions(ctx.agent_id)
        if not rows:
            raise ActionFailed("no open terminal session for this agent")
        sess = eng.get(rows[-1]["session_id"])
    if not text and not key:
        raise ActionFailed("either text or key is required")
    if text:
        sess.type_text(text)
        echo_ok, echo_reason = sess.verify_echo(text)
    else:
        echo_ok, echo_reason = False, "a named key is not echoed as text"
    if key:
        sess.send_key(key)
    return {
        "session_id": sess.session_id,
        "sent_text": bool(text),
        "key": key,
        "echo_verified": echo_ok,
        "verification": echo_reason,
        "prompt_ready": sess.prompt_ready,
    }


@action(
    name="term_interrupt",
    description="Send Ctrl+C to a session (stop a running job) and report whether the prompt returned.",
    capability="shell.exec",
    group="shell",
    parameters={"type": "object", "properties": {"session": {"type": "string"}}},
)
def term_interrupt(ctx: ActionContext, session: str = "") -> Any:
    eng = engine()
    sess = eng.get(session) if session else None
    if sess is None:
        rows = eng.sessions(ctx.agent_id)
        if not rows:
            raise ActionFailed("no open terminal session for this agent")
        sess = eng.get(rows[-1]["session_id"])
    return {"session_id": sess.session_id, **sess.interrupt()}


@action(
    name="term_list",
    description="List this agent's terminal sessions with their shell, directory, mode and output size.",
    capability="process.list",
    group="shell",
    parameters={"type": "object", "properties": {"all_agents": {"type": "boolean"}}},
)
def term_list(ctx: ActionContext, all_agents: bool = False) -> Any:
    rows = engine().sessions("" if all_agents else ctx.agent_id)
    return {"count": len(rows), "sessions": rows}


@action(
    name="term_close",
    description="Close a terminal session. Anything it started is terminated with it.",
    capability="shell.exec",
    group="shell",
    danger="medium",
    parameters={"type": "object", "properties": {"session": {"type": "string"}}, "required": ["session"]},
)
def term_close(ctx: ActionContext, session: str) -> Any:
    return engine().close(session)


all_actions = lambda: [  # noqa: E731
    term_shells,
    term_open,
    term_run,
    term_read,
    term_send,
    term_interrupt,
    term_list,
    term_close,
]