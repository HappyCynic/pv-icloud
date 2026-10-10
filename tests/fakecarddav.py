# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

"""In-memory iCloud CardDAV double for httpx.MockTransport.

Mimics what the 2026-10-09 probe saw: discovery hops from contacts.icloud.com to a
per-account pNN-contacts host; the home (`/<dsid>/carddavhome/`) holds one address book
(`card/`) and names the owner's card in `cs:me-card`; `sync-collection` lists the
collection itself plus one entry per card (deleted cards as 404) and answers an unknown token
with HTTP 412; `addressbook-multiget` fails with HTTP 400 if the collection itself is among
the hrefs. Any method other than PROPFIND/REPORT is recorded as a write attempt.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from xml.sax.saxutils import escape

import httpx

HOME_HOST = "p99-contacts.icloud.com"


def fold_line(line: str, width: int = 75) -> str:
    """RFC 6350 folding, which iCloud applies anywhere, even inside a parameter."""
    chunks = [line[i : i + width] for i in range(0, len(line), width)] or [""]
    return "\r\n ".join(chunks)


def vcard(uid: str, *lines: str, fn: str | None = None, n: str | None = None) -> str:
    body = ["BEGIN:VCARD", "VERSION:3.0", "PRODID:-//Apple Inc.//iOS 26.0//EN"]
    if n is not None:
        body.append(f"N:{n}")
    if fn is not None:
        body.append(f"FN:{fn}")
    body += [*lines, f"UID:{uid}", "END:VCARD"]
    return "\r\n".join(fold_line(x) for x in body) + "\r\n"


def group_card(uid: str, name: str, members: list[str]) -> str:
    return vcard(
        uid,
        "X-ADDRESSBOOKSERVER-KIND:group",
        *(f"X-ADDRESSBOOKSERVER-MEMBER:urn:uuid:{m}" for m in members),
        fn=name,
        n=f"{name};;;;",
    )


@dataclass
class FakeBook:
    dsid: str
    cards: dict[str, str] = field(default_factory=dict)  # file name -> vCard text
    me: str | None = None  # file name of the owner's card
    seq: int = 1
    changed_at: dict[str, int] = field(default_factory=dict)  # file name -> seq of last change
    deleted_at: dict[str, int] = field(default_factory=dict)
    fail: int | None = None  # HTTP status for every request to the account's hosts
    retry_after: str | None = None  # Retry-After header sent with `fail`

    def __post_init__(self) -> None:
        for name in self.cards:
            self.changed_at[name] = self.seq

    def put(self, name: str, text: str) -> None:
        self.seq += 1
        self.cards[name] = text
        self.changed_at[name] = self.seq
        self.deleted_at.pop(name, None)

    def delete(self, name: str) -> None:
        self.seq += 1
        self.cards.pop(name)
        self.changed_at.pop(name, None)
        self.deleted_at[name] = self.seq


def _multistatus(responses: list[str], extra: str = "") -> httpx.Response:
    body = (
        '<?xml version="1.0"?><multistatus xmlns="DAV:">'
        + "".join(responses)
        + extra
        + "</multistatus>"
    )
    return httpx.Response(207, content=body.encode(), headers={"content-type": "text/xml"})


def _response(href: str, props: str) -> str:
    return (
        f"<response><href>{escape(href)}</href><propstat><prop>{props}</prop>"
        "<status>HTTP/1.1 200 OK</status></propstat></response>"
    )


class FakeCardDav:
    def __init__(self, books: dict[str, FakeBook], password: str = "app-pw"):
        self.books = books  # Apple ID -> book
        self.password = password
        self.requests: list[tuple[str, str]] = []  # (method, host+path)
        self.sync_tokens: list[str] = []  # tokens sent in sync-collection ("" = initial)
        self.multiget_counts: list[int] = []
        self.write_attempts: list[str] = []
        self.forget_tokens = False  # answer every non-empty token with 412

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def _user(self, request: httpx.Request) -> FakeBook | None:
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Basic "):
            return None
        user, _, pw = base64.b64decode(auth[6:]).decode().partition(":")
        return self.books.get(user) if pw == self.password else None

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        self.requests.append((request.method, f"{url.host}{url.path}"))
        if request.method not in ("PROPFIND", "REPORT"):
            self.write_attempts.append(request.method)
            return httpx.Response(405)
        book = self._user(request)
        if book is None:
            return httpx.Response(401)
        if book.fail:
            headers = {"retry-after": book.retry_after} if book.retry_after else {}
            return httpx.Response(book.fail, headers=headers)
        body = request.content.decode()
        home = f"/{book.dsid}/carddavhome/"
        if url.host == "contacts.icloud.com" and url.path == "/":
            return _multistatus(
                [
                    _response(
                        "/",
                        f"<current-user-principal><href>/{book.dsid}/principal/</href>"
                        "</current-user-principal>",
                    )
                ]
            )
        if url.host == "contacts.icloud.com" and url.path == f"/{book.dsid}/principal/":
            assert "addressbook-home-set" in body
            return _multistatus(
                [
                    _response(
                        url.path,
                        '<addressbook-home-set xmlns="urn:ietf:params:xml:ns:carddav">'
                        f"<href xmlns='DAV:'>https://{HOME_HOST}:443{home}</href>"
                        "</addressbook-home-set>",
                    )
                ]
            )
        if url.host != HOME_HOST:
            return httpx.Response(404)
        if request.method == "PROPFIND" and url.path == home:
            me = (
                '<me-card xmlns="http://calendarserver.org/ns/">'
                f"<href xmlns='DAV:'>{home}card/{book.me}</href></me-card>"
                if book.me
                else ""
            )
            return _multistatus(
                [
                    _response(home, f"<resourcetype><collection/></resourcetype>{me}"),
                    _response(
                        f"{home}card/",
                        "<resourcetype><collection/>"
                        '<addressbook xmlns="urn:ietf:params:xml:ns:carddav"/></resourcetype>',
                    ),
                ]
            )
        if request.method == "REPORT" and url.path == f"{home}card/":
            if "sync-collection" in body:
                return self._sync(book, f"{home}card/", body)
            if "addressbook-multiget" in body:
                return self._multiget(book, f"{home}card/", body)
        return httpx.Response(404)

    def _sync(self, book: FakeBook, path: str, body: str) -> httpx.Response:
        m = re.search(r"<d:sync-token>(.*?)</d:sync-token>", body)
        token = m.group(1) if m else ""
        self.sync_tokens.append(token)
        if token:
            since = int(token.removeprefix("t")) if re.fullmatch(r"t\d+", token) else None
            if since is None or self.forget_tokens:
                return httpx.Response(412, content=b"<error><valid-sync-token/></error>")
        else:
            since = 0
        out = [_response(path, '<getetag>"c"</getetag>')] if not token else []
        for name, seq in book.changed_at.items():
            if seq > since:
                out.append(_response(f"{path}{name}", f'<getetag>"{seq}"</getetag>'))
        if token:
            for name, seq in book.deleted_at.items():
                if seq > since:
                    out.append(
                        f"<response><href>{path}{name}</href>"
                        "<status>HTTP/1.1 404 Not Found</status></response>"
                    )
        return _multistatus(out, f"<sync-token>t{book.seq}</sync-token>")

    def _multiget(self, book: FakeBook, path: str, body: str) -> httpx.Response:
        hrefs = re.findall(r"<d:href>(.*?)</d:href>", body)
        self.multiget_counts.append(len(hrefs))
        out = []
        for href in hrefs:
            if href.rstrip("/") == path.rstrip("/"):
                return httpx.Response(400, content=b"Bad Request")
            name = href.rsplit("/", 1)[-1]
            if name in book.cards:
                out.append(
                    _response(
                        href,
                        '<getetag>"e"</getetag><address-data xmlns='
                        f'"urn:ietf:params:xml:ns:carddav">{escape(book.cards[name])}'
                        "</address-data>",
                    )
                )
            else:
                out.append(
                    f"<response><href>{href}</href>"
                    "<status>HTTP/1.1 404 Not Found</status></response>"
                )
        return _multistatus(out)
