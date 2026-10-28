"""Every helper process goes through here (contract 22.1).

The supervisor only guarantees "nothing outlives the app" if **nothing escapes it**. Drivers
that call ``subprocess.Popen`` directly are the leak: a WhatsApp sidecar, an automation
browser or an OCR worker started that way keeps running after ORBYNTH dies, with nobody
watching it — the exact failure the supervisor exists to prevent.

Two kinds of process, deliberately treated differently:

* **Helpers we own** (sidecar, automation browser, OCR/audio workers) are launched through the
  supervisor: registered before they run, tied to a process group, killed with the app,
  restarted if they crash, reconciled at startup.
* **Applications the user asked to open** are *not* killed when ORBYNTH exits. Closing
  someone's Word document because they quit the assistant would be a bug, not a feature.

``managed_popen`` needs the pipe-based stdio mode for RPC helpers (a JSON-RPC sidecar cannot
have its stdout redirected into a log file), which is why the supervisor supports both.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ..events import LOG, EventBus
from .supervisor import Job, Supervisor

_supervisor: Supervisor | None = None
_supervisor_bus: EventBus | None = None


def set_supervisor(supervisor: Supervisor | None, bus: EventBus | None = None) -> None:
    """Register the process book for this run. Called once, by the stack assembly."""
    global _supervisor, _supervisor_bus
    _supervisor = supervisor
    _supervisor_bus = bus


def current_supervisor() -> Supervisor | None:
    return _supervisor


@dataclass
class Launched:
    """A process we started, with its handle and (when supervised) its job record."""

    process: subprocess.Popen[bytes] | Any
    job: Job | None = None
    supervised: bool = False
    #: The supervisor that owns this job. Held rather than looked up at stop time: the module
    #: global can point at a *different* supervisor by then, and stopping a job through the
    #: wrong process book means the real child is left running under a stale record.
    supervisor: Supervisor | None = None

    @property
    def pid(self) -> int:
        return int(getattr(self.process, "pid", 0) or (self.job.pid if self.job else 0))

    def poll(self) -> int | None:
        return self.process.poll() if hasattr(self.process, "poll") else None

    def terminate(self, force: bool = False) -> None:
        if self.job is not None and self.supervisor is not None:
            self.supervisor.stop(self.job.job_id, force=force, reason="requested by owner driver")
            return
        try:
            self.process.terminate()
        except Exception:
            pass


def managed_popen(
    argv: Sequence[str],
    name: str,
    kind: str = "helper",
    pipes: bool = False,
    restart: str = "on-failure",
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    bus: EventBus | None = None,
) -> Launched:
    """Start a helper under supervision, or plainly when there is no supervisor.

    ``pipes=True`` keeps stdin/stdout/stderr connected to the caller (for JSON-RPC sidecars).
    Falling back to an unsupervised process is allowed, but it is announced: silently starting
    something that may outlive the app is how orphans are created.
    """
    supervisor = _supervisor
    if supervisor is not None and not supervisor.shutting_down:
        job = supervisor.spawn(
            list(argv),
            name=name,
            kind=kind,
            owned=True,
            restart=restart,
            cwd=cwd,
            env=dict(env or {}),
            log_name=name,
            pipes=pipes,
        )
        process = supervisor.process(job.job_id)
        return Launched(process=process, job=job, supervised=True, supervisor=supervisor)

    creation: dict[str, Any] = {}
    if os.name == "nt":  # pragma: no cover - windows only
        creation["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        creation["start_new_session"] = True
    if pipes:
        process = subprocess.Popen(
            list(argv),
            cwd=str(cwd) if cwd else None,
            env={**os.environ, **(env or {})},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            **creation,
        )
    else:
        process = subprocess.Popen(
            list(argv),
            cwd=str(cwd) if cwd else None,
            env={**os.environ, **(env or {})},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **creation,
        )
    (bus or _supervisor_bus or EventBus()).emit(
        LOG,
        source="spawn",
        severity="warning",
        message=(
            f"helper {name!r} was started without a supervisor, so it may outlive ORBYNTH. "
            "Start the app through `orbynth run`/`orbynth hud` (which own a supervisor) to fix this."
        ),
        pid=process.pid,
        kind=kind,
    )
    return Launched(process=process, job=None, supervised=False)


def supervised() -> bool:
    """True when this process has a supervisor, so helpers die with the app."""
    return _supervisor is not None and not _supervisor.shutting_down


def audit_orphans(expected_names: Sequence[str] = ()) -> dict[str, Any]:  # pragma: no cover - diagnostic
    """Report helper processes on this machine that look like ours but have no supervisor.

    Deliberately narrow: it only looks for the exact names passed in, never pattern-kills
    anything, because a name pattern can match the agent process itself.
    """
    found: list[dict[str, Any]] = []
    for name in expected_names:
        try:
            if sys.platform.startswith("linux"):
                import glob

                for path in glob.glob("/proc/[0-9]*/cmdline"):
                    try:
                        raw = open(path, "rb").read().decode("utf-8", "replace")
                    except OSError:
                        continue
                    if name in raw:
                        pid = int(path.split("/")[2])
                        if pid != os.getpid():
                            found.append({"name": name, "pid": pid})
        except Exception:
            continue
    return {"supervised": supervised(), "suspicious": found}