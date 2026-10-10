# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

"""Expand a feed's events into occurrences overlapping a window (RFC 5545 subset).

Subscribed feeds arrive unexpanded (CalDAV calendars are expanded by iCloud instead).
Handled: RRULE (via dateutil), RDATE, EXDATE, and overrides (RECURRENCE-ID), including
overrides moved into or out of the window. Rules are evaluated in the event's own wall-clock
time, so a 09:00 meeting stays at 09:00 across DST changes. Not handled:
RANGE=THISANDFUTURE overrides (treated as single-occurrence overrides) and sub-hourly rules
(only DTSTART counts); neither appeared in the feeds probed on 2026-10-09.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

import icalendar
from dateutil import rrule

log = logging.getLogger(__name__)

# Bound on rule iteration per event: a daily rule since 1990 is ~13k steps.
MAX_RULE_STEPS = 100_000
_SKIP_FREQS = {"SECONDLY", "MINUTELY"}


@dataclass
class Occurrence:
    component: icalendar.cal.Component  # the master, or the override for a moved instance
    start: dt.date | dt.datetime
    end: dt.date | dt.datetime
    recurring: bool
    uid: str


def _first(component: icalendar.cal.Component, name: str) -> Any:
    value = component.get(name)
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _value(component: icalendar.cal.Component, name: str) -> Any:
    prop = _first(component, name)
    try:
        return prop.dt if prop is not None else None
    except Exception:
        return None


def uid_of(component: icalendar.cal.Component) -> str:
    uid = _first(component, "UID")
    if uid is not None and str(uid).strip():
        return str(uid).strip()
    # Feeds sometimes omit UID; derive a stable one from the event's own text.
    return "nouid-" + hashlib.sha256(component.to_ical()).hexdigest()[:16]


def _end(component: icalendar.cal.Component, start: dt.date | dt.datetime) -> Any:
    end = _value(component, "DTEND")
    if end is not None and type(end) is type(start):
        return end
    duration = _value(component, "DURATION")
    if isinstance(duration, dt.timedelta):
        return start + duration
    if isinstance(start, dt.datetime):
        if isinstance(end, dt.date):  # DTEND as a date next to a date-time start
            return dt.datetime.combine(end, dt.time(), start.tzinfo)
        return start
    return start + dt.timedelta(days=1)


def _utc(value: dt.date | dt.datetime, zone: dt.tzinfo) -> dt.datetime:
    if not isinstance(value, dt.datetime):
        value = dt.datetime.combine(value, dt.time())
    if value.tzinfo is None:
        value = value.replace(tzinfo=zone)
    return value.astimezone(dt.UTC)


def _overlaps(
    start: dt.date | dt.datetime,
    end: dt.date | dt.datetime,
    lo: dt.datetime,
    hi: dt.datetime,
    zone: dt.tzinfo,
) -> bool:
    s, e = _utc(start, zone), _utc(end, zone)
    return s < hi and (e > lo or s >= lo)


class _Wall:
    """Converts values to naive wall-clock time in the master's frame of reference."""

    def __init__(self, dtstart: dt.date | dt.datetime, default_tz: dt.tzinfo):
        self.is_date = not isinstance(dtstart, dt.datetime)
        self.tz = None if self.is_date else dtstart.tzinfo  # type: ignore[union-attr]
        self.frame = self.tz or default_tz

    def __call__(self, value: Any) -> dt.datetime | None:
        if isinstance(value, tuple):  # RDATE PERIOD: (start, end or duration)
            value = value[0]
        if isinstance(value, dt.datetime):
            if value.tzinfo is not None:
                return value.astimezone(self.frame).replace(tzinfo=None)
            return value
        if isinstance(value, dt.date):
            return dt.datetime.combine(value, dt.time())
        return None

    def back(self, naive: dt.datetime) -> dt.date | dt.datetime:
        if self.is_date:
            return naive.date()
        return naive.replace(tzinfo=self.tz) if self.tz is not None else naive


def _date_list(component: icalendar.cal.Component, name: str) -> list[Any]:
    props = component.get(name)
    if props is None:
        return []
    out: list[Any] = []
    for prop in props if isinstance(props, list) else [props]:
        out += [d.dt for d in getattr(prop, "dts", [])]
    return out


def _rule_starts(
    master: icalendar.cal.Component,
    wall: _Wall,
    first: dt.datetime,
    lo: dt.datetime,
    hi: dt.datetime,
) -> set[dt.datetime]:
    starts: set[dt.datetime] = set()
    rules = master.get("RRULE")
    for rule in rules if isinstance(rules, list) else [rules] if rules is not None else []:
        try:
            rule = copy.deepcopy(rule)
            if any(f in _SKIP_FREQS for f in rule.get("FREQ", [])):
                continue
            if "UNTIL" in rule:
                raw_until = rule["UNTIL"][0]
                until = wall(raw_until)
                if until is not None and not isinstance(raw_until, dt.datetime):
                    until = until.replace(hour=23, minute=59, second=59)  # UNTIL as a date
                rule["UNTIL"] = [until]
            generator = rrule.rrulestr(rule.to_ical().decode(), dtstart=first)
        except Exception:  # malformed rule: DTSTART still counts as the one occurrence
            continue
        for steps, occ in enumerate(generator):
            if occ > hi or steps >= MAX_RULE_STEPS:
                break
            if occ >= lo:
                starts.add(occ)
    return starts


def expand(
    calendar: icalendar.Calendar,
    lo_utc: dt.datetime,
    hi_utc: dt.datetime,
    default_tz: dt.tzinfo,
) -> list[Occurrence]:
    """Occurrences overlapping [lo_utc, hi_utc); dates and floating times read in default_tz."""
    by_uid: dict[str, list[icalendar.cal.Component]] = defaultdict(list)
    for component in calendar.walk("VEVENT"):
        by_uid[uid_of(component)].append(component)
    out: list[Occurrence] = []
    for uid, components in by_uid.items():
        try:
            out += _expand_series(uid, components, lo_utc, hi_utc, default_tz)
        except Exception as exc:  # one broken series must not hide the rest of the feed
            log.warning("skipping unexpandable feed event: %s", type(exc).__name__)
    return out


def _expand_series(
    uid: str,
    components: list[icalendar.cal.Component],
    lo_utc: dt.datetime,
    hi_utc: dt.datetime,
    default_tz: dt.tzinfo,
) -> list[Occurrence]:
    out: list[Occurrence] = []
    overrides = [c for c in components if c.get("RECURRENCE-ID") is not None]
    masters = [c for c in components if c.get("RECURRENCE-ID") is None]
    recurring_series = bool(overrides) or any(
        c.get("RRULE") is not None or c.get("RDATE") is not None for c in masters
    )

    for master in masters:
        start = _value(master, "DTSTART")
        if not isinstance(start, dt.date):
            continue
        end = _end(master, start)
        if master.get("RRULE") is None and master.get("RDATE") is None and not overrides:
            if _overlaps(start, end, lo_utc, hi_utc, default_tz):
                out.append(Occurrence(master, start, end, False, uid))
            continue

        wall = _Wall(start, default_tz)
        first = wall(start)
        assert first is not None
        duration = (end - start) if isinstance(end, type(start)) else dt.timedelta(0)
        # Generous wall-clock bounds; the exact overlap test happens per occurrence.
        pad = dt.timedelta(days=2)
        lo = lo_utc.astimezone(wall.frame).replace(tzinfo=None) - duration - pad
        hi = hi_utc.astimezone(wall.frame).replace(tzinfo=None) + pad

        starts = _rule_starts(master, wall, first, lo, hi)
        starts |= {s for s in map(wall, _date_list(master, "RDATE")) if s and lo <= s <= hi}
        if lo <= first <= hi:
            starts.add(first)  # DTSTART is always the first occurrence (RFC 5545)
        starts -= {s for s in map(wall, _date_list(master, "EXDATE")) if s}
        starts -= {wall(_value(o, "RECURRENCE-ID")) for o in overrides}
        for naive in sorted(starts):
            occ_start = wall.back(naive)
            occ_end = occ_start + duration
            if _overlaps(occ_start, occ_end, lo_utc, hi_utc, default_tz):
                out.append(Occurrence(master, occ_start, occ_end, True, uid))

    for override in overrides:
        start = _value(override, "DTSTART")
        if not isinstance(start, dt.date):
            continue
        end = _end(override, start)
        if _overlaps(start, end, lo_utc, hi_utc, default_tz):
            out.append(Occurrence(override, start, end, recurring_series, uid))
    return out
