# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

"""Contacts tool implementations for one account. Blocking; the server runs them in threads.

The address book is held in memory only (never written to disk) and kept current with
`sync-collection`: the first call loads every card, later calls at most every
`REFRESH_SECONDS` fetch only what changed, and a contact write refreshes it at once.

Contact writes (ADR-0006) exist only when PV_ICLOUD_CONTACTS_WRITE=enabled: the server then
passes a `CardDavWriter`. Every update and delete re-reads the cards it changes with their
ETags, checks everything against that fresh read before the first write, and edits the
stored text in place (`compose`); nothing is retried.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import re
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..config import AccountConfig
from ..dav import DavError, DavThrottled, home_may_have_moved
from ..errors import ToolInputError, WriteFailed
from ..text import fold
from .carddav import CardDavClient, SyncTokenInvalid
from .vcard import Card, parse_vcard

if TYPE_CHECKING:  # the write modules are imported only when contact writes are on
    from .carddav_write import CardDavWriter
    from .compose import Changes

log = logging.getLogger(__name__)

SEARCH_LIMIT_DEFAULT = 25
SEARCH_LIMIT_MAX = 100
NOTE_MAX_CHARS = 20_000
REFRESH_SECONDS = 60
OFF_MESSAGE = "contacts are turned off for this account (contacts.enabled: false)"
STALE_NOTE = (
    "iCloud Contacts is not answering right now; these results come from the copy loaded at "
    "contacts_as_of."
)
NOT_FOUND = "contact not found (deleted, hidden, or a stale id)"
ME_CARD = "this is the account owner's own card; it cannot be deleted"
_PHONE_TERM = re.compile(r"^\+?[\d\s().\-/]+$")
_NON_DIGIT = re.compile(r"\D")


@dataclass
class _Book:
    token: str | None = None
    cards: dict[str, Card] = field(default_factory=dict)  # href -> card


@dataclass(frozen=True)
class Snapshot:
    """What callers see: visible contacts and groups, rebuilt after every change."""

    contacts: dict[str, Card]  # id -> card (individuals; excluded groups' members removed)
    groups: dict[str, tuple[str, list[str]]]  # group name -> (group id, member ids)
    groups_of: dict[str, list[str]]  # contact id -> group names
    by_email: dict[str, str]  # casefolded address -> contact id
    me: str | None  # id of the owner's own card
    as_of: float  # wall-clock time of the last successful check with iCloud
    stale: bool = False  # the last refresh failed; this is the previous copy
    # id -> (address book URL, href) of each visible contact and group: the only way a write
    # finds a card (an id is never used to build a URL).
    hrefs: dict[str, tuple[str, str]] = field(default_factory=dict)

    @staticmethod
    def empty() -> Snapshot:
        return Snapshot({}, {}, {}, {}, None, 0.0)


def card_id(href: str) -> str:
    """The card's file name without `.vcf`: stable, opaque, and never used to build URLs."""
    name = href.rstrip("/").rsplit("/", 1)[-1]
    return name.removesuffix(".vcf")


def matches(card: Card, terms: list[str]) -> bool:
    if not terms:
        return True
    text = card.search_text()
    digits = card.phone_digits()
    for term in terms:
        if term in text:
            continue
        if _PHONE_TERM.match(term):
            want = _NON_DIGIT.sub("", term)
            if len(want) >= 3 and any(want in d for d in digits):
                continue
        return False
    return True


def query_terms(query: str | None) -> list[str]:
    return [fold(t) for t in (query or "").replace('"', " ").split() if t]


class ContactsService:
    def __init__(
        self,
        account: AccountConfig,
        client: CardDavClient,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        writer: CardDavWriter | None = None,
        now: Callable[[], dt.datetime] | None = None,
    ):
        self.account = account
        self.client = client
        self.writer = writer  # only when PV_ICLOUD_CONTACTS_WRITE=enabled (ADR-0006)
        self._clock = clock
        self._wall = wall
        self._now = now or (lambda: dt.datetime.now(dt.UTC).replace(microsecond=0))
        self._lock = threading.Lock()  # one refresh at a time
        self._books: dict[str, _Book] = {}
        self._me_href: str | None = None
        self._checked: float | None = None
        self._snapshot = Snapshot.empty()

    @property
    def enabled(self) -> bool:
        return self.account.contacts.enabled

    # --- cache -------------------------------------------------------------------------

    def snapshot(self) -> Snapshot:
        if not self.enabled:
            raise ToolInputError(OFF_MESSAGE)
        with self._lock:
            if self._checked is None or self._clock() - self._checked >= REFRESH_SECONDS:
                try:
                    self._refresh()
                except DavError as exc:
                    if home_may_have_moved(exc):  # the account may have moved to another host
                        self.client.forget_home()
                    if not self._snapshot.as_of:
                        raise  # nothing loaded yet
                    # Keep answering from the copy we have; try iCloud again next interval.
                    log.warning("%s: contacts refresh failed: %s", self.account.id, exc)
                    self._snapshot = dataclasses.replace(self._snapshot, stale=True)
                self._checked = self._clock()
            return self._snapshot

    def warm(self) -> None:
        """Load the address book ahead of the first call (startup thread)."""
        if not self.enabled:
            return
        try:
            self.snapshot()
        except Exception as exc:
            log.warning("%s: contacts preload failed: %s", self.account.id, exc)

    def _refresh(self) -> None:
        info = self.client.home_info()
        changed = info.me_card != self._me_href or set(info.books) != set(self._books)
        self._me_href = info.me_card
        self._books = {url: self._books.get(url) or _Book() for url in info.books}
        for url, book in self._books.items():
            changed |= self._sync_book(url, book)
        if changed or self._snapshot.as_of == 0.0:
            self._snapshot = self._build()
        else:
            self._snapshot = dataclasses.replace(self._snapshot, as_of=self._wall(), stale=False)

    def _sync_book(self, url: str, book: _Book) -> bool:
        try:
            result = self.client.sync(url, book.token)
        except SyncTokenInvalid:
            book.token, book.cards = None, {}
            result = self.client.sync(url, None)
        full = book.token is None
        for href in result.removed:
            book.cards.pop(href, None)
        fetched = dict(self.client.multiget(url, result.changed)) if result.changed else {}
        if full:
            book.cards = {}
        for href in result.changed:
            text = fetched.get(href)
            if text is None:
                book.cards.pop(href, None)  # deleted between the two requests
                continue
            try:
                book.cards[href] = parse_vcard(text)
            except Exception:
                book.cards.pop(href, None)  # unparseable card; the rest still count
        book.token = result.token
        return full or bool(result.changed or result.removed)

    def _build(self) -> Snapshot:
        cfg = self.account.contacts
        everyone: dict[str, Card] = {}
        groups: dict[str, tuple[str, Card]] = {}
        hrefs: dict[str, tuple[str, str]] = {}
        me: str | None = None
        for url, book in self._books.items():
            for href, card in book.cards.items():
                cid = card_id(href)
                hrefs[cid] = (url, href)
                if card.kind == "group":
                    groups[cid] = (cid, card)
                else:
                    everyone[cid] = card
                    if href == self._me_href:
                        me = cid
        by_uid = {card.uid: cid for cid, card in everyone.items()}
        hidden: set[str] = set()
        for _, group in groups.values():
            if cfg.hides_group(group.name):
                hidden.update(by_uid[u] for u in group.members if u in by_uid)
        contacts = {cid: c for cid, c in everyone.items() if cid not in hidden}

        visible_groups: dict[str, tuple[str, list[str]]] = {}
        groups_of: dict[str, list[str]] = {}
        for gid, group in sorted(groups.values(), key=lambda g: g[1].name.casefold()):
            if cfg.hides_group(group.name):
                continue
            members = [by_uid[u] for u in group.members if by_uid.get(u) in contacts]
            visible_groups[group.name] = (gid, members)
            for cid in members:
                groups_of.setdefault(cid, []).append(group.name)
        group_ids = {gid for gid, _ in visible_groups.values()}
        by_email: dict[str, str] = {}
        for cid, card in sorted(contacts.items(), key=lambda kv: kv[1].name.casefold()):
            for addr in card.email_addresses():
                by_email.setdefault(addr.casefold(), cid)
        return Snapshot(
            contacts=contacts,
            groups=visible_groups,
            groups_of=groups_of,
            by_email=by_email,
            me=me if me in contacts else None,
            as_of=self._wall(),
            hrefs={cid: loc for cid, loc in hrefs.items() if cid in contacts or cid in group_ids},
        )

    # --- output ------------------------------------------------------------------------

    @staticmethod
    def summary(cid: str, card: Card) -> dict[str, Any]:
        return {
            "id": cid,
            "name": card.name,
            "organization": card.organization,
            "title": card.title,
            "emails": [{"label": e["label"], "value": e["value"]} for e in card.emails],
            "phones": [{"label": p["label"], "value": p["value"]} for p in card.phones],
        }

    def search_cards(self, snap: Snapshot, query: str | None) -> list[tuple[str, Card]]:
        """Matching contacts, best first: more terms found in the name rank higher."""
        terms = query_terms(query)
        hits = [(cid, c) for cid, c in snap.contacts.items() if matches(c, terms)]

        def rank(item: tuple[str, Card]) -> tuple[int, str, str]:
            cid, card = item
            names = card.name_text()
            return (-sum(t in names for t in terms), card.name.casefold(), cid)

        hits.sort(key=rank)
        return hits

    # --- tools -------------------------------------------------------------------------

    def search(
        self,
        *,
        query: str | None = None,
        group: str | None = None,
        limit: int = SEARCH_LIMIT_DEFAULT,
        offset: int = 0,
    ) -> dict:
        if not 1 <= limit <= SEARCH_LIMIT_MAX:
            raise ToolInputError(f"limit must be between 1 and {SEARCH_LIMIT_MAX}")
        if offset < 0:
            raise ToolInputError("offset must be >= 0")
        snap = self.snapshot()
        hits = self.search_cards(snap, query)
        if group:
            entry = next(
                (v for k, v in snap.groups.items() if k.casefold() == group.casefold()), None
            )
            if entry is None:
                # Same message for an excluded group: don't confirm that it exists.
                raise ToolInputError(f"unknown group: {group}")
            members = set(entry[1])
            hits = [h for h in hits if h[0] in members]
        page = hits[offset : offset + limit]
        end = offset + len(page)
        return {
            "contacts": [self.summary(cid, c) for cid, c in page],
            "total": len(hits),
            "offset": offset,
            "next_offset": end if end < len(hits) else None,
            **_freshness(snap),
        }

    def get(self, contact_id: str) -> dict:
        snap = self.snapshot()
        card = snap.contacts.get(contact_id)
        if card is None:
            raise ToolInputError("contact not found (deleted, hidden, or a stale id)")
        note = card.note
        return {
            "id": contact_id,
            "name": card.name,
            "given_name": card.given,
            "family_name": card.family,
            "middle_name": card.middle,
            "prefix": card.prefix,
            "suffix": card.suffix,
            "nickname": card.nickname,
            "maiden_name": card.maiden_name,
            "organization": card.organization,
            "department": card.department,
            "title": card.title,
            "emails": card.emails,
            "phones": card.phones,
            "addresses": card.addresses,
            "urls": card.urls,
            "social_profiles": card.social_profiles,
            "instant_messaging": card.instant_messaging,
            "birthday": card.birthday,
            "dates": card.dates,
            "related_names": card.related_names,
            "note": note[:NOTE_MAX_CHARS] if note else None,
            "note_truncated": bool(note and len(note) > NOTE_MAX_CHARS),
            "groups": snap.groups_of.get(contact_id, []),
            "is_me": contact_id == snap.me,
            **_freshness(snap),
        }

    def groups(self) -> dict:
        snap = self.snapshot()
        return {
            "groups": [
                {"name": name, "members": len(members)}
                for name, (_, members) in snap.groups.items()
            ],
            "contacts": len(snap.contacts),
            **_freshness(snap),
        }

    # --- writes (ADR-0006) -------------------------------------------------------------------

    def _require_writer(self) -> CardDavWriter:
        if self.writer is None:  # unreachable: the tools exist only with the switch on
            raise RuntimeError("contact writes are not enabled")
        return self.writer

    @staticmethod
    def _locate(snap: Snapshot, contact_id: str) -> tuple[str, str]:
        """(address book, href) of a visible individual contact; the same error for every
        other id (unknown, stale, a group, hidden by an excluded group)."""
        loc = snap.hrefs.get(contact_id) if contact_id in snap.contacts else None
        if loc is None:
            raise ToolInputError(NOT_FOUND)
        return loc

    @staticmethod
    def _group_hrefs(snap: Snapshot, names: Sequence[str], book: str) -> dict[str, str]:
        """Visible group name -> href for each name given (case-insensitive). A missing and an
        excluded group get the same error, so nothing is learnt about excluded ones."""
        out: dict[str, str] = {}
        for name in names:
            match = next((k for k in snap.groups if k.casefold() == name.strip().casefold()), None)
            loc = snap.hrefs.get(snap.groups[match][0]) if match is not None else None
            if match is None or loc is None or loc[0] != book:
                raise ToolInputError(f"unknown group: {name}")
            out[match] = loc[1]
        return out

    def _fresh_groups(
        self, fresh: Mapping[str, tuple[str, str]], hrefs: Mapping[str, str]
    ) -> dict[str, tuple[str, str, str, Card]]:
        """Group name -> (href, stored text, ETag, card) as just read."""
        out: dict[str, tuple[str, str, str, Card]] = {}
        for name, href in hrefs.items():
            if href not in fresh:
                raise ToolInputError(f"unknown group: {name}")
            text, tag = fresh[href]
            try:
                card = parse_vcard(text)
            except Exception:
                raise ToolInputError(f"cannot change this contact safely: group {name}") from None
            if card.kind != "group" or self.account.contacts.hides_group(card.name):
                raise ToolInputError(f"unknown group: {name}")
            out[name] = (href, text, tag, card)
        return out

    def _hidden(self, uid: str, fresh: Mapping[str, Card]) -> bool:
        """Whether `uid` is in a group matched by contacts.exclude_groups, by the group cards
        in memory; a group card just read (`fresh`, by href) wins over its copy."""
        with self._lock:
            groups = {
                href: card
                for book in self._books.values()
                for href, card in book.cards.items()
                if card.kind == "group"
            }
        groups.update(fresh)
        cfg = self.account.contacts
        return any(cfg.hides_group(g.name) and uid in g.members for g in groups.values())

    def _read(
        self, book: str, href: str, group_hrefs: Mapping[str, str]
    ) -> tuple[str, str, Card, dict[str, tuple[str, str, str, Card]]]:
        """The contact card and the group cards a call changes, read again from iCloud in one
        request with their ETags (never ones from an earlier call), and checked again."""
        fresh = self.client.cards(book, [href, *group_hrefs.values()])
        if href not in fresh:
            raise ToolInputError(NOT_FOUND)
        text, tag = fresh[href]
        try:
            card = parse_vcard(text)
        except Exception:
            raise ToolInputError(NOT_FOUND) from None
        groups = self._fresh_groups(fresh, group_hrefs)
        if card.kind != "individual" or self._hidden(
            card.uid, {g[0]: g[3] for g in groups.values()}
        ):
            raise ToolInputError(NOT_FOUND)
        return text, tag, card, groups

    def _group_ops(
        self,
        uid: str,
        adds: Sequence[str],
        removes: Sequence[str],
        groups: Mapping[str, tuple[str, str, str, Card]],
    ) -> list[tuple[str, bool, str, str, str]]:
        """(name, adding, href, new text, ETag) for each group card that changes, in name
        order; adding a member or removing a non-member changes nothing."""
        from . import compose

        ops: list[tuple[str, bool, str, str, str]] = []
        now = self._now()
        for name in sorted({*adds, *removes}, key=str.casefold):
            href, text, tag, _ = groups[name]
            adding = name in adds
            try:
                new = (compose.add_member if adding else compose.remove_member)(text, uid, now)
            except compose.Unsafe:
                raise ToolInputError(f"cannot change this contact safely: group {name}") from None
            if new is not None:
                ops.append((name, adding, href, new, tag))
        return ops

    def _write_groups(
        self,
        book: str,
        ops: Sequence[tuple[str, bool, str, str, str]],
        contact_saved: bool,
        written: dict[str, str | None],
    ) -> None:
        """Write each group card in turn. If one fails, refresh with what was written and say
        exactly what was saved; nothing is rolled back or retried."""
        from .carddav_write import UNCONFIRMED, Changed

        writer = self._require_writer()
        for done, (name, _, href, text, tag) in enumerate(ops):
            try:
                writer.update(book, href, text, tag)
            except (ToolInputError, WriteFailed, DavError) as exc:
                if isinstance(exc, Changed):
                    reason = f"group {name} changed in Contacts"
                elif isinstance(exc, DavThrottled):
                    reason = f"iCloud is limiting requests, so group {name} was not changed"
                elif str(exc) == UNCONFIRMED:
                    reason = f"iCloud did not confirm the change to group {name}"
                else:
                    reason = f"group {name} could not be changed"
                kinds = {adding for _, adding, *_ in ops}
                if kinds == {True}:
                    progress = f"added to {done} of {len(ops)} groups"
                elif kinds == {False}:
                    progress = f"removed from {done} of {len(ops)} groups"
                else:
                    progress = f"{done} of {len(ops)} group changes were saved"
                if contact_saved:
                    progress = "the contact was saved and " + progress
                if written:
                    self._after_write(book, written)
                error = ToolInputError if isinstance(exc, Changed) else WriteFailed
                raise error(f"{progress}; {reason}; read the contact again") from exc
            written[href] = text

    def _after_write(self, book: str, written: Mapping[str, str | None]) -> Snapshot:
        """Refresh the address book at once, whatever the interval, so every read tool sees
        the change. If iCloud does not answer, apply what was written (text, or None for a
        delete) to the copy in memory and mark it stale: the write itself was confirmed."""
        with self._lock:
            try:
                self._refresh()
            except DavError as exc:
                if home_may_have_moved(exc):
                    self.client.forget_home()
                log.warning("%s: contacts refresh after a write failed: %s", self.account.id, exc)
                cards = self._books.setdefault(book, _Book()).cards
                for href, text in written.items():
                    cards.pop(href, None)
                    if text is not None:  # text this call wrote, after reading it back
                        cards[href] = parse_vcard(text)
                as_of = self._snapshot.as_of
                self._snapshot = dataclasses.replace(self._build(), as_of=as_of, stale=True)
            self._checked = self._clock()
            return self._snapshot

    @staticmethod
    def _written(snap: Snapshot, cid: str, card: Card) -> dict[str, Any]:
        shown = snap.contacts.get(cid, card)
        return {
            "id": cid,
            "name": shown.name,
            "groups": snap.groups_of.get(cid, []),
            **_freshness(snap),
        }

    def create(self, fields: Mapping[str, Any], groups: Sequence[str] = ()) -> dict:
        """One new contact, optionally added to visible groups (contacts_create)."""
        from . import compose

        writer = self._require_writer()
        compose.check_new(fields)
        compose.check_groups(groups)
        snap = self.snapshot()
        books = sorted({loc[0] for loc in snap.hrefs.values()} or set(self._books))
        if not books:
            raise ToolInputError("this account has no address book")
        book = books[0]  # iCloud has one per account
        group_hrefs = self._group_hrefs(snap, groups, book)
        fresh = self.client.cards(book, list(group_hrefs.values())) if group_hrefs else {}
        fresh_groups = self._fresh_groups(fresh, group_hrefs)
        uid = str(uuid.uuid4()).upper()
        text = compose.new_card(uid, fields, self._now())
        ops = self._group_ops(uid, list(group_hrefs), [], fresh_groups)
        href = writer.create(book, f"{uid}.vcf", text)
        written: dict[str, str | None] = {href: text}
        self._write_groups(book, ops, True, written)
        snap = self._after_write(book, written)
        return self._written(snap, card_id(href), parse_vcard(text))

    def update(
        self,
        contact_id: str,
        changes: Changes,
        add_groups: Sequence[str] = (),
        remove_groups: Sequence[str] = (),
    ) -> dict:
        """Change one contact in place, and its membership of visible groups
        (contacts_update)."""
        from . import compose

        writer = self._require_writer()
        compose.check_changes(changes, groups=bool(add_groups or remove_groups))
        compose.check_groups([*add_groups, *remove_groups])
        snap = self.snapshot()
        book, href = self._locate(snap, contact_id)
        adds = self._group_hrefs(snap, add_groups, book)
        removes = self._group_hrefs(snap, remove_groups, book)
        both = sorted(set(adds) & set(removes))
        if both:
            raise ToolInputError(f"add_groups and remove_groups both name {both[0]}")
        text, tag, card, groups = self._read(book, href, {**adds, **removes})
        new_text = (
            compose.apply_update(text, changes, self._now()) if changes.touches_card() else None
        )
        ops = self._group_ops(card.uid, list(adds), list(removes), groups)
        if new_text is None and not ops:
            raise ToolInputError("nothing to change")
        written: dict[str, str | None] = {}
        if new_text is not None:
            writer.update(book, href, new_text, tag)
            written[href] = new_text
        self._write_groups(book, ops, new_text is not None, written)
        snap = self._after_write(book, written)
        return self._written(snap, contact_id, parse_vcard(new_text or text))

    def delete(self, contact_id: str) -> dict:
        """Delete one contact, never the owner's own card (contacts_delete). Group cards are
        not edited: their member entry for it is left, as Apple's own clients leave them."""
        writer = self._require_writer()
        snap = self.snapshot()
        book, href = self._locate(snap, contact_id)
        _, tag, _, _ = self._read(book, href, {})
        if contact_id == snap.me or href == self._me_href:
            raise ToolInputError(ME_CARD)
        writer.delete(book, href, tag)
        self._after_write(book, {href: None})
        return {"deleted": True, "id": contact_id}


def _freshness(snap: Snapshot) -> dict[str, Any]:
    out: dict[str, Any] = {"contacts_as_of": _iso(snap.as_of)}
    if snap.stale:
        out["note"] = STALE_NOTE
    return out


def _iso(epoch: float) -> str | None:
    return dt.datetime.fromtimestamp(epoch, dt.UTC).isoformat() if epoch else None
