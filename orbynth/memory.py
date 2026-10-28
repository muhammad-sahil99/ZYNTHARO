"""Per-agent memory, shared team board, transcript and append-only audit — one SQLite file.

Design decisions that matter:

* **Namespaces.** Memories are keyed by ``agent_id``. An agent recalls only its own
  namespace plus the shared board of its team unless it holds ``memory.admin``. This is
  what makes "each subagent has its own memory, the main agent has its own" real rather
  than cosmetic.
* **Survives model swaps.** Memory is attached to the *agent identity*, not to the model.
  When the Steward transplants an agent onto a fallback model, the agent id is unchanged
  and it keeps its entire history — that is the whole point of the requirement.
* **Retrieval.** FTS5 when available (SQLite builds), otherwise a LIKE fallback. Results
  are re-ranked in Python by lexical overlap × importance × recency so a forgotten hint
  can still beat a chatty recent line.
* **Persistence of learning.** ``facts`` is the semantic layer: durable key/value notes an
  agent has decided are true. ``board`` is the team's shared scratchpad for long tasks.
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS agents (
    agent_id     TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    rank         INTEGER NOT NULL DEFAULT 5,
    role         TEXT NOT NULL DEFAULT 'operative',
    parent_id    TEXT,
    team_id      TEXT,
    model        TEXT,
    status       TEXT NOT NULL DEFAULT 'idle',
    capabilities TEXT NOT NULL DEFAULT '[]',
    born_at      REAL NOT NULL,
    last_seen    REAL NOT NULL,
    meta         TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS episodes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id   TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'note',
    text       TEXT NOT NULL,
    tags       TEXT NOT NULL DEFAULT '[]',
    importance REAL NOT NULL DEFAULT 0.5,
    ts         REAL NOT NULL,
    meta       TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS episodes_agent_ts ON episodes(agent_id, ts DESC);
CREATE INDEX IF NOT EXISTS episodes_kind ON episodes(kind);

CREATE TABLE IF NOT EXISTS facts (
    agent_id   TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0,
    updated_at REAL NOT NULL,
    PRIMARY KEY (agent_id, key)
);

CREATE TABLE IF NOT EXISTS transcript (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id  TEXT NOT NULL,
    role      TEXT NOT NULL,          -- system | user | assistant | tool
    content   TEXT NOT NULL,
    ts        REAL NOT NULL,
    meta      TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS transcript_agent ON transcript(agent_id, id);

CREATE TABLE IF NOT EXISTS board (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id  TEXT NOT NULL,
    topic    TEXT NOT NULL,
    body     TEXT NOT NULL,
    author   TEXT NOT NULL,
    ts       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS board_team ON board(team_id, id);

CREATE TABLE IF NOT EXISTS audit (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id   TEXT NOT NULL,
    action     TEXT NOT NULL,
    capability TEXT NOT NULL,
    args       TEXT NOT NULL DEFAULT '{}',
    outcome    TEXT NOT NULL,          -- ok | denied | error | confirmed
    duration   REAL NOT NULL DEFAULT 0,
    evidence   TEXT NOT NULL DEFAULT '{}',
    detail     TEXT NOT NULL DEFAULT '',
    ts         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_ts ON audit(ts DESC);
CREATE INDEX IF NOT EXISTS audit_agent ON audit(agent_id, ts DESC);

CREATE TABLE IF NOT EXISTS receipts (
    receipt_id TEXT PRIMARY KEY,
    task_id    TEXT NOT NULL DEFAULT '',
    agent_id   TEXT NOT NULL DEFAULT '',
    tool       TEXT NOT NULL,
    capability TEXT NOT NULL DEFAULT '',
    outcome    TEXT NOT NULL,          -- VERIFIED | UNVERIFIED | FAILED (spec 4.2)
    rung       INTEGER NOT NULL DEFAULT 1,
    target     TEXT NOT NULL DEFAULT '',
    duration   REAL NOT NULL DEFAULT 0,
    body       TEXT NOT NULL DEFAULT '{}',
    ts         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS receipts_task ON receipts(task_id, ts DESC);
CREATE INDEX IF NOT EXISTS receipts_tool ON receipts(tool, ts DESC);

CREATE TABLE IF NOT EXISTS tasks (
    task_id     TEXT PRIMARY KEY,
    team_id     TEXT NOT NULL,
    parent_task TEXT,
    owner       TEXT NOT NULL,
    title       TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',  -- open | running | blocked | done | failed
    detail      TEXT NOT NULL DEFAULT '{}',
    report      TEXT NOT NULL DEFAULT '{}',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS tasks_team ON tasks(team_id, created_at);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts USING fts5(
    text, tags, content='episodes', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS episodes_ai AFTER INSERT ON episodes BEGIN
  INSERT INTO episodes_fts(rowid, text, tags) VALUES (new.id, new.text, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS episodes_ad AFTER DELETE ON episodes BEGIN
  INSERT INTO episodes_fts(episodes_fts, rowid, text, tags) VALUES('delete', old.id, old.text, old.tags);
END;
CREATE TRIGGER IF NOT EXISTS episodes_au AFTER UPDATE ON episodes BEGIN
  INSERT INTO episodes_fts(episodes_fts, rowid, text, tags) VALUES('delete', old.id, old.text, old.tags);
  INSERT INTO episodes_fts(rowid, text, tags) VALUES (new.id, new.text, new.tags);
END;
"""


@dataclass(slots=True)
class MemoryHit:
    id: int
    agent_id: str
    kind: str
    text: str
    tags: list[str]
    importance: float
    ts: float
    score: float = 0.0

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.ts)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "kind": self.kind,
            "text": self.text,
            "tags": self.tags,
            "importance": round(self.importance, 3),
            "ts": self.ts,
            # Recall-only fields: they describe this search, not the stored record.
            "age_s": round(self.age_seconds, 1),
            "score": round(self.score, 4),
        }


@dataclass(slots=True)
class AgentRecord:
    agent_id: str
    name: str
    rank: int
    role: str
    parent_id: str | None
    team_id: str | None
    model: str
    status: str
    capabilities: list[str]
    born_at: float
    last_seen: float
    meta: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "rank": self.rank,
            "role": self.role,
            "parent_id": self.parent_id,
            "team_id": self.team_id,
            "model": self.model,
            "status": self.status,
            "capabilities": self.capabilities,
            "age_s": round(time.time() - self.born_at, 1),
            "meta": self.meta,
        }


def _tokens(text: str) -> set[str]:
    out: set[str] = set()
    for raw in text.lower().replace("/", " ").replace("_", " ").split():
        cleaned = "".join(ch for ch in raw if ch.isalnum())
        if len(cleaned) > 2:
            out.add(cleaned)
    return out


class _Result:
    """Materialised query result.

    Rows are fetched while the store's lock is still held, so a cursor can never be interleaved
    with another thread's statement. Every query in this module is bounded (``LIMIT`` on the
    unbounded ones), so materialising is cheap and removes a whole class of concurrency bugs.
    """

    __slots__ = ("_rows", "rowcount", "lastrowid")

    def __init__(self, cursor: sqlite3.Cursor):
        self._rows = cursor.fetchall()
        self.rowcount = cursor.rowcount
        self.lastrowid = cursor.lastrowid

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[Any]:
        return self._rows

    def __iter__(self):
        return iter(self._rows)

    def __len__(self) -> int:
        return len(self._rows)


class _LockedConnection:
    """Serialises access to one SQLite connection.

    Agents run in parallel worker threads, and they all write to the same store. SQLite is
    happy with cross-thread use of a connection created with ``check_same_thread=False`` but
    *not* with two statements in flight at once — which is exactly what a fleet of subagents
    does. One re-entrant lock around every statement is the honest fix.
    """

    def __init__(self, connection: sqlite3.Connection):
        self._connection = connection
        self._lock = threading.RLock()

    @property
    def row_factory(self):
        return self._connection.row_factory

    @row_factory.setter
    def row_factory(self, value) -> None:
        self._connection.row_factory = value

    def execute(self, sql: str, parameters: Iterable[Any] = ()) -> _Result:
        with self._lock:
            return _Result(self._connection.execute(sql, tuple(parameters)))

    def executescript(self, script: str) -> None:
        with self._lock:
            self._connection.executescript(script)

    def commit(self) -> None:
        with self._lock:
            self._connection.commit()

    def rollback(self) -> None:
        with self._lock:
            self._connection.rollback()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "_LockedConnection":
        with self._lock:
            self._connection.__enter__()
        return self

    def __exit__(self, *exc: Any) -> None:
        with self._lock:
            self._connection.__exit__(*exc)


class MemoryStore:
    """SQLite-backed memory for every agent in the fleet (thread-safe)."""

    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # A per-thread connection would silently give ``:memory:`` databases to different
        # threads, so one shared connection behind a lock is the correct design for both cases.
        self._conn = _LockedConnection(sqlite3.connect(self.path, check_same_thread=False))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self.has_fts = self._try_fts()
        self._conn.commit()

    def _try_fts(self) -> bool:
        try:
            self._conn.executescript(FTS_SCHEMA)
            return True
        except sqlite3.OperationalError:
            return False

    def close(self) -> None:
        self._conn.commit()
        self._conn.close()

    # ----------------------------------------------------------------------------------
    # Agents
    # ----------------------------------------------------------------------------------
    def register_agent(
        self,
        name: str,
        rank: int = 5,
        role: str = "operative",
        parent_id: str | None = None,
        team_id: str | None = None,
        model: str = "",
        capabilities: Iterable[str] = (),
        agent_id: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> AgentRecord:
        agent_id = agent_id or f"{role[:4]}-{uuid.uuid4().hex[:8]}"
        now = time.time()
        self._conn.execute(
            """INSERT OR REPLACE INTO agents
               (agent_id, name, rank, role, parent_id, team_id, model, status, capabilities,
                born_at, last_seen, meta)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                agent_id,
                name,
                rank,
                role,
                parent_id,
                team_id,
                model,
                "idle",
                json.dumps(sorted(capabilities)),
                now,
                now,
                json.dumps(meta or {}),
            ),
        )
        self._conn.commit()
        return self.get_agent(agent_id)  # type: ignore[return-value]

    def get_agent(self, agent_id: str) -> AgentRecord | None:
        row = self._conn.execute("SELECT * FROM agents WHERE agent_id=?", (agent_id,)).fetchone()
        return self._row_to_agent(row) if row else None

    def find_agent(self, name: str) -> AgentRecord | None:
        row = self._conn.execute(
            "SELECT * FROM agents WHERE name=? ORDER BY last_seen DESC LIMIT 1", (name,)
        ).fetchone()
        return self._row_to_agent(row) if row else None

    def list_agents(self, team_id: str | None = None, role: str | None = None) -> list[AgentRecord]:
        sql = "SELECT * FROM agents WHERE 1=1"
        args: list[Any] = []
        if team_id:
            sql += " AND team_id=?"
            args.append(team_id)
        if role:
            sql += " AND role=?"
            args.append(role)
        sql += " ORDER BY rank ASC, born_at ASC"
        return [self._row_to_agent(r) for r in self._conn.execute(sql, args).fetchall()]

    def set_agent_status(self, agent_id: str, status: str, model: str | None = None) -> None:
        if model is None:
            self._conn.execute(
                "UPDATE agents SET status=?, last_seen=? WHERE agent_id=?",
                (status, time.time(), agent_id),
            )
        else:
            self._conn.execute(
                "UPDATE agents SET status=?, model=?, last_seen=? WHERE agent_id=?",
                (status, model, time.time(), agent_id),
            )
        self._conn.commit()

    def set_agent_limits(
        self,
        agent_id: str,
        max_actions: int = 0,
        max_wall_seconds: int = 0,
        max_tokens: int = 0,
        capabilities: list[str] | None = None,
    ) -> None:
        """Persist an agent's limits and permissions (contract: a manager can bound a worker).

        Stored in the agent's ``meta`` blob so a limit set by the Steward is still there after a
        restart or a model swap, instead of living only in the process that applied it.
        """
        record = self.get_agent(agent_id)
        if record is None:
            return
        meta = dict(getattr(record, "meta", {}) or {})
        limits = {
            "max_actions": int(max_actions or (meta.get("limits") or {}).get("max_actions", 0)),
            "max_wall_seconds": int(max_wall_seconds or (meta.get("limits") or {}).get("max_wall_seconds", 0)),
            "max_tokens": int(max_tokens or (meta.get("limits") or {}).get("max_tokens", 0)),
            "set_at": time.time(),
        }
        meta["limits"] = limits
        sql = "UPDATE agents SET meta=?, last_seen=?"
        args: list[Any] = [json.dumps(meta), time.time()]
        if capabilities is not None:
            sql += ", capabilities=?"
            args.append(json.dumps(sorted(capabilities)))
        sql += " WHERE agent_id=?"
        args.append(agent_id)
        self._conn.execute(sql, args)
        self._conn.commit()

    def _row_to_agent(self, row: sqlite3.Row) -> AgentRecord:
        return AgentRecord(
            agent_id=row["agent_id"],
            name=row["name"],
            rank=row["rank"],
            role=row["role"],
            parent_id=row["parent_id"],
            team_id=row["team_id"],
            model=row["model"],
            status=row["status"],
            capabilities=json.loads(row["capabilities"]),
            born_at=row["born_at"],
            last_seen=row["last_seen"],
            meta=json.loads(row["meta"]),
        )

    # ----------------------------------------------------------------------------------
    # Episodic memory
    # ----------------------------------------------------------------------------------
    def remember(
        self,
        agent_id: str,
        text: str,
        kind: str = "note",
        tags: Sequence[str] = (),
        importance: float = 0.5,
        meta: dict[str, Any] | None = None,
        ts: float | None = None,
    ) -> int:
        cur = self._conn.execute(
            "INSERT INTO episodes (agent_id, kind, text, tags, importance, ts, meta) VALUES (?,?,?,?,?,?,?)",
            (
                agent_id,
                kind,
                text,
                json.dumps(list(tags)),
                float(importance),
                ts if ts is not None else time.time(),
                json.dumps(meta or {}),
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid or 0)

    def recall(
        self,
        agent_id: str,
        query: str = "",
        k: int = 8,
        kinds: Sequence[str] | None = None,
        include_teams: Sequence[str] = (),
        include_global: bool = False,
    ) -> list[MemoryHit]:
        """Hybrid retrieval: FTS/LIKE candidates, then overlap×importance×recency re-rank."""
        scopes = [agent_id, *include_teams]
        if include_global:
            scopes.append("*")
        placeholders = ",".join("?" for _ in scopes)
        params: list[Any] = list(scopes)

        sql = f"SELECT * FROM episodes WHERE agent_id IN ({placeholders})"
        if kinds:
            sql += f" AND kind IN ({','.join('?' for _ in kinds)})"
            params.extend(kinds)

        if query.strip():
            if self.has_fts:
                sql = (
                    "SELECT e.* FROM episodes e JOIN episodes_fts f ON f.rowid = e.id "
                    f"WHERE e.agent_id IN ({placeholders})"
                )
                fts_params: list[Any] = list(scopes)
                if kinds:
                    sql += f" AND e.kind IN ({','.join('?' for _ in kinds)})"
                    fts_params.extend(kinds)
                sql += " AND episodes_fts MATCH ? ORDER BY bm25(episodes_fts) LIMIT 200"
                fts_params.append(self._fts_query(query))
                try:
                    rows = self._conn.execute(sql, fts_params).fetchall()
                except sqlite3.OperationalError:
                    rows = self._fallback_query(query, params)
            else:
                rows = self._fallback_query(query, params)
        else:
            sql += " ORDER BY ts DESC LIMIT 200"
            rows = self._conn.execute(sql, params).fetchall()

        query_tokens = _tokens(query) if query else set()
        now = time.time()
        hits: list[MemoryHit] = []
        for row in rows:
            tags = json.loads(row["tags"])
            overlap = 0.0
            if query_tokens:
                text_tokens = _tokens(row["text"]) | {t.lower() for t in tags}
                if text_tokens:
                    overlap = len(query_tokens & text_tokens) / len(query_tokens)
            recency = 1.0 / (1.0 + (now - row["ts"]) / 86_400.0)
            # Weights chosen so a forgotten high-importance fact still surfaces.
            score = 0.55 * overlap + 0.30 * float(row["importance"]) + 0.15 * recency
            hits.append(
                MemoryHit(
                    id=row["id"],
                    agent_id=row["agent_id"],
                    kind=row["kind"],
                    text=row["text"],
                    tags=tags,
                    importance=float(row["importance"]),
                    ts=row["ts"],
                    score=score,
                )
            )
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:k]

    def _fallback_query(self, query: str, params: list[Any]) -> list[sqlite3.Row]:
        clauses = " OR ".join("text LIKE ?" for _ in _tokens(query)) or "text LIKE ?"
        like_args = [f"%{tok}%" for tok in (_tokens(query) or [query])]
        sql = (
            "SELECT * FROM episodes WHERE agent_id IN "
            f"({','.join('?' for _ in params)}) AND ({clauses}) ORDER BY ts DESC LIMIT 200"
        )
        return self._conn.execute(sql, params + like_args).fetchall()

    @staticmethod
    def _fts_query(query: str) -> str:
        toks = _tokens(query)
        return " OR ".join(f'"{t}"' for t in toks) if toks else '"' 

    def forget(self, agent_id: str, episode_id: int) -> bool:
        cur = self._conn.execute("DELETE FROM episodes WHERE agent_id=? AND id=?", (agent_id, episode_id))
        self._conn.commit()
        return cur.rowcount > 0

    # ----------------------------------------------------------------------------------
    # Semantic memory (facts)
    # ----------------------------------------------------------------------------------
    def set_fact(self, agent_id: str, key: str, value: Any, confidence: float = 1.0) -> None:
        self._conn.execute(
            """INSERT INTO facts (agent_id, key, value, confidence, updated_at) VALUES (?,?,?,?,?)
               ON CONFLICT(agent_id, key) DO UPDATE SET
                 value=excluded.value, confidence=excluded.confidence, updated_at=excluded.updated_at""",
            (agent_id, key, json.dumps(value), float(confidence), time.time()),
        )
        self._conn.commit()

    def get_fact(self, agent_id: str, key: str, default: Any = None) -> Any:
        row = self._conn.execute(
            "SELECT value FROM facts WHERE agent_id=? AND key=?", (agent_id, key)
        ).fetchone()
        return json.loads(row["value"]) if row else default

    def facts(self, agent_id: str) -> dict[str, Any]:
        rows = self._conn.execute("SELECT key, value FROM facts WHERE agent_id=?", (agent_id,)).fetchall()
        return {r["key"]: json.loads(r["value"]) for r in rows}

    # ----------------------------------------------------------------------------------
    # Transcript
    # ----------------------------------------------------------------------------------
    def append_message(
        self, agent_id: str, role: str, content: str, meta: dict[str, Any] | None = None
    ) -> int:
        cur = self._conn.execute(
            "INSERT INTO transcript (agent_id, role, content, ts, meta) VALUES (?,?,?,?,?)",
            (agent_id, role, content, time.time(), json.dumps(meta or {})),
        )
        self._conn.commit()
        return int(cur.lastrowid or 0)

    def transcript(self, agent_id: str, limit: int = 40) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM transcript WHERE agent_id=? ORDER BY id DESC LIMIT ?", (agent_id, limit)
        ).fetchall()
        return [
            {"id": r["id"], "role": r["role"], "content": r["content"], "ts": r["ts"], "meta": json.loads(r["meta"])}
            for r in reversed(rows)
        ]

    def transcript_len(self, agent_id: str) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM transcript WHERE agent_id=?", (agent_id,)).fetchone()
        return int(row["n"])

    # ----------------------------------------------------------------------------------
    # Team board
    # ----------------------------------------------------------------------------------
    def post_board(self, team_id: str, topic: str, body: str, author: str) -> int:
        cur = self._conn.execute(
            "INSERT INTO board (team_id, topic, body, author, ts) VALUES (?,?,?,?,?)",
            (team_id, topic, body, author, time.time()),
        )
        self._conn.commit()
        return int(cur.lastrowid or 0)

    def read_board(self, team_id: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM board WHERE team_id=? ORDER BY id DESC LIMIT ?", (team_id, limit)
        ).fetchall()
        return [
            {"id": r["id"], "topic": r["topic"], "body": r["body"], "author": r["author"], "ts": r["ts"]}
            for r in reversed(rows)
        ]

    # ----------------------------------------------------------------------------------
    # Tasks
    # ----------------------------------------------------------------------------------
    def create_task(
        self,
        team_id: str,
        owner: str,
        title: str,
        parent_task: str | None = None,
        detail: dict[str, Any] | None = None,
        task_id: str | None = None,
    ) -> str:
        task_id = task_id or f"task-{uuid.uuid4().hex[:10]}"
        now = time.time()
        self._conn.execute(
            """INSERT INTO tasks (task_id, team_id, parent_task, owner, title, status, detail, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (task_id, team_id, parent_task, owner, title, "open", json.dumps(detail or {}), now, now),
        )
        self._conn.commit()
        return task_id

    def update_task(
        self,
        task_id: str,
        status: str | None = None,
        report: dict[str, Any] | None = None,
        owner: str | None = None,
    ) -> None:
        sets = ["updated_at=?"]
        args: list[Any] = [time.time()]
        if status:
            sets.append("status=?")
            args.append(status)
        if owner:
            sets.append("owner=?")
            args.append(owner)
        if report is not None:
            sets.append("report=?")
            args.append(json.dumps(report))
        args.append(task_id)
        self._conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE task_id=?", args)
        self._conn.commit()

    def task(self, task_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            return None
        return {
            "task_id": row["task_id"],
            "team_id": row["team_id"],
            "parent_task": row["parent_task"],
            "owner": row["owner"],
            "title": row["title"],
            "status": row["status"],
            "detail": json.loads(row["detail"]),
            "report": json.loads(row["report"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def tasks(self, team_id: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT task_id FROM tasks WHERE 1=1"
        args: list[Any] = []
        if team_id:
            sql += " AND team_id=?"
            args.append(team_id)
        if status:
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY created_at ASC"
        return [t for tid in (r["task_id"] for r in self._conn.execute(sql, args).fetchall()) if (t := self.task(tid))]

    # ----------------------------------------------------------------------------------
    # Audit
    # ----------------------------------------------------------------------------------
    def audit(
        self,
        agent_id: str,
        action: str,
        capability: str,
        outcome: str,
        args: dict[str, Any] | None = None,
        duration: float = 0.0,
        evidence: dict[str, Any] | None = None,
        detail: str = "",
    ) -> int:
        cur = self._conn.execute(
            """INSERT INTO audit (agent_id, action, capability, args, outcome, duration, evidence, detail, ts)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                agent_id,
                action,
                capability,
                json.dumps(_redact(args or {})),
                outcome,
                float(duration),
                json.dumps(evidence or {}),
                detail,
                time.time(),
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid or 0)

    def audit_log(self, limit: int = 100, agent_id: str | None = None) -> list[dict[str, Any]]:
        if agent_id:
            rows = self._conn.execute(
                "SELECT * FROM audit WHERE agent_id=? ORDER BY id DESC LIMIT ?", (agent_id, limit)
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [
            {
                "id": r["id"],
                "agent_id": r["agent_id"],
                "action": r["action"],
                "capability": r["capability"],
                "args": json.loads(r["args"]),
                "outcome": r["outcome"],
                "duration": r["duration"],
                "evidence": json.loads(r["evidence"]),
                "detail": r["detail"],
                "ts": r["ts"],
            }
            for r in rows
        ]

    # ----------------------------------------------------------------------------------
    # Receipts (spec 4.2): the evidence behind every claim an agent makes
    # ----------------------------------------------------------------------------------
    def store_receipt(self, receipt: dict[str, Any]) -> str:
        """Persist one host-generated receipt. Never written by a plugin or a driver."""
        receipt_id = str(receipt.get("receipt_id", ""))
        if not receipt_id:
            raise ValueError("a receipt without an id is not a receipt")
        self._conn.execute(
            """INSERT OR REPLACE INTO receipts
               (receipt_id, task_id, agent_id, tool, capability, outcome, rung, target, duration, body, ts)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                receipt_id,
                str(receipt.get("task_id", "")),
                str(receipt.get("agent_id", "")),
                str(receipt.get("tool", "")),
                str(receipt.get("capability", "")),
                str(receipt.get("outcome", "UNVERIFIED")),
                int(receipt.get("rung", 1) or 1),
                str(receipt.get("target", "")),
                float(receipt.get("duration", 0.0) or 0.0),
                json.dumps(receipt, default=str),
                time.time(),
            ),
        )
        self._conn.commit()
        return receipt_id

    def receipts(
        self,
        task_id: str | None = None,
        limit: int = 50,
        outcome: str | None = None,
    ) -> list[dict[str, Any]]:
        """Receipts, newest first, for the Trust Dashboard and the outcome verifier."""
        clauses: list[str] = []
        args: list[Any] = []
        if task_id:
            clauses.append("task_id=?")
            args.append(task_id)
        if outcome:
            clauses.append("outcome=?")
            args.append(outcome)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"SELECT body FROM receipts{where} ORDER BY ts DESC LIMIT ?", (*args, int(limit))
        ).fetchall()
        return [json.loads(row["body"]) for row in rows]

    def receipt_stats(self, task_id: str | None = None) -> dict[str, Any]:
        sql = "SELECT outcome, COUNT(*) AS n FROM receipts"
        args: tuple[Any, ...] = ()
        if task_id:
            sql += " WHERE task_id=?"
            args = (task_id,)
        rows = self._conn.execute(sql + " GROUP BY outcome", args).fetchall()
        counts = {str(row["outcome"]): int(row["n"]) for row in rows}
        return {"by_outcome": counts, "total": sum(counts.values())}

    def prune_audit(self, keep_days: int = 90) -> int:
        cutoff = time.time() - keep_days * 86_400
        cur = self._conn.execute("DELETE FROM audit WHERE ts < ?", (cutoff,))
        self._conn.commit()
        return cur.rowcount

    # ----------------------------------------------------------------------------------
    # Stats (feeds the HUD's memory panel)
    # ----------------------------------------------------------------------------------
    def stats(self, agent_id: str | None = None) -> dict[str, Any]:
        def scalar(sql: str, args: tuple[Any, ...] = ()) -> int:
            return int(self._conn.execute(sql, args).fetchone()[0])

        where, args = ("", ()) if not agent_id else (" WHERE agent_id=?", (agent_id,))
        episodes = scalar(f"SELECT COUNT(*) FROM episodes{where}", args)
        return {
            "agents": scalar("SELECT COUNT(*) FROM agents"),
            "episodes": episodes,
            "facts": scalar(f"SELECT COUNT(*) FROM facts{where}", args),
            "messages": scalar(f"SELECT COUNT(*) FROM transcript{where}", args),
            "tasks": scalar("SELECT COUNT(*) FROM tasks"),
            "audit": scalar("SELECT COUNT(*) FROM audit"),
            "fts": self.has_fts,
        }


_SECRET_HINTS = ("key", "token", "secret", "password", "auth", "cookie", "session")


def _redact(args: dict[str, Any], depth: int = 0) -> dict[str, Any]:
    """Audit records keep shape but never keep secrets."""
    out: dict[str, Any] = {}
    for key, value in args.items():
        low = key.lower()
        if any(h in low for h in _SECRET_HINTS):
            out[key] = "***"
        elif isinstance(value, dict) and depth < 3:
            out[key] = _redact(value, depth + 1)
        elif isinstance(value, str) and len(value) > 4000:
            out[key] = value[:4000] + f"…(+{len(value) - 4000} chars)"
        else:
            out[key] = value
    return out
