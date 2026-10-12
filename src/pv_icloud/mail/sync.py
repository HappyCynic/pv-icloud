# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

"""Per-account sync worker: backfill, periodic incremental passes, IDLE on INBOX.

Change detection (per folder, each pass):
  * EXAMINE gives UIDVALIDITY, HIGHESTMODSEQ (CONDSTORE), UIDNEXT and EXISTS. If all match
    the index, the folder is skipped without further round trips.
  * Otherwise one `UID SEARCH SINCE <window>` returns the server's UID set. Index-only UIDs
    were expunged, moved away, or aged out of the window -> removed. Server-only UIDs are
    fetched (newest first) with BODY.PEEK and indexed.
  * A changed UIDVALIDITY drops the folder's rows and re-indexes it.
Message content is immutable in IMAP, so flag changes (the other half of CONDSTORE) are
irrelevant to a content index; this replaces QRESYNC VANISHED with an equivalent UID diff,
which needs no ENABLE/SELECT-parameter support from imaplib.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from ..config import AccountConfig
from .imap import FolderStatus, ImapClient, ImapError
from .index import MailIndex
from .parse import ParsedMessage, parse_message

log = logging.getLogger(__name__)

INDEX_FETCH_BYTES = 256 * 1024  # partial fetch per message for indexing
FETCH_BATCH = 50
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def imap_date(day: dt.date) -> str:
    return f"{day.day:02d}-{_MONTHS[day.month - 1]}-{day.year}"


def _unparseable(date: float | None) -> ParsedMessage:
    return ParsedMessage(
        message_id=None,
        in_reply_to=None,
        references=[],
        date=date,
        date_header=None,
        from_="",
        to="",
        cc="",
        subject="(unparseable message)",
        body="",
    )


@dataclass
class SyncStatus:
    state: str = "starting"  # starting | backfilling | ok | error | stopped
    last_pass_started: float | None = None
    last_success: float | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    passes: int = 0
    indexed: int = 0
    skipped_malformed: int = 0
    folder_errors: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "last_success": self.last_success,
            "last_error": self.last_error,
            "consecutive_failures": self.consecutive_failures,
            "passes": self.passes,
            "indexed_messages": self.indexed,
            "malformed_messages": self.skipped_malformed,
            "folder_errors": dict(self.folder_errors),
        }


class MailSync:
    def __init__(
        self,
        account: AccountConfig,
        index: MailIndex,
        client_factory: Callable[[], ImapClient],
        *,
        interval: float = 300.0,
        idle_chunk: float = 120.0,
        today: Callable[[], dt.date] = lambda: dt.datetime.now(dt.UTC).date(),
    ):
        self.account = account
        self.index = index
        self._client_factory = client_factory
        self.interval = interval
        self.idle_chunk = idle_chunk
        self._today = today
        self.status = SyncStatus()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._client: ImapClient | None = None
        self.inbox_changed = threading.Event()
        # Folders a mail write touched, synced before the next periodic pass.
        self._resync: set[str] = set()
        self._resync_lock = threading.Lock()
        self._wake = threading.Event()

    # --- one pass ----------------------------------------------------------------------

    def window_start(self) -> dt.date:
        return self._today() - dt.timedelta(days=self.account.mail.max_age_days)

    def sync_folder(self, client: ImapClient, name: str) -> int:
        """Bring one folder's index rows in line with the server. Returns messages added."""
        st: FolderStatus = client.examine(name)
        prev = self.index.folder(name)
        if prev and prev.uidvalidity != st.uidvalidity:
            log.info("%s: UIDVALIDITY changed for %r; re-indexing", self.account.id, name)
            self.index.drop_folder(name)
            prev = None
        if (
            prev
            and st.highestmodseq is not None
            and prev.highestmodseq == st.highestmodseq
            and prev.uidnext == st.uidnext
            and prev.messages == st.messages
        ):
            return 0

        server = (
            set(client.uid_search("SINCE", imap_date(self.window_start())))
            if st.messages
            else set()
        )
        local = self.index.uids(name)
        gone = local - server
        if gone:
            self.index.remove(name, gone)
        added = 0
        new = sorted(server - local, reverse=True)  # newest UIDs first
        for i in range(0, len(new), FETCH_BATCH):
            if self._stop.is_set():
                return added
            batch = new[i : i + FETCH_BATCH]
            rows = []
            for fetched in client.fetch(batch, INDEX_FETCH_BYTES, batch=FETCH_BATCH):
                try:
                    parsed = parse_message(
                        fetched.raw,
                        truncated=fetched.truncated,
                        fallback_date=fetched.internaldate,
                    )
                except Exception as exc:  # malformed: index a stub, keep going
                    log.warning(
                        "%s: unparseable %r uid %s: %s", self.account.id, name, fetched.uid, exc
                    )
                    self.status.skipped_malformed += 1
                    parsed = _unparseable(fetched.internaldate)
                rows.append((name, st.uidvalidity, fetched.uid, parsed, fetched.size))
            added += self.index.add_many(rows)
            self.status.indexed += len(rows)
        self.index.upsert_folder(name, st.uidvalidity, st.highestmodseq, st.uidnext, st.messages)
        return added

    def full_pass(self, client: ImapClient) -> int:
        scope = self.account.mail.folders
        names = [n for n in client.list_folders() if scope.allows(n)]
        for stale in {f.name for f in self.index.folders()} - set(names):
            # Excluded (or deleted) folders are purged from the index on the next sync.
            self.index.drop_folder(stale)
        names.sort(key=lambda n: (n.upper() != "INBOX", n))
        added = 0
        for name in names:
            if self._stop.is_set():
                break
            try:
                added += self.sync_folder(client, name)
                self.status.folder_errors.pop(name, None)
            except ImapError as exc:  # folder-level failure (e.g. NO on EXAMINE)
                log.warning("%s: folder %r failed: %s", self.account.id, name, exc)
                self.status.folder_errors[name] = str(exc)
        return added

    def request_resync(self, *folders: str) -> None:
        """Ask the worker to sync these folders soon (after a mail write), not in 5 minutes.

        Folders outside the read scope are skipped. During IDLE on INBOX the request waits
        for the current IDLE chunk to end; a write involving INBOX ends it at once.
        """
        with self._resync_lock:
            self._resync.update(folders)
        self._wake.set()

    def _sync_requested(self, client: ImapClient) -> None:
        with self._resync_lock:
            names, self._resync = sorted(self._resync), set()
            self._wake.clear()
        scope = self.account.mail.folders
        for name in names:
            if not scope.allows(name):
                continue
            try:
                self.sync_folder(client, name)
                self.status.folder_errors.pop(name, None)
            except ImapError as exc:
                log.warning("%s: folder %r failed: %s", self.account.id, name, exc)
                self.status.folder_errors[name] = str(exc)

    # --- worker thread -----------------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name=f"mail-sync-{self.account.id}", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout)
        if self._client:
            self._client.close()
        self.status.state = "stopped"

    def _backoff(self) -> float:
        return min(900.0, 30.0 * 2 ** max(0, self.status.consecutive_failures - 1))

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._session()
            except Exception as exc:  # connection-level; back off and retry
                self.status.state = "error"
                self.status.last_error = f"{type(exc).__name__}: {exc}"
                self.status.consecutive_failures += 1
                log.error("%s: sync failed: %s", self.account.id, self.status.last_error)
                if self._client:
                    self._client.close()
                    self._client = None
                self._stop.wait(self._backoff())

    def _session(self) -> None:
        client = self._client = self._client_factory()
        while not self._stop.is_set():
            self.status.last_pass_started = time.time()
            if self.status.passes == 0:
                self.status.state = "backfilling"
            self.full_pass(client)
            self.status.passes += 1
            self.status.state = "ok"
            self.status.last_success = time.time()
            self.status.last_error = None
            self.status.consecutive_failures = 0
            self._wait_for_next_pass(client)

    def _wait_for_next_pass(self, client: ImapClient) -> None:
        deadline = time.monotonic() + self.interval
        idle_ok = "IDLE" in client.capabilities and self.account.mail.folders.allows("INBOX")
        while not self._stop.is_set():
            self._sync_requested(client)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            if not idle_ok:
                self._wake.wait(remaining)  # set by request_resync() and stop()
                continue
            client.examine("INBOX")
            changed = False
            with client.idle(duration=min(remaining, self.idle_chunk)) as responses:
                for typ, _data in responses:
                    if typ in ("EXISTS", "EXPUNGE", "FETCH", "VANISHED"):
                        changed = True
                        break
            if changed:
                self.sync_folder(client, "INBOX")
                self.inbox_changed.set()
