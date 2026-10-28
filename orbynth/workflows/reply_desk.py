"""Workflow 2: reply desk.

Inbound customer messages are collected, a draft reply is prepared, and the draft **waits for the
owner**. This is not a limitation to be removed later: an agent that can send messages to a
customer on its own is a liability the first time it is wrong, and no small business will accept
that risk on day one. Approval is one click in the app or one CLI command.

Everything is durable in SQLite, so a crash loses no draft, and a message is never answered twice:
inbound rows are unique per platform message id, and each inbound row can produce exactly one
draft.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .base import DONE, FAILED, OK, PARTIAL, SKIPPED, Workflow, WorkflowError, WorkflowResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS inbound (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,
    chat TEXT NOT NULL,
    message_id TEXT NOT NULL,
    sender TEXT DEFAULT '',
    text TEXT NOT NULL,
    ts REAL DEFAULT 0,
    collected_at REAL DEFAULT 0,
    UNIQUE(channel, message_id)
);
CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    inbound_id INTEGER NOT NULL UNIQUE,
    channel TEXT NOT NULL,
    chat TEXT NOT NULL,
    reply TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending_approval',
    created_at REAL DEFAULT 0,
    decided_at REAL DEFAULT 0,
    error TEXT DEFAULT '',
    FOREIGN KEY (inbound_id) REFERENCES inbound(id)
);
"""

PENDING = "pending_approval"
SENT = "sent"
REJECTED = "rejected"
SEND_FAILED = "send_failed"


class ReplyDesk(Workflow):
    name = "reply_desk"
    title = "Reply desk"
    description = (
        "Collect customer messages from a connected channel and prepare replies. Nothing is sent "
        "until you approve it; every draft is stored and traceable."
    )
    needs = "A connected channel (Telegram or WhatsApp) and a reply style the drafter can follow."

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    # -- collection ------------------------------------------------------------------
    def collect(self, channel: Any, chat: str = "", limit: int = 20) -> int:
        """Pull new inbound messages. Returns how many rows were new."""
        messages = channel.inbox(limit=limit, chat=chat)
        added = 0
        with self._lock, self._connect() as conn:
            for message in messages:
                if not str(getattr(message, "text", "")).strip():
                    continue
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO inbound (channel, chat, message_id, sender, text, ts, collected_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        channel.name,
                        getattr(message, "chat", chat),
                        getattr(message, "message_id", ""),
                        getattr(message, "sender", ""),
                        message.text,
                        float(getattr(message, "ts", 0) or 0),
                        time.time(),
                    ),
                )
                added += cursor.rowcount or 0
        return added

    def undrafted(self, channel_name: str = "", limit: int = 50) -> list[dict[str, Any]]:
        query = (
            "SELECT i.* FROM inbound i LEFT JOIN drafts d ON d.inbound_id = i.id "
            "WHERE d.id IS NULL"
        )
        params: list[Any] = []
        if channel_name:
            query += " AND i.channel = ?"
            params.append(channel_name)
        query += " ORDER BY i.id LIMIT ?"
        params.append(max(1, int(limit)))
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(query, params)]

    # -- drafting --------------------------------------------------------------------
    def draft(self, drafter: Callable[[dict[str, Any]], str], channel_name: str = "", limit: int = 50) -> list[dict[str, Any]]:
        """Create pending drafts. A drafter that returns nothing leaves the row undrafted."""
        created: list[dict[str, Any]] = []
        for row in self.undrafted(channel_name, limit):
            try:
                reply = (drafter(row) or "").strip()
            except Exception as exc:  # noqa: BLE001 - one bad message must not stop the batch
                reply = ""
                row = {**row, "draft_error": f"{type(exc).__name__}: {exc}"}
            if not reply:
                continue
            with self._lock, self._connect() as conn:
                cursor = conn.execute(
                    "INSERT INTO drafts (inbound_id, channel, chat, reply, status, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (row["id"], row["channel"], row["chat"], reply, PENDING, time.time()),
                )
                created.append({"draft_id": cursor.lastrowid, "channel": row["channel"], "chat": row["chat"], "reply": reply})
        return created

    # -- the owner's decision --------------------------------------------------------
    def pending(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT d.*, i.text AS inbound_text, i.sender AS sender FROM drafts d "
                "JOIN inbound i ON i.id = d.inbound_id WHERE d.status = ? ORDER BY d.id LIMIT ?",
                (PENDING, max(1, int(limit))),
            )
            return [dict(row) for row in rows]

    def approve(self, draft_id: int, channel: Any) -> dict[str, Any]:
        """Send one approved draft through its channel, exactly once."""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM drafts WHERE id = ?", (int(draft_id),)).fetchone()
        if row is None:
            raise WorkflowError(f"no draft {draft_id}")
        if row["status"] != PENDING:
            raise WorkflowError(f"draft {draft_id} is {row['status']}, not waiting for approval")
        if row["channel"] != channel.name:
            raise WorkflowError(
                f"draft {draft_id} belongs to {row['channel']}; approving it through {channel.name} would send "
                "the reply to the wrong place"
            )
        try:
            delivered = channel.send(row["chat"], row["reply"])
        except Exception as exc:  # noqa: BLE001 - the failure is stored, not hidden
            with self._lock, self._connect() as conn:
                conn.execute(
                    "UPDATE drafts SET status = ?, decided_at = ?, error = ? WHERE id = ?",
                    (SEND_FAILED, time.time(), str(exc)[:500], int(draft_id)),
                )
            raise WorkflowError(f"the channel refused the send: {exc}") from exc
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE drafts SET status = ?, decided_at = ?, error = '' WHERE id = ?",
                (SENT, time.time(), int(draft_id)),
            )
        return {"draft_id": int(draft_id), "status": SENT, "sent": delivered}

    def reject(self, draft_id: int, reason: str = "") -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "UPDATE drafts SET status = ?, decided_at = ?, error = ? WHERE id = ? AND status = ?",
                (REJECTED, time.time(), reason[:500], int(draft_id), PENDING),
            )
            if not cursor.rowcount:
                raise WorkflowError(f"draft {draft_id} is not waiting for approval")
        return {"draft_id": int(draft_id), "status": REJECTED}

    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM drafts ORDER BY id DESC LIMIT ?", (max(1, int(limit)),))
            return [dict(row) for row in rows]

    # -- workflow entry point --------------------------------------------------------
    def run(
        self,
        channel: Any,
        drafter: Callable[[dict[str, Any]], str] | None = None,
        chat: str = "",
        limit: int = 20,
        **_: Any,
    ) -> WorkflowResult:
        result = WorkflowResult(self.name, FAILED, "")
        try:
            added = self.collect(channel, chat=chat, limit=limit)
        except Exception as exc:  # noqa: BLE001 - reported, never raised out of a workflow
            result.step("collect", SKIPPED, f"could not read {channel.name}: {exc}")
            return result.finish(PARTIAL, f"no messages collected: {exc}")
        result.step("collect", OK, f"{added} new message(s) from {channel.name}")
        if drafter is None:
            return result.finish(
                DONE,
                f"{added} new message(s) stored; no drafter was configured, so no reply was prepared",
            )
        created = self.draft(drafter, channel_name=channel.name, limit=limit)
        result.step("draft", OK if created else SKIPPED, f"{len(created)} draft(s) prepared for approval")
        waiting = len(self.pending())
        result.counts = {"collected": added, "drafted": len(created), "waiting_for_approval": waiting}
        if not created:
            return result.finish(DONE, f"no new drafts; {waiting} already waiting for your approval")
        return result.finish(
            DONE,
            f"{len(created)} draft(s) prepared and waiting for your approval — nothing was sent",
        )