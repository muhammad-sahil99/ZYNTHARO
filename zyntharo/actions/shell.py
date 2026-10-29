"""Process and shell actions.

Shell execution is confirm-level by design: it is the widest capability in the system. It
still exists because an operative must be able to run a build, a CLI tool or a script the
user asked for — refusing would make the tool useless for real work.
"""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import time
from typing import Any

from .registry import Action, ActionContext, ActionFailed, action


@action(
    name="proc_run",
    description=(
        "Run a command and return exit code, stdout, stderr and duration. Prefers argument "
        "arrays over shell strings. Confirm-level: it can do anything the user can."
    ),
    capability="shell.exec",
    group="shell",
    danger="high",
    parameters={
        "type": "object",
        "properties": {
            "argv": {"type": "array", "items": {"type": "string"}, "description": "Command as an argument array."},
            "command": {"type": "string", "description": "Shell string (used when argv is absent)."},
            "cwd": {"type": "string"},
            "env": {"type": "object", "description": "Extra environment variables."},
            "timeout": {"type": "number", "description": "Seconds, default 120."},
            "stdin": {"type": "string"},
        },
    },
)
def proc_run(
    ctx: ActionContext,
    argv: list[str] | None = None,
    command: str = "",
    cwd: str = "",
    env: dict[str, str] | None = None,
    timeout: float = 120.0,
    stdin: str = "",
) -> Any:
    if not argv and not command:
        raise ActionFailed("either argv or command is required")
    started = time.time()
    run_env = {**os.environ, **(env or {})}
    signal_kwargs: dict[str, Any] = {}
    if os.name != "nt":
        signal_kwargs["start_new_session"] = True  # so we can kill the whole tree
    try:
        if argv:
            proc = subprocess.run(
                argv,
                cwd=cwd or None,
                env=run_env,
                input=(stdin.encode() if stdin else None),
                capture_output=True,
                timeout=timeout,
                **signal_kwargs,
            )
        else:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=cwd or None,
                env=run_env,
                input=(stdin.encode() if stdin else None),
                capture_output=True,
                timeout=timeout,
                **signal_kwargs,
            )
    except subprocess.TimeoutExpired as exc:
        raise ActionFailed(
            f"command timed out after {timeout}s",
            {"stdout": _safe(exc.stdout), "stderr": _safe(exc.stderr), "timeout": timeout},
        ) from exc
    except FileNotFoundError as exc:
        raise ActionFailed(f"executable not found: {exc}", {"argv": argv, "command": command}) from exc
    return {
        "exit_code": proc.returncode,
        "stdout": _safe(proc.stdout, 20_000),
        "stderr": _safe(proc.stderr, 8_000),
        "duration": round(time.time() - started, 3),
    }


def _safe(data: bytes | str | None, limit: int = 8_000) -> str:
    if data is None:
        return ""
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    return text if len(text) <= limit else text[:limit] + f"…(+{len(text) - limit} chars)"


@action(
    name="proc_list",
    description="List running processes (pid, name, cpu, memory, command line where readable).",
    capability="process.list",
    group="shell",
    parameters={"type": "object", "properties": {"filter": {"type": "string"}},},
)
def proc_list(ctx: ActionContext, filter: str = "") -> Any:  # noqa: A002 - matches tool schema
    rows: list[dict[str, Any]] = []
    try:
        import psutil  # type: ignore
    except Exception:
        psutil = None
    if psutil is not None:  # pragma: no cover - optional dependency
        for proc in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info", "cmdline"]):
            info = proc.info
            if filter and filter.lower() not in str(info.get("name", "")).lower():
                continue
            rows.append(
                {
                    "pid": info["pid"],
                    "name": info.get("name"),
                    "cpu": info.get("cpu_percent"),
                    "rss_mb": round((info.get("memory_info").rss if info.get("memory_info") else 0) / 1e6, 1),
                    "cmd": " ".join(info.get("cmdline") or [])[:200],
                }
            )
        rows.sort(key=lambda r: r.get("rss_mb") or 0, reverse=True)
        return {"count": len(rows), "processes": rows[:200], "source": "psutil"}
    # Portable fallback with no dependency at all.
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True).stdout.decode("utf-8", "replace")
        for line in out.splitlines():
            parts = [p.strip('"') for p in line.split('","')]
            if len(parts) >= 2 and (not filter or filter.lower() in parts[0].lower()):
                rows.append({"name": parts[0], "pid": parts[1]})
    else:
        out = subprocess.run(["ps", "-eo", "pid,pcpu,rss,comm"], capture_output=True).stdout.decode("utf-8", "replace")
        for line in out.splitlines()[1:]:
            parts = line.split(None, 3)
            if len(parts) < 4 or (filter and filter.lower() not in parts[3].lower()):
                continue
            rows.append({"pid": parts[0], "cpu": float(parts[1]), "rss_mb": round(int(parts[2]) / 1024, 1), "name": parts[3]})
    return {"count": len(rows), "processes": rows[:200], "source": "fallback"}


@action(
    name="proc_kill",
    description="Terminate a process by pid (SIGTERM, then SIGKILL after grace). Destructive.",
    capability="process.kill",
    group="shell",
    danger="high",
    parameters={
        "type": "object",
        "properties": {"pid": {"type": "integer"}, "grace": {"type": "number"}},
        "required": ["pid"],
    },
)
def proc_kill(ctx: ActionContext, pid: int, grace: float = 3.0) -> Any:
    if pid <= 0:
        raise ActionFailed(f"refusing to kill pid {pid}")
    try:
        os.kill(pid, signal.SIGTERM if os.name != "nt" else signal.SIGTERM)
    except ProcessLookupError as exc:
        raise ActionFailed(f"no such process: {pid}") from exc
    except PermissionError as exc:
        raise ActionFailed(f"permission denied killing {pid}") from exc
    deadline = time.time() + grace
    while time.time() < deadline:
        if not _alive(pid):
            return {"pid": pid, "terminated": True, "forced": False}
        time.sleep(0.1)
    if _alive(pid):  # pragma: no cover - platform dependent
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        return {"pid": pid, "terminated": not _alive(pid), "forced": True}
    return {"pid": pid, "terminated": True, "forced": False}


def _alive(pid: int) -> bool:
    if os.name == "nt":  # pragma: no cover
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


@action(
    name="proc_which",
    description="Locate an executable on PATH, and report whether a program is installed.",
    capability="process.list",
    group="shell",
    parameters={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
)
def proc_which(ctx: ActionContext, name: str) -> Any:
    found = shutil.which(name)
    return {"name": name, "path": found, "installed": bool(found), "argv_hint": shlex.quote(name)}


all_actions = lambda: [proc_run, proc_list, proc_kill, proc_which]  # noqa: E731
