#!/usr/bin/env python3
"""Build the Windows installer from a verified ORBYNTH payload on a Windows release runner."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PAYLOAD = ROOT / "dist" / "windows" / "ORBYNTH"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_manifest(payload: Path) -> list[str]:
    manifest_path = payload / "bundle-manifest.json"
    if not manifest_path.is_file():
        return ["missing bundle-manifest.json"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"unreadable manifest: {exc}"]
    issues: list[str] = []
    for row in manifest.get("files", []):
        path = payload / row.get("path", "")
        if not path.is_file():
            issues.append(f"missing {row.get('path', '?')}")
        elif sha256(path) != row.get("sha256"):
            issues.append(f"hash mismatch {row.get('path', '?')}")
    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the signed ORBYNTH Windows installer.")
    parser.add_argument("--payload", type=Path, default=DEFAULT_PAYLOAD)
    parser.add_argument("--check", action="store_true", help="validate the payload but do not invoke Inno Setup")
    args = parser.parse_args(argv)
    issues = validate_manifest(args.payload)
    if issues:
        print("installer refused: " + "; ".join(issues), file=sys.stderr)
        return 2
    if args.check:
        print(f"validated installer input: {args.payload}")
        return 0
    if platform.system() != "Windows":
        print("installer refused: build a Windows installer on Windows", file=sys.stderr)
        return 2
    iscc = shutil.which("ISCC.exe") or shutil.which("iscc")
    if not iscc:
        print("installer refused: Inno Setup 6 compiler (ISCC.exe) is not installed", file=sys.stderr)
        return 2
    manifest = json.loads((args.payload / "bundle-manifest.json").read_text(encoding="utf-8"))
    env = {**os.environ, "ORBYNTH_PAYLOAD_DIR": str(args.payload), "ORBYNTH_VERSION": str(manifest["version"])}
    result = subprocess.run([iscc, str(ROOT / "installer" / "ORBYNTH.iss")], cwd=ROOT, env=env, check=False)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
