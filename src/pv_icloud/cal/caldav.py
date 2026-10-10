# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

"""Minimal read-only CalDAV client for iCloud.

Only PROPFIND and REPORT are ever sent (`dav.DavClient._request` refuses anything else).
Recurring events are expanded by the server (`calendar-query` with `<C:expand>`); iCloud keeps
each instance's `TZID` in the expanded output, so no client-side recurrence engine is needed (probed
2026-10-09). iCloud ignores `<C:expand>` in `calendar-multiget` (it returns the RRULE master),
so single events are re-read with a narrow `calendar-query` as well.

Subscribed (webcal) calendars are listed with their feed URL (`cs:source`), but iCloud
returns no event data for them over CalDAV; `feeds.py` fetches those, as Apple clients do.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import threading
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import httpx

from ..dav import NS, DavAuthError, DavClient, DavError, Throttle
from ..dav import text as _text

log = logging.getLogger(__name__)

ROOT_URL = "https://caldav.icloud.com/"

_TZID_RE = re.compile(r"^TZID:(.+?)\r?$", re.M)

_CALENDARS_BODY = (
    '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" '
    'xmlns:a="http://apple.com/ns/ical/" xmlns:cs="http://calendarserver.org/ns/"><d:prop>'
    "<d:resourcetype/><d:displayname/><a:calendar-color/><cs:source/>"
    "<c:supported-calendar-component-set/><c:calendar-timezone/>"
    "</d:prop></d:propfind>"
)

# The calendar code predates the shared DAV base; keep its names.
CalDavError = DavError
CalDavAuthError = DavAuthError


@dataclass(frozen=True)
class CalendarInfo:
    id: str  # last path segment of the collection URL; stable per calendar
    url: str
    name: str
    color: str | None
    kind: str  # "calendar" (has events) or "subscribed" (webcal; no events over CalDAV)
    shared_with_me: bool
    timezone: str | None  # TZID of the calendar's default time zone
    # Feed URL of a subscription. Often embeds a private token: never log or return it.
    source: str | None = field(default=None, repr=False)


def _utc_stamp(when: dt.datetime) -> str:
    return when.astimezone(dt.UTC).strftime("%Y%m%dT%H%M%SZ")


def _events_body(start: dt.datetime, end: dt.datetime) -> str:
    s, e = _utc_stamp(start), _utc_stamp(end)
    return (
        '<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
        f'<d:prop><c:calendar-data><c:expand start="{s}" end="{e}"/></c:calendar-data></d:prop>'
        '<c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">'
        f'<c:time-range start="{s}" end="{e}"/>'
        "</c:comp-filter></c:comp-filter></c:filter></c:calendar-query>"
    )


def _color(raw: str | None) -> str | None:
    # Apple stores #RRGGBBAA; the alpha channel is always FF.
    if raw and re.fullmatch(r"#[0-9A-Fa-f]{8}", raw):
        return raw[:7]
    return raw


class CalDavClient(DavClient):
    """One account's CalDAV connection. Thread-safe; httpx pools the HTTPS connections."""

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
        """Calendar home URL (on a per-account pNN-caldav host), discovered once."""
        with self._lock:
            if self._home is None:
                self._home = self._discover("c:calendar-home-set")
            return self._home

    def forget_home(self) -> None:
        with self._lock:
            self._home = None

    def calendars(self) -> list[CalendarInfo]:
        home = self.home()
        root, base = self._request("PROPFIND", home, _CALENDARS_BODY, "1")
        out: list[CalendarInfo] = []
        for resp in root.findall("d:response", NS):
            href = _text(resp.find("d:href", NS))
            if href is None:
                continue
            url = base.join(href)
            if url.path.rstrip("/") == home.path.rstrip("/"):
                continue
            types = {el.tag.split("}")[-1] for el in resp.findall(".//d:resourcetype/*", NS)}
            comps = {
                el.get("name")
                for el in resp.findall(".//c:supported-calendar-component-set/c:comp", NS)
            }
            if "subscribed" in types:
                kind = "subscribed"
            elif "calendar" in types and "VEVENT" in comps:
                kind = "calendar"
            else:
                continue  # scheduling inbox/outbox, notifications, Reminders (VTODO) lists
            tz_blob = _text(resp.find(".//c:calendar-timezone", NS))
            tz_match = _TZID_RE.search(tz_blob) if tz_blob else None
            out.append(
                CalendarInfo(
                    id=url.path.rstrip("/").rsplit("/", 1)[-1],
                    url=str(url),
                    name=_text(resp.find(".//d:displayname", NS)) or "(unnamed)",
                    color=_color(_text(resp.find(".//a:calendar-color", NS))),
                    kind=kind,
                    shared_with_me="shared" in types,
                    timezone=tz_match.group(1).strip() if tz_match else None,
                    source=_text(resp.find(".//cs:source/d:href", NS)),
                )
            )
        return out

    @staticmethod
    def _resources(root: ET.Element, base: httpx.URL) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for resp in root.findall("d:response", NS):
            href = _text(resp.find("d:href", NS))
            data = resp.find("d:propstat/d:prop/c:calendar-data", NS)
            if href and data is not None and data.text:
                # Keep the path percent-encoded as served; it is sent back in multiget.
                raw = base.join(href).raw_path.decode("ascii").split("?", 1)[0]
                out.append((raw, data.text))
        return out

    def events(
        self, calendar: CalendarInfo, start: dt.datetime, end: dt.datetime
    ) -> list[tuple[str, str]]:
        """(resource path, iCalendar text) per resource, instances in [start, end) expanded."""
        url = httpx.URL(calendar.url)
        root, base = self._request("REPORT", url, _events_body(start, end), "1")
        return self._resources(root, base)
