"""Action layer: every capability that touches the computer, plus the gate they pass through.

``build_registry`` is the single place that assembles the tool surface. Drivers that need a
display or a native dependency are still registered — they fail with a precise, actionable
message when called on a machine that cannot support them, which is far better than a
missing tool the model cannot reason about.
"""

from __future__ import annotations

from typing import Any, Sequence

from ..config import AppConfig
from ..events import EventBus
from ..llm.providers import ToolSpec
from . import (
    browser,
    channels,
    connectors,
    filesystem,
    fleet,
    hardware,
    input_control,
    localsearch,
    office,
    office_files,
    orchestration,
    presence,
    registry as registry_module,
    schedule,
    shell,
    terminal,
    vision,
    whatsapp,
    windows,
)
from .registry import (
    Action,
    ActionContext,
    ActionFailed,
    ActionResult,
    ActionRegistry,
    CapabilityError,
    ConfirmationRequired,
    DriverUnavailable,
    action,
)

DRIVER_MODULES = (
    registry_module,
    channels,
    connectors,
    filesystem,
    shell,
    terminal,
    hardware,
    input_control,
    localsearch,
    vision,
    windows,
    office,
    office_files,
    browser,
    whatsapp,
    orchestration,
    fleet,
    presence,
    schedule,
)


def build_registry(
    config: AppConfig | None = None,
    bus: EventBus | None = None,
    memory: Any = None,
    services: orchestration.Services | None = None,
    capabilities: bool = True,
) -> ActionRegistry:
    """Assemble every action into one registry and wire orchestration services.

    A Capability Matrix is attached by default (spec 4.7): it is what stops a tool whose
    driver is missing on this machine from being offered to a model at all, and what labels
    the tools nobody has proven yet. Pass ``capabilities=False`` only to inspect the raw
    declared surface, for example in the matrix generator itself.
    """
    registry = ActionRegistry(bus=bus, memory=memory, config=config)
    for module in DRIVER_MODULES:
        factory = getattr(module, "all_actions", None)
        if callable(factory):
            registry.register_all(factory())
        else:  # pragma: no cover - defensive
            registry.register_module(module)
    if services is not None:
        orchestration.set_services(services)
    if capabilities:
        from ..ops.capability import AuditRunStats, CapabilityMatrix

        registry.capabilities = CapabilityMatrix(
            registry,
            stats=AuditRunStats(memory) if memory is not None else None,
        )
    return registry


def driver_report() -> list[dict[str, Any]]:
    """Which drivers can actually execute on this machine (used by ``zyntharo doctor``)."""
    import os
    import platform

    checks: list[tuple[str, str, str]] = [
        ("input", "pyautogui", "pip install pyautogui"),
        ("vision", "mss", "pip install mss"),
        ("vision", "PIL", "pip install Pillow"),
        ("vision", "rapidocr_onnxruntime", "pip install rapidocr-onnxruntime"),
        ("browser", "playwright", "pip install playwright && playwright install chromium"),
        ("browser", "websocket", "pip install websocket-client"),
        ("voice", "sounddevice", "pip install sounddevice"),
        ("voice", "faster_whisper", "pip install faster-whisper"),
        ("hud", "webview", "pip install pywebview"),
    ]
    if os.name == "nt":
        checks += [("office", "win32com.client", "pip install pywin32"), ("windows", "uiautomation", "pip install uiautomation")]
    rows: list[dict[str, Any]] = []
    for driver, module, hint in checks:
        try:
            __import__(module)
            rows.append({"driver": driver, "module": module, "available": True, "hint": ""})
        except Exception:
            rows.append({"driver": driver, "module": module, "available": False, "hint": hint})
    rows.append({"driver": "platform", "module": f"{platform.system()} {platform.release()}", "available": True, "hint": ""})
    return rows


__all__ = [
    "Action",
    "ActionContext",
    "ActionFailed",
    "ActionResult",
    "ActionRegistry",
    "CapabilityError",
    "ConfirmationRequired",
    "DriverUnavailable",
    "action",
    "build_registry",
    "driver_report",
    "orchestration",
    "connectors",
    "fleet",
    "filesystem",
    "shell",
    "terminal",
    "hardware",
    "input_control",
    "localsearch",
    "vision",
    "windows",
    "office",
    "office_files",
    "browser",
    "whatsapp",
]
