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


# --- series edits (ADR-0007) ----------------------------------------------------------------

MON = dt.datetime(2027, 1, 4, 9)  # a Monday
DAY = dt.timedelta(days=1)
WEEK = dt.timedelta(weeks=1)
HOUR = dt.timedelta(hours=1)
PATH = "/111/calendars/home/0.ics"
NEW_PATH = "/111/calendars/home/NEW-UID.ics"


def stamp(value: dt.datetime) -> str:
    return f"{value:%Y%m%dT%H%M%S}"


def rid(value: dt.datetime) -> str:
    return f"RECURRENCE-ID;TZID=America/Chicago:{stamp(value)}"


def exdate(*values: dt.datetime) -> str:
    return "EXDATE;TZID=America/Chicago:" + ",".join(stamp(v) for v in values)


def master(rule: str = "FREQ=WEEKLY;COUNT=10", *extra: str, start=MON, uid="s", title="Class"):
    end = start + (HOUR if isinstance(start, dt.datetime) else DAY)
    tz = "America/Chicago" if isinstance(start, dt.datetime) else None
    return vevent(uid, title, start, end, tz=tz, extra=(f"RRULE:{rule}", *extra))


def override(original: dt.datetime, *extra: str, start=None, title="Class", uid="s"):
    start = start or original
    return vevent(uid, title, start, start + HOUR, extra=(rid(original), *extra))


def at(value: dt.datetime) -> str:
    return value.replace(tzinfo=CHI).isoformat()


def plan(
    text: str,
    mode: str,
    effective: str | None = None,
    *,
    others=(),
    occurrence: str | None = None,
    recurrence=None,
    drop: bool = False,
    preview: bool = False,
    **changes,
):
    cal = parsed(text)
    target = compose.find_target(cal, occurrence or at(MON), CHI)
    assert target is not None
    named = compose.Part(PATH, '"e0"', cal, compose.series_master(cal, target))
    edit = compose.series_edit(mode, effective, Changes(**changes), recurrence, drop, preview)
    out = compose.plan_series_edit(
        named, list(others), edit, CHI, NOW,
        new_uid="NEW-UID", new_value="SET-NEW", new_path=NEW_PATH,
    )  # fmt: skip
    return out, cal


def written(out, path=PATH) -> icalendar.Calendar:
    [w] = [w for w in out.writes if w.path == path]
    return parsed(w.text)


def starts(*texts: str, weeks: int = 80) -> list[tuple[dt.datetime | dt.date, str]]:
    from pv_icloud.cal.expand import expand

    lo = dt.datetime(2026, 9, 1, tzinfo=CHI)
    out = []
    for text in texts:
        for occ in expand(parsed(text), lo, lo + weeks * WEEK, CHI):
            start = (
                occ.start.replace(tzinfo=None) if isinstance(occ.start, dt.datetime) else occ.start
            )
            out.append((start, str(occ.component.get("SUMMARY"))))
    return sorted(out)


def texts(out) -> list[str]:
    return [w.text for w in out.writes]


def series_master_of(cal: icalendar.Calendar, uid: str | None = None):
    return next(
        c
        for c in vevents(cal)
        if c.get("RECURRENCE-ID") is None and (uid is None or str(c["UID"]) == uid)
    )


class TestSeriesInput:
    @pytest.mark.parametrize(
        ("args", "field"),
        [
            (("this_and_following", None), "mode"),
            (("replace_from", None), "effective"),
            (("update_from", "next week"), "effective"),
            (("update_from", "2027-01-11T09:00"), "effective"),
            (("update_all", "2027-01-11"), "effective"),
        ],
    )
    def test_mode_and_effective(self, args, field):
        with pytest.raises(ToolInputError, match=field):
            compose.series_edit(*args, Changes(title="x"), None)

    @pytest.mark.parametrize("name", ["drop_unmatched", "preview"])
    def test_booleans(self, name):
        flags = {"drop_unmatched": False, "preview": False, name: "yes"}
        with pytest.raises(ToolInputError, match=name):
            compose.series_edit("update_all", None, Changes(title="x"), None, **flags)

    def test_nothing_to_change_and_reset(self):
        with pytest.raises(ToolInputError, match="nothing to change"):
            compose.series_edit("update_all", None, Changes(), None)
        with pytest.raises(ToolInputError, match="nothing to change"):
            compose.series_edit("update_from", "2027-01-11", Changes(), None)
        assert compose.series_edit("replace_all", None, Changes(), None).replace
        assert compose.series_edit("update_all", None, Changes(), {"frequency": "daily"}).pattern

    def test_start_before_effective(self):
        with pytest.raises(ToolInputError, match="start"):
            compose.series_edit(
                "update_from", "2027-01-11", Changes(start="2027-01-04T09:00"), None
            )

    def test_field_limits_and_recurrence(self):
        with pytest.raises(ToolInputError, match="title"):
            compose.series_edit("update_all", None, Changes(title="x" * 256), None)
        with pytest.raises(ToolInputError, match="recurrence"):
            compose.series_edit("update_all", None, Changes(), {"frequency": "hourly"})

    def test_pattern_and_field_edits(self):
        def edit(**kw):
            return compose.series_edit("update_all", None, Changes(**kw), None)

        assert not edit(title="x", location="y", notes="z", alerts=(5,)).pattern
        assert not edit(clear=frozenset({"notes"})).pattern
        for kw in ({"start": "2027-01-04T10:00"}, {"end": "2027-01-04T11:00"},
                   {"all_day": True}, {"timezone": "Europe/London"}):  # fmt: skip
            assert edit(**kw).pattern, kw


class TestSeriesShape:
    def test_any_occurrence_names_the_series(self):
        text = resource(master(), override(MON + 2 * WEEK, start=MON + 2 * WEEK + HOUR))
        for occurrence in (at(MON), at(MON + WEEK), at(MON + 2 * WEEK + HOUR)):
            out, _ = plan(text, "update_all", occurrence=occurrence, title="New")
            assert [w.path for w in out.writes] == [PATH]

    def test_single_event(self):
        with pytest.raises(
            ToolInputError, match="^not a recurring series; use calendar_event_update$"
        ):
            plan(resource(vevent("one", "One", MON, MON + HOUR)), "update_all", title="x")

    @pytest.mark.parametrize(
        "text",
        [
            resource(
                master("FREQ=WEEKLY;COUNT=10", f"RDATE;TZID=America/Chicago:{stamp(MON + DAY)}")
            ),
            resource(master("FREQ=WEEKLY;COUNT=10", "EXRULE:FREQ=MONTHLY")),
            resource(master("FREQ=WEEKLY;COUNT=10", "RRULE:FREQ=DAILY;COUNT=3")),
            resource(
                master(),
                override(MON + WEEK).replace(
                    "RECURRENCE-ID;", "RECURRENCE-ID;RANGE=THISANDFUTURE;"
                ),
            ),
            resource(override(MON + WEEK)),  # a lone override, no master
        ],
    )
    def test_refused_shapes(self, text):
        with pytest.raises(
            ToolInputError, match="^this series cannot be changed here; change it in Calendar$"
        ):
            plan(text, "update_all", occurrence=at(MON + WEEK), title="x")


class TestSeriesDefinition:
    def test_fields_kept_and_touched(self):
        text = resource(
            master("FREQ=WEEKLY;COUNT=10", "LOCATION:Room 1", "SEQUENCE:3", "X-APPLE-X:keep")
        )
        out, _ = plan(text, "update_all", title="New")
        [ev] = vevents(written(out))
        assert (str(ev["SUMMARY"]), str(ev["LOCATION"]), str(ev["X-APPLE-X"])) == (
            "New",
            "Room 1",
            "keep",
        )
        assert int(ev["SEQUENCE"]) == 4 and ev["LAST-MODIFIED"].dt == NOW
        assert ev["DTSTART"].dt == MON.replace(tzinfo=CHI) and "COUNT=10" in out.writes[0].text

    def test_new_weekdays(self):
        out, _ = plan(
            resource(master("FREQ=WEEKLY;BYDAY=MO,WE;COUNT=6")), "update_all",
            recurrence={"frequency": "weekly", "weekdays": ["TU", "TH"]},
        )  # fmt: skip
        [ev] = vevents(written(out))
        assert ev["RRULE"].to_ical() == b"FREQ=WEEKLY;BYDAY=TU,TH"
        assert ev["DTSTART"].dt == (MON + DAY).replace(tzinfo=CHI)  # the first Tuesday
        days = {s.weekday() for s, _ in starts(*texts(out))}
        assert days == {1, 3}

    def test_new_time_of_day_keeps_the_length(self):
        text = resource(
            master("FREQ=DAILY;COUNT=5").replace(
                "DTEND;TZID=America/Chicago:20270104T100000",
                "DTEND;TZID=America/Chicago:20270104T093000",
            )
        )
        out, _ = plan(text, "update_all", start="2027-01-04T10:00")
        [ev] = vevents(written(out))
        assert ev["DTSTART"].dt == MON.replace(hour=10, tzinfo=CHI)
        assert ev["DTEND"].dt - ev["DTSTART"].dt == dt.timedelta(minutes=30)
        assert [s.hour for s, _ in starts(*texts(out))] == [10] * 5

    def test_new_start_from_a_date_is_the_anchor(self):
        rule = {"frequency": "weekly", "weekdays": ["WE"], "count": 2}
        out, _ = plan(
            resource(master()),
            "update_from",
            "2027-02-01",
            start="2027-02-03T08:00",
            recurrence=rule,
        )
        new = series_master_of(written(out, NEW_PATH))
        assert new["DTSTART"].dt == dt.datetime(2027, 2, 3, 8, tzinfo=CHI)

    def test_zone_only_keeps_wall_clock_times(self):
        out, _ = plan(resource(master()), "update_all", timezone="Europe/London")
        text = out.writes[0].text
        assert "DTSTART;TZID=Europe/London:20270104T090000" in text
        assert "TZID:Europe/London" in text  # a matching VTIMEZONE

    def test_definition_without_occurrence(self):
        # Tuesdays until the first Monday: nothing.
        rule = {"frequency": "weekly", "weekdays": ["TU"], "until": "2027-01-04"}
        with pytest.raises(ToolInputError, match="no occurrence"):
            plan(resource(master()), "update_all", recurrence=rule)

    def test_all_day_until_follows_the_value_type(self):
        until = dt.datetime(2027, 1, 25, 23, 59, 59, tzinfo=CHI).astimezone(dt.UTC)
        text = resource(master(f"FREQ=WEEKLY;UNTIL={until:%Y%m%dT%H%M%SZ}"))
        out, _ = plan(text, "update_all", all_day=True)
        [ev] = vevents(written(out))
        assert ev["DTSTART"].dt == MON.date() and ev["RRULE"]["UNTIL"] == [dt.date(2027, 1, 25)]
        assert len(starts(*texts(out))) == 4


class TestSeriesExceptions:
    TEXT = resource(
        master("FREQ=WEEKLY;COUNT=10", exdate(MON + WEEK, MON + 3 * WEEK)),
        override(MON + 2 * WEEK, start=MON + 2 * WEEK + 5 * HOUR),  # moved to 14:00
        override(MON + 4 * WEEK, title="Exam"),  # retitled
    )

    def test_replace_all_discards_everything(self):
        out, _ = plan(self.TEXT, "replace_all", title="New")
        cal = written(out)
        [ev] = vevents(cal)
        assert "EXDATE" not in ev and str(ev["SUMMARY"]) == "New"
        assert out.exceptions == {"kept": 0, "discarded": 4, "unmatched": 0}
        assert [t for _, t in starts(*texts(out))] == ["New"] * 10

    def test_reset_with_no_field(self):
        out, _ = plan(self.TEXT, "replace_all")
        [ev] = vevents(written(out))
        assert "EXDATE" not in ev and str(ev["SUMMARY"]) == "Class"

    def test_update_all_keeps_exceptions(self):
        out, _ = plan(self.TEXT, "update_all", location="Gym")
        assert out.exceptions == {"kept": 4, "discarded": 0, "unmatched": 0}
        got = {s: t for s, t in starts(*texts(out))}
        assert MON + WEEK not in got and MON + 3 * WEEK not in got
        assert got[MON + 2 * WEEK + 5 * HOUR] == "Class" and got[MON + 4 * WEEK] == "Exam"
        locations = {str(c.get("LOCATION")) for c in vevents(written(out))}
        assert locations == {"Gym"}
        assert out.writes[0].text.count("EXDATE") == 1  # left as stored

    def test_new_notes_reach_overrides_without_their_own(self):
        text = resource(
            master("FREQ=WEEKLY;COUNT=10", "DESCRIPTION:old"),
            override(MON + WEEK, "DESCRIPTION:old"),
            override(MON + 2 * WEEK, "DESCRIPTION:old"),
            override(MON + 3 * WEEK, "DESCRIPTION:old", title="Retitled"),
            override(MON + 4 * WEEK, "DESCRIPTION:own notes"),
        )
        out, _ = plan(text, "update_all", notes="new")
        by_rid = {
            c["RECURRENCE-ID"].dt.replace(tzinfo=None): c
            for c in vevents(written(out))
            if "RECURRENCE-ID" in c
        }
        for week in (1, 2, 3):
            assert str(by_rid[MON + week * WEEK]["DESCRIPTION"]) == "new"
        assert str(by_rid[MON + 3 * WEEK]["SUMMARY"]) == "Retitled"
        assert str(by_rid[MON + 4 * WEEK]["DESCRIPTION"]) == "own notes"

    def test_moved_occurrence_keeps_its_time_and_title_rule(self):
        text = resource(
            master("FREQ=DAILY;COUNT=10"),
            override(MON + 2 * DAY, start=MON + 2 * DAY + 5 * HOUR),
            override(MON + 3 * DAY, start=MON + 3 * DAY + 5 * HOUR, title="Own"),
        )
        out, _ = plan(text, "update_all", title="New", start="2027-01-04T10:00")
        got = dict(starts(*texts(out)))
        assert got[MON + 2 * DAY + 5 * HOUR] == "New" and got[MON + 3 * DAY + 5 * HOUR] == "Own"
        assert got[MON + DAY + HOUR] == "New"

    def test_unmoved_override_takes_the_new_time(self):
        text = resource(master("FREQ=DAILY;COUNT=5"), override(MON + 2 * DAY, title="Own"))
        out, _ = plan(text, "update_all", start="2027-01-04T10:00")
        [ov] = [c for c in vevents(written(out)) if "RECURRENCE-ID" in c]
        assert (
            ov["RECURRENCE-ID"].dt == ov["DTSTART"].dt == (MON + 2 * DAY + HOUR).replace(tzinfo=CHI)
        )
        assert str(ov["SUMMARY"]) == "Own" and int(ov["SEQUENCE"]) == 1

    def test_new_time_re_keys_a_deleted_occurrence(self):
        feb15 = dt.datetime(2027, 2, 15, 9)
        text = resource(master("FREQ=DAILY;COUNT=60", exdate(feb15)))
        out, _ = plan(text, "update_all", start="2027-01-04T10:00")
        [ev] = vevents(written(out))
        assert [d.dt for d in ev["EXDATE"].dts] == [(feb15 + HOUR).replace(tzinfo=CHI)]
        assert not any(s.date() == feb15.date() for s, _ in starts(*texts(out)))
        assert out.unmatched == []

    def test_re_keyed_override_carries_the_recurrence_set(self):
        text = resource(
            master(
                "FREQ=DAILY;COUNT=5", "RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:SET-1"
            ),
            override(MON + DAY, title="Own"),
        )
        out, _ = plan(text, "update_all", start="2027-01-04T10:00")
        [ov] = [c for c in vevents(written(out)) if "RECURRENCE-ID" in c]
        assert compose.recurrence_set(ov) == "SET-1"
        # Not re-keyed: left as stored.
        same, _ = plan(text, "update_all", title="New")
        [ov] = [c for c in vevents(written(same)) if "RECURRENCE-ID" in c]
        assert compose.recurrence_set(ov) is None

    WEEKDAYS = resource(
        master("FREQ=WEEKLY;BYDAY=MO,WE;COUNT=8", exdate(MON + 2 * DAY)),
        override(MON + WEEK, title="Changed Monday"),
    )

    def test_deleted_occurrence_that_no_longer_fits(self):
        out, _ = plan(
            self.WEEKDAYS,
            "update_all",
            recurrence={"frequency": "weekly", "weekdays": ["MO"], "count": 4},
        )
        assert out.unmatched == [{"kind": "deleted", "original_start": at(MON + 2 * DAY)}]
        assert out.exceptions == {"kept": 1, "discarded": 0, "unmatched": 1}
        [ev] = [c for c in vevents(written(out)) if "RECURRENCE-ID" not in c]
        assert "EXDATE" not in ev
        assert (MON + WEEK, "Changed Monday") in starts(*texts(out))

    def test_changed_occurrence_that_no_longer_fits(self):
        rule = {"frequency": "weekly", "weekdays": ["WE"], "count": 4}
        with pytest.raises(
            ToolInputError,
            match="^1 changed occurrences no longer fit the new pattern; preview to see them, "
            "or pass drop_unmatched: true to drop them$",
        ):
            plan(self.WEEKDAYS, "update_all", recurrence=rule)
        out, _ = plan(self.WEEKDAYS, "update_all", recurrence=rule, preview=True)
        assert out.requires_drop_unmatched is True
        assert out.unmatched == [{
            "kind": "changed", "original_start": at(MON + WEEK), "start": at(MON + WEEK),
            "end": at(MON + WEEK + HOUR), "title": "Changed Monday",
        }]  # fmt: skip
        out, _ = plan(self.WEEKDAYS, "update_all", recurrence=rule, drop=True)
        assert out.requires_drop_unmatched is False
        assert "RECURRENCE-ID" not in out.writes[0].text
        assert "Changed Monday" not in out.writes[0].text
        assert len(out.unmatched) == 1

    def test_two_occurrences_that_day_do_not_match(self):
        text = resource(master("FREQ=DAILY;BYHOUR=9,15;COUNT=6", exdate(MON + DAY)))
        out, _ = plan(text, "update_all", end="2027-01-04T09:30")
        assert out.unmatched == [{"kind": "deleted", "original_start": at(MON + DAY)}]

    def test_all_day_change_keeps_no_override(self):
        text = resource(
            master("FREQ=DAILY;COUNT=5", exdate(MON + DAY)), override(MON + 2 * DAY, title="Own")
        )
        out, _ = plan(text, "update_all", all_day=True, drop=True)
        [ev] = vevents(written(out))
        assert [d.dt for d in ev["EXDATE"].dts] == [(MON + DAY).date()]
        assert [e["kind"] for e in out.unmatched] == ["changed"]

    def test_cancelled_override_is_a_deleted_occurrence(self):
        text = resource(master("FREQ=DAILY;COUNT=5"), override(MON + DAY, "STATUS:CANCELLED"))
        out, _ = plan(text, "update_all", recurrence={"frequency": "weekly", "count": 2})
        assert out.unmatched == [{"kind": "deleted", "original_start": at(MON + DAY)}]
        out, _ = plan(text, "update_all", title="New")
        assert out.occurrences == 4

    def test_side_of_the_date_is_the_original_start(self):
        # Moved from after the date to before it: it belongs to the part from the date.
        text = resource(master(), override(MON + 5 * WEEK, start=MON + 3 * WEEK + DAY))
        out, _ = plan(text, "update_from", "2027-02-01", location="Gym")
        assert "RECURRENCE-ID" not in out.writes[1].text
        [ov] = [c for c in vevents(written(out, NEW_PATH)) if "RECURRENCE-ID" in c]
        assert ov["DTSTART"].dt == (MON + 3 * WEEK + DAY).replace(tzinfo=CHI)
        assert str(ov["UID"]) == "NEW-UID" and str(ov["LOCATION"]) == "Gym"


class TestSeriesSplit:
    def test_apples_cut(self):
        start = dt.datetime(2026, 10, 6, 9)  # a Tuesday
        out, cal = plan(resource(master("FREQ=WEEKLY;COUNT=10", start=start)), "update_from",
                        "2026-11-03", occurrence=at(start), title="Later")  # fmt: skip
        new, old = out.writes
        assert (new.path, new.etag, old.path, old.etag) == (NEW_PATH, None, PATH, '"e0"')
        assert "RRULE:FREQ=WEEKLY;UNTIL=20261103T055959Z" in old.text and "COUNT" not in old.text
        old_master, new_master = (
            series_master_of(parsed(old.text)),
            series_master_of(parsed(new.text)),
        )
        value = compose.recurrence_set(old_master)
        assert value == "SET-NEW" == compose.recurrence_set(new_master)
        assert value not in (str(old_master["UID"]), str(new_master["UID"]))
        assert "RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:SET-NEW" in new.text
        assert new_master["DTSTART"].dt == dt.datetime(2026, 11, 3, 9, tzinfo=CHI)
        assert "COUNT=6" in new.text and str(new_master["SUMMARY"]) == "Later"
        assert out.split and out.previous == {
            "uid": "s",
            "last_start": at(dt.datetime(2026, 10, 27, 9)),
        }
        both = starts(old.text, new.text)
        assert [s for s, _ in both] == [start + i * WEEK for i in range(10)]
        assert [t for _, t in both] == ["Class"] * 4 + ["Later"] * 6

    def test_cut_across_a_dst_change(self):
        out, _ = plan(resource(master("FREQ=WEEKLY")), "update_from", "2027-03-15", title="x")
        assert "UNTIL=20270315T045959Z" in out.writes[1].text  # CDT (UTC-5) on 14 March

    def test_all_day_cut(self):
        text = resource(master("FREQ=WEEKLY", start=MON.date()))
        out, _ = plan(
            text, "update_from", "2027-03-01", occurrence=MON.date().isoformat(), title="x"
        )
        assert "RRULE:FREQ=WEEKLY;UNTIL=20270228\r\n" in out.writes[1].text

    def test_floating_cut(self):
        text = resource(
            vevent("s", "Class", MON, MON + HOUR, tz=None, extra=("RRULE:FREQ=WEEKLY",))
        )
        out, _ = plan(text, "update_from", "2027-02-01", occurrence=MON.isoformat(), title="x")
        assert (
            "UNTIL=20270131T235959;" in out.writes[1].text
            or "UNTIL=20270131T235959\r" in out.writes[1].text
        )

    def test_recurrence_set_value_reused(self):
        text = resource(
            master("FREQ=WEEKLY", "RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:OLD-SET")
        )
        out, _ = plan(text, "update_from", "2027-02-01", title="x")
        new, old = (series_master_of(parsed(w.text)) for w in out.writes)
        assert compose.recurrence_set(new) == compose.recurrence_set(old) == "OLD-SET"
        assert out.writes[1].text.count("RELATED-TO") == 1

    def test_count_split_keeps_the_dates(self):
        text = resource(master("FREQ=WEEKLY;COUNT=10", exdate(MON + WEEK)))
        before = starts(text)
        out, _ = plan(text, "update_from", "2027-02-01", location="x")  # the fifth occurrence
        new, old = out.writes
        assert "UNTIL=20270201T055959Z" in old.text and "COUNT" not in old.text
        assert "COUNT=6" in new.text
        assert [s for s, _ in starts(*texts(out))] == [s for s, _ in before]

    def test_earlier_part_kept_as_stored(self):
        early = override(MON + WEEK, "X-APPLE-TRAVEL-ADVISORY-BEHAVIOR:AUTOMATIC", title="Early")
        late = override(MON + 6 * WEEK, title="Late")
        text = resource(
            master("FREQ=WEEKLY;COUNT=10", exdate(MON + 2 * WEEK, MON + 7 * WEEK)), early, late
        )
        out, _ = plan(text, "update_from", "2027-02-01", location="Gym")
        new, old = out.writes
        [kept] = [c for c in vevents(parsed(old.text)) if "RECURRENCE-ID" in c]
        [stored] = [c for c in vevents(parsed(text)) if str(c["SUMMARY"]) == "Early"]
        assert props(kept) == props(stored)  # every property as stored
        assert "Late" not in old.text and stamp(MON + 7 * WEEK) not in old.text
        assert stamp(MON + 2 * WEEK) in old.text
        new_cal = parsed(new.text)
        [ov] = [c for c in vevents(new_cal) if "RECURRENCE-ID" in c]
        assert (str(ov["UID"]), str(ov["SUMMARY"]), compose.recurrence_set(ov)) == (
            "NEW-UID",
            "Late",
            "SET-NEW",
        )
        assert [d.dt for d in series_master_of(new_cal)["EXDATE"].dts] == [
            (MON + 7 * WEEK).replace(tzinfo=CHI)
        ]
        assert out.exceptions == {"kept": 2, "discarded": 0, "unmatched": 0}

    def test_new_resource_copies_the_master(self):
        text = resource(master(
            "FREQ=WEEKLY", "LOCATION:Room 1",
            "X-APPLE-STRUCTURED-LOCATION;VALUE=URI;X-TITLE=Room 1:geo:1,2", "TRANSP:TRANSPARENT",
            "SEQUENCE:4", "ORGANIZER;EMAIL=me@example.net:/111/principal/",
            "ATTENDEE;EMAIL=me@example.net:/111/principal/",
            "BEGIN:VALARM", "ACTION:DISPLAY", "DESCRIPTION:Reminder", "TRIGGER:-PT10M",
            "END:VALARM",
        ))  # fmt: skip
        out, _ = plan(text, "replace_from", "2027-02-01")
        cal = parsed(out.writes[0].text)
        ev = series_master_of(cal)
        assert str(ev["UID"]) == "NEW-UID" and int(ev["SEQUENCE"]) == 0
        assert ev["CREATED"].dt == ev["DTSTAMP"].dt == NOW
        for name in ("LOCATION", "X-APPLE-STRUCTURED-LOCATION", "TRANSP"):
            assert name in ev, name
        assert "ORGANIZER" not in out.writes[0].text and "ATTENDEE" not in out.writes[0].text
        assert [a["TRIGGER"].dt for a in ev.walk("VALARM")] == [-dt.timedelta(minutes=10)]
        assert [tz["TZID"] for tz in cal.walk("VTIMEZONE")] == ["America/Chicago"]
        old = series_master_of(parsed(out.writes[1].text))
        assert int(old["SEQUENCE"]) == 5 and "ORGANIZER" in old

    def test_no_occurrence_on_or_after_the_date(self):
        with pytest.raises(
            ToolInputError, match="^the series has no occurrence on or after 2027-06-07$"
        ):
            plan(resource(master()), "update_from", "2027-06-07", title="x")

    @pytest.mark.parametrize("effective", ["2027-01-04", "2026-12-01"])
    def test_effective_at_or_before_the_first_occurrence_changes_in_place(self, effective):
        out, _ = plan(
            resource(master(), override(MON + WEEK, title="Own")),
            "update_from",
            effective,
            title="New",
        )
        [w] = out.writes
        assert (w.path, w.etag, out.split, out.previous) == (PATH, '"e0"', False, None)
        assert {t for _, t in starts(w.text)} == {"New", "Own"}


def part(path: str, *events: str, etag: str | None = None) -> compose.Part:
    cal = parsed(resource(*events))
    return compose.Part(path, etag or f'"{path}"', cal, series_master_of(cal))


SET = "RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:SET-1"
FIRST = master("FREQ=WEEKLY;UNTIL=20270223T055959Z", SET)  # 2027-01-04 to 2027-02-22: 8
SECOND = master("FREQ=WEEKLY", SET, start=dt.datetime(2027, 3, 1, 9), uid="s2", title="Class B")


class TestRecurrenceSets:
    def test_pick_parts_by_the_masters_value(self):
        other = master("FREQ=DAILY", SET.replace("SET-1", "OTHER"), uid="o")
        listed = [
            ("/c/0.ics", resource(FIRST), '"0"'),
            ("/c/1.ics", resource(SECOND), '"1"'),
            ("/c/2.ics", resource(other), '"2"'),
            ("/c/3.ics", resource(master("FREQ=DAILY", "RELATED-TO:SET-1", uid="p")), '"3"'),
            ("/c/4.ics", resource(override(MON, SET, uid="q")), '"4"'),
            ("/c/5.ics", "not icalendar", '"5"'),
            ("/c/1.ics", resource(SECOND), '"1"'),
        ]  # fmt: skip
        found = compose.pick_parts(listed, "SET-1", ["/c/0.ics"])
        assert [(p.path, p.etag, p.uid) for p in found] == [("/c/1.ics", '"1"', "s2")]

    def test_field_edits_across_parts(self):
        second = part("/c/1.ics", SECOND)
        out, _ = plan(resource(FIRST), "update_all", others=[second], title="New")
        assert [(w.path, w.etag) for w in out.writes] == [
            (PATH, '"e0"'),
            ("/c/1.ics", '"/c/1.ics"'),
        ]
        assert {t for _, t in starts(*texts(out), weeks=20)} == {"New"}
        assert (out.found, out.changed, out.split) == (2, 2, False)

    @pytest.mark.parametrize(
        ("mode", "changes"),
        [("replace_all", {}), ("update_all", {"start": "2027-01-04T10:00"}),
         ("update_all", {"recurrence": {"frequency": "daily"}})],
    )  # fmt: skip
    def test_whole_set_pattern_refused(self, mode, changes):
        with pytest.raises(
            ToolInputError,
            match=r"^this series was split; change it from a date \(replace_from / update_from\)$",
        ):
            plan(resource(FIRST), mode, others=[part("/c/1.ics", SECOND)], **changes)

    def test_from_a_date_with_a_later_part(self):
        out, _ = plan(
            resource(FIRST),
            "update_from",
            "2027-02-01",
            others=[part("/c/1.ics", SECOND)],
            location="Gym",
        )
        assert [(w.path, w.etag is None) for w in out.writes] == [
            (NEW_PATH, True), (PATH, False), ("/c/1.ics", False),
        ]  # fmt: skip
        assert "LOCATION:Gym" in out.writes[2].text and "LOCATION" not in out.writes[1].text
        assert (out.found, out.changed, out.split) == (2, 2, True)

    @pytest.mark.parametrize(
        ("mode", "changes"),
        [("update_from", {"recurrence": {"frequency": "daily"}}), ("replace_from", {})],
    )
    def test_pattern_from_a_date_with_a_later_part(self, mode, changes):
        with pytest.raises(
            ToolInputError,
            match="^this series was split on 2027-03-01; change its pattern from that date or "
            "later$",
        ):
            plan(resource(FIRST), mode, "2027-02-01", others=[part("/c/1.ics", SECOND)], **changes)

    def test_from_the_start_of_a_later_part(self):
        out, _ = plan(
            resource(FIRST), "update_from", "2027-03-01", others=[part("/c/1.ics", SECOND)],
            recurrence={"frequency": "weekly", "weekdays": ["TU"]},
        )  # fmt: skip
        [w] = out.writes
        assert (w.path, out.split, out.found, out.changed) == ("/c/1.ics", False, 2, 1)

    def test_the_part_containing_the_date_is_split(self):
        # Named through the later part; the date falls in the first part.
        first = part(PATH, FIRST, etag='"e0"')
        cal = parsed(resource(SECOND))
        named = compose.Part("/c/1.ics", '"1"', cal, series_master_of(cal))
        edit = compose.series_edit("update_from", "2027-02-01", Changes(title="x"), None)
        out = compose.plan_series_edit(
            named, [first], edit, CHI, NOW, new_uid="NEW-UID", new_value="v", new_path=NEW_PATH
        )
        assert [w.path for w in out.writes] == [NEW_PATH, PATH, "/c/1.ics"]
        assert compose.recurrence_set(series_master_of(parsed(out.writes[0].text))) == "SET-1"

    def test_too_many_parts(self):
        others = [
            part(
                f"/c/{i}.ics",
                master("FREQ=WEEKLY;COUNT=2", SET, start=MON + i * 4 * WEEK, uid=f"p{i}"),
            )
            for i in range(1, 11)
        ]
        with pytest.raises(
            ToolInputError, match="^this series has more than 10 parts; change it in Calendar$"
        ):
            plan(
                resource(master("FREQ=WEEKLY;COUNT=2", SET)), "update_all", others=others, title="x"
            )
        out, _ = plan(
            resource(master("FREQ=WEEKLY;COUNT=2", SET)), "update_all", others=others[:9], title="x"
        )
        assert len(out.writes) == 10
        assert [w.uid for w in out.writes] == ["s", *(f"p{i}" for i in range(1, 10))]  # in order

    def test_invited_later_part_is_refused_by_the_people_check(self):
        invited = part(
            "/c/1.ics",
            SECOND.replace("END:VEVENT", "ATTENDEE:mailto:carol@example.com\r\nEND:VEVENT"),
        )
        with pytest.raises(ToolInputError, match="event has attendees"):
            compose.check_people(invited.cal, ME)

    def windows(self, events, mode, effective=None, today=dt.date(2027, 6, 1)):
        cal = parsed(resource(*events))
        named = compose.Part(PATH, '"e"', cal, series_master_of(cal))
        edit = compose.series_edit(mode, effective, Changes(title="x"), None)
        return compose.part_windows(named, edit, CHI, today)

    def test_listing_windows(self):
        last = dt.datetime(2027, 2, 22, 9, tzinfo=CHI).astimezone(dt.UTC)
        first = MON.replace(tzinfo=CHI).astimezone(dt.UTC)
        since = dt.datetime(2026, 5, 31, tzinfo=CHI).astimezone(dt.UTC)
        assert self.windows([FIRST], "update_all") == ([(last, None), (since, first)], "2026-05-31")
        # An endless named part has no later parts; one starting before the window, no earlier.
        later = dt.datetime(2027, 3, 1, tzinfo=CHI).astimezone(dt.UTC)
        assert self.windows([SECOND], "update_all", today=dt.date(2028, 6, 1)) == ([], "2027-06-01")
        assert self.windows([SECOND], "replace_all") == (
            [(since, later.replace(hour=15))],
            "2026-05-31",
        )
        effective = dt.datetime(2027, 3, 8, tzinfo=CHI).astimezone(dt.UTC)
        assert self.windows([FIRST], "update_from", "2027-03-08") == ([(effective, None)], None)


class TestSeriesCounts:
    def test_counts_from_a_date(self):
        nov = [MON + i * WEEK for i in (12, 14, 17)]
        text = resource(master("FREQ=WEEKLY;COUNT=20", exdate(*nov)))
        out, _ = plan(text, "update_from", (MON + 10 * WEEK).date().isoformat(), location="x")
        assert (out.occurrences, out.occurrences_through) == (7, None)
        assert out.exceptions == {"kept": 3, "discarded": 0, "unmatched": 0}

    def test_endless_series(self):
        out, _ = plan(resource(master("FREQ=WEEKLY")), "update_all", title="x")
        assert out.occurrences == 53  # 366 days from Monday 2027-01-04
        assert out.occurrences_through == "2028-01-04"

    def test_first_occurrence_skips_deleted_ones(self):
        text = resource(master("FREQ=WEEKLY;COUNT=5", exdate(MON)))
        out, _ = plan(text, "update_all", occurrence=at(MON + WEEK), title="x")
        assert out.first["DTSTART"].dt == (MON + WEEK).replace(tzinfo=CHI)
        assert out.occurrences == 4

    def test_counts_over_several_parts(self):
        out, _ = plan(resource(FIRST), "update_all", others=[part("/c/1.ics", SECOND)], title="x")
        # 8 in the first part; the endless second through 2028-01-04.
        assert out.occurrences == 8 + 45 and out.occurrences_through == "2028-01-04"
