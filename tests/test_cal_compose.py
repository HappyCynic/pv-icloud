# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

"""Building and editing iCalendar data for the calendar write tools (cal/compose.py)."""

from __future__ import annotations

import datetime as dt
import zoneinfo

import icalendar
import pytest

from pv_icloud.cal import compose
from pv_icloud.cal.caldav import AccountAddresses
from pv_icloud.cal.compose import Changes
from pv_icloud.errors import ToolInputError

from .fakecaldav import resource, vevent

CHI = zoneinfo.ZoneInfo("America/Chicago")
LON = zoneinfo.ZoneInfo("Europe/London")
NOW = dt.datetime(2026, 10, 14, 12, tzinfo=dt.UTC)
ME = AccountAddresses(frozenset({"me@example.net"}), frozenset({"/111/principal/"}))


def create(**kw) -> icalendar.Event:
    recurrence = kw.pop("recurrence", None)
    cal = compose.new_event(
        Changes(**{"title": "Dentist", "start": "2026-11-03T09:00", **kw}),
        compose.parse_recurrence(recurrence),
        CHI,
        "uid-1",
        NOW,
    )
    # As iCloud will read it.
    [ev] = icalendar.Calendar.from_ical(compose.to_text(cal)).walk("VEVENT")
    return ev


def parsed(text: str) -> icalendar.Calendar:
    return icalendar.Calendar.from_ical(text)


def vevents(cal: icalendar.Calendar) -> list[icalendar.cal.Component]:
    return [c for c in cal.subcomponents if c.name == "VEVENT"]


class TestCreate:
    def test_timed_event_in_a_zone(self):
        cal = compose.new_event(
            Changes(title="Dentist", start="2026-11-03T09:00", timezone="Europe/London"),
            None,
            CHI,
            "uid-1",
            NOW,
        )
        text = compose.to_text(cal)
        assert "DTSTART;TZID=Europe/London:20261103T090000" in text
        assert "DTEND;TZID=Europe/London:20261103T100000" in text  # default: one hour
        assert "BEGIN:VTIMEZONE" in text and "TZID:Europe/London" in text
        [ev] = vevents(parsed(text))
        assert (str(ev["UID"]), str(ev["SUMMARY"]), int(ev["SEQUENCE"])) == ("uid-1", "Dentist", 0)
        assert ev["DTSTAMP"].dt == NOW and ev["CREATED"].dt == NOW
        assert ev["LAST-MODIFIED"].dt == NOW
        assert "PRODID:-//pv-icloud//EN" in text
        assert "ORGANIZER" not in text and "ATTENDEE" not in text

    def test_offset_is_converted_into_the_zone(self):
        ev = create(
            start="2026-11-03T09:00+01:00", end="2026-11-03T10:30+01:00", timezone="Europe/London"
        )
        assert ev["DTSTART"].dt == dt.datetime(2026, 11, 3, 8, tzinfo=LON)
        assert ev["DTEND"].dt == dt.datetime(2026, 11, 3, 9, 30, tzinfo=LON)

    def test_default_zone(self):
        ev = create()
        assert ev["DTSTART"].dt == dt.datetime(2026, 11, 3, 9, tzinfo=CHI)
        assert ev["DTSTART"].dt.tzinfo.key == "America/Chicago"

    def test_all_day_end_is_inclusive(self):
        text = compose.to_text(
            compose.new_event(
                Changes(title="Holiday", start="2026-12-24", end="2026-12-26", all_day=True),
                None,
                CHI,
                "u",
                NOW,
            )  # fmt: skip
        )
        assert "DTSTART;VALUE=DATE:20261224" in text and "DTEND;VALUE=DATE:20261227" in text
        ev = create(start="2026-12-24", all_day=True)
        assert ev["DTEND"].dt == dt.date(2026, 12, 25)  # default: that one day

    def test_recurrence(self):
        ev = create(recurrence={"frequency": "weekly", "weekdays": ["MO", "WE"], "count": 10})
        assert dict(ev["RRULE"]) == {"FREQ": ["WEEKLY"], "BYDAY": ["MO", "WE"], "COUNT": [10]}
        ev = create(recurrence={"frequency": "monthly", "interval": 2, "until": "2027-06-30"})
        rule = ev["RRULE"]
        assert rule["FREQ"] == ["MONTHLY"] and rule["INTERVAL"] == [2]
        # UNTIL of a timed series is UTC: the end of that day in the event's zone.
        assert rule["UNTIL"] == [
            dt.datetime(2027, 6, 30, 23, 59, 59, tzinfo=CHI).astimezone(dt.UTC)
        ]
        ev = create(
            start="2026-12-24",
            all_day=True,
            recurrence={"frequency": "yearly", "until": "2030-12-24"},
        )
        assert ev["RRULE"]["UNTIL"] == [dt.date(2030, 12, 24)]

    def test_alerts_are_display_only(self):
        ev = create(alerts=(15, 0))
        alarms = [c for c in ev.subcomponents if c.name == "VALARM"]
        assert [str(a["ACTION"]) for a in alarms] == ["DISPLAY", "DISPLAY"]
        assert [a["TRIGGER"].dt for a in alarms] == [-dt.timedelta(minutes=15), dt.timedelta(0)]
        assert all("DESCRIPTION" in a for a in alarms)

    def test_location_and_notes(self):
        ev = create(location="Main St, 1", notes="Bring forms\nand card")
        assert (
            str(ev["LOCATION"]) == "Main St, 1"
            and str(ev["DESCRIPTION"]) == "Bring forms\nand card"
        )


class TestLimits:
    @pytest.mark.parametrize(
        ("kw", "field"),
        [
            ({"title": " "}, "title"),
            ({"title": "x" * 256}, "title"),
            ({"title": "a\nb"}, "title"),
            ({"location": "x" * 1001}, "location"),
            ({"notes": "x" * 10_001}, "notes"),
            ({"alerts": (1, 2, 3, 4, 5, 6)}, "alerts"),
            ({"alerts": (5, 5)}, "alerts"),
            ({"alerts": (40_321,)}, "alerts"),
            ({"alerts": (-1,)}, "alerts"),
            ({"timezone": "Mars/Olympus"}, "timezone"),
            ({"start": "next tuesday"}, "start"),
            ({"start": "2026-11-03"}, "start"),  # a date for a timed event
            ({"start": "2026-11-03T09:00", "all_day": True}, "start"),
            ({"end": "2026-11-03T09:00"}, "end"),  # not after start
            ({"end": "2026-11-03T08:00"}, "end"),
            ({"start": "2026-11-03", "end": "2026-11-02", "all_day": True}, "end"),
            ({"end": "2027-11-05T09:00"}, "366 days"),
            ({"start": "2026-11-03", "end": "2027-11-04", "all_day": True}, "366 days"),
        ],
    )
    def test_refused_with_the_field_named(self, kw, field):
        with pytest.raises(ToolInputError, match=field):
            create(**kw)

    @pytest.mark.parametrize(
        "recurrence",
        [
            {"frequency": "hourly"},
            {"frequency": "daily", "interval": 0},
            {"frequency": "daily", "interval": 100},
            {"frequency": "weekly", "weekdays": ["XX"]},
            {"frequency": "daily", "weekdays": ["MO"]},
            {"frequency": "daily", "count": 0},
            {"frequency": "daily", "count": 731},
            {"frequency": "daily", "count": 5, "until": "2027-01-01"},
            {"frequency": "daily", "until": "soon"},
            {"frequency": "daily", "until": "2026-11-01"},  # before the start
            {"frequency": "daily", "byhour": [9]},
            {},
        ],
    )
    def test_recurrence_refused(self, recurrence):
        with pytest.raises(ToolInputError, match="recurrence"):
            create(recurrence=recurrence)

    def test_given_and_cleared(self):
        with pytest.raises(ToolInputError, match="notes"):
            compose.check_changes(Changes(notes="x", clear=frozenset({"notes"})), creating=False)
        with pytest.raises(ToolInputError, match="clear"):
            compose.check_changes(Changes(clear=frozenset({"title"})), creating=False)


SINGLE = resource(
    vevent(
        "single",
        "Dentist",
        dt.datetime(2026, 11, 3, 9),
        dt.datetime(2026, 11, 3, 10),
        extra=(
            "LOCATION:Main St",
            "DESCRIPTION:Bring forms",
            "SEQUENCE:2",
            "X-APPLE-TRAVEL-ADVISORY-BEHAVIOR:AUTOMATIC",
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            "DESCRIPTION:Reminder",
            "TRIGGER:-PT30M",
            "X-WR-ALARMUID:alarm-1",
            "END:VALARM",
        ),
    ),
    "BEGIN:X-APPLE-THING\r\nX-A:1\r\nEND:X-APPLE-THING",
)
SERIES = resource(
    vevent(
        "series",
        "Standup",
        dt.datetime(2026, 11, 2, 9),
        dt.datetime(2026, 11, 2, 9, 15),
        extra=("RRULE:FREQ=WEEKLY;COUNT=6", "SEQUENCE:1"),
    ),
    vevent(
        "series",
        "Standup (moved)",
        dt.datetime(2026, 11, 16, 11),
        dt.datetime(2026, 11, 16, 11, 15),
        extra=("RECURRENCE-ID;TZID=America/Chicago:20261116T090000",),
    ),
)


def iso(*args) -> str:
    return dt.datetime(*args, tzinfo=CHI).isoformat()


def props(component) -> dict:
    return {k: v.to_ical() if hasattr(v, "to_ical") else v for k, v in component.items()}


class TestFindTarget:
    def test_single_event(self):
        cal = parsed(SINGLE)
        target = compose.find_target(cal, iso(2026, 11, 3, 9), CHI)
        assert target is not None and target.override is None and target.original is None
        assert compose.find_target(cal, iso(2026, 11, 3, 10), CHI) is None

    def test_occurrences(self):
        cal = parsed(SERIES)
        second = compose.find_target(cal, iso(2026, 11, 9, 9), CHI)
        assert second is not None and second.override is None
        assert second.original == dt.datetime(2026, 11, 9, 9, tzinfo=CHI)
        moved = compose.find_target(cal, iso(2026, 11, 16, 11), CHI)
        assert moved is not None and moved.override is not None
        assert moved.original == dt.datetime(2026, 11, 16, 9, tzinfo=CHI)
        # The original start of a moved occurrence is no longer an occurrence; nor is a
        # time outside the rule.
        assert compose.find_target(cal, iso(2026, 11, 16, 9), CHI) is None
        assert compose.find_target(cal, iso(2026, 11, 10, 9), CHI) is None
        assert compose.find_target(cal, "garbage", CHI) is None

    def test_excluded_occurrence_is_gone(self):
        cal = parsed(
            SERIES.replace(
                "SEQUENCE:1", "SEQUENCE:1\r\nEXDATE;TZID=America/Chicago:20261109T090000"
            )
        )
        assert compose.find_target(cal, iso(2026, 11, 9, 9), CHI) is None


class TestUpdate:
    def test_one_field_keeps_everything_else(self):
        cal = parsed(SINGLE)
        before = vevents(parsed(SINGLE))[0]
        target = compose.find_target(cal, iso(2026, 11, 3, 9), CHI)
        compose.apply_update(cal, target, Changes(location="Elm St"), CHI, NOW)
        after = vevents(parsed(compose.to_text(cal)))[0]
        changed = {
            k
            for k in set(props(before)) | set(props(after))
            if props(before).get(k) != props(after).get(k)
        }
        assert changed == {"LOCATION", "SEQUENCE", "LAST-MODIFIED", "DTSTAMP"}
        assert int(after["SEQUENCE"]) == 3
        assert [a["X-WR-ALARMUID"] for a in after.subcomponents] == ["alarm-1"]
        assert "BEGIN:X-APPLE-THING" in compose.to_text(cal)

    def test_times_title_alerts_and_clear(self):
        cal = parsed(SINGLE)
        target = compose.find_target(cal, iso(2026, 11, 3, 9), CHI)
        compose.apply_update(
            cal,
            target,
            Changes(
                title="Dentist (new)",
                start="2026-11-04T14:00",
                alerts=(10,),
                clear=frozenset({"notes"}),
            ),
            CHI,
            NOW,
        )
        [ev] = vevents(cal)
        assert str(ev["SUMMARY"]) == "Dentist (new)"
        # Moving the start keeps the length.
        assert (ev["DTSTART"].dt, ev["DTEND"].dt) == (
            dt.datetime(2026, 11, 4, 14, tzinfo=CHI), dt.datetime(2026, 11, 4, 15, tzinfo=CHI),
        )  # fmt: skip
        assert "DESCRIPTION" not in ev
        assert [a["TRIGGER"].dt for a in ev.subcomponents] == [-dt.timedelta(minutes=10)]

    def test_change_zone_keeps_wall_clock_and_adds_vtimezone(self):
        cal = parsed(SINGLE)
        target = compose.find_target(cal, iso(2026, 11, 3, 9), CHI)
        compose.apply_update(cal, target, Changes(timezone="Europe/London"), CHI, NOW)
        [ev] = vevents(cal)
        assert ev["DTSTART"].dt == dt.datetime(2026, 11, 3, 9, tzinfo=LON)
        assert "TZID:Europe/London" in compose.to_text(cal)

    def test_to_all_day_and_back(self):
        cal = parsed(SINGLE)
        target = compose.find_target(cal, iso(2026, 11, 3, 9), CHI)
        compose.apply_update(cal, target, Changes(all_day=True), CHI, NOW)
        [ev] = vevents(cal)
        assert (ev["DTSTART"].dt, ev["DTEND"].dt) == (dt.date(2026, 11, 3), dt.date(2026, 11, 4))
        target = compose.find_target(cal, "2026-11-03", CHI)
        with pytest.raises(ToolInputError, match="start"):
            compose.apply_update(cal, target, Changes(all_day=False), CHI, NOW)

    def test_new_override_for_one_occurrence(self):
        cal = parsed(SERIES)
        target = compose.find_target(cal, iso(2026, 11, 9, 9), CHI)
        changed = compose.apply_update(cal, target, Changes(start="2026-11-09T10:00"), CHI, NOW)
        master, *overrides = vevents(cal)
        assert "RRULE" in master and int(master["SEQUENCE"]) == 1  # the series is unchanged
        new = next(o for o in overrides if o is changed)
        assert new["RECURRENCE-ID"].dt == dt.datetime(2026, 11, 9, 9, tzinfo=CHI)
        assert (new["DTSTART"].dt, new["DTEND"].dt) == (
            dt.datetime(2026, 11, 9, 10, tzinfo=CHI), dt.datetime(2026, 11, 9, 10, 15, tzinfo=CHI),
        )  # fmt: skip
        assert "RRULE" not in new and "EXDATE" not in new and str(new["SUMMARY"]) == "Standup"
        assert int(new["SEQUENCE"]) == 2
        assert "RECURRENCE-ID;TZID=America/Chicago:20261109T090000" in compose.to_text(cal)

    def test_existing_override_is_edited(self):
        cal = parsed(SERIES)
        target = compose.find_target(cal, iso(2026, 11, 16, 11), CHI)
        compose.apply_update(cal, target, Changes(title="Moved again"), CHI, NOW)
        assert [str(e["SUMMARY"]) for e in vevents(cal)] == ["Standup", "Moved again"]

    def test_all_day_series_override_uses_a_date(self):
        cal = parsed(
            resource(
                vevent(
                    "s",
                    "Bins",
                    dt.date(2026, 11, 2),
                    dt.date(2026, 11, 3),
                    extra=("RRULE:FREQ=WEEKLY",),
                )
            )
        )
        target = compose.find_target(cal, "2026-11-09", CHI)
        compose.apply_update(cal, target, Changes(title="Bins (late)"), CHI, NOW)
        assert "RECURRENCE-ID;VALUE=DATE:20261109" in compose.to_text(cal)


class TestDelete:
    def test_single_event_deletes_the_resource(self):
        cal = parsed(SINGLE)
        target = compose.find_target(cal, iso(2026, 11, 3, 9), CHI)
        assert compose.apply_delete(cal, target, NOW) is True

    def test_occurrence_becomes_an_exdate(self):
        cal = parsed(SERIES)
        target = compose.find_target(cal, iso(2026, 11, 9, 9), CHI)
        assert compose.apply_delete(cal, target, NOW) is False
        text = compose.to_text(cal)
        assert "EXDATE;TZID=America/Chicago:20261109T090000" in text
        assert int(vevents(cal)[0]["SEQUENCE"]) == 2
        assert compose.find_target(cal, iso(2026, 11, 9, 9), CHI) is None
        assert compose.find_target(cal, iso(2026, 11, 2, 9), CHI) is not None

    def test_overridden_occurrence_loses_its_override(self):
        cal = parsed(SERIES)
        target = compose.find_target(cal, iso(2026, 11, 16, 11), CHI)
        assert compose.apply_delete(cal, target, NOW) is False
        assert len(vevents(cal)) == 1
        assert "EXDATE;TZID=America/Chicago:20261116T090000" in compose.to_text(cal)

    def test_utc_and_date_series_keep_their_value_type(self):
        cal = parsed(
            resource(
                vevent(
                    "s",
                    "Bins",
                    dt.date(2026, 11, 2),
                    dt.date(2026, 11, 3),
                    extra=("RRULE:FREQ=WEEKLY",),
                )
            )
        )
        compose.apply_delete(cal, compose.find_target(cal, "2026-11-09", CHI), NOW)
        assert "EXDATE;VALUE=DATE:20261109" in compose.to_text(cal)
        utc = parsed(
            resource(
                vevent(
                    "s", "Sync", dt.datetime(2026, 11, 2, 15), tz="UTC", extra=("RRULE:FREQ=DAILY",)
                )
            )
        )
        start = dt.datetime(2026, 11, 3, 15, tzinfo=dt.UTC).isoformat()
        compose.apply_delete(utc, compose.find_target(utc, start, CHI), NOW)
        assert "EXDATE:20261103T150000Z" in compose.to_text(utc)

    def test_lone_override(self):
        lone = vevent(
            "x",
            "Invite",
            dt.datetime(2026, 11, 9, 9),
            extra=("RECURRENCE-ID;TZID=America/Chicago:20261109T090000",),
        )
        cal = parsed(resource(lone))
        assert (
            compose.apply_delete(cal, compose.find_target(cal, iso(2026, 11, 9, 9), CHI), NOW)
            is True
        )
        cal = parsed(resource(lone, lone.replace("20261109T09", "20261116T09")))
        assert (
            compose.apply_delete(cal, compose.find_target(cal, iso(2026, 11, 9, 9), CHI), NOW)
            is False
        )
        assert len(vevents(cal)) == 1


class TestPeople:
    @pytest.mark.parametrize(
        "line",
        [
            "ORGANIZER:mailto:me@example.net",
            "ORGANIZER:mailto:ME@Example.NET",
            "ORGANIZER;EMAIL=me@example.net:/111/principal/",
            "ORGANIZER;EMAIL=Me@Example.net:https://p99-caldav.icloud.com:443/111/principal/",
            "ORGANIZER:/111/principal/",
            "ATTENDEE;PARTSTAT=ACCEPTED;EMAIL=me@example.net:/111/principal/",
        ],
    )
    def test_the_account_itself_is_allowed(self, line):
        compose.check_people(
            parsed(resource(vevent("a", "A", dt.datetime(2026, 11, 3, 9), extra=(line,)))), ME
        )

    @pytest.mark.parametrize(
        ("line", "message"),
        [
            ("ORGANIZER:mailto:someone@example.org", "organised by someone else"),
            ("ORGANIZER;EMAIL=someone@example.org:/222/principal/", "organised by someone else"),
            ("ATTENDEE:mailto:carol@example.com", "event has attendees"),
            ("ATTENDEE:/222/principal/", "event has attendees"),
            ("ATTENDEE:urn:uuid:1234", "event has attendees"),
        ],
    )
    def test_anyone_else_is_refused(self, line, message):
        cal = parsed(resource(vevent("a", "A", dt.datetime(2026, 11, 3, 9), extra=(line,))))
        with pytest.raises(ToolInputError, match=f"{message}; change it in Calendar"):
            compose.check_people(cal, ME)

    def test_any_component_of_the_series_counts(self):
        cal = parsed(
            SERIES.replace("SEQUENCE:1", "SEQUENCE:1\r\nATTENDEE:mailto:carol@example.com")
        )
        with pytest.raises(ToolInputError, match="event has attendees"):
            compose.check_people(cal, ME)
