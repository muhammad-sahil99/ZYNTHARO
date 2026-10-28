"""Process architecture (contract 22.1).

The first design said "one binary". It cannot be one binary: the watchdog, the sensor and
audio helpers, sandboxes and the approval surface each need their own process. What replaces
"one binary" is a small package of cooperating processes under this supervisor.

Two guarantees make that safe:

1. **Ownership.** Everything the AI starts is registered here before it runs, with the
   process group (POSIX) or process-group flag (Windows) it was launched with. The
   supervisor is the only thing that kills it, and it kills the whole group, not one child.
2. **No orphans.** Jobs die with the supervisor. On startup the journal is replayed: any
   job still marked running whose process is gone is marked orphaned; any that is somehow
   still alive while its supervisor is not is terminated, because nothing is watching it.
   Temporary workspaces from interrupted jobs are cleaned at the same time.

A job whose process silently dies (a helper crashing in a game overlay, say) is restarted
up to ``max_restarts`` inside ``restart_window``; past that it is left dead and reported,
because a crash loop is a bug to surface, not to hide.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Sequence

from ..events import (
    LOG,
    EventBus,
    JOB_EXITED,
    JOB_ORPHANED,
    JOB_RESTARTED,
    JOB_STARTED,
    SUPERVISOR_RECONCILED,
    SUPERVISOR_STARTED,
    SUPERVISOR_STOPPED,
)

RUNNING = "running"
EXITED = "exited"
CRASHED = "crashed"
ORPHANED = "orphaned"
STOPPED = "stopped"
GAVE_UP = "gave_up"


@dataclass
class Job:
    """One supervised child process."""

    job_id: str
    name: str
    kind: str
    pid: int
    pgid: int
    argv: list[str]
    cwd: str = ""
    owned: bool = True
    restart: str = "on-failure"  # on-failure | always | never
    critical: bool = False
    workspace: str = ""
    log_path: str = ""
    env: dict[str, str] = field(default_factory=dict)
    status: str = RUNNING
    exit_code: int | None = None
    restarts: int = 0
    started_at: float = field(default_factory=time.time)
    start_token: str = ""
    last_exit_at: float = 0.0

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        # The journal is a plain-text file in the user's profile, and a helper's environment can
        # carry a provider API key. Record *which* variables were set, never their values: a
        # supervisor journal is not the place a secret should end up because a sidecar needed it.
        data["env_keys"] = sorted(self.env)
        data.pop("env", None)
        data["alive"] = _pid_alive(self.pid)
        data["age_s"] = round(time.time() - self.started_at, 1)
        return data


def _pid_alive(pid: int) -> bool:
    """Is this process still running? A zombie is *not* running for our purposes.

    ``os.kill(pid, 0)`` succeeds for an unreaped zombie, which would make the supervisor
    believe a finished helper is still alive and refuse to clean up after it. On Linux the
    state field settles it exactly; elsewhere we reap what we can and accept the remainder.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another user
    except OSError:
        return False
    if sys.platform.startswith("linux"):
        try:
            raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
            state = raw[raw.rfind(")") + 2 :].split()[0]
            return state != "Z"
        except (OSError, IndexError):
            return False
    return True


def _start_token(pid: int) -> str:
    """Best-effort identity for a pid, to defend against pid reuse.

    Linux gives an exact answer (field 22 of /proc/<pid>/stat). Elsewhere we return an empty
    string, and callers must then treat the pid as trusted only while the supervisor that
    wrote it is the one running.
    """
    if sys.platform.startswith("linux"):
        try:
            raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
            tail = raw[raw.rfind(")") + 2:].split()
            return tail[19] if len(tail) > 19 else ""
        except OSError:
            return ""
    return ""


def _terminate_tree(pid: int, pgid: int, force: bool = False, grace: float = 5.0) -> None:
    """Kill a supervised job and everything it spawned. Never pattern-matches by name."""
    if pid <= 0:
        return
    if os.name == "nt":  # pragma: no cover - windows only
        flags = ["/PID", str(pid), "/T"]
        if force:
            flags.append("/F")
        subprocess.run(["taskkill", *flags], capture_output=True, check=False)
        return
    try:
        os.killpg(pgid if pgid > 0 else pid, signal.SIGKILL if force else signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
        except OSError:
            return
    if force:
        return
    deadline = time.time() + grace
    while time.time() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(0.05)
    try:
        os.killpg(pgid if pgid > 0 else pid, signal.SIGKILL)
    except OSError:
        pass


class Supervisor:
    """Owns every process ORBYNTH starts, and cleans up after the previous session."""

    def __init__(
        self,
        home: str | os.PathLike[str],
        bus: EventBus | None = None,
        max_restarts: int = 3,
        restart_window: float = 60.0,
        poll_interval: float = 1.0,
        adopt_orphans: bool = False,
    ):
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        self.bus = bus or EventBus()
        self.max_restarts = max_restarts
        self.restart_window = restart_window
        self.poll_interval = poll_interval
        #: When True a *live* job from a previous supervisor run is adopted instead of
        #: killed. Off by default: an unwatched process is exactly the failure we are fixing.
        self.adopt_orphans = adopt_orphans
        self.jobs: dict[str, Job] = {}
        self._procs: dict[str, subprocess.Popen] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._watcher: threading.Thread | None = None
        self._lifecycle: dict[str, list[float]] = {}
        #: Identifies *this* supervisor run. Journal entries from an earlier run are orphans,
        #: even when the pid matches because the same process started a second supervisor.
        self.run_id = uuid.uuid4().hex[:12]
        self.shutting_down = False

    # -- journal -----------------------------------------------------------------------
    @property
    def journal_path(self) -> Path:
        return self.home / "jobs.json"

    def _save(self) -> None:
        data = {
            "supervisor": os.getpid(),
            "run_id": self.run_id,
            "saved_at": time.time(),
            "jobs": [j.to_json() for j in self.jobs.values()],
        }
        tmp = self.journal_path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(tmp, self.journal_path)
        except OSError:
            pass

    def _load_journal(self) -> list[dict[str, Any]]:
        if not self.journal_path.exists():
            return []
        try:
            data = json.loads(self.journal_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return list(data.get("jobs", []))

    # -- startup -----------------------------------------------------------------------
    def start(self, reconcile: bool = True) -> dict[str, Any]:
        """Open the process book. Returns the reconciliation report."""
        report = self.reconcile() if reconcile else {"checked": 0, "orphaned": [], "terminated": [], "workspaces_cleaned": []}
        self.bus.emit(SUPERVISOR_STARTED, source="supervisor", pid=os.getpid(), home=str(self.home))
        self._watcher = threading.Thread(target=self._watch_loop, name="orbynth-supervisor", daemon=True)
        self._watcher.start()
        return report

    def reconcile(self) -> dict[str, Any]:
        """Deal with whatever the last session left behind, before doing anything else."""
        orphaned: list[dict[str, Any]] = []
        terminated: list[dict[str, Any]] = []
        cleaned: list[str] = []
        adopted_ids: set[str] = set()
        entries = self._load_journal()
        journal_run_id = ""
        if self.journal_path.exists():
            try:
                journal_run_id = str(json.loads(self.journal_path.read_text(encoding="utf-8")).get("run_id", ""))
            except (OSError, json.JSONDecodeError):
                journal_run_id = ""
        for entry in entries:
            if entry.get("status") != RUNNING:
                continue
            pid = int(entry.get("pid") or 0)
            alive = _pid_alive(pid)
            token_ok = not entry.get("start_token") or entry.get("start_token") == _start_token(pid)
            if not alive:
                orphaned.append({"job_id": entry.get("job_id"), "name": entry.get("name"), "pid": pid, "reason": "process gone"})
                continue
            if journal_run_id and journal_run_id == self.run_id:
                continue  # these are our own jobs, already tracked
            if not token_ok:
                orphaned.append({"job_id": entry.get("job_id"), "name": entry.get("name"), "pid": pid, "reason": "pid reused by another process"})
                continue
            if self.adopt_orphans:
                adopted = self._adopt(entry)
                adopted_ids.add(adopted.job_id)
                continue
            _terminate_tree(pid, int(entry.get("pgid") or 0), force=True)
            terminated.append({"job_id": entry.get("job_id"), "name": entry.get("name"), "pid": pid, "reason": "left running by a previous supervisor"})
        # Temporary workspaces belong to jobs that no longer run: the process is gone (orphaned)
        # or we just killed it (terminated). A job we adopted is still live and keeps its
        # workspace. The first version cleaned only the terminated ones, which skipped exactly
        # the interrupted case -- a crashed run's scratch directories accumulated forever.
        for entry in entries:
            job_id = str(entry.get("job_id") or "")
            if job_id in adopted_ids:
                continue
            ws = entry.get("workspace") or ""
            if ws and Path(ws).exists() and Path(ws).is_dir():
                _rmtree_quiet(Path(ws))
                cleaned.append(ws)
        # Drop what is no longer alive; keep adopted jobs and anything this run still owns.
        for job_id, job in list(self.jobs.items()):
            if job.status != RUNNING or not _pid_alive(job.pid):
                self.jobs.pop(job_id, None)
                self._procs.pop(job_id, None)
        self._save()
        report = {
            "checked": len(entries),
            "orphaned": orphaned,
            "terminated": terminated,
            "workspaces_cleaned": cleaned,
            "adopted": [j.name for j in self.jobs.values()],
        }
        self.bus.emit(SUPERVISOR_RECONCILED, source="supervisor", severity="notice" if (orphaned or terminated) else "info", **report)
        return report

    def _adopt(self, entry: dict[str, Any]) -> Job:
        job = Job(
            job_id=entry.get("job_id") or uuid.uuid4().hex[:10],
            name=entry.get("name", "adopted"),
            kind=entry.get("kind", "helper"),
            pid=int(entry.get("pid") or 0),
            pgid=int(entry.get("pgid") or 0),
            argv=list(entry.get("argv") or []),
            cwd=entry.get("cwd", ""),
            restart="never",
            workspace=entry.get("workspace", ""),
            log_path=entry.get("log_path", ""),
            started_at=float(entry.get("started_at") or time.time()),
            start_token=entry.get("start_token", ""),
        )
        self.jobs[job.job_id] = job
        return job

    # -- launching ---------------------------------------------------------------------
    def spawn(
        self,
        argv: Sequence[str],
        name: str = "",
        kind: str = "helper",
        owned: bool = True,
        restart: str = "on-failure",
        critical: bool = False,
        cwd: str | os.PathLike[str] | None = None,
        env: dict[str, str] | None = None,
        workspace: str | os.PathLike[str] | None = None,
        log_name: str = "",
        pipes: bool = False,
    ) -> Job:
        """Start a child process and take responsibility for it.

        ``argv`` is a list, never a shell string: a task the AI composed must not be able to
        smuggle shell metacharacters through the supervisor.

        ``pipes=True`` keeps stdin/stdout/stderr with the caller instead of redirecting them to
        a log file. A JSON-RPC sidecar needs that; a helper does not, and a helper whose output
        goes to a log can be diagnosed after it dies.
        """
        if not argv:
            raise ValueError("spawn needs a command")
        job_id = uuid.uuid4().hex[:10]
        log_dir = self.home / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{log_name or job_id}.log"
        handle = None if pipes else open(log_path, "ab", buffering=0)
        creation: dict[str, Any] = {}
        if os.name == "nt":  # pragma: no cover - windows only
            creation["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            creation["start_new_session"] = True
        workdir = str(workspace or cwd or Path.cwd())
        Path(workdir).mkdir(parents=True, exist_ok=True)
        stdio: dict[str, Any] = (
            {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "bufsize": 0}
            if pipes
            else {"stdin": subprocess.DEVNULL, "stdout": handle, "stderr": subprocess.STDOUT}
        )
        try:
            proc = subprocess.Popen(
                list(argv),
                cwd=workdir,
                env={**os.environ, **(env or {})},
                **stdio,
                **creation,
            )
        finally:
            if handle is not None:
                handle.close()
        pgid = proc.pid
        if not os.name == "nt":
            try:
                pgid = os.getpgid(proc.pid)
            except OSError:
                pgid = proc.pid
        job = Job(
            job_id=job_id,
            name=name or Path(str(argv[0])).name or job_id,
            kind=kind,
            pid=proc.pid,
            pgid=pgid,
            argv=[str(a) for a in argv],
            cwd=workdir,
            owned=owned,
            restart=restart,
            critical=critical,
            workspace=str(workspace or ""),
            log_path=str(log_path),
            env=dict(env or {}),
            start_token=_start_token(proc.pid),
        )
        with self._lock:
            self._procs[job_id] = proc
            self.jobs[job_id] = job
        self._save()
        self.bus.emit(JOB_STARTED, source="supervisor", job_id=job_id, name=job.name, pid=job.pid, kind=kind, critical=critical)
        return job

    # -- supervision loop --------------------------------------------------------------
    def _watch_loop(self) -> None:
        while not self._stop.wait(self.poll_interval):
            try:
                self.poll()
            except Exception as exc:  # a watcher must never die of a hiccup
                self.bus.emit(LOG, source="supervisor", severity="error", message=f"watcher poll failed: {exc!r}")

    def poll(self) -> list[dict[str, Any]]:
        """Check every managed job; restart the ones whose policy says to.

        Guarded by the instance lock: the watcher thread and an explicit poll from a command
        must never both act on the same exit, or a single crash turns into a restart storm.
        """
        with self._lock:
            return self._poll_locked()

    def _poll_locked(self) -> list[dict[str, Any]]:
        changes: list[dict[str, Any]] = []
        for job in list(self.jobs.values()):
            proc = self._procs.get(job.job_id)
            if job.status != RUNNING:
                continue
            code = proc.poll() if proc is not None else (None if _pid_alive(job.pid) else 0)
            if code is None:
                continue
            job.exit_code = code
            job.last_exit_at = time.time()
            crashed = code != 0
            job.status = CRASHED if crashed else EXITED
            self.bus.emit(
                JOB_EXITED,
                source="supervisor",
                severity="warning" if crashed else "info",
                job_id=job.job_id,
                name=job.name,
                exit_code=code,
            )
            if self._should_restart(job, crashed):
                if self._restart(job):
                    changes.append({"job": job.name, "action": "restarted", "restarts": job.restarts})
                    continue
                job.status = GAVE_UP
                self.bus.emit(
                    JOB_EXITED,
                    source="supervisor",
                    severity="error",
                    job_id=job.job_id,
                    name=job.name,
                    reason=f"restart budget exhausted ({self.max_restarts} in {self.restart_window:.0f}s)",
                )
            changes.append({"job": job.name, "action": job.status, "exit_code": code})
        if changes:
            self._save()
        return changes

    def _should_restart(self, job: Job, crashed: bool) -> bool:
        if self.shutting_down or not job.owned:
            return False
        if job.restart == "never":
            return False
        if job.restart == "on-failure" and not crashed:
            return False
        history = [t for t in self._lifecycle.get(job.job_id, []) if time.time() - t <= self.restart_window]
        return len(history) < self.max_restarts

    def _restart(self, job: Job) -> bool:
        """Start the job again in place: same job id, same log, one more attempt.

        Restarting in place matters: a new job id would orphan the log, lose the restart
        history, and show up in the HUD as a different worker.
        """
        self._lifecycle.setdefault(job.job_id, []).append(time.time())
        creation: dict[str, Any] = {}
        if os.name == "nt":  # pragma: no cover - windows only
            creation["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            creation["start_new_session"] = True
        try:
            handle = open(job.log_path, "ab", buffering=0)
            try:
                proc = subprocess.Popen(
                    list(job.argv),
                    cwd=job.cwd or None,
                    env={**os.environ, **(job.env or {})},
                    stdin=subprocess.DEVNULL,
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    **creation,
                )
            finally:
                handle.close()
        except (OSError, ValueError) as exc:
            self.bus.emit(LOG, source="supervisor", severity="error", message=f"restart of {job.name} failed: {exc!r}")
            return False
        job.pid = proc.pid
        job.pgid = proc.pid
        if not os.name == "nt":
            try:
                job.pgid = os.getpgid(proc.pid)
            except OSError:
                pass
        job.start_token = _start_token(proc.pid)
        job.status = RUNNING
        job.exit_code = None
        job.started_at = time.time()
        job.restarts += 1
        self._procs[job.job_id] = proc
        self._save()
        self.bus.emit(JOB_RESTARTED, source="supervisor", severity="notice", name=job.name, restarts=job.restarts, pid=job.pid)
        return True

    # -- stopping ----------------------------------------------------------------------
    def stop(self, job_id: str, force: bool = False, reason: str = "requested") -> bool:
        job = self.jobs.get(job_id)
        if job is None:
            return False
        _terminate_tree(job.pid, job.pgid, force=force)
        job.status = STOPPED
        job.last_exit_at = time.time()
        self.bus.emit(JOB_EXITED, source="supervisor", job_id=job_id, name=job.name, reason=reason, exit_code=job.exit_code)
        self._save()
        return True

    def remove(self, job_id: str, kill: bool = True) -> None:
        job = self.jobs.get(job_id)
        if job is None:
            return
        if kill and _pid_alive(job.pid):
            _terminate_tree(job.pid, job.pgid, force=True)
        self._procs.pop(job_id, None)
        with self._lock:
            self.jobs.pop(job_id, None)
        self._save()

    def shutdown(self, grace: float = 5.0) -> dict[str, Any]:
        """Stop everything, including work the AI started. Nothing outlives this call."""
        self.shutting_down = True
        self._stop.set()
        if self._watcher is not None and self._watcher.is_alive():
            self._watcher.join(timeout=grace)
        owned = [j for j in self.jobs.values() if j.owned and _pid_alive(j.pid)]
        for job in owned:
            _terminate_tree(job.pid, job.pgid, force=False, grace=grace)
        survivors = [j.name for j in owned if _pid_alive(j.pid)]
        for job in owned:
            if _pid_alive(job.pid):
                _terminate_tree(job.pid, job.pgid, force=True)
        for job in self.jobs.values():
            if job.status == RUNNING:
                job.status = STOPPED
        self._save()
        self.bus.emit(SUPERVISOR_STOPPED, source="supervisor", stopped=[j.name for j in owned], forced=survivors)
        return {"stopped": [j.name for j in owned], "forced": survivors, "left_running": [j.name for j in self.jobs.values() if j.owned and _pid_alive(j.pid)]}

    def process(self, job_id: str) -> subprocess.Popen | None:
        """The Popen handle for a job, for piped helpers that need to talk over stdio."""
        return self._procs.get(job_id)

    # -- introspection -----------------------------------------------------------------
    def inventory(self) -> dict[str, Any]:
        return {
            "supervisor_pid": os.getpid(),
            "jobs": [j.to_json() for j in self.jobs.values()],
            "running": sum(1 for j in self.jobs.values() if j.status == RUNNING and _pid_alive(j.pid)),
            "journal": str(self.journal_path),
        }

    def logs(self, job_id: str, lines: int = 80) -> str:
        job = self.jobs.get(job_id)
        if job is None or not job.log_path or not Path(job.log_path).exists():
            return ""
        try:
            content = Path(job.log_path).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ""
        return "\n".join(content[-lines:])


def _rmtree_quiet(path: Path) -> None:
    import shutil

    try:
        shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass
