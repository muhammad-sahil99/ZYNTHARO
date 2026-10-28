"""Inter-agent comms: memos, help requests, answers and file handoffs.

The fleet is only a fleet if its members can talk to each other. Before this module an
Operative that hit a wall had exactly two options: fail its report, or escalate all the way
to the Sovereign and consume the most expensive model in the building for a question a
sibling could have answered in one line.

What this gives the agents, concretely:

* **Asking for help.** ``agent_ask`` opens a *request* addressed to an agent by id, by name,
  by role (``role:cartographer`` — the standing specialist) or to ``*`` for the fleet. The
  request stays open until someone answers it, so a manager can see what is still waiting.
* **Answering.** ``agent_reply`` marks the parent request answered and posts the answer into
  the same thread. Answering an already-answered request is refused rather than silently
  creating a fork — two conflicting answers to one question is worse than a slow one.
* **File handoff.** An attachment is *copied* into the comms store with its sha256, not
  referenced by path. A path would let the sender's cleanup delete the recipient's evidence
  halfway through a task. Both sides can read the stored copy afterwards.
* **Persistence.** Everything lands in SQLite. A restarted app still has the thread, the
  open requests and the attachments; "the conversation is in RAM" is how a long job loses
  the one answer it needed.

Recipients are resolved against a live roster callback, so a spawned-and-terminated agent
cannot quietly receive mail: an unknown recipient is an error that lists the real names.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from ..events import AGENT_COMMS, AGENT_HELP_ANSWERED, AGENT_HELP_REQUEST, LOG, EventBus

#: Kinds of inter-agent message. ``request`` is the only one that stays open.
KINDS: tuple[str, ...] = ("memo", "request", "answer", "file", "notice")

#: A single message body is bounded: a report belongs in the report, not in a mail body.
MAX_BODY = 24_000
#: Attachments are copied, so the cap is about disk and memory, not trust.
MAX_ATTACHMENT_BYTES = 64 * 1024 * 1024

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    msg_id       TEXT PRIMARY KEY,
    thread_id    TEXT NOT NULL,
    sender_id    TEXT NOT NULL,
    sender_name  TEXT NOT NULL,
    recipient    TEXT NOT NULL,
    kind         TEXT NOT NULL,
    subject      TEXT NOT NULL DEFAULT '',
    body         TEXT NOT NULL DEFAULT '',
    attachments  TEXT NOT NULL DEFAULT '[]',
    reply_to     TEXT NOT NULL DEFAULT '',
    created_at   REAL NOT NULL,
    answered_at  REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS deliveries (
    msg_id    TEXT NOT NULL,
    agent_id  TEXT NOT NULL,
    read_at   REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (msg_id, agent_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(sender_id, created_at);
CREATE INDEX IF NOT EXISTS idx_deliveries_agent ON deliveries(agent_id, read_at);
"""


@dataclass(slots=True)
class Attachment:
    """A file copied into the comms store, identified by content, not by path."""

    name: str
    path: str
    sha256: str
    bytes: int
    note: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "path": self.path, "sha256": self.sha256, "bytes": self.bytes, "note": self.note}


@dataclass(slots=True)
class Message:
    msg_id: str
    thread_id: str
    sender_id: str
    sender_name: str
    recipient: str
    kind: str
    subject: str
    body: str
    attachments: list[dict[str, Any]] = field(default_factory=list)
    reply_to: str = ""
    created_at: float = 0.0
    answered_at: float = 0.0
    #: Per-recipient read state is tracked in ``deliveries``; this is the viewer's view.
    read: bool = False
    answered: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "msg_id": self.msg_id,
            "thread_id": self.thread_id,
            "sender_id": self.sender_id,
            "sender_name": self.sender_name,
            "recipient": self.recipient,
            "kind": self.kind,
            "subject": self.subject,
            "body": self.body,
            "attachments": self.attachments,
            "reply_to": self.reply_to,
            "created_at": round(self.created_at, 3),
            "answered": self.answered,
            "read": self.read,
        }

    def to_prompt_line(self) -> str:
        """One line for an agent's context — a thread is read, not dumped whole."""
        attach = f" [{len(self.attachments)} file(s)]" if self.attachments else ""
        status = " (answered)" if self.answered else ""
        return f"{self.sender_name} → {self.recipient} · {self.kind}{attach}{status}: {self.subject or self.body[:80]}"


class AgentComms:
    """Stored, threaded messaging between agents of one running fleet."""

    def __init__(
        self,
        path: str | Path,
        bus: EventBus | None = None,
        roster: Callable[[], Sequence[dict[str, Any]]] | None = None,
        attachments_dir: str | Path | None = None,
        memory: Any = None,
    ):
        self.path = str(path)
        self.bus = bus or EventBus()
        self.roster = roster
        self.memory = memory
        self._lock = threading.RLock()
        base = Path(self.path).parent
        self.attachments_dir = Path(attachments_dir) if attachments_dir else base / "comms-files"
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # -- roster --------------------------------------------------------------------------
    def _roster(self) -> list[dict[str, Any]]:
        if self.roster is None:
            return []
        try:
            return [dict(row) for row in self.roster() or []]
        except Exception as exc:  # a broken roster must name itself, not look like "no agents"
            self.bus.emit(LOG, source="comms", severity="warning", message=f"roster unavailable: {exc!r}")
            return []

    def known(self) -> list[str]:
        rows = self._roster()
        names = [f"{r.get('name', '?')} ({r.get('role', '?')})" for r in rows]
        return sorted(names)

    def resolve(self, recipient: str) -> tuple[str, list[str]]:
        """Resolve a recipient to (stored key, agent ids).

        Accepted forms: an agent id, an agent name, ``role:<role>`` for the standing
        specialist of that role, or ``*`` for the whole fleet. Anything else is an error
        that names the agents that do exist — guessing here would deliver work to nobody.
        """
        target = (recipient or "").strip()
        if not target:
            raise ValueError("recipient is empty; pass an agent id, name, role:<role> or *")
        rows = self._roster()
        if target == "*":
            return "*", [r["agent_id"] for r in rows if r.get("agent_id")]
        for row in rows:
            if row.get("agent_id") == target:
                return target, [target]
        matches = [r for r in rows if str(r.get("name", "")).lower() == target.lower()]
        if matches:
            return target, [str(m["agent_id"]) for m in matches]
        if target.lower().startswith("role:"):
            role = target.split(":", 1)[1].strip().lower()
            by_role = [r for r in rows if str(r.get("role", "")).lower() == role]
            if by_role:
                return target, [str(r["agent_id"]) for r in by_role]
        raise ValueError(
            f"unknown recipient {target!r}; known agents: {', '.join(self.known()) or '(none registered)'}. "
            "Use role:<role> to reach the standing specialist of a role."
        )

    # -- writing -------------------------------------------------------------------------
    def send(
        self,
        sender_id: str,
        sender_name: str,
        recipient: str,
        body: str,
        subject: str = "",
        kind: str = "memo",
        attachments: Iterable[dict[str, Any]] = (),
        thread_id: str = "",
        reply_to: str = "",
    ) -> Message:
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
        text = (body or "").strip()
        files = list(attachments)
        if not text and not files:
            raise ValueError("a message needs a body or at least one attachment")
        if len(text) > MAX_BODY:
            raise ValueError(f"body is {len(text)} chars; the limit is {MAX_BODY} — put detail in a file or memory")
        key, targets = self.resolve(recipient)
        msg_id = uuid.uuid4().hex[:12]
        parent = self.thread_of(reply_to) if reply_to else ""
        thread = thread_id or parent or msg_id
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO messages (msg_id, thread_id, sender_id, sender_name, recipient, kind, subject, body, "
                "attachments, reply_to, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    msg_id,
                    thread,
                    sender_id,
                    sender_name,
                    key,
                    kind,
                    (subject or "")[:200],
                    text,
                    json.dumps(files),
                    reply_to or "",
                    now,
                ),
            )
            for agent_id in targets:
                self._conn.execute(
                    "INSERT OR IGNORE INTO deliveries (msg_id, agent_id, read_at) VALUES (?,?,0)",
                    (msg_id, agent_id),
                )
            self._conn.commit()
        message = Message(
            msg_id=msg_id,
            thread_id=thread,
            sender_id=sender_id,
            sender_name=sender_name,
            recipient=key,
            kind=kind,
            subject=(subject or "")[:200],
            body=text,
            attachments=files,
            reply_to=reply_to or "",
            created_at=now,
        )
        topic = AGENT_HELP_REQUEST if kind == "request" else AGENT_COMMS
        self.bus.emit(
            topic,
            source=sender_id,
            severity="notice" if kind == "request" else "info",
            **{
                "msg_id": msg_id,
                "thread_id": thread,
                "from": sender_name,
                "from_id": sender_id,
                "to": key,
                "recipients": len(targets),
                "kind": kind,
                "subject": message.subject,
                "attachments": len(files),
            },
        )
        self._mirror(sender_id, f"sent {kind} to {key}: {message.subject or text[:120]}")
        for agent_id in targets:
            self._mirror(agent_id, f"received {kind} from {sender_name}: {message.subject or text[:120]}")
        return message

    def answer(self, msg_id: str, agent_id: str, agent_name: str, body: str, attachments: Iterable[dict[str, Any]] = ()) -> Message:
        """Answer an open request. One answer per request; a second is refused."""
        original = self.get(msg_id)
        if original is None:
            raise ValueError(f"no message {msg_id!r}")
        if original.kind != "request":
            raise ValueError(f"message {msg_id} is a {original.kind}, not a request; reply in its thread instead")
        if original.answered_at:
            raise ValueError(
                f"request {msg_id} was already answered at {time.strftime('%H:%M:%S', time.localtime(original.answered_at))} "
                "— open a new request instead of forking this one"
            )
        reply = self.send(
            sender_id=agent_id,
            sender_name=agent_name,
            recipient=original.sender_id,
            body=body,
            subject=f"re: {original.subject}" if original.subject else "answer",
            kind="answer",
            attachments=attachments,
            thread_id=original.thread_id,
            reply_to=msg_id,
        )
        with self._lock:
            self._conn.execute("UPDATE messages SET answered_at=? WHERE msg_id=?", (time.time(), msg_id))
            self._conn.commit()
        self.bus.emit(
            AGENT_HELP_ANSWERED,
            source=agent_id,
            **{
                "msg_id": msg_id,
                "thread_id": original.thread_id,
                "from": agent_name,
                "to": original.sender_name,
                "question": original.subject or original.body[:120],
                "answer": body[:200],
            },
        )
        return reply

    def mark_read(self, agent_id: str, msg_ids: Sequence[str] | None = None) -> int:
        now = time.time()
        with self._lock:
            if msg_ids:
                placeholders = ",".join("?" for _ in msg_ids)
                cur = self._conn.execute(
                    f"UPDATE deliveries SET read_at=? WHERE agent_id=? AND read_at=0 AND msg_id IN ({placeholders})",
                    (now, agent_id, *msg_ids),
                )
            else:
                cur = self._conn.execute(
                    "UPDATE deliveries SET read_at=? WHERE agent_id=? AND read_at=0", (now, agent_id)
                )
            self._conn.commit()
            return int(cur.rowcount or 0)

    # -- attachment handling -------------------------------------------------------------
    def stage_files(self, paths: Sequence[str], note: str = "") -> list[dict[str, Any]]:
        """Copy files into the comms store so the handoff survives the sender's cleanup."""
        staged: list[dict[str, Any]] = []
        for raw in paths:
            source = Path(raw).expanduser()
            if not source.exists():
                raise ValueError(f"attachment {raw!r} does not exist")
            if source.is_dir():
                raise ValueError(f"attachment {raw!r} is a directory; zip it first, then attach the archive")
            size = source.stat().st_size
            if size > MAX_ATTACHMENT_BYTES:
                raise ValueError(
                    f"attachment {source.name!r} is {size} bytes; the limit is {MAX_ATTACHMENT_BYTES} — split or zip it"
                )
            digest = hashlib.sha256()
            with source.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    digest.update(chunk)
            sha = digest.hexdigest()
            self.attachments_dir.mkdir(parents=True, exist_ok=True)
            # Content-addressed: the same file twice costs one copy, and the name cannot lie
            # about what is inside it.
            target = self.attachments_dir / f"{sha[:16]}-{source.name}"
            if not target.exists():
                shutil.copy2(source, target)
            staged.append(
                Attachment(name=source.name, path=str(target), sha256=sha, bytes=size, note=note).to_json()
            )
        return staged

    # -- reading -------------------------------------------------------------------------
    def get(self, msg_id: str) -> Message | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM messages WHERE msg_id=?", (msg_id,)).fetchone()
        return self._to_message(row) if row else None

    def thread_of(self, msg_id: str) -> str:
        message = self.get(msg_id)
        return message.thread_id if message else ""

    def inbox(
        self,
        agent_id: str,
        limit: int = 40,
        unread_only: bool = False,
        include_broadcast: bool = True,
    ) -> list[Message]:
        sql = (
            "SELECT m.*, d.read_at FROM messages m JOIN deliveries d ON d.msg_id = m.msg_id "
            "WHERE d.agent_id = ?"
        )
        params: list[Any] = [agent_id]
        if unread_only:
            sql += " AND d.read_at = 0"
        sql += " ORDER BY m.created_at DESC LIMIT ?"
        params.append(max(1, int(limit)))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        messages = [self._to_message(row, read=bool(row["read_at"])) for row in rows]
        if include_broadcast:
            direct_ids = {m.msg_id for m in messages}
            for row in self._broadcasts(limit):
                if row["msg_id"] in direct_ids:
                    continue
                messages.append(self._to_message(row))
        return messages[: max(1, int(limit))]

    def _broadcasts(self, limit: int) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM messages WHERE recipient='*' ORDER BY created_at DESC LIMIT ?", (int(limit),)
            ).fetchall()

    def thread(self, thread_id: str, limit: int = 100) -> list[Message]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE thread_id=? ORDER BY created_at ASC LIMIT ?",
                (thread_id, max(1, int(limit))),
            ).fetchall()
        return [self._to_message(row) for row in rows]

    def open_requests(self, agent_id: str = "") -> list[Message]:
        """Requests nobody has answered yet — what a manager must actually chase."""
        sql = "SELECT * FROM messages WHERE kind='request' AND answered_at = 0"
        params: list[Any] = []
        if agent_id:
            sql += " AND (sender_id = ? OR msg_id IN (SELECT msg_id FROM deliveries WHERE agent_id = ?))"
            params += [agent_id, agent_id]
        sql += " ORDER BY created_at ASC"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._to_message(row) for row in rows]

    def _to_message(self, row: sqlite3.Row, read: bool = False) -> Message:
        try:
            attachments = json.loads(row["attachments"] or "[]")
        except json.JSONDecodeError:
            attachments = []
        return Message(
            msg_id=row["msg_id"],
            thread_id=row["thread_id"],
            sender_id=row["sender_id"],
            sender_name=row["sender_name"],
            recipient=row["recipient"],
            kind=row["kind"],
            subject=row["subject"],
            body=row["body"],
            attachments=attachments,
            reply_to=row["reply_to"],
            created_at=row["created_at"],
            answered_at=row["answered_at"],
            read=read,
            answered=bool(row["answered_at"]),
        )

    # -- introspection for the HUD -------------------------------------------------------
    def stats(self, agent_id: str = "") -> dict[str, Any]:
        with self._lock:
            total = int(self._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
            open_requests = int(
                self._conn.execute("SELECT COUNT(*) FROM messages WHERE kind='request' AND answered_at=0").fetchone()[0]
            )
            threads = int(self._conn.execute("SELECT COUNT(DISTINCT thread_id) FROM messages").fetchone()[0])
            unread = 0
            if agent_id:
                unread = int(
                    self._conn.execute(
                        "SELECT COUNT(*) FROM deliveries WHERE agent_id=? AND read_at=0", (agent_id,)
                    ).fetchone()[0]
                )
        return {
            "messages": total,
            "threads": threads,
            "open_requests": open_requests,
            "unread_for_me": unread,
            "attachments_dir": str(self.attachments_dir),
        }

    def snapshot(self, limit: int = 20) -> dict[str, Any]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM messages ORDER BY created_at DESC LIMIT ?", (int(limit),)).fetchall()
        return {
            "stats": self.stats(),
            "recent": [self._to_message(row).to_json() for row in rows],
            "open_requests": [m.to_json() for m in self.open_requests()[:10]],
        }

    # -- memory --------------------------------------------------------------------------
    def _mirror(self, agent_id: str, text: str) -> None:
        """Write the exchange into each agent's own memory.

        A failure here is reported on the bus rather than swallowed: losing the mirror is not
        fatal (the message is already stored), but pretending it worked would be.
        """
        if self.memory is None or not agent_id:
            return
        try:
            self.memory.remember(agent_id, text[:600], kind="comms", tags=["comms"], importance=0.55)
        except Exception as exc:
            self.bus.emit(
                LOG,
                source="comms",
                severity="warning",
                message=f"could not mirror comms into {agent_id}'s memory: {exc!r}",
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()
