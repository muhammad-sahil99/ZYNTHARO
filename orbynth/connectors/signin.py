"""Interactive sign-in for the two connectors that cannot work from a pasted value alone.

Google and Microsoft both issue tokens through a consent screen, so a caller has to be told
*what to do* and then *when it finished*. Two flows, both documented for native apps and both
requiring no secret in the binary:

* **Google** — PKCE with a loopback redirect (:func:`orbynth.connectors.google.authorize`). The
  browser opens, the customer consents, the local listener catches the code. Reading mail is a
  *restricted* scope, so the profile chosen here decides whether the account is asking Google for
  a review-gated grant; that is stated in the profile's label rather than buried.
* **Microsoft** — device code (:func:`orbynth.connectors.microsoft.start_device_flow`). A short
  code is shown, the customer types it at ``microsoft.com/devicelogin``, and polling returns the
  tokens. No redirect port, so it works behind a firewall.

Design rules this module exists to keep:

1. **A sign-in is a session with a state, not a blocking call.** The interface polls
   :meth:`SignInManager.status`; nothing in the UI thread ever waits on a browser.
2. **The thread is bounded.** Every flow has a deadline in its own protocol (Google's ``timeout``,
   Microsoft's ``expires_in``), the session records when it started, and a stale session is
   reported as expired rather than left spinning.
3. **Tokens are written, never returned.** The session payload carries the *instruction* and the
   outcome. Access and refresh tokens go straight into the vault; they are not put in a status
   object, an event, or a log line.
4. **Scopes are chosen, not maximised.** Each profile is a documented group with its consequence
   written next to it, because "grant everything" is how a product ends up needing a security
   assessment it cannot pass.
"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from .base import Connection, ConnectorError, Field

PREPARING = "preparing"
WAITING = "waiting"
DONE = "done"
FAILED = "failed"


@dataclass(slots=True)
class ScopeProfile:
    """One consent request, with the consequence of granting it stated in the customer's terms."""

    key: str
    label: str
    scopes: tuple[str, ...]
    note: str = ""
    restricted: bool = False

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def _google_profiles() -> list[ScopeProfile]:
    from .google import GoogleScopes

    groups = GoogleScopes()
    return [
        ScopeProfile(
            "mail_read",
            "Read mail",
            groups.gmail_read,
            "Reading Gmail is a restricted scope: a public release needs Google's verification. "
            "ORBYNTH reads the mailbox locally, which is what keeps this shippable.",
            restricted=True,
        ),
        ScopeProfile(
            "mail_full",
            "Read, organise and send mail",
            groups.gmail_read + groups.gmail_send,
            "Adds sending, which is a sensitive scope (no verification review) and cannot be recalled.",
            restricted=True,
        ),
        ScopeProfile("drive", "Drive files", groups.drive, "Only files you pick with this app."),
        ScopeProfile("calendar", "Calendar", groups.calendar),
        ScopeProfile("sheets", "Spreadsheets", groups.sheets),
        ScopeProfile("tasks", "Tasks", groups.tasks),
        ScopeProfile("contacts", "Contacts", groups.contacts),
    ]


def _microsoft_profiles() -> list[ScopeProfile]:
    core = (
        "offline_access",
        "User.Read",
        "Mail.Read",
        "Mail.ReadWrite",
        "Mail.Send",
        "Calendars.ReadWrite",
        "Files.ReadWrite",
        "Contacts.Read",
        "Tasks.ReadWrite",
    )
    teams = (
        "offline_access",
        "User.Read",
        "Chat.Read",
        "ChannelMessage.Read.All",
        "ChannelMessage.Send",
    )
    return [
        ScopeProfile(
            "desk",
            "Mail, calendar, files, contacts and tasks",
            core,
            "Mail.Send is included: the owner still approves each send, an agent cannot send alone.",
        ),
        ScopeProfile(
            "teams",
            "Teams chats and channels",
            teams,
            "Channel message scopes usually need a tenant admin to consent.",
        ),
        ScopeProfile(
            "files",
            "Profile and OneDrive files only",
            ("offline_access", "User.Read", "Files.ReadWrite"),
            "The smallest grant that still does useful work.",
        ),
    ]


@dataclass(slots=True)
class SignInSession:
    """What the interface needs to show, and nothing it must not."""

    connector: str
    stage: str = PREPARING
    profile: str = ""
    instruction: str = ""
    user_code: str = ""
    verification_uri: str = ""
    url: str = ""
    detail: str = ""
    state: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    def done(self) -> bool:
        return self.stage in {DONE, FAILED}


class SignInManager:
    """Runs one interactive sign-in per connector, in the background, and reports its progress.

    The flow functions are injectable so the session logic — deadlines, state transitions, token
    writing — is tested without a browser and without a Microsoft tenant. A test that had to open
    a real consent screen would be a test nobody runs.
    """

    def __init__(
        self,
        registry: Any,
        device_flow: Callable[..., Any] | None = None,
        poll_flow: Callable[..., Any] | None = None,
        browser_flow: Callable[..., Any] | None = None,
        on_change: Callable[[SignInSession], None] | None = None,
    ):
        self.registry = registry
        self._device = device_flow
        self._poll = poll_flow
        self._browser = browser_flow
        self.on_change = on_change
        self._sessions: dict[str, SignInSession] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

    # -- profiles ---------------------------------------------------------------------
    def profiles(self, connector: str) -> list[dict[str, Any]]:
        name = str(connector).strip().lower()
        if name == "google":
            return [profile.to_json() for profile in _google_profiles()]
        if name == "microsoft":
            return [profile.to_json() for profile in _microsoft_profiles()]
        return []

    def supported(self, connector: str) -> bool:
        return bool(self.profiles(connector))

    def _profile(self, connector: str, key: str) -> ScopeProfile:
        profiles = self.profiles(connector)
        if not profiles:
            raise ConnectorError(
                f"{connector} does not use an interactive sign-in: it takes a value you paste "
                "(a token or an app password)."
            )
        chosen = next((p for p in profiles if p["key"] == key), None) or profiles[0]
        return ScopeProfile(
            key=chosen["key"],
            label=chosen["label"],
            scopes=tuple(chosen["scopes"]),
            note=chosen.get("note", ""),
            restricted=bool(chosen.get("restricted", False)),
        )

    # -- session control --------------------------------------------------------------
    def status(self, connector: str) -> SignInSession | None:
        return self._sessions.get(str(connector).strip().lower())

    def sessions(self) -> list[dict[str, Any]]:
        return [session.to_json() for session in self._sessions.values()]

    def start(self, connector: str, profile: str = "", timeout: float = 240.0) -> SignInSession:
        name = str(connector).strip().lower()
        if not self.supported(name):
            raise ConnectorError(
                f"{name} has no interactive sign-in. Paste its token instead — that is the whole "
                "setup for it."
            )
        with self._lock:
            running = self._threads.get(name)
            if running is not None and running.is_alive():
                # One live session per connector: two consent screens for one account is how a
                # customer ends up with two half-granted token sets and no idea which is which.
                return self._sessions[name]
            chosen = self._profile(name, profile)
            session = SignInSession(connector=name, profile=chosen.key)
            session.instruction = self._preview(name, chosen)
            self._sessions[name] = session
            thread = threading.Thread(target=self._run, args=(name, chosen, timeout), name=f"signin-{name}", daemon=True)
            self._threads[name] = thread
        self._notify(session)
        thread.start()
        return session

    def cancel(self, connector: str) -> bool:
        session = self._sessions.get(str(connector).strip().lower())
        if session is None or session.done():
            return False
        session.stage = FAILED
        session.detail = "cancelled by the owner"
        session.finished_at = time.time()
        self._notify(session)
        return True

    # -- the flow ---------------------------------------------------------------------
    def _preview(self, name: str, profile: ScopeProfile) -> str:
        return (
            f"Requesting: {profile.label}. {profile.note}".strip()
            if name == "microsoft"
            else f"A browser window opens for {profile.label}. {profile.note}".strip()
        )

    def _run(self, name: str, profile: ScopeProfile, timeout: float) -> None:
        session = self._sessions[name]
        try:
            connection = self.registry.vault.get(name) or Connection(name=name)
            if name == "microsoft":
                tokens = self._microsoft(connection, profile, session, timeout)
            else:
                tokens = self._google(connection, profile, session, timeout)
            connection.values = {**connection.values, **{k: str(v) for k, v in tokens.items() if v}}
            connection.name = name
            self.registry.vault.save(connection)
            status = self.registry.verify(name)
            session.stage = DONE
            session.state = status.state
            session.detail = status.detail
            session.finished_at = time.time()
        except ConnectorError as exc:
            session.stage = FAILED
            session.detail = str(exc)
            session.finished_at = time.time()
        except Exception as exc:  # noqa: BLE001 - a crashed flow must land as a failed session
            session.stage = FAILED
            session.detail = f"{type(exc).__name__}: {exc}"
            session.finished_at = time.time()
        finally:
            self._notify(session)

    def _microsoft(self, connection: Connection, profile: ScopeProfile, session: SignInSession, timeout: float) -> dict[str, Any]:
        client_id = connection.values.get("client_id", "")
        tenant = connection.values.get("tenant", "") or "common"
        if not client_id:
            raise ConnectorError(
                "paste the Application (client) id first — the sign-in needs it before Microsoft "
                "can show you a code."
            )
        start = self._device or self._default_device_flow()
        poll = self._poll or self._default_poll_flow()
        code = start(client_id, tenant, profile.scopes)
        session.stage = WAITING
        session.user_code = getattr(code, "user_code", "")
        session.verification_uri = getattr(code, "verification_uri", "https://microsoft.com/devicelogin")
        session.instruction = getattr(code, "instruction", lambda: "")() or (
            f"Open {session.verification_uri} and enter {session.user_code}."
        )
        self._notify(session)
        expires = float(min(float(getattr(code, "expires_in", 900) or 900), timeout))
        return poll(client_id, tenant, getattr(code, "device_code", ""), float(getattr(code, "interval", 5) or 5), expires)

    def _google(self, connection: Connection, profile: ScopeProfile, session: SignInSession, timeout: float) -> dict[str, Any]:
        client_id = connection.values.get("client_id", "")
        if not client_id:
            raise ConnectorError(
                "paste the OAuth client id first — Google's consent screen is tied to it."
            )
        flow = self._browser or self._default_browser_flow()
        session.stage = WAITING
        session.instruction = (
            f"Consent for {profile.label}: a browser window was opened. If it did not appear, open "
            "the address below yourself."
        )
        self._notify(session)

        def remember(address: str) -> None:
            session.url = str(address)
            self._notify(session)

        tokens = flow(client_id, profile.scopes, timeout=timeout, on_url=remember)
        if not tokens.get("access_token"):
            raise ConnectorError("Google returned no access token; the consent screen was not completed")
        values = dict(tokens)
        values["scopes"] = " ".join(profile.scopes)
        return values

    # -- defaults (imported lazily so the suite runs without network modules loaded) ----
    def _default_device_flow(self) -> Callable[..., Any]:
        from .microsoft import start_device_flow

        return start_device_flow

    def _default_poll_flow(self) -> Callable[..., Any]:
        from .microsoft import poll_device_flow

        return poll_device_flow

    def _default_browser_flow(self) -> Callable[..., Any]:
        from .google import authorize

        return authorize

    def _notify(self, session: SignInSession) -> None:
        if self.on_change is None:
            return
        try:
            self.on_change(session)
        except Exception:  # noqa: BLE001 - an interface callback must never break the flow
            pass


def describe_signin_fields(connector: str) -> list[Field]:
    """Which fields a sign-in satisfies, so the screen can label them instead of hiding them."""
    if connector == "google":
        return [Field("refresh_token", "Google consent", signin=True, required=False)]
    if connector == "microsoft":
        return [Field("refresh_token", "Microsoft sign-in", signin=True, required=False)]
    return []
