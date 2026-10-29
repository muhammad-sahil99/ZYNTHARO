"""Filesystem actions: read ranges, write, edit, create any file type, move, delete, archive.

This is a fully working driver — no native dependency, no display, testable headless. It is
also the layer that makes the "it can read a file's line range, edit files, create any kind
of file (.py, .rs, .docx, …)" requirement concrete.

Safety properties:
* writes are atomic (temp file + ``os.replace``) so a crash never leaves a half-written file;
* reads are bounded (``max_bytes``) so an agent cannot blow its context on a 4 GB log;
* binary files are returned as metadata + base64 preview rather than mojibake;
* every path is checked against ``safety.allowed_fs_roots`` when that list is non-empty.
"""

from __future__ import annotations

import base64
import fnmatch
import hashlib
import json
import os
import shutil
import stat as stat_module
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, Iterator

from .registry import Action, ActionContext, ActionFailed, action

TEXT_SUFFIXES = {
    ".txt", ".md", ".rst", ".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx",
    ".rs", ".go", ".java", ".kt", ".c", ".h", ".cpp", ".hpp", ".cs", ".rb", ".php", ".swift",
    ".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd", ".json", ".jsonc", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".conf", ".env", ".csv", ".tsv", ".sql", ".html", ".htm",
    ".css", ".scss", ".xml", ".svg", ".tex", ".log", ".gitignore", ".dockerfile", ".mk",
}
MAX_READ_BYTES = 2_000_000


def _resolve(ctx: ActionContext, raw: str, must_exist: bool = False) -> Path:
    if not raw:
        raise ActionFailed("path is required")
    path = Path(os.path.expandvars(os.path.expanduser(str(raw)))).resolve()
    roots = []
    if ctx.config is not None:
        roots = [Path(r).expanduser().resolve() for r in ctx.config.safety.allowed_fs_roots]
    if roots and not any(_is_within(path, root) for root in roots):
        raise ActionFailed(
            f"path outside allowed roots: {path}",
            {"path": str(path), "allowed_roots": [str(r) for r in roots]},
        )
    if must_exist and not path.exists():
        raise ActionFailed(f"no such path: {path}", {"path": str(path)})
    return path


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _is_text(path: Path, sample: bytes = b"") -> bool:
    if path.suffix.lower() in TEXT_SUFFIXES:
        return True
    if path.name.lower() in TEXT_SUFFIXES:
        return True
    try:
        chunk = sample or path.read_bytes()[:4096]
    except OSError:
        return False
    if b"\x00" in chunk:
        return False
    return True


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".zyntharo-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _iter_files(root: Path, pattern: str, recursive: bool, limit: int) -> Iterator[Path]:
    count = 0
    if recursive:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in {".git", "__pycache__", "node_modules", ".venv"}]
            for name in filenames:
                if fnmatch.fnmatch(name, pattern):
                    yield Path(dirpath) / name
                    count += 1
                    if count >= limit:
                        return
    else:
        for entry in sorted(root.iterdir()):
            if entry.is_file() and fnmatch.fnmatch(entry.name, pattern):
                yield entry
                count += 1
                if count >= limit:
                    return


# --------------------------------------------------------------------------------------
# Read
# --------------------------------------------------------------------------------------


@action(
    name="fs_read",
    description=(
        "Read a file. Supports line ranges (start_line/end_line, 1-based, inclusive), byte "
        "budgets and encoding. Returns text for text files and base64 preview plus metadata "
        "for binary files. This is the preferred way to inspect any file."
    ),
    capability="filesystem.read",
    group="filesystem",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute or ~ path to the file."},
            "start_line": {"type": "integer", "description": "First line to read (1-based)."},
            "end_line": {"type": "integer", "description": "Last line to read (inclusive)."},
            "max_bytes": {"type": "integer", "description": "Hard cap on returned bytes."},
            "encoding": {"type": "string", "description": "Text encoding, default utf-8."},
        },
        "required": ["path"],
    },
)
def fs_read(
    ctx: ActionContext,
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
    max_bytes: int = MAX_READ_BYTES,
    encoding: str = "utf-8",
) -> Any:
    target = _resolve(ctx, path, must_exist=True)
    if target.is_dir():
        raise ActionFailed(f"{target} is a directory; use fs_list", {"path": str(target)})
    size = target.stat().st_size
    binary = not _is_text(target)
    if binary:
        with target.open("rb") as fh:
            head = fh.read(min(size, 4096))
        return {
            "path": str(target),
            "kind": "binary",
            "size_bytes": size,
            "sha256": _sha256(target),
            "preview_base64": base64.b64encode(head).decode("ascii"),
            "note": "binary file: text decoding skipped; nothing was lost",
        }
    raw = target.read_bytes()
    text = raw[:max_bytes].decode(encoding, errors="replace")
    truncated_bytes = size > max_bytes
    lines = text.splitlines()
    total_lines = lines.__len__()
    if start_line is not None or end_line is not None:
        start = max(1, int(start_line or 1))
        end = int(end_line) if end_line is not None else total_lines
        if end < start:
            raise ActionFailed(f"end_line ({end}) precedes start_line ({start})")
        selected = lines[start - 1 : end]
    else:
        selected = lines
    return {
        "path": str(target),
        "kind": "text",
        "size_bytes": size,
        "total_lines_in_slice": total_lines,
        "range": [start_line or 1, end_line or total_lines],
        "truncated_by_bytes": truncated_bytes,
        "content": "\n".join(selected),
        "sha256": _sha256(target),
    }


@action(
    name="fs_stat",
    description="Metadata for a path: type, size, timestamps, permissions, sha256 for files.",
    capability="filesystem.read",
    group="filesystem",
    parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
)
def fs_stat(ctx: ActionContext, path: str) -> Any:
    target = _resolve(ctx, path, must_exist=True)
    st = target.stat()
    return {
        "path": str(target),
        "is_dir": target.is_dir(),
        "is_file": target.is_file(),
        "is_symlink": target.is_symlink(),
        "size_bytes": st.st_size,
        "modified": st.st_mtime,
        "created": st.st_ctime,
        "mode": stat_module.filemode(st.st_mode),
        "sha256": _sha256(target) if target.is_file() else None,
    }


@action(
    name="fs_list",
    description="List a directory, optionally recursively with a glob pattern.",
    capability="filesystem.read",
    group="filesystem",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "pattern": {"type": "string", "description": "Glob, default *"},
            "recursive": {"type": "boolean"},
            "limit": {"type": "integer"},
        },
        "required": ["path"],
    },
)
def fs_list(ctx: ActionContext, path: str, pattern: str = "*", recursive: bool = False, limit: int = 500) -> Any:
    target = _resolve(ctx, path, must_exist=True)
    if target.is_file():
        return {"entries": [{"name": target.name, "path": str(target), "size": target.stat().st_size}]}
    entries: list[dict[str, Any]] = []
    if recursive:
        for file in _iter_files(target, pattern, True, limit):
            try:
                st = file.stat()
            except OSError:
                continue
            entries.append({"path": str(file), "size": st.st_size, "dir": False})
    else:
        for entry in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
            try:
                st = entry.stat()
            except OSError:
                continue
            entries.append({"path": str(entry), "name": entry.name, "dir": entry.is_dir(), "size": st.st_size})
    return {"path": str(target), "count": len(entries), "entries": entries[:limit]}


@action(
    name="fs_search",
    description=(
        "Search inside files under a root: by content substring/regex (grep-like) or by name "
        "glob. Returns matches with file, line number and the matching line."
    ),
    capability="filesystem.read",
    group="filesystem",
    parameters={
        "type": "object",
        "properties": {
            "root": {"type": "string"},
            "content": {"type": "string", "description": "Substring or regex to find in file contents."},
            "name_glob": {"type": "string", "description": "Only consider files matching this glob."},
            "regex": {"type": "boolean"},
            "case_sensitive": {"type": "boolean"},
            "max_results": {"type": "integer"},
        },
        "required": ["root"],
    },
)
def fs_search(
    ctx: ActionContext,
    root: str,
    content: str = "",
    name_glob: str = "*",
    regex: bool = False,
    case_sensitive: bool = False,
    max_results: int = 100,
) -> Any:
    import re

    target = _resolve(ctx, root, must_exist=True)
    pattern = None
    if content:
        pattern = re.compile(content if regex else re.escape(content), 0 if case_sensitive else re.IGNORECASE)
    hits: list[dict[str, Any]] = []
    for file in _iter_files(target, name_glob, True, 20_000):
        if pattern is None:
            hits.append({"path": str(file)})
        else:
            if not _is_text(file):
                continue
            try:
                with file.open("r", encoding="utf-8", errors="replace") as fh:
                    for number, line in enumerate(fh, 1):
                        if pattern.search(line):
                            hits.append({"path": str(file), "line": number, "text": line.rstrip()[:400]})
                            break
            except OSError:
                continue
        if len(hits) >= max_results:
            break
    return {"root": str(target), "count": len(hits), "matches": hits}


# --------------------------------------------------------------------------------------
# Write / create / edit
# --------------------------------------------------------------------------------------


@action(
    name="fs_write",
    description=(
        "Write a file atomically, creating parent directories. mode='overwrite' replaces the "
        "file, 'append' appends, 'create_only' fails if the file exists. Any extension is "
        "allowed (.py, .rs, .docx, .xlsx, …) — content is written byte-exact."
    ),
    capability="filesystem.write",
    group="filesystem",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
            "mode": {"type": "string", "enum": ["overwrite", "append", "create_only"]},
            "encoding": {"type": "string"},
            "base64": {"type": "boolean", "description": "Treat content as base64-encoded bytes."},
        },
        "required": ["path", "content"],
    },
)
def fs_write(
    ctx: ActionContext,
    path: str,
    content: str,
    mode: str = "overwrite",
    encoding: str = "utf-8",
    base64: bool = False,
) -> Any:
    target = _resolve(ctx, path)
    data = __import__("base64").b64decode(content) if base64 else content.encode(encoding)
    existed = target.exists()
    if mode == "create_only" and existed:
        raise ActionFailed(f"file already exists: {target}", {"path": str(target)})
    if mode == "append":
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("ab") as fh:
            fh.write(data)
    else:
        _atomic_write(target, data)
    return {
        "path": str(target),
        "bytes": len(data),
        "mode": mode,
        "created": not existed,
        "sha256": _sha256(target),
    }


@action(
    name="fs_create",
    description=(
        "Create a new file (or directory when kind='dir'). Fails if the target already exists, "
        "so it is safe to call as an existence check. Any extension is supported; for common "
        "code extensions the file is created with a minimal valid skeleton."
    ),
    capability="filesystem.create",
    group="filesystem",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
            "kind": {"type": "string", "enum": ["file", "dir"]},
        },
        "required": ["path"],
    },
)
def fs_create(ctx: ActionContext, path: str, content: str = "", kind: str = "file") -> Any:
    target = _resolve(ctx, path)
    if target.exists():
        raise ActionFailed(f"already exists: {target}", {"path": str(target)})
    if kind == "dir":
        target.mkdir(parents=True)
        return {"path": str(target), "kind": "dir", "created": True}
    skeleton = _SKELETONS.get(target.suffix.lower())
    # ``%NAME%`` rather than ``str.format``: several skeletons are source files full of braces,
    # and format() would treat those as fields.
    body = content if content else (skeleton.replace("%NAME%", target.stem) if skeleton else "")
    _atomic_write(target, body.encode("utf-8"))
    return {"path": str(target), "kind": "file", "created": True, "bytes": target.stat().st_size}


_SKELETONS: dict[str, str] = {
    ".py": '"""Created by ZYNTHARO."""\n\n\ndef main() -> None:\n    pass\n\n\nif __name__ == "__main__":\n    main()\n',
    ".js": "// Created by ZYNTHARO\n",
    ".ts": "// Created by ZYNTHARO\nexport {};\n",
    ".rs": "// Created by ZYNTHARO\nfn main() {\n    println!(\"hello\");\n}\n",
    ".go": "package main\n\nfunc main() {\n}\n",
    ".json": "{\n}\n",
    ".md": "# %NAME%\n",
    ".txt": "",
    ".csv": "",
    ".sh": "#!/usr/bin/env bash\nset -euo pipefail\n",
}


@action(
    name="fs_edit",
    description=(
        "Edit a text file in place: literal or regex find/replace, optional occurrence limited "
        "to a line range. Reports how many replacements were made and returns a diff summary. "
        "Use this instead of rewriting whole files when changing code."
    ),
    capability="filesystem.write",
    group="filesystem",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "find": {"type": "string"},
            "replace": {"type": "string"},
            "regex": {"type": "boolean"},
            "count": {"type": "integer", "description": "Max replacements, 0 = all."},
            "start_line": {"type": "integer"},
            "end_line": {"type": "integer"},
            "allow_empty": {"type": "boolean", "description": "Allow replacing with empty string."},
        },
        "required": ["path", "find", "replace"],
    },
)
def fs_edit(
    ctx: ActionContext,
    path: str,
    find: str,
    replace: str,
    regex: bool = False,
    count: int = 0,
    start_line: int | None = None,
    end_line: int | None = None,
    allow_empty: bool = False,
) -> Any:
    import re

    if find == "" :
        raise ActionFailed("find must not be empty")
    target = _resolve(ctx, path, must_exist=True)
    if not _is_text(target):
        raise ActionFailed(f"{target} is not a text file; refusing a textual edit", {"path": str(target)})
    original = target.read_text(encoding="utf-8", errors="replace")
    lines = original.splitlines(keepends=True)
    head = lines[: (start_line - 1)] if start_line else []
    body_lines = lines[(start_line - 1) : end_line] if (start_line or end_line) else lines
    tail = lines[end_line:] if end_line else []
    body = "".join(body_lines)

    pattern = re.compile(find if regex else re.escape(find))
    limit = count if count and count > 0 else 0
    matches = pattern.findall(body)
    if not matches:
        raise ActionFailed(
            "find did not match anything",
            {"path": str(target), "find": find[:120], "lines": len(lines)},
        )
    if not allow_empty and replace == "":
        raise ActionFailed("replace is empty; pass allow_empty=true to delete matched text")
    updated, replacements = pattern.subn(replace, body, count=limit)
    if updated == body:
        raise ActionFailed("edit produced no change", {"path": str(target)})
    backup = target.with_suffix(target.suffix + ".zyntharo-bak")
    if not backup.exists():
        _atomic_write(backup, original.encode("utf-8"))
    _atomic_write(target, "".join(head).encode("utf-8") + updated.encode("utf-8") + "".join(tail).encode("utf-8"))
    return {
        "path": str(target),
        "replacements": replacements,
        "backup": str(backup),
        "bytes_before": len(original),
        "bytes_after": target.stat().st_size,
    }


@action(
    name="fs_move",
    description="Move or rename a file/directory. Creates the destination parents.",
    capability="filesystem.move",
    group="filesystem",
    parameters={
        "type": "object",
        "properties": {
            "src": {"type": "string"},
            "dst": {"type": "string"},
            "overwrite": {"type": "boolean"},
        },
        "required": ["src", "dst"],
    },
)
def fs_move(ctx: ActionContext, src: str, dst: str, overwrite: bool = False) -> Any:
    source = _resolve(ctx, src, must_exist=True)
    destination = _resolve(ctx, dst)
    if destination.exists() and not overwrite:
        raise ActionFailed(f"destination exists: {destination}", {"dst": str(destination)})
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(destination))
    return {"src": str(source), "dst": str(destination), "overwritten": bool(destination.exists())}


@action(
    name="fs_copy",
    description="Copy a file or directory tree.",
    capability="filesystem.write",
    group="filesystem",
    parameters={
        "type": "object",
        "properties": {"src": {"type": "string"}, "dst": {"type": "string"}, "overwrite": {"type": "boolean"}},
        "required": ["src", "dst"],
    },
)
def fs_copy(ctx: ActionContext, src: str, dst: str, overwrite: bool = False) -> Any:
    source = _resolve(ctx, src, must_exist=True)
    destination = _resolve(ctx, dst)
    if destination.exists() and not overwrite:
        raise ActionFailed(f"destination exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, destination, dirs_exist_ok=overwrite)
    else:
        shutil.copy2(source, destination)
    return {"src": str(source), "dst": str(destination)}


@action(
    name="fs_delete",
    description="Delete a file or a directory (recursive=true for non-empty directories). Destructive.",
    capability="filesystem.delete",
    group="filesystem",
    danger="high",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "recursive": {"type": "boolean"},
            "to_trash": {"type": "boolean", "description": "Prefer OS recycle bin when available."},
        },
        "required": ["path"],
    },
)
def fs_delete(ctx: ActionContext, path: str, recursive: bool = False, to_trash: bool = True) -> Any:
    target = _resolve(ctx, path, must_exist=True)
    if target.is_dir():
        if recursive:
            shutil.rmtree(target)
        else:
            try:
                target.rmdir()
            except OSError as exc:
                raise ActionFailed(f"directory not empty: {exc}", {"path": str(target)}) from exc
    elif to_trash and _send_to_trash(target):
        return {"path": str(target), "deleted": True, "via": "trash"}
    else:
        target.unlink()
    return {"path": str(target), "deleted": True, "via": "unlink"}


def _send_to_trash(path: Path) -> bool:
    """Best-effort recycle bin; falls back to hard delete when unavailable."""
    try:  # pragma: no cover - platform specific
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            FO_DELETE, FOF_ALLOWUNDO, FOF_NOCONFIRMATION, FOF_SILENT = 3, 0x40, 0x10, 0x4

            class SHFILEOPSTRUCTW(ctypes.Structure):
                _fields_ = [
                    ("hwnd", wintypes.HWND),
                    ("wFunc", wintypes.UINT),
                    ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR),
                    ("fFlags", ctypes.c_uint16),
                    ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p),
                    ("lpszProgressTitle", wintypes.LPCWSTR),
                ]

            op = SHFILEOPSTRUCTW()
            op.wFunc = FO_DELETE
            op.pFrom = str(path) + "\0\0"
            op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT
            return ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op)) == 0
        if os.uname().sysname == "Darwin":  # type: ignore[attr-defined]
            from subprocess import run

            return run(["osascript", "-e", f'tell app "Finder" to delete POSIX file "{path}"']).returncode == 0
        from subprocess import run

        return run(["gio", "trash", str(path)]).returncode == 0
    except Exception:
        return False


@action(
    name="fs_zip",
    description="Create a Zip archive from one or more paths (files or directories).",
    capability="archive.zip",
    group="filesystem",
    parameters={
        "type": "object",
        "properties": {
            "paths": {"type": "array", "items": {"type": "string"}},
            "dest": {"type": "string"},
            "compression": {"type": "string", "enum": ["stored", "deflate", "bzip2", "lzma"]},
        },
        "required": ["paths", "dest"],
    },
)
def fs_zip(ctx: ActionContext, paths: list[str], dest: str, compression: str = "deflate") -> Any:
    modes = {
        "stored": zipfile.ZIP_STORED,
        "deflate": zipfile.ZIP_DEFLATED,
        "bzip2": zipfile.ZIP_BZIP2,
        "lzma": zipfile.ZIP_LZMA,
    }
    destination = _resolve(ctx, dest)
    destination.parent.mkdir(parents=True, exist_ok=True)
    added = 0
    with zipfile.ZipFile(destination, "w", compression=modes.get(compression, zipfile.ZIP_DEFLATED)) as archive:
        for raw in paths:
            item = _resolve(ctx, raw, must_exist=True)
            if item.is_dir():
                for file in item.rglob("*"):
                    if file.is_file():
                        archive.write(file, file.relative_to(item.parent))
                        added += 1
            else:
                archive.write(item, item.name)
                added += 1
    return {"dest": str(destination), "entries": added, "size_bytes": destination.stat().st_size}


@action(
    name="fs_unzip",
    description="Extract a Zip archive into a destination directory. Guards against path traversal.",
    capability="archive.unzip",
    group="filesystem",
    parameters={
        "type": "object",
        "properties": {"src": {"type": "string"}, "dest": {"type": "string"}},
        "required": ["src", "dest"],
    },
)
def fs_unzip(ctx: ActionContext, src: str, dest: str) -> Any:
    archive_path = _resolve(ctx, src, must_exist=True)
    destination = _resolve(ctx, dest)
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.namelist():
            resolved = (destination / member).resolve()
            if not _is_within(resolved, destination):
                raise ActionFailed(f"archive contains an unsafe path: {member}", {"member": member})
        names = archive.namelist()
        archive.extractall(destination)
    return {"src": str(archive_path), "dest": str(destination), "entries": len(names)}


def all_actions() -> list[Action]:
    """Every filesystem action, for registry bootstrap."""
    return [
        fs_read,
        fs_stat,
        fs_list,
        fs_search,
        fs_write,
        fs_create,
        fs_edit,
        fs_move,
        fs_copy,
        fs_delete,
        fs_zip,
        fs_unzip,
    ]
