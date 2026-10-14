# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

"""Build and edit iCalendar data for the calendar write tools (ADR-0005).

Pure functions over `icalendar` objects; nothing here talks to iCloud. A new event is one
VEVENT with times in an IANA zone (`TZID` plus a matching VTIMEZONE; floating times are never
written), display alerts only, and never an ORGANIZER or ATTENDEE: iCloud schedules on the
server and ignores `SCHEDULE-AGENT=CLIENT`, so an attendee would get real mail.

An edit changes one component of a stored resource and keeps every other component and
property as stored: one occurrence of a series becomes an override (`RECURRENCE-ID` = its
original start, in the master's value type and zone) or, when deleted, an `EXDATE` on the
master. `check_people` refuses any resource whose events involve someone other than the
account, before anything is changed.
"""

from __future__ import annotations

import copy
import datetime as dt
import zoneinfo
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import icalendar

from ..errors import ToolInputError
from .caldav import AccountAddresses, principal_key
from .expand import expand
from .parse import person_email

PRODID = "-//pv-icloud//EN"
TITLE_MAX_CHARS = 255
LOCATION_MAX_CHARS = 1000
NOTES_MAX_CHARS = 10_000
ALERTS_MAX = 5
ALERT_MAX_MINUTES = 40_320  # four weeks
EVENT_MAX_DAYS = 366
INTERVAL_MAX = 99
COUNT_MAX = 730
FREQUENCIES = ("daily", "weekly", "monthly", "yearly")
WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
CLEARABLE = ("location", "notes", "alerts")
_RECURRENCE_KEYS = frozenset({"frequency", "interval", "weekdays", "count", "until"})
# Window around an occurrence's start when expanding a series to find it (any zone offset).
_FIND_WINDOW = dt.timedelta(days=2)
_SERIES_PROPS = ("RRULE", "RDATE", "EXDATE", "EXRULE")

ORGANISED_ELSEWHERE = "this event was organised by someone else; change it in Calendar"
HAS_ATTENDEES = "event has attendees; change it in Calendar"

Moment = dt.date | dt.datetime


@dataclass(frozen=True)
class Changes:
    """Tool input for a new event or an update; None means not given."""

    title: str | None = None
    start: str | None = None
    end: str | None = None
    all_day: bool | None = None
    timezone: str | None = None
    location: str | None = None
    notes: str | None = None
    alerts: tuple[int, ...] | None = None
    clear: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Recurrence:
    frequency: str
    interval: int = 1
    weekdays: tuple[str, ...] = ()
    count: int | None = None
    until: dt.date | None = None


@dataclass
class Target:
    """The event or occurrence an id names, inside a stored resource."""

    master: icalendar.cal.Component | None  # None: a lone override (no master stored)
    override: icalendar.cal.Component | None  # the occurrence's own component, if any
    original: Moment | None  # original start of an occurrence; None: a single event


# --- validation ---------------------------------------------------------------------------


def check_changes(changes: Changes, *, creating: bool) -> None:
    """Field limits, before anything is read or sent. Times are checked with the event."""
    unknown = sorted(changes.clear - set(CLEARABLE))
    if unknown:
        raise ToolInputError(
            f"clear: unknown field(s) {', '.join(unknown)}; allowed: {', '.join(CLEARABLE)}"
        )
    for name in sorted(changes.clear):
        if getattr(changes, name) is not None:
            raise ToolInputError(f"{name}: given and also listed in clear")
    if changes.title is not None or creating:
        title = changes.title or ""
        if not title.strip():
            raise ToolInputError("title is required and must not be blank")
        if len(title) > TITLE_MAX_CHARS:
            raise ToolInputError(f"title must be at most {TITLE_MAX_CHARS} characters")
        if "\n" in title or "\r" in title:
            raise ToolInputError("title must not contain line breaks")
    if creating and not changes.start:
        raise ToolInputError("start is required")
    if changes.location is not None and len(changes.location) > LOCATION_MAX_CHARS:
        raise ToolInputError(f"location must be at most {LOCATION_MAX_CHARS} characters")
    if changes.notes is not None and len(changes.notes) > NOTES_MAX_CHARS:
        raise ToolInputError(f"notes must be at most {NOTES_MAX_CHARS} characters")
    if changes.alerts is not None:
        alerts = changes.alerts
        if len(alerts) > ALERTS_MAX:
            raise ToolInputError(f"alerts: at most {ALERTS_MAX}")
        if len(set(alerts)) != len(alerts):
            raise ToolInputError("alerts: each may be given only once")
        if any(not 0 <= a <= ALERT_MAX_MINUTES for a in alerts):
            raise ToolInputError(f"alerts: minutes before the start, 0 to {ALERT_MAX_MINUTES}")
    if changes.timezone is not None:
        zone(changes.timezone)


def zone(name: str) -> zoneinfo.ZoneInfo:
    try:
        return zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError) as exc:
        raise ToolInputError(f"timezone: unknown time zone {name!r} (use an IANA name)") from exc


def parse_recurrence(raw: Mapping[str, Any] | None) -> Recurrence | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ToolInputError("recurrence must be an object")
    unknown = sorted(set(raw) - _RECURRENCE_KEYS)
    if unknown:
        raise ToolInputError(f"recurrence: unknown key(s) {', '.join(unknown)}")
    frequency = raw.get("frequency")
    if frequency not in FREQUENCIES:
        raise ToolInputError(f"recurrence: frequency must be one of {', '.join(FREQUENCIES)}")
    interval = raw.get("interval", 1)
    if not isinstance(interval, int) or isinstance(interval, bool):
        raise ToolInputError("recurrence: interval must be a whole number")
    if not 1 <= interval <= INTERVAL_MAX:
        raise ToolInputError(f"recurrence: interval must be between 1 and {INTERVAL_MAX}")
    weekdays = raw.get("weekdays") or []
    if not isinstance(weekdays, list) or any(d not in WEEKDAYS for d in weekdays):
        raise ToolInputError(f"recurrence: weekdays are from {', '.join(WEEKDAYS)}")
    if weekdays and frequency != "weekly":
        raise ToolInputError("recurrence: weekdays need frequency weekly")
    count, until = raw.get("count"), raw.get("until")
    if count is not None and until is not None:
        raise ToolInputError("recurrence: give count or until, not both")
    if count is not None and (
        not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= COUNT_MAX
    ):
        raise ToolInputError(f"recurrence: count must be between 1 and {COUNT_MAX}")
    until_day = None
    if until is not None:
        try:
            until_day = dt.date.fromisoformat(str(until))
        except ValueError as exc:
            raise ToolInputError("recurrence: until must be an ISO date (YYYY-MM-DD)") from exc
    return Recurrence(frequency, interval, tuple(dict.fromkeys(weekdays)), count, until_day)


def _date(value: str, field: str) -> dt.date:
    try:
        if len(value) != 10:
            raise ValueError(value)
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ToolInputError(f"{field}: expected an ISO date (YYYY-MM-DD)") from exc


def _datetime(value: str, field: str, tz: dt.tzinfo) -> dt.datetime:
    try:
        if len(value) <= 10:  # a bare date would silently become midnight
            raise ValueError(value)
        when = dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise ToolInputError(
            f"{field}: expected an ISO datetime (YYYY-MM-DDTHH:MM), or a date with all_day"
        ) from exc
    when = when.replace(microsecond=0)
    return when.astimezone(tz) if when.tzinfo else when.replace(tzinfo=tz)


def _length(component: icalendar.cal.Component, start: Moment) -> dt.timedelta:
    end = _value(component, "DTEND")
    if end is not None and type(end) is type(start):
        return end - start
    duration = _value(component, "DURATION")
    if isinstance(duration, dt.timedelta):
        return duration
    return dt.timedelta(0) if isinstance(start, dt.datetime) else dt.timedelta(days=1)


def _times(
    changes: Changes,
    default_tz: dt.tzinfo,
    current: tuple[Moment, Moment] | None = None,
) -> tuple[Moment, Moment]:
    """(DTSTART, exclusive DTEND) after applying `changes` to the current times, if any.
    Omitted bounds keep the current ones; a moved start keeps the event's length."""
    cur_start, cur_end = current if current is not None else (None, None)
    was_all_day = cur_start is not None and not isinstance(cur_start, dt.datetime)
    all_day = changes.all_day if changes.all_day is not None else was_all_day
    same_kind = cur_start is not None and was_all_day == all_day
    if all_day:
        if changes.start:
            start: Moment = _date(changes.start, "start")
        elif isinstance(cur_start, dt.datetime):
            start = cur_start.date()
        else:
            assert cur_start is not None
            start = cur_start
        if changes.end:
            last = _date(changes.end, "end")
        elif same_kind and cur_end is not None and cur_start is not None:
            days = max((cur_end - cur_start).days, 1)
            last = start + dt.timedelta(days=days - 1)
        else:
            last = start
        if last < start:
            raise ToolInputError("end must not be before start (end is the last day)")
        if (last - start).days + 1 > EVENT_MAX_DAYS:
            raise ToolInputError(f"an event may last at most {EVENT_MAX_DAYS} days")
        return start, last + dt.timedelta(days=1)

    if changes.timezone:
        tz: dt.tzinfo = zone(changes.timezone)
    elif isinstance(cur_start, dt.datetime) and cur_start.tzinfo is not None:
        tz = cur_start.tzinfo
    else:
        tz = default_tz
    if changes.start:
        start_dt = _datetime(changes.start, "start", tz)
    elif isinstance(cur_start, dt.datetime):
        start_dt = cur_start
        if changes.timezone or cur_start.tzinfo is None:  # same wall-clock time, new zone
            start_dt = cur_start.replace(tzinfo=tz)
    else:
        raise ToolInputError("start: give a time when changing an all-day event to a timed one")
    if changes.end:
        end_dt = _datetime(changes.end, "end", tz)
    elif same_kind and cur_start is not None and cur_end is not None:
        end_dt = start_dt + (cur_end - cur_start)
    else:
        end_dt = start_dt + dt.timedelta(hours=1)
    if end_dt <= start_dt:
        raise ToolInputError("end must be after start")
    if end_dt - start_dt > dt.timedelta(days=EVENT_MAX_DAYS):
        raise ToolInputError(f"an event may last at most {EVENT_MAX_DAYS} days")
    return start_dt, end_dt


# --- building -----------------------------------------------------------------------------


def _set(component: icalendar.cal.Component, name: str, value: Any) -> None:
    component.pop(name, None)
    component.add(name, value)


def _alarms(component: icalendar.cal.Component, minutes: tuple[int, ...]) -> None:
    component.subcomponents = [c for c in component.subcomponents if c.name != "VALARM"]
    for n in minutes:
        alarm = icalendar.Alarm()
        alarm.add("ACTION", "DISPLAY")  # never EMAIL: iCloud would send it
        alarm.add("DESCRIPTION", "Reminder")
        alarm.add("TRIGGER", -dt.timedelta(minutes=n))
        component.add_component(alarm)


def _apply(
    component: icalendar.cal.Component,
    changes: Changes,
    default_tz: dt.tzinfo,
    current: tuple[Moment, Moment] | None,
) -> None:
    if changes.title is not None:
        _set(component, "SUMMARY", changes.title)
    if current is None or any(
        v is not None for v in (changes.start, changes.end, changes.all_day, changes.timezone)
    ):
        start, end = _times(changes, default_tz, current)
        component.pop("DURATION", None)
        _set(component, "DTSTART", start)
        _set(component, "DTEND", end)
    for name, prop in (("location", "LOCATION"), ("notes", "DESCRIPTION")):
        value = getattr(changes, name)
        if name in changes.clear:
            component.pop(prop, None)
        elif value is not None:
            _set(component, prop, value)
    if "alerts" in changes.clear:
        _alarms(component, ())
    elif changes.alerts is not None:
        _alarms(component, changes.alerts)


def _rrule(recurrence: Recurrence, start: Moment) -> dict[str, Any]:
    rule: dict[str, Any] = {"FREQ": recurrence.frequency.upper()}
    if recurrence.interval != 1:
        rule["INTERVAL"] = recurrence.interval
    if recurrence.weekdays:
        rule["BYDAY"] = list(recurrence.weekdays)
    if recurrence.count is not None:
        rule["COUNT"] = recurrence.count
    if recurrence.until is not None:
        first_day = start.date() if isinstance(start, dt.datetime) else start
        if recurrence.until < first_day:
            raise ToolInputError("recurrence: until must not be before start")
        if isinstance(start, dt.datetime):
            # RFC 5545: UNTIL of a zoned series is UTC; the whole last day counts.
            last = dt.datetime.combine(recurrence.until, dt.time(23, 59, 59), start.tzinfo)
            rule["UNTIL"] = last.astimezone(dt.UTC)
        else:
            rule["UNTIL"] = recurrence.until
    return rule


def _touch(component: icalendar.cal.Component, now: dt.datetime) -> None:
    try:
        sequence = int(component.get("SEQUENCE", 0))
    except TypeError, ValueError:
        sequence = 0
    _set(component, "SEQUENCE", sequence + 1)
    _set(component, "LAST-MODIFIED", now)
    _set(component, "DTSTAMP", now)


def new_event(
    changes: Changes,
    recurrence: Recurrence | None,
    default_tz: dt.tzinfo,
    uid: str,
    now: dt.datetime,
) -> icalendar.Calendar:
    check_changes(changes, creating=True)
    if changes.clear:
        raise ToolInputError("clear is only for calendar_event_update")
    event = icalendar.Event()
    event.add("UID", uid)
    for name in ("DTSTAMP", "CREATED", "LAST-MODIFIED"):
        event.add(name, now)
    event.add("SEQUENCE", 0)
    _apply(event, changes, default_tz, None)
    if recurrence is not None:
        event.add("RRULE", _rrule(recurrence, _value(event, "DTSTART")))
    cal = icalendar.Calendar()
    cal.add("VERSION", "2.0")
    cal.add("PRODID", PRODID)
    cal.add_component(event)
    cal.add_missing_timezones()
    return cal


def to_text(cal: icalendar.Calendar) -> str:
    return cal.to_ical().decode()


def occurrence_text(component: icalendar.cal.Component, start: Moment, recurring: bool) -> str:
    """One instance as an expanded read would return it, so the result of a write is
    formatted (and its id built) by the same code as calendar_event's."""
    inst = copy.deepcopy(component)
    length = _length(component, _value(component, "DTSTART"))
    for name in (*_SERIES_PROPS, "DURATION"):
        inst.pop(name, None)
    _set(inst, "DTSTART", start)
    _set(inst, "DTEND", start + length)
    if recurring and "RECURRENCE-ID" not in inst:
        inst.add("RECURRENCE-ID", start)
    cal = icalendar.Calendar()
    cal.add("VERSION", "2.0")
    cal.add("PRODID", PRODID)
    cal.add_component(inst)
    return to_text(cal)


# --- editing a stored resource ------------------------------------------------------------


def _value(component: icalendar.cal.Component, name: str) -> Any:
    prop = component.get(name)
    if isinstance(prop, list):
        prop = prop[0] if prop else None
    try:
        return prop.dt if prop is not None else None
    except AttributeError:
        return None


def _vevents(cal: icalendar.Calendar) -> list[icalendar.cal.Component]:
    return [c for c in cal.subcomponents if c.name == "VEVENT"]


def _uid(component: icalendar.cal.Component) -> str:
    return str(component.get("UID", "")).strip()


def _is_series(component: icalendar.cal.Component) -> bool:
    return any(component.get(name) is not None for name in ("RRULE", "RDATE"))


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, dt.date) else None


def find_target(cal: icalendar.Calendar, start: str, default_tz: dt.tzinfo) -> Target | None:
    """The event or occurrence whose (current) start, as the read tools write it in ids, is
    `start`; None when the resource no longer has it."""
    events = _vevents(cal)
    for ev in events:
        if ev.get("RECURRENCE-ID") is not None and _iso(_value(ev, "DTSTART")) == start:
            master = next(
                (
                    m
                    for m in events
                    if m.get("RECURRENCE-ID") is None and _uid(m) == _uid(ev) and _is_series(m)
                ),
                None,
            )
            return Target(master, ev, _value(ev, "RECURRENCE-ID"))
    for ev in events:
        single = ev.get("RECURRENCE-ID") is None and not _is_series(ev)
        if single and _iso(_value(ev, "DTSTART")) == start:
            return Target(ev, None, None)
    try:
        when = dt.datetime.fromisoformat(start)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.UTC)
    for occ in expand(cal, when - _FIND_WINDOW, when + _FIND_WINDOW, default_tz):
        comp = occ.component
        if comp.get("RECURRENCE-ID") is None and _is_series(comp) and _iso(occ.start) == start:
            return Target(comp, None, occ.start)
    return None


def is_account(prop: Any, addresses: AccountAddresses) -> bool:
    """Whether an ORGANIZER or ATTENDEE value is the account itself: one of its addresses,
    or its principal (iCloud's rewrite, with the address in `EMAIL=`). Anything else, an
    unknown form included, is someone else."""
    value = str(prop).strip()
    email = person_email(prop)
    if email is not None and email.lower() in addresses.emails:
        return True
    return not value.lower().startswith("mailto:") and principal_key(value) in addresses.principals


def check_people(cal: icalendar.Calendar, addresses: AccountAddresses) -> None:
    """Refuse a resource any of whose events was organised by, or invites, someone other than
    the account: changing it would make iCloud email them (ADR-0005)."""
    events = _vevents(cal)
    for ev in events:
        organizer = ev.get("ORGANIZER")
        for prop in organizer if isinstance(organizer, list) else [organizer]:
            if prop is not None and not is_account(prop, addresses):
                raise ToolInputError(ORGANISED_ELSEWHERE)
    for ev in events:
        attendees = ev.get("ATTENDEE")
        for prop in attendees if isinstance(attendees, list) else [attendees]:
            if prop is not None and not is_account(prop, addresses):
                raise ToolInputError(HAS_ATTENDEES)


def _like(master_start: Moment, value: Moment) -> Moment:
    """`value` in the value type and zone of the master's DTSTART, for RECURRENCE-ID and
    EXDATE."""
    if not isinstance(master_start, dt.datetime):
        return value.date() if isinstance(value, dt.datetime) else value
    if not isinstance(value, dt.datetime):
        value = dt.datetime.combine(value, master_start.timetz())
    if master_start.tzinfo is None:
        return value.replace(tzinfo=None)
    if value.tzinfo is None:
        return value.replace(tzinfo=master_start.tzinfo)
    return value.astimezone(master_start.tzinfo)


def _new_override(
    cal: icalendar.Calendar, master: icalendar.cal.Component, original: Moment, now: dt.datetime
) -> icalendar.cal.Component:
    master_start = _value(master, "DTSTART")
    original = _like(master_start, original)
    override = copy.deepcopy(master)
    for name in (*_SERIES_PROPS, "DURATION", "RECURRENCE-ID"):
        override.pop(name, None)
    _set(override, "DTSTART", original)
    _set(override, "DTEND", original + _length(master, master_start))
    override.add("RECURRENCE-ID", original)
    cal.subcomponents.insert(cal.subcomponents.index(master) + 1, override)
    return override


def apply_update(
    cal: icalendar.Calendar,
    target: Target,
    changes: Changes,
    default_tz: dt.tzinfo,
    now: dt.datetime,
) -> icalendar.cal.Component:
    """Change one event or occurrence in place; returns the component changed."""
    check_changes(changes, creating=False)
    if target.override is not None:
        component = target.override
    elif target.original is not None:
        assert target.master is not None
        component = _new_override(cal, target.master, target.original, now)
    else:
        assert target.master is not None
        component = target.master
    start = _value(component, "DTSTART")
    current = (start, start + _length(component, start))
    _apply(component, changes, default_tz, current)
    _touch(component, now)
    cal.add_missing_timezones()
    return component


def apply_delete(cal: icalendar.Calendar, target: Target, now: dt.datetime) -> bool:
    """Remove one event or occurrence. True: delete the whole resource instead (a single
    event, or a lone override that is all the resource holds)."""
    if target.override is None and target.original is None:
        return True
    if target.master is None:
        assert target.override is not None
        if all(c is target.override for c in _vevents(cal)):
            return True
        cal.subcomponents.remove(target.override)
        return False
    assert target.original is not None
    master_start = _value(target.master, "DTSTART")
    original = _like(master_start, target.original)
    key = _iso(original)
    cal.subcomponents = [
        c
        for c in cal.subcomponents
        if not (
            c.name == "VEVENT"
            and c.get("RECURRENCE-ID") is not None
            and _uid(c) == _uid(target.master)
            and _iso(_like(master_start, _value(c, "RECURRENCE-ID"))) == key
        )
    ]
    target.master.add("EXDATE", original)
    _touch(target.master, now)
    return False
