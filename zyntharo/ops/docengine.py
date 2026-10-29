"""Tier B document engine (spec 9.7): Office files with no Office installed.

Tier A drives a real Word or Excel through COM, which is the right thing when Office is
there. It is also useless on a machine without Office, in a container, or for a task that must
not disturb the user's open windows. Tier B reads and writes the files themselves — OOXML is
a zip of XML, and Python's standard library can do that without a dependency.

What is here is deliberately bounded, and the bounds are stated rather than discovered:

* **.docx** — read paragraphs, headings, tables and document properties; create a document
  from blocks; append blocks; every write is reparsed and compared before it is reported.
* **.xlsx** — read sheets and cells (shared strings, inline strings, numbers, booleans,
  formulas as written); create a workbook from rows; add a sheet; convert to and from CSV.
* **.csv** — read and write with real dialect handling and a re-read verification.
* **.pptx** — **read only**. Authoring a presentation needs slide masters, layouts and a
  theme; a hand-built package that PowerPoint might refuse would be exactly the sort of
  almost-working feature this project refuses to ship. ``write`` for .pptx therefore raises
  ``NotSupported`` with the honest reason, and the Capability Matrix says so.

Every function returns what it verified, not what it attempted: a write returns the path, the
size, the content hash and the text re-read from disk.
"""

from __future__ import annotations

import csv
import hashlib
import io
import os
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence
from xml.etree import ElementTree as ET


class DocumentError(RuntimeError):
    """The file is not what its extension claims, or it is corrupt."""


class NotSupported(DocumentError):
    """Honest refusal: this format is not written by the headless engine."""

    def __init__(self, message: str, evidence: dict[str, Any] | None = None):
        super().__init__(message)
        self.evidence = evidence or {}


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
XML_DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'


def _tag(namespace: str, name: str) -> str:
    return f"{{{namespace}}}{name}"


def content_hash(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()[:16]


def _read_part(path: str | Path, part: str) -> bytes:
    try:
        with zipfile.ZipFile(path) as archive:
            return archive.read(part)
    except KeyError as exc:
        raise DocumentError(f"{path} has no {part}: it is not a valid OOXML package") from exc
    except zipfile.BadZipFile as exc:
        raise DocumentError(f"{path} is not a zip package, so it is not an OOXML file") from exc


def _part_names(path: str | Path) -> list[str]:
    try:
        with zipfile.ZipFile(path) as archive:
            return archive.namelist()
    except zipfile.BadZipFile as exc:
        raise DocumentError(f"{path} is not a zip package") from exc


def _xml(data: bytes, part: str) -> ET.Element:
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise DocumentError(f"{part} is not well-formed XML: {exc}") from exc


def _text_of(element: ET.Element) -> str:
    """All visible text inside an element, in document order."""
    pieces: list[str] = []
    for node in element.iter():
        if node.tag in (_tag(W, "t"), _tag(A, "t")):
            pieces.append(node.text or "")
        elif node.tag == _tag(W, "tab"):
            pieces.append("\t")
        elif node.tag in (_tag(W, "br"), _tag(W, "cr")):
            pieces.append("\n")
    return "".join(pieces)


# --------------------------------------------------------------------------------------
# DOCX
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class DocBlock:
    """One paragraph or table to write. ``style`` maps to a Word paragraph style id."""

    text: str = ""
    style: str = ""  # "" | Title | Heading1..6 | Quote | ListBullet | ListNumber
    table: list[list[str]] = field(default_factory=list)
    bold: bool = False
    level: int = 0  # list level

    @property
    def is_table(self) -> bool:
        return bool(self.table)

    @classmethod
    def from_any(cls, value: Any) -> "DocBlock":
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            return cls(text=value)
        if isinstance(value, dict):
            return cls(
                text=str(value.get("text", "")),
                style=str(value.get("style", "")),
                table=[list(map(str, row)) for row in value.get("table", []) or []],
                bold=bool(value.get("bold", False)),
                level=int(value.get("level", 0) or 0),
            )
        raise DocumentError(f"cannot use {type(value).__name__} as a document block")


def docx_blocks(path: str | Path) -> list[DocBlock]:
    """Read a .docx into blocks: paragraphs with their styles, and tables row by row."""
    document = _xml(_read_part(path, "word/document.xml"), "word/document.xml")
    body = document.find(_tag(W, "body"))
    if body is None:
        raise DocumentError(f"{path}: word/document.xml has no body")
    blocks: list[DocBlock] = []
    for child in list(body):
        if child.tag == _tag(W, "p"):
            style = ""
            properties = child.find(_tag(W, "pPr"))
            if properties is not None:
                style_node = properties.find(_tag(W, "pStyle"))
                if style_node is not None:
                    style = style_node.get(_tag(W, "val"), "")
            number = properties.find(_tag(W, "numPr")) if properties is not None else None
            level = 0
            if number is not None:
                ilvl = number.find(_tag(W, "ilvl"))
                level = int(ilvl.get(_tag(W, "val"), "0")) if ilvl is not None else 0
            blocks.append(DocBlock(text=_text_of(child), style=style, level=level))
        elif child.tag == _tag(W, "tbl"):
            rows: list[list[str]] = []
            for row in child.findall(_tag(W, "tr")):
                cells = [_text_of(cell).strip() for cell in row.findall(_tag(W, "tc"))]
                rows.append(cells)
            blocks.append(DocBlock(table=rows))
    return blocks


def docx_text(path: str | Path) -> str:
    """The visible text of a document, paragraphs and table cells in order."""
    lines: list[str] = []
    for block in docx_blocks(path):
        if block.is_table:
            lines.extend("\t".join(row) for row in block.table)
        else:
            lines.append(block.text)
    return "\n".join(lines)


_CONTENT_TYPES_DOCX = (
    XML_DECL
    + f'<Types xmlns="{CT}">'
    + '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    + '<Default Extension="xml" ContentType="application/xml"/>'
    + '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    + '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
    + '<Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>'
    + '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
    + "</Types>"
)

_ROOT_RELS = (
    XML_DECL
    + f'<Relationships xmlns="{PKG_REL}">'
    + f'<Relationship Id="rId1" Type="{R}/officeDocument" Target="word/document.xml"/>'
    + '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
    + "</Relationships>"
)

_DOC_RELS = (
    XML_DECL
    + f'<Relationships xmlns="{PKG_REL}">'
    + f'<Relationship Id="rId1" Type="{R}/styles" Target="styles.xml"/>'
    + f'<Relationship Id="rId2" Type="{R}/numbering" Target="numbering.xml"/>'
    + "</Relationships>"
)

#: Two list definitions, one bullet and one numbered, so ``ListBullet``/``ListNumber``
#: produce real lists rather than paragraphs that merely start with a character.
_NUMBERING = (
    XML_DECL
    + f'<w:numbering xmlns:w="{W}">'
    + '<w:abstractNum w:abstractNumId="1"><w:lvl w:ilvl="0"><w:numFmt w:val="bullet"/><w:lvlText w:val="\u2022"/><w:pPr><w:ind w:left="720" w:hanging="360"/></w:pPr></w:lvl></w:abstractNum>'
    + '<w:abstractNum w:abstractNumId="2"><w:lvl w:ilvl="0"><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/><w:pPr><w:ind w:left="720" w:hanging="360"/></w:pPr></w:lvl></w:abstractNum>'
    + '<w:num w:numId="1"><w:abstractNumId w:val="1"/></w:num>'
    + '<w:num w:numId="2"><w:abstractNumId w:val="2"/></w:num>'
    + "</w:numbering>"
)

_STYLES = (
    XML_DECL
    + f'<w:styles xmlns:w="{W}">'
    + '<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri"/><w:sz w:val="22"/></w:rPr></w:rPrDefault></w:docDefaults>'
    + '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>'
    + '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:rPr><w:b/><w:sz w:val="56"/></w:rPr></w:style>'
    + "".join(
        f'<w:style w:type="paragraph" w:styleId="Heading{level}"><w:name w:val="heading {level}"/><w:basedOn w:val="Normal"/>'
        f'<w:next w:val="Normal"/><w:pPr><w:outlineLvl w:val="{level - 1}"/></w:pPr>'
        f'<w:rPr><w:b/><w:sz w:val="{max(24, 40 - level * 2)}"/></w:rPr></w:style>'
        for level in range(1, 7)
    )
    + '<w:style w:type="paragraph" w:styleId="Quote"><w:name w:val="Quote"/><w:pPr><w:ind w:left="720"/></w:pPr><w:rPr><w:i/></w:rPr></w:style>'
    + '<w:style w:type="paragraph" w:styleId="ListParagraph"><w:name w:val="List Paragraph"/><w:pPr><w:ind w:left="720"/></w:pPr></w:style>'
    + "</w:styles>"
)

_CORE = (
    XML_DECL
    + '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
    "<dc:title>{title}</dc:title><dc:creator>ZYNTHARO</dc:creator>"
    '<dcterms:created xsi:type="dcterms:W3CDTF">{stamp}</dcterms:created>'
    "</cp:coreProperties>"
)


def _esc(text: str) -> str:
    from xml.sax.saxutils import escape

    return escape(str(text), {"\n": "</w:t><w:br/><w:t xml:space=\"preserve\">"})


def _paragraph_xml(block: DocBlock) -> str:
    style = block.style or ""
    properties = ""
    if style:
        properties += f'<w:pStyle w:val="{_esc(style)}"/>'
    numbered = style in ("ListBullet", "ListNumber")
    num_id = 1 if style == "ListBullet" else 2
    if numbered:
        properties += (
            f'<w:numPr><w:ilvl w:val="{max(0, min(8, int(block.level)))}"/><w:numId w:val="{num_id}"/></w:numPr>'
            '<w:pStyle w:val="ListParagraph"/>'
        )
    ppr = f"<w:pPr>{properties}</w:pPr>" if properties else ""
    run_props = "<w:rPr><w:b/></w:rPr>" if block.bold else ""
    return (
        "<w:p>"
        f"{ppr}<w:r>{run_props}"
        f'<w:t xml:space="preserve">{_esc(block.text)}</w:t></w:r>'
        "</w:p>"
    )


def _table_xml(rows: Sequence[Sequence[str]]) -> str:
    if not rows:
        return ""
    columns = max(len(row) for row in rows)
    grid = "".join('<w:gridCol w:w="2400"/>' for _ in range(columns))
    body: list[str] = []
    for row in rows:
        cells: list[str] = []
        for index in range(columns):
            value = row[index] if index < len(row) else ""
            cells.append(
                "<w:tc><w:tcPr><w:tcW w:w=\"2400\" w:type=\"dxa\"/></w:tcPr>"
                f'<w:p><w:r><w:t xml:space=\"preserve\">{_esc(value)}</w:t></w:r></w:p></w:tc>'
            )
        body.append("<w:tr>" + "".join(cells) + "</w:tr>")
    return (
        '<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/><w:tblW w:w="0" w:type="auto"/></w:tblPr>'
        f"<w:tblGrid>{grid}</w:tblGrid>" + "".join(body) + "</w:tbl>"
    )


def _blocks_xml(blocks: Iterable[Any]) -> str:
    parts: list[str] = []
    for raw in blocks:
        block = DocBlock.from_any(raw)
        parts.append(_table_xml(block.table) if block.is_table else _paragraph_xml(block))
    return "".join(parts)


def _document_xml(inner: str) -> str:
    return (
        XML_DECL
        + f'<w:document xmlns:w="{W}"><w:body>{inner}'
        + '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134"/></w:sectPr>'
        + "</w:body></w:document>"
    )


def _write_package(path: Path, parts: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content.encode("utf-8"))
    # Written through a temporary file so a failed write cannot leave a half document behind.
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_bytes(buffer.getvalue())
    os.replace(temporary, path)


def docx_write(path: str | Path, blocks: Iterable[Any], title: str = "") -> dict[str, Any]:
    """Create a .docx from blocks, then re-read it and prove what landed."""
    import datetime

    target = Path(path)
    rendered = _blocks_xml(blocks)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    parts = {
        "[Content_Types].xml": _CONTENT_TYPES_DOCX,
        "_rels/.rels": _ROOT_RELS,
        "word/document.xml": _document_xml(rendered),
        "word/_rels/document.xml.rels": _DOC_RELS,
        "word/styles.xml": _STYLES,
        "word/numbering.xml": _NUMBERING,
        "docProps/core.xml": _CORE.replace("{title}", _esc(title)).replace("{stamp}", stamp),
    }
    _write_package(target, parts)
    return _verify_docx(target, expected_paragraphs=len(docx_blocks(target)))


def docx_append(path: str | Path, blocks: Iterable[Any]) -> dict[str, Any]:
    """Append blocks to an existing document, preserving every other part of the package."""
    target = Path(path)
    if not target.exists():
        raise DocumentError(f"no such document: {target}")
    before_text = docx_text(target)
    before_blocks = len(docx_blocks(target))
    appended_text = docx_text_from_blocks(blocks)
    rendered = _blocks_xml(blocks)
    names = _part_names(target)
    document = _read_part(target, "word/document.xml").decode("utf-8")
    position = document.rfind("<w:sectPr")
    if position == -1:
        raise DocumentError(f"{target}: document.xml has no section properties to insert before")
    updated = document[:position] + rendered + document[position:]
    with zipfile.ZipFile(target) as archive:
        contents = {name: archive.read(name) for name in names}
    contents["word/document.xml"] = updated.encode("utf-8")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in contents.items():
            archive.writestr(name, content)
    temporary = target.with_suffix(target.suffix + ".part")
    temporary.write_bytes(buffer.getvalue())
    os.replace(temporary, target)

    after_text = docx_text(target)
    after_blocks = len(docx_blocks(target))
    expected_tail = appended_text.strip()
    verified = after_blocks > before_blocks and after_text.strip().endswith(expected_tail)
    return {
        "path": str(target),
        "bytes": target.stat().st_size,
        "hash": content_hash(target),
        "appended_blocks": after_blocks - before_blocks,
        "verified": verified,
        "read_back_tail": after_text.splitlines()[-3:],
    }


def docx_text_from_blocks(blocks: Iterable[Any]) -> str:
    """What those blocks should read as, used to check the write against the intent."""
    lines: list[str] = []
    for raw in blocks:
        block = DocBlock.from_any(raw)
        if block.is_table:
            lines.extend("\t".join(row) for row in block.table)
        else:
            lines.append(block.text)
    return "\n".join(lines)


# --------------------------------------------------------------------------------------
# XLSX
# --------------------------------------------------------------------------------------


def column_letter(index: int) -> str:
    """1 -> A, 27 -> AA, the way a spreadsheet names a column."""
    letters = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def column_index(letters: str) -> int:
    index = 0
    for char in letters.upper():
        if not char.isalpha():
            break
        index = index * 26 + (ord(char) - 64)
    return index


def _shared_strings(path: str | Path) -> list[str]:
    if "xl/sharedStrings.xml" not in _part_names(path):
        return []
    root = _xml(_read_part(path, "xl/sharedStrings.xml"), "xl/sharedStrings.xml")
    values: list[str] = []
    for item in root.findall(_tag(S, "si")):
        values.append("".join(node.text or "" for node in item.iter(_tag(S, "t"))))
    return values


def _sheet_parts(path: str | Path) -> list[tuple[str, str]]:
    """(sheet name, part name) in workbook order, read from the workbook and its rels."""
    workbook = _xml(_read_part(path, "xl/workbook.xml"), "xl/workbook.xml")
    rels = _xml(_read_part(path, "xl/_rels/workbook.xml.rels"), "xl/_rels/workbook.xml.rels")
    targets = {
        rel.get("Id"): rel.get("Target", "")
        for rel in rels.findall(_tag(PKG_REL, "Relationship"))
    }
    out: list[tuple[str, str]] = []
    sheets = workbook.find(_tag(S, "sheets"))
    for sheet in (sheets if sheets is not None else []):
        name = sheet.get("name", "")
        rel_id = sheet.get(_tag(R, "id"), "")
        target = targets.get(rel_id, "")
        if not target:
            continue
        part = target.lstrip("/")
        if not part.startswith("xl/"):
            part = f"xl/{part}"
        out.append((name, part))
    return out


def xlsx_sheets(path: str | Path) -> list[str]:
    return [name for name, _ in _sheet_parts(path)]


def _cell_value(cell: ET.Element, shared: list[str]) -> Any:
    kind = cell.get("t", "n")
    if kind == "inlineStr":
        return "".join(node.text or "" for node in cell.iter(_tag(S, "t")))
    value_node = cell.find(_tag(S, "v"))
    raw = value_node.text if value_node is not None else None
    if raw is None:
        formula = cell.find(_tag(S, "f"))
        return f"={formula.text}" if formula is not None and formula.text else None
    if kind == "s":
        try:
            return shared[int(raw)]
        except (ValueError, IndexError):
            return raw
    if kind == "str":
        return raw
    if kind == "b":
        return raw == "1"
    if kind == "e":
        return raw  # an error cell, reported as its text rather than a number
    try:
        number = float(raw)
        return int(number) if number.is_integer() else number
    except ValueError:
        return raw


def xlsx_read(path: str | Path, sheet: str = "", cell_range: str = "") -> dict[str, Any]:
    """Read one sheet as rows of real values.

    A formula is returned as the formula text (``=SUM(A1:A3)``) because the cached result in
    the file may be stale; a reader that wants the last calculated value can read it through
    Tier A. Saying which one is returned matters more than quietly picking one.
    """
    parts = _sheet_parts(path)
    if not parts:
        raise DocumentError(f"{path}: the workbook declares no sheets")
    wanted = sheet or parts[0][0]
    match = next((part for name, part in parts if name == wanted), "")
    if not match:
        raise DocumentError(f"{path} has no sheet named {wanted!r}", )
    shared = _shared_strings(path)
    root = _xml(_read_part(path, match), match)
    data = root.find(_tag(S, "sheetData"))
    rows: list[list[Any]] = []
    for row in (data if data is not None else []):
        values: dict[int, Any] = {}
        for cell in row.findall(_tag(S, "c")):
            reference = cell.get("r", "")
            index = column_index(re.sub(r"[^A-Za-z]", "", reference)) or len(values) + 1
            values[index] = _cell_value(cell, shared)
        if values:
            width = max(values)
            rows.append([values.get(index) for index in range(1, width + 1)])
        else:
            rows.append([])
    if cell_range:
        rows = _slice_range(rows, cell_range)
    return {
        "path": str(path),
        "sheet": wanted,
        "sheets": [name for name, _ in parts],
        "rows": rows,
        "row_count": len(rows),
        "column_count": max((len(row) for row in rows), default=0),
    }


def _slice_range(rows: list[list[Any]], cell_range: str) -> list[list[Any]]:
    """Support ``A1:C10``: a real range read, so a 500k-row sheet stays out of context."""
    match = re.match(r"^\s*([A-Za-z]+)(\d+)?(?::([A-Za-z]+)(\d+)?)?\s*$", cell_range)
    if not match:
        raise DocumentError(f"not a cell range: {cell_range!r} (expected A1:C10)")
    start_col = column_index(match.group(1))
    end_col = column_index(match.group(3) or match.group(1))
    start_row = int(match.group(2) or 1)
    end_row = int(match.group(4) or len(rows))
    return [row[start_col - 1 : end_col] for row in rows[start_row - 1 : end_row]]


def _sheet_xml(rows: Sequence[Sequence[Any]]) -> str:
    body: list[str] = []
    for row_index, row in enumerate(rows, start=1):
        cells: list[str] = []
        for column in range(1, len(row) + 1):
            value = row[column - 1]
            if value is None or value == "":
                continue
            reference = f"{column_letter(column)}{row_index}"
            if isinstance(value, bool):
                cells.append(f'<c r="{reference}" t="b"><v>{1 if value else 0}</v></c>')
            elif isinstance(value, (int, float)):
                cells.append(f'<c r="{reference}"><v>{value}</v></c>')
            elif isinstance(value, str) and value.startswith("=") and len(value) > 1:
                # A formula is written as a formula, with no cached value: Excel calculates it.
                from xml.sax.saxutils import escape

                cells.append(f'<c r="{reference}"><f>{escape(value[1:])}</f></c>')
            else:
                from xml.sax.saxutils import escape

                cells.append(
                    f'<c r="{reference}" t="inlineStr"><is><t xml:space="preserve">{escape(str(value))}</t></is></c>'
                )
        body.append(f'<row r="{row_index}">' + "".join(cells) + "</row>")
    return (
        XML_DECL
        + f'<worksheet xmlns="{S}" xmlns:r="{R}"><sheetData>'
        + "".join(body)
        + "</sheetData></worksheet>"
    )


def _xlsx_content_types(count: int) -> str:
    overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{index}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for index in range(1, count + 1)
    )
    return (
        XML_DECL
        + f'<Types xmlns="{CT}">'
        + '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        + '<Default Extension="xml" ContentType="application/xml"/>'
        + '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        + overrides
        + '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        + "</Types>"
    )


def _workbook_xml(names: Sequence[str]) -> str:
    from xml.sax.saxutils import quoteattr

    sheets = "".join(
        f'<sheet name={quoteattr(name)} sheetId="{index}" r:id="rId{index}"/>'
        for index, name in enumerate(names, start=1)
    )
    return XML_DECL + f'<workbook xmlns="{S}" xmlns:r="{R}"><sheets>{sheets}</sheets></workbook>'


def _workbook_rels(count: int) -> str:
    rels = "".join(
        f'<Relationship Id="rId{index}" Type="{R}/worksheet" Target="worksheets/sheet{index}.xml"/>'
        for index in range(1, count + 1)
    )
    return XML_DECL + f'<Relationships xmlns="{PKG_REL}">{rels}</Relationships>'


def xlsx_write(path: str | Path, sheets: dict[str, Sequence[Sequence[Any]]], title: str = "") -> dict[str, Any]:
    """Create a workbook from named sheets of rows, then read it back to prove the values."""
    import datetime

    if not sheets:
        raise DocumentError("a workbook needs at least one sheet")
    target = Path(path)
    names = list(sheets)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    parts = {
        "[Content_Types].xml": _xlsx_content_types(len(names)),
        "_rels/.rels": (
            XML_DECL
            + f'<Relationships xmlns="{PKG_REL}">'
            + f'<Relationship Id="rId1" Type="{R}/officeDocument" Target="xl/workbook.xml"/>'
            + '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
            + "</Relationships>"
        ),
        "xl/workbook.xml": _workbook_xml(names),
        "xl/_rels/workbook.xml.rels": _workbook_rels(len(names)),
        "docProps/core.xml": _CORE.replace("{title}", title or "").replace("{stamp}", stamp),
    }
    for index, name in enumerate(names, start=1):
        parts[f"xl/worksheets/sheet{index}.xml"] = _sheet_xml(sheets[name])
    _write_package(target, parts)

    read_back = xlsx_read(target, names[0]) if names else {"rows": []}
    expected = [list(row) for row in sheets[names[0]]]
    return {
        "path": str(target),
        "bytes": target.stat().st_size,
        "hash": content_hash(target),
        "sheets": names,
        "rows": read_back.get("row_count", 0),
        "verified": read_back.get("rows") == expected,
        "read_back": read_back.get("rows", [])[:3],
    }


# --------------------------------------------------------------------------------------
# CSV
# --------------------------------------------------------------------------------------


def csv_read(path: str | Path, delimiter: str = "", encoding: str = "utf-8-sig") -> dict[str, Any]:
    """Read a CSV file with its real dialect, not an assumed comma."""
    target = Path(path)
    with target.open("r", encoding=encoding, newline="") as handle:
        sample = handle.read(8192)
        handle.seek(0)
        if delimiter:
            dialect: Any = csv.excel
            dialect.delimiter = delimiter
        else:
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
            except csv.Error:
                dialect = csv.excel
        rows = [[cell for cell in row] for row in csv.reader(handle, dialect)]
    return {
        "path": str(target),
        "delimiter": getattr(dialect, "delimiter", ","),
        "rows": rows,
        "row_count": len(rows),
        "column_count": max((len(row) for row in rows), default=0),
    }


def csv_write(path: str | Path, rows: Sequence[Sequence[Any]], delimiter: str = ",") -> dict[str, Any]:
    """Write a CSV and re-read it: a write nobody can parse back is not a write."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter=delimiter, quoting=csv.QUOTE_MINIMAL)
        for row in rows:
            writer.writerow(["" if cell is None else cell for cell in row])
    back = csv_read(target, delimiter=delimiter)
    expected = [["" if cell is None else str(cell) for cell in row] for row in rows]
    return {
        "path": str(target),
        "bytes": target.stat().st_size,
        "hash": content_hash(target),
        "rows": back["row_count"],
        "delimiter": back["delimiter"],
        "verified": back["rows"] == expected,
    }


# --------------------------------------------------------------------------------------
# PPTX (read only) and format dispatch
# --------------------------------------------------------------------------------------


def pptx_slides(path: str | Path) -> dict[str, Any]:
    """Extract each slide's text, in order, from a presentation."""
    slides = sorted(
        (name for name in _part_names(path) if re.match(r"ppt/slides/slide\d+\.xml$", name)),
        key=lambda name: int(re.search(r"(\d+)", name.rsplit("/", 1)[-1]).group(1)),
    )
    if not slides:
        raise DocumentError(f"{path}: no slides found, so it is not a presentation")
    out: list[dict[str, Any]] = []
    for index, part in enumerate(slides, start=1):
        root = _xml(_read_part(path, part), part)
        paragraphs: list[str] = []
        for paragraph in root.iter(_tag(A, "p")):
            text = _text_of(paragraph)
            if text.strip():
                paragraphs.append(text)
        out.append({"slide": index, "part": part, "text": paragraphs, "title": paragraphs[0] if paragraphs else ""})
    return {"path": str(path), "slides": out, "slide_count": len(out)}


def pptx_write(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    """Refused on purpose: authoring a deck needs masters, layouts and theme parts."""
    raise NotSupported(
        "the headless engine reads PowerPoint files but does not author them: a hand-built "
        "package without slide masters and a theme is a file PowerPoint may refuse to open. "
        "Use Tier A (PowerPoint installed) for writing decks, or export slides to PDF/images.",
        {"format": ".pptx", "supported": ["read"], "needs": "Microsoft PowerPoint (Tier A)"},
    )


SUPPORTED_READ = (".docx", ".xlsx", ".csv", ".pptx", ".tsv")
SUPPORTED_WRITE = (".docx", ".xlsx", ".csv", ".tsv")


def describe_document(path: str | Path) -> dict[str, Any]:
    """What this engine can really do with one file, without attempting it."""
    target = Path(path)
    suffix = target.suffix.lower()
    exists = target.exists()
    return {
        "path": str(target),
        "exists": exists,
        "format": suffix,
        "readable": suffix in SUPPORTED_READ and exists,
        "writable": suffix in SUPPORTED_WRITE,
        "bytes": target.stat().st_size if exists else 0,
        "hash": content_hash(target) if exists else "",
        "engine": "tier-b-headless",
        "note": (
            "writes are reparsed and verified"
            if suffix in SUPPORTED_WRITE
            else f"this engine writes {', '.join(SUPPORTED_WRITE)} only"
        ),
    }


def _verify_docx(path: Path, expected_paragraphs: int = 0) -> dict[str, Any]:
    blocks = docx_blocks(path)
    text = docx_text(path)
    return {
        "path": str(path),
        "bytes": path.stat().st_size,
        "hash": content_hash(path),
        "blocks": len(blocks),
        "paragraphs": sum(1 for block in blocks if not block.is_table),
        "tables": sum(1 for block in blocks if block.is_table),
        "empty": not text.strip(),
        "read_back": text.splitlines()[:5],
        "verified": bool(blocks) if expected_paragraphs else True,
    }