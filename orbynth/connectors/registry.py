"""The connector registry: every outside account, in its real state.

This is the module a customer's question lands on — "can it do my Gmail?" — and it answers in one
of exactly four states, never in marketing language:

* ``ready`` — connected, credential proven against the service.
* ``configured`` — credentials stored, not yet exercised. Honest about the difference.
* ``needs_setup`` — the exact next step, with the URL or setting name.
* ``unavailable`` — a business gate this build cannot pass (a platform review, an enterprise
  contract). The gate is quoted, so the answer is a decision instead of a maybe.

Connectors are built lazily from the vault on every call, so connecting an account in the app takes
effect without a restart, and revoking one (deleting the entry) cannot leave a live token in a
long-lived object.
"""

from __future__ import annotations

import os
from typing import Any

from .base import (
    AUTH_NONE,
    AUTH_TOKEN,
    UNAVAILABLE,
    READY,
    Connector,
    ConnectorError,
    ConnectorStatus,
    Unsupported,
    describe_catalogue,
)
from .google import GoogleConnector
from .mail import MailConnector, provider_list
from .microsoft import MicrosoftConnector
from .saas import SPECS, SaasConnector
from .signin import SignInManager
from .store import Vault

#: Gates that are not about code. Each is quoted to the customer as-is.
UNSUPPORTED: dict[str, str] = {
    "instagram": "Meta requires an Instagram Business account, a reviewed Meta app and the "
    "instagram_manage_messages permission. Until that review is granted, no desktop product can "
    "read a customer's DMs. Use the phone mirroring automation instead.",
    "facebook": "Meta requires a reviewed app with pages_messaging for page inboxes. Same review "
    "gate as Instagram.",
    "x": "X direct messages require a paid API tier (currently $200+/month for DM access). Not "
    "included in this build.",
    "linkedin": "LinkedIn does not license direct-message automation to third-party software at all.",
    "tiktok": "TikTok has no third-party direct-message API.",
    "snapchat": "Snapchat has no third-party messaging API.",
    "apple_notes": "Apple exposes no API for Notes. It is reachable only through the desktop "
    "automation path, not as a connector.",
}


class ConnectorRegistry:
    def __init__(self, vault: Vault | None = None, home: str = ""):
        self.vault = vault or Vault(home or os.environ.get("ORBYNTH_HOME", ""))
        self._extra_status: dict[str, ConnectorStatus] = {}
        #: The two connectors that finish with a consent screen rather than a pasted value. The
        #: manager lives here so the interface has one object to ask and one place sessions are
        #: kept — a second copy would drift on which profiles exist.
        self.signin = SignInManager(self)

    # -- construction -----------------------------------------------------------------
    def _factory(self, name: str, provider: str = "") -> Connector:
        key = str(name).strip().lower()
        connection = self.vault.get(key)
        if key == "google":
            connector = GoogleConnector(connection)
        elif key == "microsoft":
            connector = MicrosoftConnector(connection)
        elif key == "mail":
            # ``provider`` lets the settings screen ask "which mailbox?" before the form is built,
            # so a Gmail user is never shown IMAP host fields they do not have to know about.
            connector = MailConnector(connection, provider=provider)
        else:
            spec = next((s for s in SPECS if s.name == key), None)
            if spec is None:
                known = ", ".join(self.names())
                gate = UNSUPPORTED.get(key)
                if gate:
                    raise Unsupported(f"{key} is not available: {gate}")
                raise KeyError(f"unknown connector {name!r}; known connectors: {known}")
            connector = SaasConnector(spec, connection)
        connector.vault = self.vault  # type: ignore[attr-defined]
        return connector

    def names(self) -> list[str]:
        return sorted(["google", "microsoft", "mail", *[spec.name for spec in SPECS], *UNSUPPORTED])

    def connected_names(self) -> list[str]:
        return sorted(self.vault.load())

    def get(self, name: str, provider: str = "") -> Connector:
        return self._factory(name, provider)

    # -- state ------------------------------------------------------------------------
    def statuses(self) -> list[ConnectorStatus]:
        rows: list[ConnectorStatus] = []
        for name in self.names():
            if name in UNSUPPORTED:
                rows.append(ConnectorStatus(name, UNAVAILABLE, UNSUPPORTED[name], AUTH_NONE))
                continue
            try:
                rows.append(self._factory(name).status())
            except Unsupported as exc:
                rows.append(ConnectorStatus(name, UNAVAILABLE, str(exc), AUTH_NONE))
            except Exception as exc:  # noqa: BLE001 - a status check must never break the screen
                rows.append(
                    ConnectorStatus(
                        name,
                        "needs_setup",
                        f"the connector could not report its state: {type(exc).__name__}: {exc}",
                        AUTH_TOKEN,
                    )
                )
        return rows

    def to_json(self) -> list[dict[str, Any]]:
        return [row.to_json() for row in self.statuses()]

    def ready(self) -> list[str]:
        return [row.name for row in self.statuses() if row.state == READY]

    def catalogue(self) -> dict[str, Any]:
        """The settings screen and the sales page, from one call."""
        connectors: list[Connector] = []
        for name in self.names():
            if name in UNSUPPORTED:
                continue
            try:
                connectors.append(self._factory(name))
            except Exception:  # noqa: BLE001 - one broken connector must not hide the others
                continue
        data = describe_catalogue(connectors)
        data["unavailable"] = dict(UNSUPPORTED)
        data["mail_providers"] = provider_list()
        data["vault"] = {
            "path": str(self.vault.path),
            "at_rest_protected": self.vault.at_rest_protected,
            "note": self.vault.protection_note,
        }
        data["connected"] = self.vault.summary()
        data["signin_profiles"] = {
            name: self.signin.profiles(name) for name in ("google", "microsoft")
        }
        return data

    # -- mutation ---------------------------------------------------------------------
    def connect(self, name: str, values: dict[str, str], meta: dict[str, str] | None = None) -> ConnectorStatus:
        """Store a credential, then immediately prove it against the real service."""
        from .base import Connection
        import time

        connector = self._factory(name)
        existing = self.vault.get(connector.name) or Connection(name=connector.name)
        merged = {**existing.values, **{k: str(v) for k, v in values.items() if str(v).strip()}}
        connection = Connection(
            name=connector.name,
            values=merged,
            meta={**existing.meta, **{k: str(v) for k, v in (meta or {}).items()}},
            created_at=existing.created_at or time.time(),
        )
        self.vault.save(connection)
        connector = self._factory(name)
        try:
            status = connector.verify()
        except ConnectorError as exc:
            # The credential is stored: the customer can fix a typo without retyping everything.
            return ConnectorStatus(connector.name, "needs_setup", f"stored, but the service refused it: {exc}", connector.auth)
        self.vault.mark_verified(connector.name, status.account)
        return connector.status()

    def verify(self, name: str) -> ConnectorStatus:
        connector = self._factory(name)
        status = connector.verify()
        self.vault.mark_verified(connector.name, status.account)
        return connector.status()

    def disconnect(self, name: str) -> bool:
        return self.vault.remove(str(name).strip().lower())

    # -- calls ------------------------------------------------------------------------
    def call(self, name: str, operation: str, **args: Any) -> Any:
        connector = self._factory(name)
        # Validate the operation *before* the readiness check: "that operation does not exist" and
        # "this account is not connected" are different problems, and reporting the second when
        # the first is true sends the customer to connect an account they may already have.
        connector._operation(operation)
        status = connector.status()
        if status.state != READY:
            raise ConnectorError(
                f"{connector.title} is not ready ({status.state}): {status.detail}"
            )
        return connector.call(operation, **args)


def default_registry(home: str = "") -> ConnectorRegistry:
    return ConnectorRegistry(Vault(home or os.environ.get("ORBYNTH_HOME", "")))
