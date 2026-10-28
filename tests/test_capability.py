"""Capability Matrix tests (spec 4.7).

The matrix decides what a model is allowed to believe exists, so the tests are about the
ways that decision can lie: a tool promoted without a recording, a recording counted without
a machine or a date, a tool offered on a machine whose driver is absent, and evidence left
behind for a tool that no longer exists.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

from orbynth.actions.registry import ActionContext, ActionRegistry, action
from orbynth.ops.capability import (
    GREEN,
    LOCAL_DRIVER_MISSING,
    LOCAL_WRONG_PLATFORM,
    PROVISIONAL,
    UNAVAILABLE,
    CapabilityMatrix,
    load_ledger,
)


@action("probe_click", "Click something on the desktop.", "input.click", {}, group="input")
def probe_click(ctx: ActionContext) -> str:  # pragma: no cover - never executed
    return "clicked"


@action("probe_write", "Write a file.", "filesystem.write", {}, group="filesystem")
def probe_write(ctx: ActionContext) -> str:  # pragma: no cover - never executed
    return "written"


@action("probe_word", "Edit a Word document.", "office.word", {}, group="office")
def probe_word(ctx: ActionContext) -> str:  # pragma: no cover - never executed
    return "typed"


@action("probe_mystery", "Something with no declared driver profile.", "mystery.do", {}, group="mystery")
def probe_mystery(ctx: ActionContext) -> str:  # pragma: no cover - never executed
    return "?"


class FakeRuns:
    """Run history from a dictionary: what a real store would answer."""

    def __init__(self, rows: dict[str, dict]) -> None:
        self.rows = rows

    def for_action(self, name: str) -> dict:
        row = self.rows.get(name)
        if not row:
            return {"runs": 0, "ok": 0, "last": None, "source": "fake"}
        return {"runs": row["runs"], "ok": row["ok"], "last": row.get("last"), "source": "fake"}


def registry_with(*, capabilities: bool = True) -> ActionRegistry:
    registry = ActionRegistry()
    registry.register_all([probe_click, probe_write, probe_word, probe_mystery])
    return registry


class LedgerTests(unittest.TestCase):
    def test_incomplete_entries_are_reported_not_silently_trusted(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / "ledger.json"
            path.write_text(
                json.dumps(
                    {
                        "tools": {
                            "probe_write": {
                                "machine": "desk",
                                "os": "Windows 11",
                                "date": "2026-10-01",
                                "recording": "artifacts/recordings/fs_write.mp4",
                            },
                            "probe_click": {
                                "machine": "desk",
                                "os": "Windows 11",
                                "recording": "artifacts/recordings/click.mp4",
                            },
                            "probe_word": {
                                "machine": "",
                                "os": "Windows 11",
                                "date": "not-a-date",
                                "recording": "",
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            ledger = load_ledger(path)
        by_tool = {entry.tool: entry for entry in ledger}
        self.assertTrue(by_tool["probe_write"].valid)
        self.assertFalse(by_tool["probe_click"].valid)
        self.assertIn("date", by_tool["probe_click"].problem)
        self.assertFalse(by_tool["probe_word"].valid)
        self.assertIn("machine", by_tool["probe_word"].problem)
        self.assertIn("recording", by_tool["probe_word"].problem)

    def test_a_broken_ledger_is_an_error_not_an_empty_success(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / "ledger.json"
            path.write_text("{not json", encoding="utf-8")
            ledger = load_ledger(path)
        self.assertEqual(len(ledger), 1)
        self.assertFalse(ledger[0].valid)


class MatrixStateTests(unittest.TestCase):
    def build(self, runs: dict[str, dict] | None = None, **kwargs) -> CapabilityMatrix:
        # The ledger is read lazily, so the directory has to outlive this helper. It is
        # removed when the test finishes, not when the fixture returns.
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home, True)
        path = Path(home) / "ledger.json"
        path.write_text(json.dumps({"tools": kwargs.pop("tools", {})}), encoding="utf-8")
        registry = registry_with()
        return CapabilityMatrix(
            registry,
            ledger_path=path,
            stats=FakeRuns(runs or {}),
            platform=kwargs.pop("platform", "windows"),
            driver_probe=kwargs.pop("driver_probe", lambda module: True),
        )

    def test_green_requires_a_recording_and_a_real_run(self) -> None:
        tools = {
            "probe_write": {
                "machine": "desk",
                "os": "Windows 11",
                "date": "2026-10-01",
                "recording": "artifacts/recordings/fs_write.mp4",
            }
        }
        recorded_but_never_run = self.build(runs={}, tools=tools)
        self.assertEqual(recorded_but_never_run.record("probe_write").status, PROVISIONAL)
        self.assertIn("any real run", recorded_but_never_run.record("probe_write").reason)

        run_but_never_recorded = self.build(runs={"probe_write": {"runs": 3, "ok": 3}}, tools={})
        self.assertEqual(run_but_never_recorded.record("probe_write").status, PROVISIONAL)
        self.assertIn("recording", run_but_never_recorded.record("probe_write").reason)

        both = self.build(runs={"probe_write": {"runs": 4, "ok": 3, "last": 1700000000.0}}, tools=tools)
        record = both.record("probe_write")
        self.assertEqual(record.status, GREEN)
        self.assertTrue(record.green)
        self.assertEqual(record.runs, 4)
        self.assertEqual(record.ok, 3)
        self.assertEqual(record.last_verified, "2026-10-01")
        self.assertAlmostEqual(record.success_rate or 0, 0.75, places=4)
        self.assertTrue(both.record("probe_write").recording.endswith("fs_write.mp4"))

    def test_incomplete_evidence_cannot_green_a_tool(self) -> None:
        matrix = self.build(
            runs={"probe_word": {"runs": 2, "ok": 2}},
            tools={
                "probe_word": {
                    "machine": "desk",
                    "os": "Windows 11",
                    "date": "",
                    "recording": "artifacts/recordings/word.mp4",
                }
            },
        )
        record = matrix.record("probe_word")
        self.assertEqual(record.status, PROVISIONAL)
        self.assertIn("incomplete", record.evidence_problem)
        self.assertEqual(matrix.summary()["invalid_evidence"][0]["tool"], "probe_word")
        self.assertFalse([r for r in matrix.records() if r.tool == "probe_word" and r.green])

    def test_a_group_without_a_driver_profile_is_never_claimed(self) -> None:
        matrix = self.build()
        record = matrix.record("probe_mystery")
        self.assertEqual(record.status, UNAVAILABLE)
        self.assertFalse(record.available)
        self.assertIn("no driver profile", record.unavailability)

    def test_missing_driver_hides_the_tool_here_but_not_the_capability(self) -> None:
        matrix = self.build(driver_probe=lambda module: module != "pyautogui")
        record = matrix.record("probe_click")
        self.assertEqual(record.status, PROVISIONAL)
        self.assertTrue(record.available)
        self.assertEqual(record.local, LOCAL_DRIVER_MISSING)
        self.assertFalse(record.available_here)
        self.assertIn("pyautogui", record.unavailability)
        self.assertNotIn("probe_click", matrix.available_names())
        self.assertIn("probe_click", [hidden.tool for hidden in matrix.hidden()])

    def test_wrong_platform_hides_desktop_tools_on_linux(self) -> None:
        matrix = self.build(platform="linux")
        record = matrix.record("probe_word")
        self.assertEqual(record.status, PROVISIONAL)
        self.assertEqual(record.local, LOCAL_WRONG_PLATFORM)
        self.assertFalse(record.available_here)
        self.assertIn("windows", record.unavailability)
        self.assertIn("probe_write", matrix.available_names())

    def test_phantom_evidence_is_reported(self) -> None:
        matrix = self.build(
            tools={
                "probe_removed": {
                    "machine": "desk",
                    "os": "Windows 11",
                    "date": "2026-10-01",
                    "recording": "artifacts/recordings/gone.mp4",
                }
            }
        )
        self.assertEqual(matrix.phantom_recordings(), ["probe_removed"])
        self.assertIn("probe_removed", matrix.summary()["phantom_evidence"])

    def test_markdown_covers_every_declared_tool_and_states_the_rule(self) -> None:
        matrix = self.build()
        doc = matrix.markdown()
        for name in ["probe_click", "probe_write", "probe_word", "probe_mystery"]:
            self.assertIn(f"`{name}`", doc)
        self.assertIn("4 declared tool(s)", doc)
        self.assertIn("never attempted and guessed at", doc)
        self.assertIn("How a tool becomes green", doc)


class RegistryFilteringTests(unittest.TestCase):
    def test_unavailable_tools_are_never_offered_to_a_model(self) -> None:
        registry = registry_with()
        registry.capabilities = CapabilityMatrix(
            registry,
            ledger_path=Path(tempfile.mkdtemp()) / "absent.json",
            platform="windows",
            driver_probe=lambda module: module != "pyautogui" and module != "win32com.client",
        )
        offered = [spec.name for spec in registry.tool_specs()]
        self.assertNotIn("probe_click", offered)
        self.assertNotIn("probe_word", offered)
        self.assertIn("probe_write", offered)
        reasons = {item["tool"]: item["reason"] for item in registry.unavailable_tools()}
        self.assertIn("pyautogui", reasons["probe_click"])
        self.assertIn("win32com.client", reasons["probe_word"])

    def test_unproven_tools_are_labelled_in_their_own_description(self) -> None:
        registry = registry_with()
        registry.capabilities = CapabilityMatrix(
            registry,
            ledger_path=Path(tempfile.mkdtemp()) / "absent.json",
            platform="windows",
            driver_probe=lambda module: True,
        )
        specs = {spec.name: spec for spec in registry.tool_specs()}
        self.assertIn("[unproven]", specs["probe_write"].description)

    def test_raw_surface_can_still_be_inspected(self) -> None:
        registry = registry_with()
        specs = registry.tool_specs(available_only=False)
        self.assertEqual(len(specs), 4)
        self.assertTrue(all("[unproven]" not in spec.description for spec in specs))


class CommittedDocumentTests(unittest.TestCase):
    def test_the_committed_matrix_and_json_are_current(self) -> None:
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "capability_matrix.py"), "--check"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=180,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = json.loads((ROOT / "artifacts" / "capabilities.json").read_text(encoding="utf-8"))
        self.assertEqual(data["summary"]["tools"], len(data["tools"]))
        self.assertGreaterEqual(data["summary"]["tools"], 100)
        for row in data["tools"]:
            self.assertIn(row["status"], {GREEN, PROVISIONAL, UNAVAILABLE})

    def test_the_committed_document_does_not_depend_on_this_machine(self) -> None:
        doc = (ROOT / "CAPABILITY_MATRIX.md").read_text(encoding="utf-8")
        self.assertIn("declared surface", doc)
        self.assertNotIn("Machine platform for this report", doc)


if __name__ == "__main__":
    unittest.main()