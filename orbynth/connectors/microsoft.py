"""Microsoft 365 connectors through Graph, using the device-code flow.

Why device code and not a redirect: it needs no client secret, no local web server and no
redirect port, which is exactly right for a desktop product installed on a machine behind a
firewall. The customer sees a short code, types it at ``microsoft.com/devicelogin``, and the app
receives the token. The app registration lives in the customer's own tenant (2 minutes, free), so
ORBYNTH ships no secret and the customer's admin stays in control.

Covers what a small office actually asks for: Outlook mail (read, search, send, move), Calendar,
OneDrive files, Excel workbook ranges, and Teams channel messages. Every one is a documented Graph
endpoint; nothing here invents an API.

Honest limits, reported rather than hidden: Teams and organisational mailboxes may require tenant
admin consent, and Graph throttling is per-resource, so ``429`` + ``Retry-After`` is respected by
the shared transport.
"""

from __future__ import annotations

import base64
import os
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

from .base import AUTH_DEVICE, Connector, ConnectorError, Field, Operation
from .transport import AuthError, HttpError, request_json

GRAPH = "https://graph.microsoft.com/v1.0"
DEVICE_CODE_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/devicecode"
TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

SCOPES: dict[str, str] = {
    "User.Read": "your profile",
    "Mail.Read": "read your mail",
    "Mail.ReadWrite": "read, move and label your mail",
    "Mail.Send": "send mail as you",
    "Calendars.ReadWrite": "read and create calendar events",
    "Files.ReadWrite": "read and write your OneDrive files",
    "Chat.Read": "read your Teams chats",
    "ChannelMessage.Read.All": "read Teams channel messages (admin consent usually required)",
    "ChannelMessage.Send": "post to Teams channels",
    "Contacts.Read": "your contacts",
    "Tasks.ReadWrite": "your To Do tasks",
}


@dataclass(slots=True)
class DeviceCode:
    user_code: str
    verification_uri: str
    device_code: str
    interval: float
    expires_in: float
    message: str

    def instruction(self) -> str:
        return (
            f"Open {self.verification_uri} and enter the code {self.user_code}. "
            f"{self.message}".strip()
        )


def start_device_flow(client_id: str, tenant: str, scopes: tuple[str, ...]) -> DeviceCode:
    if not client_id.strip():
        raise ConnectorError(
            "a Microsoft app (client) id is required. Register one free at portal.azure.com → "
            "Microsoft Entra ID → App registrations → New registration → allow public client flows, "
            "then paste the Application (client) ID."
        )
    body = urllib.parse.urlencode({"client_id": client_id, "scope": " ".join(scopes)}).encode()
    import urllib.request

    request = urllib.request.Request(
        DEVICE_CODE_URL.format(tenant=tenant or "common"),
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = __import__("json").loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ConnectorError(f"Microsoft did not start the device flow: {exc}") from exc
    return DeviceCode(
        user_code=str(data.get("user_code", "")),
        verification_uri=str(data.get("verification_uri", "https://microsoft.com/devicelogin")),
        device_code=str(data.get("device_code", "")),
        interval=float(data.get("interval", 5) or 5),
        expires_in=float(data.get("expires_in", 900) or 900),
        message=str(data.get("message", "")),
    )


def poll_device_flow(client_id: str, tenant: str, device_code: str, interval: float, expires_in: float) -> dict[str, Any]:
    """Wait for the customer to finish signing in. Bounded by the code's own lifetime."""
    import json as _json
    import urllib.request

    deadline = time.time() + min(expires_in, 900)
    body = urllib.parse.urlencode(
        {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": client_id,
            "device_code": device_code,
        }
    ).encode()
    last = ""
    while time.time() < deadline:
        request = urllib.request.Request(
            TOKEN_URL.format(tenant=tenant or "common"),
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = _json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:  # type: ignore[attr-defined]
            raw = exc.read().decode("utf-8", "replace")
            try:
                payload = _json.loads(raw)
            except ValueError:
                payload = {"error": raw}
            error = str(payload.get("error", ""))
            if error == "authorization_pending":
                time.sleep(interval)
                continue
            if error == "slow_down":
                interval += 5
                time.sleep(interval)
                continue
            raise ConnectorError(
                f"Microsoft refused the sign-in: {payload.get('error_description') or error}"
            ) from exc
        except Exception as exc:  # noqa: BLE001
            last = str(exc)
            time.sleep(interval)
            continue
        data["expires_at"] = time.time() + float(data.get("expires_in", 3600)) - 60
        return data
    raise ConnectorError(
        f"the sign-in was not completed in time ({last or 'no answer from Microsoft'}). Run the "
        "connect command again to get a fresh code."
    )


def refresh_token(client_id: str, tenant: str, refresh: str) -> dict[str, Any]:
    import json as _json
    import urllib.request

    body = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh,
            "scope": " ".join(("offline_access", "User.Read")),
        }
    ).encode()
    request = urllib.request.Request(
        TOKEN_URL.format(tenant=tenant or "common"),
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = _json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ConnectorError(
            f"the Microsoft token could not be refreshed ({exc}). Reconnect the account."
        ) from exc
    data["expires_at"] = time.time() + float(data.get("expires_in", 3600)) - 60
    data.setdefault("refresh_token", refresh)
    return data


class MicrosoftConnector(Connector):
    name = "microsoft"
    title = "Microsoft 365 (Outlook, Calendar, OneDrive, Teams)"
    category = "mail"
    auth = AUTH_DEVICE
    needs = (
        "A Microsoft app registration (free, in your own tenant) with public client flows enabled. "
        "No client secret is shipped, and some Teams scopes need tenant admin consent."
    )
    where = "portal.azure.com → Microsoft Entra ID → App registrations → New registration."
    docs = "https://learn.microsoft.com/en-us/graph/auth-v2-device-authorization"

    def __init__(self, connection=None, timeout: float = 45.0):
        super().__init__(connection)
        self.timeout = timeout

    # -- token ------------------------------------------------------------------------
    def _token(self) -> str:
        if self.connection is None:
            raise ConnectorError("Microsoft 365 is not connected yet")
        values = self.connection.values
        token = values.get("access_token", "")
        expires = float(values.get("expires_at", "0") or 0)
        if token and time.time() < expires:
            return token
        refresh = values.get("refresh_token", "")
        if not refresh:
            raise ConnectorError("the Microsoft token expired and there is no refresh token; reconnect")
        data = refresh_token(values.get("client_id", ""), values.get("tenant", "common"), refresh)
        values["access_token"] = data["access_token"]
        values["expires_at"] = str(data["expires_at"])
        if data.get("refresh_token"):
            values["refresh_token"] = data["refresh_token"]
        from .store import Vault

        (getattr(self, "vault", None) or Vault(os.environ.get("ORBYNTH_HOME", ""))).save(self.connection)
        return str(data["access_token"])

    def _call(self, method: str, url: str, **kwargs: Any) -> Any:
        headers = {"Authorization": f"Bearer {self._token()}", **kwargs.pop("headers", {})}
        try:
            return request_json(method, url, headers=headers, timeout=self.timeout, **kwargs)
        except AuthError as exc:
            raise ConnectorError(
                f"Microsoft refused this call: {exc}. The app registration may lack the delegated "
                "permission, or a tenant admin has not consented to it."
            ) from exc
        except HttpError as exc:
            if exc.status == 429:
                raise ConnectorError(
                    f"Microsoft is throttling this mailbox; wait {int(exc.retry_after or 30)}s and retry"
                ) from exc
            raise

    # -- contract ---------------------------------------------------------------------
    def fields(self) -> list[Field]:
        """The client id and tenant are typed; the token set arrives from the device-code sign-in.

        ``tenant`` defaults to ``common`` and is only needed when a company requires sign-in from
        one directory, which is exactly the case where guessing it would be wrong.
        """
        return [
            Field("client_id", "Application (client) id", secret=False, hint=self.where),
            Field(
                "tenant",
                "Directory (tenant) id or domain",
                secret=False,
                required=False,
                hint="leave blank for 'common'",
            ),
            Field("refresh_token", "Sign-in from your Microsoft account", signin=True, required=False),
        ]

    def operations(self) -> list[Operation]:
        return [
            Operation("whoami", "Which mailbox and tenant this connection belongs to."),
            Operation("mail_list", "Recent messages in a folder, newest first."),
            Operation("mail_read", "One message: headers, body text, attachment names."),
            Operation("mail_send", "Send mail as the connected account.", "connector.send", "POST", "me/sendMail", True),
            Operation("mail_move", "Move a message to another folder (archive, triage).", "connector.write", "POST", "me/messages/move", True),
            Operation("mail_folders", "Mail folders with unread counts."),
            Operation("calendar_events", "Upcoming calendar events."),
            Operation("calendar_create", "Create a calendar event.", "connector.write", "POST", "me/events", True),
            Operation("files_list", "Items in a OneDrive folder."),
            Operation("excel_read", "Rows from an Excel workbook range on OneDrive."),
            Operation("excel_append", "Append rows to an Excel table.", "connector.write", "POST", "me/drive/items", True),
            Operation("teams_channels", "Channels in a team."),
            Operation("teams_messages", "Recent messages in a channel."),
            Operation("teams_send", "Post a message to a channel.", "connector.send", "POST", "teams/channels/messages", True),
        ]

    def status(self):
        if self.connection is None:
            return self._needs_setup(
                "Microsoft 365 is not connected. Register a free app in your tenant (public client "
                "flows enabled), then sign in once with a device code. No client secret is used."
            )
        if not self.connection.values.get("client_id"):
            return self._needs_setup("the Microsoft connection is missing its client_id")
        account = self.connection.meta.get("account", "")
        if not self.connection.verified_at:
            return self._configured("credentials stored; run verify to prove the token works")
        return self._ready(
            f"Microsoft 365 connected as {account or 'the signed-in account'}", account
        )

    def verify(self):
        data = self._call("GET", f"{GRAPH}/me?$select=displayName,mail,userPrincipalName")
        account = str(data.get("mail") or data.get("userPrincipalName") or data.get("displayName") or "")
        return self._ready(f"Microsoft 365 verified as {account}", account)

    def call(self, operation: str, **args: Any) -> Any:
        self._operation(operation)
        handler = getattr(self, f"_op_{operation}", None)
        if handler is None:  # pragma: no cover
            raise ConnectorError(f"Microsoft declares {operation} but has no implementation")
        return handler(**args)

    # -- mail -------------------------------------------------------------------------
    def _op_whoami(self, **_: Any) -> dict[str, Any]:
        data = self._call("GET", f"{GRAPH}/me?$select=displayName,mail,userPrincipalName,jobTitle")
        values = self.connection.values if self.connection else {}
        return {
            "name": data.get("displayName", ""),
            "email": data.get("mail") or data.get("userPrincipalName", ""),
            "title": data.get("jobTitle", ""),
            "tenant": values.get("tenant", ""),
            "scopes": values.get("scopes", ""),
        }

    def _op_mail_folders(self, **_: Any) -> dict[str, Any]:
        data = self._call("GET", f"{GRAPH}/me/mailFolders?$top=50")
        rows = [
            {"id": f.get("id", ""), "name": f.get("displayName", ""), "unread": f.get("unreadItemCount", 0), "total": f.get("totalItemCount", 0)}
            for f in data.get("value") or []
        ]
        return {"count": len(rows), "folders": rows}

    def _op_mail_list(
        self, folder: str = "inbox", limit: int = 20, search: str = "", unread_only: bool = False, **_: Any
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "$top": max(1, min(int(limit), 100)),
            "$select": "id,subject,from,toRecipients,receivedDateTime,isRead,hasAttachments,bodyPreview",
            "$orderby": "receivedDateTime desc",
        }
        if search:
            params["$search"] = f'"{search}"'
        elif unread_only:
            params["$filter"] = "isRead eq false"
        data = self._call("GET", f"{GRAPH}/me/mailFolders/{urllib.parse.quote(str(folder))}/messages", params=params)
        rows = []
        for m in data.get("value") or []:
            sender = ((m.get("from") or {}).get("emailAddress") or {})
            rows.append(
                {
                    "id": m.get("id", ""),
                    "subject": m.get("subject", ""),
                    "from": f"{sender.get('name', '')} <{sender.get('address', '')}>".strip(),
                    "received": m.get("receivedDateTime", ""),
                    "unread": not m.get("isRead", True),
                    "has_attachments": bool(m.get("hasAttachments")),
                    "preview": m.get("bodyPreview", ""),
                }
            )
        return {"folder": folder, "count": len(rows), "messages": rows}

    def _op_mail_read(self, message_id: str, **_: Any) -> dict[str, Any]:
        if not str(message_id).strip():
            raise ConnectorError("mail_read needs a message_id (from mail_list)")
        data = self._call(
            "GET",
            f"{GRAPH}/me/messages/{urllib.parse.quote(str(message_id))}",
            params={"$select": "id,subject,from,toRecipients,ccRecipients,receivedDateTime,body,hasAttachments"},
        )
        body = (data.get("body") or {}).get("content", "")
        if (data.get("body") or {}).get("contentType") == "html":
            import re

            body = re.sub(r"<[^>]+>", " ", body)
            body = re.sub(r"\s{2,}", " ", body)
        attachments: list[dict[str, Any]] = []
        if data.get("hasAttachments"):
            listing = self._call("GET", f"{GRAPH}/me/messages/{urllib.parse.quote(str(message_id))}/attachments?$select=name,size,contentType")
            attachments = [
                {"filename": a.get("name", ""), "bytes": a.get("size", 0), "mime": a.get("contentType", "")}
                for a in listing.get("value") or []
            ]
        sender = ((data.get("from") or {}).get("emailAddress") or {})
        return {
            "id": str(message_id),
            "subject": data.get("subject", ""),
            "from": f"{sender.get('name', '')} <{sender.get('address', '')}>".strip(),
            "to": [((r.get("emailAddress") or {}).get("address", "")) for r in data.get("toRecipients") or []],
            "cc": [((r.get("emailAddress") or {}).get("address", "")) for r in data.get("ccRecipients") or []],
            "received": data.get("receivedDateTime", ""),
            "body": body.strip()[:20000],
            "attachments": attachments,
        }

    def _op_mail_send(
        self, to: str, subject: str, body: str, cc: str = "", attachments: list[str] | None = None, html: bool = False, **_: Any
    ) -> dict[str, Any]:
        if not str(to).strip():
            raise ConnectorError("mail_send needs at least one recipient")

        def recipients(raw: str) -> list[dict[str, Any]]:
            return [{"emailAddress": {"address": part.strip()}} for part in raw.split(",") if part.strip()]

        message: dict[str, Any] = {
            "subject": subject,
            "body": {"contentType": "HTML" if html else "Text", "content": body},
            "toRecipients": recipients(to),
        }
        if cc:
            message["ccRecipients"] = recipients(cc)
        attachments_payload = []
        for item in attachments or []:
            from pathlib import Path

            path = Path(str(item)).expanduser()
            if not path.is_file():
                raise ConnectorError(f"attachment not found: {path}")
            attachments_payload.append(
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": path.name,
                    "contentBytes": base64.b64encode(path.read_bytes()).decode("ascii"),
                }
            )
        if attachments_payload:
            message["attachments"] = attachments_payload
        self._call("POST", f"{GRAPH}/me/sendMail", body={"message": message, "saveToSentItems": True})
        # sendMail returns 202 with no body; Graph is the record of the send.
        return {"sent": True, "to": to, "cc": cc, "subject": subject, "chars": len(body), "attachments": len(attachments_payload), "sent_at": time.time()}

    def _op_mail_move(self, message_id: str, dest: str = "archive", **_: Any) -> dict[str, Any]:
        folders = self._op_mail_folders()["folders"]
        match = next((f for f in folders if f["name"].lower() == str(dest).lower() or f["id"] == dest), None)
        if match is None and len(str(dest)) < 40:
            raise ConnectorError(f"no mailbox folder named {dest!r}; known: {', '.join(f['name'] for f in folders)}")
        target = match["id"] if match else dest
        data = self._call(
            "POST",
            f"{GRAPH}/me/messages/{urllib.parse.quote(str(message_id))}/move",
            body={"destinationId": target},
        )
        return {"moved": True, "id": data.get("id", ""), "folder": data.get("parentFolderId", ""), "to": dest}

    # -- calendar ---------------------------------------------------------------------
    def _op_calendar_events(self, days: int = 7, limit: int = 25, **_: Any) -> dict[str, Any]:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        later = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + max(1, days) * 86400))
        data = self._call(
            "GET",
            f"{GRAPH}/me/calendarView",
            params={
                "startDateTime": now,
                "endDateTime": later,
                "$orderby": "start/dateTime",
                "$top": max(1, min(int(limit), 100)),
                "$select": "id,subject,start,end,location,attendees,webLink",
            },
        )
        rows = [
            {
                "id": e.get("id", ""),
                "subject": e.get("subject", ""),
                "start": (e.get("start") or {}).get("dateTime", ""),
                "end": (e.get("end") or {}).get("dateTime", ""),
                "location": ((e.get("location") or {}).get("displayName", "")),
                "attendees": [((a.get("emailAddress") or {}).get("address", "")) for a in e.get("attendees") or []],
                "link": e.get("webLink", ""),
            }
            for e in data.get("value") or []
        ]
        return {"window_days": days, "count": len(rows), "events": rows}

    def _op_calendar_create(
        self, subject: str, start: str, end: str, attendees: list[str] | None = None, body: str = "", location: str = "", **_: Any
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            "start": {"dateTime": start, "timeZone": time.tzname[0]},
            "end": {"dateTime": end, "timeZone": time.tzname[0]},
            "location": {"displayName": location},
        }
        if attendees:
            payload["attendees"] = [{"emailAddress": {"address": a}, "type": "required"} for a in attendees]
        data = self._call("POST", f"{GRAPH}/me/events", body=payload)
        return {"created": True, "id": data.get("id", ""), "subject": data.get("subject", ""), "link": data.get("webLink", "")}

    # -- files ------------------------------------------------------------------------
    def _op_files_list(self, folder: str = "", limit: int = 50, **_: Any) -> dict[str, Any]:
        path = f"/me/drive/root/children" if not folder else f"/me/drive/root:/{urllib.parse.quote(str(folder))}:/children"
        data = self._call(
            "GET",
            f"{GRAPH}{path}",
            params={"$top": max(1, min(int(limit), 200)), "$select": "id,name,size,lastModifiedDateTime,folder,file,webUrl"},
        )
        rows = [
            {
                "id": i.get("id", ""),
                "name": i.get("name", ""),
                "type": "folder" if i.get("folder") else "file",
                "bytes": i.get("size", 0),
                "modified": i.get("lastModifiedDateTime", ""),
                "url": i.get("webUrl", ""),
            }
            for i in data.get("value") or []
        ]
        return {"folder": folder or "root", "count": len(rows), "items": rows}

    def _op_excel_read(self, item_id: str, worksheet: str = "Sheet1", range_: str = "A1:Z100", **_: Any) -> dict[str, Any]:
        if not str(item_id).strip():
            raise ConnectorError("excel_read needs the OneDrive item_id of the workbook (from files_list)")
        url = (
            f"{GRAPH}/me/drive/items/{urllib.parse.quote(str(item_id))}/workbook/worksheets/"
            f"{urllib.parse.quote(str(worksheet))}/range(address='{range_}')"
        )
        data = self._call("GET", url)
        return {"worksheet": worksheet, "range": data.get("address", ""), "rows": data.get("values") or [], "count": len(data.get("values") or [])}

    def _op_excel_append(self, item_id: str, table: str, rows: list[list[Any]], **_: Any) -> dict[str, Any]:
        if not rows:
            raise ConnectorError("excel_append needs at least one row")
        columns = [{"index": idx, "values": [[row[idx]] for row in rows]} for idx in range(len(rows[0]))]
        url = (
            f"{GRAPH}/me/drive/items/{urllib.parse.quote(str(item_id))}/workbook/tables/"
            f"{urllib.parse.quote(str(table))}/rows/add"
        )
        data = self._call("POST", url, body={"index": None, "values": rows})
        return {"appended": len(rows), "index": data.get("index"), "verified": bool(data)}

    # -- teams ------------------------------------------------------------------------
    def _op_teams_channels(self, team_id: str, **_: Any) -> dict[str, Any]:
        if not str(team_id).strip():
            teams = self._call("GET", f"{GRAPH}/me/joinedTeams?$select=id,displayName")
            names = ", ".join(f"{t.get('displayName')} ({t.get('id')})" for t in teams.get("value") or [])
            raise ConnectorError(f"teams_channels needs a team_id. You are in: {names or 'no teams'}")
        data = self._call("GET", f"{GRAPH}/teams/{urllib.parse.quote(str(team_id))}/channels?$select=id,displayName,description")
        rows = [{"id": c.get("id", ""), "name": c.get("displayName", ""), "description": c.get("description", "")} for c in data.get("value") or []]
        return {"team": team_id, "count": len(rows), "channels": rows}

    def _op_teams_messages(self, team_id: str, channel_id: str, limit: int = 20, **_: Any) -> dict[str, Any]:
        data = self._call(
            "GET",
            f"{GRAPH}/teams/{urllib.parse.quote(str(team_id))}/channels/{urllib.parse.quote(str(channel_id))}/messages",
            params={"$top": max(1, min(int(limit), 50))},
        )
        rows = []
        for m in data.get("value") or []:
            sender = ((m.get("from") or {}).get("user") or {})
            rows.append(
                {
                    "id": m.get("id", ""),
                    "from": sender.get("displayName", ""),
                    "created": m.get("createdDateTime", ""),
                    "body": ((m.get("body") or {}).get("content", "") or "")[:4000],
                    "subject": m.get("subject", ""),
                }
            )
        return {"channel": channel_id, "count": len(rows), "messages": rows}

    def _op_teams_send(self, team_id: str, channel_id: str, text: str, **_: Any) -> dict[str, Any]:
        if not str(text).strip():
            raise ConnectorError("teams_send needs text")
        data = self._call(
            "POST",
            f"{GRAPH}/teams/{urllib.parse.quote(str(team_id))}/channels/{urllib.parse.quote(str(channel_id))}/messages",
            body={"body": {"contentType": "html", "content": text}},
        )
        return {"sent": True, "id": data.get("id", ""), "created": data.get("createdDateTime", "")}
