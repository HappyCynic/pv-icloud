# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

"""Minimal read-only CardDAV client for iCloud.

Probed 2026-10-09: discovery hops from contacts.icloud.com to a per-account pNN-contacts
host; the home (`.../carddavhome/`) holds one address book (`card/`) and names the owner's
own card in `cs:me-card`. iCloud supports `sync-collection` (an unchanged token costs one
small round trip; an unknown token gets HTTP 412) and `addressbook-multiget`, so the
service keeps an in-memory copy and asks only for what changed. Groups are vCards in the
same book (`X-ADDRESSBOOKSERVER-KIND:group`).
"""

from __future__ import annotations

import threading
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from xml.sax.saxutils import escape

import httpx

from ..dav import NS, DavClient, DavError, DavStatusError, Throttle, text

ROOT_URL = "https://contacts.icloud.com/"
MULTIGET_BATCH = 100

_HOME_BODY = (
    '<d:propfind xmlns:d="DAV:" xmlns:cs="http://calendarserver.org/ns/">'
    "<d:prop><d:resourcetype/><cs:me-card/></d:prop></d:propfind>"
)


class SyncTokenInvalid(DavError):
    """The server no longer accepts the sync token; reload the address book."""


@dataclass(frozen=True)
class SyncResult:
    token: str | None
    changed: list[str]  # hrefs (percent-encoded paths) added or modified
    removed: list[str]


@dataclass(frozen=True)
class HomeInfo:
    books: list[str]  # address book URLs
    me_card: str | None  # path of the owner's own card, if set


def _path(base: httpx.URL, href: str) -> str:
    # Keep the path percent-encoded as served; it is sent back in multiget.
    return base.join(href).raw_path.decode("ascii").split("?", 1)[0]


class CardDavClient(DavClient):
    """One account's CardDAV connection. Thread-safe; httpx pools the HTTPS connections."""

    def __init__(
        self,
        user: str,
        password: str,
        *,
        transport: httpx.BaseTransport | None = None,
        root_url: str = ROOT_URL,
        throttle: Throttle | None = None,
    ):
        super().__init__(user, password, root_url=root_url, transport=transport, throttle=throttle)
        self._home: httpx.URL | None = None
        self._lock = threading.Lock()

    def home(self) -> httpx.URL:
        """Address book home URL (on a per-account pNN-contacts host), discovered once."""
        with self._lock:
            if self._home is None:
                self._home = self._discover("card:addressbook-home-set")
            return self._home

    def forget_home(self) -> None:
        with self._lock:
            self._home = None

    def home_info(self) -> HomeInfo:
        home = self.home()
        root, base = self._request("PROPFIND", home, _HOME_BODY, "1")
        books: list[str] = []
        me_card: str | None = None
        for resp in root.findall("d:response", NS):
            href = text(resp.find("d:href", NS))
            if href is None:
                continue
            url = base.join(href)
            me = text(resp.find(".//cs:me-card/d:href", NS))
            if me and url.path.rstrip("/") == home.path.rstrip("/"):
                me_card = _path(base, me)
            if resp.find(".//d:resourcetype/card:addressbook", NS) is not None:
                books.append(str(url))
        return HomeInfo(books=books, me_card=me_card)

    def sync(self, book: str, token: str | None) -> SyncResult:
        """Changes since `token` (everything when None). Raises SyncTokenInvalid."""
        body = (
            '<d:sync-collection xmlns:d="DAV:">'
            f"<d:sync-token>{escape(token or '')}</d:sync-token><d:sync-level>1</d:sync-level>"
            "<d:prop><d:getetag/></d:prop></d:sync-collection>"
        )
        url = httpx.URL(book)
        try:
            root, base = self._request("REPORT", url, body, "1")
        except DavStatusError as exc:
            if token and exc.status in (403, 409, 412):  # RFC 6578 valid-sync-token
                raise SyncTokenInvalid(str(exc)) from exc
            raise
        book_path = url.raw_path.decode("ascii").rstrip("/")
        changed: list[str] = []
        removed: list[str] = []
        for resp in root.findall("d:response", NS):
            href = text(resp.find("d:href", NS))
            if href is None:
                continue
            path = _path(base, href)
            if path.rstrip("/") == book_path:
                continue  # the collection itself
            status = text(resp.find("d:status", NS)) or ""
            if " 404 " in f"{status} ":
                removed.append(path)
            else:
                changed.append(path)
        return SyncResult(text(root.find("d:sync-token", NS)), changed, removed)

    def multiget(self, book: str, hrefs: list[str]) -> list[tuple[str, str]]:
        """(href, vCard text) for each card that still exists."""
        url = httpx.URL(book)
        out: list[tuple[str, str]] = []
        for i in range(0, len(hrefs), MULTIGET_BATCH):
            chunk = hrefs[i : i + MULTIGET_BATCH]
            body = (
                '<card:addressbook-multiget xmlns:d="DAV:" '
                'xmlns:card="urn:ietf:params:xml:ns:carddav">'
                "<d:prop><d:getetag/><card:address-data/></d:prop>"
                + "".join(f"<d:href>{escape(h)}</d:href>" for h in chunk)
                + "</card:addressbook-multiget>"
            )
            root, base = self._request("REPORT", url, body, "1")
            out.extend(self._cards(root, base))
        return out

    @staticmethod
    def _cards(root: ET.Element, base: httpx.URL) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for resp in root.findall("d:response", NS):
            href = text(resp.find("d:href", NS))
            data = resp.find("d:propstat/d:prop/card:address-data", NS)
            if href and data is not None and data.text:
                out.append((_path(base, href), data.text))
        return out
