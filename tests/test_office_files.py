"""Tier B document engine tests (spec 9.7): real files, read back, no Office involved."""

from __future__ import annotations

import os
import tempfile
import unittest
import zipfile

from zyntharo.actions.registry import ActionContext, ActionRegistry
from zyntharo.ops import docengine
from zyntharo.ops.docengine import (
    DocumentError,
    NotSupported,
    content_hash,
    csv_read,
    csv_write,
    describe_document,
    docx_append,
    docx_blocks,
    docx_text,
    docx_write,
    pptx_slides,
    pptx_write,
    xlsx_read,
    xlsx_sheets,
    xlsx_write,
)


class DocxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.home = tempfile.mkdtemp()
        self.path = os.path.join(self.home, "report.docx")

    def test_a_document_is_created_and_read_back_with_styles_and_tables(self) -> None:
        info = docx_write(
            self.path,
            [
                {"text": "Quarterly Report", "style": "Title"},
                {"text": "Summary", "style": "Heading1"},
                {"text": "Revenue grew 12%."},
                {"text": "first", "style": "ListBullet"},
                {"table": [["Region", "Q1"], ["EMEA", "10"]]},
            ],
            title="Quarterly Report",
        )
        self.assertTrue(info["verified"])
        self.assertEqual(info["paragraphs"], 4)
        self.assertEqual(info["tables"], 1)
        blocks = docx_blocks(self.path)
        self.assertEqual(blocks[0].style, "Title")
        self.assertEqual(blocks[1].style, "Heading1")
        self.assertEqual(blocks[-1].table, [["Region", "Q1"], ["EMEA", "10"]])
        text = docx_text(self.path)
        self.assertIn("Revenue grew 12%.", text)
        self.assertIn("EMEA\t10", text)

    def test_the_package_is_a_valid_ooxml_zip(self) -> None:
        docx_write(self.path, [{"text": "hello"}])
        with zipfile.ZipFile(self.path) as archive:
            names = set(archive.namelist())
        for required in ("[Content_Types].xml", "_rels/.rels", "word/document.xml", "word/styles.xml"):
            self.assertIn(required, names)
        self.assertEqual(content_hash(self.path), docx_write(self.path, [{"text": "hello"}])["hash"])

    def test_appending_keeps_what_was_there(self) -> None:
        docx_write(self.path, [{"text": "Body"}])
        info = docx_append(self.path, [{"text": "Appendix", "style": "Heading1"}])
        self.assertTrue(info["verified"])
        self.assertEqual(info["appended_blocks"], 1)
        text = docx_text(self.path)
        self.assertLess(text.index("Body"), text.index("Appendix"))

    def test_a_non_document_is_refused_with_a_reason(self) -> None:
        bogus = os.path.join(self.home, "bogus.docx")
        with open(bogus, "wb") as handle:
            handle.write(b"this is not a zip")
        with self.assertRaises(DocumentError):
            docx_blocks(bogus)

    def test_unicode_and_line_breaks_survive(self) -> None:
        docx_write(self.path, [{"text": "urdu: \u0627\u0631\u062f\u0648 and a break\nsecond line"}])
        text = docx_text(self.path)
        self.assertIn("\u0627\u0631\u062f\u0648", text)
        self.assertIn("second line", text)


class XlsxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.home = tempfile.mkdtemp()
        self.path = os.path.join(self.home, "budget.xlsx")

    def test_values_are_written_and_read_back_with_their_types(self) -> None:
        info = xlsx_write(
            self.path,
            {"Budget": [["Item", "Cost"], ["Hosting", 120], ["Domain", 12.5], ["Total", "=SUM(B2:B3)"], ["Note", True]]},
        )
        self.assertTrue(info["verified"], info)
        self.assertEqual(xlsx_sheets(self.path), ["Budget"])
        rows = xlsx_read(self.path)["rows"]
        self.assertEqual(rows[1], ["Hosting", 120])
        self.assertEqual(rows[2], ["Domain", 12.5])
        self.assertEqual(rows[3], ["Total", "=SUM(B2:B3)"])
        self.assertEqual(rows[4], ["Note", True])

    def test_a_range_read_returns_only_that_window(self) -> None:
        xlsx_write(self.path, {"S": [["a", "b", "c"], ["d", "e", "f"], ["g", "h", "i"]]})
        rows = xlsx_read(self.path, cell_range="B2:C3")["rows"]
        self.assertEqual(rows, [["e", "f"], ["h", "i"]])

    def test_an_unknown_sheet_is_an_error_not_an_empty_sheet(self) -> None:
        xlsx_write(self.path, {"S": [["a"]]})
        with self.assertRaises(DocumentError):
            xlsx_read(self.path, sheet="Missing")

    def test_column_names_round_trip(self) -> None:
        for index, letters in ((1, "A"), (26, "Z"), (27, "AA"), (52, "AZ"), (703, "AAA")):
            self.assertEqual(docengine.column_letter(index), letters)
            self.assertEqual(docengine.column_index(letters), index)


class CsvTests(unittest.TestCase):
    def test_a_semicolon_file_is_detected_not_assumed(self) -> None:
        path = os.path.join(tempfile.mkdtemp(), "t.csv")
        info = csv_write(path, [["a", "b"], ["1", "2"]], delimiter=";")
        self.assertTrue(info["verified"])
        back = csv_read(path)
        self.assertEqual(back["delimiter"], ";")
        self.assertEqual(back["rows"], [["a", "b"], ["1", "2"]])

    def test_quoted_cells_and_unicode_survive(self) -> None:
        path = os.path.join(tempfile.mkdtemp(), "u.csv")
        csv_write(path, [["name", "note"], ["ann", 'says "hi", then leaves'], ["\u0639\u0644\u06cc", "ok"]])
        rows = csv_read(path)["rows"]
        self.assertEqual(rows[1][1], 'says "hi", then leaves')
        self.assertEqual(rows[2][0], "\u0639\u0644\u06cc")


class LimitedFormatTests(unittest.TestCase):
    def test_pptx_reading_works_and_writing_refuses_honestly(self) -> None:
        with self.assertRaises(NotSupported) as caught:
            pptx_write("deck.pptx")
        self.assertIn("does not author", str(caught.exception))
        self.assertEqual(caught.exception.evidence["supported"], ["read"])

    def test_a_pptx_reader_rejects_a_file_that_is_not_a_presentation(self) -> None:
        path = os.path.join(tempfile.mkdtemp(), "fake.pptx")
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("hello.txt", "not a deck")
        with self.assertRaises(DocumentError):
            pptx_slides(path)

    def test_describe_document_is_honest_about_write_support(self) -> None:
        home = tempfile.mkdtemp()
        deck = os.path.join(home, "d.pptx")
        with zipfile.ZipFile(deck, "w") as archive:
            archive.writestr("ppt/slides/slide1.xml", "<p/>")
        info = describe_document(deck)
        self.assertTrue(info["readable"])
        self.assertFalse(info["writable"])
        self.assertIn(".docx", info["note"])

    def test_unsupported_extension_is_refused_by_the_action(self) -> None:
        registry = ActionRegistry()
        from zyntharo.actions import office_files

        registry.register_all(office_files.all_actions())
        ctx = ActionContext(capabilities=frozenset({"office.word", "office.excel", "office.powerpoint", "filesystem.read"}))
        result = registry.invoke("docx_write", ctx, path="/tmp/thing.exe", blocks=[{"text": "x"}])
        self.assertFalse(result.ok)
        self.assertIn("does not write", result.error)


class ActionTests(unittest.TestCase):
    def setUp(self) -> None:
        from zyntharo.actions import office_files

        self.registry = ActionRegistry()
        self.registry.register_all(office_files.all_actions())
        self.ctx = ActionContext(
            capabilities=frozenset({"office.word", "office.excel", "office.powerpoint", "filesystem.read"})
        )
        self.home = tempfile.mkdtemp()

    def test_a_docx_written_through_the_action_is_verified_and_receipted(self) -> None:
        path = os.path.join(self.home, "letter.docx")
        result = self.registry.invoke(
            "docx_write",
            self.ctx,
            path=path,
            blocks=[{"text": "Dear owner", "style": "Heading1"}, {"text": "Body."}],
        )
        self.assertTrue(result.ok, result.error)
        self.assertTrue(result.output["verified"])
        receipt = result.evidence["receipt"]
        self.assertEqual(receipt["outcome"], "VERIFIED")
        self.assertEqual(receipt["channels"], ["content hash", "reparsed document"])
        self.assertIn("Dear owner", docx_text(path))

    def test_an_xlsx_written_through_the_action_reads_back(self) -> None:
        path = os.path.join(self.home, "book.xlsx")
        result = self.registry.invoke("xlsx_write", self.ctx, path=path, sheets={"S": [["k", "v"], ["a", 1]]})
        self.assertTrue(result.ok, result.error)
        self.assertTrue(result.output["verified"])
        read = self.registry.invoke("xlsx_read", self.ctx, path=path)
        self.assertEqual(read.output["rows"][1], ["a", 1])

    def test_a_missing_file_is_reported_as_missing(self) -> None:
        result = self.registry.invoke("docx_read", self.ctx, path=os.path.join(self.home, "absent.docx"))
        self.assertFalse(result.ok)
        self.assertIn("no such file", result.error)

    def test_pptx_write_action_carries_the_reason_and_the_need(self) -> None:
        result = self.registry.invoke("pptx_write", self.ctx, path=os.path.join(self.home, "x.pptx"))
        self.assertFalse(result.ok)
        self.assertEqual(result.evidence.get("needs"), "Microsoft PowerPoint (Tier A)")


if __name__ == "__main__":
    unittest.main()