# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

"""find_person: one answer to "who is X" from contacts and the mail index together.

A person is a contact card (with every address on it), or, for someone who is not in the
contacts, one email address seen in mail. Mail statistics come from the local index only, so
the call is fast and covers the indexed window (`mail.max_age_days`).
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from .contacts.service import STALE_NOTE, ContactsService, Snapshot, matches, query_terms
from .dav import DavError
from .errors import ToolInputError
from .mail.ids import MailId
from .mail.index import IndexedMessage
from .mail.parse import split_addresses
from .mail.service import MailService
from .text import fold

log = logging.getLogger(__name__)

FIND_LIMIT_DEFAULT = 10
FIND_LIMIT_MAX = 25
MAIL_SCAN_MAX = 2000  # newest matching messages examined per call
RECENT_MAX = 3
NAMES_SEEN_MAX = 3


@dataclass
class _MailStats:
    messages: dict[tuple[str, int], IndexedMessage] = field(default_factory=dict)
    from_them: set[tuple[str, int]] = field(default_factory=set)
    to_them: set[tuple[str, int]] = field(default_factory=set)
    names: Counter[str] = field(default_factory=Counter)
    address: str = ""  # as first written in mail (keys are casefolded)

    def add(self, msg: IndexedMessage, name: str, address: str, sender: bool) -> None:
        self.address = self.address or address
        key = (msg.folder, msg.uid)
        self.messages[key] = msg
        (self.from_them if sender else self.to_them).add(key)
        if name:
            self.names[name] += 1

    def merge(self, other: _MailStats) -> None:
        self.messages.update(other.messages)
        self.from_them |= other.from_them
        self.to_them |= other.to_them
        self.names.update(other.names)

    def as_dict(self) -> dict[str, Any] | None:
        if not self.messages:
            return None
        msgs = sorted(self.messages.values(), key=lambda m: m.date or 0, reverse=True)
        dates = [m.date for m in msgs if m.date is not None]
        return {
            "messages": len(msgs),
            "from_them": len(self.from_them),
            "to_them": len(self.to_them),
            "first": _iso(min(dates)) if dates else None,
            "last": _iso(max(dates)) if dates else None,
            "recent": [
                {
                    "id": MailId(m.folder, m.uidvalidity, m.uid).encode(),
                    "date": _iso(m.date),
                    "folder": m.folder,
                    "from": m.from_addr,
                    "subject": m.subject,
                }
                for m in msgs[:RECENT_MAX]
            ],
        }


def _iso(epoch: float | None) -> str | None:
    return dt.datetime.fromtimestamp(epoch, dt.UTC).isoformat() if epoch is not None else None


class PeopleService:
    def __init__(self, mail: MailService, contacts: ContactsService):
        self.mail = mail
        self.contacts = contacts

    def _mail_stats(
        self, terms: list[str], addresses: set[str]
    ) -> tuple[dict[str, _MailStats], bool, dt.datetime]:
        """Per casefolded address: messages it appears in, if it matches every term or is
        one of `addresses`."""
        since = self.mail.window_start()
        found = self.mail.index.correspondence(
            folders=self.mail.allowed_folders(),
            terms=terms,
            addresses=sorted(addresses),
            since=since.timestamp(),
            limit=MAIL_SCAN_MAX,
        )
        stats: dict[str, _MailStats] = {}
        for msg in found:
            roles = ((msg.from_addr, True), (msg.to_addr, False), (msg.cc_addr, False))
            for stored, sender in roles:
                for name, addr in split_addresses(stored):
                    key = addr.casefold()
                    hit = key in addresses or (
                        bool(terms) and all(t in fold(f"{name} {addr}") for t in terms)
                    )
                    if hit:
                        stats.setdefault(key, _MailStats()).add(msg, name, addr, sender)
        return stats, len(found) >= MAIL_SCAN_MAX, since

    def find(self, query: str, limit: int = FIND_LIMIT_DEFAULT, offset: int = 0) -> dict:
        terms = query_terms(query)
        if not terms:
            raise ToolInputError("query is required")
        if not 1 <= limit <= FIND_LIMIT_MAX:
            raise ToolInputError(f"limit must be between 1 and {FIND_LIMIT_MAX}")
        if offset < 0:
            raise ToolInputError("offset must be >= 0")

        notes: list[str] = []
        snap = Snapshot.empty()
        if self.contacts.enabled:
            try:
                snap = self.contacts.snapshot()
            except DavError as exc:
                log.warning("%s find_person: contacts unavailable: %s", self.mail.account.id, exc)
                notes.append("iCloud Contacts is unavailable right now; showing mail only.")
        else:
            notes.append("Contacts are turned off for this account; showing mail only.")
        if snap.stale:
            notes.append(STALE_NOTE)

        matched = [cid for cid, card in snap.contacts.items() if matches(card, terms)]
        wanted = {a.casefold() for cid in matched for a in snap.contacts[cid].email_addresses()}
        stats, truncated, since = self._mail_stats(terms, wanted)

        own = {self.mail.account.icloud_user.casefold()}
        if snap.me:
            own |= {a.casefold() for a in snap.contacts[snap.me].email_addresses()}

        people: dict[str, dict[str, Any]] = {}  # "c:<id>" or "m:<address>" -> person
        person_stats: dict[str, _MailStats] = {}
        for cid in matched:
            people[f"c:{cid}"] = self._contact_person(cid, snap)
            person_stats[f"c:{cid}"] = _MailStats()
        for addr, st in stats.items():
            owner = snap.by_email.get(addr)
            if owner is not None:  # an address on a card: the card is the person
                key = f"c:{owner}"
                if key not in people:
                    people[key] = self._contact_person(owner, snap)
                    person_stats[key] = _MailStats()
            else:
                key = f"m:{addr}"
                name = st.names.most_common(1)[0][0] if st.names else addr
                people[key] = {
                    "name": name,
                    "contact_id": None,
                    "emails": [st.address],
                    "names_seen": [n for n, _ in st.names.most_common(NAMES_SEEN_MAX)],
                    "is_me": addr in own,
                }
                person_stats[key] = _MailStats()
            person_stats[key].merge(st)

        rows = []
        for key, person in people.items():
            person["mail"] = person_stats[key].as_dict()
            rows.append(person)
        rows.sort(
            key=lambda p: (
                p["is_me"],
                -(p["mail"]["messages"] if p["mail"] else 0),
                p["name"].casefold(),
            )
        )
        page = rows[offset : offset + limit]
        end = offset + len(page)
        out: dict[str, Any] = {
            "people": page,
            "total": len(rows),
            "offset": offset,
            "next_offset": end if end < len(rows) else None,
            "mail_searched_since": since.date().isoformat(),
        }
        if truncated:
            out["mail_scan_truncated"] = True
        if notes:
            out["notes"] = notes
        return out

    @staticmethod
    def _contact_person(cid: str, snap: Snapshot) -> dict[str, Any]:
        card = snap.contacts[cid]
        return {
            "name": card.name,
            "contact_id": cid,
            "organization": card.organization,
            "title": card.title,
            "emails": card.email_addresses(),
            "phones": [{"label": p["label"], "value": p["value"]} for p in card.phones],
            "groups": snap.groups_of.get(cid, []),
            "is_me": cid == snap.me,
        }
