"""Paths to assets that must work both from source and a packaged application."""

from __future__ import annotations

import sys
from pathlib import Path


def app_root() -> Path:
    """Return the immutable application payload root.

    PyInstaller exposes extracted bundled files via ``_MEIPASS``.  Falling back to the source
    root keeps developer execution identical without making callers guess from ``__file__``.
    """
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        return Path(bundled)
    return Path(__file__).resolve().parent.parent


def resource_path(*parts: str) -> Path:
    """Resolve a bundled, read-only application resource."""
    return app_root().joinpath(*parts)
