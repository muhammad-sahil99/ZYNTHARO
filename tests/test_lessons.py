"""Mistake memory: does a repeated failure actually teach the next attempt something?

The failure this prevents is specific and was observed in practice: an agent retries a tool
that cannot work on this machine, gets the same error, and retries again. A lesson book is only
worth having if the *second* failure carries the fix — so that is what these tests assert,
including the parts that must not happen (no fix invented for an unknown failure, no growth
without a cap, no crash when the book cannot be written).
"""

from __future__ import annotations

import os
import tempfile
import unittest

from zyntharo.ops.lessons import LessonBook, signature_of


class SignatureTests(unittest.TestCase):
    def test_numbers_and_paths_do_not_create_duplicate_lessons(self):
        first = signature_of("browser_click", "timed out after 12s at C:\\Users\\a\\x.html")
        second = signature_of("browser_click", "timed out after 300s at C:\\Users\\b\\y.html")
        self.assertEqual(first, second)

    def test_different_tools_are_different_lessons(self):
        self.assertNotEqual(
            signature_of("browser_click", "selector matched nothing"),
            signature_of("input_click", "selector matched nothing"),
        )


class LessonBookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.book = LessonBook(self.tmp.name)

    def test_the_second_failure_carries_the_known_fix(self):
        self.book.record("browser_click", "selector matched nothing", fix="use pierce=true or vision_ground")
        error = self.book.annotate("browser_click", "selector matched nothing")
        self.assertIn("known fix", error)
        self.assertIn("pierce=true", error)
        self.assertIn("seen 2x", error)

    def test_a_fix_is_not_invented_for_an_unknown_failure(self):
        error = self.book.annotate("browser_click", "an error nobody has seen before")
        self.assertNotIn("known fix", error)
        self.assertEqual(self.book.rows()[0]["fix"], "")
        self.assertEqual(self.book.top(), [], "a lesson without a fix must not be offered to an agent")

    def test_repeats_are_counted_rather_than_duplicated(self):
        for _ in range(4):
            self.book.record("filesystem_delete", "capability denied", fix="ask the owner for filesystem.delete")
        self.assertEqual(self.book.stats()["lessons"], 1)
        lesson = self.book.rows()[0]
        self.assertEqual(lesson["hits"], 4)

    def test_lessons_rank_by_relevance_to_the_action_and_error(self):
        self.book.record("browser_click", "selector matched nothing", fix="pierce the shadow root")
        self.book.record("office_excel", "workbook is locked", fix="close the workbook first")
        best = self.book.recall("browser_click")[0]
        self.assertEqual(best.action, "browser_click")
        similar = self.book.recall("", "the selector matched nothing again")[0]
        self.assertEqual(similar.action, "browser_click")

    def test_the_book_survives_a_restart(self):
        self.book.record("input_type", "focus lost before typing", fix="verify the focused handle first")
        reopened = LessonBook(self.tmp.name)
        self.assertEqual(reopened.rows()[0]["action"], "input_type")

    def test_forgetting_is_possible_and_reports_how_many_went(self):
        self.book.record("a", "x", fix="f")
        self.book.record("b", "y", fix="g")
        self.assertEqual(self.book.forget("a"), 1)
        self.assertEqual(len(self.book.rows()), 1)

    def test_an_unwritable_path_fails_loudly_at_the_call_not_silently_inside_the_registry(self):
        book = LessonBook(path=os.path.join(self.tmp.name, "no-such-dir", "x", "lessons.json"))
        book.save()  # creates the tree, so the caller learns about real permission errors
        self.assertTrue(os.path.exists(book.path))

    def test_the_book_is_capped_so_it_cannot_grow_forever(self):
        book = LessonBook(path=os.path.join(self.tmp.name, "capped.json"))

        def word(index: int) -> str:
            # Alphabetic only: "distinct failure 41" and "distinct failure 42" are the *same*
            # lesson once numbers are normalised, so they cannot be used to fill the book.
            return "".join(chr(97 + (index // 26 ** power) % 26) for power in range(3, -1, -1))

        for index in range(430):
            book.record("tool", f"distinct failure {word(index)}", fix="fix")
        self.assertLessEqual(len(book.lessons), 400)
        self.assertEqual(len(book.rows()), 400)


class RegistryIntegrationTests(unittest.TestCase):
    """The registry is where the fix has to appear, because that is where the agent reads errors."""

    def test_a_repeat_failure_returns_its_error_with_the_learned_fix(self):
        """The first failure is recorded; the *second* one carries the correction.

        Annotating the first failure taught agents to ignore the hint, because every validation
        refusal came with a "known fix" line. The test drives the real registry twice, which is
        also the only way to prove the signature matches what the action actually emits.
        """
        from tests.helpers import Stack

        stack = Stack()
        self.addCleanup(stack.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        stack.actions.lessons = LessonBook(tmp.name)
        first = stack.tool("browser_navigate", capabilities=["browser.navigate"], url="https://example.com")
        self.assertFalse(first.ok)
        self.assertNotIn("known fix", first.error)
        stack.actions.lessons.record(
            "browser_navigate", first.error, fix='pip install playwright && playwright install chromium'
        )
        second = stack.tool("browser_navigate", capabilities=["browser.navigate"], url="https://example.com")
        self.assertFalse(second.ok)
        self.assertIn("known fix", second.error)
        self.assertIn("playwright install chromium", second.error)

    def test_a_working_action_is_untouched_by_the_lesson_book(self):
        from tests.helpers import Stack

        stack = Stack()
        self.addCleanup(stack.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        stack.actions.lessons = LessonBook(tmp.name)
        result = stack.tool("fs_list", capabilities=["filesystem.read"], path=tmp.name)
        self.assertTrue(result.ok, result.error)
        self.assertNotIn("known fix", str(result.output))


if __name__ == "__main__":
    unittest.main()
