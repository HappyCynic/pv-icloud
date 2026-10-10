# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

"""Mail tool implementations for one account. Blocking; the server runs them in threads."""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Any

from ..config import AccountConfig
from ..errors import ToolInputError
from .ids import BadMailId, MailId, decode_id
from .imap import ImapPool
from .index import IndexedMessage, MailIndex
from .parse import ParsedMessage, parse_message
from .sync import MailSync

MAX_FETCH_BYTES = 5 * 1024 * 1024
SEARCH_LIMIT_DEFAULT = 20
SEARCH_LIMIT_MAX = 50
THREAD_LIMIT_DEFAULT = 50
THREAD_LIMIT_MAX = 100
GET_MAX_CHARS = 20_000
WEB_URL = "https://www.icloud.com/mail/"
WEB_URL_NOTE = "iCloud Mail has no per-message deep links; this opens the mailbox."


def parse_when(value: str | None, field: str) -> dt.datetime | None:
    if value is None or value == "":
        return None
    try:
        when = dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise ToolInputError(f"{field}: expected ISO date (YYYY-MM-DD) or datetime") from exc
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.UTC)
    return when


def _iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return dt.datetime.fromtimestamp(epoch, dt.UTC).isoformat()


class MailService:
    def __init__(
        self, account: AccountConfig, index: MailIndex, pool: ImapPool, sync: MailSync | None
    ):
        self.account = account
        self.index = index
        self.pool = pool
        self.sync = sync

    # --- scope -------------------------------------------------------------------------

    def allowed_folders(self) -> list[str]:
        scope = self.account.mail.folders
        return [f.name for f in self.index.folders() if scope.allows(f.name)]

    def _check_folder(self, name: str) -> None:
        if not self.account.mail.folders.allows(name):
            # Same message as an unknown folder: don't confirm that an excluded one exists.
            raise ToolInputError(f"unknown folder: {name}")

    def window_start(self) -> dt.datetime:
        now = dt.datetime.now(dt.UTC)
        return now - dt.timedelta(days=self.account.mail.max_age_days)

    # --- tools -------------------------------------------------------------------------

    def folders(self) -> dict:
        indexed = [f for f in self.index.folders() if self.account.mail.folders.allows(f.name)]
        if indexed:
            rows: list[dict[str, Any]] = [{"name": f.name, "messages": f.messages} for f in indexed]
            synced = [f.synced_at for f in indexed if f.synced_at]
            source = "index"
            as_of = _iso(min(synced)) if synced else None
        else:  # first start, before the initial sync has recorded any folder
            with self.pool.connection() as client:
                names = [n for n in client.list_folders() if self.account.mail.folders.allows(n)]
                rows = [{"name": n, "messages": client.status(n).messages} for n in names]
            source, as_of = "live", _iso(dt.datetime.now(dt.UTC).timestamp())
        rows.sort(key=lambda r: (r["name"].upper() != "INBOX", r["name"].lower()))
        return {"folders": rows, "count_source": source, "counts_as_of": as_of}

    def search(
        self,
        *,
        query: str | None = None,
        from_: str | None = None,
        to: str | None = None,
        subject: str | None = None,
        folders: Sequence[str] | None = None,
        since: str | None = None,
        before: str | None = None,
        limit: int = SEARCH_LIMIT_DEFAULT,
        offset: int = 0,
    ) -> dict:
        if not 1 <= limit <= SEARCH_LIMIT_MAX:
            raise ToolInputError(f"limit must be between 1 and {SEARCH_LIMIT_MAX}")
        if offset < 0:
            raise ToolInputError("offset must be >= 0")
        since_dt = parse_when(since, "since")
        before_dt = parse_when(before, "before")
        window = self.window_start()
        clamped = since_dt is None or since_dt < window
        since_dt = max(since_dt or window, window)
        if folders:
            for name in folders:
                self._check_folder(name)
            known = set(self.allowed_folders())
            target = [f for f in folders if f in known]
        else:
            target = self.allowed_folders()

        # Always the index, even for one folder: iCloud's IMAP SEARCH is accent-sensitive and
        # matches substrings, so a live search would match differently (checked 2026-10-09).
        hits = self.index.search(
            folders=target,
            query=query,
            from_=from_,
            to=to,
            subject=subject,
            since=since_dt.timestamp(),
            before=before_dt.timestamp() if before_dt else None,
            limit=limit + 1,  # one extra row tells whether another page exists
            offset=offset,
        )
        more = len(hits) > limit
        out: dict = {
            "results": [self._summary(h) for h in hits[:limit]],
            "source": "index",
            "offset": offset,
            "next_offset": offset + limit if more else None,
        }
        if clamped:
            out["searched_since"] = since_dt.date().isoformat()
        if self.sync is not None:
            out["index_synced_at"] = _iso(self.sync.status.last_success)
        return out

    def _summary(self, m: IndexedMessage) -> dict:
        return {
            "id": MailId(m.folder, m.uidvalidity, m.uid).encode(),
            "folder": m.folder,
            "uid": m.uid,
            "message_id": m.message_id,
            "date": _iso(m.date),
            "from": m.from_addr,
            "to": m.to_addr,
            "subject": m.subject,
            "snippet": m.snippet,
            "has_attachments": m.has_attachments,
        }

    def _resolve(self, mail_id: str) -> MailId:
        try:
            mid = decode_id(mail_id)
        except BadMailId as exc:
            raise ToolInputError(str(exc)) from exc
        self._check_folder(mid.folder)
        return mid

    def _fetch_one(self, mid: MailId) -> tuple[ParsedMessage, int | None]:
        with self.pool.connection() as client:
            st = client.examine(mid.folder)
            if st.uidvalidity != mid.uidvalidity:
                raise ToolInputError("stale id (folder UIDVALIDITY changed); search again")
            fetched = list(client.fetch([mid.uid], MAX_FETCH_BYTES))
        if not fetched:
            raise ToolInputError("message not found (deleted or moved); search again")
        f = fetched[0]
        try:
            return parse_message(f.raw, truncated=f.truncated, fallback_date=f.internaldate), f.size
        except Exception as exc:
            raise ToolInputError(f"message could not be parsed: {type(exc).__name__}") from exc

    def get(self, mail_id: str, max_chars: int = GET_MAX_CHARS, offset: int = 0) -> dict:
        if not 1 <= max_chars <= GET_MAX_CHARS:
            raise ToolInputError(f"max_chars must be between 1 and {GET_MAX_CHARS}")
        if offset < 0:
            raise ToolInputError("offset must be >= 0")
        mid = self._resolve(mail_id)
        p, size = self._fetch_one(mid)
        body = p.body[offset : offset + max_chars]
        end = offset + len(body)
        return {
            "id": mail_id,
            "folder": mid.folder,
            "uid": mid.uid,
            "headers": {
                "message_id": p.message_id,
                "in_reply_to": p.in_reply_to,
                "references": p.references,
                "date": _iso(p.date),
                "date_header": p.date_header,
                "from": p.from_,
                "to": p.to,
                "cc": p.cc,
                "subject": p.subject,
            },
            "body": body,
            "body_offset": offset,
            "body_total_chars": len(p.body),
            "next_offset": end if end < len(p.body) else None,
            "message_truncated": p.truncated,
            "size_bytes": size,
            "attachments": [
                {"filename": a.filename, "content_type": a.content_type, "size": a.size}
                for a in p.attachments
            ],
            "web_url": WEB_URL,
            "web_url_note": WEB_URL_NOTE,
        }

    def thread(self, mail_id: str, limit: int = THREAD_LIMIT_DEFAULT, offset: int = 0) -> dict:
        if not 1 <= limit <= THREAD_LIMIT_MAX:
            raise ToolInputError(f"limit must be between 1 and {THREAD_LIMIT_MAX}")
        if offset < 0:
            raise ToolInputError("offset must be >= 0")
        mid = self._resolve(mail_id)
        if self.index.get(mid.folder, mid.uid) is None:
            # Not indexed yet (newer than the last sync): read it live and index it.
            parsed, size = self._fetch_one(mid)
            self.index.add(mid.folder, mid.uidvalidity, mid.uid, parsed, size)
        members = self.index.thread(mid.folder, mid.uid, self.allowed_folders())
        page = members[offset : offset + limit]
        end = offset + len(page)
        return {
            "messages": [self._summary(m) for m in page],
            "source": "index",
            "total": len(members),
            "offset": offset,
            "next_offset": end if end < len(members) else None,
        }
