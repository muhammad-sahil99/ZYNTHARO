"""Operational layer: staying alive and staying honest (contract sections 22.1-22.11).

* supervisor   - process ownership and orphan cleanup (22.1)
* preflight    - task requirements and unattended decisions (22.2)
* verification - outcome checks and risk tiers (22.4, 22.8)
* owner        - owner identity, PIN/recovery, shared PCs (22.7)
* billing      - BYOK / gateway / local-only (22.6)
* catalog      - signed model catalog and safe apply gate (22.10)
* governor     - low priority, pause during games and calls (22.11)
* languages    - Urdu/Arabic/RTL support (22.11)
"""

__all__ = [
    "supervisor",
    "preflight",
    "verification",
    "owner",
    "billing",
    "catalog",
    "governor",
    "languages",
]
