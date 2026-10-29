"""Local search index (spec 14.2): SQLite FTS5 over the files this machine actually has.

What this is
------------
A real, incremental, permissioned text index: it walks the sources the owner enabled,
extracts text (plain/code/CSV/Markdown natively, Office files through the same engine the
document tools use), stores it in FTS5 and answers ranked queries with snippets. Every hit is
verified against the file on disk before it is returned -- a path is not a result.

What this is not (stated here, in LIMITATIONS.md and in the HUD, not discovered later)
------------------------------------------------------------------------------
* No embeddings and no ``sqlite-vec``: search is lexical (FTS5 + BM25), not semantic. A query
  phrased with completely different words from the document will miss. The interface is
  shaped so an embedding column can be added without changing callers.
* No OS file-change notifications. Freshness comes from a scan (which is incremental by
  size/mtime/hash), so a file written a second ago is found when the index is next updated.
* No OCR unless a recogniser happens to be installed; images without one are recorded as
  ``unextractable`` with the reason rather than silently skipped.
* No PDF text unless a PDF library is present. Same rule: recorded, with the reason.

The worker model from the spec holds: indexing is a loop that processes a bounded batch, sleeps
between batches to stay out of the way, and exits when its queue is empty.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS sources (
    name        TEXT PRIMARY KEY,
    root        TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'files',
    enabled     INTEGER NOT NULL DEFAULT 1,
    opt_in      INTEGER NOT NULL DEFAULT 0,
    added_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS docs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    path        TEXT NOT NULL UNIQUE,
    source      TEXT NOT NULL,
    kind        TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    body        TEXT NOT NULL DEFAULT '',
    size        INTEGER NOT NULL DEFAULT 0,
    mtime       REAL NOT NULL DEFAULT 0,
    sha         TEXT NOT NULL DEFAULT '',
    indexed_at  REAL NOT NULL DEFAULT 0,
    error       TEXT NOT NULL DEFAULT ''
);

CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
    title, body, path UNINDEXED, source UNINDEXED, tokenize='porter unicode61'
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  REAL NOT NULL,
    finished_at REAL NOT NULL DEFAULT 0,
    scanned     INTEGER NOT NULL DEFAULT 0,
    updated     INTEGER NOT NULL DEFAULT 0,
    removed     INTEGER NOT NULL DEFAULT 0,
    skipped     INTEGER NOT NULL DEFAULT 0,
    failed      INTEGER NOT NULL DEFAULT 0,
    seconds     REAL NOT NULL DEFAULT 0
);
"""

#: Directories that are never worth indexing: caches, package trees, version-control internals
#: and operating-system noise. Excluding them is also what keeps the index inside its budget.
DEFAULT_EXCLUDES = (
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", "env",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", "dist", "build",
    "AppData", "Library/Caches", ".cache", ".npm", ".nuget", "site-packages",
    ".Trash", "$Recycle.Bin", "System Volume Information",
)

#: Extensions read as text, with the kind label the HUD and the model see.
TEXT_KINDS = {
    ".txt": "text", ".md": "markdown", ".rst": "text", ".log": "log", ".csv": "csv",
    ".tsv": "csv", ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
    ".ini": "config", ".cfg": "config", ".conf": "config", ".env.example": "config",
    ".py": "code", ".js": "code", ".ts": "code", ".tsx": "code", ".jsx": "code",
    ".rs": "code", ".go": "code", ".java": "code", ".kt": "code", ".cs": "code",
    ".c": "code", ".h": "code", ".cpp": "code", ".hpp": "code", ".rb": "code",
    ".php": "code", ".sh": "code", ".ps1": "code", ".sql": "code", ".css": "code",
    ".html": "code", ".xml": "code", ".vue": "code", ".svelte": "code", ".lua": "code",
    ".swift": "code", ".dart": "code", ".r": "code", ".m": "code", ".ipynb": "notebook",
}

OFFICE_KINDS = {".docx": "docx", ".xlsx": "xlsx", ".pptx": "pptx"}
IMAGE_KINDS = {".png": "image", ".jpg": "image", ".jpeg": "image", ".bmp": "image", ".webp": "image", ".tif": "image", ".tiff": "image"}

MAX_FILE_BYTES = 8 * 1024 * 1024  # a bigger file is recorded, not silently read in full
MAX_BODY_CHARS = 400_000


@dataclass(slots=True)
class Doc:
    """One indexed file: what it is, what it said, and the evidence it said it."""

    path: str
    source: str
    kind: str
    title: str = ""
    body: str = ""
    size: int = 0
    mtime: float = 0.0
    sha: str = ""
    error: str = ""


@dataclass(slots=True)
class Hit:
    path: str
    source: str
    kind: str
    title: str
    snippet: str
    score: float
    verified: bool
    problem: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "source": self.source,
            "kind": self.kind,
            "title": self.title,
            "snippet": self.snippet,
            "score": round(self.score, 4),
            "verified": self.verified,
            "problem": self.problem,
        }


def sha256_file(path: Path, limit: int = MAX_FILE_BYTES) -> str:
    digest = hashlib.sha256()
    read = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(262_144)
            if not chunk:
                break
            read += len(chunk)
            if read > limit:
                break
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def extract(path: Path) -> tuple[str, str, str]:
    """Return ``(kind, text, error)``. An unreadable format is an error, never empty success."""
    suffix = path.suffix.lower()
    if suffix in TEXT_KINDS:
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return TEXT_KINDS[suffix], "", f"cannot read: {exc}"
        return TEXT_KINDS[suffix], raw[:MAX_BODY_CHARS], ""
    if suffix in OFFICE_KINDS:
        from . import docengine

        try:
            if suffix == ".docx":
                return "docx", docengine.docx_text(path)[:MAX_BODY_CHARS], ""
            if suffix == ".xlsx":
                book = docengine.xlsx_read(path)
                rows = []
                for sheet, values in (book.get("sheets") or {}).items():
                    for row in values:
                        rows.append(f"{sheet}\t" + "\t".join("" if cell is None else str(cell) for cell in row))
                return "xlsx", "\n".join(rows)[:MAX_BODY_CHARS], ""
            return "pptx", "", "reading PowerPoint text is not implemented; authoring is refused too"
        except Exception as exc:  # a corrupt document is a finding, not a crash
            return suffix.lstrip("."), "", f"cannot extract: {exc}"
    if suffix in IMAGE_KINDS:
        try:
            from rapidocr_onnxruntime import RapidOCR  # type: ignore

            engine = RapidOCR()
            result, _ = engine(str(path))
            text = "\n".join(item[1] for item in (result or []))
            return "image", text[:MAX_BODY_CHARS], ""
        except Exception as exc:
            return "image", "", f"no OCR engine available ({type(exc).__name__}); the image is not searchable"
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader  # type: ignore

            reader = PdfReader(str(path))
            pages = [(page.extract_text() or "") for page in reader.pages]
            return "pdf", "\n".join(pages)[:MAX_BODY_CHARS], ""
        except Exception as exc:
            return "pdf", "", f"no PDF text extractor available ({type(exc).__name__}); the file is not searchable"
    return "binary", "", f"{suffix or 'no extension'} is not a searchable format"

# --------------------------------------------------------------------------------------
# The index
# --------------------------------------------------------------------------------------


class SearchIndex:
    """Incremental FTS5 index with per-source permissions and verified results."""

    def __init__(self, path: str | Path = ":memory:", bus: Any = None) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.bus = bus
        self.excludes: tuple[str, ...] = DEFAULT_EXCLUDES
        self._lock: Any = None
        self.stats = {"scanned": 0, "updated": 0, "skipped": 0, "removed": 0, "failed": 0}

    # -- sources -----------------------------------------------------------------------
    def add_source(self, name: str, root: str | Path, kind: str = "files", opt_in: bool = False) -> dict[str, Any]:
        """Register a root. Personal sources (mail, chats, history) must be opted into."""
        location = Path(root).expanduser()
        if not location.exists():
            raise FileNotFoundError(f"source root does not exist: {location}")
        personal = kind in ("email", "whatsapp", "browser_history", "clipboard")
        if personal and not opt_in:
            raise PermissionError(
                f"{kind} is personal data: it is indexed only when the owner opts in explicitly"
            )
        self.conn.execute(
            "INSERT INTO sources (name, root, kind, enabled, opt_in, added_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET root=excluded.root, kind=excluded.kind, opt_in=excluded.opt_in",
            (name, str(location), kind, 1, 1 if opt_in else 0, time.time()),
        )
        self.conn.commit()
        return {"name": name, "root": str(location), "kind": kind, "opt_in": bool(opt_in)}

    def set_enabled(self, name: str, enabled: bool) -> dict[str, Any]:
        self.conn.execute("UPDATE sources SET enabled=? WHERE name=?", (1 if enabled else 0, name))
        self.conn.commit()
        if not enabled:
            self.forget_source(name)
        return {"name": name, "enabled": bool(enabled)}

    def sources(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM sources ORDER BY name").fetchall()
        return [dict(row) for row in rows]

    def forget_source(self, name: str) -> int:
        ids = [row["id"] for row in self.conn.execute("SELECT id FROM docs WHERE source=?", (name,))]
        for doc_id in ids:
            self._delete(doc_id)
        self.conn.commit()
        return len(ids)

    # -- walking -----------------------------------------------------------------------
    def _excluded(self, path: Path) -> bool:
        text = str(path).replace("\\", "/")
        return any(f"/{name}/" in text or text.endswith(f"/{name}") for name in self.excludes)

    def _walk(self, root: Path, limit: int) -> Iterable[Path]:
        if root.is_file():
            yield root
            return
        seen = 0
        for dirpath, dirnames, filenames in os.walk(root):
            base = Path(dirpath)
            dirnames[:] = [name for name in dirnames if not self._excluded(base / name)]
            for name in filenames:
                candidate = base / name
                if self._excluded(candidate):
                    continue
                yield candidate
                seen += 1
                if seen >= limit:
                    return

    # -- indexing ----------------------------------------------------------------------
    def update(self, source: str | None = None, limit: int = 2_000, batch: int = 200, sleep: float = 0.0) -> dict[str, Any]:
        """Scan enabled sources, indexing only what changed. Returns what actually happened.

        ``limit`` bounds one run so a first index of a huge disk is many short runs rather than
        one long freeze; ``sleep`` yields between batches for the responsiveness governor.
        """
        started = time.time()
        self.stats = {"scanned": 0, "updated": 0, "skipped": 0, "removed": 0, "failed": 0}
        rows = self.conn.execute(
            "SELECT * FROM sources WHERE enabled=1" + (" AND name=?" if source else ""),
            ((source,) if source else ()),
        ).fetchall()
        pending = 0
        for row in rows:
            root = Path(row["root"])
            if not root.exists():
                self.stats["failed"] += 1
                continue
            touched: set[str] = set()
            for file in self._walk(root, limit):
                touched.add(str(file))
                self.stats["scanned"] += 1
                self._consider(file, row["name"], row["kind"])
                pending += 1
                if pending >= batch:
                    pending = 0
                    if sleep:
                        time.sleep(sleep)
            # Deletions are found per source, on every scan. An earlier version only swept
            # when one source was named explicitly, so a file removed from disk stayed in the
            # index (and in search results) until someone ran a targeted update.
            known = {r["path"] for r in self.conn.execute("SELECT path FROM docs WHERE source=?", (row["name"],))}
            for path in known - touched:
                if not Path(path).exists() or self._excluded(Path(path)):
                    self.stats["removed"] += 1
                    self._delete_path(path)
        self.conn.commit()
        elapsed = time.time() - started
        self.conn.execute(
            "INSERT INTO runs (started_at, finished_at, scanned, updated, removed, skipped, failed, seconds) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                started, time.time(), self.stats["scanned"], self.stats["updated"],
                self.stats["removed"], self.stats["skipped"], self.stats["failed"], elapsed,
            ),
        )
        self.conn.commit()
        if self.bus is not None:
            try:
                from ..events import LOCAL_INDEX

                self.bus.emit(LOCAL_INDEX, source="index", **self.stats)
            except Exception:
                pass
        return {**self.stats, "seconds": round(elapsed, 3), "sources": len(rows)}

    def _consider(self, file: Path, source: str, kind: str) -> None:
        try:
            stat = file.stat()
        except OSError:
            self.stats["failed"] += 1
            return
        row = self.conn.execute("SELECT id, size, mtime FROM docs WHERE path=?", (str(file),)).fetchone()
        if row is not None and row["size"] == stat.st_size and abs(row["mtime"] - stat.st_mtime) < 0.001:
            self.stats["skipped"] += 1
            return
        if stat.st_size > MAX_FILE_BYTES:
            self._store(file, source, "large", "", f"{stat.st_size} bytes exceeds the {MAX_FILE_BYTES} byte limit")
            self.stats["failed"] += 1
            return
        doc_kind, text, error = extract(file)
        digest = sha256_text(text) if text else ""
        self._store(file, source, doc_kind, text, error, size=stat.st_size, mtime=stat.st_mtime, sha=digest)
        if error:
            self.stats["failed"] += 1
        else:
            self.stats["updated"] += 1

    def _store(
        self,
        file: Path,
        source: str,
        kind: str,
        text: str,
        error: str = "",
        size: int = 0,
        mtime: float = 0.0,
        sha: str = "",
    ) -> None:
        title = self._title(file, text)
        existing = self.conn.execute("SELECT id FROM docs WHERE path=?", (str(file),)).fetchone()
        if existing is None:
            cursor = self.conn.execute(
                "INSERT INTO docs (path, source, kind, title, body, size, mtime, sha, indexed_at, error) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (str(file), source, kind, title, text, size, mtime, sha, time.time(), error),
            )
            doc_id = int(cursor.lastrowid)
        else:
            doc_id = int(existing["id"])
            self.conn.execute(
                "UPDATE docs SET source=?, kind=?, title=?, body=?, size=?, mtime=?, sha=?, indexed_at=?, error=? "
                "WHERE id=?",
                (source, kind, title, text, size, mtime, sha, time.time(), error, doc_id),
            )
            self.conn.execute("DELETE FROM docs_fts WHERE rowid=?", (doc_id,))
        self.conn.execute(
            "INSERT INTO docs_fts (rowid, title, body, path, source) VALUES (?,?,?,?,?)",
            (doc_id, title, text, str(file), source),
        )

    @staticmethod
    def _title(file: Path, text: str) -> str:
        for line in text.splitlines()[:40]:
            stripped = line.strip()
            if stripped.startswith("#"):
                return stripped.lstrip("# ").strip()[:200]
        return file.name

    def _delete(self, doc_id: int) -> None:
        self.conn.execute("DELETE FROM docs_fts WHERE rowid=?", (doc_id,))
        self.conn.execute("DELETE FROM docs WHERE id=?", (doc_id,))

    def _delete_path(self, path: str) -> None:
        row = self.conn.execute("SELECT id FROM docs WHERE path=?", (path,)).fetchone()
        if row is not None:
            self._delete(int(row["id"]))

    # -- search ------------------------------------------------------------------------
    def search(
        self,
        query: str,
        limit: int = 20,
        kinds: Sequence[str] | None = None,
        sources: Sequence[str] | None = None,
        verify: bool = True,
    ) -> dict[str, Any]:
        """Ranked lexical search. Every result is checked against the file before it is returned."""
        terms = [word for word in _split_terms(query) if word]
        if not terms:
            return {"query": query, "count": 0, "hits": [], "note": "no searchable terms in the query"}
        expression = " AND ".join(f'"{term}"*' for term in terms)
        sql = (
            "SELECT d.id, d.path, d.source, d.kind, d.title, d.sha, d.size, d.mtime, "
            "snippet(docs_fts, 1, '<<', '>>', ' ... ', 16) AS snippet, bm25(docs_fts) AS rank "
            "FROM docs_fts JOIN docs d ON d.id = docs_fts.rowid WHERE docs_fts MATCH ?"
        )
        params: list[Any] = [expression]
        if kinds:
            sql += " AND d.kind IN (" + ",".join("?" for _ in kinds) + ")"
            params.extend(kinds)
        if sources:
            sql += " AND d.source IN (" + ",".join("?" for _ in sources) + ")"
            params.extend(sources)
        sql += " ORDER BY rank LIMIT ?"
        params.append(max(1, min(limit, 200)))
        try:
            rows = self.conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError as exc:
            return {"query": query, "count": 0, "hits": [], "error": f"the query could not be parsed: {exc}"}
        hits: list[Hit] = []
        for row in rows:
            hit = Hit(
                path=row["path"],
                source=row["source"],
                kind=row["kind"],
                title=row["title"],
                snippet=row["snippet"] or "",
                score=-float(row["rank"] or 0.0),
                verified=True,
            )
            if verify:
                problem = self._verify(row)
                if problem:
                    hit.verified = False
                    hit.problem = problem
                    self._delete(int(row["id"]))
                    continue
            hits.append(hit)
        self.conn.commit()
        return {"query": query, "count": len(hits), "hits": [hit.to_json() for hit in hits], "terms": terms}

    def _verify(self, row: sqlite3.Row) -> str:
        """A path is not a result: the file must still exist and still be the file we indexed."""
        path = Path(row["path"])
        if not path.exists():
            return "the file no longer exists; the stale entry was removed"
        try:
            stat = path.stat()
        except OSError as exc:
            return f"cannot stat the file: {exc}"
        if row["sha"]:
            try:
                if sha256_text(extract(path)[1]) != row["sha"]:
                    return "the file changed since it was indexed; reindex to trust the snippet"
            except Exception as exc:
                return f"cannot re-read the file: {exc}"
        elif stat.st_size and stat.st_size != row["size"]:
            return "the file size changed since it was indexed"
        return ""

    # -- operations --------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        docs = self.conn.execute("SELECT COUNT(*) AS n FROM docs").fetchone()["n"]
        failed = self.conn.execute("SELECT COUNT(*) AS n FROM docs WHERE error != ''").fetchone()["n"]
        last = self.conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        kinds = {
            row["kind"]: row["n"]
            for row in self.conn.execute("SELECT kind, COUNT(*) AS n FROM docs GROUP BY kind ORDER BY n DESC")
        }
        return {
            "path": self.path,
            "documents": docs,
            "unextractable": failed,
            "kinds": kinds,
            "sources": self.sources(),
            "last_run": dict(last) if last is not None else None,
            "capabilities": {
                "lexical": True,
                "semantic": False,
                "ocr": _has_ocr(),
                "pdf": _has_pdf(),
                "file_watch": False,
            },
            "note": (
                "search is lexical (FTS5, BM25): no embeddings, no OCR or PDF text without an "
                "installed extractor, and freshness comes from update(), not OS notifications"
            ),
        }

    def run_until_idle(self, batch: int = 200, sleep: float = 0.05, max_runs: int = 40) -> dict[str, Any]:
        """The worker model from the spec: bounded batches, yield between them, stop when idle."""
        total = {"runs": 0, "updated": 0, "removed": 0, "failed": 0, "scanned": 0}
        for _ in range(max_runs):
            report = self.update(batch=batch, sleep=sleep)
            total["runs"] += 1
            for key in ("updated", "removed", "failed", "scanned"):
                total[key] += report.get(key, 0)
            if not report.get("updated") and not report.get("removed"):
                break
        return total

    def close(self) -> None:
        self.conn.close()


def _split_terms(query: str) -> list[str]:
    cleaned = []
    for word in query.replace('"', " ").replace("'", " ").split():
        word = "".join(ch for ch in word if ch.isalnum() or ch in "_-.")
        if len(word) > 1:
            cleaned.append(word)
    return cleaned


def _has_ocr() -> bool:
    try:
        import rapidocr_onnxruntime  # type: ignore  # noqa: F401

        return True
    except Exception:
        return False


def _has_pdf() -> bool:
    try:
        import pypdf  # type: ignore  # noqa: F401

        return True
    except Exception:
        return False
