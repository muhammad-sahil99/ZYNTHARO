"""Desktop application entry point used by the packaged ZYNTHARO executable."""
from __future__ import annotations

from .cli import main


def run() -> int:
    """Launch the primary local HUD without exposing CLI arguments to end users."""
    return main(["hud"])


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run())
