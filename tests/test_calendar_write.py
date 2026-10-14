# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

"""Calendar writes (add-calendar-write, ADR-0005) against the fake CalDAV server."""

from __future__ import annotations

import datetime as dt
import zoneinfo

import httpx
import icalendar
import pytest

from .fakecaldav import HOME_HOST, FakeAccount, FakeCalDav, FakeCalendar, etag, resource, vevent

CHI = zoneinfo.ZoneInfo("America/Chicago")
NEXT_MONDAY = dt.date.today() + dt.timedelta(days=7 - dt.date.today().weekday())


def monday(week: int, hour: int = 9) -> dt.datetime:
    return dt.datetime.combine(NEXT_MONDAY + dt.timedelta(weeks=week), dt.time(hour))


def weekly(uid: str = "series", *extra: str, count: int = 4) -> str:
    return vevent(
        uid,
        "Standup",
        monday(0),
        monday(0).replace(minute=15),
        extra=(f"RRULE:FREQ=WEEKLY;COUNT={count}", *extra),
    )


class TestFake:
    """The fake behaves as the live checks saw iCloud behave."""

    @pytest.fixture
    def fake(self):
        cal = FakeCalendar("home", "Home", resources=[resource(vevent("a", "A", monday(0)))])
        return FakeCalDav({"alice@icloud.com": FakeAccount("111", [cal])})

    @pytest.fixture
    def http(self, fake):
        return httpx.Client(auth=("alice@icloud.com", "app-pw"), transport=fake.transport())

    URL = f"https://{HOME_HOST}/111/calendars/home/"

    def test_conditional_put_and_delete(self, fake, http):
        cal = fake.accounts["alice@icloud.com"].calendars[0]
        body = resource(vevent("b", "B", monday(1)))
        ct = {"content-type": "text/calendar; charset=utf-8"}
        assert (
            http.put(
                self.URL + "b.ics", content=body, headers={**ct, "if-none-match": "*"}
            ).status_code
            == 201
        )
        assert (
            http.put(
                self.URL + "b.ics", content=body, headers={**ct, "if-none-match": "*"}
            ).status_code
            == 412
        )
        tag = etag(cal.get("b.ics"))
        changed = body.replace("SUMMARY:B", "SUMMARY:C")
        assert (
            http.put(
                self.URL + "b.ics", content=changed, headers={**ct, "if-match": '"old"'}
            ).status_code
            == 412
        )
        resp = http.put(self.URL + "b.ics", content=changed, headers={**ct, "if-match": tag})
        assert resp.status_code == 204 and resp.headers["etag"] != tag
        assert http.delete(self.URL + "b.ics", headers={"if-match": tag}).status_code == 412
        assert (
            http.delete(self.URL + "b.ics", headers={"if-match": resp.headers["etag"]}).status_code
            == 204
        )
        assert http.delete(self.URL + "b.ics", headers={"if-match": tag}).status_code == 404
        assert fake.write_attempts == [] and fake.scheduling_messages == []

    def test_forbidden_writes_are_recorded(self, fake, http):
        ct = {"content-type": "text/calendar"}
        http.put(self.URL + "x.ics", content=resource(), headers=ct)  # no precondition
        http.delete(self.URL, headers={"if-match": '"x"'})  # the collection
        http.request("MKCALENDAR", self.URL + "new/")
        assert len(fake.write_attempts) == 3

    def test_multiget_and_resource_query(self, fake, http):
        multiget = (
            '<c:calendar-multiget xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
            "<d:prop><d:getetag/><c:calendar-data/></d:prop>"
            "<d:href>/111/calendars/home/0.ics</d:href><d:href>/111/calendars/home/9.ics</d:href>"
            "</c:calendar-multiget>"
        )
        resp = http.request("REPORT", self.URL, content=multiget, headers={"depth": "1"})
        assert resp.status_code == 207
        assert "SUMMARY:A" in resp.text and "404 Not Found" in resp.text
        assert http.request("REPORT", self.URL + "0.ics", content="x").status_code == 403

    def test_expands_rules_with_exdate_and_overrides(self):
        from .fakecaldav import _expanded

        moved = vevent(
            "series", "Standup moved", monday(2, 11), monday(2, 12),
            extra=(f"RECURRENCE-ID;TZID=America/Chicago:{monday(2):%Y%m%dT%H%M%S}",),
        )  # fmt: skip
        res = resource(
            weekly("series", f"EXDATE;TZID=America/Chicago:{monday(1):%Y%m%dT%H%M%S}"), moved
        )
        lo = dt.datetime.combine(NEXT_MONDAY, dt.time(), CHI).astimezone(dt.UTC)
        out = _expanded(res, lo, lo + dt.timedelta(weeks=8), "America/Chicago")
        assert out is not None
        starts = sorted(
            (ev["DTSTART"].dt.replace(tzinfo=None), str(ev["SUMMARY"]))
            for ev in icalendar.Calendar.from_ical(out).walk("VEVENT")
        )
        assert starts == [
            (monday(0), "Standup"), (monday(2, 11), "Standup moved"), (monday(3), "Standup"),
        ]  # fmt: skip

    def test_own_address_stored_as_principal_path_and_scheduling_recorded(self, fake, http):
        cal = fake.accounts["alice@icloud.com"].calendars[0]
        ct = {"content-type": "text/calendar"}
        body = resource(
            vevent(
                "m",
                "Meeting",
                monday(1),
                extra=("ORGANIZER:mailto:alice@icloud.com", "ATTENDEE:mailto:carol@example.com"),
            )  # fmt: skip
        )
        http.put(self.URL + "m.ics", content=body, headers={**ct, "if-none-match": "*"})
        assert "ORGANIZER;EMAIL=alice@icloud.com:/111/principal/" in cal.get("m.ics")
        assert fake.scheduling_messages == [("PUT", "m.ics")]
