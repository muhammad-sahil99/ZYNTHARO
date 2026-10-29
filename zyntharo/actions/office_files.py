"""Tier B document actions (spec 9.7): Word and Excel files without Word or Excel.

Tier A (``office.py``) drives an installed Office through COM and is the richer path. These
actions work on the file itself, so they run on a machine with no Office, in a container, and
without taking over the user's open windows. Each write is reparsed and compared before it is
reported, so ``verified`` on the result means the text is really in the file.

``.pptx`` is read-only here, and says so. Authoring a presentation needs slide masters, a
layout and a theme; a hand-built package that PowerPoint might refuse would be a feature that
looks implemented and is not, which is the exact failure mode this project bans.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .registry import ActionContext, ActionFailed, action
from ..ops import docengine


def _require(path: str, *, write: bool = False) -> Path:
    if not path.strip():
        raise ActionFailed("a path is required")
    target = Path(path).expanduser()
    if write:
        if not target.suffix:
            raise ActionFailed(f"refusing to write without a file extension: {target}")
        if target.suffix.lower() not in docengine.SUPPORTED_WRITE:
            raise ActionFailed(
                f"the headless engine does not write {target.suffix} files",
                {"supported": list(docengine.SUPPORTED_WRITE), "needs": "Tier A / Office installed"},
            )
    elif not target.exists():
        raise ActionFailed(f"no such file: {target}")
    return target


def _wrap(payload: dict[str, Any], *, verified: bool, evidence: dict[str, Any]) -> Any:
    from .registry import ActionResult

    result = ActionResult.success(payload, evidence=evidence)
    result.verified = verified
    return result


@action(
    name="doc_info",
    description=(
        "Report what the headless document engine can do with one file: format, size, hash, "
        "whether it is readable and writable here. Never attempts the operation."
    ),
    capability="filesystem.read",
    group="office",
    parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
)
def doc_info(ctx: ActionContext, path: str) -> Any:
    return docengine.describe_document(Path(path).expanduser())


@action(
    name="docx_read",
    description="Read a .docx into paragraphs (with styles), tables and its visible text. No Word needed.",
    capability="office.word",
    group="office",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "max_blocks": {"type": "integer", "description": "Stop after this many blocks (default all)."},
        },
        "required": ["path"],
    },
)
def docx_read(ctx: ActionContext, path: str, max_blocks: int = 0) -> Any:
    target = _require(path)
    try:
        blocks = docengine.docx_blocks(target)
    except docengine.DocumentError as exc:
        raise ActionFailed(str(exc)) from exc
    shown = blocks[:max_blocks] if max_blocks else blocks
    return {
        "path": str(target),
        "hash": docengine.content_hash(target),
        "blocks": [
            {"text": block.text, "style": block.style, "table": block.table, "level": block.level,
             "is_table": block.is_table}
            for block in shown
        ],
        "block_count": len(blocks),
        "text": docengine.docx_text(target),
    }


@action(
    name="docx_write",
    description=(
        "Create a .docx from blocks (text, headings, lists, tables) and verify it by reading it "
        "back. No Word needed."
    ),
    capability="office.word",
    group="office",
    danger="medium",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "blocks": {"type": "array", "items": {"type": "object"}, "description": "Paragraph or table blocks."},
            "title": {"type": "string"},
        },
        "required": ["path", "blocks"],
    },
)
def docx_write(ctx: ActionContext, path: str, blocks: list[dict[str, Any]], title: str = "") -> Any:
    target = _require(path, write=True)
    try:
        info = docengine.docx_write(target, blocks, title=title)
    except docengine.DocumentError as exc:
        raise ActionFailed(str(exc)) from exc
    return _wrap(
        info,
        verified=bool(info.get("verified")),
        evidence={
            "path": str(target),
            "hash": info.get("hash", ""),
            "target": str(target),
            "reparsed": bool(info.get("verified")),
            "paragraphs": info.get("paragraphs", 0),
        },
    )


@action(
    name="docx_append",
    description="Append blocks to an existing .docx, preserving everything already in the package.",
    capability="office.word",
    group="office",
    danger="medium",
    parameters={
        "type": "object",
        "properties": {"path": {"type": "string"}, "blocks": {"type": "array", "items": {"type": "object"}}},
        "required": ["path", "blocks"],
    },
)
def docx_append(ctx: ActionContext, path: str, blocks: list[dict[str, Any]]) -> Any:
    target = _require(path)
    try:
        info = docengine.docx_append(target, blocks)
    except docengine.DocumentError as exc:
        raise ActionFailed(str(exc)) from exc
    return _wrap(
        info,
        verified=bool(info.get("verified")),
        evidence={
            "path": str(target),
            "hash": info.get("hash", ""),
            "target": str(target),
            "reparsed": bool(info.get("verified")),
        },
    )


@action(
    name="xlsx_read",
    description="Read a sheet from a .xlsx (formulas as written, values typed) with an optional A1:C10 range.",
    capability="office.excel",
    group="office",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "sheet": {"type": "string", "description": "Sheet name; default the first sheet."},
            "range": {"type": "string", "description": "Cell range such as A1:C10."},
        },
        "required": ["path"],
    },
)
def xlsx_read(ctx: ActionContext, path: str, sheet: str = "", range: str = "") -> Any:  # noqa: A002
    target = _require(path)
    try:
        return docengine.xlsx_read(target, sheet=sheet, cell_range=range)
    except docengine.DocumentError as exc:
        raise ActionFailed(str(exc)) from exc


@action(
    name="xlsx_write",
    description=(
        "Create a .xlsx workbook from named sheets of rows (strings, numbers, booleans and "
        "=formulas) and verify every value by reading it back. No Excel needed."
    ),
    capability="office.excel",
    group="office",
    danger="medium",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "sheets": {
                "type": "object",
                "description": "Sheet name -> rows of values.",
            },
            "title": {"type": "string"},
        },
        "required": ["path", "sheets"],
    },
)
def xlsx_write(ctx: ActionContext, path: str, sheets: dict[str, list[list[Any]]], title: str = "") -> Any:
    target = _require(path, write=True)
    try:
        info = docengine.xlsx_write(target, sheets, title=title)
    except docengine.DocumentError as exc:
        raise ActionFailed(str(exc)) from exc
    return _wrap(
        info,
        verified=bool(info.get("verified")),
        evidence={
            "path": str(target),
            "hash": info.get("hash", ""),
            "target": str(target),
            "reparsed": bool(info.get("verified")),
            "sheets": info.get("sheets", []),
        },
    )


@action(
    name="csv_read",
    description="Read a CSV or TSV file with its real delimiter, detected from the content.",
    capability="office.excel",
    group="office",
    parameters={"type": "object", "properties": {"path": {"type": "string"}, "delimiter": {"type": "string"}}, "required": ["path"]},
)
def csv_read(ctx: ActionContext, path: str, delimiter: str = "") -> Any:
    target = _require(path)
    try:
        return docengine.csv_read(target, delimiter=delimiter)
    except (docengine.DocumentError, UnicodeDecodeError) as exc:
        raise ActionFailed(f"{target} could not be read as CSV: {exc}") from exc


@action(
    name="csv_write",
    description="Write rows to a CSV/TSV file and re-read it to verify the values survived.",
    capability="office.excel",
    group="office",
    danger="medium",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "rows": {"type": "array", "items": {"type": "array"}},
            "delimiter": {"type": "string", "description": "Default is a comma; use \\t for TSV."},
        },
        "required": ["path", "rows"],
    },
)
def csv_write(ctx: ActionContext, path: str, rows: list[list[Any]], delimiter: str = ",") -> Any:
    target = _require(path, write=True)
    info = docengine.csv_write(target, rows, delimiter=delimiter)
    return _wrap(
        info,
        verified=bool(info.get("verified")),
        evidence={"path": str(target), "hash": info.get("hash", ""), "target": str(target), "reparsed": bool(info.get("verified"))},
    )


@action(
    name="pptx_read",
    description="Read the text of every slide in a .pptx, in slide order. No PowerPoint needed.",
    capability="office.powerpoint",
    group="office",
    parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
)
def pptx_read(ctx: ActionContext, path: str) -> Any:
    target = _require(path)
    try:
        return docengine.pptx_slides(target)
    except docengine.DocumentError as exc:
        raise ActionFailed(str(exc)) from exc


@action(
    name="pptx_write",
    description=(
        "Not available: reading presentations works without PowerPoint, authoring them does not. "
        "Refused with the reason rather than writing a file PowerPoint may reject."
    ),
    capability="office.powerpoint",
    group="office",
    parameters={"type": "object", "properties": {"path": {"type": "string"}}},
)
def pptx_write(ctx: ActionContext, path: str = "") -> Any:
    try:
        return docengine.pptx_write(path)
    except docengine.NotSupported as exc:
        raise ActionFailed(str(exc), exc.evidence) from exc


all_actions = lambda: [  # noqa: E731
    doc_info,
    docx_read,
    docx_write,
    docx_append,
    xlsx_read,
    xlsx_write,
    csv_read,
    csv_write,
    pptx_read,
    pptx_write,
]