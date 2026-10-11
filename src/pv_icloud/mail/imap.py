# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

"""Read-only IMAP client over imaplib, plus a small per-account connection pool.

Only read-only commands are ever issued: CAPABILITY (sent by imaplib on connect), LOGIN,
ENABLE, LIST, STATUS, EXAMINE, UID SEARCH, UID FETCH with BODY.PEEK, NOOP, IDLE, LOGOUT.
There is deliberately no select(readonly=False), STORE, COPY, MOVE, EXPUNGE or APPEND anywhere
in this module.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import imaplib
import logging
import queue
import re
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from .. import utf7

log = logging.getLogger(__name__)

IMAP_HOST = "imap.mail.me.com"
IMAP_PORT = 993

ImapErrors = (imaplib.IMAP4.error, OSError, EOFError)

_LIST_RE = re.compile(rb'^\((?P<flags>[^)]*)\) (?P<delim>"(?:[^"\\]|\\.)*"|NIL) (?P<name>.*)$')
_STATUS_ITEM_RE = re.compile(rb"(MESSAGES|UIDNEXT|UIDVALIDITY|HIGHESTMODSEQ) (\d+)")
_FETCH_UID_RE = re.compile(rb"\bUID (\d+)")
_FETCH_SIZE_RE = re.compile(rb"\bRFC822\.SIZE (\d+)")
_FETCH_INTERNALDATE_RE = re.compile(rb'\bINTERNALDATE "([^"]+)"')


class ImapError(Exception):
    pass


@dataclass(frozen=True)
class FolderStatus:
    name: str
    messages: int
    uidnext: int | None
    uidvalidity: int
    highestmodseq: int | None


@dataclass(frozen=True)
class FetchedMessage:
    uid: int
    size: int | None
    raw: bytes
    truncated: bool
    internaldate: float | None = None  # server receipt time, epoch UTC


def quote(name: str) -> str:
    """Wire form of a mailbox name: modified UTF-7, quoted."""
    encoded = utf7.encode(name)
    return '"' + encoded.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _unquote(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace").strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = re.sub(r"\\(.)", r"\1", text[1:-1])
    return text


def uid_set(uids: Iterable[int]) -> str:
    """Compress sorted UIDs into an IMAP sequence set, e.g. 1:3,7,9:10."""
    ordered = sorted(set(uids))
    parts: list[str] = []
    i = 0
    while i < len(ordered):
        j = i
        while j + 1 < len(ordered) and ordered[j + 1] == ordered[j] + 1:
            j += 1
        parts.append(str(ordered[i]) if i == j else f"{ordered[i]}:{ordered[j]}")
        i = j + 1
    return ",".join(parts)


def _check(typ: str, data: object, what: str) -> None:
    if typ != "OK":
        raise ImapError(f"{what} failed: {typ} {data!r}")


ConnFactory = Callable[[], imaplib.IMAP4]


def default_conn_factory(timeout: float = 30.0) -> ConnFactory:
    return lambda: imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=timeout)


class ImapClient:
    """One authenticated connection. Not thread-safe; the pool hands it to one user at a time."""

    def __init__(self, user: str, password: str, conn_factory: ConnFactory):
        self._user = user
        self._password = password
        self._factory = conn_factory
        self.conn: imaplib.IMAP4 | None = None
        self.selected: str | None = None
        self.last_used = 0.0

    def connect(self) -> None:
        conn = self._factory()
        try:
            _check(*conn.login(self._user, self._password), "LOGIN")
        except Exception:
            with contextlib.suppress(Exception):
                conn.shutdown()
            raise
        # iCloud reports HIGHESTMODSEQ (used to skip unchanged folders) only after ENABLE.
        if "CONDSTORE" in getattr(conn, "capabilities", ()):
            with contextlib.suppress(imaplib.IMAP4.error):
                conn.enable("CONDSTORE")
        self.conn = conn
        self.selected = None
        self.last_used = time.monotonic()

    def _c(self) -> imaplib.IMAP4:
        if self.conn is None:
            raise ImapError("not connected")
        self.last_used = time.monotonic()
        return self.conn

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset(getattr(self._c(), "capabilities", ()))

    def noop(self) -> None:
        _check(*self._c().noop(), "NOOP")

    def close(self) -> None:
        if self.conn is not None:
            with contextlib.suppress(Exception):
                self.conn.logout()
        self.conn = None
        self.selected = None

    def list_folders(self) -> list[str]:
        typ, data = self._c().list()
        _check(typ, data, "LIST")
        names: list[str] = []
        for item in data:
            if item is None:
                continue
            if isinstance(item, tuple):  # name sent as a literal
                head, literal = item
                m = _LIST_RE.match(head)
                flags = m.group("flags") if m else b""
                raw_name = literal
            else:
                m = _LIST_RE.match(item)
                if not m:
                    log.warning("unparsable LIST line: %r", item)
                    continue
                flags, raw_name = m.group("flags"), m.group("name")
            if b"\\noselect" in flags.lower() or b"\\nonexistent" in flags.lower():
                continue
            names.append(utf7.decode(_unquote(raw_name)))
        return names

    def status(self, folder: str) -> FolderStatus:
        typ, data = self._c().status(quote(folder), "(MESSAGES UIDNEXT UIDVALIDITY HIGHESTMODSEQ)")
        _check(typ, data, f"STATUS {folder}")
        line = b" ".join(x if isinstance(x, bytes) else b" ".join(x) for x in data if x)
        items = {k.decode(): int(v) for k, v in _STATUS_ITEM_RE.findall(line)}
        return FolderStatus(
            name=folder,
            messages=items.get("MESSAGES", 0),
            uidnext=items.get("UIDNEXT"),
            uidvalidity=items.get("UIDVALIDITY", 0),
            highestmodseq=items.get("HIGHESTMODSEQ"),
        )

    def examine(self, folder: str) -> FolderStatus:
        """Open a folder read-only (EXAMINE). Never SELECT: that could change \\Recent/\\Seen."""
        conn = self._c()
        typ, data = conn.select(quote(folder), readonly=True)
        if typ != "OK":
            self.selected = None
            raise ImapError(f"EXAMINE {folder} failed: {typ} {data!r}")
        self.selected = folder

        def code(name: str) -> int | None:
            _, vals = conn.response(name)
            for v in vals or []:
                if v is None:
                    continue
                with contextlib.suppress(ValueError):
                    return int(v.split()[0])
            return None

        exists = int(data[0]) if data and data[0] else 0
        return FolderStatus(
            name=folder,
            messages=exists,
            uidnext=code("UIDNEXT"),
            uidvalidity=code("UIDVALIDITY") or 0,
            highestmodseq=code("HIGHESTMODSEQ"),
        )

    def uid_search(self, *criteria: str) -> list[int]:
        """UID SEARCH in the examined folder. Criteria must be ASCII (callers check)."""
        typ, data = self._c().uid("SEARCH", *criteria)
        _check(typ, data, "UID SEARCH")
        uids: list[int] = []
        for chunk in data:
            if chunk:
                uids.extend(int(x) for x in chunk.split() if x.isdigit())
        return uids

    def fetch(
        self, uids: Iterable[int], max_bytes: int, batch: int = 50
    ) -> Iterator[FetchedMessage]:
        """UID FETCH BODY.PEEK[]<0.max_bytes> (PEEK: never sets \\Seen)."""
        ordered = sorted(set(uids))
        for i in range(0, len(ordered), batch):
            chunk = ordered[i : i + batch]
            typ, data = self._c().uid(
                "FETCH",
                uid_set(chunk),
                f"(UID RFC822.SIZE INTERNALDATE BODY.PEEK[]<0.{max_bytes}>)",
            )
            _check(typ, data, "UID FETCH")
            yield from _parse_fetch(data, max_bytes)

    @contextlib.contextmanager
    def idle(self, duration: float) -> Iterator[Iterable[tuple[str, Any]]]:
        """IMAP IDLE (Python 3.14 imaplib) on the examined folder."""
        with self._c().idle(duration=duration) as idler:
            yield idler


def _parse_fetch(data: list, max_bytes: int) -> Iterator[FetchedMessage]:
    meta = b""
    raw: bytes | None = None

    def emit() -> FetchedMessage | None:
        uid_m = _FETCH_UID_RE.search(meta)
        if raw is None or not uid_m:
            return None
        size_m = _FETCH_SIZE_RE.search(meta)
        size = int(size_m.group(1)) if size_m else None
        truncated = size > max_bytes if size is not None else len(raw) >= max_bytes
        date_m = _FETCH_INTERNALDATE_RE.search(meta)
        return FetchedMessage(
            uid=int(uid_m.group(1)),
            size=size,
            raw=raw,
            truncated=truncated,
            internaldate=_internaldate(date_m.group(1)) if date_m else None,
        )

    for item in data:
        if isinstance(item, tuple):
            done = emit()
            if done:
                yield done
            meta, raw = item[0], item[1]
        elif isinstance(item, bytes):
            meta += b" " + item
    done = emit()
    if done:
        yield done


def _internaldate(raw: bytes) -> float | None:
    """'08-Oct-2026 20:45:01 +0000' (day may be space-padded) -> epoch seconds."""
    try:
        when = dt.datetime.strptime(raw.decode("ascii").strip(), "%d-%b-%Y %H:%M:%S %z")
    except ValueError:
        return None
    return when.timestamp()


class ImapPool:
    """Bounded pool of ImapClients for one account (live tool calls; sync has its own)."""

    def __init__(
        self,
        user: str,
        password: str,
        size: int,
        conn_factory: ConnFactory,
        idle_check_after: float = 60.0,
    ):
        self._user = user
        self._password = password
        self._factory = conn_factory
        self._idle_check_after = idle_check_after
        self._slots = threading.BoundedSemaphore(size)
        self._free: queue.LifoQueue[ImapClient] = queue.LifoQueue()
        self._closed = False

    def new_client(self) -> ImapClient:
        client = ImapClient(self._user, self._password, self._factory)
        client.connect()
        return client

    @contextlib.contextmanager
    def connection(self, timeout: float = 30.0) -> Iterator[ImapClient]:
        if not self._slots.acquire(timeout=timeout):
            raise ImapError("timed out waiting for an IMAP connection")
        client: ImapClient | None = None
        try:
            client = self._checkout()
            yield client
        except ImapErrors:
            if client is not None:
                client.close()
                client = None
            raise
        finally:
            if client is not None and not self._closed:
                self._free.put(client)
            elif client is not None:
                client.close()
            self._slots.release()

    def _checkout(self) -> ImapClient:
        while True:
            try:
                client = self._free.get_nowait()
            except queue.Empty:
                return self.new_client()
            if time.monotonic() - client.last_used < self._idle_check_after:
                return client
            try:
                client.noop()
                return client
            except ImapErrors:
                client.close()

    def close(self) -> None:
        self._closed = True
        while True:
            try:
                self._free.get_nowait().close()
            except queue.Empty:
                return
