"""Plugin system: turning repeated work into permanent, first-class capability.

The requirement was precise: when the system notices it is doing something the same way again
and again, it must write *real working code* and install it, so the next time it calls an
action directly instead of re-running vision+click+type.

How a plugin works here:

* A plugin is a directory under ``<home>/plugins/<name>`` containing ``plugin.json`` (the
  manifest) and a Python module that declares actions with the same ``@action`` decorator the
  built-in drivers use. A plugin action is therefore *indistinguishable* from a built-in one:
  same capability gate, same audit trail, same HUD events, same tool schema for the model.
* **Install is transactional.** Validate manifest → compile the module → run its self-test in
  a throwaway registry → only then activate. Any failure leaves the previous state untouched.
* **Capabilities are scoped by the manifest.** A plugin may only declare actions inside the
  capabilities its manifest requests, and those are in turn granted by the install policy.
  A plugin that requests ``shell.exec`` and was granted only ``filesystem.read`` fails
  validation rather than silently escalating.
* **Determinism is required.** A plugin must not depend on screen geometry when a stable
  interface exists, must not hold secrets, and must ship a self-test — because a plugin with
  no test is a liability that will be trusted by other agents.
"""

from __future__ import annotations

import importlib.util
import json
import os
import py_compile
import shutil
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..config import expand_capabilities
from ..events import PLUGIN_FAILED, PLUGIN_INSTALLED, PLUGIN_UNINSTALLED, EventBus
from ..actions.registry import Action, ActionContext, ActionRegistry

MANIFEST_NAME = "plugin.json"
SCHEMA_VERSION = 1


@dataclass
class PluginManifest:
    name: str
    version: str = "0.1.0"
    description: str = ""
    author: str = "ORBYNTH Artificer"
    entry: str = "actions.py"
    kind: str = "python"
    capabilities: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    schema_version: int = SCHEMA_VERSION
    source: str = "forged"  # forged | manual | imported
    task: str = ""
    runtime: str = ""  # e.g. "chrome:profile=default" for browser-bound plugins

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "PluginManifest":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    declared_actions: list[str] = field(default_factory=list)
    undeclared_actions: list[str] = field(default_factory=list)
    self_test: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


class PluginHost:
    """Installs, validates, loads and removes plugins."""

    def __init__(
        self,
        home: str | os.PathLike[str],
        registry: ActionRegistry,
        bus: EventBus | None = None,
        granted_capabilities: Sequence[str] | None = None,
        memory: Any = None,
    ):
        self.home = Path(home)
        self.plugins_dir = self.home / "plugins"
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        self.registry = registry
        self.bus = bus or EventBus()
        self.memory = memory
        #: Fleet-wide ceiling on what any plugin may request.
        self.grant_policy = expand_capabilities(list(granted_capabilities or ["*"]))
        self.loaded: dict[str, PluginManifest] = {}
        self.modules: dict[str, Any] = {}

    # ----------------------------------------------------------------------------------
    # Discovery
    # ----------------------------------------------------------------------------------
    def manifest_path(self, name: str) -> Path:
        return self.plugins_dir / name / MANIFEST_NAME

    def installed(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for directory in sorted(self.plugins_dir.iterdir() if self.plugins_dir.exists() else []):
            manifest_file = directory / MANIFEST_NAME
            if not directory.is_dir() or not manifest_file.exists():
                continue
            try:
                manifest = PluginManifest.from_json(json.loads(manifest_file.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                rows.append({"name": directory.name, "status": "corrupt", "error": "unreadable plugin.json"})
                continue
            rows.append({**manifest.to_json(), "status": "loaded" if manifest.name in self.loaded else "installed"})
        return rows

    # ----------------------------------------------------------------------------------
    # Validation
    # ----------------------------------------------------------------------------------
    def validate(self, directory: Path, run_self_test: bool = True) -> ValidationResult:
        errors: list[str] = []
        warnings: list[str] = []
        manifest_file = directory / MANIFEST_NAME
        if not manifest_file.exists():
            return ValidationResult(False, [f"missing {MANIFEST_NAME}"])
        try:
            manifest = PluginManifest.from_json(json.loads(manifest_file.read_text(encoding="utf-8")))
        except json.JSONDecodeError as exc:
            return ValidationResult(False, [f"{MANIFEST_NAME} is not valid JSON: {exc}"])
        if manifest.schema_version != SCHEMA_VERSION:
            errors.append(f"unsupported schema_version {manifest.schema_version} (expected {SCHEMA_VERSION})")
        if not manifest.name.replace("-", "").replace("_", "").isalnum():
            errors.append("name must be alphanumeric with - or _")
        module_path = directory / manifest.entry
        if not module_path.exists():
            errors.append(f"entry module missing: {manifest.entry}")
            return ValidationResult(False, errors, warnings)
        # Compile check: a plugin that does not compile must never be installed.
        try:
            py_compile.compile(str(module_path), doraise=True, cfile=str(directory / ".compile-check.pyc"))
        except py_compile.PyCompileError as exc:
            errors.append(f"compilation failed: {exc.msg if hasattr(exc, 'msg') else exc}")
        # Capability ceiling.
        forbidden = sorted(set(manifest.capabilities) - set(self.grant_policy))
        if forbidden:
            errors.append(f"requested capabilities exceed the install policy: {forbidden}")
        # Load into a scratch registry and inspect what it declares.
        declared: list[str] = []
        undeclared: list[str] = []
        self_test: dict[str, Any] = {}
        if not errors or run_self_test:
            try:
                module = _import_module(manifest.name, module_path)
                found = _collect_actions(module)
                declared = sorted(found)
                undeclared = sorted(set(found) - set(manifest.actions)) if manifest.actions else []
                if undeclared:
                    warnings.append(f"actions not listed in the manifest: {undeclared}")
                outside = sorted({found[name].capability for name in found} - set(expand_capabilities(manifest.capabilities)))
                if outside:
                    errors.append(f"actions use capabilities outside the manifest grant: {outside}")
                if run_self_test and hasattr(module, "self_test"):
                    # A self-test must see the real action surface, or a plugin that chains
                    # existing actions would look broken when it is not.
                    scratch = ActionRegistry(bus=self.bus, memory=self.memory)
                    for existing in self.registry.names():
                        action_obj = self.registry.get(existing)
                        if action_obj is not None:
                            scratch.register(action_obj)
                    for name in found:
                        scratch.register(found[name])
                    try:
                        self_test = module.self_test(scratch) or {"ok": True}
                    except Exception as exc:  # noqa: BLE001 - reported, never raised
                        self_test = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                    if not self_test.get("ok"):
                        errors.append(f"self-test failed: {self_test.get('error', 'unknown')}")
                elif run_self_test:
                    warnings.append("no self_test(registry) function: plugin is untested")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"import failed: {type(exc).__name__}: {exc}")
        return ValidationResult(not errors, errors, warnings, declared, undeclared, self_test)

    # ----------------------------------------------------------------------------------
    # Install / uninstall
    # ----------------------------------------------------------------------------------
    def install(self, source: str | os.PathLike[str], activate: bool = True, replace: bool = False) -> dict[str, Any]:
        source_dir = Path(source)
        if not source_dir.is_dir():
            raise FileNotFoundError(f"plugin source directory does not exist: {source_dir}")
        manifest_file = source_dir / MANIFEST_NAME
        if not manifest_file.exists():
            raise ValueError(f"no {MANIFEST_NAME} in {source_dir}")
        manifest = PluginManifest.from_json(json.loads(manifest_file.read_text(encoding="utf-8")))
        target = self.plugins_dir / manifest.name
        if target.exists() and not replace:
            raise FileExistsError(f"plugin {manifest.name} is already installed (pass replace=True)")
        validation = self.validate(source_dir)
        if not validation.ok:
            self.bus.emit(
                PLUGIN_FAILED,
                source="plugin_host",
                severity="error",
                plugin=manifest.name,
                errors=validation.errors,
            )
            return {"installed": False, "name": manifest.name, "validation": validation.to_json()}

        staging = self.plugins_dir / f".staging-{uuid.uuid4().hex[:8]}"
        backup: Path | None = None
        try:
            shutil.copytree(source_dir, staging)
            if target.exists():
                backup = self.plugins_dir / f".backup-{manifest.name}-{uuid.uuid4().hex[:6]}"
                target.rename(backup)
            staging.rename(target)
            if activate:
                loaded = self.activate(manifest.name)
                if not loaded.get("activated"):
                    raise RuntimeError(f"activation failed: {loaded.get('error')}")
            if backup is not None:
                shutil.rmtree(backup, ignore_errors=True)
        except Exception as exc:  # rollback: the previous state must be exactly restored
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            if backup is not None and backup.exists():
                backup.rename(target)
                self.activate(manifest.name)
            shutil.rmtree(staging, ignore_errors=True)
            self.bus.emit(PLUGIN_FAILED, source="plugin_host", severity="error", plugin=manifest.name, errors=[str(exc)])
            return {"installed": False, "name": manifest.name, "error": str(exc), "rolled_back": True}

        self.bus.emit(
            PLUGIN_INSTALLED,
            source="plugin_host",
            plugin=manifest.name,
            version=manifest.version,
            actions=manifest.actions,
            capabilities=manifest.capabilities,
        )
        if self.memory is not None:
            self.memory.remember(
                "artificer",
                f"installed plugin {manifest.name} v{manifest.version} providing {manifest.actions} "
                f"for task: {manifest.task or 'n/a'}",
                kind="plugin",
                tags=["plugin", "install"],
                importance=0.8,
            )
        return {"installed": True, "name": manifest.name, "actions": manifest.actions, "validation": validation.to_json()}

    def activate(self, name: str) -> dict[str, Any]:
        manifest_file = self.manifest_path(name)
        if not manifest_file.exists():
            return {"activated": False, "error": f"plugin {name} is not installed"}
        manifest = PluginManifest.from_json(json.loads(manifest_file.read_text(encoding="utf-8")))
        module_path = self.plugins_dir / name / manifest.entry
        try:
            module = _import_module(f"orbynth_plugin_{name}", module_path)
        except Exception as exc:  # noqa: BLE001
            return {"activated": False, "error": f"{type(exc).__name__}: {exc}"}
        found = _collect_actions(module)
        allowed = expand_capabilities(manifest.capabilities)
        for action_name, act in found.items():
            if act.capability not in allowed:
                return {"activated": False, "error": f"action {action_name} needs {act.capability}, not granted"}
            self.registry.register(act)
        self.loaded[name] = manifest
        self.modules[name] = module
        return {"activated": True, "name": name, "actions": sorted(found)}

    def uninstall(self, name: str, keep_backup: bool = True) -> dict[str, Any]:
        if name in self.modules:
            module = self.modules.pop(name)
            for action_name in _collect_actions(module):
                self.registry._actions.pop(action_name, None)  # noqa: SLF001 - registry owns its table
            self.loaded.pop(name, None)
        target = self.plugins_dir / name
        if not target.exists():
            return {"uninstalled": False, "name": name, "error": "not installed"}
        if keep_backup:
            graveyard = self.plugins_dir / ".removed"
            graveyard.mkdir(exist_ok=True)
            destination = graveyard / f"{name}-{int(time.time())}"
            target.rename(destination)
        else:
            shutil.rmtree(target, ignore_errors=True)
        self.bus.emit(PLUGIN_UNINSTALLED, source="plugin_host", plugin=name, kept_backup=keep_backup)
        return {"uninstalled": True, "name": name, "backup_kept": keep_backup}

    def load_all(self) -> dict[str, Any]:
        loaded: list[str] = []
        failed: list[dict[str, Any]] = []
        for directory in sorted(self.plugins_dir.iterdir() if self.plugins_dir.exists() else []):
            if not directory.is_dir() or directory.name.startswith("."):
                continue
            result = self.activate(directory.name)
            if result.get("activated"):
                loaded.append(directory.name)
            else:
                failed.append({"name": directory.name, "error": result.get("error")})
        return {"loaded": loaded, "failed": failed, "action_count": len(self.registry)}


def _import_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _collect_actions(module: Any) -> dict[str, Action]:
    found: dict[str, Action] = {}
    for value in vars(module).values():
        if isinstance(value, Action):
            found[value.name] = value
        elif isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, Action):
                    found[item.name] = item
    return found
