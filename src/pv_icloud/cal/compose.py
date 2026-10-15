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

A series edit (ADR-0007) changes a whole series, or a series from a date on. From a date, it
splits the series as Apple Calendar does: the stored resource's rule ends the day before
(`UNTIL`), and the rest becomes a new series with a new `UID` in a new resource; both carry
one `RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET` value, and Apple treats every
resource with that value (a *part*) as one series.
"""

from __future__ import annotations

import copy
import dataclasses
import datetime as dt
import zoneinfo
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

import icalendar
from dateutil import rrule as dateutil_rrule

from ..errors import ToolInputError
from .caldav import AccountAddresses, principal_key
from .expand import MAX_RULE_STEPS, _Wall, expand
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
    for name in ("start", "end"):  # the format only; the times are checked with the event
        value = getattr(changes, name)
        if value is None:
            continue
        if changes.all_day is True or (changes.all_day is None and len(value) == 10):
            _date(value, name)
        else:
            _datetime(value, name, dt.UTC)


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


def occurrence_text(component: icalendar.cal.Component, recurring: bool) -> str:
    """The component's first instance as an expanded read would return it, so the result
    of a write is formatted (and its id built) by the same code as calendar_event's."""
    inst = copy.deepcopy(component)
    start = _value(component, "DTSTART")
    length = _length(component, start)
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


# --- series edits (ADR-0007) --------------------------------------------------------------

MODES = ("replace_all", "replace_from", "update_all", "update_from")
RECURRENCE_SET = "X-CALENDARSERVER-RECURRENCE-SET"
PARTS_MAX = 10
COUNT_DAYS = 366  # an endless series is counted through this many days
LOOKBACK_DAYS = 366  # earlier parts of a recurrence set are looked for back to here
_PATTERN = ("start", "end", "all_day", "timezone")
_FIELDS = ("title", "location", "notes", "alerts")
_FIELD_PROPS = {"title": "SUMMARY", "location": "LOCATION", "notes": "DESCRIPTION"}

NOT_A_SERIES = "not a recurring series; use calendar_event_update"
UNSUPPORTED_SERIES = "this series cannot be changed here; change it in Calendar"
SPLIT_SERIES = "this series was split; change it from a date (replace_from / update_from)"
TOO_MANY_PARTS = f"this series has more than {PARTS_MAX} parts; change it in Calendar"
NO_OCCURRENCE = "the new definition has no occurrence; check start and recurrence"


@dataclass(frozen=True)
class SeriesEdit:
    """Checked tool input of calendar_series_update."""

    mode: str
    effective: dt.date | None
    changes: Changes
    recurrence: Recurrence | None
    drop_unmatched: bool = False
    preview: bool = False

    @property
    def replace(self) -> bool:
        return self.mode.startswith("replace_")

    @property
    def from_date(self) -> bool:
        return self.mode.endswith("_from")

    @property
    def pattern(self) -> bool:
        """Changes the pattern (times, zone, all-day or rule), not only field edits."""
        return self.recurrence is not None or any(
            getattr(self.changes, name) is not None for name in _PATTERN
        )


def series_edit(
    mode: Any,
    effective: Any,
    changes: Changes,
    recurrence: Mapping[str, Any] | None,
    drop_unmatched: Any = False,
    preview: Any = False,
) -> SeriesEdit:
    """Input checks, before anything is read or sent."""
    if mode not in MODES:
        raise ToolInputError(f"mode must be one of {', '.join(MODES)}")
    for name, value in (("drop_unmatched", drop_unmatched), ("preview", preview)):
        if not isinstance(value, bool):
            raise ToolInputError(f"{name} must be true or false")
    day = None
    if mode.endswith("_from"):
        if not effective:
            raise ToolInputError(f"effective is required for {mode} (an ISO date, YYYY-MM-DD)")
        day = _date(str(effective), "effective")
    elif effective is not None:
        raise ToolInputError("effective is only for replace_from and update_from")
    check_changes(changes, creating=False)
    rule = parse_recurrence(recurrence)
    if mode.startswith("update_") and changes == Changes() and rule is None:
        raise ToolInputError("nothing to change")
    if day is not None and changes.start is not None and changes.start[:10] < day.isoformat():
        raise ToolInputError("start must not be before effective")
    return SeriesEdit(mode, day, changes, rule, drop_unmatched, preview)


@dataclass
class Part:
    """One stored resource of a series (a recurrence set has several), as just read."""

    path: str
    etag: str
    cal: icalendar.Calendar
    master: icalendar.cal.Component

    @property
    def uid(self) -> str:
        return _uid(self.master)

    @property
    def start(self) -> Moment:
        return _value(self.master, "DTSTART")


def _props(component: icalendar.cal.Component, name: str) -> list[Any]:
    props = component.get(name)
    return list(props) if isinstance(props, list) else [props] if props is not None else []


def recurrence_set(component: icalendar.cal.Component) -> str | None:
    """The component's `RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET` value."""
    for prop in _props(component, "RELATED-TO"):
        if str(prop.params.get("RELTYPE", "")).upper() == RECURRENCE_SET:
            return str(prop).strip() or None
    return None


def _set_recurrence_set(component: icalendar.cal.Component, value: str | None) -> None:
    others = [
        p
        for p in _props(component, "RELATED-TO")
        if str(p.params.get("RELTYPE", "")).upper() != RECURRENCE_SET
    ]
    component.pop("RELATED-TO", None)
    for prop in others:
        component.add("RELATED-TO", prop)
    if value is not None:
        component.add("RELATED-TO", value, parameters={"RELTYPE": RECURRENCE_SET})


def _overrides(cal: icalendar.Calendar, master: icalendar.cal.Component) -> list[Any]:
    uid = _uid(master)
    return [c for c in _vevents(cal) if c.get("RECURRENCE-ID") is not None and _uid(c) == uid]


def check_series(cal: icalendar.Calendar, master: icalendar.cal.Component) -> None:
    """Refuse a series this tool cannot change exactly: anything but one master with one
    RRULE and single-occurrence overrides."""
    if (
        not isinstance(master.get("RRULE"), icalendar.prop.vRecur)
        or any(master.get(name) is not None for name in ("RDATE", "EXRULE"))
        or not isinstance(_value(master, "DTSTART"), dt.date)
    ):
        raise ToolInputError(UNSUPPORTED_SERIES)
    uid = _uid(master)
    masters = [c for c in _vevents(cal) if c.get("RECURRENCE-ID") is None and _uid(c) == uid]
    if len(masters) != 1:
        raise ToolInputError(UNSUPPORTED_SERIES)
    for override in _overrides(cal, master):
        rid = override.get("RECURRENCE-ID")
        if isinstance(rid, list) or str(rid.params.get("RANGE", "")).upper() == "THISANDFUTURE":
            raise ToolInputError(UNSUPPORTED_SERIES)
        if not isinstance(_value(override, "DTSTART"), dt.date):
            raise ToolInputError(UNSUPPORTED_SERIES)


def series_master(cal: icalendar.Calendar, target: Target) -> icalendar.cal.Component:
    """The master of the series an occurrence id names, after the shape checks."""
    if target.original is None and target.override is None:
        raise ToolInputError(NOT_A_SERIES)
    if target.master is None:
        raise ToolInputError(UNSUPPORTED_SERIES)
    check_series(cal, target.master)
    return target.master


def pick_parts(
    listed: Iterable[tuple[str, str, str]], value: str, skip: Iterable[str]
) -> list[Part]:
    """The other parts of recurrence set `value` in a listing: resources whose master carries
    that value (matched here: iCloud has no property filter)."""
    seen = set(skip)
    out: list[Part] = []
    for path, text, tag in listed:
        if path in seen:
            continue
        try:
            cal = icalendar.Calendar.from_ical(text)
        except ValueError:
            continue
        for ev in _vevents(cal):
            if ev.get("RECURRENCE-ID") is None and recurrence_set(ev) == value:
                out.append(Part(path, tag, cal, ev))
                seen.add(path)
                break
    return out


# Wall-clock occurrences: naive datetimes in the series' own zone (as cal/expand.py works).


def _generator(rule: Any, wall: _Wall, first: dt.datetime) -> Iterator[dt.datetime]:
    rule = copy.deepcopy(rule)
    if "UNTIL" in rule:
        raw = rule["UNTIL"][0]
        until = wall(raw)
        if until is not None and not isinstance(raw, dt.datetime):
            until = until.replace(hour=23, minute=59, second=59)  # UNTIL as a date
        rule["UNTIL"] = [until]
    text = rule.to_ical().decode()
    return iter(dateutil_rrule.rrulestr(text, dtstart=first))


def _rule(master: icalendar.cal.Component) -> Any:
    """A copy of the master's one RRULE (check_series made sure there is one)."""
    return copy.deepcopy(master["RRULE"])


def _wall_of(master: icalendar.cal.Component, default_tz: dt.tzinfo) -> _Wall:
    return _Wall(_value(master, "DTSTART"), default_tz)


def _local(wall: _Wall, value: Moment) -> dt.datetime:
    local = wall(value)
    assert local is not None
    return local


def _iter_walls(master: icalendar.cal.Component, default_tz: dt.tzinfo) -> Iterator[dt.datetime]:
    """Every start the series generates, DTSTART first, in order (at most MAX_RULE_STEPS)."""
    wall = _wall_of(master, default_tz)
    first = _local(wall, _value(master, "DTSTART"))
    yield first
    for steps, occ in enumerate(_generator(_rule(master), wall, first)):
        if steps >= MAX_RULE_STEPS:
            return
        if occ > first:
            yield occ


def _walls(
    master: icalendar.cal.Component, default_tz: dt.tzinfo, before: dt.date | None = None
) -> list[dt.datetime]:
    """The series' starts, all of them or those on days before `before`."""
    out: list[dt.datetime] = []
    for occ in _iter_walls(master, default_tz):
        if before is not None and occ.date() >= before:
            break
        out.append(occ)
    return out


def _endless(master: icalendar.cal.Component) -> bool:
    rule = _rule(master)
    return "COUNT" not in rule and "UNTIL" not in rule


def _last_wall(master: icalendar.cal.Component, default_tz: dt.tzinfo) -> dt.datetime | None:
    if _endless(master):
        return None
    walls = _walls(master, default_tz)
    return walls[-1]


def _has_from(part: Part, day: dt.date, default_tz: dt.tzinfo) -> bool:
    last = _last_wall(part.master, default_tz)
    return last is None or last.date() >= day


def _day(part: Part, default_tz: dt.tzinfo) -> dt.date:
    """The day of a part's first occurrence, in its own zone."""
    return _local(_wall_of(part.master, default_tz), part.start).date()


def _utc(value: Moment, default_tz: dt.tzinfo) -> dt.datetime:
    if not isinstance(value, dt.datetime):
        value = dt.datetime.combine(value, dt.time())
    if value.tzinfo is None:
        value = value.replace(tzinfo=default_tz)
    return value.astimezone(dt.UTC)


def part_windows(
    named: Part, edit: SeriesEdit, default_tz: dt.tzinfo, today: dt.date
) -> tuple[list[tuple[dt.datetime, dt.datetime | None]], str | None]:
    """The time ranges to list to find the other parts of the named part's recurrence set,
    and how far back earlier parts were looked for (`_all` modes only)."""
    wall = _wall_of(named.master, default_tz)
    if edit.from_date:  # never looks before the date
        assert edit.effective is not None
        lo = dt.datetime.combine(edit.effective, dt.time())
        return [(_utc(wall.back(lo), wall.frame), None)], None
    windows: list[tuple[dt.datetime, dt.datetime | None]] = []
    last = _last_wall(named.master, default_tz)
    if last is not None:  # an endless part cannot be followed by another
        windows.append((_utc(wall.back(last), wall.frame), None))
    since = today - dt.timedelta(days=LOOKBACK_DAYS)
    lo_utc = _utc(since, default_tz)
    first_utc = _utc(named.start, wall.frame)
    if first_utc > lo_utc:
        windows.append((lo_utc, first_utc))
    return windows, since.isoformat()


# Exceptions: deleted (EXDATE, or a STATUS:CANCELLED override) and changed occurrences.


@dataclass
class _Exc:
    kind: str  # "deleted" or "changed"
    original: Moment  # EXDATE value or RECURRENCE-ID
    override: icalendar.cal.Component | None  # None: an EXDATE value


def _exceptions(cal: icalendar.Calendar, master: icalendar.cal.Component) -> list[_Exc]:
    out = [_Exc("deleted", d.dt, None) for prop in _props(master, "EXDATE") for d in prop.dts]
    for override in _overrides(cal, master):
        status = str(override.get("STATUS", "")).upper()
        kind = "deleted" if status == "CANCELLED" else "changed"
        out.append(_Exc(kind, _value(override, "RECURRENCE-ID"), override))
    return out


def _same(a: Moment, b: Moment) -> bool:
    """The same start as stored: value type, and instant (zoned) or wall-clock time."""
    if isinstance(a, dt.datetime) and isinstance(b, dt.datetime):
        return (a.tzinfo is None) == (b.tzinfo is None) and a == b
    return isinstance(a, dt.datetime) == isinstance(b, dt.datetime) and a == b


def _field(component: icalendar.cal.Component, name: str) -> Any:
    if name == "alerts":
        return sorted(c.to_ical() for c in component.subcomponents if c.name == "VALARM")
    [value, *_] = _props(component, _FIELD_PROPS[name]) or [None]
    return str(value) if value is not None else None


def _changed_fields(changes: Changes) -> list[str]:
    return [n for n in _FIELDS if getattr(changes, n) is not None or n in changes.clear]


def _inherit(
    override: icalendar.cal.Component,
    before: dict[str, Any],
    master: icalendar.cal.Component,
    fields: list[str],
) -> bool:
    """Give an override each changed field it still had from the series (Apple's rule: a
    value of its own is kept). True if it changed."""
    changed = False
    for name in fields:
        if _field(override, name) != before[name]:
            continue
        changed = True
        if name == "alerts":
            override.subcomponents = [
                *(c for c in override.subcomponents if c.name != "VALARM"),
                *(copy.deepcopy(c) for c in master.subcomponents if c.name == "VALARM"),
            ]
            continue
        prop = _FIELD_PROPS[name]
        override.pop(prop, None)
        if master.get(prop) is not None:
            override[prop] = copy.deepcopy(master[prop])
    return changed


def _out(start: Moment, end: Moment) -> tuple[str, str]:
    """Start and end as the read tools give them (an all-day end is the last day)."""
    if not isinstance(start, dt.datetime):
        last = end.date() if isinstance(end, dt.datetime) else end
        return start.isoformat(), max(start, last - dt.timedelta(days=1)).isoformat()
    return start.isoformat(), end.isoformat()


def _unmatched(exc: _Exc) -> dict[str, Any]:
    entry: dict[str, Any] = {"kind": exc.kind, "original_start": _iso(exc.original)}
    if exc.kind == "changed":
        assert exc.override is not None
        start = _value(exc.override, "DTSTART")
        entry["start"], entry["end"] = _out(start, start + _length(exc.override, start))
        entry["title"] = _field(exc.override, "title")
    return entry


def _cut_until(start: Moment, effective: dt.date) -> Moment:
    """Apple's UNTIL for a series cut before `effective`: the day before, 23:59:59 local
    time written in UTC (a date for an all-day series, local time for a floating one)."""
    last = effective - dt.timedelta(days=1)
    if not isinstance(start, dt.datetime):
        return last
    end = dt.datetime.combine(last, dt.time(23, 59, 59))
    if start.tzinfo is None:
        return end
    return end.replace(tzinfo=start.tzinfo).astimezone(dt.UTC)


def _fit_until(rule: Any, start: Moment, default_tz: dt.tzinfo) -> Any:
    """A kept rule's UNTIL in the value type of a new DTSTART (all-day or timed)."""
    if "UNTIL" not in rule:
        return rule
    until = rule["UNTIL"][0]
    if isinstance(start, dt.datetime):
        if not isinstance(until, dt.datetime):
            until = _cut_until(start, until + dt.timedelta(days=1))
        elif start.tzinfo is not None and until.tzinfo is None:
            until = until.replace(tzinfo=start.tzinfo).astimezone(dt.UTC)
        elif start.tzinfo is None and until.tzinfo is not None:
            until = until.astimezone(default_tz).replace(tzinfo=None)
    elif isinstance(until, dt.datetime):
        until = (until.astimezone(default_tz) if until.tzinfo else until).date()
    rule["UNTIL"] = [until]
    return rule


def _first_occurrence(rule: Any, start: Moment, default_tz: dt.tzinfo) -> Moment | None:
    """The first start a rule generates from `start` on: the new DTSTART, so that no
    occurrence outside the rule is added (RFC 5545 always counts DTSTART)."""
    wall = _Wall(start, default_tz)
    occ = next(_generator(rule, wall, _local(wall, start)), None)
    return wall.back(occ) if occ is not None else None


def _split_point(
    master: icalendar.cal.Component, default_tz: dt.tzinfo, day: dt.date
) -> tuple[dt.datetime | None, int]:
    """The first start on or after `day`, and how many the rule generated before it."""
    before = 0
    for occ in _iter_walls(master, default_tz):
        if occ.date() >= day:
            return occ, before
        before += 1
    return None, before


@dataclass
class _Edited:
    cal: icalendar.Calendar  # the resource holding the changed series
    master: icalendar.cal.Component  # the changed series' master
    kept: int = 0
    discarded: int = 0
    unmatched: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    unmatched_changed: int = 0


def _edit_part(
    part: Part,
    edit: SeriesEdit,
    default_tz: dt.tzinfo,
    now: dt.datetime,
    *,
    cut: dt.date | None = None,
    new_uid: str = "",
    set_value: str | None = None,
) -> _Edited:
    """Apply the edit to one part, in place (`cut` None), or split it at `cut`: the stored
    resource is cut there, and the returned resource is the new series from `cut` on."""
    cal, master = part.cal, part.master
    old_start = part.start
    old_len = _length(master, old_start)
    old_wall = _wall_of(master, default_tz)
    excs = _exceptions(cal, master)
    if cut is not None:
        excs = [e for e in excs if _local(old_wall, e.original).date() >= cut]
    fields = _changed_fields(edit.changes)
    before = {name: _field(master, name) for name in fields}

    if cut is not None:
        anchor_wall, generated_before = _split_point(master, default_tz, cut)
        assert anchor_wall is not None  # the caller checked there is an occurrence from cut
        anchor = old_wall.back(anchor_wall)
        target = copy.deepcopy(master)
        for name in ("ORGANIZER", "ATTENDEE", "EXDATE", "DURATION", "UID", "SEQUENCE"):
            target.pop(name, None)
        target.add("UID", new_uid)
        target.add("SEQUENCE", 0)
        for name in ("CREATED", "DTSTAMP", "LAST-MODIFIED"):
            _set(target, name, now)
        _set(target, "DTSTART", anchor)
        _set(target, "DTEND", anchor + old_len)
        rule = _rule(master)
        if "COUNT" in rule:
            rule["COUNT"] = [int(rule["COUNT"][0]) - generated_before]
        _set(target, "RRULE", rule)
    else:
        anchor = old_start
        target = master
        _touch(target, now)

    current = (anchor, anchor + old_len)
    fields_only = dataclasses.replace(
        edit.changes, start=None, end=None, all_day=None, timezone=None
    )
    _apply(target, fields_only, default_tz, current)
    if edit.pattern:
        if any(getattr(edit.changes, name) is not None for name in _PATTERN):
            start, end = _times(edit.changes, default_tz, current)
        else:
            start, end = current
        if edit.recurrence is not None:  # as parsed from text, so every value is a list
            made = icalendar.prop.vRecur(_rrule(edit.recurrence, start)).to_ical()
            rule = icalendar.prop.vRecur.from_ical(made.decode())
        else:
            rule = _fit_until(_rule(target), start, default_tz)
        first = _first_occurrence(rule, start, default_tz)
        if first is None:
            raise ToolInputError(NO_OCCURRENCE)
        length: dt.timedelta = end - start  # type: ignore[operator]  # same kind (_times)
        target.pop("DURATION", None)
        _set(target, "DTSTART", first)
        _set(target, "DTEND", first + length)
        _set(target, "RRULE", rule)
    new_start = _value(target, "DTSTART")
    new_len = _length(target, new_start)
    new_wall = _Wall(new_start, default_tz)
    if set_value is not None:
        _set_recurrence_set(target, set_value)
    value = recurrence_set(target)

    out = _Edited(cal, target)
    kept: list[icalendar.cal.Component] = []
    exdates: list[Moment] = []
    if edit.replace:
        out.discarded = len(excs)
    else:
        by_day: dict[dt.date, list[dt.datetime]] = {}
        if edit.pattern and excs:
            last_day = max(_local(old_wall, e.original).date() for e in excs)
            for occ in _walls(target, default_tz, before=last_day + dt.timedelta(days=1)):
                by_day.setdefault(occ.date(), []).append(occ)
        was_all_day = not isinstance(old_start, dt.datetime)
        all_day_changed = edit.changes.all_day is not None and edit.changes.all_day != was_all_day
        for exc in excs:
            key = exc.original
            if edit.pattern:
                hits = by_day.get(_local(old_wall, exc.original).date(), [])
                if len(hits) != 1 or (all_day_changed and exc.override is not None):
                    out.unmatched.append(_unmatched(exc))
                    out.unmatched_changed += exc.kind == "changed"
                    continue
                key = new_wall.back(hits[0])
            out.kept += 1
            if exc.override is None:
                exdates.append(key)
                continue
            override = copy.deepcopy(exc.override) if cut is not None else exc.override
            touched = cut is not None
            if cut is not None:
                _set(override, "UID", new_uid)
                for name in ("ORGANIZER", "ATTENDEE"):
                    override.pop(name, None)
            rekeyed = not _same(key, exc.original)
            if rekeyed:
                own_start = _value(override, "DTSTART")
                unmoved = old_wall(own_start) == old_wall(exc.original) and (
                    _length(override, own_start) == old_len
                )
                _set(override, "RECURRENCE-ID", key)
                if unmoved:
                    override.pop("DURATION", None)
                    _set(override, "DTSTART", key)
                    _set(override, "DTEND", key + new_len)
                touched = True
            if rekeyed or cut is not None:  # created or re-keyed: a copy of the master's
                _set_recurrence_set(override, value)
            if exc.kind == "changed" and _inherit(override, before, target, fields):
                touched = True
            if touched:
                _touch(override, now)
            kept.append(override)

    if cut is not None:
        _cut(part, cut, set_value, now)
        target.pop("EXDATE", None)
        for day in exdates:
            target.add("EXDATE", day)
        new = icalendar.Calendar()
        new.add("VERSION", "2.0")
        new.add("PRODID", PRODID)
        for tz in cal.walk("VTIMEZONE"):
            new.add_component(copy.deepcopy(tz))
        new.add_component(target)
        for override in kept:
            new.add_component(override)
        new.add_missing_timezones()
        out.cal = new
        return out
    dropped = [e.override for e in excs if e.override is not None]
    cal.subcomponents = [
        c
        for c in cal.subcomponents
        if any(c is k for k in kept) or not any(c is d for d in dropped)
    ]
    if edit.replace or edit.pattern:
        target.pop("EXDATE", None)
        for day in exdates:
            target.add("EXDATE", day)
    cal.add_missing_timezones()
    return out


def _cut(part: Part, cut: dt.date, set_value: str | None, now: dt.datetime) -> None:
    """End the stored series before `cut`, keeping everything earlier as stored."""
    master = part.master
    wall = _wall_of(master, dt.UTC)
    rule = _rule(master)
    rule.pop("COUNT", None)
    rule["UNTIL"] = [_cut_until(part.start, cut)]
    _set(master, "RRULE", rule)

    def later(value: Moment) -> bool:
        return _local(wall, value).date() >= cut

    props = _props(master, "EXDATE")
    if any(later(d.dt) for prop in props for d in prop.dts):
        master.pop("EXDATE", None)
        for prop in props:
            if not any(later(d.dt) for d in prop.dts):
                master.add("EXDATE", prop)  # untouched: kept as stored
                continue
            for d in prop.dts:
                if not later(d.dt):
                    master.add("EXDATE", d.dt)
    gone = [o for o in _overrides(part.cal, master) if later(_value(o, "RECURRENCE-ID"))]
    part.cal.subcomponents = [c for c in part.cal.subcomponents if not any(c is o for o in gone)]
    if recurrence_set(master) is None:
        _set_recurrence_set(master, set_value)
    _touch(master, now)


def _first_instance(
    cal: icalendar.Calendar, master: icalendar.cal.Component, default_tz: dt.tzinfo
) -> icalendar.cal.Component:
    """The series' first occurrence that is not deleted: its override, or a copy of the
    master at that start (occurrence_text then formats it like a read)."""
    wall = _wall_of(master, default_tz)
    excs = _exceptions(cal, master)
    deleted = {wall(e.original) for e in excs if e.kind == "deleted"}
    changed = {wall(e.original): e.override for e in excs if e.kind == "changed"}
    for occ in _iter_walls(master, default_tz):
        if occ in deleted:
            continue
        override = changed.get(occ)
        if override is not None:
            return override
        inst = copy.deepcopy(master)
        start = wall.back(occ)
        inst.pop("DURATION", None)
        _set(inst, "DTSTART", start)
        _set(inst, "DTEND", start + _length(master, _value(master, "DTSTART")))
        return inst
    return master


def _count(
    cal: icalendar.Calendar,
    master: icalendar.cal.Component,
    default_tz: dt.tzinfo,
    window_end: dt.date,
) -> tuple[int, bool]:
    """Occurrences of the series that are not deleted, through the day before `window_end`
    for an endless one; and whether counting stopped there."""
    wall = _wall_of(master, default_tz)
    deleted = {wall(e.original) for e in _exceptions(cal, master) if e.kind == "deleted"}
    endless = _endless(master)
    walls = _walls(master, default_tz, before=window_end if endless else None)
    return sum(1 for occ in walls if occ not in deleted), endless


def _previous(part: Part, cut: dt.date, default_tz: dt.tzinfo) -> dict[str, Any]:
    """The cut series' uid and the start of its last occurrence before the cut."""
    wall = _wall_of(part.master, default_tz)
    excs = _exceptions(part.cal, part.master)
    deleted = {wall(e.original) for e in excs if e.kind == "deleted"}
    changed = {wall(e.original): e.override for e in excs if e.kind == "changed"}
    last: Moment | None = None
    for occ in _walls(part.master, default_tz, before=cut):
        if occ in deleted:
            continue
        override = changed.get(occ)
        last = _value(override, "DTSTART") if override is not None else wall.back(occ)
    return {"uid": part.uid, "last_start": _iso(last)}


@dataclass
class PartWrite:
    """One write of a series edit: a new resource (`etag` None) or an update."""

    path: str
    text: str
    etag: str | None
    uid: str
    first: str  # start of the part's first occurrence, to name it in errors


@dataclass
class SeriesPlan:
    writes: list[PartWrite]  # in order; a split's new resource first
    first: icalendar.cal.Component  # first changed occurrence
    first_path: str
    split: bool
    found: int
    changed: int
    occurrences: int
    occurrences_through: str | None
    exceptions: dict[str, int]
    unmatched: list[dict[str, Any]]
    requires_drop_unmatched: bool
    previous: dict[str, Any] | None


def plan_series_edit(
    named: Part,
    others: list[Part],
    edit: SeriesEdit,
    default_tz: dt.tzinfo,
    now: dt.datetime,
    *,
    new_uid: str,
    new_value: str,
    new_path: str,
) -> SeriesPlan:
    """Everything a series edit writes, worked out before any write. `others` are the other
    parts of the named part's recurrence set. A split uses `new_uid` and `new_path` for the
    new series, and `new_value` as the recurrence set when the part has none yet."""
    parts = sorted([named, *others], key=lambda p: _utc(p.start, default_tz))
    if len(parts) > PARTS_MAX:
        raise ToolInputError(TOO_MANY_PARTS)
    for part in parts:
        check_series(part.cal, part.master)
    split_at: dt.date | None = None
    if edit.from_date:
        assert edit.effective is not None
        changing = [p for p in parts if _has_from(p, edit.effective, default_tz)]
        if not changing:
            raise ToolInputError(
                f"the series has no occurrence on or after {edit.effective.isoformat()}"
            )
        if len(changing) > 1 and (edit.replace or edit.pattern):
            raise ToolInputError(
                f"this series was split on {_day(changing[1], default_tz).isoformat()}; "
                "change its pattern from that date or later"
            )
        if edit.effective > _day(changing[0], default_tz):
            split_at = edit.effective
    else:
        if len(parts) > 1 and (edit.replace or edit.pattern):
            raise ToolInputError(SPLIT_SERIES)
        changing = parts

    writes: list[PartWrite] = []
    edited: list[_Edited] = []
    previous = None
    first_path = changing[0].path
    for i, part in enumerate(changing):
        part_first = _iso(part.start) or ""
        if i == 0 and split_at is not None:
            previous = _previous(part, split_at, default_tz)
            value = recurrence_set(part.master) or new_value
            result = _edit_part(
                part, edit, default_tz, now, cut=split_at, new_uid=new_uid, set_value=value
            )
            new_first = _iso(_value(result.master, "DTSTART")) or ""
            writes.append(PartWrite(new_path, to_text(result.cal), None, new_uid, new_first))
            first_path = new_path
        else:
            result = _edit_part(part, edit, default_tz, now)
        writes.append(PartWrite(part.path, to_text(part.cal), part.etag, part.uid, part_first))
        edited.append(result)

    first = _first_instance(edited[0].cal, edited[0].master, default_tz)
    first_day = _local(_wall_of(first, default_tz), _value(first, "DTSTART")).date()
    window_end = first_day + dt.timedelta(days=COUNT_DAYS)
    counted = [_count(e.cal, e.master, default_tz, window_end) for e in edited]
    unmatched = [entry for e in edited for entry in e.unmatched]
    unmatched_changed = sum(e.unmatched_changed for e in edited)
    needs_drop = unmatched_changed > 0 and not edit.drop_unmatched
    if needs_drop and not edit.preview:
        raise ToolInputError(
            f"{unmatched_changed} changed occurrences no longer fit the new pattern; preview to "
            "see them, or pass drop_unmatched: true to drop them"
        )
    stopped = any(endless for _, endless in counted)
    return SeriesPlan(
        writes=writes,
        first=first,
        first_path=first_path,
        split=split_at is not None,
        found=len(parts),
        changed=len(changing),
        occurrences=sum(n for n, _ in counted),
        occurrences_through=(window_end - dt.timedelta(days=1)).isoformat() if stopped else None,
        exceptions={
            "kept": sum(e.kept for e in edited),
            "discarded": sum(e.discarded for e in edited),
            "unmatched": len(unmatched),
        },
        unmatched=unmatched,
        requires_drop_unmatched=needs_drop,
        previous=previous,
    )
