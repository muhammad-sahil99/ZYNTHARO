"""HUD topic coverage (contract section 6).

The interface is only truthful if every event the core can emit has somewhere to land. That
already failed once: the whole operational layer (supervisor, owner gate, verdicts, updates,
governor) shipped while the HUD ignored its topics, so the app looked calm while processes
were being killed and updates applied.

This tool is the single source of that check. It reads the topic constants from
``zyntharo/events.py``, adds any topic emitted as a literal string in the codebase, reads the
handler keys out of ``hud/app.js``, and writes ``hud/coverage.json``. ``tests/test_hud.py``
imports the same functions and asserts both that nothing is unhandled and that the committed
file matches, so the file and the check cannot drift apart.

    python3 tools/hud_coverage.py            # write hud/coverage.json
    python3 tools/hud_coverage.py --check     # fail (exit 1) if the file is out of date
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVENTS = ROOT / "zyntharo" / "events.py"
PACKAGE = ROOT / "zyntharo"
HUD_APP = ROOT / "hud" / "app.js"
COVERAGE = ROOT / "hud" / "coverage.json"

#: The map in hud/app.js keys handlers by topic: `    "agent.spawned": (e) => {...}`.
_KEY = re.compile(r'^\s{4}"([a-z][a-z0-9._]*)"\s*:', re.MULTILINE)
#: hud/app.js also handles whole families: `topic.startsWith("agent.")` in the frame router.
_PREFIX = re.compile(r'topic\.startsWith\("([a-z][a-z0-9._]*\.)"\)')
#: Topics the bridge delivers as their own frame type instead of an event frame, with the
#: frame type each becomes (see zyntharo/hud/bridge.py). Handled in the switch on message.type.
FRAME_ROUTED = {
    "hud.scene": "scene",
    "hud.notice": "notice",
    "voice.transcript": "transcript",
}


def declared_topics(events_path: Path = EVENTS) -> list[str]:
    """Every topic constant in zyntharo/events.py, in declaration order."""
    found: list[str] = []
    for line in events_path.read_text(encoding="utf-8").splitlines():
        match = re.match(r'^([A-Z][A-Z0-9_]*)\s*=\s*"([a-z][a-z0-9._]*)"\s*$', line)
        if match:
            found.append(match.group(2))
    return found


def literal_topics(package: Path = PACKAGE) -> list[str]:
    """Topics published with a bare string instead of a constant.

    Counted, not ignored: a literal topic is invisible to the constant list, and an invisible
    topic is how a subsystem ends up with no interface at all.
    """
    found: set[str] = set()
    for path in sorted(package.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", getattr(node.func, "id", ""))
            if name not in {"emit", "publish"} or not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str) and "." in first.value:
                found.add(first.value)
    return sorted(found)


def hud_topics(app_path: Path = HUD_APP) -> list[str]:
    """Topics with a handler in the HUD's TOPIC_EFFECTS map."""
    if not app_path.exists():
        return []
    return sorted(set(_KEY.findall(app_path.read_text(encoding="utf-8"))))


def hud_prefixes(app_path: Path = HUD_APP) -> list[str]:
    """Topic prefixes the HUD handles as a family (``agent.``)."""
    if not app_path.exists():
        return []
    return sorted(set(_PREFIX.findall(app_path.read_text(encoding="utf-8"))))


def covered(topic: str, handled: set[str], prefixes: list[str]) -> bool:
    if topic in handled or topic in FRAME_ROUTED:
        return True
    return any(topic.startswith(prefix) for prefix in prefixes)


def report(events_path: Path = EVENTS, package: Path = PACKAGE, app_path: Path = HUD_APP) -> dict:
    backend = declared_topics(events_path)
    literals = literal_topics(package)
    topics = sorted(set(backend) | set(literals))
    handled = set(hud_topics(app_path))
    prefixes = hud_prefixes(app_path)
    return {
        "generated_from": "zyntharo/events.py",
        "handler_file": "hud/app.js",
        "topics": topics,
        "handled": sorted(t for t in topics if covered(t, handled, prefixes)),
        "unhandled": sorted(t for t in topics if not covered(t, handled, prefixes)),
        "frame_routed": sorted(t for t in topics if t in FRAME_ROUTED),
        "extra_handlers": sorted(handled - set(topics)),
        "literal_topics": literals,
        "constant_count": len(backend),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="fail if the committed file is out of date")
    args = parser.parse_args(argv)

    data = report()
    if data["unhandled"]:
        print(f"[!!] {len(data['unhandled'])} topic(s) have no HUD handler:", file=sys.stderr)
        for topic in data["unhandled"]:
            print(f"       - {topic}", file=sys.stderr)

    if args.check:
        if not COVERAGE.exists():
            print("[!!] hud/coverage.json is missing; run tools/hud_coverage.py", file=sys.stderr)
            return 1
        current = json.loads(COVERAGE.read_text(encoding="utf-8"))
        if current != data:
            print("[!!] hud/coverage.json is out of date; re-run tools/hud_coverage.py", file=sys.stderr)
            return 1
    else:
        COVERAGE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        print(f"[ok] wrote {COVERAGE.relative_to(ROOT)}: {len(data['topics'])} topic(s), {len(data['unhandled'])} unhandled")

    return 1 if data["unhandled"] else 0


if __name__ == "__main__":
    raise SystemExit(main())