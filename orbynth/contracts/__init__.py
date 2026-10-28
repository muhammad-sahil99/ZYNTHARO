"""Contracts: the schemas every persisted record and cross-boundary message must satisfy.

``contracts/`` holds JSON Schema and the version registry; this package loads them, validates
real objects against them and answers questions about compatibility. Nothing here invents a
shape: the schemas describe what the writers already produce, and
``tests/test_contracts.py`` validates live objects from the running system, so a writer that
drifts away from its contract fails the suite instead of failing a customer's upgrade.

The version policy is in ``contracts/versions.json`` and enforced in
:mod:`orbynth.contracts.compat`; generated language bindings live in
``orbynth/contracts/generated.py``, ``contracts/generated/contracts.d.ts`` and
``contracts/generated/Contracts.cs`` and are produced by ``tools/contracts.py``.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from .validator import ValidationResult, validate as _validate

#: ``contracts/`` sits beside the package, not inside it: it is the published interface, and
#: the installer ships it next to the code so a plugin author can read the same file.
CONTRACTS_DIR = Path(__file__).resolve().parents[2] / "contracts"
VERSIONS_FILE = CONTRACTS_DIR / "versions.json"


class ContractError(RuntimeError):
    """A contract is missing, unreadable, or inconsistent with the registry."""


@lru_cache(maxsize=8)
def load_bundle(file_name: str) -> Mapping[str, Any]:
    path = CONTRACTS_DIR / file_name
    if not path.exists():
        raise ContractError(f"contract bundle is missing: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ContractError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or "$defs" not in data:
        raise ContractError(f"{path} is not a schema bundle with $defs")
    return data


@lru_cache(maxsize=1)
def registry() -> dict[str, dict[str, Any]]:
    """The version registry: contract name -> file, definition, version and status."""
    if not VERSIONS_FILE.exists():
        raise ContractError(f"contract version registry is missing: {VERSIONS_FILE}")
    data = json.loads(VERSIONS_FILE.read_text(encoding="utf-8"))
    contracts = data.get("contracts")
    if not isinstance(contracts, dict) or not contracts:
        raise ContractError("contracts/versions.json declares no contracts")
    return contracts


def names() -> list[str]:
    return sorted(registry())


def version(contract: str) -> str:
    try:
        return str(registry()[contract]["version"])
    except KeyError as exc:
        raise ContractError(f"no such contract: {contract}") from exc


def definition(contract: str) -> Mapping[str, Any]:
    entry = registry().get(contract)
    if entry is None:
        raise ContractError(f"no such contract: {contract}")
    bundle = load_bundle(str(entry["file"]))
    try:
        return bundle["$defs"][str(entry["def"])]
    except KeyError as exc:
        raise ContractError(
            f"contract {contract!r} points at {entry['file']}#/$defs/{entry['def']}, which does not exist"
        ) from exc


def validate(contract: str, instance: Any) -> ValidationResult:
    """Check one record against its contract, resolving refs within the same bundle."""
    entry = registry().get(contract)
    if entry is None:
        raise ContractError(f"no such contract: {contract}")
    bundle = load_bundle(str(entry["file"]))
    return _validate(instance, definition(contract), contract=f"{contract}@{version(contract)}", root=bundle)


def audit() -> dict[str, Any]:
    """Every contract, its version, and whether its own registry entry is consistent.

    Used by the release gate: a bundle that cannot be loaded, a definition name that does not
    exist, or a malformed version string is a broken interface, not a documentation issue.
    """
    from .compat import parse_version

    report: dict[str, Any] = {"contracts": [], "problems": []}
    for contract in names():
        entry = registry()[contract]
        row: dict[str, Any] = {
            "contract": contract,
            "version": str(entry.get("version", "")),
            "file": str(entry.get("file", "")),
            "status": str(entry.get("status", "")),
        }
        try:
            parsed = parse_version(row["version"])
            row["major"], row["minor"], row["patch"] = parsed
        except ValueError as exc:
            report["problems"].append(f"{contract}: {exc}")
            row["major"] = row["minor"] = row["patch"] = None
        if row["status"] not in ("frozen", "draft"):
            report["problems"].append(f"{contract}: unknown status {row['status']!r}")
        try:
            row["properties"] = len(definition(contract).get("properties", {}))
            row["required"] = len(definition(contract).get("required", []))
        except ContractError as exc:
            report["problems"].append(str(exc))
            row["properties"] = row["required"] = 0
        report["contracts"].append(row)
    return report


__all__ = [
    "CONTRACTS_DIR",
    "ContractError",
    "VERSIONS_FILE",
    "audit",
    "definition",
    "load_bundle",
    "names",
    "registry",
    "validate",
    "version",
]