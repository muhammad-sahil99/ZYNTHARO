"""Tests for the local search index (spec 14.2).

The index is tested for the things that make it trustworthy rather than for its speed: what it
reads, what it refuses to pretend to read, whether it re-indexes only what changed, and whether
a result still points at the file it claims.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zyntharo.actions import localsearch  # noqa: E402
from zyntharo.actions.registry import ActionContext  # noqa: E402
from zyntharo.config import expand_capabilities  # noqa: E402
from zyntharo.ops.index import SearchIndex  # noqa: E402


class IndexCase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.root = self.home / "docs"
        self.root.mkdir()
        (self.root / "notes.md").write_text("# Cooling Notes\n\nThe radiator fan curve is too aggressive at idle.\n", encoding="utf-8")
        (self.root / "code.py").write_text("def thermal_margin():\n    return 'headroom'\n", encoding="utf-8")
        (self.root / "skip" ).mkdir()
        (self.root / "skip" / "huge.bin").write_bytes(b"\x00" * 64)
        (self.root / "node_modules").mkdir()
        (self.root / "node_modules" / "vendored.md").write_text("vendoredmarker inside a dependency", encoding="utf-8")
        (self.root / "picture.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
        self.index = SearchIndex(self.home / "index.db")

    def tearDown(self):
        self.index.close()

    def scan(self):
        self.index.add_source("docs", self.root)
        return self.index.update()


class Reading(IndexCase):
    def test_text_and_code_are_indexed_and_found(self):
        report = self.scan()
        self.assertGreaterEqual(report["updated"], 2)
        hits = self.index.search("radiator")
        self.assertEqual(hits["count"], 1)
        self.assertEqual(Path(hits["hits"][0]["path"]).name, "notes.md")
        self.assertIn("radiator", hits["hits"][0]["snippet"].lower())
        self.assertTrue(hits["hits"][0]["verified"])

    def test_the_title_comes_from_the_document(self):
        self.scan()
        hit = self.index.search("radiator")["hits"][0]
        self.assertEqual(hit["title"], "Cooling Notes")

    def test_excluded_directories_are_not_indexed(self):
        self.scan()
        self.assertEqual(self.index.search("vendoredmarker")["count"], 0, "node_modules must never be indexed")

    def test_an_unreadable_format_is_reported_not_hidden(self):
        self.scan()
        status = self.index.status()
        self.assertGreaterEqual(status["unextractable"], 1)
        rows = self.index.conn.execute("SELECT path, error FROM docs WHERE error != ''").fetchall()
        self.assertTrue(any("not searchable" in row["error"] for row in rows))

    def test_capabilities_are_stated_honestly(self):
        caps = self.index.status()["capabilities"]
        self.assertTrue(caps["lexical"])
        self.assertFalse(caps["semantic"], "there are no embeddings in this build and it must say so")
        self.assertFalse(caps["file_watch"])


class Incremental(IndexCase):
    def test_an_unchanged_file_is_skipped(self):
        self.scan()
        second = self.index.update()
        self.assertEqual(second["updated"], 0)
        self.assertGreaterEqual(second["skipped"], 2)

    def test_a_changed_file_is_reindexed(self):
        self.scan()
        time.sleep(0.01)
        target = self.root / "notes.md"
        target.write_text("# Cooling Notes\n\nThe pump makes a clicking sound.\n", encoding="utf-8")
        os.utime(target, (time.time() + 2, time.time() + 2))
        report = self.index.update()
        self.assertEqual(report["updated"], 1)
        self.assertEqual(self.index.search("clicking")["count"], 1)
        self.assertEqual(self.index.search("radiator")["count"], 0, "the old text must not linger")

    def test_a_deleted_file_leaves_no_result(self):
        self.scan()
        (self.root / "notes.md").unlink()
        report = self.index.update()
        self.assertEqual(report["removed"], 1)
        self.assertEqual(self.index.search("radiator")["count"], 0)

    def test_run_until_idle_stops_when_nothing_changes(self):
        self.index.add_source("docs", self.root)
        total = self.index.run_until_idle(batch=50, sleep=0.0)
        self.assertGreaterEqual(total["runs"], 1)
        self.assertLessEqual(total["runs"], 3)


class Verification(IndexCase):
    def test_a_result_whose_file_changed_is_dropped_with_a_reason(self):
        self.scan()
        target = self.root / "notes.md"
        target.write_text("# Cooling Notes\n\ncompletely different content\n", encoding="utf-8")
        result = self.index.search("radiator")
        # The query still matches the stored row, but the file no longer contains it: the
        # result is removed rather than returned with a snippet that cannot be found on disk.
        self.assertEqual(result["count"], 0)
        stale = self.index.conn.execute("SELECT id FROM docs WHERE path=?", (str(target),)).fetchone()
        self.assertIsNone(stale, "the stale row was cleaned up, not returned with a dead snippet")

    def test_a_missing_file_is_not_a_result(self):
        self.scan()
        (self.root / "notes.md").unlink()
        self.assertEqual(self.index.search("radiator")["count"], 0)


class Permissions(IndexCase):
    def test_personal_sources_need_explicit_opt_in(self):
        with self.assertRaises(PermissionError):
            self.index.add_source("mail", self.root, kind="email")
        allowed = self.index.add_source("mail", self.root, kind="email", opt_in=True)
        self.assertTrue(allowed["opt_in"])

    def test_forgetting_a_source_removes_its_rows(self):
        self.scan()
        removed = self.index.forget_source("docs")
        self.assertGreaterEqual(removed, 2)
        self.assertEqual(self.index.search("radiator")["count"], 0)


class Actions(IndexCase):
    def setUp(self):
        super().setUp()
        localsearch.set_index(self.index)
        self.ctx = ActionContext(agent_id="t", capabilities=expand_capabilities(["filesystem.read"]))

    def tearDown(self):
        localsearch.set_index(None)
        super().tearDown()

    def test_an_agent_cannot_add_a_personal_source(self):
        with self.assertRaises(Exception) as caught:
            localsearch.local_index_add.invoke(self.ctx, name="mail", root=str(self.root), kind="email")
        self.assertIn("owner", str(caught.exception))

    def test_search_and_update_round_trip_through_actions(self):
        localsearch.local_index_add.invoke(self.ctx, name="docs", root=str(self.root))
        localsearch.local_index_update.invoke(self.ctx)
        hits = localsearch.local_search.invoke(self.ctx, query="thermal_margin")
        self.assertEqual(hits["count"], 1)

    def test_status_action_reports_unextractable_files(self):
        localsearch.local_index_add.invoke(self.ctx, name="docs", root=str(self.root))
        localsearch.local_index_update.invoke(self.ctx)
        status = localsearch.local_index_status.invoke(self.ctx)
        self.assertIn("capabilities", status)
        self.assertGreaterEqual(status["unextractable"], 1)


if __name__ == "__main__":
    unittest.main()