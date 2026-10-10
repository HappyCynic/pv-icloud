# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

"""Per-account SQLite FTS5 mail index ($PV_ICLOUD_DATA_DIR/<account_id>/mail.sqlite3)."""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from ..text import fold
from .parse import ParsedMessage, snippet

SCHEMA_VERSION = 2
BODY_INDEX_CHARS = 100_000
THREAD_MAX = 1000  # safety bound on thread discovery; results are paged by the service

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS folders (
    name          TEXT PRIMARY KEY,
    uidvalidity   INTEGER NOT NULL,
    highestmodseq INTEGER,
    uidnext       INTEGER,
    messages      INTEGER NOT NULL DEFAULT 0,
    synced_at     REAL
);
CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY,
    folder          TEXT NOT NULL,
    uidvalidity     INTEGER NOT NULL,
    uid             INTEGER NOT NULL,
    message_id      TEXT,
    in_reply_to     TEXT,
    date            REAL,
    from_addr       TEXT NOT NULL DEFAULT '',
    to_addr         TEXT NOT NULL DEFAULT '',
    cc_addr         TEXT NOT NULL DEFAULT '',
    subject         TEXT NOT NULL DEFAULT '',
    snippet         TEXT NOT NULL DEFAULT '',
    has_attachments INTEGER NOT NULL DEFAULT 0,
    size            INTEGER,
    UNIQUE (folder, uid)
);
CREATE INDEX IF NOT EXISTS messages_date ON messages (date DESC);
CREATE INDEX IF NOT EXISTS messages_msgid ON messages (message_id);
CREATE INDEX IF NOT EXISTS messages_irt ON messages (in_reply_to);
CREATE TABLE IF NOT EXISTS refs (
    msg INTEGER NOT NULL REFERENCES messages (id) ON DELETE CASCADE,
    ref TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS refs_ref ON refs (ref);
CREATE INDEX IF NOT EXISTS refs_msg ON refs (msg);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5 (
    subject, from_addr, to_addr, body, tokenize = 'unicode61 remove_diacritics 2'
);
"""

_TERM_RE = re.compile(r'"([^"]*)"|(\S+)')


def fts_query(text: str) -> str | None:
    """User text -> FTS5 query: every word or "quoted phrase" must match (AND)."""
    terms = []
    for phrase, word in _TERM_RE.findall(text):
        term = (phrase or word).strip()
        if term:
            terms.append('"' + term.replace('"', '""') + '"')
    return " ".join(terms) or None


def _like(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


@dataclass(frozen=True)
class IndexedFolder:
    name: str
    uidvalidity: int
    highestmodseq: int | None
    uidnext: int | None
    messages: int
    synced_at: float | None


@dataclass(frozen=True)
class IndexedMessage:
    folder: str
    uidvalidity: int
    uid: int
    message_id: str | None
    in_reply_to: str | None
    date: float | None
    from_addr: str
    to_addr: str
    cc_addr: str
    subject: str
    snippet: str
    has_attachments: bool
    size: int | None


_MSG_COLS = (
    "m.folder, m.uidvalidity, m.uid, m.message_id, m.in_reply_to, m.date, m.from_addr, "
    "m.to_addr, m.cc_addr, m.subject, m.snippet, m.has_attachments, m.size"
)


def _row_to_msg(row: Sequence) -> IndexedMessage:
    values = list(row)
    values[11] = bool(values[11])
    return IndexedMessage(*values)


class MailIndex:
    """Thread-safe: one SQLite connection per thread, WAL mode, writes serialized by a lock."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.Lock()
        with self._write_lock:
            db = self._db()
            db.executescript(_SCHEMA)
            row = db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
            if row is not None and int(row[0]) < 2:
                # v1 stored no date for messages without a Date header; drop them so the
                # next sync re-fetches them with the INTERNALDATE fallback.
                self._delete_where(db, "date IS NULL", ())
            db.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            db.commit()

    def _db(self) -> sqlite3.Connection:
        db = getattr(self._local, "db", None)
        if db is None:
            db = sqlite3.connect(self.path, timeout=30)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=NORMAL")
            # Case- and accent-insensitive matching for the from/to/subject filters (plain
            # LIKE only ignores ASCII case); FTS5 folds the same way for full-text queries.
            db.create_function("pv_fold", 1, lambda s: fold(s or ""), deterministic=True)
            self._local.db = db
        return db

    def close(self) -> None:
        db = getattr(self._local, "db", None)
        if db is not None:
            db.close()
            self._local.db = None

    # --- folders -----------------------------------------------------------------------

    def folders(self) -> list[IndexedFolder]:
        rows = self._db().execute(
            "SELECT name, uidvalidity, highestmodseq, uidnext, messages, synced_at FROM folders"
        )
        return [IndexedFolder(*r) for r in rows]

    def folder(self, name: str) -> IndexedFolder | None:
        row = (
            self._db()
            .execute(
                "SELECT name, uidvalidity, highestmodseq, uidnext, messages, synced_at "
                "FROM folders WHERE name = ?",
                (name,),
            )
            .fetchone()
        )
        return IndexedFolder(*row) if row else None

    def upsert_folder(
        self,
        name: str,
        uidvalidity: int,
        highestmodseq: int | None,
        uidnext: int | None,
        messages: int,
    ) -> None:
        """Record folder state after a sync. A changed UIDVALIDITY drops the folder's rows."""
        with self._write_lock:
            db = self._db()
            prev = db.execute("SELECT uidvalidity FROM folders WHERE name = ?", (name,)).fetchone()
            if prev and prev[0] != uidvalidity:
                self._delete_where(db, "folder = ?", (name,))
            db.execute(
                "INSERT INTO folders"
                " (name, uidvalidity, highestmodseq, uidnext, messages, synced_at)"
                " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (name) DO UPDATE SET"
                " uidvalidity = excluded.uidvalidity, highestmodseq = excluded.highestmodseq,"
                " uidnext = excluded.uidnext, messages = excluded.messages,"
                " synced_at = excluded.synced_at",
                (name, uidvalidity, highestmodseq, uidnext, messages, time.time()),
            )
            db.commit()

    def drop_folder(self, name: str) -> None:
        with self._write_lock:
            db = self._db()
            self._delete_where(db, "folder = ?", (name,))
            db.execute("DELETE FROM folders WHERE name = ?", (name,))
            db.commit()

    # --- messages ----------------------------------------------------------------------

    def uids(self, folder: str) -> set[int]:
        rows = self._db().execute("SELECT uid FROM messages WHERE folder = ?", (folder,))
        return {r[0] for r in rows}

    def max_uid(self, folder: str) -> int:
        row = (
            self._db()
            .execute("SELECT MAX(uid) FROM messages WHERE folder = ?", (folder,))
            .fetchone()
        )
        return row[0] or 0

    def add(
        self, folder: str, uidvalidity: int, uid: int, msg: ParsedMessage, size: int | None
    ) -> None:
        self.add_many([(folder, uidvalidity, uid, msg, size)])

    def add_many(self, items: Iterable[tuple[str, int, int, ParsedMessage, int | None]]) -> int:
        count = 0
        with self._write_lock:
            db = self._db()
            for folder, uidvalidity, uid, msg, size in items:
                self._delete_where(db, "folder = ? AND uid = ?", (folder, uid))
                cur = db.execute(
                    "INSERT INTO messages (folder, uidvalidity, uid, message_id, in_reply_to,"
                    " date, from_addr, to_addr, cc_addr, subject, snippet, has_attachments, size)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        folder,
                        uidvalidity,
                        uid,
                        msg.message_id,
                        msg.in_reply_to,
                        msg.date,
                        msg.from_,
                        msg.to,
                        msg.cc,
                        msg.subject,
                        snippet(msg.body),
                        int(msg.has_attachments),
                        size,
                    ),
                )
                rowid = cur.lastrowid
                db.execute(
                    "INSERT INTO messages_fts (rowid, subject, from_addr, to_addr, body)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        rowid,
                        msg.subject,
                        msg.from_,
                        f"{msg.to} {msg.cc}".strip(),
                        msg.body[:BODY_INDEX_CHARS],
                    ),
                )
                refs = set(msg.references)
                if msg.in_reply_to:
                    refs.add(msg.in_reply_to)
                db.executemany(
                    "INSERT INTO refs (msg, ref) VALUES (?, ?)", [(rowid, r) for r in refs]
                )
                count += 1
            db.commit()
        return count

    def remove(self, folder: str, uids: Iterable[int]) -> int:
        uids = list(uids)
        if not uids:
            return 0
        removed = 0
        with self._write_lock:
            db = self._db()
            for i in range(0, len(uids), 500):
                chunk = uids[i : i + 500]
                marks = ",".join("?" * len(chunk))
                removed += self._delete_where(
                    db, f"folder = ? AND uid IN ({marks})", (folder, *chunk)
                )
            db.commit()
        return removed

    @staticmethod
    def _delete_where(db: sqlite3.Connection, where: str, params: Sequence) -> int:
        ids = [r[0] for r in db.execute(f"SELECT id FROM messages WHERE {where}", params)]
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            marks = ",".join("?" * len(chunk))
            db.execute(f"DELETE FROM messages_fts WHERE rowid IN ({marks})", chunk)
            db.execute(f"DELETE FROM refs WHERE msg IN ({marks})", chunk)
            db.execute(f"DELETE FROM messages WHERE id IN ({marks})", chunk)
        return len(ids)

    def get(self, folder: str, uid: int) -> IndexedMessage | None:
        row = (
            self._db()
            .execute(
                f"SELECT {_MSG_COLS} FROM messages m WHERE m.folder = ? AND m.uid = ?",
                (folder, uid),
            )
            .fetchone()
        )
        return _row_to_msg(row) if row else None

    def count(self) -> int:
        return self._db().execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    # --- queries -----------------------------------------------------------------------

    def search(
        self,
        *,
        folders: Sequence[str],
        query: str | None = None,
        from_: str | None = None,
        to: str | None = None,
        subject: str | None = None,
        since: float | None = None,
        before: float | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[IndexedMessage]:
        if not folders:
            return []
        where = [f"m.folder IN ({','.join('?' * len(folders))})"]
        params: list = list(folders)
        join = ""
        match = fts_query(query) if query else None
        if match:
            join = "JOIN messages_fts f ON f.rowid = m.id"
            where.append("messages_fts MATCH ?")
            params.append(match)
        if from_:
            where.append("pv_fold(m.from_addr) LIKE ? ESCAPE '\\'")
            params.append(_like(fold(from_)))
        if to:
            where.append(
                "(pv_fold(m.to_addr) LIKE ? ESCAPE '\\' OR pv_fold(m.cc_addr) LIKE ? ESCAPE '\\')"
            )
            params += [_like(fold(to)), _like(fold(to))]
        if subject:
            where.append("pv_fold(m.subject) LIKE ? ESCAPE '\\'")
            params.append(_like(fold(subject)))
        if since is not None:
            where.append("m.date >= ?")
            params.append(since)
        if before is not None:
            where.append("m.date < ?")
            params.append(before)
        sql = (
            f"SELECT {_MSG_COLS} FROM messages m {join} WHERE {' AND '.join(where)}"
            " ORDER BY m.date IS NULL, m.date DESC, m.id DESC LIMIT ? OFFSET ?"
        )
        params += [limit, offset]
        return [_row_to_msg(r) for r in self._db().execute(sql, params)]

    def correspondence(
        self,
        *,
        folders: Sequence[str],
        terms: Sequence[str] = (),
        addresses: Sequence[str] = (),
        since: float | None = None,
        limit: int,
    ) -> list[IndexedMessage]:
        """Newest messages whose From/To/Cc contain every term (as word prefixes), or any of
        the addresses.

        A cheap pre-filter: terms may match across different people in one message, so the
        caller checks each address separately.
        """
        if not folders or not (terms or addresses):
            return []

        def phrase(text: str) -> str:
            return '"' + text.replace('"', '""') + '"'

        groups = []
        if terms:  # word prefixes: "jon" finds Jonathan
            groups.append(" AND ".join(phrase(t) + "*" for t in terms))
        if addresses:
            groups.append(" OR ".join(phrase(a) for a in addresses))
        match = "{from_addr to_addr} : (" + " OR ".join(f"({g})" for g in groups) + ")"
        where = ["messages_fts MATCH ?", f"m.folder IN ({','.join('?' * len(folders))})"]
        params: list = [match, *folders]
        if since is not None:
            where.append("m.date >= ?")
            params.append(since)
        sql = (
            f"SELECT {_MSG_COLS} FROM messages m JOIN messages_fts f ON f.rowid = m.id"
            f" WHERE {' AND '.join(where)}"
            " ORDER BY m.date IS NULL, m.date DESC, m.id DESC LIMIT ?"
        )
        params.append(limit)
        return [_row_to_msg(r) for r in self._db().execute(sql, params)]

    def thread(self, folder: str, uid: int, folders: Sequence[str]) -> list[IndexedMessage]:
        """Messages linked to (folder, uid) through Message-ID / In-Reply-To / References."""
        db = self._db()
        start = db.execute(
            "SELECT id, message_id FROM messages WHERE folder = ? AND uid = ?", (folder, uid)
        ).fetchone()
        if not start:
            return []
        member_ids: set[int] = {start[0]}
        keys: set[str] = set()
        frontier_ids = {start[0]}
        while frontier_ids and len(member_ids) < THREAD_MAX:
            marks = ",".join("?" * len(frontier_ids))
            new_keys = {
                r[0]
                for r in db.execute(
                    f"SELECT message_id FROM messages WHERE id IN ({marks}) AND message_id"
                    f" IS NOT NULL UNION SELECT ref FROM refs WHERE msg IN ({marks})",
                    (*frontier_ids, *frontier_ids),
                )
            } - keys
            if not new_keys:
                break
            keys |= new_keys
            kmarks = ",".join("?" * len(new_keys))
            found = {
                r[0]
                for r in db.execute(
                    f"SELECT id FROM messages WHERE message_id IN ({kmarks})"
                    f" UNION SELECT msg FROM refs WHERE ref IN ({kmarks})",
                    (*new_keys, *new_keys),
                )
            }
            frontier_ids = found - member_ids
            member_ids |= frontier_ids
        ids = sorted(member_ids)[:THREAD_MAX]
        if not folders:
            return []
        marks = ",".join("?" * len(ids))
        fmarks = ",".join("?" * len(folders))
        rows = db.execute(
            f"SELECT {_MSG_COLS} FROM messages m WHERE m.id IN ({marks}) AND m.folder IN"
            f" ({fmarks}) ORDER BY m.date IS NULL, m.date, m.id",
            (*ids, *folders),
        )
        return [_row_to_msg(r) for r in rows]
