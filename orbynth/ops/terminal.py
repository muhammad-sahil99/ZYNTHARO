"""The terminal engine (spec 9.5): owned shells, prompt markers, echo verification.

Why this exists next to ``proc_run``: running a command is easy, knowing when a command has
*finished* is not. ``proc_run`` waits for the process, which is fine for a build and useless
for a REPL, an installer, a pager or an ``ssh`` login. This module owns long-lived sessions
instead, and the difference is what makes interactive work possible:

* **Markers, not sleeps.** Shell integration emits OSC 133 prompt/command markers, so
  completion is a fact reported by the shell rather than a guess made by waiting.
* **Echo verification.** Text is typed, read back from the terminal's own echo, compared,
  and only then is Enter pressed. A keystroke that was swallowed never becomes a command.
* **Risk classification before execution.** A command is parsed and classed; unparseable
  text is L3 by definition, because "I could not understand it" is not a reason to run it.
* **A bounded buffer.** Output is kept as numbered lines with a hash and a size, so a model
  reads a range or a search result and a 2 GB log never enters a prompt.

Honest limits, recorded rather than hidden: a real PTY (interactive programs, echo, resize)
needs ``pty`` on POSIX or ConPTY on Windows. On a machine with neither, sessions fall back to
pipes, and ``mode`` says ``pipe``: commands still run and their output is still captured, but
``supports_tty`` is false, echo cannot be verified, and an interactive program will not work.
The engine reports that instead of pretending the session is a terminal.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

#: Risk levels, in the same vocabulary as the journal (0 observation .. 3 irreversible).
L0, L1, L2, L3 = 0, 1, 2, 3


@dataclass(slots=True)
class ShellProfile:
    """A shell this machine actually has."""

    id: str
    label: str
    argv: list[str]
    platform: str
    kind: str = "host"  # host | wsl | container | ssh
    notes: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "argv": self.argv,
            "platform": self.platform,
            "kind": self.kind,
            "notes": self.notes,
        }


def _profile(shell_id: str, label: str, path: str, args: list[str], kind: str = "host", notes: str = "") -> ShellProfile:
    return ShellProfile(shell_id, label, [path, *args], sys.platform, kind, notes)


def detect_shells() -> list[ShellProfile]:
    """Every shell present on this machine, in a stable order.

    Nothing is listed that is not installed: a shell in the list that cannot start would be
    exactly the kind of claimed capability the Capability Matrix exists to prevent.
    """
    found: list[ShellProfile] = []
    if os.name == "nt":  # pragma: no cover - Windows only
        for shell_id, label, names, args in (
            ("powershell", "Windows PowerShell 5", ("powershell.exe",), ["-NoLogo"]),
            ("pwsh", "PowerShell 7+", ("pwsh.exe", "pwsh"), ["-NoLogo"]),
            ("cmd", "Command Prompt", ("cmd.exe",), ["/Q", "/K"]),
            ("gitbash", "Git Bash", ("bash.exe",), ["--login", "-i"]),
        ):
            for name in names:
                path = shutil.which(name)
                if path:
                    found.append(_profile(shell_id, label, path, args))
                    break
        if shutil.which("wsl.exe"):  # pragma: no cover - Windows only
            found.append(_profile("wsl", "WSL", "wsl.exe", [], kind="wsl", notes="a second filesystem and user; permissions are the distro's"))
        for shell_id, label in (("nu", "Nushell"),):
            path = shutil.which(shell_id)
            if path:
                found.append(_profile(shell_id, label, path, []))
    else:
        for shell_id, label in (
            ("bash", "bash"),
            ("zsh", "zsh"),
            ("fish", "fish"),
            ("sh", "sh"),
            ("nu", "Nushell"),
        ):
            path = shutil.which(shell_id)
            if path:
                found.append(_profile(shell_id, label, path, ["-i"] if shell_id in ("bash", "zsh", "sh") else []))
    if shutil.which("docker"):
        found.append(_profile("docker", "Container (docker exec)", "docker", ["exec", "-i"], kind="container", notes="needs a running container id per session"))
    if shutil.which("kubectl"):
        found.append(_profile("kubectl", "Pod (kubectl exec)", "kubectl", ["exec", "-i"], kind="container", notes="needs a pod per session"))
    return found


def shell_profiles() -> dict[str, ShellProfile]:
    return {profile.id: profile for profile in detect_shells()}


def default_shell() -> ShellProfile | None:
    profiles = detect_shells()
    for preferred in ("pwsh", "powershell", "bash", "zsh", "sh", "cmd"):
        for profile in profiles:
            if profile.id == preferred:
                return profile
    return profiles[0] if profiles else None


# --------------------------------------------------------------------------------------
# Command risk (spec 9.5): parse first, judge second, refuse third
# --------------------------------------------------------------------------------------

#: Commands that only look. Anything not here is judged on its operators instead.
READ_ONLY_TOKENS = frozenset(
    {
        "ls", "dir", "cat", "type", "pwd", "cd", "echo", "printf", "grep", "find", "head",
        "tail", "wc", "sort", "uniq", "cut", "which", "where", "whoami", "hostname", "date",
        "env", "printenv", "tree", "file", "stat", "df", "du", "uname", "ps", "tasklist",
        "uptime", "python", "python3", "node", "npm", "pip", "git", "rg", "fd", "bat", "less",
        "more", "man", "help", "true", "false", "sleep", "time", "watch",
    }
)

#: Commands that change something the user would notice. Still ordinary work.
WRITING_TOKENS = frozenset(
    {
        "mkdir", "touch", "cp", "mv", "rename", "ln", "tee", "sed", "awk", "patch", "tar",
        "zip", "unzip", "gzip", "gunzip", "curl", "wget", "git", "pip", "npm", "yarn",
        "pnpm", "cargo", "go", "make", "cmake", "dotnet", "mvn", "gradle", "chmod",
        "chown", "export", "set", "source", "activate", "conda", "venv", "sqlite3",
    }
)

#: Patterns that make a command irreversible whatever else it contains. Each is a real way
#: to destroy a machine or exfiltrate data, not a style preference.
DANGEROUS_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\brm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+)+/", "recursive delete of an absolute path"),
    (r"\brmdir\s+/s\b", "recursive directory delete"),
    (r"\bdel\s+/[fsq]\b", "forced delete"),
    (r"\bformat\s+[a-zA-Z]:", "disk format"),
    (r"\bmkfs(\.\w+)?\b", "filesystem creation over a device"),
    (r"\bdd\b[^\n]*\bof=/dev/", "raw write to a device"),
    (r"\bdiskpart\b", "partition editing"),
    (r"\bshutdown\b|\breboot\b|\bhalt\b", "power state change"),
    (r"\bsudo\b|\brunas\b|\bdoas\b", "privilege elevation"),
    (r"\b(curl|wget)\b[^\n|]*\|\s*(ba)?sh\b", "piping a download into a shell"),
    (r"\bpowershell(\.exe)?\b[^\n]*-e(nc|ncodedcommand)?\b", "encoded PowerShell"),
    (r"\bbase64\s+(-d|--decode)\b[^\n]*\|\s*(ba)?sh\b", "decoded payload executed"),
    (r"\bchmod\s+(-R\s+)?777\s+/", "world-writable root"),
    (r"\b:\(\)\s*\{.*\}\s*;?\s*:", "fork bomb"),
    (r"\bgit\s+push\b[^\n]*--force", "history rewrite on a remote"),
    (r"\bgit\s+reset\b[^\n]*--hard", "discards uncommitted work"),
    (r"\bsqlite3\b[^\n]*\bDROP\s+TABLE\b", "destructive SQL"),
    (r"\breg\s+delete\b", "registry deletion"),
    (r"\battrib\b[^\n]*-s\b|\bicacls\b[^\n]*/reset\b", "permission tampering"),
    (r"\bnc\b[^\n]*-[el]\b|\bncat\b[^\n]*-[el]\b", "listening socket / reverse shell"),
    (r"\bhistory\s+-c\b", "audit trail removal"),
)

_DANGEROUS = tuple((re.compile(pattern, re.IGNORECASE), why) for pattern, why in DANGEROUS_PATTERNS)

#: Shell operators that mean "more than one command" or "a substitution I cannot see into".
_OPERATORS = ("&&", "||", ";", "|", ">", "<", "`", "$(", "${")


@dataclass(slots=True)
class RiskReport:
    """The verdict on one command line, with the reasons that produced it."""

    level: int
    reasons: list[str] = field(default_factory=list)
    parsed: bool = True
    tokens: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return {0: "L0-observe", 1: "L1-read", 2: "L2-write", 3: "L3-irreversible"}.get(self.level, f"L{self.level}")

    @property
    def needs_bound_confirmation(self) -> bool:
        return self.level >= L3

    def to_json(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "label": self.label,
            "reasons": self.reasons,
            "parsed": self.parsed,
            "tokens": self.tokens[:40],
            "needs_bound_confirmation": self.needs_bound_confirmation,
        }


def command_token(command: str) -> str:
    """The short token that binds an approval to one exact command (spec 5.3)."""
    return hashlib.sha256(command.strip().encode("utf-8", "replace")).hexdigest()[:12]


def classify(command: str) -> RiskReport:
    """Classify a command before it is typed.

    Unparseable input is L3: an unbalanced quote or a substitution this parser cannot follow
    is a command whose effects cannot be predicted, and predicting them is the whole point.
    """
    text = (command or "").strip()
    if not text:
        return RiskReport(L0, ["empty command"], True, [])

    reasons: list[str] = []
    level = L1

    for pattern, why in _DANGEROUS:
        if pattern.search(text):
            reasons.append(why)
            level = L3

    try:
        tokens = shlex.split(text, posix=True)
        parsed = True
    except ValueError as exc:
        tokens = []
        parsed = False
        reasons.append(f"could not be parsed ({exc}); its effects cannot be predicted")
        level = L3

    # ``$(`` runs a command this parser cannot see into. Arithmetic ``$((`` does not, so it
    # is excluded deliberately rather than treated as opaque.
    if re.search(r"\$\((?!\()", text) or "`" in text:
        reasons.append("contains a command substitution the parser cannot evaluate")
        level = L3
    if len(set(text) & set(";&|")) and any(op in text for op in ("&&", "||", ";", "|")):
        reasons.append("chains several commands; each part is judged as written, not as intended")
        level = max(level, L2)

    first = tokens[0].lower() if tokens and parsed else ""
    if first in {"rm", "del", "erase", "rmdir", "unlink"}:
        reasons.append(f"{first} destroys data")
        level = L3
    elif first in {"shutdown", "reboot", "poweroff", "halt"}:
        reasons.append(f"{first} changes the machine's power state")
        level = L3
    elif first in {"dd", "mkfs", "fdisk", "diskpart", "format"}:
        reasons.append(f"{first} writes to raw devices")
        level = L3
    elif first in READ_ONLY_TOKENS and level < L2 and not any(ch in text for ch in (">", "|")):
        # Read-only never *lowers* a level: ``echo $(curl | sh)`` starts with a harmless word
        # and is still irreversible, which is exactly the case a downgrade would miss.
        reasons.append(f"{first} only reads on its own; nothing here escalates it")
    elif first in WRITING_TOKENS and level < L2:
        reasons.append(f"{first} may change files or state")
        level = max(level, L2)
    elif first and level < L2:
        reasons.append(f"{first} is not a known read-only command")
        level = max(level, L2)

    return RiskReport(level, reasons, parsed, tokens)


# --------------------------------------------------------------------------------------
# Output: numbered lines, bounded, hashed
# --------------------------------------------------------------------------------------

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")


def strip_ansi(text: str) -> str:
    """Remove escape sequences for matching and for showing a model, never for the display."""
    return _ANSI.sub("", text).replace("\r", "")


class OutputBuffer:
    """A bounded, numbered log of what a session printed.

    ``total_lines`` keeps counting after the oldest lines fall out of the ring, so a line
    number a model was given still means the same line later, and an old range is reported
    as dropped rather than silently returning the wrong text.
    """

    def __init__(self, capacity: int = 20_000) -> None:
        self.capacity = max(1000, capacity)
        self.lines: list[str] = []
        self.total_lines = 0
        self.dropped = 0
        self.bytes = 0
        self._hash = hashlib.sha256()
        self._partial = ""
        self.version = 0

    # -- writing -----------------------------------------------------------------------
    def feed(self, chunk: str) -> None:
        self.bytes += len(chunk.encode("utf-8", "replace"))
        self._hash.update(chunk.encode("utf-8", "replace"))
        text = self._partial + chunk
        parts = re.split(r"\r\n|\n|\r", text)
        self._partial = parts.pop() if parts else ""
        for part in parts:
            self.lines.append(strip_ansi(part))
            self.total_lines += 1
        if len(self.lines) > self.capacity:
            excess = len(self.lines) - self.capacity
            del self.lines[:excess]
            self.dropped += excess
        self.version += 1

    def flush(self) -> None:
        if self._partial:
            self.lines.append(strip_ansi(self._partial))
            self.total_lines += 1
            self._partial = ""
            self.version += 1

    # -- reading -----------------------------------------------------------------------
    @property
    def digest(self) -> str:
        return self._hash.hexdigest()[:16]

    @property
    def first_line_number(self) -> int:
        return self.dropped + 1

    def tail(self, count: int = 50) -> list[str]:
        count = max(0, min(int(count), self.capacity))
        return self.lines[-count:] if count else []

    def range(self, start: int, end: int = 0) -> dict[str, Any]:
        """Lines ``start``..``end`` (1-based, inclusive), with an honest gap report."""
        start = max(1, int(start))
        end = int(end) if end else self.total_lines
        if start < self.first_line_number:
            return {
                "error": "those lines have left the buffer",
                "requested_from": start,
                "earliest_available": self.first_line_number,
                "total_lines": self.total_lines,
            }
        offset = start - self.first_line_number
        slice_ = self.lines[offset : offset + max(0, end - start + 1)]
        return {
            "start": start,
            "end": start + len(slice_) - 1,
            "lines": slice_,
            "total_lines": self.total_lines,
        }

    def search(self, pattern: str, limit: int = 50) -> dict[str, Any]:
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            return {"error": f"bad pattern: {exc}"}
        hits: list[dict[str, Any]] = []
        for index, line in enumerate(self.lines):
            if regex.search(line):
                hits.append({"line": self.first_line_number + index, "text": line[:400]})
                if len(hits) >= limit:
                    break
        return {"pattern": pattern, "hits": hits, "count": len(hits), "total_lines": self.total_lines}

    def text(self, limit: int = 12_000) -> str:
        """The buffered text *including the unfinished last line*.

        A shell prompt arrives without a trailing newline, so excluding the partial line would
        make readiness detection blind to exactly the thing it is looking for.
        """
        blob = "\n".join(self.lines)
        if self._partial:
            blob = f"{blob}\n{strip_ansi(self._partial)}" if blob else strip_ansi(self._partial)
        return blob if len(blob) <= limit else blob[-limit:]

    def to_json(self) -> dict[str, Any]:
        return {
            "total_lines": self.total_lines,
            "buffered": len(self.lines),
            "dropped": self.dropped,
            "bytes": self.bytes,
            "digest": self.digest,
        }


# --------------------------------------------------------------------------------------
# Shell integration markers (OSC 133) and directory reporting (OSC 9)
# --------------------------------------------------------------------------------------

#: ``ESC ] 133 ; <A|B|C|D> [; value] BEL`` -- the prompt/command protocol semantic prompts use.
#: A: prompt starts, B: prompt ends (input begins), C: command starts running, D: command
#: finished; the value of D is the exit code. Shells that do not emit these are detected and
#: handled by prompt analysis instead, never by waiting a fixed time.
MARKER = re.compile(r"\x1b\]133;([ABCD])(?:;([^\x07\x1b]*))?(?:\x07|\x1b\\)")
#: Our own extension: the working directory after each command, so ``cd`` is observed.
CWD_MARKER = re.compile(r"\x1b\]9;cwd=([^\x07\x1b]*)(?:\x07|\x1b\\)")

BEL = "\x07"
ESC = "\x1b"


def parse_markers(chunk: str) -> tuple[list[tuple[str, str]], str]:
    """Split markers out of a raw chunk: ([(kind, value)], remaining text)."""
    events: list[tuple[str, str]] = []
    for match in MARKER.finditer(chunk):
        events.append((match.group(1), (match.group(2) or "").strip()))
    for match in CWD_MARKER.finditer(chunk):
        events.append(("CWD", match.group(1).strip()))
    cleaned = CWD_MARKER.sub("", MARKER.sub("", chunk))
    return events, cleaned


def hook_for(shell_id: str) -> str:
    """The shell-integration script for one shell, or "" when markers are unavailable.

    The hooks are deliberately small: emit A when the prompt is drawn, B when input begins,
    C before a command runs, D with the exit code after it, plus the working directory. They
    preserve the user's own prompt and history behaviour by appending, not replacing.
    """
    if shell_id in ("bash", "sh"):
        return (
            "__orb_pre() { printf '\\033]133;C\\007'; };\n"
            "__orb_post() { local s=$?; printf '\\033]133;D;%s\\007\\033]9;cwd=%s\\007' \"$s\" \"$PWD\"; };\n"
            "PS1='\\[\\033]133;A\\007\\]'$PS1'\\[\\033]133;B\\007\\]';\n"
            "PROMPT_COMMAND=\"__orb_post${PROMPT_COMMAND:+; $PROMPT_COMMAND}\";\n"
            "trap __orb_pre DEBUG;\n"
        )
    if shell_id == "zsh":
        return (
            "__orb_precmd() { printf '\\033]133;D;%s\\007\\033]9;cwd=%s\\007' \"$?\" \"$PWD\"; };\n"
            "precmd_functions+=(__orb_precmd);\n"
            "__orb_preexec() { printf '\\033]133;C\\007'; };\n"
            "preexec_functions+=(__orb_preexec);\n"
            "PROMPT='%{\\033]133;A\\007%}'$PROMPT'%{\\033]133;B\\007%}';\n"
        )
    if shell_id == "fish":
        return (
            "function __orb_post --on-event fish_prompt; printf '\\e]133;D;%s\\e\\\\' $status; printf '\\e]9;cwd=%s\\e\\\\' (pwd); end;\n"
            "function __orb_pre --on-event fish_preexec; printf '\\e]133;C\\e\\\\'; end;\n"
        )
    if shell_id in ("powershell", "pwsh"):
        return (
            "function prompt {\n"
            "  $code = if ($global:LASTEXITCODE -ne $null) { $global:LASTEXITCODE } else { 0 }\n"
            "  Write-Host -NoNewline ([char]27 + ']133;D;' + $code + [char]7)\n"
            "  Write-Host -NoNewline ([char]27 + ']9;cwd=' + (Get-Location).Path + [char]7)\n"
            "  Write-Host -NoNewline ([char]27 + ']133;A' + [char]7)\n"
            "  'PS> '\n"
            "}\n"
        )
    return ""


#: A prompt nobody configured: used only when the shell has no markers. Deliberately narrow,
#: because a false "ready" would type into a running command.
FALLBACK_PROMPT = re.compile(r"(?:^|\n)[\w.@:/~\\()\[\]-]{0,120}[$#%>\u276f]\s?$")

#: Things that mean the session is waiting for a human, never for us. Password prompts are
#: answered by the owner, not read by the AI (spec 9.5).
INPUT_PROMPTS = re.compile(
    r"(password|passphrase|\[sudo\] password|continue\? \[y/n\]|\(y/n\)|\(yes/no\)|are you sure|press any key|overwrite\?)",
    re.IGNORECASE,
)

#: Output that means a command failed even when the exit code said zero. This is the whole
#: reason exit codes are not proof: many tools print an error and still exit 0.
FAILURE_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"No such file or directory", "missing path"),
    (r"cannot find (the )?(path|file)", "missing path"),
    (r"is not recognized as an internal or external command", "unknown command"),
    (r"command not found", "unknown command"),
    (r"Permission denied|Access is denied|EACCES", "permission refused"),
    (r"Traceback \(most recent call last\)", "unhandled exception"),
    (r"\berror\b[: ]", "reported an error"),
    (r"fatal:", "fatal error"),
    (r"failed to ", "reported a failure"),
)
_FAILURES = tuple((re.compile(pattern, re.IGNORECASE), why) for pattern, why in FAILURE_PATTERNS)


class TerminalError(RuntimeError):
    """A terminal action failed; ``evidence`` says what the session looked like."""

    def __init__(self, message: str, evidence: dict[str, Any] | None = None):
        super().__init__(message)
        self.evidence = evidence or {}


@dataclass(slots=True)
class CommandResult:
    """What a command did, as observed rather than as assumed."""

    command: str
    session_id: str
    exit_code: int | None
    duration: float
    cwd_before: str
    cwd_after: str
    first_line: int
    last_line: int
    output: list[str]
    output_digest: str
    verified: bool
    verification: str
    risk: RiskReport
    echo_verified: bool
    timed_out: bool = False
    needs_user_input: bool = False
    failure_signals: list[str] = field(default_factory=list)
    markers: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "session_id": self.session_id,
            "exit_code": self.exit_code,
            "duration": round(self.duration, 3),
            "cwd_before": self.cwd_before,
            "cwd_after": self.cwd_after,
            "lines": [self.first_line, self.last_line],
            "output": self.output,
            "output_digest": self.output_digest,
            "verified": self.verified,
            "verification": self.verification,
            "risk": self.risk.to_json(),
            "echo_verified": self.echo_verified,
            "timed_out": self.timed_out,
            "needs_user_input": self.needs_user_input,
            "failure_signals": self.failure_signals,
            "markers": self.markers,
        }


def failure_signals(text: str) -> list[str]:
    """Failure phrases in some output, ignoring the harmless uses of the word "error"."""
    found: list[str] = []
    for pattern, why in _FAILURES:
        for match in pattern.finditer(text):
            start = max(0, match.start() - 40)
            context = text[start : match.end() + 40].lower()
            if "0 error" in context or "no error" in context or "errors: 0" in context:
                continue
            if why not in found:
                found.append(why)
            break
    return found


# --------------------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------------------


def _posix_pty() -> tuple[int, int]:
    import pty  # imported here so the module imports on Windows

    return pty.openpty()


@dataclass
class Session:
    """One owned shell, with its own buffer, markers and lock.

    ``mode`` is ``pty`` when the platform gave us a real terminal and ``pipe`` when it did
    not. In pipe mode ``supports_tty`` is false: commands run, output is captured, echo cannot
    be verified and interactive programs will not work. That is reported, never smoothed over.
    """

    session_id: str
    profile: ShellProfile
    agent_id: str = "system"
    cwd: str = ""
    mode: str = "pipe"
    buffer: OutputBuffer = field(default_factory=OutputBuffer)
    proc: Any = None
    master_fd: int | None = None
    reader: Any = None
    lock: Any = None
    events: list[tuple[str, str]] = field(default_factory=list)
    condition: Any = None
    cwd_current: str = ""
    last_exit: int | None = None
    markers_seen: bool = False
    alive: bool = False
    prompt_ready: bool = False
    started_at: float = 0.0
    commands: int = 0
    hook_path: str = ""

    @property
    def supports_tty(self) -> bool:
        return self.mode == "pty"

    def to_json(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "shell": self.profile.id,
            "label": self.profile.label,
            "kind": self.profile.kind,
            "agent_id": self.agent_id,
            "cwd": self.cwd_current or self.cwd,
            "mode": self.mode,
            "supports_tty": self.supports_tty,
            "markers": self.markers_seen,
            "alive": self.alive,
            "prompt_ready": self.prompt_ready,
            "last_exit": self.last_exit,
            "commands": self.commands,
            "output": self.buffer.to_json(),
            "started_at": round(self.started_at, 3),
        }

    # -- lifecycle ---------------------------------------------------------------------
    def _argv(self) -> tuple[list[str], dict[str, str]]:
        """The command line that starts this shell with integration, plus extra environment."""
        argv = list(self.profile.argv)
        extra: dict[str, str] = {}
        hook = hook_for(self.profile.id)
        if not hook:
            return argv, extra
        path = Path(self.hook_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if self.profile.id in ("bash", "sh"):
            path.write_text(hook, encoding="utf-8")
            # --rcfile replaces the user's rc, so ours sources it first.
            return [argv[0], "--rcfile", str(path), "-i"], extra
        if self.profile.id == "zsh":
            path.write_text(f"[ -f ~/.zshrc ] && source ~/.zshrc\n{hook}", encoding="utf-8")
            # zsh reads .zshrc from ZDOTDIR; pointing it at our directory keeps the user's
            # shell untouched on disk and still gives us markers.
            zdot = path.parent
            (zdot / ".zshrc").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            extra["ZDOTDIR"] = str(zdot)
            return [argv[0], "-i"], extra
        if self.profile.id == "fish":
            return [argv[0], "-C", hook], extra
        if self.profile.id in ("powershell", "pwsh"):
            path.write_text(hook, encoding="utf-8")
            return [argv[0], *argv[1:], "-NoExit", "-Command", f". '{path}'"], extra
        return argv, extra

    def open(self, env: dict[str, str] | None = None, columns: int = 120, rows: int = 32) -> "Session":
        import subprocess
        import threading
        import time

        self.lock = threading.RLock()
        self.condition = threading.Condition()
        self.started_at = time.time()
        argv, extra_env = self._argv()
        run_env = {**os.environ, **extra_env, **(env or {})}
        run_env.setdefault("TERM", "xterm-256color")
        run_env.setdefault("PYTHONIOENCODING", "utf-8")

        if os.name != "nt":
            try:
                master, slave = _posix_pty()
                import fcntl
                import struct
                import termios

                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
                self.proc = subprocess.Popen(
                    argv,
                    stdin=slave,
                    stdout=slave,
                    stderr=slave,
                    cwd=self.cwd or None,
                    env=run_env,
                    start_new_session=True,
                    close_fds=True,
                )
                os.close(slave)
                self.master_fd = master
                self.mode = "pty"
            except (OSError, ImportError):
                self.master_fd = None
        if self.master_fd is None:
            self.proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=self.cwd or None,
                env=run_env,
                bufsize=0,
                start_new_session=(os.name != "nt"),
            )
            self.mode = "pipe"

        self.alive = True
        self.reader = threading.Thread(target=self._read_loop, name=f"term-{self.session_id}", daemon=True)
        self.reader.start()
        return self

    def _read_loop(self) -> None:
        """The only reader. Everything else watches the buffer and the event list."""
        import os as _os

        while True:
            try:
                if self.master_fd is not None:
                    chunk = _os.read(self.master_fd, 8192)
                else:
                    chunk = _os.read(self.proc.stdout.fileno(), 8192)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            text = chunk.decode("utf-8", "replace")
            events, cleaned = parse_markers(text)
            self.buffer.feed(cleaned)
            if events:
                with self.condition:
                    for kind, value in events:
                        if kind == "A":
                            self.prompt_ready = True
                            self.markers_seen = True
                        elif kind in ("B", "C"):
                            self.markers_seen = True
                            if kind == "C":
                                self.prompt_ready = False
                        elif kind == "D":
                            self.markers_seen = True
                            self.prompt_ready = True
                            try:
                                self.last_exit = int(value or 0)
                            except ValueError:
                                self.last_exit = None
                        elif kind == "CWD":
                            self.cwd_current = value
                        self.events.append((kind, value))
                    self.condition.notify_all()
            else:
                with self.condition:
                    self.condition.notify_all()
        self.buffer.flush()
        with self.condition:
            self.alive = False
            self.condition.notify_all()

    # -- input -------------------------------------------------------------------------
    def _write(self, data: str) -> None:
        if not self.alive or self.proc is None:
            raise TerminalError("session is not running", {"session_id": self.session_id})
        payload = data.encode("utf-8", "replace")
        try:
            if self.master_fd is not None:
                os.write(self.master_fd, payload)
            else:
                self.proc.stdin.write(payload)
                self.proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise TerminalError(f"could not write to the session: {exc}", {"session_id": self.session_id}) from exc

    def type_text(self, text: str, cadence: float = 0.0) -> None:
        """Type into the terminal. ``cadence`` is a deliberate human speed, not a wait."""
        import time

        for char in text:
            self._write(char)
            if cadence:
                time.sleep(cadence)

    def wait_until(self, predicate: Any, timeout: float = 10.0) -> bool:
        """Wait for a real condition on the buffer or the marker list. Never a fixed sleep.

        ``predicate`` is called with the session; the condition variable is notified by the
        reader as bytes arrive, so this returns as soon as the shell has actually done it.
        """
        import time

        deadline = time.monotonic() + max(0.0, timeout)
        with self.condition:
            while True:
                if predicate(self):
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self.alive:
                    return predicate(self)
                self.condition.wait(min(0.1, remaining))

    def markers_since(self, index: int) -> list[tuple[str, str]]:
        with self.condition:
            return list(self.events[index:])

    def echo_read_line(self) -> str:
        """What the terminal believes is on the current input line."""
        current = strip_ansi(self.buffer._partial)  # noqa: SLF001 - the buffer owns the line
        if self.buffer.lines:
            return f"{self.buffer.lines[-1]}{current}"
        return current

    def verify_echo(self, text: str, timeout: float = 2.0) -> tuple[bool, str]:
        """Compare what was typed with what the terminal echoed back (spec 9.5).

        Returns ``(matched, reason)``. A password prompt is reported as such and never
        treated as a mismatch to retry: the right answer there is the owner's, not ours.
        """
        wanted = strip_ansi(text).rstrip()
        if not self.supports_tty:
            return False, "the session has no pty here, so the terminal cannot echo; not verified"
        matched = self.wait_until(lambda s: wanted and wanted in s.echo_read_line(), timeout=timeout)
        if matched:
            return True, "read back from the terminal echo"
        line = self.echo_read_line()
        if INPUT_PROMPTS.search(strip_ansi(self.buffer.text(2000))[-400:]):
            return False, "the session is waiting for a human (password or confirmation prompt)"
        return False, f"echo did not match; the line reads {line[-80:]!r}"

    def clear_line(self) -> None:
        """Erase the pending input line without executing it (Ctrl+U on Unix, Esc on Windows)."""
        self._write("\x15" if os.name != "nt" else "\x1b")

    def send_key(self, key: str) -> None:
        """Send one named key or control sequence."""
        sequences = {
            "enter": "\r",
            "tab": "\t",
            "escape": "\x1b",
            "backspace": "\x7f",
            "up": "\x1b[A",
            "down": "\x1b[B",
            "right": "\x1b[C",
            "left": "\x1b[D",
            "ctrl-c": "\x03",
            "ctrl-d": "\x04",
            "ctrl-z": "\x1a",
            "ctrl-l": "\x0c",
            "ctrl-u": "\x15",
        }
        sequence = sequences.get(key.lower().strip())
        if sequence is None and len(key) == 1:
            sequence = key
        if sequence is None:
            raise TerminalError(f"unknown key {key!r}", {"supported": sorted(sequences)})
        self._write(sequence)

    def interrupt(self) -> dict[str, Any]:
        """Ctrl+C, then report whether the shell came back to a prompt."""
        self._write("\x03")
        ready = self.wait_until(lambda s: s.prompt_ready, timeout=3.0)
        return {"sent": "ctrl-c", "prompt_ready": ready, "markers": self.markers_seen}

    def wait_prompt(self, timeout: float = 10.0) -> bool:
        """Ready for input: a prompt marker, or (marker-less shells) a recognisable prompt.

        The predicate re-checks whether markers have appeared, because the first prompt may
        arrive in the same chunk that installs the integration: deciding once at entry would
        fall back to guessing for the rest of the session's life.
        """
        def ready(session: "Session") -> bool:
            if session.markers_seen:
                return session.prompt_ready
            return bool(FALLBACK_PROMPT.search(session.buffer.text(4000)))

        return self.wait_until(ready, timeout=timeout)

    # -- commands ----------------------------------------------------------------------
    def run(
        self,
        command: str,
        timeout: float = 120.0,
        cadence: float = 0.0,
        idle_quiet: float = 1.2,
        risk: RiskReport | None = None,
    ) -> CommandResult:
        """Type a command, prove the line before Enter, then observe it finish.

        Completion is decided by, in order: the shell's D marker (exit code included), the
        prompt reappearing, or — only for marker-less sessions — output going quiet *and* a
        prompt-looking line. Nothing here waits a fixed time to decide that a command ended.
        """
        import time

        report = risk or classify(command)
        if not self.alive:
            raise TerminalError("session is not running", self.to_json())
        with self.lock:
            self.wait_prompt(timeout=5.0)
            cwd_before = self.cwd_current or self.cwd
            start_line = self.buffer.total_lines + 1
            with self.condition:
                marker_index = len(self.events)
            started = time.monotonic()

            self.type_text(command, cadence)
            echo_ok, echo_reason = self.verify_echo(command, timeout=2.0 + cadence * len(command))
            if not echo_ok:
                if "human" in echo_reason:
                    return self._result(
                        command, start_line, time.monotonic() - started, cwd_before,
                        report, False, echo_reason, timed_out=False, needs_input=True,
                    )
                # Clear and retype once: a swallowed keystroke must never become a command.
                self.clear_line()
                self.type_text(command, cadence)
                echo_ok, echo_reason = self.verify_echo(command, timeout=2.0)
                if not echo_ok:
                    self.clear_line()
                    raise TerminalError(
                        f"refusing to press Enter: the typed line could not be verified ({echo_reason})",
                        {"command": command, "session_id": self.session_id, "line": self.echo_read_line()[-200:]},
                    )

            self._write("\r")
            finished, reason, exit_code, needs_input = self._await_completion(marker_index, timeout, idle_quiet)
            duration = time.monotonic() - started
            if not finished:
                # Cancel the way a human would: Ctrl+C first, and only then the process tree.
                reason = f"{reason}; {self._cancel()}" if not needs_input else reason
                return self._result(command, start_line, duration, cwd_before, report, False, reason,
                                    timed_out=not needs_input, needs_input=needs_input)
            return self._result(command, start_line, duration, cwd_before, report, True, reason,
                                needs_input=needs_input, echo_ok=echo_ok)

    def _await_completion(
        self, marker_index: int, timeout: float, idle_quiet: float
    ) -> tuple[bool, str, int | None, bool]:
        """Wait for the command to end and say why we believe it did."""
        import time

        deadline = time.monotonic() + max(1.0, timeout)
        quiet_since: float | None = None
        last_version = -1
        while time.monotonic() < deadline:
            events = self.markers_since(marker_index)
            for kind, value in events:
                if kind == "D":
                    tail = self.buffer.text(2000)
                    if INPUT_PROMPTS.search(tail[-400:]):
                        return True, "finished, but the session is now waiting for a human", None, True
                    return True, "shell reported completion (OSC 133 D)", self.last_exit, False
            tail = strip_ansi(self.buffer.text(1200))
            if INPUT_PROMPTS.search(tail[-300:]) and FALLBACK_PROMPT.search(tail[-120:]):
                return True, "finished, but the session is now waiting for a human", None, True
            if not self.markers_seen:
                if self.buffer.version != last_version:
                    last_version = self.buffer.version
                    quiet_since = time.monotonic()
                elif quiet_since and time.monotonic() - quiet_since >= idle_quiet:
                    if FALLBACK_PROMPT.search(tail):
                        return True, "no markers: output went quiet on a prompt-looking line", None, False
                    return False, "no markers and no prompt: completion could not be observed", None, False
            time.sleep(0.02)
        return False, f"did not finish within {timeout}s", None, False

    def _result(
        self,
        command: str,
        start_line: int,
        duration: float,
        cwd_before: str,
        risk: RiskReport,
        verified: bool,
        verification: str,
        timed_out: bool = False,
        needs_input: bool = False,
        echo_ok: bool = False,
    ) -> CommandResult:
        self.buffer.flush()
        first = max(start_line, self.buffer.first_line_number)
        last = self.buffer.total_lines
        captured = self.buffer.range(first, last) if last >= first else {"lines": []}
        lines = list(captured.get("lines", []))
        text = "\n".join(lines)
        signals = failure_signals(text)
        digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]
        self.commands += 1
        return CommandResult(
            command=command,
            session_id=self.session_id,
            exit_code=self.last_exit,
            duration=duration,
            cwd_before=cwd_before,
            cwd_after=self.cwd_current or cwd_before,
            first_line=first,
            last_line=last,
            output=lines,
            output_digest=digest,
            verified=verified,
            verification=verification,
            risk=risk,
            echo_verified=echo_ok,
            timed_out=timed_out,
            needs_user_input=needs_input,
            failure_signals=signals,
            markers=self.markers_seen,
        )

    # -- teardown ----------------------------------------------------------------------
    def resize(self, columns: int, rows: int) -> dict[str, Any]:
        """Resize the terminal the way a window would, so full-screen programs reflow."""
        if self.master_fd is None:
            return {"resized": False, "reason": "pipes have no size; this session is not a tty"}
        try:
            import fcntl
            import struct
            import termios

            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
            return {"resized": True, "columns": columns, "rows": rows}
        except (OSError, ImportError) as exc:
            return {"resized": False, "reason": str(exc)}

    def _cancel(self) -> str:
        """Stop a running command: Ctrl+C, then the tree only if the shell did not come back."""
        send = getattr(self, "_write", None)
        if send is None:
            return "nothing to cancel"
        try:
            self._write("\x03")
        except TerminalError:
            return "the session was already gone"
        if self.wait_until(lambda s: s.prompt_ready, timeout=1.5):
            return "cancelled with ctrl-c and the prompt came back"
        self._kill_tree()
        self.alive = False
        return "ctrl-c did not return a prompt, so the process tree was terminated"

    def _kill_tree(self) -> None:
        """Stop this session's process tree, and its children, without touching anything else."""
        import signal
        import subprocess

        proc = self.proc
        if proc is None:
            return
        try:
            if os.name != "nt":
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            else:  # pragma: no cover - Windows only
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
                proc.terminate()
        except (ProcessLookupError, PermissionError, OSError):
            pass

    def close(self, grace: float = 2.0) -> dict[str, Any]:
        """End the session: ask politely, then make sure."""
        import time

        was_alive = self.alive
        if was_alive:
            try:
                self._write("exit\r")
            except TerminalError:
                pass
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline and self.alive:
                time.sleep(0.05)
        if self.alive:
            self._kill_tree()
        try:
            if self.reader is not None:
                self.reader.join(timeout=1.0)
        except RuntimeError:
            pass
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None
        self.alive = False
        return {"session_id": self.session_id, "closed": True, "was_alive": was_alive, "commands": self.commands}


def _new_session(
    session_id: str,
    profile: ShellProfile,
    agent_id: str,
    cwd: str,
    hook_dir: Any,
    capacity: int,
) -> Session:
    hook_path = str(hook_dir / f"orbynth-{profile.id}-{session_id}.rc")
    return Session(
        session_id=session_id,
        profile=profile,
        agent_id=agent_id,
        cwd=cwd or os.getcwd(),
        buffer=OutputBuffer(capacity),
        hook_path=hook_path,
    )


class TerminalEngine:
    """Owns every AI-started shell on this machine, one lock each (spec 9.5).

    Sessions are per-agent: a shared session needs an explicit lock, which is what stops two
    agents from typing over each other in the same shell. Closing the engine closes every
    session, so nothing outlives the app that started it.
    """

    def __init__(self, home: Any = None, buffer_lines: int = 20_000) -> None:
        import threading

        self._sessions: dict[str, Session] = {}
        self._lock = threading.RLock()
        self._counter = 0
        self.buffer_lines = buffer_lines
        base = Path(home) if home is not None else Path(os.environ.get("ORBYNTH_HOME", ".")).expanduser()
        self.hook_dir = base / "terminal"

    # -- lifecycle ---------------------------------------------------------------------
    def shell_list(self) -> dict[str, Any]:
        profiles = detect_shells()
        return {
            "shells": [profile.to_json() for profile in profiles],
            "default": (default_shell().id if default_shell() else ""),
            "count": len(profiles),
        }

    def open(
        self,
        shell_id: str = "",
        agent_id: str = "system",
        cwd: str = "",
        env: dict[str, str] | None = None,
        name: str = "",
    ) -> Session:
        profiles = shell_profiles()
        profile = profiles.get(shell_id) if shell_id else default_shell()
        if profile is None:
            raise TerminalError(
                f"no such shell on this machine: {shell_id!r}",
                {"available": sorted(profiles), "hint": "call term_shells for the real list"},
            )
        if cwd and not os.path.isdir(cwd):
            raise TerminalError(f"no such directory: {cwd}", {"cwd": cwd})
        with self._lock:
            self._counter += 1
            prefix = (name or profile.id)[:16].replace(" ", "_")
            session_id = f"{prefix}-{self._counter}"
            session = _new_session(session_id, profile, agent_id, cwd, self.hook_dir, self.buffer_lines)
            session.open(env=env)
            self._sessions[session_id] = session
        return session

    def get(self, session_id: str) -> Session:
        session = self._sessions.get(session_id)
        if session is None:
            raise TerminalError(
                f"no such session: {session_id!r}",
                {"available": sorted(self._sessions), "hint": "call term_list"},
            )
        return session

    def sessions(self, agent_id: str = "") -> list[dict[str, Any]]:
        with self._lock:
            values = list(self._sessions.values())
        return [s.to_json() for s in values if not agent_id or s.agent_id == agent_id]

    def close(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is None:
            raise TerminalError(f"no such session: {session_id!r}", {"available": sorted(self._sessions)})
        return session.close()

    def close_all(self) -> list[dict[str, Any]]:
        with self._lock:
            ids = list(self._sessions)
        return [self.close(session_id) for session_id in ids]