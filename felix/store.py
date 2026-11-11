"""Local SQLite store. Everything Felix knows lives in this one file.

One connection per thread, WAL mode, and a process-wide write lock so that the
scheduler, the HTTP server and the phone link never trip over each other.
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from .util import json_dumps, json_loads

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS contacts (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  emails TEXT NOT NULL DEFAULT '[]',
  phones TEXT NOT NULL DEFAULT '[]',
  org TEXT, notes TEXT,
  vip INTEGER NOT NULL DEFAULT 0,
  source TEXT, updated_at REAL
);
CREATE INDEX IF NOT EXISTS contacts_name ON contacts(name);

CREATE TABLE IF NOT EXISTS emails (
  id INTEGER PRIMARY KEY,
  account TEXT NOT NULL, folder TEXT NOT NULL, uid INTEGER NOT NULL, uidvalidity INTEGER NOT NULL DEFAULT 0,
  message_id TEXT, in_reply_to TEXT, refs TEXT, thread_key TEXT,
  from_name TEXT, from_addr TEXT, to_addrs TEXT DEFAULT '[]', cc_addrs TEXT DEFAULT '[]', reply_to TEXT,
  subject TEXT, date_ts REAL, snippet TEXT, body TEXT,
  is_read INTEGER DEFAULT 0, is_flagged INTEGER DEFAULT 0, is_answered INTEGER DEFAULT 0,
  attachments TEXT DEFAULT '[]', size INTEGER,
  is_bulk INTEGER DEFAULT 0, direction TEXT DEFAULT 'in',
  removed INTEGER DEFAULT 0, synced_at REAL,
  UNIQUE(account, folder, uid, uidvalidity)
);
CREATE INDEX IF NOT EXISTS emails_date ON emails(date_ts);
CREATE INDEX IF NOT EXISTS emails_thread ON emails(thread_key);
CREATE INDEX IF NOT EXISTS emails_msgid ON emails(message_id);

CREATE TABLE IF NOT EXISTS mail_folders (
  account TEXT NOT NULL, folder TEXT NOT NULL, uidvalidity INTEGER, last_uid INTEGER DEFAULT 0,
  last_sync REAL, PRIMARY KEY(account, folder)
);

CREATE TABLE IF NOT EXISTS chats (
  id INTEGER PRIMARY KEY,
  channel TEXT NOT NULL, key TEXT NOT NULL,
  name TEXT, is_group INTEGER DEFAULT 0, address TEXT,
  last_ts REAL, unread INTEGER DEFAULT 0, muted INTEGER DEFAULT 0,
  transport TEXT, reply_token TEXT, reply_token_ts REAL,
  UNIQUE(channel, key)
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY,
  chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
  remote_id TEXT, direction TEXT NOT NULL,
  sender TEXT, sender_addr TEXT, text TEXT, ts REAL NOT NULL,
  status TEXT, transport TEXT,
  UNIQUE(chat_id, remote_id)
);
CREATE INDEX IF NOT EXISTS messages_chat_ts ON messages(chat_id, ts);
CREATE INDEX IF NOT EXISTS messages_ts ON messages(ts);

CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY, device_id TEXT, package TEXT, app TEXT, key TEXT,
  title TEXT, text TEXT, ts REAL, category TEXT, removed INTEGER DEFAULT 0,
  UNIQUE(device_id, key, ts)
);
CREATE INDEX IF NOT EXISTS notifications_ts ON notifications(ts);

CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL, ref_id INTEGER NOT NULL, ts REAL NOT NULL,
  source TEXT, who TEXT, who_addr TEXT, title TEXT, snippet TEXT,
  sentiment REAL, urgency REAL, severity REAL,
  categories TEXT DEFAULT '[]', topic TEXT,
  needs_reply INTEGER DEFAULT 0, vip INTEGER DEFAULT 0,
  engine TEXT, resolved INTEGER DEFAULT 0,
  UNIQUE(kind, ref_id)
);
CREATE INDEX IF NOT EXISTS signals_ts ON signals(ts);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL, remote_id TEXT, calendar TEXT, uid TEXT, series_id TEXT,
  title TEXT, description TEXT, location TEXT,
  start_ts REAL NOT NULL, end_ts REAL NOT NULL, all_day INTEGER DEFAULT 0, tz TEXT,
  rrule TEXT, exdates TEXT DEFAULT '[]', recurrence_id REAL,
  attendees TEXT DEFAULT '[]', organizer TEXT, status TEXT DEFAULT 'confirmed',
  my_response TEXT, reminders TEXT DEFAULT '[]', url TEXT,
  etag TEXT, updated_at REAL, expanded INTEGER DEFAULT 0,
  UNIQUE(source, remote_id)
);
CREATE INDEX IF NOT EXISTS events_time ON events(start_ts, end_ts);

CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL, notes TEXT,
  due_ts REAL, remind_ts REAL, reminded INTEGER DEFAULT 0,
  priority INTEGER DEFAULT 2, status TEXT DEFAULT 'open',
  origin TEXT, origin_ref TEXT, suggested INTEGER DEFAULT 0,
  created_at REAL, done_at REAL
);
CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status, due_ts);

CREATE TABLE IF NOT EXISTS notes (
  id INTEGER PRIMARY KEY, text TEXT NOT NULL, tags TEXT, created_at REAL, updated_at REAL
);

CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL, payload TEXT NOT NULL, summary TEXT,
  status TEXT DEFAULT 'queued', attempts INTEGER DEFAULT 0, last_error TEXT,
  created_at REAL, next_try REAL, sent_at REAL, approval_id INTEGER
);
CREATE INDEX IF NOT EXISTS outbox_status ON outbox(status, next_try);

CREATE TABLE IF NOT EXISTS approvals (
  id INTEGER PRIMARY KEY,
  tool TEXT NOT NULL, args TEXT NOT NULL, preview TEXT, risk TEXT, channel TEXT,
  status TEXT DEFAULT 'pending', thread TEXT, source TEXT,
  created_at REAL, expires_at REAL, decided_at REAL, decided_via TEXT, result TEXT
);
CREATE INDEX IF NOT EXISTS approvals_status ON approvals(status, created_at);

CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY, ts REAL, actor TEXT, action TEXT, detail TEXT, outcome TEXT
);
CREATE INDEX IF NOT EXISTS audit_ts ON audit(ts);

CREATE TABLE IF NOT EXISTS conversations (
  id INTEGER PRIMARY KEY, thread TEXT NOT NULL, engine TEXT NOT NULL,
  created_at REAL, updated_at REAL, closed INTEGER DEFAULT 0, input_tokens INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS conversations_thread ON conversations(thread, closed, updated_at);
CREATE TABLE IF NOT EXISTS turns (
  id INTEGER PRIMARY KEY,
  conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  role TEXT NOT NULL, text TEXT, native TEXT, ts REAL, meta TEXT
);
CREATE INDEX IF NOT EXISTS turns_conv ON turns(conversation_id, id);

CREATE TABLE IF NOT EXISTS devices (
  id TEXT PRIMARY KEY, name TEXT, token_hash TEXT NOT NULL,
  model TEXT, os TEXT, app_version TEXT,
  paired_at REAL, last_seen REAL, state TEXT DEFAULT '{}', capabilities TEXT DEFAULT '{}',
  revoked INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS phone_commands (
  id INTEGER PRIMARY KEY, device_id TEXT NOT NULL, type TEXT NOT NULL, args TEXT,
  status TEXT DEFAULT 'queued', created_at REAL, sent_at REAL, done_at REAL,
  expires_at REAL, result TEXT
);
CREATE INDEX IF NOT EXISTS phone_commands_q ON phone_commands(device_id, status, id);

CREATE TABLE IF NOT EXISTS feed_items (
  id INTEGER PRIMARY KEY, feed TEXT NOT NULL, guid TEXT NOT NULL,
  title TEXT, link TEXT, summary TEXT, ts REAL, UNIQUE(feed, guid)
);

CREATE TABLE IF NOT EXISTS briefs (
  id INTEGER PRIMARY KEY, kind TEXT, day TEXT, text TEXT, data TEXT,
  created_at REAL, delivered_via TEXT, delivered_at REAL, status TEXT, engine TEXT
);
CREATE INDEX IF NOT EXISTS briefs_day ON briefs(day, kind);

CREATE TABLE IF NOT EXISTS jobs (
  name TEXT PRIMARY KEY, last_run REAL, last_ok REAL, last_error TEXT, runs INTEGER DEFAULT 0
);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(
  kind UNINDEXED, ref UNINDEXED, title, body, who, ts UNINDEXED,
  tokenize = 'unicode61 remove_diacritics 2'
);
"""

FALLBACK_SEARCH_SCHEMA = """
CREATE TABLE IF NOT EXISTS search (kind TEXT, ref INTEGER, title TEXT, body TEXT, who TEXT, ts REAL);
CREATE INDEX IF NOT EXISTS search_ref ON search(kind, ref);
"""

_JSON_COLUMNS = {
    "emails", "phones", "to_addrs", "cc_addrs", "attachments", "categories", "exdates",
    "attendees", "reminders", "args", "result", "payload", "detail", "native", "meta",
    "state", "capabilities", "data",
}


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self._memory_conn: sqlite3.Connection | None = None
        self.fts = False
        if self.path == ":memory:":
            # A private in-memory database is per-connection, so share one.
            self._memory_conn = self._open()
        self._init_schema()

    # -- connections -----------------------------------------------------
    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        if self.path != ":memory:":
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        if self._memory_conn is not None:
            return self._memory_conn
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._open()
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
        if self._memory_conn is not None:
            self._memory_conn.close()
            self._memory_conn = None

    def _init_schema(self) -> None:
        with self._write_lock:
            conn = self.conn
            conn.executescript(SCHEMA)
            try:
                conn.executescript(FTS_SCHEMA)
                self.fts = True
            except sqlite3.OperationalError:
                conn.executescript(FALLBACK_SEARCH_SCHEMA)
                self.fts = False
            else:
                # An older fallback table would shadow FTS; detect which one we have.
                sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='search'").fetchone()
                self.fts = bool(sql and "fts5" in (sql[0] or "").lower())
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    # -- primitives ------------------------------------------------------
    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Serialized write transaction."""
        with self._write_lock:
            conn = self.conn
            nested = conn.in_transaction
            if not nested:
                conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                if not nested:
                    conn.execute("ROLLBACK")
                raise
            else:
                if not nested:
                    conn.execute("COMMIT")

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self.tx() as conn:
            return conn.execute(sql, tuple(params))

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        cur = self.conn.execute(sql, tuple(params))
        return [_decode(dict(row)) for row in cur.fetchall()]

    def one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        cur = self.conn.execute(sql, tuple(params))
        row = cur.fetchone()
        return _decode(dict(row)) if row is not None else None

    def scalar(self, sql: str, params: Iterable[Any] = (), default: Any = None) -> Any:
        row = self.conn.execute(sql, tuple(params)).fetchone()
        return row[0] if row is not None and row[0] is not None else default

    def insert(self, table: str, values: dict[str, Any], replace: bool = False, ignore: bool = False) -> int:
        cols = list(values)
        verb = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE" if ignore else "INSERT"
        sql = f"{verb} INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
        with self.tx() as conn:
            cur = conn.execute(sql, [_encode(values[c]) for c in cols])
            return int(cur.lastrowid or 0) if cur.rowcount else 0

    def update(self, table: str, values: dict[str, Any], where: str, params: Iterable[Any] = ()) -> int:
        if not values:
            return 0
        sets = ", ".join(f"{c} = ?" for c in values)
        with self.tx() as conn:
            cur = conn.execute(
                f"UPDATE {table} SET {sets} WHERE {where}",
                [_encode(v) for v in values.values()] + list(params),
            )
            return cur.rowcount

    def delete(self, table: str, where: str, params: Iterable[Any] = ()) -> int:
        with self.tx() as conn:
            return conn.execute(f"DELETE FROM {table} WHERE {where}", tuple(params)).rowcount

    # -- meta ------------------------------------------------------------
    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return json_loads(row[0], default) if row else default

    def set_meta(self, key: str, value: Any) -> None:
        self.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, json_dumps(value)))

    # -- audit -----------------------------------------------------------
    def audit(self, actor: str, action: str, detail: Any = None, outcome: str = "ok") -> None:
        self.insert(
            "audit",
            {"ts": time.time(), "actor": actor, "action": action, "detail": detail or {}, "outcome": outcome},
        )

    # -- search ----------------------------------------------------------
    def index(self, kind: str, ref: int, title: str, body: str = "", who: str = "", ts: float = 0.0) -> None:
        with self.tx() as conn:
            conn.execute("DELETE FROM search WHERE kind = ? AND ref = ?", (kind, ref))
            conn.execute(
                "INSERT INTO search (kind, ref, title, body, who, ts) VALUES (?, ?, ?, ?, ?, ?)",
                (kind, ref, title or "", body or "", who or "", ts),
            )

    def unindex(self, kind: str, ref: int) -> None:
        self.execute("DELETE FROM search WHERE kind = ? AND ref = ?", (kind, ref))

    def search(self, text: str, kinds: Iterable[str] | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """Full-text search across emails, messages, events, tasks and notes."""
        terms = [t for t in re.findall(r"[\w@.+-]+", text or "", flags=re.UNICODE) if len(t) > 1]
        if not terms:
            return []
        kinds = list(kinds or [])
        kind_sql = f" AND kind IN ({', '.join('?' for _ in kinds)})" if kinds else ""
        if self.fts:
            match = " ".join('"' + t.replace('"', '""') + '"*' for t in terms)
            sql = (
                "SELECT kind, ref, title, snippet(search, 3, '', '', ' … ', 18) AS excerpt, who, ts "
                f"FROM search WHERE search MATCH ?{kind_sql} ORDER BY bm25(search), ts DESC LIMIT ?"
            )
            try:
                return self.query(sql, [match, *kinds, limit])
            except sqlite3.OperationalError:
                pass  # malformed MATCH expression: fall through to LIKE
        clauses = " AND ".join("(title LIKE ? OR body LIKE ? OR who LIKE ?)" for _ in terms)
        params: list[Any] = []
        for term in terms:
            like = f"%{term}%"
            params += [like, like, like]
        sql = (
            "SELECT kind, ref, title, substr(body, 1, 160) AS excerpt, who, ts "
            f"FROM search WHERE {clauses}{kind_sql} ORDER BY ts DESC LIMIT ?"
        )
        return self.query(sql, [*params, *kinds, limit])


def _encode(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple, set)):
        return json_dumps(value)
    if isinstance(value, bool):
        return int(value)
    return value


def _decode(row: dict[str, Any]) -> dict[str, Any]:
    for key in row:
        if key in _JSON_COLUMNS and isinstance(row[key], str):
            text = row[key]
            if text[:1] in "[{":
                row[key] = json_loads(text, text)
    return row
