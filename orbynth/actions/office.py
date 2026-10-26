"""Office automation: Word, Excel, Access, PowerPoint and Outlook.

Strategy, and the honest reasoning behind it: the Office object models expose *thousands* of
members, and no hand-written list of named wrappers will ever cover "all operations". So
this driver gives the fleet two layers:

1. **Curated actions** for the things that are actually done constantly — create a document,
   write a range, run a report, export a PDF, run a macro. Fast, validated, easy for a model
   to call correctly.
2. **:func:`office_invoke`** — an arbitrary object-model passthrough: give an application, a
   dotted member path and arguments, and the driver walks the COM/AX graph and calls it. That
   is what makes the surface complete rather than merely large, and it is the layer the
   Artificer writes plugins against once a workflow repeats.

Platform notes: full COM automation is Windows (``pywin32``). On macOS the same actions
route through AppleScript/``appscript`` where available; both are reported honestly by
``orbynth doctor`` instead of failing mysteriously at call time.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import time
from typing import Any, Sequence

from .registry import Action, ActionContext, ActionFailed, DriverUnavailable, action

IS_WINDOWS = os.name == "nt"
IS_MAC = platform.system() == "Darwin"

#: COM ProgIDs (Windows) and AppleScript application names (macOS) per Office app.
OFFICE_APPS: dict[str, dict[str, str]] = {
    "word": {"progid": "Word.Application", "app": "Microsoft Word"},
    "excel": {"progid": "Excel.Application", "app": "Microsoft Excel"},
    "access": {"progid": "Access.Application", "app": "Microsoft Access"},
    "powerpoint": {"progid": "PowerPoint.Application", "app": "Microsoft PowerPoint"},
    "outlook": {"progid": "Outlook.Application", "app": "Microsoft Outlook"},
}


def _com(app: str, visible: bool = False) -> Any:
    """Attach to a running Office application or start one (Windows)."""
    if not IS_WINDOWS:
        raise DriverUnavailable(
            f"office.{app}",
            "Windows + pywin32 (COM)",
            "On macOS, Office automation is routed through AppleScript; enable it in the app's "
            "preferences, or run ORBYNTH on Windows for full object-model access.",
        )
    key = app.lower()
    if key not in OFFICE_APPS:
        raise ActionFailed(f"unknown office app {app!r}", {"known": sorted(OFFICE_APPS)})
    try:
        import win32com.client  # type: ignore
    except Exception as exc:  # pragma: no cover - Windows only
        raise DriverUnavailable("office.com", "pywin32", str(exc)) from exc
    progid = OFFICE_APPS[key]["progid"]
    try:  # pragma: no cover - Windows only
        application = win32com.client.GetActiveObject(progid)
    except Exception:  # pragma: no cover - Windows only
        application = win32com.client.Dispatch(progid)
    if visible:  # pragma: no cover
        try:
            application.Visible = True
        except Exception:
            pass
    return application


def _applescript(app: str, script: str) -> str:
    if not IS_MAC:  # pragma: no cover
        raise ActionFailed("AppleScript Office automation is macOS-only")
    name = OFFICE_APPS.get(app.lower(), {}).get("app", app)
    result = subprocess.run(["osascript", "-e", f'tell application "{name}"\n{script}\nend tell'], capture_output=True, text=True)
    if result.returncode != 0:
        raise ActionFailed(f"AppleScript failed: {result.stderr.strip()[:300]}", {"script": script[:200]})
    return result.stdout.strip()


def _resolve_member(root: Any, path: str) -> tuple[Any, str]:
    """Walk a dotted member path, e.g. ``ActiveDocument.Tables``."""
    parts = [p for p in path.split(".") if p]
    if not parts:
        raise ActionFailed("member path is empty")
    current = root
    for part in parts[:-1]:
        current = getattr(current, part)
    return current, parts[-1]


def _call_member(root: Any, path: str, args: Sequence[Any] = ()) -> Any:
    parent, leaf = _resolve_member(root, path)
    member = getattr(parent, leaf)
    if callable(member):
        return member(*args)
    if args:
        try:
            setattr(parent, leaf, args[0])
        except Exception as exc:
            raise ActionFailed(f"cannot set {path}: {exc}") from exc
    return member


# --------------------------------------------------------------------------------------
# Curated actions
# --------------------------------------------------------------------------------------


@action(
    name="office_word",
    description=(
        "Drive Microsoft Word. operation: new | open | write | append | find_replace | "
        "insert_table | save_as | export_pdf | print | close | read_text | macro."
    ),
    capability="office.word",
    group="office",
    parameters={
        "type": "object",
        "properties": {
            "operation": {"type": "string"},
            "path": {"type": "string", "description": "Document path for open/save_as/export."},
            "text": {"type": "string"},
            "find": {"type": "string"},
            "replace": {"type": "string"},
            "rows": {"type": "integer"},
            "columns": {"type": "integer"},
            "macro": {"type": "string"},
            "visible": {"type": "boolean"},
        },
        "required": ["operation"],
    },
)
def office_word(
    ctx: ActionContext,
    operation: str,
    path: str = "",
    text: str = "",
    find: str = "",
    replace: str = "",
    rows: int = 0,
    columns: int = 0,
    macro: str = "",
    visible: bool = False,
) -> Any:
    if IS_MAC:  # pragma: no cover - macOS only
        if operation == "write":
            _applescript("word", f'set content of active document to "{text}"')
            return {"operation": operation, "via": "applescript"}
    application = _com("word", visible=visible)
    if operation == "new":
        application.Documents.Add()
    elif operation == "open":
        if not path:
            raise ActionFailed("path is required for open")
        application.Documents.Open(os.path.abspath(os.path.expanduser(path)))
    elif operation in {"write", "append"}:
        document = _active_document(application)
        if operation == "write":
            document.Content.Text = text
        else:
            document.Content.InsertAfter(text)
    elif operation == "find_replace":
        found = application.Selection.Find
        found.ClearFormatting()
        found.Replacement.ClearFormatting()
        found.Text = find
        found.Replacement.Text = replace
        found.Execute(Replace=2)  # wdReplaceAll
    elif operation == "insert_table":
        if rows <= 0 or columns <= 0:
            raise ActionFailed("rows and columns are required for insert_table")
        application.ActiveDocument.Tables.Add(application.Selection.Range, rows, columns)
    elif operation in {"save_as", "export_pdf"}:
        document = _active_document(application)
        target = os.path.abspath(os.path.expanduser(path or document.FullName))
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        if operation == "export_pdf" or target.lower().endswith(".pdf"):
            document.ExportAsFixedFormat(target, 17)  # wdExportFormatPDF
        else:
            document.SaveAs2(target)
        return {"operation": operation, "saved_to": target, "exists": os.path.exists(target)}
    elif operation == "print":
        _active_document(application).PrintOut()
    elif operation == "close":
        if application.Documents.Count:
            application.ActiveDocument.Close(SaveChanges=0)
        if application.Documents.Count == 0:
            application.Quit(SaveChanges=0)
    elif operation == "read_text":
        return {"text": _active_document(application).Content.Text[:200_000]}
    elif operation == "macro":
        if not macro:
            raise ActionFailed("macro name is required")
        application.Run(macro)
    else:
        raise ActionFailed(f"unknown word operation {operation!r}")
    return {"operation": operation, "documents_open": int(application.Documents.Count)}


def _active_document(application: Any) -> Any:
    try:
        return application.ActiveDocument
    except Exception as exc:  # pragma: no cover - Windows only
        raise ActionFailed(f"no active document: {exc}") from exc


@action(
    name="office_excel",
    description=(
        "Drive Microsoft Excel. operation: new | open | set_cell | set_range | get_range | "
        "formula | add_sheet | list_sheets | autofilter | save_as | export_pdf | print | "
        "calculate | macro | close."
    ),
    capability="office.excel",
    group="office",
    parameters={
        "type": "object",
        "properties": {
            "operation": {"type": "string"},
            "path": {"type": "string"},
            "sheet": {"type": "string"},
            "cell": {"type": "string", "description": "e.g. B7"},
            "range": {"type": "string", "description": "e.g. A1:D40"},
            "values": {"type": "array", "items": {"type": "array"}, "description": "2D array for set_range."},
            "value": {"type": "string"},
            "formula": {"type": "string"},
            "macro": {"type": "string"},
            "visible": {"type": "boolean"},
        },
        "required": ["operation"],
    },
)
def office_excel(
    ctx: ActionContext,
    operation: str,
    path: str = "",
    sheet: str = "",
    cell: str = "",
    range: str = "",  # noqa: A002 - matches the tool schema
    values: list[list[Any]] | None = None,
    value: str = "",
    formula: str = "",
    macro: str = "",
    visible: bool = False,
) -> Any:
    application = _com("excel", visible=visible)
    workbook = None
    if operation == "new":
        workbook = application.Workbooks.Add()
    elif operation == "open":
        if not path:
            raise ActionFailed("path is required for open")
        workbook = application.Workbooks.Open(os.path.abspath(os.path.expanduser(path)))
    elif operation == "macro":
        if not macro:
            raise ActionFailed("macro name is required")
        application.Run(macro)
        return {"operation": operation, "macro": macro}
    else:
        try:
            workbook = application.ActiveWorkbook
        except Exception as exc:
            raise ActionFailed(f"no active workbook: {exc}") from exc

    worksheet = workbook.Worksheets(sheet) if sheet else workbook.ActiveSheet
    if operation == "set_cell":
        if not cell:
            raise ActionFailed("cell is required for set_cell")
        worksheet.Range(cell).Value = value
        return {"operation": operation, "cell": cell, "value": value, "read_back": worksheet.Range(cell).Value}
    if operation == "set_range":
        if not (range and values):
            raise ActionFailed("range and values are required for set_range")
        worksheet.Range(range).Value = values
        return {"operation": operation, "range": range, "rows": len(values), "columns": len(values[0]) if values else 0}
    if operation == "get_range":
        target = worksheet.Range(range or "A1:Z200").Value
        rows = [list(row) for row in target] if isinstance(target, tuple) else [[target]]
        return {"operation": operation, "range": range or "A1:Z200", "rows": rows}
    if operation == "formula":
        if not (cell and formula):
            raise ActionFailed("cell and formula are required")
        worksheet.Range(cell).Formula = formula
        return {"operation": operation, "cell": cell, "result": worksheet.Range(cell).Value}
    if operation == "add_sheet":
        created = workbook.Worksheets.Add()
        if sheet:
            created.Name = sheet
        return {"operation": operation, "sheet": created.Name}
    if operation == "list_sheets":
        return {"operation": operation, "sheets": [sheet_.Name for sheet_ in workbook.Worksheets]}
    if operation == "autofilter":
        worksheet.Range(range or "A1").AutoFilter()
        return {"operation": operation}
    if operation == "calculate":
        application.CalculateFull()
        return {"operation": operation}
    if operation in {"save_as", "export_pdf"}:
        target = os.path.abspath(os.path.expanduser(path or workbook.FullName))
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        if operation == "export_pdf" or target.lower().endswith(".pdf"):
            workbook.ExportAsFixedFormat(0, target)  # xlTypePDF
        else:
            workbook.SaveAs(target)
        return {"operation": operation, "saved_to": target, "exists": os.path.exists(target)}
    if operation == "print":
        workbook.PrintOut()
        return {"operation": operation}
    if operation == "close":
        workbook.Close(SaveChanges=value.lower() in {"true", "1", "yes"} if value else False)
        if int(application.Workbooks.Count) == 0:
            application.Quit()
        return {"operation": operation}
    raise ActionFailed(f"unknown excel operation {operation!r}")


@action(
    name="office_access",
    description=(
        "Drive Microsoft Access: open a database, list tables, run SQL, export a query to "
        "Excel/CSV/PDF, compact & repair, run macros."
    ),
    capability="office.access",
    group="office",
    parameters={
        "type": "object",
        "properties": {
            "operation": {"type": "string", "enum": ["open_db", "list_tables", "run_query", "export_query", "compact", "macro", "close"]},
            "path": {"type": "string"},
            "sql": {"type": "string"},
            "query": {"type": "string"},
            "export_path": {"type": "string"},
            "export_format": {"type": "string", "enum": ["xlsx", "csv", "pdf", "txt"]},
            "macro": {"type": "string"},
        },
        "required": ["operation"],
    },
)
def office_access(
    ctx: ActionContext,
    operation: str,
    path: str = "",
    sql: str = "",
    query: str = "",
    export_path: str = "",
    export_format: str = "xlsx",
    macro: str = "",
) -> Any:
    application = _com("access")
    if operation in {"open_db", "compact"}:
        if not path:
            raise ActionFailed("path is required")
        database = os.path.abspath(os.path.expanduser(path))
        if operation == "compact":
            application.CompactRepair(database, database)
            return {"operation": operation, "path": database}
        application.OpenCurrentDatabase(database)
        return {"operation": operation, "path": database, "tables": _access_tables(application)}
    if operation == "list_tables":
        return {"operation": operation, "tables": _access_tables(application)}
    if operation == "run_query":
        if not sql:
            raise ActionFailed("sql is required")
        recordset = application.CurrentDb().OpenRecordset(sql)
        rows: list[list[Any]] = []
        while not recordset.EOF and len(rows) < 5000:
            rows.append([recordset.Fields(i).Value for i in range(recordset.Fields.Count)])
            recordset.MoveNext()
        return {"operation": operation, "rows": rows, "truncated": len(rows) >= 5000}
    if operation == "export_query":
        if not (query and export_path):
            raise ActionFailed("query and export_path are required")
        target = os.path.abspath(os.path.expanduser(export_path))
        formats = {"xlsx": 8, "csv": 3, "txt": 2, "pdf": 17}  # AcExportExcel/CSV/TXT/PDF
        application.DoCmd.OutputTo(0, query, f"*{formats.get(export_format, 8)}", target, False)
        return {"operation": operation, "exported_to": target, "exists": os.path.exists(target)}
    if operation == "macro":
        application.DoCmd.RunMacro(macro)
        return {"operation": operation, "macro": macro}
    if operation == "close":
        application.CloseCurrentDatabase()
        return {"operation": operation}
    raise ActionFailed(f"unknown access operation {operation!r}")


def _access_tables(application: Any) -> list[str]:
    names: list[str] = []
    try:
        for index in range(application.CurrentData.AllTables.Count):
            names.append(application.CurrentData.AllTables(index).Name)
    except Exception:
        pass
    return names


@action(
    name="office_powerpoint",
    description="Drive PowerPoint: new | open | add_slide | set_text | save_as | export_pdf | print | close.",
    capability="office.powerpoint",
    group="office",
    parameters={
        "type": "object",
        "properties": {
            "operation": {"type": "string"},
            "path": {"type": "string"},
            "text": {"type": "string"},
            "layout": {"type": "integer"},
            "visible": {"type": "boolean"},
        },
        "required": ["operation"],
    },
)
def office_powerpoint(
    ctx: ActionContext, operation: str, path: str = "", text: str = "", layout: int = 2, visible: bool = False
) -> Any:
    application = _com("powerpoint", visible=visible)
    if operation == "new":
        application.Presentations.Add()
    elif operation == "open":
        if not path:
            raise ActionFailed("path is required for open")
        application.Presentations.Open(os.path.abspath(os.path.expanduser(path)))
    elif operation == "add_slide":
        presentation = application.ActivePresentation
        slide = presentation.Slides.Add(presentation.Slides.Count + 1, layout)
        if text and slide.Shapes.Count:
            slide.Shapes(1).TextFrame.TextRange.Text = text
    elif operation == "set_text":
        slide = application.ActiveWindow.View.Slide
        if not slide.Shapes.Count:
            raise ActionFailed("the slide has no shapes to write into")
        slide.Shapes(1).TextFrame.TextRange.Text = text
    elif operation in {"save_as", "export_pdf"}:
        presentation = application.ActivePresentation
        target = os.path.abspath(os.path.expanduser(path or presentation.FullName))
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        if operation == "export_pdf" or target.lower().endswith(".pdf"):
            presentation.ExportAsFixedFormat(target, 2)  # ppFixedFormatTypePDF
        else:
            presentation.SaveAs(target)
        return {"operation": operation, "saved_to": target, "exists": os.path.exists(target)}
    elif operation == "print":
        application.ActivePresentation.PrintOut()
    elif operation == "close":
        application.ActivePresentation.Close()
    else:
        raise ActionFailed(f"unknown powerpoint operation {operation!r}")
    return {"operation": operation, "slides": int(application.ActivePresentation.Slides.Count)}


@action(
    name="office_invoke",
    description=(
        "Call any member of an Office object model directly (the completeness escape hatch). "
        "Example: app='excel', member='ActiveWorkbook.Worksheets.Count'; app='word', "
        "member='ActiveDocument.Tables.Add', args=[range_obj, 3, 4]. Provide the app, a dotted "
        "member path and positional args. Use this when no curated action fits."
    ),
    capability="office.word",
    group="office",
    danger="medium",
    parameters={
        "type": "object",
        "properties": {
            "app": {"type": "string", "enum": ["word", "excel", "access", "powerpoint", "outlook"]},
            "member": {"type": "string", "description": "Dotted path, e.g. ActiveDocument.Content"},
            "args": {"type": "array", "description": "Positional arguments for a callable member."},
            "assign": {"description": "Value to assign when the member is a property."},
            "visible": {"type": "boolean"},
        },
        "required": ["app", "member"],
    },
)
def office_invoke(
    ctx: ActionContext,
    app: str,
    member: str,
    args: Sequence[Any] | None = None,
    assign: Any = None,
    visible: bool = False,
) -> Any:
    capability_by_app = {
        "word": "office.word",
        "excel": "office.excel",
        "access": "office.access",
        "powerpoint": "office.powerpoint",
        "outlook": "network.http",
    }
    if capability_by_app.get(app.lower()) not in ctx.capabilities:
        raise ActionFailed(f"office_invoke on {app} requires {capability_by_app.get(app.lower())}")
    application = _com(app, visible=visible)
    call_args = list(args or [])
    if assign is not None:
        call_args.append(assign)
    result = _call_member(application, member, call_args)
    try:
        json.dumps(result)
        payload: Any = result
    except (TypeError, ValueError):
        payload = repr(result)
    return {"app": app, "member": member, "result": payload}


@action(
    name="office_macro",
    description=(
        "Run a VBA macro in any Office application. Highest-risk capability: a macro can do "
        "anything the user can, so it is confirm-gated and audited."
    ),
    capability="office.macro",
    group="office",
    danger="high",
    parameters={
        "type": "object",
        "properties": {
            "app": {"type": "string", "enum": ["word", "excel", "access", "powerpoint", "outlook"]},
            "macro": {"type": "string"},
            "args": {"type": "array"},
        },
        "required": ["app", "macro"],
    },
)
def office_macro(ctx: ActionContext, app: str, macro: str, args: Sequence[Any] | None = None) -> Any:
    application = _com(app)
    application.Run(macro, *(args or []))
    return {"app": app, "macro": macro, "args": list(args or [])}


@action(
    name="office_status",
    description="Report which Office applications are installed/running and whether automation is available.",
    capability="office.word",
    group="office",
    parameters={"type": "object", "properties": {}},
)
def office_status(ctx: ActionContext) -> Any:
    rows: list[dict[str, Any]] = []
    for key, meta in OFFICE_APPS.items():
        entry: dict[str, Any] = {"app": key, "progid": meta["progid"], "automation": "unavailable"}
        if IS_WINDOWS:
            try:  # pragma: no cover - Windows only
                import win32com.client  # type: ignore

                win32com.client.GetActiveObject(meta["progid"])
                entry["automation"] = "running"
            except Exception:
                try:  # pragma: no cover
                    import win32com.client  # type: ignore

                    win32com.client.Dispatch(meta["progid"])
                    entry["automation"] = "installed"
                except Exception:
                    entry["automation"] = "not-installed"
        elif IS_MAC:
            probe = subprocess.run(["osascript", "-e", f'id of application "{meta["app"]}"'], capture_output=True, text=True)
            entry["automation"] = "applescript" if probe.returncode == 0 else "not-installed"
        rows.append(entry)
    return {"platform": platform.system(), "apps": rows}


all_actions = lambda: [  # noqa: E731
    office_word,
    office_excel,
    office_access,
    office_powerpoint,
    office_invoke,
    office_macro,
    office_status,
]
