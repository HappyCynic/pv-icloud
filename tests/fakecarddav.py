# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

"""In-memory iCloud CardDAV double for httpx.MockTransport.

Mimics what the 2026-10-09 probe saw: discovery hops from contacts.icloud.com to a
per-account pNN-contacts host; the home (`/<dsid>/carddavhome/`) holds one address book
(`card/`) and names the owner's card in `cs:me-card`; `sync-collection` lists the
collection itself plus one entry per card (deleted cards as 404) and answers an unknown token
with HTTP 412; `addressbook-multiget` fails with HTTP 400 if the collection itself is among
the hrefs.

Writes follow what the ADR-0006 live checks saw (2026-10-14): every card has an ETag, which
changes whenever the card does; `PUT` and `DELETE` of one `.vcf` directly in the address book
are allowed; `If-None-Match: *` on an existing card and an `If-Match` that does not match get
HTTP 412. A `PUT` body is stored byte for byte. Any other method, an unconditional write, a
write to the collection, the home, a nested or non-`.vcf` path or another host, and a
`DELETE` of a group card or the me-card are recorded in `write_attempts` (forbidden) and
refused.
"""

from __future__ import annotations

import base64
import hashlib
import re
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from xml.sax.saxutils import escape

import httpx

HOME_HOST = "p99-contacts.icloud.com"
# Card text is served with each CR as a character reference, as Apple's server does, so the
# CRLF line endings survive XML parsing (a literal CR LF would be read back as LF).
_CR = {"\r": "&#13;"}


def etag(text: str) -> str:
    """The card's ETag: changes whenever its text does."""
    return '"' + hashlib.sha256(text.encode()).hexdigest()[:16] + '"'


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


def group_card(uid: str, name: str, members: list[str], *extra: str) -> str:
    return vcard(
        uid,
        "X-ADDRESSBOOKSERVER-KIND:group",
        *(f"X-ADDRESSBOOKSERVER-MEMBER:urn:uuid:{m}" for m in members),
        *extra,
        fn=name,
        n=f"{name};;;;",
    )


# Real-shaped cards for the write tests, as Apple's apps write them (live address book,
# 2026-10-14): one TYPE= parameter per type, IPHONE before CELL, VOICE only on built-in voice
# types, INTERNET first on emails, PREF last; item groups with Apple and custom labels; a
# birthday and a date without a year; a photo; Apple-internal and unknown properties.
PHOTO_DATA = "/9j/4AAQSkZJRgABAQAAAQABAAD" * 12
ANN = vcard(
    "ann-uid",
    "ORG:Widgets;Sales",
    "TITLE:Buyer",
    "EMAIL;TYPE=INTERNET;TYPE=WORK;TYPE=PREF:ann@widgets.example",
    "item1.EMAIL;TYPE=INTERNET:ann.lee@mail.example",
    "item1.X-ABLabel:_$!<Other>!$_",
    "TEL;TYPE=CELL;TYPE=VOICE;TYPE=PREF:+1 312 555 0100",
    "TEL;TYPE=IPHONE;TYPE=CELL;TYPE=VOICE:+1 312 555 0101",
    "TEL;TYPE=WORK;TYPE=VOICE:+1 312 555 0102",
    "TEL;TYPE=MAIN:+1 312 555 0103",
    "TEL;TYPE=HOME;TYPE=FAX:+1 312 555 0104",
    "TEL;TYPE=PAGER:+1 312 555 0105",
    "TEL:+1 312 555 0106",
    "item2.TEL:+1 312 555 0107",
    "item2.X-ABLabel:Boat",
    "item3.ADR;TYPE=HOME;TYPE=PREF:;;1 Main St;Springfield;IL;62701;United States",
    "item3.X-ABADR:us",
    "item4.X-ABRELATEDNAMES;TYPE=PREF:Bo Lee",
    "item4.X-ABLabel:_$!<Sister>!$_",
    "item5.X-ABDATE;X-APPLE-OMIT-YEAR=1604:1604-06-01",
    "item5.X-ABLabel:_$!<Anniversary>!$_",
    "item6.URL;TYPE=PREF;X-PV-NOTE=a-parameter-long-enough-to-be-folded-by-icloud:https://ann.example",
    "item6.X-ABLabel:_$!<HomePage>!$_",
    "BDAY;X-APPLE-OMIT-YEAR=1604:1604-03-15",
    "X-SOCIALPROFILE;TYPE=linkedin;X-USER=annlee:https://www.linkedin.com/in/annlee",
    "IMPP;X-SERVICE-TYPE=Skype;TYPE=PREF:skype:ann.lee",
    "NOTE:Met at the fair\\, 2024\\nLikes tea",
    "X-APPLE-SUBLOCALITY:Downtown",
    'X-PV-UNKNOWN;X-FOO="a:b":kept as is',
    f"PHOTO;ENCODING=b;TYPE=JPEG:{PHOTO_DATA}",
    "REV:2026-01-02T03:04:05Z",
    fn="Ann Lee",
    n="Lee;Ann;;;",
)
BOB = vcard(  # a custom display name and no REV
    "bob-uid",
    "EMAIL;TYPE=INTERNET;TYPE=HOME:bob@example.org",
    "TEL;TYPE=HOME;TYPE=VOICE:+1 312 555 0200",
    fn="Bob Smith",
    n="Smith;Robert;;;",
)
WIDGETS = vcard(  # shows as a company
    "widgets-uid",
    "ORG:Widgets;",
    "X-ABShowAs:COMPANY",
    "TEL;TYPE=WORK;TYPE=VOICE:+1 312 555 0300",
    "REV:2026-02-03T04:05:06Z",
    fn="Widgets",
    n=";;;;",
)
CAROL = vcard(  # two NOTE lines: a note change is ambiguous
    "carol-uid", "NOTE:first", "NOTE:second", "REV:2026-01-01T00:00:00Z", fn="Carol Diaz",
    n="Diaz;Carol;;;",
)  # fmt: skip
DAVE = vcard(  # two REV lines: no update is safe
    "dave-uid", "REV:2026-01-01T00:00:00Z", "REV:2026-01-02T00:00:00Z", fn="Dave Fox",
    n="Fox;Dave;;;",
)  # fmt: skip
ERIN = vcard("erin-uid", fn="Erin Gale", n="Gale;Erin;;;").replace("VERSION:3.0", "VERSION:4.0")
OWNER = vcard(
    "me-uid", "EMAIL;TYPE=INTERNET;TYPE=HOME:owner@example.com", fn="Alice Owner",
    n="Owner;Alice;;;",
)  # fmt: skip
DOCTOR = vcard("doc-uid", "EMAIL;TYPE=INTERNET:dr@clinic.example", fn="Dr. Strange")
FAMILY = group_card("g-family", "Family", ["ann-uid", "doc-uid"], "REV:2026-01-05T00:00:00Z")
WORK = group_card("g-work", "Work", ["gone-uid"], "X-PV-GROUP-NOTE:kept")
MEDICAL = group_card("g-medical", "Medical", ["doc-uid"])


def write_book() -> FakeBook:
    """Alice's address book for the write tests. Dr. Strange is in the excluded group
    `Medical` (exclude it in the account config) and, hidden, in `Family`."""
    return FakeBook(
        "111",
        cards={
            "ann-file.vcf": ANN,  # file name differs from UID
            "bob-uid.vcf": BOB,
            "widgets-uid.vcf": WIDGETS,
            "carol-uid.vcf": CAROL,
            "dave-uid.vcf": DAVE,
            "erin-uid.vcf": ERIN,
            "me-uid.vcf": OWNER,
            "doc-uid.vcf": DOCTOR,
            "g-family.vcf": FAMILY,
            "g-work.vcf": WORK,
            "g-medical.vcf": MEDICAL,
        },
        me="me-uid.vcf",
    )


@dataclass
class Write:
    method: str
    path: str
    headers: dict[str, str]
    body: str
    status: int


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
        self.write_attempts: list[str] = []  # forbidden: refused and recorded
        self.writes: list[Write] = []  # PUT and DELETE requests that reached a card
        self.forget_tokens = False  # answer every non-empty token with 412
        # Runs once, just before the next write is handled (e.g. an edit in Contacts).
        self.before_write: Callable[[], None] | None = None
        self.write_fail: tuple[int, str | None] | None = None  # (status, Retry-After)
        self.fail_reads = False  # answer REPORTs with 500 (a refresh after a write fails)

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
        if request.method not in ("PROPFIND", "REPORT", "PUT", "DELETE"):
            self.write_attempts.append(f"{request.method} {url.path}")
            return httpx.Response(405)
        book = self._user(request)
        if book is None:
            return httpx.Response(401)
        if book.fail:
            headers = {"retry-after": book.retry_after} if book.retry_after else {}
            return httpx.Response(book.fail, headers=headers)
        body = request.content.decode()
        home = f"/{book.dsid}/carddavhome/"
        if request.method in ("PUT", "DELETE"):
            return self._write(request, book, home, body)
        if self.fail_reads and request.method == "REPORT":
            return httpx.Response(500)
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
                tag = escape(etag(book.cards[name]))
                out.append(_response(f"{path}{name}", f"<getetag>{tag}</getetag>"))
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
            name = urllib.parse.unquote(href.rsplit("/", 1)[-1])
            if name in book.cards:
                text = book.cards[name]
                out.append(
                    _response(
                        href,
                        f"<getetag>{escape(etag(text))}</getetag><address-data xmlns="
                        f'"urn:ietf:params:xml:ns:carddav">{escape(text, _CR)}'
                        "</address-data>",
                    )
                )
            else:
                out.append(
                    f"<response><href>{href}</href>"
                    "<status>HTTP/1.1 404 Not Found</status></response>"
                )
        return _multistatus(out)

    # --- writes ----------------------------------------------------------------------------

    def _write(self, request: httpx.Request, book: FakeBook, home: str, body: str):
        method, url = request.method, request.url
        path = url.path
        headers = {k.lower(): v for k, v in request.headers.items()}
        if self.before_write is not None:
            hook, self.before_write = self.before_write, None
            hook()
        collection = f"{home}card/"
        name = urllib.parse.unquote(url.raw_path.decode("ascii"))[len(collection) :]
        if (
            url.host != HOME_HOST
            or not path.startswith(collection)
            or not name
            or "/" in name
            or not name.endswith(".vcf")
        ):
            self.write_attempts.append(f"{method} {path}")  # collection, home, nested, other
            return httpx.Response(403)
        if "if-match" not in headers and headers.get("if-none-match") != "*":
            self.write_attempts.append(f"unconditional {method} {path}")
            return httpx.Response(428)
        if method == "PUT" and "if-match" in headers and "if-none-match" in headers:
            self.write_attempts.append(f"two preconditions {method} {path}")
            return httpx.Response(400)
        current = book.cards.get(name)
        protected = current is not None and (
            name == book.me or "X-ADDRESSBOOKSERVER-KIND:group" in current
        )
        if method == "DELETE" and protected:
            self.write_attempts.append(f"DELETE of a protected card {path}")
            return httpx.Response(403)

        def done(status: int, extra: dict[str, str] | None = None) -> httpx.Response:
            self.writes.append(Write(method, path, headers, body, status))
            return httpx.Response(status, headers=extra or {})

        if self.write_fail is not None:
            status, retry_after = self.write_fail
            return done(status, {"retry-after": retry_after} if retry_after else None)
        if method == "DELETE":
            if current is None:
                return done(404)
            if headers.get("if-match") != etag(current):
                return done(412)
            book.delete(name)
            return done(204)
        if headers.get("if-none-match") == "*":
            if current is not None:
                return done(412)
        elif current is None or headers.get("if-match") != etag(current):
            return done(412)
        if not headers.get("content-type", "").startswith("text/vcard"):
            return done(415)
        if not body.startswith("BEGIN:VCARD") or "UID:" not in body:
            return done(400)
        book.put(name, body)  # stored byte for byte
        return done(201 if current is None else 204, {"etag": etag(body)})
