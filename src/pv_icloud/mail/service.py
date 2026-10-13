# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

"""Mail tool implementations for one account. Blocking; the server runs them in threads."""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from ..config import AccountConfig
from ..errors import ToolInputError, WriteFailed
from .compose import (
    Original,
    Outgoing,
    addr_specs,
    build_draft,
    build_outgoing,
    check_draft,
    check_send,
    not_allowed,
)
from .ids import BadMailId, MailId, decode_id
from .imap import ImapError, ImapErrors, ImapPool
from .index import IndexedMessage, MailIndex
from .parse import ParsedMessage, parse_message
from .sync import MailSync

if TYPE_CHECKING:  # imported at run time only when mail writes are switched on
    from .imap_write import MailWriter
    from .smtp import SmtpSender

log = logging.getLogger(__name__)

NOT_ALLOWED = "recipients not allowed for this token"
MAX_FETCH_BYTES = 5 * 1024 * 1024
SEARCH_LIMIT_DEFAULT = 20
SEARCH_LIMIT_MAX = 50
THREAD_LIMIT_DEFAULT = 50
THREAD_LIMIT_MAX = 100
GET_MAX_CHARS = 20_000
WEB_URL = "https://www.icloud.com/mail/"
WEB_URL_NOTE = "iCloud Mail has no per-message deep links; this opens the mailbox."
TRASH = "Deleted Messages"
PROTECTED = "folder is protected"


def _protected(name: str, protected: Iterable[str]) -> bool:
    """Exact names, except INBOX, which IMAP treats case-insensitively."""
    if name.upper() == "INBOX":
        return any(p.upper() == "INBOX" for p in protected)
    return name in protected


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
        self,
        account: AccountConfig,
        index: MailIndex,
        pool: ImapPool,
        sync: MailSync | None,
        writer: MailWriter | None = None,  # only when PV_ICLOUD_MAIL_WRITE=enabled
        sender: SmtpSender | None = None,  # likewise
    ):
        self.account = account
        self.index = index
        self.pool = pool
        self.sync = sync
        self.writer = writer
        self.sender = sender

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

    # --- writes (ADR-0004) -------------------------------------------------------------
    # Every refusal here happens before mail/imap_write.py sends anything that can change
    # mail. `token_protected` is the calling token's own protected_folders.

    def move(self, mail_id: str, folder: str, token_protected: Sequence[str] = ()) -> dict:
        mid, protected = self._write_source(mail_id, token_protected)
        self._check_folder(folder)  # the caller's destination must be in scope
        if _protected(folder, protected):
            raise ToolInputError(PROTECTED)
        if folder == mid.folder:
            raise ToolInputError("message is already in that folder")

        def resolve(folders: Mapping[str, frozenset[str]]) -> str:
            if folder not in folders:
                raise ToolInputError(f"unknown folder: {folder}")
            return folder

        return self._move(mid, resolve)

    def archive(self, mail_id: str, token_protected: Sequence[str] = ()) -> dict:
        mid, protected = self._write_source(mail_id, token_protected)
        archive = self.account.mail.write.archive_folder  # by name, even if hidden from reads
        if _protected(archive, protected):
            raise ToolInputError(PROTECTED)
        if mid.folder == archive:
            raise ToolInputError("message is already in the archive folder")

        def resolve(folders: Mapping[str, frozenset[str]]) -> str:
            from .imap_write import find_archive

            found = find_archive(folders, archive)
            if found is None:
                raise ToolInputError("archive folder not found; nothing was changed")
            return found

        return self._move(mid, resolve)

    def delete(self, mail_id: str, token_protected: Sequence[str] = ()) -> dict:
        """Move to Deleted Messages (even if hidden from reads). Never deletes permanently."""
        mid, protected = self._write_source(mail_id, token_protected)
        if mid.folder == TRASH:
            raise ToolInputError("already in Deleted Messages")

        def resolve(folders: Mapping[str, frozenset[str]]) -> str:
            from .imap_write import find_trash

            trash = find_trash(folders)
            if trash is None:
                raise ToolInputError("Deleted Messages folder not found; nothing was changed")
            if trash == mid.folder:
                raise ToolInputError("already in Deleted Messages")
            if _protected(trash, protected):
                raise ToolInputError(PROTECTED)
            return trash

        return self._move(mid, resolve)

    def draft(
        self,
        *,
        to: Sequence[str] = (),
        cc: Sequence[str] = (),
        bcc: Sequence[str] = (),
        subject: str | None = None,
        body: str = "",
        in_reply_to: str | None = None,
    ) -> dict:
        """Save one plain-text draft in Drafts (even if hidden from reads). Never sends.

        Protected folders do not apply: they fence moves, and the `draft` grant is the
        control. A reply's original is read like mail_get (EXAMINE, BODY.PEEK), unchanged.
        """
        if self.writer is None:
            raise ToolInputError("mail writes are not enabled")
        check_draft(to=to, cc=cc, bcc=bcc, subject=subject, body=body, in_reply_to=in_reply_to)
        reply = None
        if in_reply_to:
            p, _ = self._fetch_one(self._resolve(in_reply_to))
            reply = Original(p.message_id, p.in_reply_to, p.references, p.subject)
        message = build_draft(
            to=to,
            cc=cc,
            bcc=bcc,
            subject=subject,
            body=body,
            send_from=self.account.mail.write.send_from,
            reply=reply,
        )
        saved = self.writer.append_draft(message)
        if self.sync is not None and self.account.mail.folders.allows(saved.folder):
            self.sync.request_resync(saved.folder)
        return {
            "saved": True,
            "folder": saved.folder,
            "id": saved.new_id.encode() if saved.new_id else None,
            "in_reply_to": in_reply_to or None,
        }

    def send(
        self,
        *,
        to: Sequence[str] = (),
        cc: Sequence[str] = (),
        bcc: Sequence[str] = (),
        subject: str | None = None,
        body: str = "",
        in_reply_to: str | None = None,
        send_to: Sequence[str] | None = None,
    ) -> dict:
        """Send one plain-text message over SMTP from mail.write.send_from, then make sure
        Sent Messages holds exactly one copy. `send_to` is the calling token's allowlist.

        Every refusal (limits, send_to, a bad reply id, no Sent Messages) happens before the
        SMTP connection. Protected folders do not apply: the `send` grant is the control.
        """
        if self.writer is None or self.sender is None:
            raise ToolInputError("mail writes are not enabled")
        send_from = self.account.mail.write.send_from
        if send_from is None:  # config.py refuses a send grant without it
            raise ToolInputError("mail.write.send_from is not set for this account")
        check_send(to=to, cc=cc, bcc=bcc, subject=subject, body=body)
        envelope = addr_specs([*to, *cc, *bcc])
        if send_to is not None:
            refused = not_allowed(envelope, send_to)
            if refused:  # the caller's own input; never logged (the audit line has counts)
                raise ToolInputError(f"{NOT_ALLOWED}: {', '.join(refused)}")
        reply = None
        if in_reply_to:
            p, _ = self._fetch_one(self._resolve(in_reply_to))
            reply = Original(p.message_id, p.in_reply_to, p.references, p.subject)
        from .imap_write import find_sent

        with self.pool.connection() as client:
            sent_folder = find_sent(client.folder_attributes())  # even if hidden from reads
        if sent_folder is None:
            raise ToolInputError("Sent Messages folder not found; nothing was sent")
        out = build_outgoing(
            to=to, cc=cc, bcc=bcc, subject=subject, body=body, send_from=send_from, reply=reply
        )
        result = self.sender.send(send_from, envelope, out.sent)
        # The mail has gone: from here on nothing fails the call.
        saved, copy_id = self._sent_copy(sent_folder, out)
        if self.sync is not None and self.account.mail.folders.allows(sent_folder):
            self.sync.request_resync(sent_folder)
        return {
            "sent": True,
            "message_id": out.message_id,
            "recipients": len(envelope),
            "refused_recipients": list(result.refused),
            "sent_copy_saved": saved,
            "sent_copy_id": copy_id.encode() if copy_id else None,
            "in_reply_to": in_reply_to or None,
        }

    def _sent_copy(self, folder: str, out: Outgoing) -> tuple[bool, MailId | None]:
        """Exactly one copy in Sent Messages: the one iCloud filed, if the read path finds it
        by Message-ID, otherwise one appended now. Returns whether a copy is there, and its
        id when known. Logs name only the account and the error class."""
        assert self.writer is not None
        if out.message_id.isascii():  # UID SEARCH criteria must be ASCII
            try:
                with self.pool.connection() as client:
                    st = client.examine(folder)
                    uids = client.uid_search("HEADER", "Message-ID", f'"{out.message_id}"')
                if uids:
                    return True, MailId(folder, st.uidvalidity, max(uids))
            except (ImapError, *ImapErrors) as exc:  # the check failed: append anyway
                log.warning(
                    "%s: looking for the sent copy failed (%s)", self.account.id, type(exc).__name__
                )
        try:
            return True, self.writer.append_sent(out.copy).new_id
        except (WriteFailed, ImapError, *ImapErrors) as exc:
            log.warning(
                "%s: the mail was sent, but saving its copy failed (%s)",
                self.account.id,
                type(exc).__name__,
            )
            return False, None

    def _write_source(
        self, mail_id: str, token_protected: Sequence[str]
    ) -> tuple[MailId, tuple[str, ...]]:
        if self.writer is None:
            raise ToolInputError("mail writes are not enabled")
        mid = self._resolve(mail_id)
        protected = self.account.mail.write.protected_folders + tuple(token_protected)
        if _protected(mid.folder, protected):
            raise ToolInputError(PROTECTED)
        return mid, protected

    def _move(self, mid: MailId, resolve: Callable[[Mapping[str, frozenset[str]]], str]) -> dict:
        assert self.writer is not None
        moved = self.writer.guarded_move(mid, resolve)
        # Drop the old id now so searches stop returning it; sync indexes the new one.
        self.index.remove(mid.folder, [mid.uid])
        if self.sync is not None:
            self.sync.request_resync(moved.from_folder, moved.to_folder)
        return {
            "moved": True,
            "from_folder": moved.from_folder,
            "to_folder": moved.to_folder,
            "new_id": moved.new_id.encode(),
        }
