"""Channels: one honest interface to the messaging platforms a customer actually uses.

The product rule here is deliberate and narrow: a platform appears in this package only when
ORBYNTH can really talk to it. A platform that needs a business account, an app review, a paid
API tier or an unofficial automation of a web client is *declared* with that reason and cannot
be selected by accident. A fake "Connect Instagram" button that always fails is worse than an
honest "not available: Meta requires an app review for DM access".

Each channel reports one of four states, and nothing is inferred:

* ``ready``        — credentials present and the last call succeeded
* ``configured``   — credentials present, not yet exercised in this session
* ``needs_setup``  — a precise, actionable instruction is attached
* ``unavailable``  — this channel cannot be offered, with the reason
"""

from __future__ import annotations

from .base import Channel, ChannelError, ChannelMessage, ChannelStatus
from .registry import ChannelRegistry, declared_platforms, default_registry

__all__ = [
    "Channel",
    "ChannelError",
    "ChannelMessage",
    "ChannelStatus",
    "ChannelRegistry",
    "declared_platforms",
    "default_registry",
]