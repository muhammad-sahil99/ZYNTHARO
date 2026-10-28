"""What "irreversible" means, in exactly one place.

Three modules used to answer this question separately, and they disagreed:

* ``ops/owner.py`` listed ten capabilities (including ``model.assign`` and ``memory.admin``);
* ``ops/verification.py`` listed eight, with no ``model.assign``/``memory.admin``;
* ``actions/registry.py`` imported the owner's tuple, so the *gate* that demands a PIN and
  the *tiering* that decides how carefully an action is verified were reading different
  truths. A task that reassigned a model or rewrote an agent's memory was therefore gated as
  irreversible but tiered as if it were routine.

A duplicated safety list is a safety bug: the two copies drift, and the drift always goes in
the direction of *less* protection. This module is the one copy. If a capability is added,
it is added here and every consumer sees it.
"""

from __future__ import annotations

#: Capabilities whose effects ORBYNTH cannot undo. Everything that reads this list does so
#: for the same reason: these need the owner's PIN, the strictest verification tier, and a
#: human confirmation -- never a batch, a shortcut, or a voice match.
IRREVERSIBLE_CAPABILITIES: frozenset[str] = frozenset(
    {
        # data and system
        "filesystem.delete",
        "shell.exec",
        "process.kill",
        "memory.admin",
        # documents and browsers
        "office.macro",
        "browser.eval",
        # outward-facing and self-modifying
        "whatsapp.call",
        # A delivered message cannot be recalled: the customer has already read it.
        "channel.send",
        "plugin.install",
        "plugin.uninstall",
        # fleet control: moving the model under a running agent loses its context, and a
        # rogue reassignment can silence the agent that was watching it.
        "model.assign",
    }
)


def is_irreversible(capability: str) -> bool:
    """True when this capability may not be undone, batched, or approved by voice alone."""
    return capability in IRREVERSIBLE_CAPABILITIES


def irreversible_among(capabilities: "list[str] | tuple[str, ...] | set[str]") -> tuple[str, ...]:
    """The irreversible subset of a capability list, order preserved."""
    return tuple(cap for cap in capabilities if cap in IRREVERSIBLE_CAPABILITIES)
