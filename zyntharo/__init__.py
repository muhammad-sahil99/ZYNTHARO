"""ZYNTHARO — a desktop-resident, voice-first, multi-agent AI operative.

The public entry points live in :mod:`zyntharo.cli`. Importing this package must stay
dependency-free: the core is stdlib-only so the packaged installer runs on a machine with
no coding environment at all.
"""

__version__ = "0.9.0b1"
__product__ = "ZYNTHARO Beta"
__tagline__ = "Desktop operative: vision, input, browser, files, Office, self-extending."
__alternate_brand__ = "SYNOVARIS"

__all__ = ["__version__", "__product__", "__tagline__", "__alternate_brand__"]
