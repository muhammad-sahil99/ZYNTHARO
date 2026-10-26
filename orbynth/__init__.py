"""ORBYNTH — a desktop-resident, voice-first, multi-agent AI operative.

The public entry points live in :mod:`orbynth.cli`. Importing this package must stay
dependency-free: the core is stdlib-only so the packaged installer runs on a machine with
no coding environment at all.
"""

__version__ = "0.1.0"
__product__ = "ORBYNTH"
__tagline__ = "Desktop operative: vision, input, browser, files, Office, self-extending."
__alternate_brand__ = "SYNOVARIS"

__all__ = ["__version__", "__product__", "__tagline__", "__alternate_brand__"]
