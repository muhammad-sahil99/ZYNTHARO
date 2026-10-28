"""The Reality Gate: the static scanner that bans fake functionality (spec 4.1).

The failure mode this exists to prevent is not a crash — it is a *plausible* lie. A stubbed
driver that returns success, a canned result standing in for real data, a function whose body
is ``pass``, a ``TODO`` where the work should be, a mocked module imported into shipped code:
each one turns into a feature that appears to work and does not, which is the single worst
outcome for a product that claims to operate a computer.

The scanner is AST-based for Python (the shipped language here) and rule-driven. Two severities:

* ``blocking`` — the pattern is banned outright in shipped code. The gate fails.
* ``review`` — the pattern has legitimate uses and is therefore *listed* rather than banned:
  an interface method that raises ``NotImplementedError``, a swallowed error in a path that
  must not break execution, a poll that sleeps between checks of real state. These are shown
  in the report so they are visible decisions, not silent ones.

Anything under ``tests/`` is a harness, where doubles and fault injection are required, and is
counted separately. Exceptions to a ``blocking`` rule belong in ``tools/reality_gate_allow.json``
with a path, a rule and a reason, so an exception is a recorded decision rather than a rule
quietly weakened.

    python3 tools/reality_gate.py              # scan orbynth/ (the shipped package)
    python3 tools/reality_gate.py --all        # include tooling and tests (informational)
    python3 tools/reality_gate.py --json
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALLOWLIST = ROOT / "tools" / "reality_gate_allow.json"

#: Whole words in an identifier that mean "this is not real". Matched per word, so that a
#: genuine plural like ``placeholders`` (a string of SQL '?' marks) is not mistaken for one.
FAKE_NAME_FRAGMENTS = ("mock", "mocked", "fake", "faked", "stub", "stubbed", "dummy", "placeholder", "pretend")
#: Comment markers that mean "this work was never finished".
UNFINISHED_MARKERS = re.compile(r"\b(TODO|FIXME|XXX|HACK)\b")
#: Strings that advertise a mode that must never ship.
FAKE_MODE_STRINGS = ("demo mode", "sample data", "fake progress", "placeholder value", "lorem ipsum")
#: Functions whose only statement is a constant return are claiming to have done something.
ACTION_NAME_HINTS = ("apply", "install", "send", "write", "create", "delete", "execute", "run", "launch", "publish", "deploy", "sync")
#: Modules that only make sense in a test harness.
TEST_ONLY_MODULES = ("unittest.mock", "mock", "pytest_mock", "faker")

BLOCKING = "blocking"
REVIEW = "review"


@dataclass(slots=True)
class Finding:
    rule: str
    severity: str
    path: str
    line: int
    detail: str

    def to_json(self) -> dict:
        return {"rule": self.rule, "severity": self.severity, "path": self.path, "line": self.line, "detail": self.detail}


def load_allowlist(path: Path = ALLOWLIST) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    entries = data.get("allow", data if isinstance(data, list) else [])
    return [entry for entry in entries if isinstance(entry, dict)]


def python_files(root: Path, include_tests: bool = False) -> list[Path]:
    """Every shipped Python file under ``root``, with the harness separated out."""
    found: list[Path] = []
    for path in sorted(root.rglob("*.py")):
        parts = set(path.parts)
        if not include_tests and ("tests" in parts or path.name.startswith("test_")):
            continue
        if not include_tests and "tools" in parts:
            continue
        found.append(path)
    return found


def is_harness(path: Path) -> bool:
    return "tests" in path.parts or path.name.startswith("test_")


def _is_constant(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant)


def _name_parts(name: str) -> list[str]:
    """Split an identifier into words: ``MockWhatsApp`` -> ['mock','whats','app']
    and ``placeholders`` -> ['placeholders'] (so the plural is not a match)."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)
    return [part.lower() for part in re.split(r"[^A-Za-z0-9]+", spaced) if part]


def _looks_like_success(node: ast.AST) -> bool:
    """A literal `True` or a dict whose only value is a literal `True`."""
    if isinstance(node, ast.Constant) and node.value is True:
        return True
    if isinstance(node, ast.Dict):
        values = [v for v in node.values if v is not None]
        return bool(values) and all(isinstance(v, ast.Constant) and v.value is True for v in values)
    return False


class _Scanner(ast.NodeVisitor):
    """Walks one module and records what it finds. No rewriting, no guessing."""

    def __init__(self, path: Path, source: str) -> None:
        self.path = path
        self.source = source
        self.lines = source.splitlines()
        self.findings: list[Finding] = []
        self._class_depth = 0

    # -- helpers ---------------------------------------------------------------------
    def _add(self, rule: str, severity: str, node: ast.AST, detail: str) -> None:
        self.findings.append(
            Finding(rule=rule, severity=severity, path=str(self.path), line=int(getattr(node, "lineno", 0)), detail=detail)
        )

    def _fake_name(self, name: str) -> str:
        for part in _name_parts(name):
            if part in FAKE_NAME_FRAGMENTS:
                return part
        return ""

    # -- rules -----------------------------------------------------------------------
    def visit_Module(self, node: ast.Module) -> None:
        for number, line in enumerate(self.lines, start=1):
            comment = line.split("#", 1)[1] if "#" in line else ""
            if comment and UNFINISHED_MARKERS.search(comment):
                self.findings.append(
                    Finding("unfinished_work", BLOCKING, str(self.path), number, comment.strip()[:120])
                )
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name.split(".")[0] in TEST_ONLY_MODULES or alias.name in TEST_ONLY_MODULES:
                self._add("test_double_import", BLOCKING, node, f"imports {alias.name}, which is a test harness")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        names = [alias.name for alias in node.names]
        if module in TEST_ONLY_MODULES or module == "unittest.mock":
            self._add("test_double_import", BLOCKING, node, f"imports from {module}, which is a test harness")
        elif module == "unittest" and any(name in TEST_ONLY_MODULES for name in names):
            flagged = ", ".join(name for name in names if name in TEST_ONLY_MODULES)
            self._add("test_double_import", BLOCKING, node, f"imports {flagged} from unittest")
        for name in names:
            if self._fake_name(name):
                self._add("fake_identifier", BLOCKING, node, f"imports {name}")
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        fragment = self._fake_name(node.name)
        if fragment:
            self._add(
                "fake_identifier",
                BLOCKING,
                node,
                f"class {node.name} is named like a double ('{fragment}') outside the test harness",
            )
        self._class_depth += 1
        self.generic_visit(node)
        self._class_depth -= 1

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Store):
            fragment = self._fake_name(node.id)
            if fragment:
                self._add("fake_identifier", BLOCKING, node, f"assigns to {node.id} ('{fragment}')")
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            lowered = node.value.lower()
            for phrase in FAKE_MODE_STRINGS:
                if phrase in lowered:
                    self._add("fake_mode_string", BLOCKING, node, f"ships the phrase {phrase!r}")
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._check_function(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._check_function(node)
        self.generic_visit(node)

    def _check_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        decorators = {getattr(dec, "attr", getattr(dec, "id", "")) for dec in node.decorator_list}
        body = [stmt for stmt in node.body if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant))]
        fragment = self._fake_name(node.name)
        if fragment:
            self._add("fake_identifier", BLOCKING, node, f"function {node.name} is named like a double ('{fragment}')")
        if len(body) == 1 and isinstance(body[0], ast.Pass):
            if "abstractmethod" in decorators or node.name.startswith("__"):
                self._add("interface_body", REVIEW, node, f"{node.name} is an interface hook with an empty body")
            else:
                self._add("empty_body", BLOCKING, node, f"{node.name} has a `pass` body: it does nothing")
        if len(body) == 1 and isinstance(body[0], ast.Raise):
            raised = body[0].exc
            name = getattr(raised, "id", getattr(getattr(raised, "func", None), "id", ""))
            if name.endswith("NotImplementedError"):
                if self._class_depth and not decorators:
                    self._add("interface_declaration", REVIEW, node, f"{node.name} declares an interface by raising")
                else:
                    self._add("not_implemented", BLOCKING, node, f"{node.name} raises NotImplementedError outside an interface class")
        if len(body) == 1 and isinstance(body[0], ast.Return) and body[0].value is not None:
            if _looks_like_success(body[0].value) and any(hint in node.name.lower() for hint in ACTION_NAME_HINTS):
                self._add(
                    "hardcoded_success",
                    BLOCKING,
                    node,
                    f"{node.name} returns a literal success value and does nothing else",
                )

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        body = [stmt for stmt in node.body if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant))]
        if body and all(isinstance(stmt, (ast.Pass, ast.Continue)) for stmt in body):
            self._add(
                "swallowed_error",
                REVIEW,
                node,
                "an exception is swallowed with no log, no counter and no re-raise",
            )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute):
            base = getattr(func.value, "id", "")
            name = f"{base}.{func.attr}" if base else func.attr
        else:
            name = getattr(func, "id", "")
        if name in ("time.sleep", "sleep") and node.args and _is_constant(node.args[0]):
            self._add(
                "sleep_wait",
                REVIEW,
                node,
                f"sleeps {getattr(node.args[0], 'value', '?')}s; waiting on real state is preferred",
            )
        self.generic_visit(node)


def scan_file(path: Path) -> list[Finding]:
    """Every finding in one file. An unreadable or unparsable file is itself a finding."""
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [Finding("unreadable", BLOCKING, str(path), 0, str(exc)[:160])]
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return [Finding("unparsable", BLOCKING, str(path), int(exc.lineno or 0), str(exc)[:160])]
    scanner = _Scanner(path, source)
    scanner.visit(tree)
    return scanner.findings


def _is_allowed(finding: Finding, allowlist: list[dict]) -> bool:
    for entry in allowlist:
        if entry.get("rule") != finding.rule:
            continue
        if entry.get("path") and not str(finding.path).endswith(str(entry["path"])):
            continue
        if entry.get("line") and int(entry["line"]) != finding.line:
            continue
        finding.detail = f"{finding.detail} [allowed: {entry.get('reason', 'no reason recorded')}]"
        return True
    return False


def scan_repository(
    root: Path | str = ROOT,
    include_tests: bool = False,
    allowlist_path: Path = ALLOWLIST,
) -> dict:
    """Scan the shipped package. Returns blocking findings, review findings and the allowlist."""
    root = Path(root)
    allowlist = load_allowlist(allowlist_path)
    findings: list[Finding] = []
    allowed: list[dict] = []
    files = python_files(root, include_tests=include_tests)
    for path in files:
        for finding in scan_file(path):
            if finding.severity == BLOCKING and _is_allowed(finding, allowlist):
                allowed.append(finding.to_json())
                continue
            findings.append(finding)

    # The harness is counted, never reported as a failure: doubles belong there.
    harness_hits = 0
    harness_dir = root / "tests"
    if harness_dir.exists():
        for path in sorted(harness_dir.rglob("*.py")):
            harness_hits += len(scan_file(path))

    blocking = [finding for finding in findings if finding.severity == BLOCKING]
    review = [finding for finding in findings if finding.severity == REVIEW]
    by_rule: dict[str, int] = {}
    for finding in findings:
        by_rule[finding.rule] = by_rule.get(finding.rule, 0) + 1
    return {
        "root": str(root),
        "files_scanned": len(files),
        "findings": [finding.to_json() for finding in findings],
        "blocking": [finding.to_json() for finding in blocking],
        "review": [finding.to_json() for finding in review],
        "allowed": allowed,
        "allowed_hits": len(allowed),
        "harness_hits": harness_hits,
        "by_rule": by_rule,
        "clean": not blocking,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", default=str(ROOT), help="repository root to scan")
    parser.add_argument("--all", action="store_true", help="include tooling and test harness files")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = scan_repository(Path(args.root), include_tests=args.all)
    if args.json:
        print(json.dumps(report, indent=2))
        return 1 if report["blocking"] else 0

    print(f"[reality gate] {report['files_scanned']} shipped file(s) scanned")
    for finding in report["blocking"]:
        print(f"  [BLOCK] {finding['rule']:<20} {finding['path']}:{finding['line']} {finding['detail']}")
    for finding in report["review"]:
        print(f"  [review] {finding['rule']:<19} {finding['path']}:{finding['line']} {finding['detail']}")
    for entry in report["allowed"]:
        print(f"  [allow] {entry['rule']:<20} {entry['path']}:{entry['line']} {entry['detail']}")
    print(
        f"  {len(report['blocking'])} blocking, {len(report['review'])} to review, "
        f"{report['allowed_hits']} allowed by waiver, {report['harness_hits']} in the test harness"
    )
    return 1 if report["blocking"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
