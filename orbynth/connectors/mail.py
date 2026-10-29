"""Mail through IMAP and SMTP — the connector that works with no app registration at all.

Why this exists next to the Gmail and Microsoft Graph connectors: those two require an OAuth app
the customer must create (and, for Gmail read access, Google's restricted-scope review). This one
needs a single app password from the customer's own account settings, which takes about a minute
and involves no approval from anybody. For a business that wants "read this mailbox and file the
invoices", that is the shortest honest path to working on day one.

Supported: Gmail, iCloud, Yahoo, Zoho, Fastmail, GMX, Proton Bridge and any generic IMAP/SMTP
host. Outlook/Microsoft 365 is **not** offered here: Microsoft has disabled basic authentication
for its mailboxes, so the honest route is the ``microsoft`` connector's OAuth flow, and this
module refuses with that exact instruction instead of failing at login.

Real work, all stdlib: folder listing, IMAP search, header+body fetch with correct MIME decoding,
attachment extraction to disk, send with attachments, mark read/unread, move, and archive.
"""

from __future__ import annotations

import email
import email.policy
import imaplib
import os
import re
import smtplib
import ssl
import time
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, parsedate_to_datetime
from pathlib import Path
from typing import Any

from .base import (
    AUTH_PASSWORD,
    Connector,
    ConnectorError,
    NotConfigured,
    Operation,
    READY,
)


@dataclass(slots=True)
class Provider:
    key: str
    title: str
    imap_host: str
    imap_port: int
    smtp_host: str
    smtp_port: int
    note: str = ""
    oauth_required: bool = False


PROVIDERS: dict[str, Provider] = {
    "gmail": Provider(
        "gmail",
        "Gmail / Google Workspace",
        "imap.gmail.com",
        993,
        "smtp.gmail.com",
        587,
        "Create an App Password at myaccount.google.com → Security → 2-Step Verification → "
        "App passwords. A normal Google password will not work, by design.",
    ),
    "icloud": Provider(
        "icloud",
        "iCloud Mail",
        "imap.mail.me.com",
        993,
        "smtp.mail.me.com",
        587,
        "Create an app-specific password at appleid.apple.com → Sign-In and Security.",
    ),
    "yahoo": Provider("yahoo", "Yahoo Mail", "imap.mail.yahoo.com", 993, "smtp.mail.yahoo.com", 587,
                      "Generate an app password in Yahoo Account Security."),
    "zoho": Provider("zoho", "Zoho Mail", "imap.zoho.com", 993, "smtp.zoho.com", 587,
                     "Enable IMAP in Zoho Mail settings and create an app-specific password."),
    "fastmail": Provider("fastmail", "Fastmail", "imap.fastmail.com", 993, "smtp.fastmail.com", 587,
                         "Create an app password in Fastmail → Settings → Privacy & Security."),
    "gmx": Provider("gmx", "GMX", "imap.gmx.com", 993, "mail.gmx.com", 587,
                    "Enable IMAP/POP3 in GMX settings and use an app password."),
    "proton": Provider("proton", "Proton Mail (Bridge)", "127.0.0.1", 1143, "127.0.0.1", 1025,
                       "Install Proton Mail Bridge on the same PC and copy the IMAP/SMTP "
                       "credentials it shows. Bridge must be running."),
    "outlook": Provider("outlook", "Outlook / Microsoft 365", "outlook.office365.com", 993,
                        "smtp.office365.com", 587,
                        "Microsoft has disabled password sign-in for its mailboxes. Use the "
                        "'microsoft' connector (Graph OAuth) instead.",
                        oauth_required=True),
    "generic": Provider("generic", "Any IMAP/SMTP mailbox", "", 993, "", 587,
                        "Fill in the host names your mail provider documents."),
}


def _decode_header(value: str | None) -> str:
    if not value:
        return ""
    try:
        from email.header import decode_header, make_header

        return str(make_header(decode_header(value)))
    except Exception:  # noqa: BLE001 - a broken header must not lose the message
        return str(value)


def _text_body(message: email.message.Message, limit: int = 20000) -> str:
    """Prefer text/plain, fall back to stripped HTML. Never returns markup noise."""
    plain = ""
    html = ""
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_maintype() == "multipart":
                continue
            disposition = str(part.get("Content-Disposition", ""))
            if "attachment" in disposition.lower():
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                payload = part.get_payload(decode=True) or b""
                text = payload.decode(charset, "replace")
            except (LookupError, UnicodeDecodeError):
                continue
            if part.get_content_type() == "text/plain" and not plain:
                plain = text
            elif part.get_content_type() == "text/html" and not html:
                html = text
    else:
        charset = message.get_content_charset() or "utf-8"
        try:
            text = (message.get_payload(decode=True) or b"").decode(charset, "replace")
        except (LookupError, UnicodeDecodeError):
            text = ""
        if message.get_content_type() == "text/html":
            html = text
        else:
            plain = text
    body = plain or re.sub(r"<[^>]+>", " ", html)
    body = re.sub(r"[ \t]+\n", "\n", re.sub(r"\n{3,}", "\n\n", body))
    return body.strip()[:limit]


class MailConnector(Connector):
    name = "mail"
    title = "Mailbox (IMAP/SMTP)"
    category = "mail"
    auth = AUTH_PASSWORD
    needs = "The mailbox address and an app password from the provider's security settings."
    where = "Your mail provider's security page (see the provider note in the app)."
    docs = "https://support.google.com/accounts/answer/185833"

    def __init__(self, connection=None, provider: str = "", timeout: float = 30.0):
        super().__init__(connection)
        self.timeout = timeout
        self.provider_key = provider or (connection.meta.get("provider", "") if connection else "") or "generic"

    # -- configuration ---------------------------------------------------------------
    @property
    def provider(self) -> Provider:
        return PROVIDERS.get(self.provider_key, PROVIDERS["generic"])

    def _value(self, key: str) -> str:
        if self.connection is None:
            raise NotConfigured(
                "no mailbox is connected yet. Connect one with the app's Connectors screen, or: "
                "orbynth connectors connect mail --provider gmail --address you@example.com"
            )
        return self.connection.require(key)

    def _hosts(self) -> tuple[str, int, str, int]:
        values = self.connection.values if self.connection else {}
        provider = self.provider
        imap_host = values.get("imap_host") or provider.imap_host
        smtp_host = values.get("smtp_host") or provider.smtp_host
        if not imap_host or not smtp_host:
            raise ConnectorError(
                "this mailbox needs imap_host and smtp_host; the 'generic' provider has no defaults. "
                "Copy them from your provider's help page."
            )
        imap_port = int(values.get("imap_port") or provider.imap_port)
        smtp_port = int(values.get("smtp_port") or provider.smtp_port)
        return imap_host, imap_port, smtp_host, smtp_port

    # -- connection ------------------------------------------------------------------
    def _imap(self) -> imaplib.IMAP4_SSL:
        if self.provider.oauth_required:
            raise ConnectorError(self.provider.note)
        host, port, _, _ = self._hosts()
        address = self._value("address")
        password = self._value("password")
        try:
            client = imaplib.IMAP4_SSL(host, port, timeout=self.timeout, ssl_context=ssl.create_default_context())
            client.login(address, password)
        except imaplib.IMAP4.error as exc:
            raise ConnectorError(
                f"{self.provider.title} refused the login for {address}: {exc}. "
                f"{self.provider.note}"
            ) from exc
        except (OSError, ssl.SSLError) as exc:
            raise ConnectorError(f"cannot reach {host}:{port}: {exc}") from exc
        return client

    def _smtp(self) -> smtplib.SMTP:
        _, _, host, port = self._hosts()
        address = self._value("address")
        password = self._value("password")
        try:
            client: smtplib.SMTP
            if port == 465:
                client = smtplib.SMTP_SSL(host, port, timeout=self.timeout, context=ssl.create_default_context())
            else:
                client = smtplib.SMTP(host, port, timeout=self.timeout)
                client.starttls(context=ssl.create_default_context())
            client.login(address, password)
        except smtplib.SMTPAuthenticationError as exc:
            raise ConnectorError(
                f"{self.provider.title} refused the SMTP login for {address}: {exc.smtp_code} "
                f"{exc.smtp_error!r}. {self.provider.note}"
            ) from exc
        except (smtplib.SMTPException, OSError) as exc:
            raise ConnectorError(f"sending through {host}:{port} failed: {exc}") from exc
        return client

    # -- contract --------------------------------------------------------------------
    def operations(self) -> list[Operation]:
        return [
            Operation("list_folders", "Every folder and its message count.", path="imap", method="IMAP"),
            Operation("search", "Search messages in a folder by text, sender, subject or date.", path="imap", method="IMAP"),
            Operation("read", "One message: headers, decoded text body, attachment names.", path="imap", method="IMAP"),
            Operation("save_attachments", "Write a message's attachments into a folder.", capability="connector.write", path="imap", method="IMAP", mutates=True),
            Operation("send", "Send an email. This cannot be recalled.", capability="connector.send", path="smtp", method="SMTP", mutates=True),
            Operation("mark", "Mark messages seen/unseen or flagged.", capability="connector.write", path="imap", method="IMAP", mutates=True),
            Operation("move", "Move messages to another folder (this is how archiving works).", capability="connector.write", path="imap", method="IMAP", mutates=True),
        ]

    def status(self):
        if self.connection is None:
            provider_note = PROVIDERS["gmail"].note if self.provider_key == "gmail" else self.provider.note
            return self._needs_setup(
                "No mailbox connected. Connect one in Settings → Connectors; it needs only an "
                f"address and an app password. {provider_note}"
            )
        missing = [k for k in ("address", "password") if not self.connection.values.get(k)]
        if missing:
            return self._needs_setup(f"the mailbox connection is missing {', '.join(missing)}")
        if self.provider.oauth_required:
            return self._needs_setup(self.provider.note)
        if self.connection.verified_at:
            return self._ready(
                f"{self.provider.title} as {self.connection.values.get('address', '')}",
                self.connection.values.get("address", ""),
            )
        return self._configured("credentials stored; run verify to prove the login")

    def verify(self):
        client = self._imap()
        try:
            typ, data = client.select("INBOX", readonly=True)
            if typ != "OK":
                raise ConnectorError(f"IMAP accepted the login but INBOX is not readable: {data}")
            address = self.connection.values.get("address", "") if self.connection else ""
        finally:
            try:
                client.logout()
            except Exception:  # noqa: BLE001 - logout failures are noise
                pass
        return self._ready(f"{self.provider.title} verified as {address}", address)

    def call(self, operation: str, **args: Any) -> Any:
        self._operation(operation)
        handler = getattr(self, f"_op_{operation}", None)
        if handler is None:  # pragma: no cover - guard against a declaration without code
            raise ConnectorError(f"{self.title} declares {operation} but has no implementation")
        return handler(**args)

    # -- operations ------------------------------------------------------------------
    def _op_list_folders(self, **_: Any) -> dict[str, Any]:
        client = self._imap()
        try:
            typ, data = client.list()
            if typ != "OK":
                raise ConnectorError(f"could not list folders: {data}")
            folders = []
            for raw in data or []:
                line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
                match = re.match(r'\((?P<flags>[^)]*)\)\s+"(?P<delim>[^"]*)"\s+(?P<name>.+)', line)
                if not match:
                    continue
                name = match.group("name").strip().strip('"')
                count = -1
                typ, status = client.select(name, readonly=True)
                if typ == "OK" and status:
                    try:
                        count = int(status[0])
                    except (TypeError, ValueError):
                        count = -1
                folders.append({"name": name, "messages": count, "flags": match.group("flags")})
            return {"provider": self.provider_key, "folders": folders}
        finally:
            try:
                client.logout()
            except Exception:  # noqa: BLE001
                pass

    def _op_search(
        self,
        folder: str = "INBOX",
        text: str = "",
        sender: str = "",
        subject: str = "",
        since: str = "",
        unseen_only: bool = False,
        limit: int = 20,
        **_: Any,
    ) -> dict[str, Any]:
        criteria: list[str] = []
        if text:
            criteria += ["TEXT", f'"{text}"']
        if sender:
            criteria += ["FROM", f'"{sender}"']
        if subject:
            criteria += ["SUBJECT", f'"{subject}"']
        if since:
            criteria += ["SINCE", since]  # IMAP wants e.g. 01-Jan-2026
        if unseen_only:
            criteria += ["UNSEEN"]
        if not criteria:
            criteria = ["ALL"]
        client = self._imap()
        try:
            typ, _ = client.select(folder, readonly=True)
            if typ != "OK":
                raise ConnectorError(f"folder {folder!r} is not readable")
            typ, data = client.search(None, *criteria)
            if typ != "OK":
                raise ConnectorError(f"search failed: {data}")
            ids = (data[0] or b"").split()
            ids = ids[-max(1, min(int(limit), 200)) :] if ids else []
            if not ids:
                return {"folder": folder, "criteria": " ".join(criteria), "count": 0, "messages": []}
            rows = []
            for uid in reversed(ids):
                typ, fetched = client.fetch(uid, "(BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE)])")
                if typ != "OK" or not fetched:
                    continue
                raw = b""
                for chunk in fetched:
                    if isinstance(chunk, tuple) and len(chunk) > 1:
                        raw = chunk[1]
                        break
                message = email.message_from_bytes(raw, policy=email.policy.default)
                rows.append(
                    {
                        "id": uid.decode() if isinstance(uid, bytes) else str(uid),
                        "from": _decode_header(message.get("From")),
                        "to": _decode_header(message.get("To")),
                        "subject": _decode_header(message.get("Subject")),
                        "date": _decode_header(message.get("Date")),
                        "unseen": False,
                    }
                )
            return {"folder": folder, "criteria": " ".join(criteria), "count": len(rows), "messages": rows}
        finally:
            try:
                client.logout()
            except Exception:  # noqa: BLE001
                pass

    def _fetch_full(self, client: imaplib.IMAP4_SSL, message_id: str, folder: str) -> email.message.Message:
        typ, _ = client.select(folder, readonly=True)
        if typ != "OK":
            raise ConnectorError(f"folder {folder!r} is not readable")
        typ, data = client.fetch(str(message_id), "(RFC822)")
        if typ != "OK" or not data:
            raise ConnectorError(f"message {message_id} is not in {folder}")
        raw = b""
        for chunk in data:
            if isinstance(chunk, tuple) and len(chunk) > 1:
                raw = chunk[1]
                break
        if not raw:
            raise ConnectorError(f"message {message_id} came back empty")
        return email.message_from_bytes(raw, policy=email.policy.default)

    def _op_read(self, message_id: str, folder: str = "INBOX", **_: Any) -> dict[str, Any]:
        client = self._imap()
        try:
            message = self._fetch_full(client, message_id, folder)
            attachments = []
            for part in message.walk():
                disposition = str(part.get("Content-Disposition", "") or "")
                filename = part.get_filename()
                if filename and "attachment" in disposition.lower():
                    attachments.append({"filename": _decode_header(filename), "bytes": len(part.get_payload(decode=True) or b"")})
            return {
                "id": str(message_id),
                "folder": folder,
                "from": _decode_header(message.get("From")),
                "to": _decode_header(message.get("To")),
                "cc": _decode_header(message.get("Cc")),
                "subject": _decode_header(message.get("Subject")),
                "date": _decode_header(message.get("Date")),
                "body": _text_body(message),
                "attachments": attachments,
            }
        finally:
            try:
                client.logout()
            except Exception:  # noqa: BLE001
                pass

    def _op_save_attachments(
        self, message_id: str, dest: str, folder: str = "INBOX", **_: Any
    ) -> dict[str, Any]:
        client = self._imap()
        try:
            message = self._fetch_full(client, message_id, folder)
        finally:
            try:
                client.logout()
            except Exception:  # noqa: BLE001
                pass
        target = Path(dest).expanduser()
        target.mkdir(parents=True, exist_ok=True)
        saved = []
        for part in message.walk():
            disposition = str(part.get("Content-Disposition", "") or "")
            filename = part.get_filename()
            if not filename or "attachment" not in disposition.lower():
                continue
            safe = re.sub(r"[^A-Za-z0-9._-]+", "_", _decode_header(filename)).strip("._") or "attachment.bin"
            path = target / safe
            path.write_bytes(part.get_payload(decode=True) or b"")
            saved.append({"path": str(path), "bytes": path.stat().st_size})
        return {"message_id": str(message_id), "dest": str(target), "saved": saved, "count": len(saved)}

    def _op_send(
        self,
        to: str,
        subject: str,
        body: str,
        cc: str = "",
        attachments: list[str] | None = None,
        reply_to: str = "",
        **_: Any,
    ) -> dict[str, Any]:
        if not str(to).strip():
            raise ConnectorError("an email needs at least one recipient")
        address = self._value("address")
        display = (self.connection.values.get("display_name", "") if self.connection else "") or ""
        message = EmailMessage()
        message["From"] = formataddr((display, address)) if display else address
        message["To"] = to
        if cc:
            message["Cc"] = cc
        if reply_to:
            message["Reply-To"] = reply_to
        message["Subject"] = subject
        message.set_content(body)
        attached = []
        for item in attachments or []:
            path = Path(str(item)).expanduser()
            if not path.is_file():
                raise ConnectorError(f"attachment not found: {path}")
            data = path.read_bytes()
            message.add_attachment(
                data,
                maintype="application",
                subtype="octet-stream",
                filename=path.name,
            )
            attached.append({"path": str(path), "bytes": len(data)})
        client = self._smtp()
        try:
            client.send_message(message)
        finally:
            try:
                client.quit()
            except Exception:  # noqa: BLE001
                pass
        return {
            "sent": True,
            "from": address,
            "to": to,
            "cc": cc,
            "subject": subject,
            "chars": len(body),
            "attachments": attached,
            "sent_at": time.time(),
        }

    def _op_mark(self, message_ids: list[str], flag: str = "seen", folder: str = "INBOX", **_: Any) -> dict[str, Any]:
        flags = {"seen": "\\Seen", "unseen": "-\\Seen", "flagged": "\\Flagged", "unflagged": "-\\Flagged"}
        if flag not in flags:
            raise ConnectorError(f"flag must be one of {', '.join(flags)}, not {flag!r}")
        client = self._imap()
        try:
            typ, _ = client.select(folder)
            if typ != "OK":
                raise ConnectorError(f"folder {folder!r} is not writable")
            ids = ",".join(str(i) for i in message_ids) or "1:*"
            typ, data = client.store(ids, "+FLAGS" if not flags[flag].startswith("-") else "-FLAGS", f"({flags[flag].lstrip('-')})")
            if typ != "OK":
                raise ConnectorError(f"could not set {flag}: {data}")
            return {"folder": folder, "messages": len(message_ids), "flag": flag}
        finally:
            try:
                client.logout()
            except Exception:  # noqa: BLE001
                pass

    def _op_move(self, message_ids: list[str], dest: str, folder: str = "INBOX", **_: Any) -> dict[str, Any]:
        if not str(dest).strip():
            raise ConnectorError("a destination folder is required (for archiving, e.g. 'Archive')")
        client = self._imap()
        try:
            typ, _ = client.select(folder)
            if typ != "OK":
                raise ConnectorError(f"folder {folder!r} is not readable")
            ids = ",".join(str(i) for i in message_ids)
            if not ids:
                raise ConnectorError("no messages were named")
            typ, data = client.copy(ids, dest)
            if typ != "OK":
                raise ConnectorError(f"could not copy to {dest!r}: {data}")
            client.store(ids, "+FLAGS", "(\\Deleted)")
            client.expunge()
            return {"moved": len(message_ids), "from": folder, "to": dest}
        finally:
            try:
                client.logout()
            except Exception:  # noqa: BLE001
                pass


def provider_list() -> list[dict[str, Any]]:
    """The mail presets, with the honest note about which need OAuth instead."""
    return [
        {
            "key": p.key,
            "title": p.title,
            "imap": f"{p.imap_host}:{p.imap_port}" if p.imap_host else "you supply it",
            "smtp": f"{p.smtp_host}:{p.smtp_port}" if p.smtp_host else "you supply it",
            "note": p.note,
            "supported_here": not p.oauth_required,
        }
        for p in PROVIDERS.values()
    ]
