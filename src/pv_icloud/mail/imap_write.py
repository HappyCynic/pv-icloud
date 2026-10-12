# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

"""The mail write path: the only code allowed to change mail over IMAP (ADR-0004).

Imported only when PV_ICLOUD_MAIL_WRITE=enabled. Move, archive and delete all run the same
guarded move, for exactly one message, on a fresh connection that is logged out afterwards:

    SELECT <source>                              (UIDVALIDITY must match the id)
    UID FETCH <uid> (UID FLAGS)                  (the message must still be there)
    UID COPY <uid> <destination>                 (COPYUID must name exactly this UID)
    UID STORE <uid> +FLAGS.SILENT (\\Deleted)
    UID EXPUNGE <uid>                            (removes only this UID)

A message is removed from its source only after its copy in the destination is confirmed.
iCloud does not advertise MOVE, and UID EXPUNGE and COPYUID both come from UIDPLUS, so
without UIDPLUS nothing is attempted.

mail_draft creates one new message, also on a fresh connection, with no SELECT:

    APPEND "Drafts" (\\Draft \\Seen) {literal}   (APPENDUID gives the new id)

Drafts is found by name (iCloud gives it no special-use attribute). Without UIDPLUS, which
provides APPENDUID, nothing is attempted.

Nothing else that changes mail is ever sent: no plain STORE, COPY or EXPUNGE, no CLOSE (it
expunges every \\Deleted message in the folder), no MOVE, no UID sets or ranges, no other
flags, no APPEND with other flags. tests/test_repo_policy.py enforces this.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from ..errors import ToolInputError, WriteFailed
from .ids import MailId
from .imap import ImapError, ImapErrors, ImapPool, quote

log = logging.getLogger(__name__)

TRASH_NAME = "Deleted Messages"
NO_UIDPLUS = "this iCloud server cannot move messages safely (no UIDPLUS); nothing was changed"
NOT_FOUND = "message not found (deleted or moved); search again"
STALE = "stale id (folder UIDVALIDITY changed); search again"
COPY_FAILED = "copying the message failed; it was left unchanged in its folder"
NOT_CONFIRMED = (
    "the copy could not be confirmed; the message was left in its source folder, and a copy "
    "may also be in the destination folder"
)
STORE_FAILED = (
    "the message was copied, but removing it from its source failed: it is in the "
    "destination folder and still in its source folder (now in both folders)"
)
STORE_LOST = (
    "the message was copied, but the connection failed while removing it from its source: it "
    "is in the destination folder and may still be in its source folder"
)
EXPUNGE_FAILED = (
    "the message was copied, but removing it from its source failed: it is in the "
    "destination folder and may still be in its source folder, marked deleted"
)
DRAFTS_NAME = "Drafts"
DRAFT_NO_UIDPLUS = (
    "this iCloud server cannot report the new draft's id (no UIDPLUS); nothing was saved"
)
DRAFTS_NOT_FOUND = "Drafts folder not found; nothing was saved"
APPEND_FAILED = "saving the draft failed; nothing was saved"
APPEND_LOST = "the connection failed while saving the draft; it may or may not have been saved"

# Folder name -> its LIST attributes, lower-cased; given to the destination resolver.
Folders = Mapping[str, frozenset[str]]
Resolve = Callable[[Folders], str]


@dataclass(frozen=True)
class Moved:
    from_folder: str
    to_folder: str
    new_id: MailId  # from COPYUID, so always known


@dataclass(frozen=True)
class Appended:
    folder: str
    new_id: MailId | None  # None when the server saved the draft without APPENDUID


def find_trash(folders: Folders) -> str | None:
    """Deleted Messages: the folder marked \\Trash, otherwise the one with iCloud's name."""
    for name, attrs in folders.items():
        if "\\trash" in attrs:
            return name
    return TRASH_NAME if TRASH_NAME in folders else None


def find_archive(folders: Folders, name: str) -> str | None:
    """The archive folder, by name only: iCloud gives it no special-use attribute."""
    return name if name in folders else None


def _int_code(values: list | None) -> int | None:
    for v in values or []:
        if v:
            head = v.split()[0]
            if head.isdigit():
                return int(head)
    return None


def _copied_uid(codes: list | None, uid: int, uidvalidity: int) -> int | None:
    """The destination UID if a COPYUID names exactly `uid` copied into `uidvalidity`."""
    for code in codes or []:
        parts = code.split() if isinstance(code, bytes) else []
        if (
            len(parts) == 3
            and parts[0] == str(uidvalidity).encode()
            and parts[1] == str(uid).encode()
            and parts[2].isdigit()
        ):
            return int(parts[2])
    return None


def _appended_uid(codes: list | None) -> tuple[int, int] | None:
    """(uidvalidity, uid) if an APPENDUID names exactly one UID."""
    for code in codes or []:
        parts = code.split() if isinstance(code, bytes) else []
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            return int(parts[0]), int(parts[1])
    return None


class MailWriter:
    """Guarded single-message moves and draft saving for one account."""

    def __init__(self, pool: ImapPool):
        self.pool = pool

    def guarded_move(self, mid: MailId, destination: Resolve) -> Moved:
        """Move one message. `destination` picks the folder from the account's folders (and
        may refuse with ToolInputError); it runs before anything that can change mail."""
        with self.pool.write_connection() as client:
            if "UIDPLUS" not in client.capabilities:
                raise WriteFailed(NO_UIDPLUS)
            folders = client.folder_attributes()
            if mid.folder not in folders:
                raise ToolInputError(NOT_FOUND)
            dest = destination(folders)
            dest_uidvalidity = client.status(dest).uidvalidity
            conn = client.conn
            assert conn is not None

            conn.response("UIDVALIDITY")  # drop any stale response code before SELECT
            typ, data = conn.select(quote(mid.folder))
            if typ != "OK":
                raise ImapError(f"SELECT failed: {typ}")
            if _int_code(conn.response("UIDVALIDITY")[1]) != mid.uidvalidity:
                raise ToolInputError(STALE)
            typ, data = conn.uid("FETCH", str(mid.uid), "(UID FLAGS)")
            if typ != "OK":
                raise ImapError(f"UID FETCH failed: {typ}")
            if not any(
                isinstance(item, bytes) and re.search(rb"\bUID %d\b" % mid.uid, item)
                for item in data
            ):
                raise ToolInputError(NOT_FOUND)

            conn.response("COPYUID")  # drop any stale response code before COPY
            try:
                typ, data = conn.uid("COPY", str(mid.uid), quote(dest))
                codes = conn.response("COPYUID")[1]
            except ImapErrors as exc:
                raise WriteFailed(NOT_CONFIRMED) from exc
            if typ != "OK":
                raise WriteFailed(COPY_FAILED)
            new_uid = _copied_uid(codes, mid.uid, dest_uidvalidity)
            if new_uid is None:
                raise WriteFailed(NOT_CONFIRMED)

            # The copy is confirmed: only now is the original removed.
            try:
                typ, data = conn.uid("STORE", str(mid.uid), "+FLAGS.SILENT", "(\\Deleted)")
            except ImapErrors as exc:
                raise WriteFailed(STORE_LOST) from exc
            if typ != "OK":
                raise WriteFailed(STORE_FAILED)
            try:
                typ, data = conn.uid("EXPUNGE", str(mid.uid))
            except ImapErrors as exc:
                raise WriteFailed(EXPUNGE_FAILED) from exc
            if typ != "OK":
                raise WriteFailed(EXPUNGE_FAILED)
        return Moved(mid.folder, dest, MailId(dest, dest_uidvalidity, new_uid))

    def append_draft(self, message: bytes) -> Appended:
        """Save one new message in Drafts, flagged \\Draft and \\Seen. Changes nothing else."""
        with self.pool.write_connection() as client:
            if "UIDPLUS" not in client.capabilities:
                raise WriteFailed(DRAFT_NO_UIDPLUS)
            if DRAFTS_NAME not in client.folder_attributes():  # by name, even if hidden
                raise ToolInputError(DRAFTS_NOT_FOUND)
            conn = client.conn
            assert conn is not None

            conn.response("APPENDUID")  # drop any stale response code before APPEND
            try:
                typ, data = conn.append(quote(DRAFTS_NAME), "(\\Draft \\Seen)", None, message)
                codes = conn.response("APPENDUID")[1]
            except ImapErrors as exc:
                raise WriteFailed(APPEND_LOST) from exc
            if typ != "OK":
                raise WriteFailed(APPEND_FAILED)
        # Saved. A missing APPENDUID is not a failure: retrying would make a duplicate.
        appended = _appended_uid(codes)
        if appended is None:
            log.warning("draft saved without APPENDUID; its id is unknown")
            return Appended(DRAFTS_NAME, None)
        return Appended(DRAFTS_NAME, MailId(DRAFTS_NAME, *appended))
