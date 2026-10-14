# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

"""In-memory iCloud CalDAV double for httpx.MockTransport.

Mimics what the 2026-10-09 probe saw: discovery hops from caldav.icloud.com to a per-account
pNN-caldav host, the home lists calendars plus scheduling inbox/outbox, a Reminders (VTODO)
list and subscribed (webcal) collections, and REPORT with <C:expand> returns already
expanded instances that keep their TZID. Like iCloud, a matching resource without a rule
comes back whole, so instances just outside the range can appear; a stored `RRULE` master
is expanded into its instances (with `EXDATE`s and overrides applied).

Writes follow what the add-calendar-write live checks saw (2026-10-13/14): `PUT` and
`DELETE` on one event resource, `If-None-Match: *` and `If-Match` answered with 412 when
they do not hold, a Depth 0 `calendar-query` on a resource URL answered 403, a single-href
`calendar-multiget` answering with the stored resource and its ETag, and the account's own
address stored as its principal path with `EMAIL=`. iCloud schedules on the server whatever
`SCHEDULE-AGENT` says, so every write touching an event with someone else as attendee is
recorded in `scheduling_messages`: a test that sees one has made iCloud email someone.

Any other method, an unconditional write, and a write to a collection, a subscribed
calendar, a calendar without `write-content` or anything outside the calendar home are
recorded in `write_attempts` (forbidden) and refused.

Series edits (add-calendar-series-edit, live checks 2026-10-14) also need an unexpanded
`calendar-query` with a `time-range` whose end may be missing (a recurring resource matches
when any occurrence falls in it), answered with each resource's ETag and stored data, and
iCloud's answers to the filters it does not support: a `prop-filter` matches nothing (207),
a `collation` gives 412 and a `param-filter` 500. `calls` records every request in order
with what kind of read or which precondition it carried; `put_fail` answers the next `PUT`
to a resource with a chosen status, or stores it and loses the answer, and `delete_fail`
answers the next `DELETE` to a resource with a chosen status.
"""

from __future__ import annotations

import base64
import copy
import datetime as dt
import hashlib
import re
import urllib.parse
import zoneinfo
from collections.abc import Callable
from dataclasses import dataclass, field
from xml.sax.saxutils import escape

import httpx
import icalendar
from dateutil import rrule

HOME_HOST = "p99-caldav.icloud.com"
OWN_PRIVILEGES = ("read", "write", "write-properties", "write-content", "bind", "unbind")


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


def etag(data: str) -> str:
    """Changes whenever the stored data changes, as iCloud's ETags do."""
    return '"' + hashlib.sha1(data.encode()).hexdigest()[:16] + '"'


@dataclass
class FakeCalendar:
    id: str
    name: str
    color: str | None = "#0088FFFF"
    types: tuple[str, ...] = ("collection", "calendar")
    comps: tuple[str, ...] = ("VEVENT",)
    timezone: str = "America/Chicago"
    # Served as `<index>.ics`; a deleted one becomes None so the others keep their names.
    resources: list[str | None] = field(default_factory=list)
    fail: int | None = None  # HTTP status to return for REPORT
    source: str | None = None  # webcal URL of a subscribed calendar
    # current-user-privilege-set; None: the account's own (or a read-write share), or read
    # only for a subscription.
    privileges: tuple[str, ...] | None = None
    named: dict[str, str] = field(default_factory=dict)  # other resources, e.g. `<UUID>.ics`

    def privilege_set(self) -> tuple[str, ...]:
        if self.privileges is not None:
            return self.privileges
        return ("read",) if "subscribed" in self.types else OWN_PRIVILEGES

    def writable(self) -> bool:
        return "subscribed" not in self.types and bool(
            {"write-content", "write", "all"} & set(self.privilege_set())
        )

    def items(self) -> list[tuple[str, str]]:
        listed = [(f"{i}.ics", r) for i, r in enumerate(self.resources) if r is not None]
        return listed + list(self.named.items())

    def get(self, name: str) -> str | None:
        return dict(self.items()).get(name)

    def _index(self, name: str) -> int | None:
        m = re.fullmatch(r"(\d+)\.ics", name)
        return int(m.group(1)) if m and int(m.group(1)) < len(self.resources) else None

    def store(self, name: str, data: str) -> None:
        i = self._index(name)
        if i is not None:
            self.resources[i] = data
        else:
            self.named[name] = data

    def remove(self, name: str) -> None:
        i = self._index(name)
        if i is not None:
            self.resources[i] = None
        else:
            self.named.pop(name, None)


@dataclass
class FakeAccount:
    dsid: str
    calendars: list[FakeCalendar]
    # calendar-user-address-set (mailto: addresses); default: the Apple ID itself.
    addresses: tuple[str, ...] = ()

    @property
    def principal(self) -> str:
        return f"/{self.dsid}/principal/"


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
_SCHEDULING = re.compile(r"^(ORGANIZER|ATTENDEE)([^:\r\n]*):mailto:([^\r\n]+)(?=\r?$)", re.M | re.I)


@dataclass
class Write:
    method: str
    path: str
    headers: dict[str, str]
    body: str
    status: int


class FakeCalDav:
    def __init__(self, accounts: dict[str, FakeAccount], password: str = "app-pw"):
        self.accounts = accounts
        for user, acct in accounts.items():
            if not acct.addresses:
                acct.addresses = (user,)
        self.password = password
        self.requests: list[tuple[str, str]] = []  # (method, host+path)
        self.report_bodies: list[str] = []
        self.write_attempts: list[str] = []  # forbidden: refused and recorded
        self.writes: list[Write] = []  # PUT and DELETE requests that reached a calendar
        # What iCloud would have emailed: (method, resource name) per write that touched an
        # event with an attendee other than the account.
        self.scheduling_messages: list[tuple[str, str]] = []
        # Runs once, just before the next write is handled (e.g. an attendee's reply).
        self.before_write: Callable[[], None] | None = None
        self.write_fail: tuple[int, str | None] | None = None  # (status, Retry-After)
        # Resource name -> status for the next PUT to it; None: store it, lose the answer.
        self.put_fail: dict[str, int | None] = {}
        self.delete_fail: dict[str, int] = {}  # resource name -> status for the next DELETE
        # (method, resource or collection path, kind): REPORT kinds are `multiget`,
        # `expand` and `listing`; writes carry `If-Match` or `If-None-Match`.
        self.calls: list[tuple[str, str, str]] = []

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
        method = request.method
        self.requests.append((method, f"{url.host}{url.path}"))
        self.calls.append((method, url.path, _kind(request)))
        if method not in ("PROPFIND", "REPORT", "PUT", "DELETE"):
            self.write_attempts.append(method)
            return httpx.Response(405)
        acct = self._user(request)
        if acct is None:
            return httpx.Response(401)
        body = request.content.decode()
        if url.host == "caldav.icloud.com" and url.path == "/" and method == "PROPFIND":
            return _multistatus(
                [
                    _response(
                        "/",
                        f"<current-user-principal><href>{acct.principal}</href>"
                        "</current-user-principal>",
                    )
                ]
            )
        if url.host == "caldav.icloud.com" and url.path == acct.principal and method == "PROPFIND":
            return self._principal(acct, body)
        home_path = f"/{acct.dsid}/calendars/"
        if url.host != HOME_HOST or not url.path.startswith(home_path):
            if method in ("PUT", "DELETE"):
                self.write_attempts.append(f"{method} {url.path}")
                return httpx.Response(403)
            return httpx.Response(404)
        if method == "PROPFIND" and url.path == home_path:
            return self._list(acct, home_path)
        cal_id, _, name = url.path[len(home_path) :].partition("/")
        cal = next((c for c in acct.calendars if c.id == cal_id), None)
        if method in ("PUT", "DELETE"):
            return self._write(request, acct, cal, name, body)
        if cal is None:
            return httpx.Response(404)
        if method == "REPORT":
            if name:  # iCloud refuses a calendar-query on a resource URL (2026-10-14)
                return httpx.Response(403)
            return self._report(cal, url.path, body)
        return httpx.Response(404)

    def _principal(self, acct: FakeAccount, body: str) -> httpx.Response:
        props = ""
        if "calendar-home-set" in body:
            home = f"https://{HOME_HOST}:443/{acct.dsid}/calendars/"
            props += (
                '<calendar-home-set xmlns="urn:ietf:params:xml:ns:caldav">'
                f"<href xmlns='DAV:'>{home}</href></calendar-home-set>"
            )
        if "calendar-user-address-set" in body:
            hrefs = [f"mailto:{a}" for a in acct.addresses] + [acct.principal]
            props += (
                '<calendar-user-address-set xmlns="urn:ietf:params:xml:ns:caldav">'
                + "".join(f"<href xmlns='DAV:'>{escape(h)}</href>" for h in hrefs)
                + "</calendar-user-address-set>"
            )
        return _multistatus([_response(acct.principal, props)])

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
            privileges = "".join(f"<privilege><{p}/></privilege>" for p in cal.privilege_set())
            props = (
                f"{source}<resourcetype>{types}</resourcetype>"
                f"<displayname>{escape(cal.name)}</displayname>{color}"
                '<C:supported-calendar-component-set xmlns:C="urn:ietf:params:xml:ns:caldav">'
                f"{comps}</C:supported-calendar-component-set>"
                '<C:calendar-timezone xmlns:C="urn:ietf:params:xml:ns:caldav">'
                f"{escape(_vtimezone(cal.timezone))}</C:calendar-timezone>"
                f"<current-user-privilege-set>{privileges}</current-user-privilege-set>"
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
        if "calendar-multiget" in body:
            assert "<c:expand" not in body, "the fresh read must not be expanded"
            return self._multiget(cal, path, body)
        # Filters iCloud does not support (live check 2026-10-14).
        if "param-filter" in body:
            return httpx.Response(500)
        if "collation" in body:
            return httpx.Response(412)
        if "prop-filter" in body:
            return _multistatus([])
        m = re.search(r'time-range start="(\w+)"(?: end="(\w+)")?', body)
        assert m, "events are requested for a time range"
        start = dt.datetime.strptime(m.group(1), _STAMP).replace(tzinfo=dt.UTC)
        end = (
            dt.datetime.strptime(m.group(2), _STAMP).replace(tzinfo=dt.UTC)
            if m.group(2)
            else dt.datetime(2200, 1, 1, tzinfo=dt.UTC)
        )
        out = []
        if "<c:expand" not in body:  # a listing: whole stored resources with their ETags
            assert "getetag" in body, "a listing serves as a fresh read: ask for the ETag"
            for name, res in cal.items():
                if _expanded(res, start, end, cal.timezone) is not None:
                    out.append(self._data_response(f"{path}{name}", res, etag(res)))
            return _multistatus(out)
        assert m.group(2), "an expanded query has an end"
        for name, res in cal.items():
            served = _expanded(res, start, end, cal.timezone)
            if served is not None:
                out.append(self._data_response(f"{path}{name}", served, etag(res)))
        return _multistatus(out)

    def _multiget(self, cal: FakeCalendar, path: str, body: str) -> httpx.Response:
        out = []
        for href in re.findall(r"<(?:\w+:)?href>([^<]+)</(?:\w+:)?href>", body):
            href = href.strip()
            name = href[len(path) :] if href.startswith(path) else None
            res = cal.get(urllib.parse.unquote(name)) if name else None
            if res is None:
                out.append(
                    f"<response><href>{escape(href)}</href>"
                    "<status>HTTP/1.1 404 Not Found</status></response>"
                )
            else:
                out.append(self._data_response(href, res, etag(res)))
        return _multistatus(out)

    @staticmethod
    def _data_response(href: str, res: str, tag: str = '"e"') -> str:
        return _response(
            href,
            f"<getetag>{tag}</getetag><C:calendar-data "
            f'xmlns:C="urn:ietf:params:xml:ns:caldav">{escape(res)}</C:calendar-data>',
        )

    # --- writes ----------------------------------------------------------------------------

    def _write(
        self,
        request: httpx.Request,
        acct: FakeAccount,
        cal: FakeCalendar | None,
        name: str,
        body: str,
    ) -> httpx.Response:
        method, path = request.method, request.url.path
        headers = {k.lower(): v for k, v in request.headers.items()}
        if self.before_write is not None:
            hook, self.before_write = self.before_write, None
            hook()
        name = urllib.parse.unquote(name)
        if cal is None or not name or "/" in name or not name.endswith(".ics"):
            self.write_attempts.append(f"{method} {path}")  # collection, nested or unknown
            return httpx.Response(403)
        if not cal.writable():
            self.write_attempts.append(f"{method} {path}")  # subscribed or read-only share
            return httpx.Response(403)
        if "if-match" not in headers and headers.get("if-none-match") != "*":
            self.write_attempts.append(f"unconditional {method} {path}")
            return httpx.Response(428)
        if method == "PUT" and "if-match" in headers and "if-none-match" in headers:
            self.write_attempts.append(f"two preconditions {method} {path}")
            return httpx.Response(400)

        def done(status: int, extra: dict[str, str] | None = None) -> httpx.Response:
            self.writes.append(Write(method, path, headers, body, status))
            return httpx.Response(status, headers=extra or {})

        if self.write_fail is not None:
            status, retry_after = self.write_fail
            return done(status, {"retry-after": retry_after} if retry_after else None)
        lose_answer = False
        if method == "PUT" and name in self.put_fail:
            failure = self.put_fail.pop(name)
            if failure is not None:
                return done(failure)
            lose_answer = True
        if method == "DELETE" and name in self.delete_fail:
            return done(self.delete_fail.pop(name))
        current = cal.get(name)
        if method == "DELETE":
            if current is None:
                return done(404)
            if headers.get("if-match") != etag(current):
                return done(412)
            self._schedule(acct, method, name, current)
            cal.remove(name)
            return done(204)
        if headers.get("if-none-match") == "*":
            if current is not None:
                return done(412)
        elif current is None or headers.get("if-match") != etag(current):
            return done(412)
        if not headers.get("content-type", "").startswith("text/calendar"):
            return done(415)
        try:
            icalendar.Calendar.from_ical(body)
        except ValueError:
            return done(400)
        stored = self._as_icloud_stores(acct, body)
        self._schedule(acct, method, name, current, stored)
        cal.store(name, stored)
        answer = done(201 if current is None else 204, {"etag": etag(stored)})
        if lose_answer:
            raise httpx.ReadTimeout("answer lost", request=request)
        return answer

    @staticmethod
    def _as_icloud_stores(acct: FakeAccount, body: str) -> str:
        """The account's own `mailto:` organiser or attendee becomes its principal path."""
        own = {a.lower() for a in acct.addresses}

        def rewrite(m: re.Match) -> str:
            if m.group(3).strip().lower() not in own:
                return m.group(0)
            return f"{m.group(1)}{m.group(2)};EMAIL={m.group(3).strip()}:{acct.principal}"

        return _SCHEDULING.sub(rewrite, body)

    def _schedule(self, acct: FakeAccount, method: str, name: str, *versions: str | None):
        if any(v is not None and _has_other_attendee(acct, v) for v in versions):
            self.scheduling_messages.append((method, name))


def _kind(request: httpx.Request) -> str:
    if request.method == "REPORT":
        body = request.content.decode()
        if "calendar-multiget" in body:
            return "multiget"
        return "expand" if "<c:expand" in body else "listing"
    if "if-none-match" in request.headers:
        return "If-None-Match"
    return "If-Match" if "if-match" in request.headers else ""


def is_account(acct: FakeAccount, prop: icalendar.prop.vCalAddress) -> bool:
    value = str(prop).strip()
    own = {a.lower() for a in acct.addresses}
    if value.lower().startswith("mailto:"):
        return value[7:].lower() in own
    email = str(prop.params.get("EMAIL", "")).lower()
    return email in own or urllib.parse.urlsplit(value).path == acct.principal


def _has_other_attendee(acct: FakeAccount, data: str) -> bool:
    for ev in icalendar.Calendar.from_ical(data).walk("VEVENT"):
        people = ev.get("ATTENDEE") or []
        for person in people if isinstance(people, list) else [people]:
            if not is_account(acct, person):
                return True
    return False


# --- expansion (independent of pv_icloud.cal.expand, so the two check each other) -----------


def _key(value: dt.date | dt.datetime) -> dt.date | dt.datetime:
    if isinstance(value, dt.datetime) and value.tzinfo is not None:
        return value.astimezone(dt.UTC)
    return value


def _utc(value: dt.date | dt.datetime, zone: dt.tzinfo) -> dt.datetime:
    if not isinstance(value, dt.datetime):
        value = dt.datetime.combine(value, dt.time())
    return (value if value.tzinfo else value.replace(tzinfo=zone)).astimezone(dt.UTC)


def _length(ev: icalendar.cal.Component, start: dt.date | dt.datetime) -> dt.timedelta:
    if "DTEND" in ev:
        return ev["DTEND"].dt - start
    if "DURATION" in ev:
        return ev["DURATION"].dt
    return dt.timedelta(days=0 if isinstance(start, dt.datetime) else 1)


def _overlaps(s: dt.date | dt.datetime, e: dt.date | dt.datetime, lo, hi, zone) -> bool:
    us, ue = _utc(s, zone), _utc(e, zone)
    return us < hi and ue > lo


def _dates(ev: icalendar.cal.Component, name: str) -> set:
    props = ev.get(name) or []
    return {_key(d.dt) for p in (props if isinstance(props, list) else [props]) for d in p.dts}


def _rule_starts(master: icalendar.cal.Component, hi: dt.datetime, zone: dt.tzinfo):
    first = master["DTSTART"].dt
    rule = dict(master["RRULE"])
    until = rule.pop("UNTIL", [None])[0]
    text = icalendar.prop.vRecur(rule).to_ical().decode()
    is_date = not isinstance(first, dt.datetime)
    tz = None if is_date else first.tzinfo
    naive = dt.datetime.combine(first, dt.time()) if is_date else first.replace(tzinfo=None)
    for step, wall in enumerate(rrule.rrulestr(text, dtstart=naive)):
        when = wall.date() if is_date else wall.replace(tzinfo=tz)
        if step > 5000 or _utc(when, zone) >= hi:
            return
        if until is not None and _utc(when, zone) > _utc(until, zone):
            return
        yield when


def _expanded(res: str, lo: dt.datetime, hi: dt.datetime, tzid: str) -> str | None:
    """What iCloud serves for `res` in an expanded query: None when nothing overlaps."""
    zone = zoneinfo.ZoneInfo(tzid)
    try:
        events = list(icalendar.Calendar.from_ical(res).walk("VEVENT"))
    except ValueError:
        return res  # stored garbage is served as-is; the client must cope
    if not any("RRULE" in ev for ev in events):
        for ev in events:
            if "DTSTART" not in ev:
                return res
            s = ev["DTSTART"].dt
            e = ev["DTEND"].dt if "DTEND" in ev else s
            if _overlaps(s, e, lo, hi, zone):
                return res
        return None
    out = icalendar.Calendar()
    out.add("VERSION", "2.0")
    out.add("PRODID", "-//fake expanded//EN")
    for master in (ev for ev in events if "RRULE" in ev and "RECURRENCE-ID" not in ev):
        uid = str(master.get("UID"))
        overrides = [ev for ev in events if "RECURRENCE-ID" in ev and str(ev.get("UID")) == uid]
        first = master["DTSTART"].dt
        length = _length(master, first)
        skip = _dates(master, "EXDATE") | {_key(o["RECURRENCE-ID"].dt) for o in overrides}
        for when in _rule_starts(master, hi, zone):
            if _key(when) in skip or not _overlaps(when, when + length, lo, hi, zone):
                continue
            inst = copy.deepcopy(master)
            for prop in ("RRULE", "RDATE", "EXDATE", "DTSTART", "DTEND", "DURATION"):
                inst.pop(prop, None)
            inst.add("DTSTART", when)
            inst.add("DTEND", when + length)
            inst.add("RECURRENCE-ID", when)
            out.add_component(inst)
        for o in overrides:
            start = o["DTSTART"].dt
            if _overlaps(start, start + _length(o, start), lo, hi, zone):
                out.add_component(o)
    if not out.subcomponents:
        return None
    return out.to_ical().decode()


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
