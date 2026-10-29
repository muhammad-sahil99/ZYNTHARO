"""Google Workspace connectors: Gmail, Drive, Calendar, Sheets, Tasks, Contacts.

Google is the one provider where the honest story has to be explained rather than hidden, so this
module does it in one place:

* A desktop app is a **public client**. It cannot keep a client secret, so ORBYNTH never ships one;
  the customer creates their own OAuth client in Google Cloud (Desktop app type, five minutes) and
  pastes the client id. PKCE + a loopback redirect is the flow Google documents for exactly this.
* ``gmail.send`` is a *sensitive* scope, not a restricted one; reading mail (``gmail.readonly``) is
  restricted and, for an app distributed publicly, needs Google's verification (and a security
  assessment if mail content passes through a server). ORBYNTH processes mail **locally**, which is
  why the local-only design is not just a preference here — it is what keeps the product shippable.
* Every scope is requested individually and reported. If the customer only needs Drive, they can
  avoid mail scopes entirely and never trigger that review.

If a customer has no OAuth client and only needs mail, :mod:`orbynth.connectors.mail` does the same
job with an app password. Two doors, both real, and the app says which one is which.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from .base import AUTH_OAUTH, AUTH_NONE, Connector, ConnectorError, Field, Operation, READY
from .transport import HttpError, request_json

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

#: scope → what it unlocks, in the customer's words. Requested per connector, never all at once.
SCOPES: dict[str, str] = {
    "https://www.googleapis.com/auth/gmail.send": "send email as you",
    "https://www.googleapis.com/auth/gmail.readonly": "read your mail (restricted: needs Google verification for public distribution)",
    "https://www.googleapis.com/auth/gmail.modify": "read, label and archive mail (restricted)",
    "https://www.googleapis.com/auth/drive.file": "files you pick in this app",
    "https://www.googleapis.com/auth/drive": "everything in your Drive (restricted)",
    "https://www.googleapis.com/auth/spreadsheets": "read and edit your spreadsheets",
    "https://www.googleapis.com/auth/calendar": "read and create calendar events",
    "https://www.googleapis.com/auth/tasks": "your task lists",
    "https://www.googleapis.com/auth/contacts.readonly": "your contacts",
    "openid": "your account's basic identity",
    "email": "your email address",
}


@dataclass(slots=True)
class GoogleScopes:
    gmail_read: tuple[str, ...] = ("openid", "email", "https://www.googleapis.com/auth/gmail.readonly")
    gmail_send: tuple[str, ...] = ("openid", "email", "https://www.googleapis.com/auth/gmail.send")
    drive: tuple[str, ...] = ("openid", "email", "https://www.googleapis.com/auth/drive.file")
    calendar: tuple[str, ...] = ("openid", "email", "https://www.googleapis.com/auth/calendar")
    sheets: tuple[str, ...] = ("openid", "email", "https://www.googleapis.com/auth/spreadsheets")
    tasks: tuple[str, ...] = ("openid", "email", "https://www.googleapis.com/auth/tasks")
    contacts: tuple[str, ...] = ("openid", "email", "https://www.googleapis.com/auth/contacts.readonly")


def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode("ascii").rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


class _Catcher(BaseHTTPRequestHandler):
    """One-shot loopback listener for the OAuth redirect Google documents for native apps."""

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        self.server.captured = {k: v[0] for k, v in query.items()}  # type: ignore[attr-defined]
        body = b"Authorization received. You can close this tab and return to ORBYNTH."
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:  # silence the request log
        return


def authorize(
    client_id: str,
    scopes: tuple[str, ...],
    port: int = 0,
    open_browser: bool = True,
    timeout: float = 180.0,
    on_url: Any = None,
) -> dict[str, Any]:
    """Run the PKCE loopback flow and return a token set.

    Raises :class:`ConnectorError` with the provider's own error text when Google refuses, and a
    clear timeout message when the customer never finishes the consent screen.

    ``on_url`` is called with the consent address before the browser is opened. A graphical front
    end needs it for the case where ``webbrowser.open`` silently does nothing (a headless session,
    a locked-down default browser): the customer can then copy the link instead of waiting for a
    window that never appears.
    """
    if not client_id.strip():
        raise ConnectorError(
            "a Google OAuth client id is required. Create one at console.cloud.google.com → APIs & "
            "Services → Credentials → Create credentials → OAuth client ID → Desktop app."
        )
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(16)
    server = HTTPServer(("127.0.0.1", port), _Catcher)
    server.captured = {}  # type: ignore[attr-defined]
    redirect = f"http://127.0.0.1:{server.server_port}/callback"
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect,
            "scope": " ".join(scopes),
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "access_type": "offline",
            "prompt": "consent",
        }
    )
    url = f"{AUTH_URL}?{query}"
    if on_url is not None:
        try:
            on_url(url)
        except Exception:  # noqa: BLE001 - reporting the address must not break the flow
            pass
    if open_browser:
        try:
            import webbrowser

            webbrowser.open(url)
        except Exception:  # noqa: BLE001 - the URL is printed regardless
            pass
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    thread.join(timeout)
    server.server_close()
    captured: dict[str, Any] = getattr(server, "captured", {}) or {}
    if not captured:
        raise ConnectorError(
            f"no authorization arrived within {int(timeout)}s. Open this URL yourself and finish "
            f"the consent screen, then run the connect command again:\n{url}"
        )
    if captured.get("error"):
        raise ConnectorError(f"Google refused the authorization: {captured['error']} {captured.get('error_description', '')}".strip())
    if captured.get("state") != state:
        raise ConnectorError("the authorization response did not match this request (state mismatch); nothing was stored")
    code = captured.get("code", "")
    if not code:
        raise ConnectorError("Google returned no authorization code")
    return _exchange(client_id, code, verifier, redirect)


def _exchange(client_id: str, code: str, verifier: str, redirect: str) -> dict[str, Any]:
    import urllib.request

    body = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "code": code,
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        TOKEN_URL, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - normalize to one actionable message
        raise ConnectorError(f"the token exchange with Google failed: {exc}") from exc
    if not data.get("access_token"):
        raise ConnectorError(f"Google returned no access token: {data}")
    data["expires_at"] = time.time() + float(data.get("expires_in", 3600)) - 60
    return data


def refresh(client_id: str, refresh_token: str) -> dict[str, Any]:
    import urllib.request

    body = urllib.parse.urlencode(
        {"client_id": client_id, "refresh_token": refresh_token, "grant_type": "refresh_token"}
    ).encode("utf-8")
    request = urllib.request.Request(
        TOKEN_URL, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ConnectorError(
            f"the Google token could not be refreshed ({exc}). Reconnect the account; the refresh "
            "token was revoked or expired."
        ) from exc
    data["expires_at"] = time.time() + float(data.get("expires_in", 3600)) - 60
    if "refresh_token" not in data:
        data["refresh_token"] = refresh_token
    return data


class GoogleConnector(Connector):
    """A Google account, with the operations for the scopes that were actually granted."""

    name = "google"
    title = "Google (Gmail, Drive, Calendar, Sheets, Tasks, Contacts)"
    category = "mail"
    auth = AUTH_OAUTH
    needs = (
        "A Google OAuth client id (Desktop app type), created once in Google Cloud, plus consent "
        "for the scopes you want. No client secret is shipped with ORBYNTH."
    )
    where = "console.cloud.google.com → APIs & Services → Credentials → Create OAuth client ID → Desktop app."
    docs = "https://developers.google.com/identity/protocols/oauth2/native-app"

    #: Filled by the token itself; a connector is "ready" only for granted scopes.
    def __init__(self, connection=None, timeout: float = 30.0):
        super().__init__(connection)
        self.timeout = timeout

    # -- tokens -----------------------------------------------------------------------
    def _token(self) -> str:
        if self.connection is None:
            raise ConnectorError("Google is not connected yet; connect it in Settings → Connectors")
        values = self.connection.values
        token = values.get("access_token", "")
        expires = float(values.get("expires_at", "0") or 0)
        client_id = values.get("client_id", "")
        refresh_token = values.get("refresh_token", "")
        if token and time.time() < expires:
            return token
        if not refresh_token:
            raise ConnectorError(
                "the Google access token has expired and there is no refresh token; reconnect the account"
            )
        data = refresh(client_id, refresh_token)
        from .store import Vault  # local import: the vault is only needed when refreshing

        values["access_token"] = data["access_token"]
        values["expires_at"] = str(data["expires_at"])
        if data.get("refresh_token"):
            values["refresh_token"] = data["refresh_token"]
        vault = getattr(self, "vault", None) or Vault(os.environ.get("ORBYNTH_HOME", ""))
        vault.save(self.connection)
        return str(data["access_token"])

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token()}"}

    def _call(self, method: str, url: str, **kwargs: Any) -> Any:
        headers = {**self._headers(), **kwargs.pop("headers", {})}
        try:
            return request_json(method, url, headers=headers, timeout=self.timeout, **kwargs)
        except HttpError as exc:
            if exc.status == 403:
                raise ConnectorError(
                    f"Google refused this call ({exc}). The account may not have granted the scope "
                    f"this operation needs: {SCOPES.get(exc.body.get('error', {}).get('status', '') if isinstance(exc.body, dict) else '', '')}"
                ) from exc
            raise

    # -- contract ---------------------------------------------------------------------
    def fields(self) -> list[Field]:
        """One typed value (the client id) and one consent this app cannot type for you.

        The tokens are marked ``signin`` so no screen ever shows a field for them: they are
        returned by Google's consent flow and written straight to the vault.
        """
        return [
            Field("client_id", "OAuth client id (Desktop app type)", secret=False, hint=self.where),
            Field("refresh_token", "Consent from your Google account", signin=True, required=False),
        ]

    def operations(self) -> list[Operation]:
        return [
            Operation("whoami", "Which Google account is connected, and which scopes were granted."),
            Operation("gmail_list", "Recent messages matching a Gmail search query."),
            Operation("gmail_read", "One message: headers, plain-text body, attachment names."),
            Operation("gmail_send", "Send an email as the connected account.", "connector.send", "POST", "gmail/v1/users/me/messages/send", True),
            Operation("gmail_modify", "Add/remove labels on messages (archive = remove INBOX).", "connector.write", "POST", "gmail/v1/users/me/messages/modify", True),
            Operation("drive_list", "Files in Drive, with an optional query."),
            Operation("drive_download", "Download a Drive file to a local path.", "connector.write"),
            Operation("drive_upload", "Upload a local file to Drive.", "connector.write", "POST", "upload/drive/v3/files", True),
            Operation("calendar_events", "Upcoming events from a calendar."),
            Operation("calendar_create", "Create a calendar event.", "connector.write", "POST", "calendar/v3/calendars/primary/events", True),
            Operation("sheets_read", "Read a range from a spreadsheet."),
            Operation("sheets_append", "Append rows to a spreadsheet.", "connector.write", "POST", "sheets/v4:append", True),
            Operation("tasks_list", "Tasks in a task list."),
            Operation("contacts_list", "Contacts from the account."),
        ]

    def status(self):
        if self.connection is None:
            return self._needs_setup(
                "Google is not connected. You need an OAuth client id (Desktop app type) from "
                "Google Cloud, then consent once. Alternatively connect a mailbox with an app "
                "password under Providers → Mailbox (no Google Cloud project needed)."
            )
        if not self.connection.values.get("client_id"):
            return self._needs_setup("the Google connection is missing its client_id")
        granted = self.connection.values.get("scopes", "")
        account = self.connection.meta.get("account", "")
        if not self.connection.verified_at:
            return self._configured("credentials stored; run verify to prove the token works")
        return self._ready(
            f"Google connected as {account or 'the authorized account'}; scopes: {granted or 'unknown'}",
            account,
        )

    def verify(self):
        data = self._call("GET", USERINFO_URL)
        account = str(data.get("email", "")) if isinstance(data, dict) else ""
        scopes = str(self.connection.values.get("scopes", "")) if self.connection else ""
        return self._ready(f"Google verified as {account or 'unknown'}", account) if account else self._configured(
            f"Google answered but returned no email; scopes granted: {scopes}"
        )

    def call(self, operation: str, **args: Any) -> Any:
        self._operation(operation)
        handler = getattr(self, f"_op_{operation}", None)
        if handler is None:  # pragma: no cover
            raise ConnectorError(f"Google declares {operation} but has no implementation")
        return handler(**args)

    # -- Gmail ------------------------------------------------------------------------
    GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"

    def _op_whoami(self, **_: Any) -> dict[str, Any]:
        data = self._call("GET", USERINFO_URL)
        values = self.connection.values if self.connection else {}
        return {
            "email": data.get("email", ""),
            "name": data.get("name", ""),
            "scopes": values.get("scopes", ""),
            "client_id": values.get("client_id", ""),
        }

    def _op_gmail_list(self, query: str = "", limit: int = 20, **_: Any) -> dict[str, Any]:
        data = self._call(
            "GET",
            f"{self.GMAIL}/messages",
            params={"q": query, "maxResults": max(1, min(int(limit), 100))},
        )
        ids = [row.get("id") for row in (data.get("messages") or [])]
        rows = []
        for message_id in ids[: int(limit)]:
            detail = self._call(
                "GET",
                f"{self.GMAIL}/messages/{message_id}",
                params={"format": "metadata", "metadataHeaders": ["From", "To", "Subject", "Date"]},
            )
            headers = {
                str(h.get("name", "")).lower(): str(h.get("value", ""))
                for h in (detail.get("payload", {}) or {}).get("headers", [])
            }
            rows.append(
                {
                    "id": message_id,
                    "thread": detail.get("threadId", ""),
                    "from": headers.get("from", ""),
                    "to": headers.get("to", ""),
                    "subject": headers.get("subject", ""),
                    "date": headers.get("date", ""),
                    "snippet": detail.get("snippet", ""),
                    "labels": detail.get("labelIds", []),
                }
            )
        return {"query": query, "count": len(rows), "messages": rows}

    @staticmethod
    def _walk_parts(payload: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        text = ""
        attachments: list[dict[str, Any]] = []

        def visit(part: dict[str, Any]) -> None:
            nonlocal text
            body = part.get("body") or {}
            if part.get("filename"):
                attachments.append(
                    {"filename": part.get("filename"), "mime": part.get("mimeType", ""), "size": body.get("size", 0)}
                )
            if part.get("mimeType") == "text/plain" and body.get("data") and not text:
                try:
                    text = base64.urlsafe_b64decode(body["data"] + "===").decode("utf-8", "replace")
                except (ValueError, TypeError):
                    text = ""
            for child in part.get("parts") or []:
                visit(child)

        visit(payload)
        return text, attachments

    def _op_gmail_read(self, message_id: str, **_: Any) -> dict[str, Any]:
        if not str(message_id).strip():
            raise ConnectorError("gmail_read needs a message_id (from gmail_list)")
        detail = self._call("GET", f"{self.GMAIL}/messages/{message_id}", params={"format": "full"})
        headers = {
            str(h.get("name", "")).lower(): str(h.get("value", ""))
            for h in (detail.get("payload", {}) or {}).get("headers", [])
        }
        text, attachments = self._walk_parts(detail.get("payload", {}) or {})
        return {
            "id": message_id,
            "thread": detail.get("threadId", ""),
            "from": headers.get("from", ""),
            "to": headers.get("to", ""),
            "cc": headers.get("cc", ""),
            "subject": headers.get("subject", ""),
            "date": headers.get("date", ""),
            "body": text[:20000],
            "attachments": attachments,
            "labels": detail.get("labelIds", []),
        }

    def _op_gmail_send(self, to: str, subject: str, body: str, cc: str = "", html: bool = False, **_: Any) -> dict[str, Any]:
        if not str(to).strip():
            raise ConnectorError("gmail_send needs at least one recipient")
        from email.message import EmailMessage
        from email.utils import formataddr

        account = (self.connection.values.get("address", "") if self.connection else "") or "me"
        message = EmailMessage()
        message["From"] = formataddr(("", account)) if account != "me" else "me"
        message["To"] = to
        if cc:
            message["Cc"] = cc
        message["Subject"] = subject
        if html:
            message.set_content("This message contains HTML.")
            message.add_alternative(body, subtype="html")
        else:
            message.set_content(body)
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii").rstrip("=")
        result = self._call("POST", f"{self.GMAIL}/messages/send", body={"raw": raw})
        return {
            "sent": True,
            "to": to,
            "subject": subject,
            "id": result.get("id", ""),
            "thread": result.get("threadId", ""),
            "chars": len(body),
        }

    def _op_gmail_modify(
        self, message_ids: list[str], add: list[str] | None = None, remove: list[str] | None = None, **_: Any
    ) -> dict[str, Any]:
        if not message_ids:
            raise ConnectorError("gmail_modify needs at least one message id")
        modified = []
        for message_id in message_ids:
            self._call(
                "POST",
                f"{self.GMAIL}/messages/{message_id}/modify",
                body={"addLabelIds": list(add or []), "removeLabelIds": list(remove or [])},
            )
            modified.append(message_id)
        return {"modified": len(modified), "ids": modified, "added": list(add or []), "removed": list(remove or [])}

    # -- Drive ------------------------------------------------------------------------
    DRIVE = "https://www.googleapis.com/drive/v3"

    def _op_drive_list(self, query: str = "", limit: int = 25, **_: Any) -> dict[str, Any]:
        fields = "files(id,name,mimeType,modifiedTime,size,webViewLink,parents),nextPageToken"
        data = self._call(
            "GET",
            f"{self.DRIVE}/files",
            params={"q": query or None, "pageSize": max(1, min(int(limit), 1000)), "fields": fields},
        )
        return {"count": len(data.get("files") or []), "files": data.get("files") or [], "next": data.get("nextPageToken", "")}

    def _op_drive_download(self, file_id: str, dest: str, **_: Any) -> dict[str, Any]:
        import urllib.request
        from pathlib import Path

        if not str(file_id).strip():
            raise ConnectorError("drive_download needs a file_id (from drive_list)")
        url = f"{self.DRIVE}/files/{urllib.parse.quote(str(file_id))}?alt=media"
        request = urllib.request.Request(url, headers=self._headers())
        target = Path(str(dest)).expanduser()
        if target.is_dir():
            target = target / str(file_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                data = response.read()
        except Exception as exc:  # noqa: BLE001
            raise ConnectorError(
                f"Drive refused the download ({exc}). Google Docs/Sheets files have no binary form; "
                "export them instead, or download an uploaded file."
            ) from exc
        target.write_bytes(data)
        return {"path": str(target), "bytes": len(data), "verified": target.stat().st_size == len(data)}

    def _op_drive_upload(self, path: str, name: str = "", folder: str = "", **_: Any) -> dict[str, Any]:
        import urllib.request
        from pathlib import Path

        source = Path(str(path)).expanduser()
        if not source.is_file():
            raise ConnectorError(f"drive_upload cannot find {source}")
        metadata: dict[str, Any] = {"name": name or source.name}
        if folder:
            metadata["parents"] = [folder]
        boundary = "orbynth" + secrets.token_hex(8)
        body = (
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
            + json.dumps(metadata)
            + f"\r\n--{boundary}\r\nContent-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8") + source.read_bytes() + f"\r\n--{boundary}--\r\n".encode("utf-8")
        request = urllib.request.Request(
            "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart",
            data=body,
            headers={**self._headers(), "Content-Type": f"multipart/related; boundary={boundary}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                result = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise ConnectorError(f"Drive refused the upload: {exc}") from exc
        return {"uploaded": True, "id": result.get("id", ""), "name": result.get("name", ""), "bytes": source.stat().st_size}

    # -- Calendar ---------------------------------------------------------------------
    CAL = "https://www.googleapis.com/calendar/v3"

    def _op_calendar_events(self, calendar: str = "primary", days: int = 7, limit: int = 25, **_: Any) -> dict[str, Any]:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        later = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + max(1, days) * 86400))
        data = self._call(
            "GET",
            f"{self.CAL}/calendars/{urllib.parse.quote(str(calendar))}/events",
            params={
                "timeMin": now,
                "timeMax": later,
                "singleEvents": "true",
                "orderBy": "startTime",
                "maxResults": max(1, min(int(limit), 250)),
            },
        )
        rows = [
            {
                "id": item.get("id", ""),
                "summary": item.get("summary", ""),
                "start": (item.get("start") or {}).get("dateTime") or (item.get("start") or {}).get("date", ""),
                "end": (item.get("end") or {}).get("dateTime") or (item.get("end") or {}).get("date", ""),
                "attendees": [a.get("email") for a in item.get("attendees") or []],
                "location": item.get("location", ""),
                "link": item.get("htmlLink", ""),
            }
            for item in data.get("items") or []
        ]
        return {"calendar": calendar, "window_days": days, "count": len(rows), "events": rows}

    def _op_calendar_create(
        self,
        summary: str,
        start: str,
        end: str,
        calendar: str = "primary",
        description: str = "",
        attendees: list[str] | None = None,
        location: str = "",
        **_: Any,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "summary": summary,
            "description": description,
            "location": location,
            "start": {"dateTime": start, "timeZone": time.tzname[0]},
            "end": {"dateTime": end, "timeZone": time.tzname[0]},
        }
        if attendees:
            body["attendees"] = [{"email": a} for a in attendees]
        data = self._call(
            "POST",
            f"{self.CAL}/calendars/{urllib.parse.quote(str(calendar))}/events",
            body=body,
        )
        return {"created": True, "id": data.get("id", ""), "summary": data.get("summary", ""), "link": data.get("htmlLink", "")}

    # -- Sheets -----------------------------------------------------------------------
    SHEETS = "https://sheets.googleapis.com/v4/spreadsheets"

    def _op_sheets_read(self, spreadsheet_id: str, range_: str = "A1:Z100", range: str = "", **_: Any) -> dict[str, Any]:
        chosen = range or range_ or "A1:Z100"
        if not str(spreadsheet_id).strip():
            raise ConnectorError("sheets_read needs a spreadsheet_id (from the spreadsheet URL)")
        data = self._call(
            "GET",
            f"{self.SHEETS}/{urllib.parse.quote(str(spreadsheet_id))}/values/{urllib.parse.quote(chosen)}",
            params={"majorDimension": "ROWS"},
        )
        return {"range": data.get("range", chosen), "rows": data.get("values") or [], "count": len(data.get("values") or [])}

    def _op_sheets_append(self, spreadsheet_id: str, rows: list[list[Any]], range_: str = "Sheet1", range: str = "", **_: Any) -> dict[str, Any]:
        chosen = range or range_ or "Sheet1"
        if not rows:
            raise ConnectorError("sheets_append needs at least one row")
        data = self._call(
            "POST",
            f"{self.SHEETS}/{urllib.parse.quote(str(spreadsheet_id))}/values/{urllib.parse.quote(chosen)}:append",
            params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"},
            body={"values": rows},
        )
        updates = data.get("updates") or {}
        return {"appended": updates.get("updatedRows", len(rows)), "range": updates.get("updatedRange", ""), "verified": bool(updates)}

    # -- Tasks & Contacts --------------------------------------------------------------
    def _op_tasks_list(self, task_list: str = "@default", limit: int = 50, show_completed: bool = False, **_: Any) -> dict[str, Any]:
        data = self._call(
            "GET",
            f"https://tasks.googleapis.com/tasks/v1/lists/{urllib.parse.quote(str(task_list))}/tasks",
            params={"maxResults": max(1, min(int(limit), 100)), "showCompleted": "true" if show_completed else "false"},
        )
        rows = [
            {"id": t.get("id", ""), "title": t.get("title", ""), "due": t.get("due", ""), "status": t.get("status", ""), "notes": t.get("notes", "")}
            for t in data.get("items") or []
        ]
        return {"task_list": task_list, "count": len(rows), "tasks": rows}

    def _op_contacts_list(self, limit: int = 50, **_: Any) -> dict[str, Any]:
        data = self._call(
            "GET",
            "https://people.googleapis.com/v1/people/me/connections",
            params={
                "personFields": "names,emailAddresses,phoneNumbers,organizations",
                "pageSize": max(1, min(int(limit), 1000)),
            },
        )
        rows = []
        for person in data.get("connections") or []:
            names = person.get("names") or []
            emails = person.get("emailAddresses") or []
            phones = person.get("phoneNumbers") or []
            orgs = person.get("organizations") or []
            rows.append(
                {
                    "name": (names[0].get("displayName") if names else ""),
                    "email": (emails[0].get("value") if emails else ""),
                    "phone": (phones[0].get("value") if phones else ""),
                    "org": (orgs[0].get("name") if orgs else ""),
                }
            )
        return {"count": len(rows), "contacts": rows}
