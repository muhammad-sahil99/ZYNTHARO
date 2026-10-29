"""Contract compatibility (spec 3.4): a version mismatch is a decision, not a crash.

The rule the whole project follows, in one place:

* **same major** — readable. A higher minor is new fields this build ignores, which is
  allowed and reported as a warning so the operator knows a newer producer wrote the record.
* **different major** — not readable. Refused with both versions named and the migration
  that would bridge them, because guessing at an old format is how data gets corrupted.
* **unparsable** — refused. A version nobody can parse is not a promise.

Plugins declare the contract versions they were authored against
(``plugin-manifest.contracts``); the loader asks this module before importing their code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

ACCEPT = "accept"
WARN = "warn"
REFUSE = "refuse"

_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.-]+)?$")


def parse_version(text: str) -> tuple[int, int, int]:
    match = _SEMVER.match(str(text or "").strip())
    if not match:
        raise ValueError(f"{text!r} is not a semantic version (expected MAJOR.MINOR.PATCH)")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


@dataclass(slots=True)
class Compatibility:
    """The verdict for one producer/reader pair, with a sentence a human can act on."""

    contract: str
    produced: str
    required: str
    decision: str
    reason: str

    @property
    def readable(self) -> bool:
        return self.decision != REFUSE

    def to_json(self) -> dict[str, Any]:
        return {
            "contract": self.contract,
            "produced": self.produced,
            "required": self.required,
            "decision": self.decision,
            "readable": self.readable,
            "reason": self.reason,
        }


def check(contract: str, produced: str, required: str) -> Compatibility:
    """May a reader at ``required`` consume a record produced at ``produced``?"""
    try:
        p_major, p_minor, _ = parse_version(produced)
    except ValueError as exc:
        return Compatibility(contract, str(produced), str(required), REFUSE, f"unreadable producer version: {exc}")
    try:
        r_major, r_minor, _ = parse_version(required)
    except ValueError as exc:
        return Compatibility(contract, str(produced), str(required), REFUSE, f"unreadable required version: {exc}")

    if p_major == r_major:
        if p_minor > r_minor:
            return Compatibility(
                contract,
                str(produced),
                str(required),
                WARN,
                f"produced by a newer minor ({produced} > {required}); unknown fields are ignored, not dropped",
            )
        if p_minor < r_minor:
            return Compatibility(
                contract,
                str(produced),
                str(required),
                WARN,
                f"produced by an older minor ({produced} < {required}); new fields will be absent",
            )
        return Compatibility(contract, str(produced), str(required), ACCEPT, "same version")
    return Compatibility(
        contract,
        str(produced),
        str(required),
        REFUSE,
        f"major version {p_major} cannot be read by a version-{r_major} reader; a migration is required",
    )


def check_manifest(declared: Mapping[str, Any] | None) -> list[Compatibility]:
    """Check every contract a plugin manifest declares against what this build implements.

    Unknown contract names are refused rather than ignored: a plugin that targets an interface
    this build has never heard of is not compatible, whatever its version string says.
    """
    from . import ContractError, version as deployed_version

    results: list[Compatibility] = []
    for name, declared_version in (declared or {}).items():
        try:
            required = deployed_version(str(name))
        except ContractError:
            results.append(
                Compatibility(
                    str(name),
                    str(declared_version),
                    "",
                    REFUSE,
                    f"this build has no contract called {name!r}",
                )
            )
            continue
        results.append(check(str(name), str(declared_version), required))
    return results