"""The Forge: ORBYNTH writing and installing new capability for itself.

The flow, end to end:

1. Something in the fleet notices a repeated pattern (the same action sequence three times,
   or a task the Artificer decides should be permanent).
2. The **Artificer** agent turns that into a plugin *specification*: name, description,
   capability list, and one or more actions described as recorded step sequences — with
   explicit code only where a step sequence cannot express it.
3. The Forge renders the bundled template into a real module, writes the manifest, and
   validates it: compile check, capability-ceiling check, action/capability consistency, and
   the module's own ``self_test`` against the live action registry.
4. On green, the plugin is installed transactionally and its actions register into the same
   registry as built-ins, so from that moment any agent can call them directly — no vision,
   no clicking, no typing.
5. The Artificer records what it built, its API and its published behaviour in its own
   memory, so the next plugin is written with that experience available.

Design note (deliberate): step-sequence composition is the *default* generation path, not raw
code emission, because the first artifact must be trustworthy before it is installed
unattended. Raw code is supported for actions that need it, and it is compile-checked and
self-tested like everything else. This also matches the requested progression: templates
first, memory-informed generation later.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from ..actions.registry import ActionRegistry
from ..events import PLUGIN_AUTHORED, EventBus
from ..llm.providers import LLMRequest, Message, ToolSpec
from ..llm.runtime import ModelRuntime, Requirements
from .loader import MANIFEST_NAME, PluginHost, PluginManifest

TEMPLATE_DIR = Path(__file__).parent / "templates"


@dataclass
class ActionSpec:
    """One action a plugin should provide."""

    name: str
    capability: str
    description: str
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})
    steps: list[dict[str, Any]] = field(default_factory=list)
    code: str = ""  # optional explicit body when steps cannot express it

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "capability": self.capability,
            "description": self.description,
            "parameters": self.parameters,
            "steps": self.steps,
            "code": self.code,
        }


@dataclass
class PluginSpec:
    name: str
    description: str
    task: str = ""
    capabilities: list[str] = field(default_factory=list)
    actions: list[ActionSpec] = field(default_factory=list)
    runtime: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "task": self.task,
            "capabilities": self.capabilities,
            "actions": [a.to_json() for a in self.actions],
            "runtime": self.runtime,
        }


class Forge:
    """Turns an expressed need into an installed, tested plugin."""

    def __init__(
        self,
        host: PluginHost,
        runtime: ModelRuntime,
        registry: ActionRegistry,
        bus: EventBus | None = None,
        memory: Any = None,
        artificer_model: str = "",
        workdir: str | os.PathLike[str] | None = None,
    ):
        self.host = host
        self.runtime = runtime
        self.registry = registry
        self.bus = bus or EventBus()
        self.memory = memory
        self.artificer_model = artificer_model
        self.workdir = Path(workdir) if workdir else host.plugins_dir.parent / "forge"
        self.workdir.mkdir(parents=True, exist_ok=True)

    # ----------------------------------------------------------------------------------
    # Spec generation
    # ----------------------------------------------------------------------------------
    def design(self, need: str, name: str = "", capabilities: Sequence[str] | None = None, context: str = "") -> PluginSpec:
        """Ask the Artificer's model to design the plugin; fall back to a template design."""
        brief = self._capability_brief()
        prompt = (
            "You are the Artificer in ORBYNTH. Design a plugin that makes the following work "
            "permanent and repeatable, so it never has to be done again with vision, mouse or "
            "keyboard.\n\n"
            f"NEED:\n{need}\n\n"
            f"AVAILABLE ACTIONS:\n{brief}\n\n"
            f"CONTEXT (memory of previous plugins and failures):\n{context[:2000]}\n\n"
            "Reply with ONLY a JSON object:\n"
            "{\n"
            '  "name": "kebab-case-name",\n'
            '  "description": "one line",\n'
            '  "capabilities": ["filesystem.read", "browser.click"],\n'
            '  "actions": [\n'
            '    {"name": "verb_noun", "capability": "filesystem.read", "description": "one line",\n'
            '     "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},\n'
            '     "steps": [{"action": "existing_action_name", "args": {"path": "{path}"}}]}\n'
            "  ]\n"
            "}\n"
            "Rules: every step's action must exist in AVAILABLE ACTIONS; every capability must be "
            "in the plugin's capabilities list; prefer step sequences over custom code; use "
            "{placeholder} in args for parameters. Only supply \"code\" (a Python function body "
            "using `ctx` and the parameters as locals) when no step sequence can do the job."
        )
        spec: PluginSpec | None = None
        try:
            result = self.runtime.run(
                LLMRequest(messages=[Message(role="user", content=prompt)], model=self.artificer_model, tools=[], temperature=0.1),
                agent_id="artificer",
                prefer=[self.artificer_model] if self.artificer_model else [],
                requirements=Requirements(tools=False),
            )
            spec = self._parse_spec(result.response.text, name, capabilities)
        except Exception as exc:  # noqa: BLE001 - a forge run must never crash the fleet
            self.bus.emit("plugin.failed", source="forge", severity="warning", stage="design", error=str(exc)[:300])
        if spec is None or not spec.actions:
            spec = self._fallback_spec(need, name, capabilities)
        self.bus.emit(PLUGIN_AUTHORED, source="artificer", name=spec.name, actions=[a.name for a in spec.actions], needs_review=False)
        return spec

    def _parse_spec(self, text: str, name: str, capabilities: Sequence[str] | None) -> PluginSpec | None:
        payload = _extract_json(text)
        if payload is None:
            return None
        actions: list[ActionSpec] = []
        for raw in payload.get("actions", []) or []:
            if not isinstance(raw, dict) or not raw.get("name"):
                continue
            actions.append(
                ActionSpec(
                    name=str(raw["name"]),
                    capability=str(raw.get("capability", "")),
                    description=str(raw.get("description", "")),
                    parameters=raw.get("parameters") or {"type": "object", "properties": {}},
                    steps=list(raw.get("steps") or []),
                    code=str(raw.get("code", "")),
                )
            )
        spec = PluginSpec(
            name=_slug(name or payload.get("name", "") or "forged-plugin"),
            description=str(payload.get("description", "forged by the Artificer")),
            capabilities=list(payload.get("capabilities") or capabilities or []),
            actions=actions,
            runtime=str(payload.get("runtime", "")),
        )
        if not spec.capabilities:
            spec.capabilities = sorted({a.capability for a in actions if a.capability})
        return spec

    def _fallback_spec(self, need: str, name: str, capabilities: Sequence[str] | None) -> PluginSpec:
        """Template design used when no model can be reached — still produces a real plugin.

        The Artificer records the need in memory so that a later run (with a working model)
        can refine the plugin without losing the original intent.
        """
        action_name = _slug(name or "run_workflow").replace("-", "_")
        granted = list(capabilities or []) or ["filesystem.read"]
        spec = PluginSpec(
            name=_slug(name or "forged-workflow"),
            description=f"Recorded workflow: {need[:120]}",
            task=need,
            capabilities=granted,
            actions=[
                ActionSpec(
                    name=action_name,
                    capability=granted[0],
                    description=f"Replay the recorded workflow for: {need[:100]}",
                    parameters={"type": "object", "properties": {"target": {"type": "string"}}},
                    steps=[],
                )
            ],
        )
        if self.memory is not None:
            self.memory.remember(
                "artificer",
                f"plugin request recorded without a model: {need[:400]} — spec {spec.name} needs real steps",
                kind="plugin",
                tags=["forge", "unfinished"],
                importance=0.6,
            )
        return spec

    def _capability_brief(self, limit: int = 90) -> str:
        rows = [f"- {a['name']} ({a['capability']}): {a['description'][:110]}" for a in self.registry.describe()]
        return "\n".join(rows[:limit])

    # ----------------------------------------------------------------------------------
    # Render, validate, install
    # ----------------------------------------------------------------------------------
    def render(self, spec: PluginSpec) -> Path:
        directory = self.workdir / spec.name
        if directory.exists():
            shutil.rmtree(directory)
        directory.mkdir(parents=True)

        steps = {action.name: action.steps for action in spec.actions if action.steps}
        custom = {action.name: action.code for action in spec.actions if action.code}
        functions = "\n\n".join(_render_action(action, spec.name) for action in spec.actions)

        module = (TEMPLATE_DIR / "actions.py").read_text(encoding="utf-8")
        module = (
            module.replace("__NAME__", spec.name)
            .replace("__DESCRIPTION__", spec.description.replace("\n", " "))
            .replace("__TASK__", (spec.task or "n/a").replace("\n", " ")[:400])
            .replace("__STEPS__", json.dumps(steps, indent=2))
            .replace("__CUSTOM_CODE__", json.dumps(custom, indent=2))
            .replace("__ACTION_NAMES__", json.dumps([a.name for a in spec.actions]))
            .replace("__ACTIONS__", functions)
        )
        (directory / "actions.py").write_text(module, encoding="utf-8")

        manifest = (TEMPLATE_DIR / "plugin.json").read_text(encoding="utf-8")
        manifest = (
            manifest.replace("__NAME__", spec.name)
            .replace("__DESCRIPTION__", spec.description.replace('"', "'")[:300])
            .replace("__CAPABILITIES__", json.dumps(sorted(set(spec.capabilities))))
            .replace("__ACTIONS__", json.dumps([a.name for a in spec.actions]))
            .replace("__CREATED_AT__", str(int(time.time())))
            .replace("__TASK__", (spec.task or "").replace('"', "'")[:300])
            .replace("__RUNTIME__", spec.runtime)
        )
        (directory / MANIFEST_NAME).write_text(manifest, encoding="utf-8")
        return directory

    def forge(self, need: str, name: str = "", capabilities: Sequence[str] | None = None, install: bool = True, context: str = "") -> dict[str, Any]:
        """Full pipeline: design → render → validate → install → register."""
        spec = self.design(need, name=name, capabilities=capabilities, context=context)
        spec.task = need
        directory = self.render(spec)
        validation = self.host.validate(directory)
        result: dict[str, Any] = {
            "name": spec.name,
            "directory": str(directory),
            "spec": spec.to_json(),
            "validation": validation.to_json(),
            "installed": False,
        }
        if not validation.ok:
            result["error"] = "validation failed; nothing was installed"
            return result
        if install:
            install_result = self.host.install(directory, activate=True, replace=True)
            result["installed"] = bool(install_result.get("installed"))
            result["install"] = install_result
        if self.memory is not None:
            self.memory.remember(
                "artificer",
                json.dumps(
                    {
                        "plugin": spec.name,
                        "actions": [a.name for a in spec.actions],
                        "capabilities": spec.capabilities,
                        "installed": result["installed"],
                        "task": need[:300],
                    }
                ),
                kind="plugin",
                tags=["forge", spec.name],
                importance=0.85,
            )
        return result

    def promote_workflow(self, workflow_name: str, steps: Sequence[dict[str, Any]], capabilities: Sequence[str], task: str = "") -> dict[str, Any]:
        """Turn an already-verified sequence (e.g. replayed from the audit log) into a plugin.

        This is the promotion path that removes vision/clicking from a repeated workflow: the
        sequence is known-good because it already ran, so the plugin body is exactly it.
        """
        parameters = _params_from_steps(steps)
        granted = list(capabilities or []) or ["filesystem.read"]
        spec = PluginSpec(
            name=_slug(workflow_name),
            description=f"Promoted workflow '{workflow_name}' ({len(steps)} steps)",
            task=task or workflow_name,
            capabilities=granted,
            actions=[
                ActionSpec(
                    name=f"run_{_slug(workflow_name).replace('-', '_')}",
                    capability=granted[0],
                    description=f"Run the {workflow_name} workflow directly, without vision or input simulation",
                    parameters=parameters,
                    steps=list(steps),
                )
            ],
        )
        directory = self.render(spec)
        validation = self.host.validate(directory)
        if not validation.ok:
            return {"installed": False, "name": spec.name, "validation": validation.to_json()}
        install_result = self.host.install(directory, activate=True, replace=True)
        return {"installed": bool(install_result.get("installed")), "name": spec.name, "install": install_result, "validation": validation.to_json()}

    def opportunities(self, min_repeats: int = 3, max_length: int = 5) -> list[dict[str, Any]]:
        """Repeated action sequences in the audit log — candidates for promotion to a plugin.

        Every contiguous run of two or more actions ending at each audit entry is counted, not
        just the trailing window, so a workflow that appears anywhere in real history is found.
        """
        if self.memory is None:
            return []
        rows = [row for row in reversed(self.memory.audit_log(limit=1200)) if row["outcome"] == "ok"]
        sequences: dict[tuple[str, ...], dict[str, Any]] = {}
        history: list[str] = []
        for row in rows:
            history.append(row["action"])
            for length in range(2, max_length + 1):
                if len(history) < length:
                    continue
                key = tuple(history[-length:])
                entry = sequences.setdefault(
                    key,
                    {"sequence": list(key), "count": 0, "first_seen": row["ts"], "last_seen": row["ts"], "capabilities": []},
                )
                entry["count"] += 1
                entry["last_seen"] = row["ts"]
        ranked = [entry for entry in sequences.values() if entry["count"] >= min_repeats]
        ranked.sort(key=lambda entry: (entry["count"], len(entry["sequence"])), reverse=True)
        return ranked[:20]


def _params_from_steps(steps: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Derive the action's parameters from the ``{placeholders}`` its steps actually use.

    A promoted workflow knows exactly which values must be supplied at call time, so the
    schema is read off the steps rather than guessed.
    """
    found: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, str):
            for match in re.finditer(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", value):
                name = match.group(1)
                if name not in found:
                    found.append(name)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    for step in steps:
        walk(step.get("args", {}))
    if not found:
        return {"type": "object", "properties": {}}
    return {
        "type": "object",
        "properties": {name: {"type": "string", "description": f"value for {{{name}}}"} for name in found},
        "required": found,
    }


def _render_action(action: ActionSpec, plugin_name: str) -> str:
    params = json.dumps(action.parameters or {"type": "object", "properties": {}}, indent=4).replace("\n", "\n    ")
    signature = _signature_from_schema(action.parameters)
    if action.code.strip():
        body = _indent(action.code.strip(), 1)
    else:
        names = list((action.parameters or {}).get("properties") or {})
        pairs = ", ".join(f'"{name}": {name}' for name in names)
        body = (
            "    if not STEPS.get(NAME):\n"
            '        raise ActionFailed(f"action {NAME} has no recorded steps yet")\n'
            f"    call_args = {{{pairs}}}\n"
            "    return run_steps(ctx, NAME, call_args)\n"
        )
    return (
        f"@action(\n"
        f'    name="{action.name}",\n'
        f'    description="{action.description.replace(chr(34), chr(39))[:400]}",\n'
        f'    capability="{action.capability}",\n'
        f'    group="plugin:{plugin_name}",\n'
        f'    parameters={params},\n'
        f")\n"
        f"def {action.name}(ctx: ActionContext{signature}) -> Any:\n"
        f"    NAME = {action.name!r}\n"
        f"{body}"
    )


def _signature_from_schema(schema: dict[str, Any]) -> str:
    properties = (schema or {}).get("properties") or {}
    required = set((schema or {}).get("required") or [])
    parts: list[str] = []
    for key, spec in properties.items():
        annotation = {
            "string": "str",
            "integer": "int",
            "number": "float",
            "boolean": "bool",
            "array": "list",
            "object": "dict",
        }.get(str(spec.get("type", "string")), "Any")
        if key in required:
            parts.append(f", {key}: {annotation}")
        else:
            default = _default_for(spec)
            parts.append(f", {key}: {annotation} = {default!r}")
    return "".join(parts)


def _default_for(spec: dict[str, Any]) -> Any:
    if "default" in spec:
        return spec["default"]
    return {
        "string": "",
        "integer": 0,
        "number": 0.0,
        "boolean": False,
        "array": [],
        "object": {},
    }.get(str(spec.get("type", "string")), None)


def _indent(text: str, levels: int) -> str:
    pad = "    " * levels
    return "\n".join(pad + line if line.strip() else line for line in text.splitlines()) + "\n"


def _extract_json(text: str) -> dict[str, Any] | None:
    import json as _json

    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidates = [fenced.group(1)] if fenced else []
    start = text.find("{")
    if start >= 0:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start : index + 1])
                    break
    for candidate in candidates:
        try:
            payload = _json.loads(candidate)
            if isinstance(payload, dict):
                return payload
        except _json.JSONDecodeError:
            continue
    return None


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9-_]+", "-", value.strip().lower())
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-")
    return cleaned or "forged-plugin"
