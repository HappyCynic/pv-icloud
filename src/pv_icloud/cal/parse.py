# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

"""Turn server-expanded iCalendar resources into event dicts.

Each VEVENT in an expanded resource is one instance. Times keep the event's own time zone
(ISO 8601 with offset plus the zone name); all-day events use dates with an inclusive last
day; floating times (no zone) are reported without an offset.
"""

from __future__ import annotations

import datetime as dt
import logging
import zoneinfo
from collections import Counter
from dataclasses import dataclass
from typing import Any

import icalendar

from ..text import fold
from .caldav import CalendarInfo
from .expand import Occurrence
from .ids import EventId

log = logging.getLogger(__name__)

NOTES_MAX_CHARS = 1000
ATTENDEES_MAX = 50


@dataclass
class Event:
    data: dict[str, Any]  # calendar_events output (notes and attendees capped)
    start_utc: dt.datetime  # for range filtering and sorting
    end_utc: dt.datetime
    search_text: str  # folded (case, accents) title, location, full notes, calendar, people
    notes: str | None  # uncapped, for calendar_event
    attendees: list[dict[str, Any]]  # uncapped, for calendar_event


def _first(component: icalendar.cal.Component, name: str) -> Any:
    value = component.get(name)
    if isinstance(value, list):  # repeated property (malformed for single-valued ones)
        return value[0] if value else None
    return value


def _str(component: icalendar.cal.Component, name: str) -> str | None:
    value = _first(component, name)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _when(prop: Any) -> dt.date | dt.datetime | None:
    try:
        return prop.dt if prop is not None else None
    except Exception:
        return None


def _zone_name(value: dt.datetime) -> str | None:
    tz = value.tzinfo
    if tz is None:
        return None
    if isinstance(tz, zoneinfo.ZoneInfo):
        return tz.key
    if value.utcoffset() == dt.timedelta(0):
        return "UTC"
    return value.tzname()


def _as_utc(value: dt.date | dt.datetime, default_tz: dt.tzinfo) -> dt.datetime:
    """Absolute instant for filtering: dates and floating times are read in default_tz."""
    if not isinstance(value, dt.datetime):
        value = dt.datetime.combine(value, dt.time())
    if value.tzinfo is None:
        value = value.replace(tzinfo=default_tz)
    return value.astimezone(dt.UTC)


def person_email(prop: Any) -> str | None:
    """The address of an ORGANIZER or ATTENDEE value. iCloud stores the account's own address
    as its principal path with the address in an `EMAIL=` parameter (live check, 2026-10-13),
    so a value that is not `mailto:` takes the parameter instead. Shared with the calendar
    write path's "is this the account itself" check, so reads and writes agree."""
    address = str(prop).strip()
    if address.lower().startswith("mailto:"):
        return address[7:].strip() or None
    email = getattr(prop, "params", {}).get("EMAIL")
    return (str(email).strip() or None) if email else None


def _person(prop: Any) -> dict[str, Any]:
    params = getattr(prop, "params", {})
    out: dict[str, Any] = {"name": params.get("CN") or None, "email": person_email(prop)}
    if "PARTSTAT" in params:
        out["status"] = str(params["PARTSTAT"]).lower()
    return out


def _attendees(event: icalendar.cal.Component) -> list[Any]:
    value = event.get("ATTENDEE")
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def parse_resource(
    text: str, calendar: CalendarInfo, default_tz: dt.tzinfo, path: str
) -> list[Event]:
    cal = icalendar.Calendar.from_ical(text)
    vevents = list(cal.walk("VEVENT"))
    uid_counts = Counter(_str(ev, "UID") for ev in vevents)
    out: list[Event] = []
    for ev in vevents:
        try:
            event = _parse_event(ev, calendar, default_tz, uid_counts, path)
        except Exception as exc:  # one bad instance must not hide the rest
            log.warning("skipping unparseable event in %s: %s", calendar.id, type(exc).__name__)
            continue
        if event is not None:
            out.append(event)
    return out


def feed_ref(uid: str) -> str:
    """EventId reference for a feed event: there is no CalDAV resource path."""
    return "uid:" + uid


def parse_occurrences(
    occurrences: list[Occurrence], calendar: CalendarInfo, default_tz: dt.tzinfo
) -> list[Event]:
    out: list[Event] = []
    for occ in occurrences:
        try:
            event = _parse_event(
                occ.component, calendar, default_tz, Counter(), feed_ref(occ.uid), occ
            )
        except Exception as exc:
            log.warning(
                "skipping unparseable feed event in %s: %s", calendar.id, type(exc).__name__
            )
            continue
        if event is not None:
            out.append(event)
    return out


def _parse_event(
    ev: icalendar.cal.Component,
    calendar: CalendarInfo,
    default_tz: dt.tzinfo,
    uid_counts: Counter,
    path: str,
    occurrence: Occurrence | None = None,
) -> Event | None:
    start: Any
    end: Any
    if occurrence is not None:  # expanded locally (feeds): start/end/recurring are given
        start, end = occurrence.start, occurrence.end
    else:
        start, end = _when(_first(ev, "DTSTART")), _when(_first(ev, "DTEND"))
    if start is None:
        return None
    if end is None:
        duration = _when(_first(ev, "DURATION"))
        if isinstance(duration, dt.timedelta):
            end = start + duration
        else:  # RFC 5545 §3.6.1: a date lasts one day, a date-time has no duration
            end = start + dt.timedelta(days=1) if not isinstance(start, dt.datetime) else start
    all_day = not isinstance(start, dt.datetime)

    data: dict[str, Any] = {
        "id": EventId(calendar.id, start.isoformat(), path).encode(),
        "title": _str(ev, "SUMMARY") or "(no title)",
        "calendar": calendar.name,
        "calendar_id": calendar.id,
        "all_day": all_day,
    }
    if all_day:
        last_day = end if not isinstance(end, dt.datetime) else end.date()
        if last_day > start:
            last_day -= dt.timedelta(days=1)  # DTEND is exclusive; report the last day
        data.update(start=start.isoformat(), end=last_day.isoformat())
    else:
        assert isinstance(start, dt.datetime)
        if not isinstance(end, dt.datetime):
            end = dt.datetime.combine(end, dt.time(), start.tzinfo)
        data.update(start=start.isoformat(), end=end.isoformat(), timezone=_zone_name(start))
        if start.tzinfo is None:
            data["timezone_note"] = "floating time: shown as written, in no particular zone"

    location = _str(ev, "LOCATION")
    notes = _str(ev, "DESCRIPTION")
    data["location"] = location
    if notes is not None and len(notes) > NOTES_MAX_CHARS:
        data["notes"] = notes[:NOTES_MAX_CHARS]
        data["notes_truncated"] = True
    else:
        data["notes"] = notes
    if url := _str(ev, "URL"):
        data["url"] = url
    if status := _str(ev, "STATUS"):
        data["status"] = status.lower()
    organizer = _first(ev, "ORGANIZER")
    if organizer is not None:
        data["organizer"] = _person(organizer)
    attendees = [_person(a) for a in _attendees(ev)]
    if attendees:
        data["attendees"] = attendees[:ATTENDEES_MAX]
        if len(attendees) > ATTENDEES_MAX:
            data["attendees_total"] = len(attendees)
    uid = occurrence.uid if occurrence is not None else _str(ev, "UID")
    if occurrence is not None:
        data["recurring"] = occurrence.recurring
    else:
        data["recurring"] = ev.get("RECURRENCE-ID") is not None or uid_counts[uid] > 1
    data["uid"] = uid

    start_utc = _as_utc(start, default_tz)
    end_utc = _as_utc(end, default_tz)
    people = [data.get("organizer") or {}, *attendees]
    names = [str(v) for p in people for v in (p.get("name"), p.get("email")) if v]
    search_text = fold(
        " ".join(t for t in (data["title"], location, notes, calendar.name, *names) if t)
    )
    return Event(
        data=data,
        start_utc=start_utc,
        end_utc=end_utc,
        search_text=search_text,
        notes=notes,
        attendees=attendees,
    )
