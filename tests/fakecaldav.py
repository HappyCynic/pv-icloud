# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

"""In-memory iCloud CalDAV double for httpx.MockTransport.

Mimics what the 2026-10-09 probe saw: discovery hops from caldav.icloud.com to a per-account
pNN-caldav host, the home lists calendars plus scheduling inbox/outbox, a Reminders (VTODO)
list and subscribed (webcal) collections, and REPORT with <C:expand> returns already
expanded instances that keep their TZID. Like iCloud, a matching resource comes back whole,
so instances just outside the range can appear. Any method other than PROPFIND/REPORT is
recorded as a write attempt.
"""

from __future__ import annotations

import base64
import datetime as dt
import re
from dataclasses import dataclass, field
from xml.sax.saxutils import escape

import httpx

HOME_HOST = "p99-caldav.icloud.com"


def vevent(
    uid: str,
    summary: str,
    start: dt.date | dt.datetime,
    end: dt.date | dt.datetime | None = None,
    *,
    tz: str | None = "America/Chicago",
    recurrence_id: bool = False,
    extra: tuple[str, ...] = (),
) -> str:
    def prop(name: str, value: dt.date | dt.datetime) -> str:
        if not isinstance(value, dt.datetime):
            return f"{name};VALUE=DATE:{value:%Y%m%d}"
        if tz == "UTC":
            return f"{name}:{value:%Y%m%dT%H%M%S}Z"
        if tz is None:
            return f"{name}:{value:%Y%m%dT%H%M%S}"
        return f"{name};TZID={tz}:{value:%Y%m%dT%H%M%S}"

    lines = ["BEGIN:VEVENT", f"UID:{uid}", f"SUMMARY:{summary}", prop("DTSTART", start)]
    if end is not None:
        lines.append(prop("DTEND", end))
    if recurrence_id:
        lines.append(prop("RECURRENCE-ID", start))
    lines += [*extra, "END:VEVENT"]
    return "\r\n".join(lines)


def resource(*events: str) -> str:
    return "\r\n".join(
        ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//fake//EN", *events, "END:VCALENDAR", ""]
    )


@dataclass
class FakeCalendar:
    id: str
    name: str
    color: str | None = "#0088FFFF"
    types: tuple[str, ...] = ("collection", "calendar")
    comps: tuple[str, ...] = ("VEVENT",)
    timezone: str = "America/Chicago"
    resources: list[str] = field(default_factory=list)
    fail: int | None = None  # HTTP status to return for REPORT
    source: str | None = None  # webcal URL of a subscribed calendar


@dataclass
class FakeAccount:
    dsid: str
    calendars: list[FakeCalendar]


def _vtimezone(tzid: str) -> str:
    return f"BEGIN:VCALENDAR\r\nBEGIN:VTIMEZONE\r\nTZID:{tzid}\r\nEND:VTIMEZONE\r\nEND:VCALENDAR"


def _multistatus(responses: list[str]) -> httpx.Response:
    body = '<?xml version="1.0"?><multistatus xmlns="DAV:">' + "".join(responses) + "</multistatus>"
    return httpx.Response(207, content=body.encode(), headers={"content-type": "text/xml"})


def _response(href: str, props: str) -> str:
    return (
        f"<response><href>{escape(href)}</href><propstat><prop>{props}</prop>"
        "<status>HTTP/1.1 200 OK</status></propstat></response>"
    )


_STAMP = "%Y%m%dT%H%M%SZ"


class FakeCalDav:
    def __init__(self, accounts: dict[str, FakeAccount], password: str = "app-pw"):
        self.accounts = accounts
        self.password = password
        self.requests: list[tuple[str, str]] = []  # (method, host+path)
        self.report_bodies: list[str] = []
        self.write_attempts: list[str] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def _user(self, request: httpx.Request) -> FakeAccount | None:
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Basic "):
            return None
        user, _, pw = base64.b64decode(auth[6:]).decode().partition(":")
        if pw != self.password:
            return None
        return self.accounts.get(user)

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        self.requests.append((request.method, f"{url.host}{url.path}"))
        if request.method not in ("PROPFIND", "REPORT"):
            self.write_attempts.append(request.method)
            return httpx.Response(405)
        acct = self._user(request)
        if acct is None:
            return httpx.Response(401)
        body = request.content.decode()
        if url.host == "caldav.icloud.com" and url.path == "/":
            return _multistatus(
                [
                    _response(
                        "/",
                        f"<current-user-principal><href>/{acct.dsid}/principal/</href>"
                        "</current-user-principal>",
                    )
                ]
            )
        if url.host == "caldav.icloud.com" and url.path == f"/{acct.dsid}/principal/":
            home = f"https://{HOME_HOST}:443/{acct.dsid}/calendars/"
            return _multistatus(
                [
                    _response(
                        url.path,
                        '<calendar-home-set xmlns="urn:ietf:params:xml:ns:caldav">'
                        f"<href xmlns='DAV:'>{home}</href></calendar-home-set>",
                    )
                ]
            )
        home_path = f"/{acct.dsid}/calendars/"
        if url.host != HOME_HOST or not url.path.startswith(home_path):
            return httpx.Response(404)
        if request.method == "PROPFIND" and url.path == home_path:
            return self._list(acct, home_path)
        cal_id = url.path[len(home_path) :].strip("/")
        cal = next((c for c in acct.calendars if c.id == cal_id), None)
        if cal is None:
            return httpx.Response(404)
        if request.method == "REPORT":
            return self._report(cal, url.path, body)
        return httpx.Response(404)

    def _list(self, acct: FakeAccount, home_path: str) -> httpx.Response:
        entries = [_response(home_path, "<resourcetype><collection/></resourcetype>")]
        extra = [
            FakeCalendar("inbox", "", None, ("collection", "schedule-inbox")),
            FakeCalendar(
                "outbox", "", None, ("collection", "schedule-outbox"), ("VEVENT", "VTODO")
            ),
            FakeCalendar("notification", "", None, ("collection", "notification"), ()),
        ]
        for cal in [*acct.calendars, *extra]:
            types = "".join(
                f"<{t}/>" if t in ("collection",) else self._typed(t) for t in cal.types
            )
            comps = "".join(f'<C:comp name="{c}"/>' for c in cal.comps)
            color = (
                f'<calendar-color xmlns="http://apple.com/ns/ical/">{cal.color}</calendar-color>'
                if cal.color
                else ""
            )
            source = (
                f'<source xmlns="http://calendarserver.org/ns/"><href xmlns="DAV:">'
                f"{escape(cal.source)}</href></source>"
                if cal.source
                else ""
            )
            props = (
                f"{source}<resourcetype>{types}</resourcetype>"
                f"<displayname>{escape(cal.name)}</displayname>{color}"
                '<C:supported-calendar-component-set xmlns:C="urn:ietf:params:xml:ns:caldav">'
                f"{comps}</C:supported-calendar-component-set>"
                '<C:calendar-timezone xmlns:C="urn:ietf:params:xml:ns:caldav">'
                f"{escape(_vtimezone(cal.timezone))}</C:calendar-timezone>"
            )
            entries.append(_response(f"{home_path}{cal.id}/", props))
        return _multistatus(entries)

    @staticmethod
    def _typed(t: str) -> str:
        if t == "calendar":
            return '<calendar xmlns="urn:ietf:params:xml:ns:caldav"/>'
        if t in ("schedule-inbox", "schedule-outbox"):
            return f'<{t} xmlns="urn:ietf:params:xml:ns:caldav"/>'
        return f'<{t} xmlns="http://calendarserver.org/ns/"/>'

    def _report(self, cal: FakeCalendar, path: str, body: str) -> httpx.Response:
        self.report_bodies.append(body)
        if cal.fail:
            return httpx.Response(cal.fail)
        if "subscribed" in cal.types:
            # iCloud answers with the collection's etag only; no calendar data.
            return _multistatus([_response(path, '<getetag>"x"</getetag>')])
        m = re.search(r'time-range start="(\w+)" end="(\w+)"', body)
        assert m and "<c:expand" in body, "events must be requested expanded"
        start = dt.datetime.strptime(m.group(1), _STAMP).replace(tzinfo=dt.UTC)
        end = dt.datetime.strptime(m.group(2), _STAMP).replace(tzinfo=dt.UTC)
        out = []
        for i, res in enumerate(cal.resources):
            if self._overlaps(res, start, end, cal.timezone):
                out.append(self._data_response(f"{path}{i}.ics", res))
        return _multistatus(out)

    @staticmethod
    def _data_response(href: str, res: str) -> str:
        return _response(
            href,
            '<getetag>"e"</getetag><C:calendar-data '
            f'xmlns:C="urn:ietf:params:xml:ns:caldav">{escape(res)}</C:calendar-data>',
        )

    @staticmethod
    def _overlaps(res: str, start: dt.datetime, end: dt.datetime, tzid: str) -> bool:
        import zoneinfo

        import icalendar

        zone = zoneinfo.ZoneInfo(tzid)
        try:
            events = icalendar.Calendar.from_ical(res).walk("VEVENT")
        except ValueError:
            return True  # stored garbage is served as-is; the client must cope
        for ev in events:
            if "DTSTART" not in ev:
                return True
            s = ev["DTSTART"].dt
            e = ev["DTEND"].dt if "DTEND" in ev else s
            if not isinstance(s, dt.datetime):
                s = dt.datetime.combine(s, dt.time(), zone)
                e = dt.datetime.combine(e, dt.time(), zone)
            s = s if s.tzinfo else s.replace(tzinfo=zone)
            e = e if e.tzinfo else e.replace(tzinfo=zone)
            if s < end and e > start:
                return True
        return False


class FakeFeeds:
    """Publishers of subscribed calendars, plus the DNS they resolve through."""

    def __init__(self):
        self.feeds: dict[str, str] = {}  # https URL -> iCalendar text
        self.etags: dict[str, str] = {}  # URL -> ETag (feeds that support revalidation)
        self.redirects: dict[str, str] = {}  # URL -> Location
        self.status: dict[str, int] = {}  # URL -> forced HTTP status
        self.dns: dict[str, str] = {}  # host -> IP (default: a public address)
        self.rebind: dict[str, str] = {}  # host -> IP every later lookup returns
        self.answered: dict[str, set[str]] = {}  # host -> IPs handed out
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def resolve(self, host: str, port: int) -> list[str]:
        answer = self.dns.get(host, "93.184.215.14")
        self.answered.setdefault(host, set()).add(answer)
        if self.rebind.get(host):  # DNS rebinding: the next answer differs
            self.dns[host] = self.rebind.pop(host)
        return [answer]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        # The client connects to the address it checked and names the host in Host and SNI.
        host = request.headers["host"]
        name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        assert request.url.host in self.answered.get(name, set())
        if request.url.scheme == "https":
            assert request.extensions["sni_hostname"] == name
        url = str(request.url.copy_with(netloc=host.encode()))
        if url in self.redirects:
            return httpx.Response(302, headers={"location": self.redirects[url]})
        if url in self.status:
            return httpx.Response(self.status[url])
        if url not in self.feeds:
            return httpx.Response(404)
        etag = self.etags.get(url)
        if etag and request.headers.get("if-none-match") == etag:
            return httpx.Response(304, headers={"etag": etag})
        headers = {"content-type": "text/calendar", **({"etag": etag} if etag else {})}
        return httpx.Response(200, content=self.feeds[url].encode(), headers=headers)
