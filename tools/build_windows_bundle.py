#!/usr/bin/env python3
"""Build ZYNTHARO's Windows application payload for an installer.

This script is deliberately strict. It makes the payload that a signed WiX/Inno/MSIX installer
will install, but it does not claim a successful installer until native helpers, the WhatsApp
sidecar dependencies, and Playwright Chromium are all actually present. Running it on a non-
Windows host is allowed only with ``--check`` so CI can validate the manifest.
"""
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
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist" / "windows"
REQUIRED_PAYLOAD = (
    "ZYNTHARO.exe", "hud", "sidecars/whatsapp/sidecar.js", "sidecars/whatsapp/package.json",
    "node/node.exe", "ms-playwright",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def requirements() -> list[dict[str, str]]:
    """Return all non-negotiable payload ingredients with actionable remediation."""
    node_modules = ROOT / "sidecars" / "whatsapp" / "node_modules" / "whatsapp-web.js"
    node_runtime = ROOT / "vendor" / "node" / "node.exe"
    playwright_cache = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
    chromium_dir = Path(playwright_cache).expanduser() if playwright_cache else None
    return [
        {"name": "Windows host", "ok": str(platform.system() == "Windows"), "fix": "Build the release bundle on Windows 10 1809+ or Windows 11."},
        {"name": "PyInstaller", "ok": str(shutil.which("pyinstaller") is not None), "fix": "Install the packaging extra: pip install -e .[packaging]."},
        {"name": "HUD assets", "ok": str((ROOT / "hud" / "index.html").is_file()), "fix": "Restore the hud directory."},
        {"name": "WhatsApp sidecar", "ok": str((ROOT / "sidecars" / "whatsapp" / "sidecar.js").is_file()), "fix": "Restore sidecars/whatsapp."},
        {"name": "WhatsApp sidecar dependencies", "ok": str(node_modules.exists()), "fix": "Run npm ci in sidecars/whatsapp before the release build."},
        {"name": "Bundled Node runtime", "ok": str(node_runtime.is_file()), "fix": "Place the reviewed Node runtime at vendor/node/node.exe."},
        {
            "name": "Playwright Chromium payload",
            "ok": str(chromium_dir is not None and chromium_dir.is_dir() and any(chromium_dir.iterdir())),
            "fix": "Set PLAYWRIGHT_BROWSERS_PATH to a non-empty bundled Chromium directory after playwright install chromium.",
        },
    ]


def validate_payload(root: Path) -> list[str]:
    missing = [relative for relative in REQUIRED_PAYLOAD if not (root / relative).exists()]
    manifest = root / "bundle-manifest.json"
    if not manifest.is_file():
        missing.append("bundle-manifest.json")
    return missing


def write_manifest(payload: Path) -> Path:
    files = []
    for path in sorted(p for p in payload.rglob("*") if p.is_file()):
        files.append({"path": path.relative_to(payload).as_posix(), "bytes": path.stat().st_size, "sha256": _sha256(path)})
    manifest = {
        "product": "ZYNTHARO",
        "version": (ROOT / "zyntharo" / "__init__.py").read_text(encoding="utf-8").split('__version__ = "')[1].split('"')[0],
        "platform": "windows",
        "files": files,
        "installer_note": "This payload must be code-signed and installed by a signed Windows installer.",
    }
    target = payload / "bundle-manifest.json"
    target.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return target


def build(output: Path) -> int:
    # ``requirements`` has already established this directory as non-empty. Resolve it again
    # here because the actual PyInstaller command must copy the exact checked directory.
    chromium_dir = Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"]).expanduser()
    failures = [row for row in requirements() if row["ok"] != "True"]
    if failures:
        for row in failures:
            print(f"missing: {row['name']} — {row['fix']}", file=sys.stderr)
        return 2
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    command = [
        "pyinstaller", "--noconfirm", "--clean", "--onedir", "--windowed", "--name", "ZYNTHARO",
        "--add-data", f"{ROOT / 'hud'}{os.pathsep}hud",
        "--add-data", f"{ROOT / 'sidecars'}{os.pathsep}sidecars",
        "--add-binary", f"{ROOT / 'vendor' / 'node' / 'node.exe'}{os.pathsep}node/node.exe",
        "--add-data", f"{chromium_dir}{os.pathsep}ms-playwright",
        "--collect-submodules", "zyntharo",
        str(ROOT / "zyntharo" / "desktop.py"),
        "--distpath", str(output), "--workpath", str(ROOT / "build" / "pyinstaller"),
    ]
    result = subprocess.run(command, cwd=ROOT, check=False)
    if result.returncode:
        return result.returncode
    payload = output / "ZYNTHARO"
    manifest = write_manifest(payload)
    missing = validate_payload(payload)
    if missing:
        print("bundle validation failed: " + ", ".join(missing), file=sys.stderr)
        return 3
    print(f"bundle ready: {payload}")
    print(f"manifest: {manifest}")
    print("next: sign the payload and create a signed Windows installer; do not distribute this unsigned directory.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build ZYNTHARO's complete Windows application payload.")
    parser.add_argument("--output", type=Path, default=DIST)
    parser.add_argument("--check", action="store_true", help="validate bundle inputs without building")
    args = parser.parse_args(argv)
    if args.check:
        bad = [row for row in requirements() if row["ok"] != "True"]
        for row in requirements():
            print(f"[{'ok' if row['ok'] == 'True' else '--'}] {row['name']}")
        return 0 if not bad else 2
    return build(args.output)


if __name__ == "__main__":
    raise SystemExit(main())
