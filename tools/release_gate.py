#!/usr/bin/env python3
"""Master release gate (contract 22.3).

Every earlier section has its own tests, and none of them combine. This script is the single
answer to "is this releasable?":

* it runs **every** acceptance check in ``acceptance.json`` on the current machine;
* each check is marked ``blocking`` or ``non-blocking``;
* a check whose prerequisites are not present here (a Windows desktop, an Office licence, a
  WhatsApp test number, a code-signing certificate) is reported as
  ``blocked_prerequisite`` -- never as a pass, and never skipped silently;
* a blocking check may only be waived with a recorded written sign-off in
  ``release_waivers.json`` naming who signed it off, when, and why;
* the gate is green only when nothing failed, nothing blocking is unwaived, and every
  milestone up to the requested one has its checks satisfied.

Usage::

    python3 tools/release_gate.py                     # report, exit 1 if not ready
    python3 tools/release_gate.py --milestone M2       # stop after M2's checks
    python3 tools/release_gate.py --list               # show the manifest
    python3 tools/release_gate.py --only plugin_lifecycle,tests_all
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "tools" / "acceptance.json"
WAIVERS = ROOT / "tools" / "release_waivers.json"
REPORT = ROOT / "tools" / "last_gate_report.json"

PASS = "pass"
FAIL = "fail"
BLOCKED = "blocked_prerequisite"
WAIVED = "waived"
SKIPPED = "skipped"


@dataclass
class CheckResult:
    id: str
    title: str
    milestone: str
    blocking: bool
    kind: str
    status: str
    detail: str = ""
    duration: float = 0.0
    missing: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "milestone": self.milestone,
            "blocking": self.blocking,
            "kind": self.kind,
            "status": self.status,
            "detail": self.detail[:2000],
            "duration": round(self.duration, 2),
            "missing": self.missing,
        }


def host_capabilities() -> dict[str, bool]:
    """What this machine can actually verify, detected rather than assumed."""
    caps = {
        "posix": os.name != "nt",
        "windows": os.name == "nt",
        "macos": sys.platform == "darwin",
        "linux": sys.platform.startswith("linux"),
        "interactive_desktop": bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY") or os.name == "nt"),
        "python": shutil.which("python3") is not None or shutil.which("python") is not None,
        "node": shutil.which("node") is not None,
        "network": _has_network(),
    }
    # Prerequisites a person must supply; declared by the operator, never guessed.
    for name in (
        "office_license",
        "whatsapp_account",
        "test_tenant",
        "google_test_account",
        "code_signing_cert",
        "legal_review",
        "audio_hardware",
        "physical_rig",
        "dedicated_machine",
        "second_machine",
    ):
        caps[name] = os.environ.get(f"ZYNTHARO_ACCEPT_{name.upper()}", "").strip() == "1"
    return caps


def _has_network() -> bool:
    try:  # pragma: no cover - network probe
        import socket

        socket.create_connection(("1.1.1.1", 53), timeout=2).close()
        return True
    except OSError:
        return False


def load_manifest(path: Path = MANIFEST) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_waivers(path: Path = WAIVERS) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, dict[str, Any]] = {}
    today = time.strftime("%Y-%m-%d")
    for entry in data.get("waivers", []):
        check_id = str(entry.get("check_id") or "")
        signed = str(entry.get("signed_off_by") or "").strip()
        date = str(entry.get("date") or "").strip()
        expires = str(entry.get("expires") or "").strip()
        if expires and expires < today:
            continue  # an expired waiver stops applying; it does not quietly persist
        if check_id and signed and date:
            out[check_id] = {**entry, "check_id": check_id, "signed_off_by": signed, "date": date}
    return out


def milestone_order(manifest: dict[str, Any]) -> list[str]:
    return [m["id"] for m in manifest.get("milestones", [])]


def run_command_check(check: dict[str, Any], root: Path = ROOT) -> tuple[str, str]:
    command = check.get("command")
    if not command:
        return SKIPPED, "no command declared"
    proc = subprocess.run(
        command if isinstance(command, list) else command,
        shell=isinstance(command, str),
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=float(check.get("timeout", 900)),
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    tail = "\n".join(output.strip().splitlines()[-25:])
    return (PASS if proc.returncode == 0 else FAIL), tail or f"exit {proc.returncode}"


def evaluate(check: dict[str, Any], caps: dict[str, bool], waivers: dict[str, dict[str, Any]], root: Path = ROOT) -> CheckResult:
    blocking = bool(check.get("blocking", True))
    result = CheckResult(
        id=str(check.get("id")),
        title=str(check.get("title", "")),
        milestone=str(check.get("milestone", "M0")),
        blocking=blocking,
        kind=str(check.get("kind", "command")),
        status=SKIPPED,
    )
    missing = [req for req in check.get("requires", []) if not caps.get(req, False)]
    if missing:
        result.missing = missing
        if blocking and check.get("id") in waivers:
            waiver = waivers[str(check["id"])]
            result.status = WAIVED
            result.detail = f"waived by {waiver['signed_off_by']} on {waiver['date']}: {waiver.get('reason', 'no reason recorded')}"
        else:
            result.status = BLOCKED
            detail = check.get("prerequisite_note") or f"this machine cannot verify this yet; supply: {', '.join(missing)}"
            result.detail = detail
        return result

    if result.kind in {"manual", "owner"}:
        if blocking and check.get("id") in waivers:
            waiver = waivers[str(check["id"])]
            result.status = WAIVED
            result.detail = f"waived by {waiver['signed_off_by']} on {waiver['date']}: {waiver.get('reason', 'no reason recorded')}"
        else:
            result.status = BLOCKED if blocking else SKIPPED
            result.detail = check.get("procedure") or "a human must perform this check and record the result"
        return result

    if result.kind == "ops":
        # A check performed by Python rather than a shell command (kept in-process so the
        # gate exercises the actual objects, not a script that reimplements them).
        from gate_ops import OPS  # local module next to this file

        fn = OPS.get(str(check.get("op")))
        if fn is None:
            result.status = FAIL
            result.detail = f"unknown op {check.get('op')!r}"
            return result
        started = time.time()
        try:
            ok, detail = fn(root)
        except Exception as exc:
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        result.duration = time.time() - started
        result.status = PASS if ok else FAIL
        result.detail = detail
        return result

    started = time.time()
    try:
        status, detail = run_command_check(check, root)
    except subprocess.TimeoutExpired:
        status, detail = FAIL, f"timed out after {check.get('timeout', 900)}s"
    except OSError as exc:
        status, detail = FAIL, f"could not run: {exc!r}"
    result.duration = time.time() - started
    result.status, result.detail = status, detail
    return result


def run_gate(
    milestone: str = "GA",
    only: Sequence[str] = (),
    manifest_path: Path = MANIFEST,
    waivers_path: Path = WAIVERS,
    root: Path = ROOT,
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    waivers = load_waivers(waivers_path)
    caps = host_capabilities()
    order = milestone_order(manifest)
    limit = order.index(milestone) + 1 if milestone in order else len(order)
    in_scope = set(order[:limit])

    results: list[CheckResult] = []
    for check in manifest.get("checks", []):
        if only and check.get("id") not in only:
            continue
        if check.get("milestone") not in in_scope:
            continue
        results.append(evaluate(check, caps, waivers, root))

    failures = [r for r in results if r.status == FAIL]
    blocked = [r for r in results if r.status == BLOCKED and r.blocking]
    unwaived_manual = [r for r in results if r.kind in {"manual", "owner"} and r.status == BLOCKED]
    ready = not failures and not blocked
    report = {
        "generated_at": time.time(),
        "generated_at_iso": time.strftime("%Y-%m-%d %H:%M:%S"),
        "machine": {"platform": platform.platform(), "python": platform.python_version()},
        "milestone": milestone,
        "capabilities": caps,
        "verdict": "READY" if ready else "NOT_READY",
        "counts": {
            PASS: sum(1 for r in results if r.status == PASS),
            FAIL: len(failures),
            BLOCKED: len(blocked),
            WAIVED: sum(1 for r in results if r.status == WAIVED),
            SKIPPED: sum(1 for r in results if r.status == SKIPPED),
        },
        "blocking_failures": [r.id for r in failures if r.blocking],
        "blocked_prerequisites": [{"id": r.id, "missing": r.missing} for r in blocked],
        "manual_outstanding": [r.id for r in unwaived_manual],
        "results": [r.to_json() for r in results],
    }
    REPORT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def format_report(report: dict[str, Any]) -> str:
    lines = [
        f"ZYNTHARO release gate - milestone {report['milestone']} - {report['verdict']}",
        f"  passed {report['counts'][PASS]} | failed {report['counts'][FAIL]} | "
        f"blocked {report['counts'][BLOCKED]} | waived {report['counts'][WAIVED]} | skipped {report['counts'][SKIPPED]}",
        "",
    ]
    for result in report["results"]:
        mark = {PASS: "PASS ", FAIL: "FAIL ", BLOCKED: "BLOCK", WAIVED: "WAIVE", SKIPPED: "skip "}[result["status"]]
        block = "blocking" if result["blocking"] else "non-blocking"
        lines.append(f"[{mark}] {result['id']:<26} {result['milestone']:<3} ({block}) {result['title']}")
        if result["status"] != PASS and result["detail"]:
            lines.append(f"         -> {result['detail'].splitlines()[0][:180]}")
    if report["blocked_prerequisites"]:
        lines.append("")
        lines.append("Prerequisites you must provide before this can be verified:")
        for entry in report["blocked_prerequisites"]:
            lines.append(f"  - {entry['id']}: needs {', '.join(entry['missing'])}")
    if report["counts"][BLOCKED]:
        lines.append("")
        lines.append(f"Waivers (recorded sign-off) live in {WAIVERS.relative_to(ROOT)}; a waiver needs who, when and why.")
    lines.append("")
    lines.append(f"Full report: {REPORT.relative_to(ROOT)}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ZYNTHARO master release gate")
    parser.add_argument("--milestone", default="GA", help="run checks up to this milestone (M0..M4, GA)")
    parser.add_argument("--only", default="", help="comma-separated check ids")
    parser.add_argument("--list", action="store_true", help="list the manifest and exit")
    parser.add_argument("--json", action="store_true", help="print the raw report")
    args = parser.parse_args(argv)

    if args.list:
        manifest = load_manifest()
        for m in manifest.get("milestones", []):
            print(f"{m['id']:<3} {m['name']}: {m['exit']}")
        print()
        for check in manifest.get("checks", []):
            flag = "blocking" if check.get("blocking", True) else "non-blocking"
            print(f"{check['id']:<26} {check['milestone']:<3} {flag:<12} {check['title']}")
        return 0

    only = tuple(x.strip() for x in args.only.split(",") if x.strip())
    report = run_gate(milestone=args.milestone, only=only)
    print(json.dumps(report, indent=2) if args.json else format_report(report))
    return 0 if report["verdict"] == "READY" else 1


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
