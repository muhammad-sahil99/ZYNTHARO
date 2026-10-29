"""Workflow 3: the recurring report.

Small businesses do not need a dashboard; they need the file that goes to their accountant, their
partner or their bank. This workflow reads the ledger written by invoice intake and produces a
Word summary plus an Excel breakdown, then re-reads both to prove the numbers are in them.

No Office installation is required: this writes real ``.docx``/``.xlsx`` packages itself. If
Office *is* installed, the files open normally in it.
"""

from __future__ import annotations

import csv
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..ops import docengine
from .base import DONE, FAILED, OK, PARTIAL, Workflow, WorkflowError, WorkflowResult

LEDGER_COLUMNS = ["sha256", "source", "filed_to", "vendor", "amount", "currency", "invoice_date", "filed_at"]


def _read_rows(path: Path) -> tuple[list[str], list[dict[str, Any]]]:
    if not path.exists():
        raise WorkflowError(f"the ledger {path} does not exist yet; run invoice intake first")
    if path.suffix.lower() == ".csv":
        data = docengine.csv_read(path)
        raw = data.get("rows", [])
    else:
        data = docengine.xlsx_read(path)
        raw = data.get("rows", [])
    if not raw:
        raise WorkflowError(f"{path} is empty")
    header = [str(cell or "").strip() for cell in raw[0]]
    missing = [name for name in ("vendor", "amount", "currency") if name not in header]
    if missing:
        raise WorkflowError(
            f"{path} is missing the column(s) {', '.join(missing)}; this workflow expects the ZYNTHARO ledger "
            "written by invoice intake"
        )
    rows: list[dict[str, Any]] = []
    for line in raw[1:]:
        row = {header[index]: (line[index] if index < len(line) else "") for index in range(len(header))}
        try:
            row["amount"] = float(row.get("amount") or 0)
        except (TypeError, ValueError):
            row["amount"] = 0.0
        rows.append(row)
    return header, rows


def _in_window(row: dict[str, Any], cutoff: datetime | None) -> bool:
    if cutoff is None:
        return True
    raw = str(row.get("invoice_date") or row.get("filed_at") or "")
    try:
        day = datetime.fromisoformat(raw[:10])
    except ValueError:
        return True  # an unparseable date stays in the report rather than silently disappearing
    return day >= cutoff


class DailyReport(Workflow):
    name = "daily_report"
    title = "Recurring report"
    description = (
        "Turn the ledger into a Word summary and an Excel breakdown, with both files read back to "
        "prove the totals are in them. No Office installation required."
    )
    needs = "The ledger workbook produced by invoice intake, and a folder for the report."

    def run(
        self,
        ledger: str,
        out_dir: str,
        days: int = 0,
        title: str = "Invoice summary",
        **_: Any,
    ) -> WorkflowResult:
        result = WorkflowResult(self.name, FAILED, "")
        ledger_path, out = Path(ledger).expanduser(), Path(out_dir).expanduser()
        header, rows = _read_rows(ledger_path)
        cutoff = datetime.now() - timedelta(days=int(days)) if int(days or 0) > 0 else None
        selected = [row for row in rows if _in_window(row, cutoff)]
        result.step(
            "read ledger",
            OK,
            f"{len(selected)} of {len(rows)} row(s) included"
            + (f" (last {days} days)" if cutoff else " (all time)"),
        )
        if not selected:
            return result.finish(PARTIAL, "the ledger has no rows to report on yet")

        totals: dict[str, float] = defaultdict(float)
        counts: Counter[str] = Counter()
        for row in selected:
            currency = str(row.get("currency") or "USD").upper()
            totals[currency] += float(row.get("amount") or 0)
            counts[str(row.get("vendor") or "unknown")] += 1
        total_line = "; ".join(f"{currency} {value:,.2f}" for currency, value in sorted(totals.items()))
        top = counts.most_common(10)
        dates = sorted(str(row.get("invoice_date") or "")[:10] for row in selected if row.get("invoice_date"))

        out.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d")
        xlsx_path = out / f"{stamp}_{self.name}.xlsx"
        docx_path = out / f"{stamp}_{self.name}.docx"

        breakdown = [["currency", "invoices", "total", "average"]]
        for currency, value in sorted(totals.items()):
            number = sum(1 for row in selected if str(row.get("currency") or "USD").upper() == currency)
            breakdown.append([currency, number, round(value, 2), round(value / number, 2) if number else 0])
        vendor_rows = [["vendor", "invoices"]] + [[vendor, number] for vendor, number in top]
        try:
            workbook = docengine.xlsx_write(
                xlsx_path,
                {"summary": [["count", "period", "total", "earliest", "latest"],
                             [len(selected), f"last {days} days" if cutoff else "all time", total_line,
                              dates[0] if dates else "", dates[-1] if dates else ""]],
                 "by_currency": breakdown,
                 "by_vendor": vendor_rows},
                title=title,
            )
        except (docengine.DocumentError, OSError) as exc:
            result.step("write workbook", "skipped", f"{exc}")
            return result.finish(FAILED, f"the Excel report could not be written: {exc}")
        if not workbook.get("verified"):
            result.step("write workbook", "skipped", "the workbook did not read back its own values")
            return result.finish(FAILED, "the Excel report failed verification")
        result.step("write workbook", OK, f"{xlsx_path.name} ({workbook['rows']} rows)", [str(xlsx_path)])

        blocks: list[dict[str, Any]] = [
            {"text": title, "style": "Title"},
            {"text": f"Generated {datetime.now().strftime('%d %B %Y, %H:%M')}", "style": "Normal"},
            {"text": f"{len(selected)} invoice(s)", "style": "Heading 1"},
            {"text": f"Total: {total_line}" if total_line else "Total: no amounts recorded", "style": "Normal"},
        ]
        if dates:
            blocks.append({"text": f"Period covered: {dates[0]} to {dates[-1]}", "style": "Normal"})
        blocks.append({"text": "By currency", "style": "Heading 1"})
        blocks.append({"table": [["Currency", "Invoices", "Total", "Average"]] + [[str(c) for c in row] for row in breakdown[1:]]})
        blocks.append({"text": "Top vendors", "style": "Heading 1"})
        blocks.append({"table": [["Vendor", "Invoices"]] + [[str(v), str(n)] for v, n in top]})
        blocks.append({"text": "Prepared by ZYNTHARO from the invoice ledger; figures are read from the ledger rows above.", "style": "Normal"})
        try:
            document = docengine.docx_write(docx_path, blocks, title=title)
            text = docengine.docx_text(docx_path)
        except (docengine.DocumentError, OSError) as exc:
            result.step("write document", "skipped", f"{exc}")
            return result.finish(PARTIAL, f"the Excel report is ready but the Word report failed: {exc}")
        if total_line and total_line.split(";")[0] not in text:
            result.step("write document", "skipped", "the totals did not read back from the document")
            return result.finish(PARTIAL, "the Word report failed verification of its totals")
        result.step("write document", OK, f"{docx_path.name} ({len(text)} characters)", [str(docx_path)])

        result.counts = {"invoices": len(selected), "currencies": len(totals), "vendors": len(counts)}
        if not document.get("verified"):
            result.problems.append("the Word report read back fewer paragraphs than it wrote")
        return result.finish(DONE, f"{len(selected)} invoice(s) summarised: {total_line}")