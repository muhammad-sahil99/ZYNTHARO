"""A deliberately small JSON Schema validator.

The project has no runtime dependencies, so it cannot import ``jsonschema`` to check its own
records. This module implements the subset the contracts actually use — types, required
properties, additionalProperties, enums, array items and bounds, string patterns and lengths,
numeric bounds, and local ``$ref`` into the same document — and refuses anything outside that
subset loudly rather than silently passing it.

Refusing loudly is the point. A validator that quietly ignores a keyword it does not
understand turns a contract into a comment, and every record after that looks valid.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

SUPPORTED = frozenset(
    {
        "$schema",
        "$id",
        "$defs",
        "$ref",
        "title",
        "description",
        "type",
        "required",
        "properties",
        "additionalProperties",
        "enum",
        "items",
        "minItems",
        "maxItems",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "pattern",
    }
)

_TYPES: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list, tuple),
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "null": (type(None),),
}


@dataclass(slots=True)
class ValidationResult:
    """The verdict, with every error rather than only the first."""

    contract: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def raise_for_errors(self) -> None:
        if self.errors:
            raise ValueError(f"{self.contract}: " + "; ".join(self.errors))

    def to_json(self) -> dict[str, Any]:
        return {"contract": self.contract, "ok": self.ok, "errors": self.errors}


def _resolve(schema: Mapping[str, Any], root: Mapping[str, Any]) -> Mapping[str, Any]:
    ref = schema.get("$ref")
    if not ref:
        return schema
    if not isinstance(ref, str) or not ref.startswith("#/"):
        raise ValueError(f"only local refs are supported, got {ref!r}")
    node: Any = root
    for part in ref[2:].split("/"):
        if not isinstance(node, Mapping) or part not in node:
            raise ValueError(f"unresolvable ref {ref!r}")
        node = node[part]
    if not isinstance(node, Mapping):
        raise ValueError(f"ref {ref!r} does not point at a schema")
    return node


def _type_ok(value: Any, expected: str) -> bool:
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    types = _TYPES.get(expected)
    if types is None:
        raise ValueError(f"unsupported type keyword {expected!r}")
    return isinstance(value, types) and not (expected != "boolean" and isinstance(value, bool))


def _check(value: Any, schema: Mapping[str, Any], root: Mapping[str, Any], path: str, errors: list[str]) -> None:
    unknown = set(schema) - SUPPORTED
    if unknown:
        raise ValueError(f"{path}: unsupported schema keyword(s) {sorted(unknown)}")

    schema = _resolve(schema, root)

    if "type" in schema:
        expected = schema["type"]
        options = expected if isinstance(expected, list) else [expected]
        if not any(_type_ok(value, option) for option in options):
            errors.append(f"{path}: expected {'/'.join(options)}, got {type(value).__name__}")
            return

    if "enum" in schema and value not in schema["enum"]:
        allowed = ", ".join(repr(item) for item in schema["enum"][:8])
        errors.append(f"{path}: {value!r} is not one of {allowed}")

    if isinstance(value, str):
        if "minLength" in schema and len(value) < int(schema["minLength"]):
            errors.append(f"{path}: shorter than {schema['minLength']} character(s)")
        if "maxLength" in schema and len(value) > int(schema["maxLength"]):
            errors.append(f"{path}: longer than {schema['maxLength']} character(s)")
        if "pattern" in schema and not re.search(str(schema["pattern"]), value):
            errors.append(f"{path}: does not match {schema['pattern']!r}")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: below the minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: above the maximum {schema['maximum']}")

    if isinstance(value, Mapping):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: missing required property {key!r}")
        properties = schema.get("properties", {})
        additional = schema.get("additionalProperties", True)
        for key, item in value.items():
            child = f"{path}.{key}"
            if key in properties:
                _check(item, properties[key], root, child, errors)
            elif additional is False:
                errors.append(f"{path}: unexpected property {key!r}")
            elif isinstance(additional, Mapping):
                _check(item, additional, root, child, errors)

    if isinstance(value, (list, tuple)):
        if "minItems" in schema and len(value) < int(schema["minItems"]):
            errors.append(f"{path}: fewer than {schema['minItems']} item(s)")
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            errors.append(f"{path}: more than {schema['maxItems']} item(s)")
        items = schema.get("items")
        if isinstance(items, Mapping):
            for index, item in enumerate(value):
                _check(item, items, root, f"{path}[{index}]", errors)


def validate(
    instance: Any,
    schema: Mapping[str, Any],
    contract: str = "",
    root: Mapping[str, Any] | None = None,
) -> ValidationResult:
    """Validate one record against one definition, resolving local refs against the bundle."""
    if "$defs" in schema and "properties" not in schema and "type" not in schema:
        raise ValueError("pass one definition ($defs entry) plus the bundle as root, not the bundle alone")
    result = ValidationResult(contract=contract)
    _check(instance, schema, root if root is not None else schema, contract or "$", result.errors)
    return result