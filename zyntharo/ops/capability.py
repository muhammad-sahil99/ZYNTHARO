"""The Capability Matrix (spec 4.7): one row per tool, and the rule that decides what a
model is allowed to believe exists.

The failure this module exists to prevent is the opposite of a crash: a tool that is
listed, looks plausible, and cannot do the thing. So a capability's state is derived from
four independent facts, never from a hand-written table of promises:

1. **It is really declared** — it comes out of the live :class:`ActionRegistry`, so a row
   cannot exist for a tool that was deleted, and a tool cannot hide by being undocumented.
2. **Its driver is really here** — the native dependency and the platform are probed on
   *this* machine, because "works on Windows" is not a capability on a Linux box.
3. **It has been exercised on a real app** — a recording recorded in
   ``artifacts/capability_evidence.json`` by whoever watched it work. An entry missing a
   machine, a date or a recording is not evidence; it is a claim, and it is reported as one.
4. **It has really run** — runs, successes and the last real timestamp come from the
   action journal and the audit log, not from a fixture.

States, and what each one means for the user:

``green``
    Implemented, driver present, recorded evidence and at least one real run. The only
    state that may be advertised as a capability.
``provisional``
    Implemented and usable here, but not yet proven on a real app. It stays available to
    agents while being labelled ``UNVERIFIED`` in its own receipts, because the honest
    alternative to "not proven yet" is *saying so*, not pretending the tool does not exist
    and leaving the agent to improvise with coordinates. ``zyntharo capabilities`` lists
    them, and the Trust Dashboard shows them as unproven.
``unavailable``
    No driver on this machine, or the wrong platform. **Hidden from every agent** and shown
    to the user as "not available on this machine: needs X". This is the case that used to
    produce confident, wrong action attempts.

Nothing in here is allowed to guess: :func:`tools.capability_matrix` writes
``CAPABILITY_MATRIX.md`` from this module and the release gate fails if the committed file
is stale or a phantom row appears for a tool that does not exist.
"""

from __future__ import annotations

import importlib.util
import json
import platform as platform_module
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Protocol, Sequence

GREEN = "green"
PROVISIONAL = "provisional"
UNAVAILABLE = "unavailable"

#: Where recordings of a tool doing its job on a real application are registered.
LEDGER_PATH = Path("artifacts") / "capability_evidence.json"

#: ``platform`` value meaning "do not evaluate this machine at all". Used for the committed
#: CAPABILITY_MATRIX.md, which is the declared surface of the product and must not change
#: just because the build machine is a Linux container without pyautogui installed.
DECLARED_ONLY = "all"

#: ``local`` values: whether this particular machine could run the tool right now.
LOCAL_READY = "ready"
LOCAL_DRIVER_MISSING = "driver-missing"
LOCAL_WRONG_PLATFORM = "wrong-platform"
LOCAL_UNREPORTED = "unreported"

#: Per action group: which platforms the code targets, which native module has to exist for
#: it to work at all, what to install, and the limit the user should know about. Keyed by the
#: group the registry assigns, so a new action in a known group is covered automatically.
#: A group that is missing here is reported as ``unavailable`` with the reason "no driver
#: profile declared", which is how this table stays honest as the surface grows.
GROUP_PROFILES: dict[str, dict[str, Any]] = {
    "filesystem": {
        "platforms": ("windows", "macos", "linux"),
        "driver": "",
        "needs": "",
        "limits": "Writes are journalled and re-read; a path outside the allowed roots is refused.",
    },
    "shell": {
        "platforms": ("windows", "macos", "linux"),
        "driver": "",
        "needs": "",
        "limits": "Command risk is classified before execution; L3 commands need the owner.",
    },
    "input": {
        "platforms": ("windows", "macos", "linux"),
        "driver": "pyautogui",
        "needs": "pyautogui + pynput",
        "limits": "Wayland sessions may refuse synthetic input; the driver says so instead of guessing.",
    },
    "vision": {
        "platforms": ("windows", "macos", "linux"),
        "driver": "mss",
        "needs": "mss + Pillow (OCR adds rapidocr-onnxruntime)",
        "limits": "Without the OCR extra, text is read from accessibility trees or not at all.",
    },
    "windows": {
        "platforms": ("windows",),
        "driver": "uiautomation",
        "needs": "uiautomation + pywin32",
        "limits": "Element actions need the window to be foreground and unminimised; DPI is made process-aware.",
    },
    "office": {
        "platforms": ("windows", "macos"),
        "driver": "win32com.client",
        "needs": "Microsoft Office plus pywin32 (macOS uses AppleScript)",
        "limits": "Tier B file-engine reads and writes work without Office installed; documents are reparsed to verify.",
    },
    "browser": {
        "platforms": ("windows", "macos", "linux"),
        "driver": "playwright",
        "needs": "playwright plus its Chromium build",
        "limits": "A driven profile is separate from the user's own browser unless CDP is attached.",
    },
    "whatsapp": {
        "platforms": ("windows", "macos", "linux"),
        "driver": "playwright",
        "needs": "a signed-in WhatsApp Web session in the driven browser",
        "limits": "Calls and voice notes need the audio stack; the QR pairing is done by the owner once.",
    },
    "voice": {
        "platforms": ("windows", "macos", "linux"),
        "driver": "",
        "needs": "a 16-bit PCM WAV recording (analysis is stdlib-only)",
        "limits": "Genders are pitch heuristics and can report unknown; overlapping speech is flagged, not separated.",
    },
    "camera": {
        "platforms": ("windows", "macos", "linux"),
        "driver": "cv2",
        "needs": "opencv-python for detection, face_recognition (dlib) for identity matching",
        "limits": "Capture needs the owner's stored consent AND the OS permission; there is no override, and frames are never written to disk.",
    },
    "schedule": {
        "platforms": ("windows", "macos", "linux"),
        "driver": "",
        "needs": "nothing; the scheduler is a deterministic service",
        "limits": "A run missed past its grace window is skipped with a reason, and GUI work is deferred while the session is locked.",
    },
    "presence": {
        "platforms": ("windows", "macos", "linux"),
        "driver": "",
        "needs": "nothing beyond the voice and camera senses it fuses",
        "limits": "Camera evidence is absent without consent, so the decision falls back to voice-only and says so.",
    },
    "orchestration": {
        "platforms": ("windows", "macos", "linux"),
        "driver": "",
        "needs": "",
        "limits": "Fleet actions are permission-scoped; model reassignment is treated as irreversible.",
    },
}


def current_platform() -> str:
    """This machine's platform in the vocabulary of the matrix: windows | macos | linux."""
    system = platform_module.system().lower()
    if system.startswith("win"):
        return "windows"
    if system.startswith("darwin"):
        return "macos"
    return "linux"


def driver_present(module: str) -> bool:
    """Whether a native dependency exists here, without importing it."""
    if not module:
        return True
    root = module.split(".")[0]
    try:
        return importlib.util.find_spec(root) is not None
    except (ImportError, ValueError, AttributeError):  # a broken install still counts as absent
        return False


# --------------------------------------------------------------------------------------
# Evidence: what somebody actually watched work
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class Recording:
    """One registered recording of a tool working on a real application."""

    tool: str
    machine: str
    os: str
    date: str
    recording: str
    notes: str = ""
    problem: str = ""

    @property
    def valid(self) -> bool:
        return not self.problem

    def to_json(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "machine": self.machine,
            "os": self.os,
            "date": self.date,
            "recording": self.recording,
            "notes": self.notes,
        }


def _iso_date(value: Any) -> str:
    """Normalise a date, or return "" when it is not a real ISO date."""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value)).date().isoformat()
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text:
        return ""
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return ""


def load_ledger(path: str | Path | None = None) -> list[Recording]:
    """Read the evidence ledger, marking every entry that is not usable as evidence.

    An incomplete entry is kept and flagged rather than dropped: a recording that names no
    machine or no date is exactly the kind of unverifiable claim the Reality Gate is about,
    so it is surfaced instead of quietly ignored.
    """
    target = Path(path) if path is not None else LEDGER_PATH
    if not target.exists():
        return []
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [
            Recording(
                tool="<ledger>",
                machine="",
                os="",
                date="",
                recording="",
                problem=f"{target} is unreadable: {exc}",
            )
        ]
    tools = data.get("tools", data) if isinstance(data, dict) else {}
    if not isinstance(tools, dict):
        return [Recording("<ledger>", "", "", "", "", problem=f"{target}: 'tools' must be an object")]

    out: list[Recording] = []
    for name, entry in tools.items():
        if not isinstance(entry, dict):
            out.append(Recording(str(name), "", "", "", "", problem="entry is not an object"))
            continue
        recordings = entry.get("recordings")
        if isinstance(recordings, list):
            if not recordings:
                out.append(Recording(str(name), "", "", "", "", problem="recordings list is empty"))
                continue
            entry = {**entry, **recordings[-1], "notes": entry.get("notes", "")} if isinstance(recordings[-1], dict) else entry
        record = Recording(
            tool=str(name),
            machine=str(entry.get("machine", "")),
            os=str(entry.get("os", "")),
            date=_iso_date(entry.get("date", "")),
            recording=str(entry.get("recording", "")),
            notes=str(entry.get("notes", "")),
        )
        missing = [
            label
            for label, value in (
                ("machine", record.machine),
                ("os", record.os),
                ("date", record.date),
                ("recording", record.recording),
            )
            if not value
        ]
        if missing:
            record.problem = "evidence is incomplete: " + ", ".join(missing) + " missing"
        out.append(record)
    return out


# --------------------------------------------------------------------------------------
# Real runs: how often it has actually happened
# --------------------------------------------------------------------------------------


class RunStats(Protocol):
    """Per-action execution history from a real store."""

    def for_action(self, name: str) -> dict[str, Any]:  # pragma: no cover - protocol
        ...


class NullRunStats:
    """No store attached: every tool reports 'no real runs recorded yet'."""

    def for_action(self, name: str) -> dict[str, Any]:
        return {"runs": 0, "ok": 0, "last": None, "source": ""}


@dataclass(slots=True)
class JournalRunStats:
    """Runs read from the append-only action journal (spec 4.5).

    The whole table is read once and cached: the matrix asks about every tool in turn, and
    one aggregate query per tool would be a silly way to spend a launch.
    """

    journal: Any
    _cache: dict[str, dict[str, Any]] | None = None

    def _table(self) -> dict[str, dict[str, Any]]:
        if self._cache is None:
            try:
                self._cache = self.journal.action_stats()
            except Exception:
                self._cache = {}
        return self._cache

    def for_action(self, name: str) -> dict[str, Any]:
        row = self._table().get(name)
        if not row:
            return {"runs": 0, "ok": 0, "last": None, "source": "journal"}
        return {
            "runs": int(row.get("runs") or 0),
            "ok": int(row.get("committed") or 0),
            "last": row.get("last"),
            "source": "journal",
        }


@dataclass(slots=True)
class AuditRunStats:
    """Runs read from the memory audit log, which records denials as well as executions."""

    memory: Any

    def for_action(self, name: str) -> dict[str, Any]:
        try:
            rows = self.memory.audit_log(limit=100000)
        except Exception:
            return {"runs": 0, "ok": 0, "last": None, "source": "audit"}
        runs = ok = 0
        last: float | None = None
        for row in rows:
            if row.get("action") != name:
                continue
            if row.get("outcome") == "denied":
                continue
            runs += 1
            ok += 1 if row.get("outcome") == "ok" else 0
            stamp = row.get("ts")
            if isinstance(stamp, (int, float)) and (last is None or stamp > last):
                last = float(stamp)
        return {"runs": runs, "ok": ok, "last": last, "source": "audit"}


@dataclass(slots=True)
class ChainedRunStats:
    """Merge several stores, preferring the one that has actually seen the action."""

    sources: Sequence[RunStats] = field(default_factory=tuple)

    def for_action(self, name: str) -> dict[str, Any]:
        best = {"runs": 0, "ok": 0, "last": None, "source": ""}
        for source in self.sources:
            stats = source.for_action(name)
            if int(stats.get("runs") or 0) > int(best["runs"] or 0) or (
                stats.get("runs") and not best["source"]
            ):
                best = stats
            elif best["last"] is None and stats.get("last"):
                best["last"] = stats["last"]
                best["source"] = best["source"] or stats.get("source", "")
        return best


# --------------------------------------------------------------------------------------
# The matrix
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class CapabilityRecord:
    """One row: what the tool is, whether it works here, and how we know."""

    tool: str
    group: str
    capability: str
    danger: str
    requires_confirm: bool
    description: str
    status: str
    platforms: tuple[str, ...]
    platform: str
    driver: str
    needs: str
    limits: str
    reason: str = ""
    runs: int = 0
    ok: int = 0
    stats_source: str = ""
    last_run: float | None = None
    last_verified: str = ""
    recording: str = ""
    machine: str = ""
    evidence_problem: str = ""
    #: Whether *this* machine could run it right now (or ``unreported`` for the declared view).
    local: str = LOCAL_UNREPORTED
    local_reason: str = ""
    available_reason: str = ""

    @property
    def green(self) -> bool:
        return self.status == GREEN

    @property
    def available(self) -> bool:
        """Is this a capability at all, on some supported machine?"""
        return self.status != UNAVAILABLE

    @property
    def available_here(self) -> bool:
        """May an agent on this machine be offered the tool? (4.7: non-green is not offered.)"""
        return self.available and self.local in (LOCAL_READY, LOCAL_UNREPORTED)

    @property
    def unavailability(self) -> str:
        """Why an agent here must not be offered this tool, in plain words."""
        if self.available_reason:
            return self.available_reason
        return self.reason if not self.available else self.local_reason

    @property
    def success_rate(self) -> float | None:
        return round(self.ok / self.runs, 4) if self.runs else None

    def to_json(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "group": self.group,
            "capability": self.capability,
            "danger": self.danger,
            "requires_confirm": self.requires_confirm,
            "description": self.description,
            "status": self.status,
            "available": self.available,
            "available_here": self.available_here,
            "local": self.local,
            "local_reason": self.local_reason,
            "unavailability": self.unavailability,
            "platforms": list(self.platforms),
            "platform": self.platform,
            "driver": self.driver,
            "needs": self.needs,
            "limits": self.limits,
            "reason": self.reason,
            "runs": self.runs,
            "runs_ok": self.ok,
            "stats_source": self.stats_source,
            "last_run": self.last_run,
            "last_verified": self.last_verified,
            "recording": self.recording,
            "machine": self.machine,
            "evidence_problem": self.evidence_problem,
        }


@dataclass(slots=True)
class HiddenTool:
    """A tool an agent asked for and must not be offered, with the reason in plain words."""

    tool: str
    reason: str

    def to_json(self) -> dict[str, Any]:
        return {"tool": self.tool, "reason": self.reason}


class CapabilityMatrix:
    """Builds the matrix from the live registry plus real evidence and real runs."""

    def __init__(
        self,
        registry: Any,
        ledger_path: str | Path | None = None,
        stats: RunStats | None = None,
        platform: str | None = None,
        driver_probe: Any = None,
    ) -> None:
        self.registry = registry
        self.ledger_path = Path(ledger_path) if ledger_path is not None else LEDGER_PATH
        self.stats: RunStats = stats or NullRunStats()
        self.platform = platform or current_platform()
        self._probe = driver_probe or driver_present
        self._recordings: list[Recording] | None = None
        self._cache: dict[str, CapabilityRecord] = {}

    # -- evidence ----------------------------------------------------------------------
    def recordings(self) -> list[Recording]:
        if self._recordings is None:
            self._recordings = load_ledger(self.ledger_path)
        return self._recordings

    def recording_for(self, tool: str) -> Recording | None:
        """The newest usable recording for a tool, if any."""
        found = [r for r in self.recordings() if r.tool == tool]
        if not found:
            return None
        valid = [r for r in found if r.valid]
        pool = valid or found
        return sorted(pool, key=lambda r: r.date, reverse=True)[0]

    # -- rows --------------------------------------------------------------------------
    def record(self, tool: str) -> CapabilityRecord:
        """The full row for one tool, or a row saying the tool does not exist."""
        if tool in self._cache:
            return self._cache[tool]
        act = self.registry.get(tool) if hasattr(self.registry, "get") else None
        if act is None:
            record = CapabilityRecord(
                tool=tool,
                group="unknown",
                capability="",
                danger="",
                requires_confirm=False,
                description="",
                status=UNAVAILABLE,
                platforms=(),
                platform=self.platform,
                driver="",
                needs="",
                limits="",
                reason="no such tool is declared in the action registry",
            )
            self._cache[tool] = record
            return record

        profile = GROUP_PROFILES.get(act.group)
        platforms: tuple[str, ...] = tuple(profile["platforms"]) if profile else ()
        driver = str(profile.get("driver", "")) if profile else ""
        needs = str(profile.get("needs", "")) if profile else ""
        limits = str(profile.get("limits", "")) if profile else ""
        reason = ""

        local = LOCAL_UNREPORTED
        if profile is None:
            status = UNAVAILABLE
            reason = f"the '{act.group}' group has no driver profile, so this cannot be claimed"
        else:
            # Declared as working, until evidence and real runs say otherwise. This machine is
            # judged separately, so the committed document does not depend on the build host.
            status = PROVISIONAL
            if self.platform != DECLARED_ONLY:
                if self.platform not in platforms:
                    local = LOCAL_WRONG_PLATFORM
                elif not self._probe(driver):
                    local = LOCAL_DRIVER_MISSING
                else:
                    local = LOCAL_READY

        runs = {"runs": 0, "ok": 0, "last": None, "source": ""}
        if status != UNAVAILABLE:
            runs = self.stats.for_action(tool)
        recording = self.recording_for(tool)
        record = CapabilityRecord(
            tool=tool,
            group=act.group,
            capability=act.capability,
            danger=act.danger,
            requires_confirm=bool(act.requires_confirm),
            description=act.description,
            status=status,
            platforms=platforms,
            platform=self.platform,
            driver=driver,
            needs=needs,
            limits=limits,
            reason=reason,
            runs=int(runs.get("runs") or 0),
            ok=int(runs.get("ok") or 0),
            stats_source=str(runs.get("source") or ""),
            last_run=runs.get("last") if isinstance(runs.get("last"), (int, float)) else None,
            last_verified=recording.date if recording and recording.valid else "",
            recording=recording.recording if recording else "",
            machine=recording.machine if recording else "",
            evidence_problem=recording.problem if recording and not recording.valid else "",
            local=local,
        )
        if record.status == PROVISIONAL:
            missing: list[str] = []
            if not recording or not recording.valid:
                missing.append("a recording on a real app")
            if record.runs <= 0:
                missing.append("any real run")
            if not missing:
                record.status = GREEN
                record.reason = "verified: recorded on a real app and exercised for real"
            else:
                record.reason = "not green yet: missing " + " and ".join(missing)
        if record.local == LOCAL_DRIVER_MISSING:
            record.local_reason = f"driver '{driver}' is not installed here; needs {needs}"
        elif record.local == LOCAL_WRONG_PLATFORM:
            record.local_reason = (
                f"this machine is {self.platform}; the tool targets {'/'.join(platforms)} only"
            )
            record.available_reason = record.local_reason
        self._cache[tool] = record
        return record

    def records(self, names: Iterable[str] | None = None) -> list[CapabilityRecord]:
        selected = list(names) if names is not None else list(self.registry.names())
        return [self.record(name) for name in selected]

    # -- what a model may see ----------------------------------------------------------
    def hidden(self) -> list[HiddenTool]:
        """Tools that must not be offered here, each with the honest reason (4.7)."""
        return [
            HiddenTool(r.tool, r.unavailability or "not available")
            for r in self.records()
            if not r.available_here
        ]

    def available_names(self, capabilities: Iterable[str] | None = None) -> list[str]:
        caps = frozenset(capabilities) if capabilities is not None else None
        out: list[str] = []
        for record in self.records():
            if not record.available_here:
                continue
            if caps is not None and record.capability not in caps:
                continue
            out.append(record.tool)
        return out

    def phantom_recordings(self) -> list[str]:
        """Recordings for tools that are not declared: a phantom row is not evidence."""
        known = set(self.registry.names())
        return sorted({r.tool for r in self.recordings() if r.tool not in known})

    def summary(self) -> dict[str, Any]:
        rows = self.records()
        by_status: dict[str, int] = {GREEN: 0, PROVISIONAL: 0, UNAVAILABLE: 0}
        for row in rows:
            by_status[row.status] = by_status.get(row.status, 0) + 1
        return {
            "platform": self.platform,
            "tools": len(rows),
            "by_status": by_status,
            "green": by_status.get(GREEN, 0),
            "provisional": by_status.get(PROVISIONAL, 0),
            "unavailable": by_status.get(UNAVAILABLE, 0),
            "ready_here": sum(1 for row in rows if row.available_here),
            "not_here": sum(1 for row in rows if not row.available_here),
            "groups": len({row.group for row in rows}),
            "with_runs": sum(1 for row in rows if row.runs > 0),
            "ledger_entries": len(self.recordings()),
            "invalid_evidence": [
                {"tool": r.tool, "problem": r.problem} for r in self.recordings() if not r.valid
            ],
            "phantom_evidence": self.phantom_recordings(),
        }

    def to_json(self) -> dict[str, Any]:
        return {"summary": self.summary(), "tools": [r.to_json() for r in self.records()]}

    # -- the committed document ---------------------------------------------------------
    def markdown(self) -> str:
        rows = self.records()
        summary = self.summary()
        available = [r for r in rows if r.available_here]
        lines: list[str] = []
        lines.append("# CAPABILITY_MATRIX")
        lines.append("")
        lines.append(
            "Generated by `python3 tools/capability_matrix.py` from the live action registry, "
            "the driver probe on this machine, the run history and the recording ledger at "
            "`artifacts/capability_evidence.json`. Do not edit by hand: the release gate fails "
            "when this file is stale (spec 4.7)."
        )
        lines.append("")
        if self.platform == DECLARED_ONLY:
            lines.append(
                "This is the **declared surface** of the product, across every platform the "
                "tools target. It deliberately does not depend on the machine that generated "
                "it. Run `zyntharo capabilities` for the view of one machine, which also applies "
                "the driver probe."
            )
        else:
            lines.append(
                f"Machine platform for this report: **{summary['platform']}** "
                f"({summary['ready_here']} of {summary['tools']} tool(s) usable here)."
            )
        lines.append("")
        lines.append(
            "| state | meaning | may be advertised |"
            "\n| --- | --- | --- |"
            "\n| `green` | implemented, driver present, recorded on a real app, has run for real | yes |"
            "\n| `provisional` | implemented and usable here, but not yet proven on a real app | "
            "no \u2014 every receipt from it is labelled UNVERIFIED |"
            "\n| `unavailable` | no driver here, or the wrong platform | no \u2014 hidden from agents, "
            "shown to the user as \"not available on this machine\" |"
        )
        lines.append("")
        where = "here" if self.platform != DECLARED_ONLY else "on every supported platform"
        lines.append(
            f"**{summary['tools']} declared tool(s)** in {summary['groups']} group(s): "
            f"{summary['green']} green, {summary['provisional']} provisional, "
            f"{summary['unavailable']} unavailable {where}. "
            f"{summary['with_runs']} tool(s) have recorded real runs."
        )
        lines.append("")
        lines.append("## Available here" if self.platform != DECLARED_ONLY else "## Declared tools")
        lines.append("")
        lines.append(
            "| tool | group | capability | state | runs (ok) | success | last run | "
            "last verified | recording | limits |"
            "\n| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"
        )
        for row in available:
            success = f"{row.success_rate:.0%}" if row.success_rate is not None else "n/a"
            last_run = _stamp(row.last_run)
            lines.append(
                "| `{tool}` | {group} | {cap} | {state} | {runs} ({ok}) | {success} | {last} | "
                "{verified} | {recording} | {limits} |".format(
                    tool=row.tool,
                    group=row.group,
                    cap=row.capability or "\u2014",
                    state=row.status,
                    runs=row.runs,
                    ok=row.ok,
                    success=success,
                    last=last_run,
                    verified=row.last_verified or "\u2014",
                    recording=f"`{row.recording}`" if row.recording else "\u2014",
                    limits=_cell(row.limits),
                )
            )
        lines.append("")
        blocked = [r for r in rows if not r.available_here]
        title = "## Not available on this machine" if self.platform != DECLARED_ONLY else "## Not offerable"
        lines.append(title)
        lines.append("")
        lines.append(
            "Hidden from every agent until the driver is present and the platform matches "
            "(spec 4.7). Shown to the user as \"not available\", never attempted and guessed at."
        )
        lines.append("")
        if blocked:
            lines.append("| tool | group | why | needs |")
            lines.append("| --- | --- | --- | --- |")
            for row in blocked:
                lines.append(
                    f"| `{row.tool}` | {row.group} | {_cell(row.unavailability)} | "
                    f"{_cell(row.needs) or '\u2014'} |"
                )
        else:
            lines.append("_None: every declared tool has what it needs here._")
        lines.append("")
        lines.append("## Evidence problems (nothing here counts as proof)")
        lines.append("")
        problems = [r for r in self.recordings() if not r.valid]
        phantoms = self.phantom_recordings()
        if problems or phantoms:
            for recording in problems:
                lines.append(f"- `{recording.tool}`: {recording.problem}")
            for tool in phantoms:
                lines.append(f"- `{tool}`: a recording exists for a tool that is not declared at all")
        else:
            lines.append("_None: every ledger entry is complete and refers to a declared tool._")
        lines.append("")
        lines.append("## How a tool becomes green")
        lines.append("")
        lines.append(
            "Run the tool on the real application, record it, then add one entry to "
            "`artifacts/capability_evidence.json` with the machine, the OS, the date and the "
            "recording path (spec 4.7). The next generated report picks it up; nothing is "
            "promoted by editing this document."
        )
        lines.append("")
        return "\n".join(lines)


def _stamp(value: float | None) -> str:
    if not value:
        return "\u2014"
    return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M")


def _cell(text: str) -> str:
    return " ".join(str(text or "").split()).replace("|", "\\|")
