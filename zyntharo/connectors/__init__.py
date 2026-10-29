"""Connected accounts: mail, drives, calendars, chat, CRM, code — with the truth about each.

Three layers, deliberately separate:

* :mod:`.base` — the contract and the four honest states;
* :mod:`.mail`, :mod:`.google`, :mod:`.microsoft`, :mod:`.saas` — real transports;
* :mod:`.registry` — which account is connected right now, and which platform cannot be reached
  at all in this build.

Nothing here ships a client secret. A desktop app is a public client, so the credential is always
the customer's own: an app password, a token they generate, or an OAuth app registration in their
own tenant. That is a product decision, not a shortcut — it is what lets the installer work on a
machine with no coding environment and no vendor account in the middle.
"""

from __future__ import annotations

from .base import (
    AUTH_DEVICE,
    AUTH_KEY_TOKEN,
    AUTH_NONE,
    AUTH_OAUTH,
    AUTH_PASSWORD,
    AUTH_TOKEN,
    CONFIGURED,
    NEEDS_SETUP,
    READY,
    UNAVAILABLE,
    Connection,
    Connector,
    ConnectorError,
    ConnectorStatus,
    NotConfigured,
    Operation,
    Unsupported,
    describe_catalogue,
)
from .registry import ConnectorRegistry, UNSUPPORTED, default_registry
from .store import Vault, redact

__all__ = [
    "AUTH_DEVICE",
    "AUTH_KEY_TOKEN",
    "AUTH_NONE",
    "AUTH_OAUTH",
    "AUTH_PASSWORD",
    "AUTH_TOKEN",
    "CONFIGURED",
    "Connection",
    "Connector",
    "ConnectorError",
    "ConnectorRegistry",
    "ConnectorStatus",
    "NEEDS_SETUP",
    "NotConfigured",
    "Operation",
    "READY",
    "UNAVAILABLE",
    "UNSUPPORTED",
    "Unsupported",
    "Vault",
    "default_registry",
    "describe_catalogue",
    "redact",
]
