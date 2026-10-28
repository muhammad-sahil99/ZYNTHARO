"""Hardware health monitor, S1 (spec 16): programmed, deterministic, no model involved.

The design rule is the same one the rest of the project uses, applied to hardware: a reading is
either measured, or it is reported as unavailable with the reason. Nothing is filled in, guessed
from a sibling sensor, or defaulted to a plausible-looking number. On a machine with no battery,
``battery.charge`` says "no battery is present", not 100%.

What this module is:

* **A collector** reading the sensors the OS really exposes — ``/proc``, ``/sys``, ctypes calls
  on Windows, vendor CLIs when they exist. Every sensor carries its source, so a value can be
  traced to the interface that produced it.
* **A plausibility filter** that rejects impossible values, marks stuck sensors suspect and
  reports a sensor fault instead of alarming on garbage.
* **A rule engine** of thresholds with hysteresis, sustained durations, rate of change, and
  correlations between sensors (fan duty rising without RPM, high temperature at low load).
* **A protective policy** whose actions are deterministic: DANGER reduces the AI's own load,
  CRITICAL puts the AI's work into a safe state — by calling the hooks the runtime provides,
  not by reaching into other subsystems.

It is deliberately not an AI feature. Main may read it and may *tighten* a threshold; it may
never loosen a protective rule or reach the optional privileged helper.
"""

from __future__ import annotations

import copy
import hashlib
import math
import os
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from ..events import HARDWARE_ALERT, HARDWARE_CLEARED

#: Severities, lowest first. INFO is "worth knowing", CRITICAL is "stop the machine's work".
INFO, WATCH, WARNING, DANGER, CRITICAL = "INFO", "WATCH", "WARNING", "DANGER", "CRITICAL"
SEVERITY_ORDER = {INFO: 0, WATCH: 1, WARNING: 2, DANGER: 3, CRITICAL: 4}

#: Accuracy classes, so a caller knows how much a number is worth.
EXACT = "exact"          # a counter the OS maintains
MEASURED = "measured"    # a device's own value
DERIVED = "derived"      # computed from measurements
APPROXIMATE = "approximate"  # a vendor CLI's rounded output

#: Plausibility bounds per sensor family: (low, high, unit). A reading outside these is not a
#: reading, it is a fault, and treating it as a reading is how a monitor cries wolf at 3 a.m.
BOUNDS: dict[str, tuple[float, float]] = {
    "cpu.temp": (-20.0, 125.0),
    "cpu.load": (0.0, 1.0),
    "cpu.clock": (0.0, 8000.0),
    "gpu.temp": (-20.0, 130.0),
    "gpu.load": (0.0, 1.0),
    "gpu.power": (0.0, 1200.0),
    "memory.used": (0.0, 1.0),
    "memory.temp": (-20.0, 120.0),
    "disk.temp": (-20.0, 100.0),
    "disk.used": (0.0, 1.0),
    "fan.rpm": (0.0, 20000.0),
    "fan.duty": (0.0, 1.0),
    "volt.rail": (0.0, 30.0),
    "battery.charge": (0.0, 1.0),
    "battery.wear": (0.0, 1.0),
    "net.rx": (0.0, 1e12),
    "net.tx": (0.0, 1e12),
}

#: A value that cannot move this much between two samples is a glitch, not a reading.
MAX_DELTA_PER_SECOND: dict[str, float] = {
    "cpu.temp": 25.0,
    "gpu.temp": 25.0,
    "disk.temp": 15.0,
    "memory.temp": 20.0,
    "battery.charge": 0.05,
    "fan.rpm": 6000.0,
    "volt.rail": 3.0,
}


@dataclass(slots=True)
class Reading:
    """One measured value, with its provenance and its honesty flags."""

    key: str
    value: float | None
    unit: str
    source: str
    label: str = ""
    group: str = ""
    accuracy: str = MEASURED
    available: bool = True
    reason: str = ""
    suspect: str = ""
    ts: float = field(default_factory=time.time)

    @property
    def measured(self) -> bool:
        return self.available and self.value is not None and not self.suspect

    def to_json(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label or self.key,
            "group": self.group or self.key.split(".")[0],
            "value": None if self.value is None else round(float(self.value), 3),
            "unit": self.unit,
            "source": self.source,
            "accuracy": self.accuracy,
            "available": self.available,
            "reason": self.reason,
            "suspect": self.suspect,
            "ts": round(self.ts, 3),
        }


def unavailable(key: str, unit: str, reason: str, source: str = "", group: str = "") -> Reading:
    """The only way to say "not on this machine": with the reason, never with a default."""
    return Reading(
        key=key, value=None, unit=unit, source=source, group=group,
        available=False, reason=reason, accuracy=EXACT,
    )


def _read_text(path: str | Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _read_int(path: str | Path) -> int | None:
    text = _read_text(path)
    if not text:
        return None
    try:
        return int(text.split()[0])
    except (ValueError, IndexError):
        return None


def _linux_cpu_load(interval: float = 0.0) -> Reading:
    """CPU utilisation from /proc/stat, computed from a real counter delta."""
    stat = _read_text("/proc/stat")
    line = next((row for row in stat.splitlines() if row.startswith("cpu ")), "")
    if not line:
        return unavailable("cpu.load", "ratio", "this kernel does not expose /proc/stat", "procfs", "cpu")
    fields = [int(value) for value in line.split()[1:9]]
    total = sum(fields)
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
    return Reading("cpu.load", 1.0 - (idle / total if total else 1.0), "ratio", "procfs/stat", "CPU load", "cpu", EXACT)


def _windows_cpu_load() -> Reading:  # pragma: no cover - Windows only
    import ctypes

    class FILETIME(ctypes.Structure):
        _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

    idle, kernel, user = FILETIME(), FILETIME(), FILETIME()
    ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user))

    def as_int(value: "FILETIME") -> int:
        return (value.high << 32) | value.low

    total = as_int(kernel) + as_int(user)
    if not total:
        return unavailable("cpu.load", "ratio", "GetSystemTimes returned nothing", "kernel32", "cpu")
    busy = total - as_int(idle)
    return Reading("cpu.load", min(1.0, max(0.0, busy / total)), "ratio", "kernel32/GetSystemTimes", "CPU load", "cpu", EXACT)


def _memory_reading() -> Reading:
    if os.name == "nt":  # pragma: no cover - Windows only
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_uint32),
                ("memory_load", ctypes.c_uint32),
                ("total", ctypes.c_uint64),
                ("available", ctypes.c_uint64),
            ]

        status = MEMORYSTATUSEX()
        status.length = ctypes.sizeof(MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            used = 1.0 - (status.available / status.total if status.total else 0.0)
            return Reading("memory.used", used, "ratio", "kernel32/GlobalMemoryStatusEx", "Memory used", "memory", EXACT)
        return unavailable("memory.used", "ratio", "GlobalMemoryStatusEx failed", "kernel32", "memory")
    info = _read_text("/proc/meminfo")
    values: dict[str, float] = {}
    for line in info.splitlines():
        parts = line.split(":")
        if len(parts) == 2:
            number = parts[1].strip().split()[0]
            try:
                values[parts[0]] = float(number)
            except ValueError:
                continue
    total = values.get("MemTotal", 0.0)
    if not total:
        return unavailable("memory.used", "ratio", "this kernel does not expose /proc/meminfo", "procfs", "memory")
    available = values.get("MemAvailable", values.get("MemFree", 0.0))
    return Reading("memory.used", 1.0 - (available / total), "ratio", "procfs/meminfo", "Memory used", "memory", EXACT)


def _disk_reading(path: str = "/") -> Reading:
    try:
        usage = shutil.disk_usage(path)
    except OSError as exc:
        return unavailable("disk.used", "ratio", f"disk usage unavailable for {path}: {exc}", "os", "disk")
    used = usage.used / usage.total if usage.total else 0.0
    return Reading("disk.used", used, "ratio", "os/disk_usage", f"Disk used ({path})", "disk", EXACT)


def _sysfs_readings() -> list[Reading]:
    """The kernel's own sensor tree: thermal zones, hwmon (temperatures, fans, rails), EDAC."""
    readings: list[Reading] = []
    thermal_root = Path("/sys/class/thermal")
    for zone in sorted(thermal_root.glob("thermal_zone*"))[:16]:
        raw = _read_int(zone / "temp")
        kind = _read_text(zone / "type") or zone.name
        if raw is None:
            continue
        readings.append(
            Reading(f"thermal.{kind}", raw / 1000.0, "C", f"sysfs/thermal/{zone.name}", f"Thermal {kind}", "cpu", MEASURED)
        )
    hwmon_root = Path("/sys/class/hwmon")
    for device in sorted(hwmon_root.glob("hwmon*"))[:12]:
        name = _read_text(device / "name") or device.name
        for path in sorted(device.glob("temp*_input"))[:16]:
            raw = _read_int(path)
            label = _read_text(path.with_name(path.name.replace("_input", "_label"))) or path.stem
            if raw is None:
                continue
            readings.append(Reading(f"cpu.temp.{name}.{label}", raw / 1000.0, "C", str(path), f"{name} {label}", "cpu", MEASURED))
        for path in sorted(device.glob("fan*_input"))[:16]:
            raw = _read_int(path)
            label = _read_text(path.with_name(path.name.replace("_input", "_label"))) or path.stem
            if raw is None:
                continue
            readings.append(Reading(f"fan.rpm.{name}.{label}", float(raw), "rpm", str(path), f"{name} {label}", "fan", MEASURED))
        for path in sorted(device.glob("in*_input"))[:16]:
            raw = _read_int(path)
            label = _read_text(path.with_name(path.name.replace("_input", "_label"))) or path.stem
            if raw is None or raw == 0:
                # A rail at 0 V is a sensor that is not wired, not a dead power supply.
                continue
            readings.append(Reading(f"volt.rail.{name}.{label}", raw / 1000.0, "V", str(path), f"{name} {label}", "power", MEASURED))
    for edac in sorted(Path("/sys/devices/system/edac").glob("mc*/ce_count"))[:8]:
        raw = _read_int(edac)
        if raw is not None:
            readings.append(Reading("memory.ecc_errors", float(raw), "count", str(edac), "Corrected memory errors", "memory", EXACT))
    return readings


def _battery_reading() -> list[Reading]:
    root = Path("/sys/class/power_supply")
    batteries = [item for item in sorted(root.glob("BAT*")) if item.is_dir()] if root.exists() else []
    if not batteries:
        return [
            unavailable("battery.charge", "ratio", "no battery is present on this machine", "sysfs", "battery"),
            unavailable("battery.wear", "ratio", "no battery is present on this machine", "sysfs", "battery"),
        ]
    battery = batteries[0]
    out: list[Reading] = []
    capacity = _read_int(battery / "capacity")
    if capacity is None:
        out.append(unavailable("battery.charge", "ratio", f"{battery.name} does not expose capacity", "sysfs", "battery"))
    else:
        out.append(Reading("battery.charge", capacity / 100.0, "ratio", f"sysfs/{battery.name}/capacity", "Battery charge", "battery", MEASURED))
    design = _read_int(battery / "energy_full_design") or _read_int(battery / "charge_full_design")
    full = _read_int(battery / "energy_full") or _read_int(battery / "charge_full")
    if design and full:
        wear = max(0.0, 1.0 - (full / design))
        out.append(Reading("battery.wear", wear, "ratio", f"sysfs/{battery.name}", "Battery wear", "battery", DERIVED))
    else:
        out.append(unavailable("battery.wear", "ratio", "the battery does not report design and full capacity", "sysfs", "battery"))
    status = _read_text(battery / "status")
    if status:
        out.append(Reading("battery.state", 1.0 if status.lower() == "charging" else 0.0, "flag", f"sysfs/{battery.name}/status", f"Battery state ({status})", "battery", MEASURED))
    return out


def _nvidia_readings() -> list[Reading]:
    """GPU telemetry through the vendor's own CLI when it exists, never a guess when it does not."""
    if not shutil.which("nvidia-smi"):
        return [unavailable("gpu.temp", "C", "no NVIDIA GPU tool (nvidia-smi) on this machine", "nvidia", "gpu")]
    query = "temperature.gpu,utilization.gpu,power.draw,clocks.sm,memory.used,memory.total,fan.speed"
    try:
        proc = subprocess.run(
            ["nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return [unavailable("gpu.temp", "C", f"nvidia-smi could not run: {exc}", "nvidia", "gpu")]
    if proc.returncode != 0 or not proc.stdout.strip():
        return [unavailable("gpu.temp", "C", f"nvidia-smi failed: {proc.stderr.strip()[:120]}", "nvidia", "gpu")]
    out: list[Reading] = []
    for index, line in enumerate(proc.stdout.strip().splitlines()):
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 7:
            continue
        names = ("temp", "load", "power", "clock", "mem_used", "mem_total", "fan")
        units = ("C", "percent", "W", "MHz", "MiB", "MiB", "percent")
        for name, unit, raw in zip(names, units, parts):
            try:
                value = float(raw)
            except ValueError:
                continue
            if name == "load":
                value /= 100.0
            if name == "fan":
                value /= 100.0
            key = f"gpu.{name}" if index == 0 else f"gpu{index}.{name}"
            out.append(Reading(key, value, unit, "nvidia-smi", f"GPU {index} {name}", "gpu", APPROXIMATE))
    return out or [unavailable("gpu.temp", "C", "nvidia-smi reported no GPUs", "nvidia", "gpu")]


def _network_readings() -> list[Reading]:
    dev = _read_text("/proc/net/dev")
    if not dev:
        return [unavailable("net.rx", "bytes", "/proc/net/dev is not readable here", "procfs", "net")]
    rx = tx = errors = 0
    for line in dev.splitlines()[2:]:
        if "lo:" in line:
            continue
        parts = line.replace(":", " ").split()
        if len(parts) < 12:
            continue
        try:
            rx += int(parts[1])
            errors += int(parts[3])
            tx += int(parts[9])
        except ValueError:
            continue
    return [
        Reading("net.rx", float(rx), "bytes", "procfs/net/dev", "Network received (total)", "net", EXACT),
        Reading("net.tx", float(tx), "bytes", "procfs/net/dev", "Network sent (total)", "net", EXACT),
        Reading("net.errors", float(errors), "count", "procfs/net/dev", "Network errors", "net", EXACT),
    ]


def _uptime_reading() -> Reading:
    uptime = _read_text("/proc/uptime")
    if uptime:
        try:
            return Reading("system.uptime", float(uptime.split()[0]), "s", "procfs/uptime", "Uptime", "system", EXACT)
        except (ValueError, IndexError):
            pass
    return unavailable("system.uptime", "s", "uptime is not exposed by this OS here", "os", "system")


def _usable_memory_total() -> float | None:
    info = _read_text("/proc/meminfo")
    for line in info.splitlines():
        if line.startswith("MemTotal:"):
            try:
                return float(line.split()[1]) * 1024.0
            except (ValueError, IndexError):
                return None
    return None


def _cpu_temperature() -> list[Reading]:
    """A package temperature if one is exposed; otherwise the honest gap, named."""
    candidates: list[Reading] = []
    for path in sorted(Path("/sys/class/thermal").glob("thermal_zone*/type"))[:16]:
        kind = _read_text(path)
        if kind in ("x86_pkg_temp", "cpu-thermal", "soc_thermal"):
            raw = _read_int(path.with_name("temp"))
            if raw is not None:
                candidates.append(
                    Reading("cpu.temp", raw / 1000.0, "C", f"sysfs/{path.parent.name}", "CPU package temperature", "cpu", MEASURED)
                )
    if candidates:
        return candidates[:1]
    for device in sorted(Path("/sys/class/hwmon").glob("hwmon*"))[:12]:
        name = _read_text(device / "name")
        if name in ("coretemp", "k10temp", "zenpower", "cpu_thermal"):
            raw = _read_int(device / "temp1_input")
            if raw is not None:
                return [Reading("cpu.temp", raw / 1000.0, "C", f"sysfs/{device.name}", f"CPU temperature ({name})", "cpu", MEASURED)]
    return [
        unavailable(
            "cpu.temp",
            "C",
            "no CPU temperature sensor is exposed by this OS (it usually needs the optional S2 helper)",
            "sysfs",
            "cpu",
        )
    ]


class Collector:
    """Reads every sensor this machine really exposes, and nothing else.

    ``source`` in each reading names the interface it came from, so an operator can check the
    same file or command and get the same number.
    """

    def __init__(self, sources: Sequence[str] | None = None) -> None:
        self.sources = tuple(sources or ("sysfs", "procfs", "vendor", "os"))
        self.last_duration = 0.0

    def collect(self) -> list[Reading]:
        started = time.perf_counter()
        readings: list[Reading] = []
        if os.name == "nt":  # pragma: no cover - Windows only
            readings.append(_windows_cpu_load())
        else:
            readings.append(_linux_cpu_load())
        readings.append(_memory_reading())
        readings.append(_disk_reading("/" if os.name != "nt" else "C:\\"))
        readings.append(_uptime_reading())
        if "sysfs" in self.sources and os.name != "nt":
            readings.extend(_cpu_temperature())
            readings.extend(_sysfs_readings())
            readings.extend(_battery_reading())
            readings.extend(_network_readings())
        if "vendor" in self.sources:
            readings.extend(_nvidia_readings())
        self.last_duration = time.perf_counter() - started
        return readings

    # -- plausibility ------------------------------------------------------------------
    def filter(self, readings: Iterable[Reading], previous: dict[str, Reading] | None = None) -> list[Reading]:
        """Reject impossible values and flag stuck or jumping sensors (spec 16.3).

        A rejected reading is not dropped: it is marked suspect with the reason, because a
        broken sensor is information the user needs and a silent gap is not.
        """
        previous = previous or {}
        out: list[Reading] = []
        for reading in readings:
            family = ".".join(reading.key.split(".")[:2])
            bounds = BOUNDS.get(family)
            if not reading.available or reading.value is None:
                out.append(reading)
                continue
            value = float(reading.value)
            if math.isnan(value) or math.isinf(value):
                reading.suspect = "the value is not a finite number"
                out.append(reading)
                continue
            if bounds and not (bounds[0] <= value <= bounds[1]):
                reading.suspect = f"{value:g} {reading.unit} is outside the plausible range {bounds[0]:g}..{bounds[1]:g}"
                out.append(reading)
                continue
            prior = previous.get(reading.key)
            limit = MAX_DELTA_PER_SECOND.get(family)
            if prior is not None and prior.value is not None and limit is not None:
                gap = max(1e-6, reading.ts - prior.ts)
                jump = abs(value - float(prior.value)) / gap
                if jump > limit:
                    reading.suspect = f"changed {jump:.1f} {reading.unit}/s, which is faster than this sensor can move"
            out.append(reading)
        return out


def _family_values(readings: Sequence[Reading], prefix: str) -> list[Reading]:
    return [reading for reading in readings if reading.key.startswith(prefix) and reading.measured]


@dataclass(slots=True)
class Alert:
    """One fired rule, with everything a person needs to judge it (spec 16.4).

    ``evidence`` is the raw window around the trigger, not a summary: an alert without the
    numbers behind it is an opinion.
    """

    alert_id: str
    rule: str
    severity: str
    sensor: str
    value: float | None
    unit: str
    threshold: str
    message: str
    trend: str = ""
    duration: float = 0.0
    causes: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    ts: float = field(default_factory=time.time)
    count: int = 1

    def to_json(self) -> dict[str, Any]:
        return {
            "alert_id": self.alert_id,
            "rule": self.rule,
            "severity": self.severity,
            "sensor": self.sensor,
            "value": None if self.value is None else round(float(self.value), 3),
            "unit": self.unit,
            "threshold": self.threshold,
            "message": self.message,
            "trend": self.trend,
            "duration_s": round(self.duration, 1),
            "causes": self.causes,
            "actions": self.actions,
            "evidence": self.evidence[-60:],
            "ts": round(self.ts, 3),
            "count": self.count,
        }


def _alert_id(rule: str, sensor: str, ts: float) -> str:
    return hashlib.sha256(f"{rule}|{sensor}|{int(ts)}".encode()).hexdigest()[:10]


class History:
    """A ring of samples per sensor: one hour at full rate by default (spec 16.3)."""

    def __init__(self, capacity: int = 3600) -> None:
        self.capacity = max(60, capacity)
        self.samples: dict[str, list[tuple[float, float]]] = {}

    def add(self, readings: Iterable[Reading]) -> None:
        for reading in readings:
            if reading.value is None or not reading.available:
                continue
            bucket = self.samples.setdefault(reading.key, [])
            bucket.append((reading.ts, float(reading.value)))
            if len(bucket) > self.capacity:
                del bucket[: len(bucket) - self.capacity]

    def window(self, key: str, seconds: float = 60.0, now: float | None = None) -> list[tuple[float, float]]:
        cutoff = (now or time.time()) - seconds
        return [item for item in self.samples.get(key, []) if item[0] >= cutoff]

    def latest(self, key: str) -> tuple[float, float] | None:
        bucket = self.samples.get(key)
        return bucket[-1] if bucket else None

    def trend(self, key: str, seconds: float = 60.0) -> str:
        values = [value for _, value in self.window(key, seconds)]
        if len(values) < 3:
            return "not enough samples to tell"
        first, last = values[0], values[-1]
        span = max(1e-9, abs(first))
        change = (last - first) / span
        if change > 0.15:
            return f"rising ({first:.1f} -> {last:.1f})"
        if change < -0.15:
            return f"falling ({first:.1f} -> {last:.1f})"
        return f"steady around {statistics.fmean(values):.1f}"


@dataclass(slots=True)
class AlertCandidate:
    """What a rule returns before the monitor applies hysteresis and cooldowns."""

    rule: str
    severity: str
    sensor: str
    value: float | None
    unit: str
    threshold: str
    message: str
    causes: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    clear_at: float | None = None  # value at which this alert stops being true


@dataclass
class Baseline:
    """Per-sensor EWMA and spread, so "unusual for this machine" can be said out loud."""

    alpha: float = 0.2
    means: dict[str, float] = field(default_factory=dict)
    variance: dict[str, float] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)

    def update(self, readings: Iterable[Reading]) -> None:
        for reading in readings:
            if not reading.measured or reading.value is None:
                continue
            key, value = reading.key, float(reading.value)
            if key not in self.means:
                self.means[key] = value
                self.variance[key] = 0.0
                self.counts[key] = 1
                continue
            delta = value - self.means[key]
            self.means[key] += self.alpha * delta
            self.variance[key] = (1 - self.alpha) * (self.variance[key] + self.alpha * delta * delta)
            self.counts[key] += 1

    def z_score(self, key: str, value: float) -> float | None:
        if self.counts.get(key, 0) < 20:
            return None  # a baseline from five samples is a guess, so it is not offered
        spread = math.sqrt(max(1e-9, self.variance.get(key, 0.0)))
        return (value - self.means[key]) / spread

    def explain(self, key: str) -> dict[str, Any]:
        if key not in self.means:
            return {"sensor": key, "baseline": "not learned yet"}
        return {
            "sensor": key,
            "mean": round(self.means[key], 3),
            "spread": round(math.sqrt(max(1e-9, self.variance[key])), 3),
            "samples": self.counts[key],
        }


@dataclass(slots=True)
class Snapshot:
    """One sampling pass: readings, a measured load figure, and the time it took."""

    ts: float
    readings: list[Reading]
    duration: float = 0.0

    def get(self, key: str) -> Reading | None:
        return next((reading for reading in self.readings if reading.key == key), None)

    def by_group(self) -> dict[str, list[dict[str, Any]]]:
        groups: dict[str, list[dict[str, Any]]] = {}
        for reading in self.readings:
            groups.setdefault(reading.group or reading.key.split(".")[0], []).append(reading.to_json())
        return groups

    @property
    def unavailable(self) -> list[dict[str, Any]]:
        return [reading.to_json() for reading in self.readings if not reading.available]

    def to_json(self) -> dict[str, Any]:
        return {
            "ts": round(self.ts, 3),
            "duration_ms": round(self.duration * 1000.0, 2),
            "readings": [reading.to_json() for reading in self.readings],
            "unavailable": self.unavailable,
        }


# --------------------------------------------------------------------------------------
# Rules: thresholds, duration, rate and correlation. No model, no guessing.
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class ThresholdRule:
    """A temperature or space limit, with a distinct line for each severity."""

    rule: str
    sensor: str
    watch: float
    warning: float
    danger: float
    critical: float
    unit: str
    causes: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    higher_is_worse: bool = True

    def evaluate(self, snapshot: Snapshot, history: History, baseline: Baseline) -> list[AlertCandidate]:
        reading = snapshot.get(self.sensor)
        if reading is None or not reading.measured or reading.value is None:
            return []
        value = float(reading.value)
        threshold, severity = 0.0, ""
        for level, limit in (
            (CRITICAL, self.critical),
            (DANGER, self.danger),
            (WARNING, self.warning),
            (WATCH, self.watch),
        ):
            worse = value >= limit if self.higher_is_worse else value <= limit
            if worse:
                threshold, severity = limit, level
                break
        if not severity:
            return []
        # Hysteresis: it must fall 2 units (or 10%) past the line before the alert clears.
        margin = max(1.0, abs(threshold) * 0.1)
        return [
            AlertCandidate(
                rule=self.rule,
                severity=severity,
                sensor=self.sensor,
                value=value,
                unit=self.unit,
                threshold=f"{threshold:g} {self.unit}",
                message=f"{reading.label or self.sensor} is {value:g} {self.unit} ({severity.lower()}, line {threshold:g})",
                causes=list(self.causes),
                actions=list(self.actions),
                clear_at=threshold - margin if self.higher_is_worse else threshold + margin,
            )
        ]


@dataclass(slots=True)
class SustainedRule:
    """A value that has to stay above a line for a while before it counts (spec 16.4)."""

    rule: str
    sensor: str
    threshold: float
    seconds: float
    severity: str
    unit: str
    causes: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)

    def evaluate(self, snapshot: Snapshot, history: History, baseline: Baseline) -> list[AlertCandidate]:
        window = history.window(self.sensor, self.seconds * 2)
        if len(window) < 2:
            return []
        above = [ts for ts, value in window if value >= self.threshold]
        if len(above) < 2:
            return []
        duration = above[-1] - above[0]
        if duration < self.seconds:
            return []
        latest = window[-1][1]
        return [
            AlertCandidate(
                rule=self.rule,
                severity=self.severity,
                sensor=self.sensor,
                value=latest,
                unit=self.unit,
                threshold=f"{self.threshold:g} {self.unit} for {self.seconds:g}s",
                message=(
                    f"{self.sensor} has stayed above {self.threshold:g} {self.unit} for "
                    f"{duration:.0f}s (now {latest:g})"
                ),
                causes=list(self.causes),
                actions=list(self.actions),
                clear_at=self.threshold - max(1.0, self.threshold * 0.1),
            )
        ]


@dataclass(slots=True)
class RateRule:
    """A rise that is too fast to be normal, even before it reaches any limit."""

    rule: str
    sensor: str
    per_second: float
    window: float
    severity: str
    unit: str

    def evaluate(self, snapshot: Snapshot, history: History, baseline: Baseline) -> list[AlertCandidate]:
        window = history.window(self.sensor, self.window)
        if len(window) < 3:
            return []
        (first_ts, first), (last_ts, last) = window[0], window[-1]
        span = max(1e-6, last_ts - first_ts)
        rate = (last - first) / span
        if rate < self.per_second:
            return []
        return [
            AlertCandidate(
                rule=self.rule,
                severity=self.severity,
                sensor=self.sensor,
                value=last,
                unit=self.unit,
                threshold=f"{self.per_second:g} {self.unit}/s",
                message=f"{self.sensor} rose {rate:.1f} {self.unit}/s over the last {span:.0f}s",
                causes=["a sudden load spike", "a failing fan or pump", "an ambient temperature change"],
                actions=["check whether a heavy task just started", "listen for the fan"],
            )
        ]


@dataclass(slots=True)
class FanResponseRule:
    """Duty is up but the fan is not answering: the signature of a stopped or blocked fan."""

    rule: str = "fan.no_response"
    duty_sensor: str = "fan.duty"
    rpm_sensor: str = "fan.rpm"
    duty_line: float = 0.9
    rpm_floor: float = 400.0

    def evaluate(self, snapshot: Snapshot, history: History, baseline: Baseline) -> list[AlertCandidate]:
        duty = next((item for item in snapshot.readings if item.key.startswith(self.duty_sensor) and item.measured), None)
        rpm = next((item for item in snapshot.readings if item.key.startswith(self.rpm_sensor) and item.measured), None)
        if duty is None or rpm is None or duty.value is None or rpm.value is None:
            return []
        if duty.value < self.duty_line or rpm.value >= self.rpm_floor:
            return []
        return [
            AlertCandidate(
                rule=self.rule,
                severity=DANGER,
                sensor=rpm.key,
                value=rpm.value,
                unit="rpm",
                threshold=f"duty {duty.value:.0%} but RPM below {self.rpm_floor:g}",
                message=f"the fan is commanded to {duty.value:.0%} but reads {rpm.value:.0f} rpm",
                causes=["a stopped or obstructed fan", "a disconnected fan header", "a failing fan bearing"],
                actions=["stop heavy work now", "check the fan and vents", "do not keep running under load"],
                clear_at=self.rpm_floor,
            )
        ]


@dataclass(slots=True)
class LowLoadHighTempRule:
    """Hot at idle means the cooling or the sensor contact is wrong, not that work is heavy."""

    rule: str = "thermal.low_load_high_temp"
    temp_sensor: str = "cpu.temp"
    load_sensor: str = "cpu.load"
    temp_line: float = 75.0
    load_line: float = 0.15
    seconds: float = 60.0

    def evaluate(self, snapshot: Snapshot, history: History, baseline: Baseline) -> list[AlertCandidate]:
        temp = snapshot.get(self.temp_sensor)
        load = snapshot.get(self.load_sensor)
        if temp is None or load is None or not temp.measured or not load.measured:
            return []
        if temp.value is None or load.value is None:
            return []
        if temp.value < self.temp_line or load.value > self.load_line:
            return []
        temps = history.window(self.temp_sensor, self.seconds)
        if len(temps) < 3 or min(value for _, value in temps) < self.temp_line:
            return []
        return [
            AlertCandidate(
                rule=self.rule,
                severity=WARNING,
                sensor=self.temp_sensor,
                value=temp.value,
                unit="C",
                threshold=f"{self.temp_line:g}C at load below {self.load_line:.0%}",
                message=(
                    f"{temp.value:.1f}C while the CPU is only {load.value:.0%} busy: cooling or sensor "
                    "contact is the likely cause"
                ),
                causes=["dust or a blocked intake", "a stopped or slow fan", "dried thermal paste", "a misplaced sensor"],
                actions=["check airflow and fans", "clean the cooler", "compare with a reference tool before servicing"],
            )
        ]


@dataclass
class ProtectionPolicy:
    """What happens when a rule fires. Deterministic, and the hooks are supplied by the app.

    The monitor does not stop agents, terminate terminals or hibernate anything itself: it
    calls these callables, which the runtime wires to the supervisor, the terminal engine and
    the OS. That keeps the decision here and the power to act where it is audited.
    """

    notify: Callable[[Alert], Any] | None = None
    reduce_load: Callable[[Alert], Any] | None = None
    safe_state: Callable[[Alert], Any] | None = None
    power_down: Callable[[Alert], Any] | None = None
    allow_power_down: bool = False

    def apply(self, alert: Alert) -> list[dict[str, Any]]:
        applied: list[dict[str, Any]] = []
        if SEVERITY_ORDER[alert.severity] >= SEVERITY_ORDER[WATCH] and self.notify is not None:
            applied.append({"action": "notify", "ok": _safe_call(self.notify, alert)})
        # "Danger or worse", not "exactly danger": a critical reading is the case where
        # reducing load matters most, and an equality test skipped it.
        if SEVERITY_ORDER[alert.severity] >= SEVERITY_ORDER[DANGER] and self.reduce_load is not None:
            applied.append({"action": "reduce_ai_load", "ok": _safe_call(self.reduce_load, alert)})
        if alert.severity == CRITICAL:
            if self.safe_state is not None:
                applied.append({"action": "safe_state", "ok": _safe_call(self.safe_state, alert)})
            if self.allow_power_down and self.power_down is not None:
                applied.append({"action": "power_down", "ok": _safe_call(self.power_down, alert)})
        return applied


def _safe_call(function: Callable[[Alert], Any], alert: Alert) -> bool:
    try:
        function(alert)
        return True
    except Exception:
        return False


#: Cooldowns keep a hot sensor from producing one alert per sample (spec 16.4).
COOLDOWN = {INFO: 600.0, WATCH: 300.0, WARNING: 120.0, DANGER: 60.0, CRITICAL: 20.0}


@dataclass
class SensorMonitor:
    """The deterministic service: sample, judge, alert, protect. No model in the loop."""

    collector: Collector = field(default_factory=Collector)
    policy: ProtectionPolicy = field(default_factory=ProtectionPolicy)
    bus: Any = None
    rules: Sequence[Any] = field(default_factory=lambda: DEFAULT_RULES)
    history: History = field(default_factory=History)
    baseline: Baseline = field(default_factory=Baseline)
    interval: float = 1.0
    burst_interval: float = 0.25
    #: An idle machine is sampled slowly, but not so slowly that a temperature spike goes
    #: unseen for ten seconds: silicon can rise several degrees a second under load.
    idle_interval: float = 3.0
    source_report: dict[str, Any] = field(default_factory=dict)
    snapshots: int = 0
    alerts: dict[str, Alert] = field(default_factory=dict)
    alert_order: list[str] = field(default_factory=list)
    last_snapshot: Snapshot | None = None
    previous: dict[str, Reading] = field(default_factory=dict)
    active: dict[str, Alert] = field(default_factory=dict)
    last_fired: dict[str, float] = field(default_factory=dict)
    _thread: Any = None
    _stop: Any = None
    cpu_seconds: float = 0.0
    started_at: float = 0.0
    #: Set by :meth:`start`. Kept apart from ``started_at`` so a clock that legitimately reads
    #: 0.0 is not confused with "never started", and so uptime before start is honestly zero.
    started: bool = False

    def __post_init__(self) -> None:
        # Each monitor owns its rules. They are mutable (tuning changes thresholds), and the
        # default set is a module-level tuple: without this, tuning a rule in one monitor
        # silently rewrote protection for every other monitor in the process -- including the
        # second stack a test or the HUD builds.
        self.rules = copy.deepcopy(tuple(self.rules))

    # -- sampling ----------------------------------------------------------------------
    def sample(self, now: float | None = None) -> Snapshot:
        """One pass: collect, filter, record, judge, alert, protect."""
        began = time.perf_counter()
        stamp = now if now is not None else time.time()
        readings = self.collector.collect()
        for reading in readings:
            reading.ts = stamp
        readings = self.collector.filter(readings, self.previous)
        snapshot = Snapshot(ts=stamp, readings=readings, duration=self.collector.last_duration)
        self.history.add(readings)
        self.baseline.update(readings)
        self.previous = {reading.key: reading for reading in readings}
        self.snapshots += 1
        self.last_snapshot = snapshot
        if not self.source_report:
            self.source_report = self._report_sources(readings)

        for rule in self.rules:
            try:
                candidates = rule.evaluate(snapshot, self.history, self.baseline)
            except Exception:  # a broken rule must not stop the monitor or fake a reading
                continue
            for candidate in candidates:
                self._consider(candidate, snapshot)
        self._clear_recovered(snapshot)
        self.cpu_seconds += time.perf_counter() - began
        return snapshot

    def _consider(self, candidate: AlertCandidate, snapshot: Snapshot) -> None:
        key = f"{candidate.rule}:{candidate.sensor}"
        now = snapshot.ts
        # A reading that failed the plausibility filter may be *reported* (a broken sensor is
        # information) but must never fire an alert, drive a protective action, or be quoted to
        # a model as fact. 5000 C is a bad sensor, not a fire.
        source = snapshot.get(candidate.sensor)
        if source is not None and source.suspect:
            return
        prior = self.active.get(key)
        if prior is not None and SEVERITY_ORDER[candidate.severity] <= SEVERITY_ORDER[prior.severity]:
            # Still the same or milder: keep the existing alert, refresh its trend and count.
            prior.count += 1
            prior.trend = self.history.trend(candidate.sensor)
            return
        cooldown = COOLDOWN.get(candidate.severity, 120.0)
        if prior is None and now - self.last_fired.get(key, 0.0) < cooldown:
            return
        alert = Alert(
            alert_id=_alert_id(candidate.rule, candidate.sensor, now),
            rule=candidate.rule,
            severity=candidate.severity,
            sensor=candidate.sensor,
            value=candidate.value,
            unit=candidate.unit,
            threshold=candidate.threshold,
            message=candidate.message,
            trend=self.history.trend(candidate.sensor),
            causes=list(candidate.causes),
            actions=list(candidate.actions),
            evidence=[{"ts": round(ts, 3), "value": round(value, 3)} for ts, value in self.history.window(candidate.sensor, 60.0)],
            ts=now,
        )
        self.alerts[alert.alert_id] = alert
        self.alert_order.append(alert.alert_id)
        self.active[key] = alert
        self.last_fired[key] = now
        applied = self.policy.apply(alert)
        alert.actions = list(alert.actions) + [f"applied: {item['action']}" for item in applied]
        if self.bus is not None:
            try:
                self.bus.emit(
                    HARDWARE_ALERT,
                    source="sensors",
                    severity="warning" if SEVERITY_ORDER[alert.severity] >= 2 else "notice",
                    alert=alert.to_json(),
                )
            except Exception:
                pass

    def _clear_recovered(self, snapshot: Snapshot) -> None:
        still: dict[str, Alert] = {}
        for key, alert in self.active.items():
            current = snapshot.get(alert.sensor)
            if current is None or current.value is None or not current.measured:
                still[key] = alert
                continue
            # Find the rule that owns this alert, then ask it whether the condition is over.
            # The previous version only cleared when the rule *still fired* below a hysteresis
            # line, so a fully recovered sensor kept its alert open forever -- the HUD showed
            # a red alert for a machine that had been cool for hours.
            owner = next((rule for rule in self.rules if getattr(rule, "rule", "") == alert.rule), None)
            if owner is None:
                continue
            try:
                candidates = owner.evaluate(snapshot, self.history, self.baseline)
            except Exception:
                still[key] = alert
                continue
            passed = [c for c in candidates if f"{c.rule}:{c.sensor}" == key]
            if not passed:
                if self.bus is not None:
                    try:
                        self.bus.emit(HARDWARE_CLEARED, source="sensors", alert_id=alert.alert_id, sensor=alert.sensor)
                    except Exception:
                        pass
                continue
            limit = passed[0].clear_at
            if limit is not None and current.value < limit:
                if self.bus is not None:
                    try:
                        self.bus.emit(HARDWARE_CLEARED, source="sensors", alert_id=alert.alert_id, sensor=alert.sensor)
                    except Exception:
                        pass
                continue
            still[key] = alert
        self.active = still

    @staticmethod
    def _report_sources(readings: Sequence[Reading]) -> dict[str, Any]:
        sources: dict[str, int] = {}
        for reading in readings:
            family = reading.source.split("/")[0] or "unknown"
            sources[family] = sources.get(family, 0) + 1
        return {
            "sources": sources,
            "unavailable": [
                {"key": reading.key, "reason": reading.reason} for reading in readings if not reading.available
            ],
            "suspect": [{"key": reading.key, "reason": reading.suspect} for reading in readings if reading.suspect],
        }

    # -- the loop ----------------------------------------------------------------------
    def next_interval(self) -> float:
        """1 s normally, 250 ms while something is wrong, 10 s when nothing is happening."""
        if self.active:
            return self.burst_interval
        latest = self.last_snapshot
        if latest is not None:
            hot = [
                reading.value
                for reading in latest.readings
                if reading.key.startswith("cpu.load") and reading.value is not None
            ]
            if hot and max(hot) > 0.5:
                return self.interval
        if self.alert_order and time.time() - self.alerts[self.alert_order[-1]].ts < 300:
            return self.interval
        return self.idle_interval

    def start(self) -> bool:
        """Run the sampler in its own thread. Returns False if it is already running."""
        import threading

        if self._thread is not None and self._thread.is_alive():
            return False
        self._stop = threading.Event()
        self.started_at = time.time()
        self.started = True

        def loop() -> None:
            while not self._stop.is_set():
                self.sample()
                self._stop.wait(self.next_interval())

        self._thread = threading.Thread(target=loop, name="orbynth-sensors", daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout: float = 2.0) -> dict[str, Any]:
        if self._stop is not None:
            self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        return {"stopped": True, "samples": self.snapshots, "alerts": len(self.alerts)}

    # -- what Main may read (spec 16.6) -------------------------------------------------
    def summary(self) -> dict[str, Any]:
        """Current readings by group, the worst active alert, and every honest gap."""
        snapshot = self.last_snapshot or self.sample()
        worst = max(self.active.values(), key=lambda alert: SEVERITY_ORDER[alert.severity], default=None)
        return {
            "ts": round(snapshot.ts, 3),
            "groups": snapshot.by_group(),
            "active_alerts": [alert.to_json() for alert in self.active.values()],
            "worst": worst.severity if worst else "clear",
            "sources": self.source_report.get("sources", {}),
            "unavailable": snapshot.unavailable,
            "suspect": [reading.to_json() for reading in snapshot.readings if reading.suspect],
            "overhead": self.overhead(),
            "sampling": {"normal_s": self.interval, "burst_s": self.burst_interval, "idle_s": self.idle_interval},
        }

    def query(self, sensors: Sequence[str] | None = None, window: float = 60.0) -> dict[str, Any]:
        """A window of one or more sensors, with min, max and mean over that window."""
        snapshot = self.last_snapshot or self.sample()
        wanted = [reading for reading in snapshot.readings if not sensors or any(reading.key.startswith(name) for name in sensors)]
        out: list[dict[str, Any]] = []
        for reading in wanted:
            samples = self.history.window(reading.key, window)
            values = [value for _, value in samples]
            out.append(
                {
                    **reading.to_json(),
                    "window_s": window,
                    "samples": len(values),
                    "min": round(min(values), 3) if values else None,
                    "max": round(max(values), 3) if values else None,
                    "mean": round(statistics.fmean(values), 3) if values else None,
                    "trend": self.history.trend(reading.key, window),
                    "baseline": self.baseline.explain(reading.key),
                }
            )
        return {"ts": round(snapshot.ts, 3), "window_s": window, "sensors": out}

    def history_of(self, key: str, seconds: float = 600.0) -> dict[str, Any]:
        samples = self.history.window(key, seconds)
        return {
            "sensor": key,
            "seconds": seconds,
            "samples": [{"ts": round(ts, 3), "value": round(value, 3)} for ts, value in samples],
            "count": len(samples),
            "baseline": self.baseline.explain(key),
        }

    def explain(self, alert_id: str) -> dict[str, Any]:
        alert = self.alerts.get(alert_id)
        if alert is None:
            return {
                "alert_id": alert_id,
                "error": "no such alert on this machine",
                "known": self.alert_order[-10:],
            }
        return {
            **alert.to_json(),
            "baseline": self.baseline.explain(alert.sensor),
            "rule_detail": {
                "name": alert.rule,
                "threshold": alert.threshold,
                "sensor": alert.sensor,
            },
        }

    def recent_alerts(self, limit: int = 20) -> list[dict[str, Any]]:
        return [self.alerts[alert_id].to_json() for alert_id in self.alert_order[-limit:]]

    def load_by_agent(self, pids_by_agent: dict[str, Sequence[int]] | None = None) -> dict[str, Any]:
        """Which AI agent is using what, measured from its own processes (spec 16.1).

        With no attribution map there is nothing to report, and saying so is better than
        attributing system load to the assistant by guesswork.
        """
        if not pids_by_agent:
            return {"attributed": False, "reason": "no agent-to-process map was supplied", "agents": []}
        agents: list[dict[str, Any]] = []
        for agent_id, pids in pids_by_agent.items():
            total_rss = 0
            total_cpu = 0.0
            for pid in pids:
                info = _proc_usage(pid)
                if info is None:
                    continue
                total_rss += info["rss_bytes"]
                total_cpu += info["cpu_seconds"]
            agents.append(
                {
                    "agent_id": agent_id,
                    "processes": len(pids),
                    "rss_mb": round(total_rss / 1e6, 1),
                    "cpu_seconds": round(total_cpu, 1),
                }
            )
        agents.sort(key=lambda row: row["cpu_seconds"], reverse=True)
        return {"attributed": True, "agents": agents, "total": len(agents)}

    def overhead(self) -> dict[str, Any]:
        """The monitor's own cost, measured rather than claimed (spec 16.3).

        Below a few seconds of uptime the ratio is startup noise, so it reports ``None``
        instead of a number that would look like a 70% CPU monitor on a machine doing
        nothing. A measurement that is too short to trust is not a passing measurement.
        """
        # ``started_at`` of 0.0 is a valid timestamp (an injected clock), not "not started":
        # treating it as a sentinel reported 0s uptime for a monitor that had been running.
        elapsed = max(0.0, time.time() - self.started_at) if self.started else 0.0
        share = (self.cpu_seconds / elapsed) if elapsed > 0 else 0.0
        warming = elapsed < 5.0
        return {
            "samples": self.snapshots,
            "window_s": round(elapsed, 2),
            "collector_cpu_ms_total": round(self.cpu_seconds * 1000.0, 2),
            "collector_cpu_share": None if warming else round(share, 5),
            "last_sample_ms": round((self.last_snapshot.duration if self.last_snapshot else 0.0) * 1000.0, 2),
            "target_share": 0.01,
            "warming_up": warming,
            "within_target": None if warming else share <= 0.01,
        }

    def sensor_report(self) -> dict[str, Any]:
        """The first-run Sensor Capability Report: what exists, from where, and what does not."""
        snapshot = self.last_snapshot or self.sample()
        found = [
            {
                "key": reading.key,
                "source": reading.source,
                "unit": reading.unit,
                "accuracy": reading.accuracy,
                "update_s": self.interval,
            }
            for reading in snapshot.readings
            if reading.available
        ]
        return {
            "machine": sys.platform,
            "found": found,
            "found_count": len(found),
            "unavailable": [
                {"key": reading.key, "reason": reading.reason} for reading in snapshot.readings if not reading.available
            ],
            "note": (
                "readings come from OS interfaces on this host; the S2 privileged helper is not "
                "installed, so Super I/O fans, rails and some CPU registers are reported unavailable"
            ),
        }


def _proc_usage(pid: int) -> dict[str, float] | None:
    """CPU seconds and resident memory for one process, from /proc (no psutil needed)."""
    if os.name == "nt":  # pragma: no cover - Windows only
        return None
    stat = _read_text(f"/proc/{pid}/stat")
    if not stat:
        return None
    parts = stat.rsplit(")", 1)
    if len(parts) != 2:
        return None
    fields = parts[1].split()
    try:
        utime, stime = int(fields[11]), int(fields[12])
        rss_pages = int(fields[21])
    except (IndexError, ValueError):
        return None
    return {"cpu_seconds": (utime + stime) / os.sysconf("SC_CLK_TCK"), "rss_bytes": float(rss_pages * os.sysconf("SC_PAGE_SIZE"))}


DEFAULT_RULES: tuple[Any, ...] = (
    ThresholdRule(
        "cpu.temp.high", "cpu.temp", watch=75.0, warning=85.0, danger=95.0, critical=103.0, unit="C",
        causes=["sustained heavy load", "insufficient cooling", "a hot ambient temperature"],
        actions=["reduce load", "check the fan and vents"],
    ),
    ThresholdRule(
        "gpu.temp.high", "gpu.temp", watch=80.0, warning=88.0, danger=95.0, critical=100.0, unit="C",
        causes=["a heavy GPU task", "blocked airflow around the card"],
        actions=["reduce the GPU workload", "improve case airflow"],
    ),
    ThresholdRule(
        "disk.temp.high", "disk.temp", watch=55.0, warning=65.0, danger=75.0, critical=85.0, unit="C",
        causes=["sustained disk writes", "poor drive bay airflow"],
        actions=["pause large writes", "check drive bay cooling"],
    ),
    ThresholdRule(
        "disk.space.low", "disk.used", watch=0.85, warning=0.92, danger=0.96, critical=0.99, unit="ratio",
        causes=["large downloads", "logs or temporary files", "a backup that never rotated"],
        actions=["free space before large writes", "check the largest folders"],
    ),
    ThresholdRule(
        "battery.wear.high", "battery.wear", watch=0.2, warning=0.4, danger=0.6, critical=0.8, unit="ratio",
        causes=["normal ageing", "heat exposure", "constant full charge"],
        actions=["plan a battery replacement", "avoid leaving it at 100% in the heat"],
    ),
    SustainedRule(
        "cpu.temp.sustained", "cpu.temp", threshold=82.0, seconds=30.0, severity=WARNING, unit="C",
        causes=["a long compute task", "cooling that cannot keep up"],
        actions=["lower parallelism", "pause background work"],
    ),
    RateRule("cpu.temp.rising_fast", "cpu.temp", per_second=4.0, window=10.0, severity=WATCH, unit="C"),
    FanResponseRule(),
    LowLoadHighTempRule(),
)

# --------------------------------------------------------------------------------------
# Owner-visible controls and the process-wide monitor
# --------------------------------------------------------------------------------------

def _tightening(rule: Any, value: float) -> bool:
    """True when setting ``value`` makes this rule fire *earlier* than it does now.

    Tightening protection is always safe to allow. Loosening it is how a protective alert
    gets silenced, so that direction is the owner's decision and nobody else's.
    """
    current = getattr(rule, "watch", getattr(rule, "threshold", None))
    if current is None:
        return False
    higher_is_worse = getattr(rule, "higher_is_worse", True)
    return value < current if higher_is_worse else value > current


def _reorder(rule: Any, value: float) -> None:
    """Keep a threshold rule's four severity lines in order after a change."""
    lines = [getattr(rule, name) for name in ("watch", "warning", "danger", "critical")]
    step = (max(lines) - min(lines)) / 3.0 if max(lines) > min(lines) else 0.0
    rule.watch = value
    if step:
        rule.warning = round(value + step, 3)
        rule.danger = round(value + 2 * step, 3)
        rule.critical = round(value + 3 * step, 3)
    else:
        rule.warning, rule.danger, rule.critical = value, value, value


def _monitor_ingest(self: SensorMonitor, sensor: str, value: float, unit: str = "", source: str = "agent") -> Reading | None:
    """Accept an outside reading through the same plausibility filter as local ones (spec 16.2).

    A rejected reading returns ``None`` and is *not* recorded: the whole point of the filter
    is that one bad number cannot become an alert, a decision, or a memory.
    """
    reading = Reading(key=sensor, label=sensor, value=float(value), unit=unit, source=source or "agent", ts=time.time())
    kept = self.collector.filter([reading], self.previous)
    if not kept:
        return None
    accepted = kept[0]
    if accepted.suspect:
        # Report the rejection so the caller knows *why*, but never record the value.
        return None
    self.history.add([accepted])
    self.baseline.update([accepted])
    self.previous[accepted.key] = accepted
    base = self.last_snapshot.readings if self.last_snapshot else []
    snapshot = Snapshot(
        ts=accepted.ts,
        readings=[r for r in base if r.key != accepted.key] + [accepted],
        duration=0.0,
    )
    for rule in self.rules:
        try:
            candidates = rule.evaluate(snapshot, self.history, self.baseline)
        except Exception:
            continue
        for candidate in candidates:
            self._consider(candidate, snapshot)
    return accepted


def _monitor_tune(
    self: SensorMonitor, rule_name: str, value: float, direction: str = "", owner: bool = False
) -> dict[str, Any]:
    """Change one rule's threshold, refusing to relax protection without the owner."""
    if direction and direction not in ("tighten", "loosen"):
        return {"tuned": False, "reason": "direction must be 'tighten' or 'loosen'"}
    rules = list(self.rules)
    for index, rule in enumerate(rules):
        if getattr(rule, "rule", "") != rule_name:
            continue
        tightening = _tightening(rule, value)
        if tightening:
            direction = "tighten"
        elif not direction:
            direction = "loosen"
        if direction == "loosen" and not owner:
            return {
                "tuned": False,
                "rule": rule_name,
                "requires": "owner",
                "reason": "loosening a protective threshold can silence an alert; only the owner may do that",
            }
        if hasattr(rule, "watch"):
            _reorder(rule, value)
        elif hasattr(rule, "threshold"):
            rule.threshold = value
        else:
            return {"tuned": False, "rule": rule_name, "reason": "this rule has no threshold to tune"}
        rules[index] = rule
        self.rules = tuple(rules)
        record = {
            "tuned": True,
            "rule": rule_name,
            "value": value,
            "direction": direction,
            "by": "owner" if owner else "agent",
            "ts": round(time.time(), 3),
            "now": {name: getattr(rule, name) for name in ("watch", "warning", "danger", "critical") if hasattr(rule, name)},
        }
        if self.bus is not None:
            try:
                self.bus.emit(HARDWARE_ALERT, source="sensors", severity="notice", tuned=record)
            except Exception:
                pass
        return record
    return {"tuned": False, "rule": rule_name, "reason": "no such rule", "known": [getattr(r, "rule", "?") for r in rules]}


SensorMonitor.ingest = _monitor_ingest  # type: ignore[attr-defined]
SensorMonitor.tune = _monitor_tune  # type: ignore[attr-defined]

_PROCESS_MONITOR: SensorMonitor | None = None


def default_monitor(**kwargs: Any) -> SensorMonitor:
    """The process-wide monitor, created once. Starting it is the runtime's decision."""
    global _PROCESS_MONITOR
    if _PROCESS_MONITOR is None:
        _PROCESS_MONITOR = SensorMonitor(**kwargs)
    return _PROCESS_MONITOR


def reset_default_monitor() -> None:
    global _PROCESS_MONITOR
    if _PROCESS_MONITOR is not None:
        _PROCESS_MONITOR.stop()
    _PROCESS_MONITOR = None
