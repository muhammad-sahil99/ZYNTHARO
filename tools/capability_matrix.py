"""Generate and check CAPABILITY_MATRIX.md (spec 4.7).

The document is derived, never written by hand: it is built from the live action registry,
the driver probe on this machine, the action journal and the recording ledger. ``--check``
is what the release gate runs, and it fails on the two ways a capability document lies:

* the committed file no longer matches reality (a tool was added, removed or renamed), or
* evidence exists that cannot be counted (an incomplete ledger entry, or a recording for a
  tool that does not exist).

    python3 tools/capability_matrix.py            # write CAPABILITY_MATRIX.md + artifacts/capabilities.json
    python3 tools/capability_matrix.py --check    # fail if the committed files are stale
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MATRIX_DOC = ROOT / "CAPABILITY_MATRIX.md"
MATRIX_JSON = ROOT / "artifacts" / "capabilities.json"


def build(root: Path = ROOT, platform: str | None = None, stats: object | None = None):
    """The matrix for this repository, wired to the real stores when they exist."""
    from orbynth.actions import build_registry
    from orbynth.ops.capability import (
        DECLARED_ONLY,
        CapabilityMatrix,
        ChainedRunStats,
        JournalRunStats,
        LEDGER_PATH,
        current_platform,
    )

    target = platform if platform is not None else DECLARED_ONLY
    if target == "machine":
        target = current_platform()

    registry = build_registry()
    sources: list[object] = []
    journal_path = root / "artifacts" / "capability_journal.db"
    if journal_path.exists():
        from orbynth.ops.journal import Journal

        try:
            sources.append(JournalRunStats(Journal(journal_path)))
        except Exception:
            pass
    if stats is not None:
        sources.append(stats)  # type: ignore[arg-type]
    return CapabilityMatrix(
        registry,
        ledger_path=root / LEDGER_PATH,
        stats=ChainedRunStats(tuple(sources)),
        platform=target,
    )


def render(matrix) -> dict:
    return {"markdown": matrix.markdown(), "json": matrix.to_json()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="fail if the committed files are stale")
    parser.add_argument("--json", action="store_true", help="print the raw matrix")
    parser.add_argument(
        "--machine",
        action="store_true",
        help="report this machine instead of the declared all-platform surface",
    )
    args = parser.parse_args(argv)

    from orbynth.ops.capability import current_platform

    matrix = build(platform=current_platform() if args.machine else None)
    doc = matrix.markdown()
    data = matrix.to_json()
    summary = data["summary"]

    if args.machine and not args.check:
        # A machine view is never written over the committed declared surface: it belongs to
        # this host, and committing it would make the repository claim a platform it is not.
        if args.json:
            print(json.dumps(data, indent=2))
            return 0
        print(
            f"[machine] {summary['platform']}: {summary['ready_here']} of {summary['tools']} "
            f"tool(s) offerable here, {summary['not_here']} not available "
            f"({summary['green']} green overall, {summary['provisional']} provisional)"
        )
        for hidden in matrix.hidden():
            print(f"  [not here] {hidden.tool}: {hidden.reason}")
        return 1 if summary["invalid_evidence"] or summary["phantom_evidence"] else 0

    if args.json:
        print(json.dumps(data, indent=2))
        return 0
    if args.check:
        problems: list[str] = []
        if not MATRIX_DOC.exists():
            problems.append("CAPABILITY_MATRIX.md is missing; run tools/capability_matrix.py")
        elif MATRIX_DOC.read_text(encoding="utf-8") != doc:
            problems.append("CAPABILITY_MATRIX.md is stale; run tools/capability_matrix.py")
        if not MATRIX_JSON.exists():
            problems.append("artifacts/capabilities.json is missing; run tools/capability_matrix.py")
        elif json.loads(MATRIX_JSON.read_text(encoding="utf-8")) != data:
            problems.append("artifacts/capabilities.json is stale; run tools/capability_matrix.py")
        if summary["invalid_evidence"]:
            problems.append(
                "unusable evidence in the ledger: "
                + "; ".join(f"{e['tool']}: {e['problem']}" for e in summary["invalid_evidence"])
            )
        if summary["phantom_evidence"]:
            problems.append(
                "recordings for tools that do not exist: " + ", ".join(summary["phantom_evidence"])
            )
        if problems:
            for problem in problems:
                print(f"[!!] {problem}", file=sys.stderr)
            return 1
        print(
            f"[ok] capability matrix current: {summary['tools']} declared tool(s), "
            f"{summary['green']} green, {summary['provisional']} provisional, "
            f"{summary['unavailable']} without a driver profile "
            f"({summary['platform']} = declared cross-platform surface)"
        )
        return 0

    MATRIX_DOC.write_text(doc, encoding="utf-8")
    MATRIX_JSON.parent.mkdir(parents=True, exist_ok=True)
    MATRIX_JSON.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(
        f"[ok] wrote CAPABILITY_MATRIX.md and artifacts/capabilities.json: "
        f"{summary['tools']} tool(s), {summary['green']} green, "
        f"{summary['provisional']} provisional, {summary['unavailable']} unavailable"
    )
    return 1 if summary["invalid_evidence"] or summary["phantom_evidence"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
