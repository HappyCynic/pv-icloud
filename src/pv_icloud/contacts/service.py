# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

"""Contacts tool implementations for one account. Blocking; the server runs them in threads.

The address book is held in memory only (never written to disk) and kept current with
`sync-collection`: the first call loads every card, later calls at most every
`REFRESH_SECONDS` fetch only what changed.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..config import AccountConfig
from ..dav import DavError, home_may_have_moved
from ..errors import ToolInputError
from ..text import fold
from .carddav import CardDavClient, SyncTokenInvalid
from .vcard import Card, parse_vcard

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
    ):
        self.account = account
        self.client = client
        self._clock = clock
        self._wall = wall
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
        me: str | None = None
        for book in self._books.values():
            for href, card in book.cards.items():
                cid = card_id(href)
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


def _freshness(snap: Snapshot) -> dict[str, Any]:
    out: dict[str, Any] = {"contacts_as_of": _iso(snap.as_of)}
    if snap.stale:
        out["note"] = STALE_NOTE
    return out


def _iso(epoch: float) -> str | None:
    return dt.datetime.fromtimestamp(epoch, dt.UTC).isoformat() if epoch else None
