"""The connector contract: one outside account, and the truth about whether it works.

A "connector" in this product is not a logo on a settings page. It is one customer account that
ORBYNTH can read, and a fixed list of operations it can perform there. Three rules keep those
promises from rotting:

1. **The credential is the customer's own.** A desktop product cannot safely ship a client secret
   (a native app is a *public* client — anything in the binary is published). So every connector
   either takes a token the customer generates in their own account in a minute, or performs a
   standard OAuth/device-code flow against an app registration the customer owns. Nothing here
   asks for a password it does not need, and nothing embeds a secret.
2. **Every operation is declared before it can be called.** ``call`` only accepts a name from
   :meth:`Connector.operations`. A model cannot invent an endpoint, and the *unavailable* list is
   as visible as the working one.
3. **State is reported, never assumed.** ``needs_setup`` carries the exact next step (a URL, a
   setting name, a scope). ``unsupported`` carries the business gate that blocks it. A connector
   that cannot work in this build says so instead of failing halfway through a task.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

READY = "ready"
CONFIGURED = "configured"
NEEDS_SETUP = "needs_setup"
UNAVAILABLE = "unavailable"

#: How a connector authenticates. Reported to the UI and to the docs verbatim.
AUTH_OAUTH = "oauth2"
AUTH_PASSWORD = "app_password"  # IMAP/SMTP with an app-specific password
AUTH_TOKEN = "api_token"  # a bearer token the customer generates
AUTH_KEY_TOKEN = "key_and_token"  # two values, e.g. Trello key + token
AUTH_DEVICE = "device_code"  # OAuth device flow: no secret, no redirect port
AUTH_NONE = "none"


class ConnectorError(RuntimeError):
    """A connector call failed. The text is written for the customer, not for a log parser."""


class NotConfigured(ConnectorError):
    """The account has not been connected yet. Carries the exact instruction that fixes it."""


class Unsupported(ConnectorError):
    """This build cannot reach that platform. Carries the business gate, not an excuse."""


@dataclass(slots=True)
class ConnectorStatus:
    name: str
    state: str
    detail: str
    auth: str = AUTH_TOKEN
    account: str = ""
    verified_at: float = 0.0
    operations: int = 0

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Operation:
    """One callable thing on a connected account.

    ``capability`` is the gate the action layer enforces (``connector.read``,
    ``connector.write``, ``connector.send``). Anything that sends is ``connector.send`` because
    a delivered message or email cannot be recalled.
    """

    name: str
    description: str
    capability: str = "connector.read"
    method: str = "GET"
    path: str = ""
    mutates: bool = False

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Field:
    """One value the customer has to supply before an account works.

    Declared rather than inferred from the prose in ``needs``: the settings screen renders exactly
    these inputs, and a connector that wants something else does not exist. ``secret`` decides
    masking in the interface only — the vault masks everything it prints regardless, because a
    field being "not secret" is not permission to write it into a log.

    ``signin`` is the honest third option: the value is not typed at all, it comes back from an
    interactive consent (Google's loopback flow, Microsoft's device code).
    """

    name: str
    label: str
    secret: bool = True
    hint: str = ""
    required: bool = True
    signin: bool = False

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Connection:
    """What the customer typed in, kept in the local vault and never logged."""

    name: str
    values: dict[str, str] = field(default_factory=dict)
    meta: dict[str, str] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    verified_at: float = 0.0

    def require(self, key: str) -> str:
        value = self.values.get(key, "")
        if not str(value).strip():
            raise NotConfigured(f"{self.name} is missing '{key}'")
        return str(value)


class Connector:
    """Base class. A subclass either reaches the account or refuses with a reason."""

    #: Stable key used by actions, the CLI and the settings screen.
    name: str = ""
    #: What a customer calls it.
    title: str = ""
    #: ``mail`` | ``files`` | ``calendar`` | ``chat`` | ``crm`` | ``work`` | ``payments`` | ``code``
    category: str = "work"
    auth: str = AUTH_TOKEN
    #: Exactly what the customer must have before this works, in their words.
    needs: str = ""
    #: Where the credential comes from, or why there is no path at all.
    where: str = ""
    docs: str = ""

    def __init__(self, connection: Connection | None = None):
        self.connection = connection

    # -- contract ---------------------------------------------------------------------
    def operations(self) -> list[Operation]:
        """The complete, fixed surface. Unknown names are refused rather than guessed."""
        return []

    def status(self) -> ConnectorStatus:
        raise NotImplementedError

    def call(self, operation: str, **args: Any) -> Any:
        raise NotImplementedError

    def verify(self) -> ConnectorStatus:
        """Prove the credential works against the real service. Default: report only."""
        return self.status()

    def fields(self) -> list[Field]:
        """Exactly what the interface must ask for. A connector with no fields takes none."""
        return []

    # -- helpers ----------------------------------------------------------------------
    def _status(self, state: str, detail: str, account: str = "") -> ConnectorStatus:
        connection = self.connection
        return ConnectorStatus(
            name=self.name,
            state=state,
            detail=detail,
            auth=self.auth,
            account=account or (connection.meta.get("account", "") if connection else ""),
            verified_at=connection.verified_at if connection else 0.0,
            operations=len(self.operations()),
        )

    def _ready(self, detail: str, account: str = "") -> ConnectorStatus:
        return self._status(READY, detail, account)

    def _needs_setup(self, detail: str) -> ConnectorStatus:
        return self._status(NEEDS_SETUP, detail)

    def _configured(self, detail: str) -> ConnectorStatus:
        return self._status(CONFIGURED, detail)

    def _operation(self, name: str) -> Operation:
        for operation in self.operations():
            if operation.name == name:
                return operation
        known = ", ".join(sorted(op.name for op in self.operations())) or "none"
        raise ConnectorError(
            f"{self.title or self.name} has no operation {name!r}. Available: {known}"
        )


def describe_catalogue(connectors: list[Connector]) -> dict[str, Any]:
    """Everything the settings screen and the sales page need, from one source."""
    rows = []
    for connector in connectors:
        status = connector.status()
        rows.append(
            {
                **status.to_json(),
                "title": connector.title,
                "category": connector.category,
                "needs": connector.needs,
                "where": connector.where,
                "docs": connector.docs,
                "operation_list": [op.to_json() for op in connector.operations()],
                "fields": [f.to_json() for f in connector.fields()],
            }
        )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["category"], []).append(row)
    return {
        "total": len(rows),
        "ready": sum(1 for row in rows if row["state"] == READY),
        "needs_setup": sum(1 for row in rows if row["state"] == NEEDS_SETUP),
        "unavailable": sum(1 for row in rows if row["state"] == UNAVAILABLE),
        "categories": grouped,
    }
