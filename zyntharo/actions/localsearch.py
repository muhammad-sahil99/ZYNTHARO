"""Local search as agent actions (spec 14.2).

Three rules shape this surface:

* a result is a *file that was re-checked*, not a row that was once true;
* personal sources (mail, chats, history) are indexed only after the owner opts in, and the
  action cannot opt in on the owner's behalf;
* the index reports what it cannot read. An image with no OCR engine or a PDF with no
  extractor is listed as unextractable with a reason, never quietly missing.
"""

from __future__ import annotations

from typing import Any, Sequence

from ..ops.index import SearchIndex
from .registry import Action, ActionContext, ActionFailed, action

_index: SearchIndex | None = None


def set_index(index: SearchIndex | None) -> None:
    global _index
    _index = index


def index() -> SearchIndex:
    global _index
    if _index is None:
        _index = SearchIndex(":memory:")
    return _index


@action(
    name="local_search",
    description=(
        "Search this machine's indexed files and documents by words. Returns ranked hits with "
        "the file path, a snippet, and whether the file was re-verified on disk."
    ),
    capability="filesystem.read",
    group="search",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer"},
            "kinds": {"type": "array", "items": {"type": "string"}},
            "sources": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["query"],
    },
)
def local_search(
    ctx: ActionContext,
    query: str,
    limit: int = 20,
    kinds: Sequence[str] | None = None,
    sources: Sequence[str] | None = None,
) -> Any:
    return index().search(query, limit=limit, kinds=list(kinds) if kinds else None, sources=list(sources) if sources else None)


@action(
    name="local_index_add",
    description=(
        "Index a folder or file. Personal sources (email, WhatsApp, browser history, clipboard) "
        "require the owner's opt-in and cannot be added by an agent."
    ),
    capability="filesystem.read",
    group="search",
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "root": {"type": "string"},
            "kind": {"type": "string"},
        },
        "required": ["name", "root"],
    },
)
def local_index_add(ctx: ActionContext, name: str, root: str, kind: str = "files") -> Any:
    personal = {"email", "whatsapp", "browser_history", "clipboard"}
    opt_in = bool(getattr(ctx, "owner_approved", False))
    if kind in personal and not opt_in:
        raise ActionFailed(
            f"{kind} is personal data; the owner opts in through settings, not through a tool call",
            {"kind": kind, "requires": "owner"},
        )
    try:
        return index().add_source(name, root, kind=kind, opt_in=opt_in)
    except (FileNotFoundError, PermissionError) as exc:
        raise ActionFailed(str(exc)) from exc


@action(
    name="local_index_update",
    description=(
        "Run an incremental scan of the indexed sources. Only changed files are re-read; files "
        "that disappeared are removed from the index."
    ),
    capability="filesystem.read",
    group="search",
    parameters={
        "type": "object",
        "properties": {
            "source": {"type": "string"},
            "limit": {"type": "integer"},
            "batch": {"type": "integer"},
        },
    },
)
def local_index_update(ctx: ActionContext, source: str = "", limit: int = 2_000, batch: int = 200) -> Any:
    return index().update(source=source or None, limit=limit, batch=batch, sleep=0.01)


@action(
    name="local_index_status",
    description=(
        "What is indexed, what could not be read and why, and which search capabilities exist "
        "on this machine (lexical, OCR, PDF)."
    ),
    capability="filesystem.read",
    group="search",
    parameters={"type": "object", "properties": {}},
)
def local_index_status(ctx: ActionContext) -> Any:
    return index().status()


@action(
    name="local_index_forget",
    description="Stop indexing a source and delete its rows. Does not touch the files themselves.",
    capability="filesystem.read",
    group="search",
    danger="medium",
    parameters={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
)
def local_index_forget(ctx: ActionContext, name: str) -> Any:
    removed = index().forget_source(name)
    return {"source": name, "documents_removed": removed}


all_actions = lambda: [  # noqa: E731
    local_search,
    local_index_add,
    local_index_update,
    local_index_status,
    local_index_forget,
]