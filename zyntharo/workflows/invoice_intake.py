"""Workflow 1: invoice intake.

The customer points this at a folder where invoices land (downloads, a scanner folder, an email
attachment folder). Every new file is classified, dated, named predictably, filed by quarter and
recorded once in a ledger workbook. Re-running it is safe: files already in the ledger are
skipped by content hash, so a half-finished run costs nothing.

Honest boundaries, stated in the result rather than hidden:

* a ``.pdf`` needs ``pypdf``; without it the file is skipped with that instruction
* an image invoice needs an OCR engine; without one it is skipped with that instruction
* a date written ``03/04/2026`` is day-first here (the common non-US convention) and the step
  says so, because guessing silently would misfile a month
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..ops import docengine
from .base import DONE, FAILED, OK, PARTIAL, SKIPPED, Workflow, WorkflowError, WorkflowResult

SUPPORTED = {".txt", ".md", ".csv", ".docx", ".pdf"}

CURRENCY_PATTERN = re.compile(
    r"(?:(?P<code>USD|EUR|GBP|PKR|INR|AED|SAR|CAD|AUD)\s*)?(?P<symbol>[$€£₨₹]|Rs\.?)?\s*"
    r"(?P<amount>\d{1,3}(?:[,\s]\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)",
    re.IGNORECASE,
)
TOTAL_HINT = re.compile(r"(total|amount\s*due|balance|grand\s*total|invoice\s*total)", re.IGNORECASE)
ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
NUMERIC_DATE = re.compile(r"\b(\d{1,2})[/.](\d{1,2})[/.](\d{4})\b")
LEDGER_HEADER = ["sha256", "source", "filed_to", "vendor", "amount", "currency", "invoice_date", "filed_at"]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _slug(text: str, limit: int = 28) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return (cleaned or "document")[:limit]


def extract_text(path: Path) -> tuple[str, str]:
    """Return (text, note). An unsupported input raises WorkflowError with the fix."""
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md"}:
        return path.read_text(encoding="utf-8", errors="replace"), ""
    if suffix == ".csv":
        rows = docengine.csv_read(path).get("rows", [])
        return "\n".join(" ".join(str(cell) for cell in row) for row in rows), ""
    if suffix == ".docx":
        return docengine.docx_text(path), ""
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader  # type: ignore
        except Exception as exc:  # noqa: BLE001 - the instruction is the useful part
            raise WorkflowError("reading PDF invoices needs pypdf: pip install pypdf") from exc
        reader = PdfReader(str(path))
        return "\n".join((page.extract_text() or "") for page in reader.pages), ""
    raise WorkflowError(
        f"{path.name}: {suffix or 'no extension'} is not readable without an OCR engine; "
        "save it as PDF, DOCX, CSV or TXT, or install the vision extra"
    )


def parse_amount(text: str) -> tuple[float, str, str]:
    """Return (amount, currency, basis). Prefers a line that says total/amount due."""
    best: tuple[float, str, str] | None = None
    for line in text.splitlines():
        hinted = bool(TOTAL_HINT.search(line))
        for match in CURRENCY_PATTERN.finditer(line):
            raw = match.group("amount")
            if not raw:
                continue
            try:
                value = float(raw.replace(",", "").replace(" ", ""))
            except ValueError:
                continue
            if value <= 0 or value > 1_000_000_000:
                continue
            currency = (match.group("code") or "").upper()
            if not currency:
                symbol = match.group("symbol") or ""
                currency = {"$": "USD", "€": "EUR", "£": "GBP", "₨": "PKR", "₹": "INR"}.get(symbol, "USD")
                if symbol.lower().startswith("rs"):
                    currency = "PKR"
            basis = "line mentions a total" if hinted else "first amount found"
            if hinted:
                return value, currency, basis
            if best is None:
                best = (value, currency, basis)
    if best is None:
        raise WorkflowError("no amount could be read from this document")
    return best


def parse_date(text: str) -> tuple[datetime | None, str]:
    match = ISO_DATE.search(text)
    if match:
        try:
            return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3))), "ISO date"
        except ValueError:
            pass
    match = NUMERIC_DATE.search(text)
    if match:
        day, month, year = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if 1 <= month <= 12 and 1 <= day <= 31:
            try:
                return datetime(year, month, day), "day-first date, as written outside the US"
            except ValueError:
                return None, "date fields are not a real calendar date"
    return None, "no date found; the file's modification time was used"


def _load_ledger(path: Path) -> list[list[Any]]:
    if not path.exists():
        return [LEDGER_HEADER]
    try:
        rows = docengine.xlsx_read(path).get("rows", [])
    except docengine.DocumentError as exc:
        raise WorkflowError(f"the ledger at {path} cannot be read: {exc}") from exc
    if not rows:
        return [LEDGER_HEADER]
    if [str(cell) for cell in rows[0]] != LEDGER_HEADER:
        raise WorkflowError(
            f"the ledger at {path} does not look like an ZYNTHARO ledger; refusing to append to it"
        )
    return rows


class InvoiceIntake(Workflow):
    name = "invoice_intake"
    title = "Invoice intake"
    description = (
        "Read every new invoice in a folder, file it by year and quarter, and record it once in "
        "an Excel ledger. Safe to re-run: identical files are skipped."
    )
    needs = "A folder where invoices arrive, a folder to file them into, and a ledger path."

    def run(
        self,
        source: str,
        dest: str,
        ledger: str,
        move: bool = False,
        dry_run: bool = False,
        **_: Any,
    ) -> WorkflowResult:
        result = WorkflowResult(self.name, FAILED, "")
        source_dir, dest_dir, ledger_path = Path(source).expanduser(), Path(dest).expanduser(), Path(ledger).expanduser()
        if not source_dir.is_dir():
            raise WorkflowError(f"the invoice folder {source_dir} does not exist")
        dest_dir.mkdir(parents=True, exist_ok=True)
        rows = _load_ledger(ledger_path)
        seen = {str(row[0]) for row in rows[1:] if row}
        result.step("scan", OK, f"{sum(1 for p in source_dir.iterdir() if p.is_file())} file(s) in {source_dir}")

        filed = skipped = failed = 0
        for path in sorted(p for p in source_dir.iterdir() if p.is_file() and not p.name.startswith(".")):
            if path.suffix.lower() not in SUPPORTED:
                # An image invoice is a normal case, not an error: name the missing engine so the
                # customer knows the fix is one optional component, not a broken product.
                detail = (
                    f"{path.suffix or 'no extension'} needs an OCR engine (the vision extra); "
                    "save it as PDF, DOCX, CSV or TXT to file it without one"
                    if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"}
                    else f"{path.suffix or 'no extension'} is not an invoice format this build reads"
                )
                result.step(path.name, SKIPPED, detail)
                skipped += 1
                continue
            digest = _sha256(path)
            if digest in seen:
                result.step(path.name, SKIPPED, "already filed: the same content is in the ledger")
                skipped += 1
                continue
            try:
                text, note = extract_text(path)
                amount, currency, basis = parse_amount(text)
                when, date_note = parse_date(text)
            except (WorkflowError, docengine.DocumentError, OSError) as exc:
                result.step(path.name, SKIPPED, str(exc))
                result.problems.append(f"{path.name}: {exc}")
                skipped += 1
                continue
            day = when or datetime.fromtimestamp(path.stat().st_mtime)
            quarter = (day.month - 1) // 3 + 1
            vendor = _slug(path.stem)
            target = dest_dir / f"{day.year}" / f"Q{quarter}" / f"{day:%Y-%m-%d}_{vendor}_{str(amount).replace('.', '-')}_{digest[:8]}{path.suffix.lower()}"
            detail = f"{basis}; {date_note}; {note}" if note else f"{basis}; {date_note}"
            if dry_run:
                result.step(path.name, OK, f"would file to {target} ({detail})")
                filed += 1
                continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                if move:
                    shutil.move(str(path), target)
                else:
                    shutil.copy2(path, target)
            except OSError as exc:
                result.step(path.name, SKIPPED, f"could not file: {exc}")
                result.problems.append(f"{path.name}: {exc}")
                failed += 1
                continue
            rows.append(
                [
                    digest,
                    str(path),
                    str(target),
                    vendor,
                    amount,
                    currency,
                    day.strftime("%Y-%m-%d"),
                    datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                ]
            )
            seen.add(digest)
            written = docengine.xlsx_write(ledger_path, {"ledger": rows}, title="ZYNTHARO invoice ledger")
            if not written.get("verified"):
                result.step(path.name, SKIPPED, "the ledger did not read back the row that was just written")
                result.problems.append("ledger write could not be verified")
                failed += 1
                continue
            result.step(
                path.name,
                OK,
                f"filed to {target.relative_to(dest_dir)}; {currency} {amount:,.2f} ({detail})",
                [str(target), str(ledger_path)],
            )
            filed += 1

        result.counts = {"filed": filed, "skipped": skipped, "failed": failed}
        if failed:
            return result.finish(PARTIAL, f"{filed} filed, {skipped} skipped, {failed} failed — see the steps")
        if not filed and skipped:
            return result.finish(PARTIAL, f"nothing new to file; {skipped} file(s) skipped with reasons")
        return result.finish(DONE, f"{filed} invoice(s) filed and recorded in {ledger_path.name}")