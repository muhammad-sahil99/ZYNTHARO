"""Build order and capacity (contract 22.5).

The original plan froze the contracts and then ran fifteen-plus agents in parallel. That is
the riskiest possible sequence: nothing had run end to end, so every contract was a guess
being parallelised.

The order this module enforces instead:

* **M0 first: one real task through every layer.** One task that genuinely traverses plan ->
  model -> gated action -> driver -> evidence -> report -> memory -> HUD before any wide
  fan-out. A slice that works end to end is worth more than forty agents building unproven
  interfaces.
* **Then waves.** The Lead writes a capacity plan, and work proceeds in waves of a size the
  host can actually run.
* **If the host cannot run agents in parallel, the same waves run one at a time.** Nothing is
  dropped; it just takes longer, and the plan says which mode it is in.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Sequence

from ..events import EventBus, TASK_ASSIGNED, TASK_CREATED, TASK_DONE

#: A host that can hold this many agents at once is not common; three is the honest default.
DEFAULT_WAVE = 3
HARD_CEILING = 12


@dataclass
class CapacityPlan:
    per_wave: int = DEFAULT_WAVE
    parallel: bool = True
    total_items: int = 0
    waves: int = 0
    reason: str = ""
    source: str = "default"
    created_at: float = field(default_factory=time.time)

    def to_json(self) -> dict[str, Any]:
        return {**asdict(self), "one_line": self.one_line()}

    def one_line(self) -> str:
        mode = "parallel" if self.parallel else "one at a time"
        return f"{self.total_items} items in {self.waves} wave(s) of {self.per_wave}, {mode} ({self.source})"


def probe_capacity(
    configured: int = 0,
    env: dict[str, str] | None = None,
    total_items: int = 0,
) -> CapacityPlan:
    """Decide how wide this host may go, preferring an explicit, documented setting.

    Order of authority: ``ZYNTHARO_AGENT_PARALLELISM`` (an operator's explicit choice, and the
    hook a build CLI uses), then the configured agent ceiling, then the conservative default.
    Setting it to 1 is the supported way to say "this CLI cannot run agents concurrently".
    """
    env = env if env is not None else dict(os.environ)
    raw = env.get("ZYNTHARO_AGENT_PARALLELISM", "").strip()
    source = "default"
    per_wave = DEFAULT_WAVE
    if raw:
        try:
            per_wave = max(1, min(int(raw), HARD_CEILING))
            source = "ZYNTHARO_AGENT_PARALLELISM"
        except ValueError:
            source = "default (unparsable ZYNTHARO_AGENT_PARALLELISM)"
    elif configured > 0:
        per_wave = max(1, min(configured, HARD_CEILING))
        source = "config.safety.max_parallel_agents"
    parallel = per_wave > 1 and not env.get("ZYNTHARO_NO_PARALLEL")
    if not parallel:
        per_wave = 1
        source += " (sequential fallback)"
    waves = max(1, -(-total_items // per_wave)) if total_items else 0
    return CapacityPlan(per_wave=per_wave, parallel=parallel, total_items=total_items, waves=waves, source=source)


def plan_waves(items: Sequence[Any], per_wave: int = DEFAULT_WAVE) -> list[list[Any]]:
    size = max(1, per_wave)
    return [list(items[i : i + size]) for i in range(0, len(items), size)]


def run_waves(
    orchestrator: Any,
    objective: str,
    items: Sequence[Any],
    parent: Any = None,
    per_wave: int = 0,
    stop_on_failure: bool = True,
    bus: EventBus | None = None,
) -> dict[str, Any]:
    """Execute work in waves. Each wave is dispatched in parallel only when capacity allows."""
    bus = bus or getattr(orchestrator, "bus", None) or EventBus()
    plan = probe_capacity(configured=getattr(getattr(orchestrator, "config", None), "safety", None).max_parallel_agents if getattr(orchestrator, "config", None) else 0, total_items=len(items))
    if per_wave:
        plan.per_wave = max(1, per_wave)
        plan.parallel = plan.per_wave > 1
        plan.waves = max(1, -(-len(items) // plan.per_wave))
        plan.source = "explicit"
    waves = plan_waves(items, plan.per_wave)
    bus.emit(TASK_CREATED, source="planning", objective=objective[:200], waves=len(waves), plan=plan.to_json())

    reports: list[dict[str, Any]] = []
    stopped = ""
    for index, wave in enumerate(waves, start=1):
        futures = []
        for item in wave:
            future = orchestrator.dispatch(item, parent=parent, wait=not plan.parallel)
            futures.append((item, future))
        for item, future in futures:
            try:
                report = future if not plan.parallel else future.result()
            except Exception as exc:
                report = None
                reports.append({"wave": index, "name": getattr(item, "name", "?"), "status": "failed", "summary": f"dispatch failed: {exc!r}"})
                continue
            payload = report.to_json() if hasattr(report, "to_json") else dict(report or {})
            payload["wave"] = index
            reports.append(payload)
        if stop_on_failure and any(r.get("status") in {"failed", "blocked"} for r in reports if r.get("wave") == index):
            stopped = f"wave {index} reported a failure; later waves were not started"
            bus.emit(TASK_DONE, source="planning", severity="warning", objective=objective[:120], stopped=stopped)
            break
    bus.emit(TASK_DONE, source="planning", objective=objective[:200], waves_run=len(waves), reports=len(reports))
    return {
        "objective": objective,
        "plan": plan.to_json(),
        "plan_line": plan.one_line(),
        "waves": len(waves),
        "reports": reports,
        "stopped": stopped,
    }


def write_capacity_plan(path: str | Path, plan: CapacityPlan, backend: str = "python-threads") -> Path:
    """Persist the plan next to the build output so it can be reviewed, not guessed at."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"backend": backend, "written_at": time.time(), **plan.to_json()}, indent=2),
        encoding="utf-8",
    )
    return target


#: The milestone order, so "are we done?" has one answer (see tools/release_gate.py).
MILESTONES: tuple[dict[str, str], ...] = (
    {"id": "M0", "name": "One real task through every layer", "exit": "a task reaches an OS driver, is verified, reported, remembered and shown on the HUD"},
    {"id": "M1", "name": "Contracts frozen against a working slice", "exit": "the M0 slice passes on two clean machines"},
    {"id": "M2", "name": "Action surface complete", "exit": "every declared action has a driver behind it or an honest DriverUnavailable"},
    {"id": "M3", "name": "Fleet and self-extension", "exit": "teams, Steward failover and plugin authoring verified end to end"},
    {"id": "M4", "name": "Interface and voice", "exit": "voice-first session with live HUD wiring on real hardware"},
    {"id": "GA", "name": "General availability", "exit": "installer on three OSes, zero coding environment, no blocking acceptance test unwaived"},
)
