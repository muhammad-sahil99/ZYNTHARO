"""Hardware sensing as agent actions (spec 16.5).

The rules that matter for safety:

* readings pass a *plausibility filter* before they reach an agent, so a stuck sensor cannot
  make the operative believe the machine is on fire (or that it is safe);
* thresholds may be tightened by the operative but never loosened -- loosening is how a
  protective alert gets silenced, and that is the owner's decision, not the agent's;
* protective actions (notify, reduce load, safe state) are executed by the monitor itself,
  not requested from an agent, because an agent may be the thing that is misbehaving.
"""

from __future__ import annotations

from typing import Any, Sequence

from ..ops.sensors import SensorMonitor, default_monitor
from .registry import Action, ActionContext, ActionFailed, action

_monitor: SensorMonitor | None = None


def set_monitor(monitor: SensorMonitor | None) -> None:
    """Install the monitor the actions read from (the runtime passes its wired one)."""
    global _monitor
    _monitor = monitor


def monitor() -> SensorMonitor:
    global _monitor
    if _monitor is None:
        _monitor = default_monitor()
    return _monitor


@action(
    name="hw_summary",
    description=(
        "Read the hardware picture: CPU, memory, battery and power, thermal readings, disk "
        "headroom, network, which sources are available and which are not."
    ),
    capability="hardware.read",
    group="hardware",
    parameters={"type": "object", "properties": {}},
)
def hw_summary(ctx: ActionContext) -> Any:
    return monitor().query()


@action(
    name="hw_query",
    description=(
        "Read selected sensors with min, max, mean, trend and baseline over a window. Use this "
        "instead of guessing whether a number is normal."
    ),
    capability="hardware.read",
    group="hardware",
    parameters={
        "type": "object",
        "properties": {
            "sensors": {"type": "array", "items": {"type": "string"}, "description": "Sensor key prefixes, e.g. ['cpu.temp']."},
            "window": {"type": "number", "description": "Seconds of history to summarise, default 60."},
        },
    },
)
def hw_query(ctx: ActionContext, sensors: Sequence[str] | None = None, window: float = 60.0) -> Any:
    return monitor().query(list(sensors) if sensors else None, window)


@action(
    name="hw_history",
    description="Read the raw samples for one sensor to judge a trend rather than a single reading.",
    capability="hardware.read",
    group="hardware",
    parameters={
        "type": "object",
        "properties": {"sensor": {"type": "string"}, "seconds": {"type": "number"}},
        "required": ["sensor"],
    },
)
def hw_history(ctx: ActionContext, sensor: str, seconds: float = 120.0) -> Any:
    return monitor().history_of(sensor, seconds)


@action(
    name="hw_alerts",
    description="List recent sensor alerts: severity, rule, the reading that fired, the trend and what was done.",
    capability="hardware.read",
    group="hardware",
    parameters={"type": "object", "properties": {"limit": {"type": "integer"}}},
)
def hw_alerts(ctx: ActionContext, limit: int = 20) -> Any:
    alerts = monitor().recent_alerts(limit)
    return {"count": len(alerts), "alerts": alerts}


@action(
    name="hw_explain",
    description=(
        "Explain one alert: the rule, the threshold, the reading, the evidence window, the "
        "baseline and which protective actions ran."
    ),
    capability="hardware.read",
    group="hardware",
    parameters={"type": "object", "properties": {"alert_id": {"type": "string"}}, "required": ["alert_id"]},
)
def hw_explain(ctx: ActionContext, alert_id: str) -> Any:
    found = monitor().explain(alert_id)
    if "error" in found:
        raise ActionFailed(f"no alert with id {alert_id!r}; known: {found.get('known')}")
    return found


@action(
    name="hw_report",
    description=(
        "Report an outside reading (a paired phone's battery, a room sensor) so the same "
        "plausibility filter and rules apply. Rejected readings are returned as such."
    ),
    capability="hardware.read",
    group="hardware",
    parameters={
        "type": "object",
        "properties": {
            "sensor": {"type": "string"},
            "value": {"type": "number"},
            "unit": {"type": "string"},
            "source": {"type": "string"},
        },
        "required": ["sensor", "value"],
    },
)
def hw_report(ctx: ActionContext, sensor: str, value: float, unit: str = "", source: str = "agent") -> Any:
    reading = monitor().ingest(sensor, value, unit=unit, source=source)  # type: ignore[attr-defined]
    if reading is None:
        return {"accepted": False, "reason": "the reading failed the plausibility filter and was not recorded"}
    return {"accepted": True, "reading": reading.to_json()}


@action(
    name="hw_load",
    description="Which agent is using what, measured from the processes attributed to each agent.",
    capability="hardware.read",
    group="hardware",
    parameters={"type": "object", "properties": {"pids_by_agent": {"type": "object"}}},
)
def hw_load(ctx: ActionContext, pids_by_agent: dict[str, list[int]] | None = None) -> Any:
    return monitor().load_by_agent(pids_by_agent)


@action(
    name="hw_report_card",
    description="The first-run Sensor Capability Report: every sensor found and every one that is not.",
    capability="hardware.read",
    group="hardware",
    parameters={"type": "object", "properties": {}},
)
def hw_report_card(ctx: ActionContext) -> Any:
    return monitor().sensor_report()


@action(
    name="hw_tune",
    description=(
        "Adjust a rule threshold. Tightening is allowed; loosening requires the owner because it "
        "can silence a protective alert."
    ),
    capability="hardware.tune",
    group="hardware",
    danger="medium",
    parameters={
        "type": "object",
        "properties": {
            "rule": {"type": "string"},
            "value": {"type": "number"},
            "direction": {"type": "string", "enum": ["tighten", "loosen"]},
        },
        "required": ["rule", "value"],
    },
)
def hw_tune(ctx: ActionContext, rule: str, value: float, direction: str = "") -> Any:
    owner = bool(getattr(ctx, "owner_approved", False))
    result = monitor().tune(rule, value, direction=direction, owner=owner)  # type: ignore[attr-defined]
    if not result.get("tuned") and result.get("requires") == "owner":
        raise ActionFailed(result["reason"], result)
    return result


all_actions = lambda: [hw_summary, hw_query, hw_history, hw_alerts, hw_explain, hw_report, hw_load, hw_report_card, hw_tune]  # noqa: E731