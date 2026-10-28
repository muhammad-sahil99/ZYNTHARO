"""Generate language bindings from contracts/ and check they are current (spec 3.4).

One schema, three languages: the Python records the runtime uses, the TypeScript types the
interface uses, and the C# records the native helper uses. Nothing is hand-written twice, so
the three cannot drift.

    python3 tools/contracts.py            # regenerate bindings
    python3 tools/contracts.py --check    # fail if anything is stale or inconsistent
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PY_TARGET = ROOT / "orbynth" / "contracts" / "generated.py"
TS_TARGET = ROOT / "contracts" / "generated" / "contracts.d.ts"
CS_TARGET = ROOT / "contracts" / "generated" / "Contracts.cs"

_PY_TYPES = {
    "string": "str",
    "integer": "int",
    "number": "float",
    "boolean": "bool",
    "object": "dict[str, Any]",
    "array": "list[Any]",
    "null": "None",
}
_TS_TYPES = {
    "string": "string",
    "integer": "number",
    "number": "number",
    "boolean": "boolean",
    "object": "Record<string, unknown>",
    "array": "unknown[]",
    "null": "null",
}
_CS_TYPES = {
    "string": "string",
    "integer": "long",
    "number": "double",
    "boolean": "bool",
    "object": "Dictionary<string, object?>",
    "array": "List<object?>",
    "null": "object?",
}


def _type_names(schema: dict) -> list[str]:
    declared = schema.get("type", "string")
    return list(declared) if isinstance(declared, list) else [declared]


def _field_name(name: str) -> str:
    return name.replace("-", "_")


def _pascal(name: str) -> str:
    return "".join(part.capitalize() for part in name.replace("-", "_").split("_"))


def _py_type(schema: dict) -> str:
    if "$ref" in schema:
        return _pascal(str(schema["$ref"]).rsplit("/", 1)[-1])
    if "enum" in schema and all(isinstance(item, str) for item in schema["enum"]):
        return "str"
    types = [_PY_TYPES.get(name, "Any") for name in _type_names(schema)]
    unique = list(dict.fromkeys(types))
    return " | ".join(unique) if len(unique) > 1 else unique[0]


def _ts_type(schema: dict) -> str:
    if "$ref" in schema:
        return _pascal(str(schema["$ref"]).rsplit("/", 1)[-1])
    if "enum" in schema:
        return " | ".join(json.dumps(item) for item in schema["enum"])
    if _type_names(schema) == ["array"]:
        items = schema.get("items", {})
        return f"Array<{_ts_type(items) if isinstance(items, dict) else 'unknown'}>"
    types = [_TS_TYPES.get(name, "unknown") for name in _type_names(schema)]
    unique = list(dict.fromkeys(types))
    return " | ".join(unique) if len(unique) > 1 else unique[0]


def _cs_type(schema: dict) -> str:
    if "$ref" in schema:
        return _pascal(str(schema["$ref"]).rsplit("/", 1)[-1]) + "?"
    if _type_names(schema) == ["array"]:
        items = schema.get("items", {})
        return f"List<{_cs_type(items) if isinstance(items, dict) else 'object?'}>"
    types = [_CS_TYPES.get(name, "object?") for name in _type_names(schema)]
    unique = list(dict.fromkeys(types))
    return unique[0]


def _render_python(bundles: dict[str, dict]) -> str:
    lines = [
        '"""Generated from contracts/*.schema.json by tools/contracts.py. Do not edit.',
        "",
        "Every contract is a dataclass plus the constants that name it. The runtime imports",
        "these to build records whose field names cannot drift from the schema.",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "from dataclasses import dataclass, field",
        "from typing import Any",
        "",
    ]
    for file_name, bundle in bundles.items():
        for name, definition in bundle["$defs"].items():
            cls = _pascal(name)
            required = set(definition.get("required", []))
            lines.append("")
            lines.append("")
            lines.append(f"@dataclass")
            lines.append(f"class {cls}:")
            lines.append(f'    """{definition.get("title", name)} (contracts/{file_name}#/$defs/{name})."""')
            lines.append("")
            properties = definition.get("properties", {})
            if not properties:
                lines.append("    pass")
                continue
            for prop, schema in properties.items():
                py = _py_type(schema)
                if prop not in required:
                    py = f"{py} | None"
                default = "None" if prop not in required else _py_default(schema)
                lines.append(f"    {_field_name(prop)}: {py} = {default}")
            lines.append("")
            lines.append(f"    CONTRACT = {name!r}")
            lines.append("")
            lines.append("    @classmethod")
            lines.append(f"    def from_dict(cls, data: dict[str, Any]) -> \"{cls}\":")
            lines.append("        \"\"\"Unknown keys are dropped on purpose: a newer minor may add fields,")
            lines.append("        and a reader that cannot understand them must not smuggle them onward.\"\"\"")
            lines.append("        known = {f for f in cls.__dataclass_fields__}")
            lines.append("        return cls(**{k: v for k, v in data.items() if k in known})")
            lines.append("")
            lines.append("    def to_dict(self) -> dict[str, Any]:")
            lines.append("        return {k: v for k, v in self.__dict__.items() if v is not None}")
    lines.append("")
    return "\n".join(lines) + "\n"


def _py_default(schema: dict) -> str:
    types = _type_names(schema)
    if "string" in types:
        return '""'
    if "integer" in types:
        return "0"
    if "number" in types:
        return "0.0"
    if "boolean" in types:
        return "False"
    if "object" in types:
        return "field(default_factory=dict)"
    if "array" in types:
        return "field(default_factory=list)"
    return "None"


def _render_typescript(bundles: dict[str, dict]) -> str:
    lines = [
        "// Generated from contracts/*.schema.json by tools/contracts.py. Do not edit.",
        "// One schema, three languages: this file is what the interface may rely on.",
        "",
    ]
    for file_name, bundle in bundles.items():
        lines.append(f"// contracts/{file_name}")
        for name, definition in bundle["$defs"].items():
            lines.append("")
            lines.append(f"/** {definition.get('title', name)} */")
            lines.append(f"export interface {_pascal(name)} {{")
            required = set(definition.get("required", []))
            for prop, schema in definition.get("properties", {}).items():
                optional = "" if prop in required else "?"
                lines.append(f"  {json.dumps(prop)}{optional}: {_ts_type(schema)};")
            lines.append("}")
    lines.append("")
    return "\n".join(lines)


def _render_csharp(bundles: dict[str, dict]) -> str:
    lines = [
        "// Generated from contracts/*.schema.json by tools/contracts.py. Do not edit.",
        "// The native helper reads and writes exactly these shapes.",
        "",
        "using System.Collections.Generic;",
        "",
        "namespace Orbynth.Contracts",
        "{",
    ]
    for file_name, bundle in bundles.items():
        lines.append(f"    // contracts/{file_name}")
        for name, definition in bundle["$defs"].items():
            lines.append(f"    /// <summary>{definition.get('title', name)}</summary>")
            lines.append(f"    public sealed record {_pascal(name)}")
            lines.append("    {")
            required = set(definition.get("required", []))
            for prop, schema in definition.get("properties", {}).items():
                cs_type = _cs_type(schema)
                optional = "" if prop in required else "?"
                lines.append(
                    f"        public {cs_type}{optional} {_pascal(prop)} {{ get; init; }}"
                )
            lines.append("    }")
            lines.append("")
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def build() -> dict[str, object]:
    """Render all three bindings from the committed schemas."""
    from orbynth.contracts import audit, load_bundle, registry

    entries = registry()
    files = sorted({str(entry["file"]) for entry in entries.values()})
    bundles = {file_name: dict(load_bundle(file_name)) for file_name in files}
    for file_name, bundle in bundles.items():
        declared = set(bundle["$defs"])
        wanted = {str(entry["def"]) for entry in entries.values() if entry["file"] == file_name}
        missing = sorted(wanted - declared)
        if missing:
            raise SystemExit(f"[!!] contracts/{file_name} is missing definition(s): {missing}")
    return {
        "python": _render_python(bundles),
        "typescript": _render_typescript(bundles),
        "csharp": _render_csharp(bundles),
        "audit": audit(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="fail if the generated bindings are stale")
    args = parser.parse_args(argv)

    rendered = build()
    audit = rendered["audit"]
    assert isinstance(audit, dict)
    targets = {"python": PY_TARGET, "typescript": TS_TARGET, "csharp": CS_TARGET}

    problems: list[str] = list(audit.get("problems", []))
    for key, path in targets.items():
        text = str(rendered[key])
        if args.check:
            if not path.exists():
                problems.append(f"{path.relative_to(ROOT)} is missing; run tools/contracts.py")
            elif path.read_text(encoding="utf-8") != text:
                problems.append(f"{path.relative_to(ROOT)} is stale; run tools/contracts.py")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    rows = audit.get("contracts", [])
    if args.check:
        if problems:
            for problem in problems:
                print(f"[!!] {problem}", file=sys.stderr)
            return 1
        print(
            f"[ok] {len(rows)} contract(s) consistent; generated Python, TypeScript and C# "
            "bindings are current"
        )
        return 0

    print(f"[ok] generated bindings for {len(rows)} contract(s): {', '.join(str(p.relative_to(ROOT)) for p in targets.values())}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())