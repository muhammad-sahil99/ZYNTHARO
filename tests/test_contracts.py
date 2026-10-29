"""Contract tests (spec 3.4): do the things the system really produces satisfy the schemas?

These validate live objects — a receipt from a real invoke, a journal entry written by the
journal, an event off the bus, a tool catalog row, a task contract — against the committed
JSON Schema. A writer that drifts from its contract fails here, not in a customer's upgrade.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

from zyntharo import contracts
from zyntharo.actions import build_registry
from zyntharo.actions.registry import ActionContext, ActionRegistry, ActionResult, action
from zyntharo.contracts import compat
from zyntharo.events import Event, EventBus
from zyntharo.memory import MemoryStore
from zyntharo.ops.journal import Journal
from zyntharo.ops.receipt import FAILED, UNVERIFIED, VERIFIED, Receipt, channels_in
from zyntharo.ops.verification import SuccessCriterion, TaskContract
from zyntharo.plugins.loader import PluginManifest


@action("contract_probe", "A probe used only by the contract tests.", "filesystem.read", {}, group="filesystem")
def contract_probe(ctx: ActionContext) -> str:  # pragma: no cover - registered, never called
    return "probe"


def registry() -> ActionRegistry:
    reg = ActionRegistry()
    reg.register(contract_probe)
    return reg


class RegistryTests(unittest.TestCase):
    def test_every_contract_loads_and_is_consistent(self) -> None:
        report = contracts.audit()
        self.assertEqual(report["problems"], [], report["problems"])
        self.assertGreaterEqual(len(report["contracts"]), 8)
        for row in report["contracts"]:
            self.assertIn(row["status"], ("frozen", "draft"))
            self.assertGreaterEqual(row["properties"], 1, row["contract"])
            self.assertIsNotNone(row["major"], row["contract"])

    def test_an_unknown_contract_is_an_error_not_an_empty_result(self) -> None:
        with self.assertRaises(contracts.ContractError):
            contracts.definition("not-a-contract")
        with self.assertRaises(contracts.ContractError):
            contracts.version("not-a-contract")


class LiveObjectTests(unittest.TestCase):
    def test_a_tool_row_satisfies_the_tool_contract(self) -> None:
        reg = build_registry(capabilities=False)
        rows = reg.describe()
        self.assertGreater(len(rows), 100)
        for row in rows[:20] + rows[-5:]:
            enriched = {**row, "risk_tier": reg.risk_tier(row["name"])}
            result = contracts.validate("tool", enriched)
            self.assertTrue(result.ok, f"{row['name']}: {result.errors}")

    def test_an_event_satisfies_the_event_contract(self) -> None:
        bus = EventBus()
        received = []
        bus.subscribe("*", received.append)
        bus.emit("system.log", source="test", message="hello")
        self.assertEqual(len(received), 1)
        result = contracts.validate("event", received[0].to_json())
        self.assertTrue(result.ok, result.errors)

    def test_a_task_contract_satisfies_its_schema(self) -> None:
        contract = TaskContract(task_id="t1", request="Make the report")
        contract.add("file_exists", target="/tmp/report.docx")
        contract.criteria.append(SuccessCriterion(kind="subjective", note="reads well"))
        result = contracts.validate("task-contract", contract.to_json())
        self.assertTrue(result.ok, result.errors)

    def test_a_plugin_manifest_satisfies_its_schema(self) -> None:
        manifest = PluginManifest(name="sample_plugin", capabilities=["filesystem.read"])
        result = contracts.validate("plugin-manifest", manifest.to_json())
        self.assertTrue(result.ok, result.errors)

    def test_a_memory_record_satisfies_its_schema(self) -> None:
        home = tempfile.mkdtemp()
        store = MemoryStore(Path(home) / "memory.db")
        try:
            store.remember(agent_id="tester", kind="fact", text="the sky is blue", importance=0.5)
            hits = store.recall("tester", "sky")
            self.assertTrue(hits)
            result = contracts.validate("memory-record", hits[0].to_json())
            self.assertTrue(result.ok, result.errors)
        finally:
            store.close()

    def test_a_real_receipt_satisfies_the_receipt_contract(self) -> None:
        reg = registry()
        reg.register(
            __import__("zyntharo.actions.registry", fromlist=["Action"]).Action(
                name="contract_writer",
                description="Writes with a read-back channel.",
                capability="filesystem.read",
                handler=lambda ctx: ActionResult.success(
                    "ok", evidence={"readback": "hello", "target": "/tmp/x.txt", "after": {"hash": "abc"}}
                ),
                group="filesystem",
            )
        )
        ctx = ActionContext(capabilities=frozenset({"filesystem.read"}), task_id="task-1")
        result = reg.invoke("contract_writer", ctx)
        self.assertTrue(result.ok)
        receipt = result.evidence.get("receipt")
        self.assertIsNotNone(receipt, "every executed call must carry a receipt")
        check = contracts.validate("receipt", receipt)
        self.assertTrue(check.ok, check.errors)
        self.assertEqual(receipt["outcome"], VERIFIED)
        self.assertEqual(receipt["task_id"], "task-1")
        self.assertEqual(receipt["channels"], ["control read-back"])
        self.assertEqual(receipt["rung"], 1)

    def test_a_receipt_is_stored_and_readable(self) -> None:
        home = tempfile.mkdtemp()
        store = MemoryStore(Path(home) / "memory.db")
        reg = registry()
        reg.memory = store
        reg.register(
            __import__("zyntharo.actions.registry", fromlist=["Action"]).Action(
                name="contract_noop",
                description="Returns success without evidence.",
                capability="filesystem.read",
                handler=lambda ctx: ActionResult.success("nothing proven"),
                group="filesystem",
            )
        )
        reg.invoke("contract_noop", ActionContext(capabilities=frozenset({"filesystem.read"}), task_id="t9"))
        try:
            stored = store.receipts(task_id="t9")
            self.assertEqual(len(stored), 1)
            self.assertEqual(stored[0]["outcome"], UNVERIFIED)
            stats = store.receipt_stats("t9")
            self.assertEqual(stats["by_outcome"], {UNVERIFIED: 1})
        finally:
            store.close()

    def test_a_journal_entry_satisfies_the_journal_contract(self) -> None:
        home = Path(tempfile.mkdtemp())
        journal = Journal(home / "journal.db")
        reg = registry()
        reg.journal = journal
        reg.register(
            __import__("zyntharo.actions.registry", fromlist=["Action"]).Action(
                name="contract_write",
                description="Writes a file.",
                capability="filesystem.write",
                handler=lambda ctx, path: ActionResult.success(path, evidence={"hash": "deadbeef"}),
                group="filesystem",
            )
        )
        ctx = ActionContext(capabilities=frozenset({"filesystem.write"}), task_id="j1")
        reg.invoke("contract_write", ctx, path=str(home / "a.txt"))
        try:
            entries = journal.history("j1")
            self.assertTrue(entries)
            result = contracts.validate("journal-entry", entries[0].to_json())
            self.assertTrue(result.ok, result.errors)
        finally:
            journal.close()


class ValidatorTests(unittest.TestCase):
    def test_bad_records_are_rejected_with_a_useful_message(self) -> None:
        base = {
            "receipt_id": "abc123",
            "tool": "fs_read",
            "outcome": "VERIFIED",
            "rung": 1,
            "started_at": 1.0,
        }
        self.assertTrue(contracts.validate("receipt", base).ok)

        missing = dict(base)
        del missing["outcome"]
        self.assertIn("missing required property 'outcome'", " ".join(contracts.validate("receipt", missing).errors))

        enum = {**base, "outcome": "probably"}
        self.assertIn("is not one of", " ".join(contracts.validate("receipt", enum).errors))

        extra = {**base, "made_up": True}
        self.assertIn("unexpected property", " ".join(contracts.validate("receipt", extra).errors))

        rung = {**base, "rung": 9}
        self.assertTrue(contracts.validate("receipt", rung).errors)

        pattern = {"topic": "Not A Topic", "severity": "info", "ts": 1.0, "id": "abcdef"}
        self.assertTrue(contracts.validate("event", pattern).errors)

    def test_an_unsupported_keyword_is_refused_not_ignored(self) -> None:
        from zyntharo.contracts.validator import validate

        schema = {"type": "object", "properties": {"a": {"type": "string", "format": "email"}}}
        with self.assertRaises(ValueError):
            validate({"a": "x@y.z"}, schema)

    def test_refs_are_resolved_against_the_bundle(self) -> None:
        bundle = contracts.load_bundle("records.schema.json")
        contract = TaskContract(task_id="t2", request="x")
        contract.add("file_exists", target="/tmp/x")
        from zyntharo.contracts.validator import validate

        result = validate(
            contract.to_json(),
            bundle["$defs"]["task-contract"],
            contract="task-contract",
            root=bundle,
        )
        self.assertTrue(result.ok, result.errors)


class CompatibilityTests(unittest.TestCase):
    def test_same_major_is_readable_and_a_newer_minor_only_warns(self) -> None:
        self.assertEqual(compat.check("receipt", "1.0.0", "1.0.0").decision, compat.ACCEPT)
        newer = compat.check("receipt", "1.3.0", "1.0.0")
        self.assertEqual(newer.decision, compat.WARN)
        self.assertIn("unknown fields", newer.reason)
        older = compat.check("receipt", "1.0.0", "1.2.0")
        self.assertEqual(older.decision, compat.WARN)

    def test_a_different_major_is_refused_with_a_migration_named(self) -> None:
        result = compat.check("receipt", "2.0.0", "1.0.0")
        self.assertEqual(result.decision, compat.REFUSE)
        self.assertFalse(result.readable)
        self.assertIn("migration", result.reason)

    def test_an_unparsable_or_unknown_version_is_refused(self) -> None:
        self.assertEqual(compat.check("receipt", "v1", "1.0.0").decision, compat.REFUSE)
        results = compat.check_manifest({"receipt": "1.0.0", "telepathy": "1.0.0"})
        decisions = {item.contract: item.decision for item in results}
        self.assertEqual(decisions["receipt"], compat.ACCEPT)
        self.assertEqual(decisions["telepathy"], compat.REFUSE)

    def test_plugin_validation_uses_the_contract_versions(self) -> None:
        from zyntharo.plugins.loader import PluginHost

        home = Path(tempfile.mkdtemp())
        plugin_dir = home / "plugins" / "bad_target"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "actions.py").write_text("def all_actions():\n    return []\n", encoding="utf-8")
        (plugin_dir / "plugin.json").write_text(
            json.dumps(
                {
                    "name": "bad_target",
                    "version": "0.1.0",
                    "entry": "actions.py",
                    "capabilities": [],
                    "schema_version": 1,
                    "contracts": {"receipt": "9.0.0"},
                }
            ),
            encoding="utf-8",
        )
        host = PluginHost(home, registry())
        result = host.validate(plugin_dir, run_self_test=False)
        self.assertFalse(result.ok)
        self.assertTrue(any("cannot serve" in error for error in result.errors), result.errors)


class GeneratedBindingsTests(unittest.TestCase):
    def test_the_generated_bindings_are_current(self) -> None:
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "contracts.py"), "--check"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_the_python_bindings_round_trip(self) -> None:
        from zyntharo.contracts.generated import Receipt as GeneratedReceipt

        data = {
            "receipt_id": "abc123",
            "tool": "fs_read",
            "outcome": "VERIFIED",
            "rung": 1,
            "started_at": 1.0,
        }
        record = GeneratedReceipt.from_dict({**data, "unknown_field": 1})
        self.assertEqual(record.receipt_id, "abc123")
        self.assertFalse(hasattr(record, "unknown_field"))
        self.assertEqual(record.to_dict()["tool"], "fs_read")


if __name__ == "__main__":
    unittest.main()