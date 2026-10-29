"""``python -m zyntharo`` — the same entry point as the installed ``zyntharo`` command.

Kept so the system can be driven from a checkout, a container, or a packaged build without
relying on a console script being on PATH (the installer may ship the script under a
different name, and the release gate drives this form directly).
"""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())