"""Packaging regressions: a bundle must carry its resources or fail before distribution."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orbynth.resources import app_root, resource_path
from tools.build_windows_bundle import REQUIRED_PAYLOAD, validate_payload, write_manifest
from tools.build_windows_installer import validate_manifest


class ResourceResolutionTests(unittest.TestCase):
    def test_source_resources_resolve_from_the_repository_not_the_working_directory(self):
        self.assertTrue(resource_path("hud", "index.html").is_file())
        self.assertTrue(resource_path("sidecars", "whatsapp", "sidecar.js").is_file())
        self.assertTrue(app_root().is_dir())

    def test_packaged_resource_root_uses_pyinstaller_payload(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch("sys._MEIPASS", tmp, create=True):
            self.assertEqual(resource_path("hud"), Path(tmp) / "hud")


class WindowsBundleTests(unittest.TestCase):
    def test_incomplete_payload_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(validate_payload(Path(tmp)), [*REQUIRED_PAYLOAD, "bundle-manifest.json"])

    def test_complete_payload_gets_a_hashed_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ORBYNTH.exe").write_bytes(b"desktop executable")
            (root / "hud").mkdir()
            (root / "hud" / "index.html").write_text("hud", encoding="utf-8")
            sidecar = root / "sidecars" / "whatsapp"
            sidecar.mkdir(parents=True)
            (sidecar / "sidecar.js").write_text("sidecar", encoding="utf-8")
            (sidecar / "package.json").write_text("{}", encoding="utf-8")
            node = root / "node"
            node.mkdir()
            (node / "node.exe").write_bytes(b"node")
            chromium = root / "ms-playwright"
            chromium.mkdir()
            (chromium / "chromium.marker").write_text("managed browser", encoding="utf-8")
            manifest = write_manifest(root)
            self.assertTrue(manifest.is_file())
            self.assertEqual(validate_payload(root), [])
            self.assertIn('"sha256"', manifest.read_text(encoding="utf-8"))
            self.assertEqual(validate_manifest(root), [])

    def test_installer_refuses_a_tampered_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "file.txt").write_text("original", encoding="utf-8")
            write_manifest(root)
            (root / "file.txt").write_text("changed", encoding="utf-8")
            self.assertIn("hash mismatch file.txt", validate_manifest(root))


if __name__ == "__main__":
    unittest.main()
