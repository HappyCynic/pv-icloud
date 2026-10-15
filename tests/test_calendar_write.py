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

    @staticmethod
    def listing(start: dt.datetime, end: dt.datetime | None = None, filters: str = "") -> str:
        s = start.astimezone(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        e = f' end="{end.astimezone(dt.UTC):%Y%m%dT%H%M%SZ}"' if end else ""
        return (
            '<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
            "<d:prop><d:getetag/><c:calendar-data/></d:prop>"
            '<c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">'
            f'<c:time-range start="{s}"{e}/>{filters}'
            "</c:comp-filter></c:comp-filter></c:filter></c:calendar-query>"
        )

    def test_unexpanded_open_ended_listing(self, fake, http):
        cal = fake.accounts["alice@icloud.com"].calendars[0]
        series = resource(weekly("s", count=10))  # through week 9
        cal.resources.append(series)
        lo = dt.datetime.combine(NEXT_MONDAY + dt.timedelta(weeks=5), dt.time(), CHI)
        resp = http.request("REPORT", self.URL, content=self.listing(lo), headers={"depth": "1"})
        # The series has occurrences after `lo`; the single event of week 0 has none.
        assert resp.status_code == 207 and "RRULE:FREQ=WEEKLY;COUNT=10" in resp.text
        assert "SUMMARY:A" not in resp.text and etag(series).strip('"') in resp.text
        late = lo + dt.timedelta(weeks=10)
        resp = http.request("REPORT", self.URL, content=self.listing(late), headers={"depth": "1"})
        assert "BEGIN:VCALENDAR" not in resp.text
        first = dt.datetime.combine(NEXT_MONDAY, dt.time(), CHI)
        bounded = self.listing(first, first + dt.timedelta(days=1))
        resp = http.request("REPORT", self.URL, content=bounded, headers={"depth": "1"})
        assert "SUMMARY:A" in resp.text and "COUNT=10" in resp.text
        assert [k for m, _, k in fake.calls if m == "REPORT"] == ["listing"] * 3

    @pytest.mark.parametrize(
        ("filters", "status"),
        [
            ('<c:prop-filter name="SUMMARY"><c:text-match>A</c:text-match></c:prop-filter>', 207),
            (
                '<c:prop-filter name="SUMMARY"><c:text-match collation="i;octet">A'
                "</c:text-match></c:prop-filter>",
                412,
            ),
            (
                '<c:prop-filter name="RELATED-TO"><c:param-filter name="RELTYPE">'
                "<c:text-match>X</c:text-match></c:param-filter></c:prop-filter>",
                500,
            ),
        ],
    )
    def test_unsupported_filters_answer_as_icloud_does(self, http, filters, status):
        first = dt.datetime.combine(NEXT_MONDAY, dt.time(), CHI)
        body = self.listing(first, first + dt.timedelta(days=1), filters)
        resp = http.request("REPORT", self.URL, content=body, headers={"depth": "1"})
        assert resp.status_code == status
        assert "SUMMARY:A" not in resp.text  # a prop-filter matches nothing, not even SUMMARY

    def test_put_fail_hook_and_call_record(self, fake, http):
        cal = fake.accounts["alice@icloud.com"].calendars[0]
        ct = {"content-type": "text/calendar"}
        body = resource(vevent("b", "B", monday(1)))
        fake.put_fail["b.ics"] = 503
        put = http.put(self.URL + "b.ics", content=body, headers={**ct, "if-none-match": "*"})
        assert put.status_code == 503 and cal.get("b.ics") is None
        fake.put_fail["b.ics"] = None  # stored, but the answer is lost
        with pytest.raises(httpx.ReadTimeout):
            http.put(self.URL + "b.ics", content=body, headers={**ct, "if-none-match": "*"})
        assert cal.get("b.ics") == body and fake.put_fail == {}
        http.delete(self.URL + "b.ics", headers={"if-match": etag(body)})
        assert fake.calls == [
            ("PUT", "/111/calendars/home/b.ics", "If-None-Match"),
            ("PUT", "/111/calendars/home/b.ics", "If-None-Match"),
            ("DELETE", "/111/calendars/home/b.ics", "If-Match"),
        ]

    def test_expands_count_and_until(self):
        from .fakecaldav import _expanded

        lo = dt.datetime.combine(NEXT_MONDAY, dt.time(), CHI).astimezone(dt.UTC)
        hi = lo + dt.timedelta(weeks=20)
        until = dt.datetime.combine(NEXT_MONDAY + dt.timedelta(weeks=2), dt.time(23, 59, 59), CHI)
        timed = vevent(
            "u", "U", monday(0), monday(0, 10),
            extra=(f"RRULE:FREQ=WEEKLY;UNTIL={until.astimezone(dt.UTC):%Y%m%dT%H%M%SZ}",),
        )  # fmt: skip
        last_day = NEXT_MONDAY + dt.timedelta(weeks=3)
        all_day = vevent(
            "d", "D", NEXT_MONDAY, NEXT_MONDAY + dt.timedelta(days=1),
            extra=(f"RRULE:FREQ=WEEKLY;UNTIL={last_day:%Y%m%d}",),
        )  # fmt: skip
        for res, n in ((timed, 3), (all_day, 4), (weekly("c", count=5), 5)):
            out = _expanded(resource(res), lo, hi, "America/Chicago")
            assert out is not None
            assert len(icalendar.Calendar.from_ical(out).walk("VEVENT")) == n

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


# --- the write module (cal/caldav_write.py) ------------------------------------------------


def alice_fake() -> FakeCalDav:
    return FakeCalDav(
        {
            "alice@icloud.com": FakeAccount(
                "111",
                [
                    FakeCalendar("home", "Home", resources=[resource(vevent("a", "A", monday(0)))]),
                    FakeCalendar(
                        "shared-ro",
                        "Club",
                        types=("collection", "calendar", "shared"),
                        privileges=("read",),
                        resources=[resource(vevent("c", "Club night", monday(1)))],
                    ),
                    FakeCalendar(
                        "SUB1",
                        "TripIt",
                        types=("collection", "subscribed"),
                        source="webcal://feeds.example/x.ics",
                    ),
                ],
            )
        }
    )


class TestWriter:
    @pytest.fixture
    def fake(self):
        return alice_fake()

    @pytest.fixture
    def client(self, fake):
        from pv_icloud.cal.caldav import CalDavClient

        return CalDavClient("alice@icloud.com", "app-pw", transport=fake.transport())

    @pytest.fixture
    def writer(self, client):
        from pv_icloud.cal.caldav_write import CalDavWriter

        return CalDavWriter(client)

    @pytest.fixture
    def cals(self, client):
        return {c.id: c for c in client.calendars()}

    BODY = resource(vevent("new", "New", monday(2)))

    def test_create_update_delete(self, fake, writer, cals):
        home = fake.accounts["alice@icloud.com"].calendars[0]
        path = writer.create(cals["home"], "abc.ics", self.BODY)
        assert path == "/111/calendars/home/abc.ics" and home.get("abc.ics") == self.BODY
        tag = etag(self.BODY)
        changed = self.BODY.replace("SUMMARY:New", "SUMMARY:Newer")
        writer.update(cals["home"], path, changed, tag)
        writer.delete(cals["home"], path, etag(changed))
        assert home.get("abc.ics") is None
        assert [(w.method, w.status) for w in fake.writes] == [
            ("PUT", 201), ("PUT", 204), ("DELETE", 204),
        ]  # fmt: skip
        create, update, delete = fake.writes
        assert create.headers["if-none-match"] == "*" and "if-match" not in create.headers
        assert update.headers["if-match"] == tag and delete.headers["if-match"] == etag(changed)
        assert all(w.headers["schedule-reply"] == "F" for w in fake.writes)
        assert create.headers["content-type"].startswith("text/calendar")
        assert fake.write_attempts == [] and fake.scheduling_messages == []

    def test_refuses_to_send_without_a_precondition(self, fake, writer, cals):
        from pv_icloud.cal.caldav_write import CalDavWriter

        path = "/111/calendars/home/0.ics"
        with pytest.raises(RuntimeError, match="precondition"):
            writer._write("PUT", cals["home"], path, self.BODY, {})
        with pytest.raises(RuntimeError, match="PUT and DELETE"):
            writer._write("MOVE", cals["home"], path, "", {"If-Match": '"x"'})
        assert frozenset({"PUT", "DELETE"}) == CalDavWriter.METHODS
        assert fake.writes == [] and fake.write_attempts == []

    @pytest.mark.parametrize(
        "path",
        [
            "/111/calendars/home/",
            "/111/calendars/home",
            "/111/calendars/home/sub/0.ics",
            "/111/calendars/home/..%2Fshared-ro%2F0.ics",
            "/111/calendars/shared-ro/0.ics",
            "/111/calendars/home/0.vcf",
            "/222/calendars/home/0.ics",
        ],
    )
    def test_refuses_anything_but_a_direct_ics_child(self, fake, writer, cals, path):
        from pv_icloud.errors import ToolInputError

        with pytest.raises(ToolInputError, match="malformed event id"):
            writer.delete(cals["home"], path, '"x"')
        assert fake.writes == [] and fake.write_attempts == []

    @pytest.mark.parametrize("cal_id", ["shared-ro", "SUB1"])
    def test_refuses_calendars_without_write_content(self, fake, writer, cals, cal_id):
        with pytest.raises(RuntimeError, match="not writable"):
            writer.create(cals[cal_id], "x.ics", self.BODY)
        assert fake.writes == [] and fake.write_attempts == []

    def test_conflict_is_reported_and_not_retried(self, fake, writer, cals):
        from pv_icloud.errors import ToolInputError

        home = fake.accounts["alice@icloud.com"].calendars[0]
        stored = home.resources[0]
        assert stored is not None
        reply = stored.replace("SUMMARY:A", "SUMMARY:Reply")
        fake.before_write = lambda: home.store("0.ics", reply)
        with pytest.raises(ToolInputError, match="^the event changed in Calendar; read it again$"):
            writer.update(cals["home"], "/111/calendars/home/0.ics", self.BODY, etag(stored))
        assert [w.status for w in fake.writes] == [412]
        assert home.resources[0] == reply
        with pytest.raises(ToolInputError, match="changed in Calendar"):
            writer.create(cals["home"], "0.ics", self.BODY)  # name taken

    def test_status_mapping(self, fake, writer, cals):
        from pv_icloud.errors import ToolInputError, WriteFailed

        with pytest.raises(ToolInputError, match="not found"):
            writer.delete(cals["home"], "/111/calendars/home/9.ics", '"x"')
        for status in (403, 400, 409):
            fake.write_fail = (status, None)
            with pytest.raises(
                WriteFailed, match="^iCloud refused the change; nothing was changed$"
            ):
                writer.create(cals["home"], "x.ics", self.BODY)
        fake.write_fail = (500, None)
        with pytest.raises(WriteFailed, match="did not confirm"):
            writer.create(cals["home"], "x.ics", self.BODY)
        assert len(fake.writes) == 5  # one request per call, never retried

    def test_throttling_pauses_the_account_and_is_not_retried(self, fake, writer, cals, client):
        from pv_icloud.dav import DavThrottled

        fake.write_fail = (503, "60")
        with pytest.raises(DavThrottled):
            writer.create(cals["home"], "x.ics", self.BODY)
        assert len(fake.writes) == 1 and 59 <= client.throttle.remaining() <= 60
        fake.write_fail = None
        with pytest.raises(DavThrottled):  # paused: nothing is sent
            writer.create(cals["home"], "x.ics", self.BODY)
        with pytest.raises(DavThrottled):  # reads share the pause
            client.calendars()
        assert len(fake.writes) == 1

    def test_lost_answer_is_unconfirmed(self):
        from pv_icloud.cal.caldav import CalDavClient
        from pv_icloud.cal.caldav_write import CalDavWriter
        from pv_icloud.errors import WriteFailed

        fake = alice_fake()

        def handle(request: httpx.Request) -> httpx.Response:
            if request.method == "PUT":
                raise httpx.ReadTimeout("timed out", request=request)
            if request.method == "DELETE":
                raise httpx.ConnectError("refused", request=request)
            return fake.handle(request)

        client = CalDavClient("alice@icloud.com", "app-pw", transport=httpx.MockTransport(handle))
        writer = CalDavWriter(client)
        home = next(c for c in client.calendars() if c.id == "home")
        with pytest.raises(WriteFailed, match="^iCloud did not confirm the change; read the event"):
            writer.create(home, "x.ics", self.BODY)
        with pytest.raises(WriteFailed, match="nothing was changed"):
            writer.delete(home, "/111/calendars/home/0.ics", '"x"')

    def test_logs_carry_no_path_uid_or_body(self, fake, writer, cals, caplog):
        import logging

        from pv_icloud.errors import WriteFailed

        fake.write_fail = (403, None)
        with caplog.at_level(logging.DEBUG), pytest.raises(WriteFailed):
            writer.update(cals["home"], "/111/calendars/home/0.ics", self.BODY, '"x"')
        text = " ".join(r.getMessage() for r in caplog.records if r.name.startswith("pv_icloud"))
        assert "403" in text
        for secret in ("0.ics", "/111/", "UID:new", "SUMMARY", "home"):
            assert secret not in text


# --- create, update and delete in the calendar service -------------------------------------

OWN = "EMAIL=alice@example.net:/111/principal/"


def chi(week: int, hour: int, minute: int = 0) -> str:
    return monday(week, hour).replace(minute=minute, tzinfo=CHI).isoformat()


def service_fake() -> FakeCalDav:
    home = [
        vevent(
            "dentist", "Dentist", monday(0, 14), monday(0, 15),
            extra=(
                "DESCRIPTION:Bring forms", "X-APPLE-TRAVEL-ADVISORY-BEHAVIOR:AUTOMATIC",
                "BEGIN:VALARM", "ACTION:DISPLAY", "DESCRIPTION:Reminder", "TRIGGER:-PT30M",
                "END:VALARM",
            ),
        ),
        weekly("standup"),
        vevent(
            "board", "Board", monday(1, 16), monday(1, 17),
            extra=(f"ORGANIZER;{OWN}", "ATTENDEE:mailto:carol@example.com"),
        ),
        vevent(
            "invite", "Invite", monday(1, 18), monday(1, 19),
            extra=("ORGANIZER:mailto:someone@example.org", f"ATTENDEE;PARTSTAT=ACCEPTED;{OWN}"),
        ),
        vevent(
            "planning", "Planning", monday(2, 10), monday(2, 11),
            extra=(f"ORGANIZER;CN=Alice;{OWN}", f"ATTENDEE;{OWN}"),
        ),
        vevent(
            "mystery", "Mystery", monday(2, 12), monday(2, 13),
            extra=("ATTENDEE:/333/principal/",),
        ),
        vevent(
            "sync", "Team sync", monday(0, 11), monday(0, 12),
            extra=("RRULE:FREQ=WEEKLY;COUNT=4", "ATTENDEE:mailto:carol@example.com"),
        ),
    ]  # fmt: skip
    shared = ("collection", "calendar", "shared")

    def one(cal_id: str, name: str, title: str, hour: int, **kw) -> FakeCalendar:
        event = resource(vevent(f"{cal_id}-1", title, monday(1, hour)))
        return FakeCalendar(cal_id, name, resources=[event], **kw)

    return FakeCalDav(
        {
            "alice@icloud.com": FakeAccount(
                "111",
                [
                    FakeCalendar("home", "Home", resources=[resource(e) for e in home]),
                    FakeCalendar("team", "Team", types=shared),
                    one("club", "Club", "Club night", 20, types=shared, privileges=("read",)),
                    one("fam", "Family", "Dinner", 19),
                    FakeCalendar("kids", "Kids"),
                    one("work", "Work", "Review", 9),
                    one("BANK", "Banking", "Loan", 10),
                    FakeCalendar(
                        "SUB1",
                        "TripIt",
                        types=("collection", "subscribed"),
                        source="webcal://t.example/x",
                    ),
                ],
                addresses=("alice@icloud.com", "alice@example.net"),
            ),
            "bob@icloud.com": FakeAccount("222", [one("bobcal", "Bob Home", "Bob", 9)]),
        }
    )


class TestServiceWrites:
    @pytest.fixture
    def fake(self):
        fake = service_fake()
        yield fake
        # Whatever a test did: iCloud would have emailed nobody, nothing forbidden was sent,
        # and every write asked iCloud not to send replies.
        assert fake.scheduling_messages == []
        assert fake.write_attempts == []
        assert all(w.headers["schedule-reply"] == "F" for w in fake.writes)

    @pytest.fixture
    def make_service(self, fake, tmp_path):
        from pv_icloud.cal.caldav import CalDavClient
        from pv_icloud.cal.caldav_write import CalDavWriter
        from pv_icloud.cal.service import CalendarService

        from .conftest import make_config

        cfg = make_config(
            tmp_path,
            calendar_write=True,
            alice_calendar={
                "calendars": {"exclude": ["Banking"]},
                "write": {"protected_calendars": ["Family"]},
            },
            default_calendar={"write": {"protected_calendars": ["Kids"]}},
        )

        def make(account="alice"):
            acct = cfg.account(account)
            client = CalDavClient(acct.icloud_user, acct.app_password, transport=fake.transport())
            return CalendarService(acct, client, writer=CalDavWriter(client))

        return make

    @pytest.fixture
    def service(self, make_service):
        return make_service()

    @staticmethod
    def find(service, title, week=0, calendars=None):
        start = NEXT_MONDAY + dt.timedelta(weeks=week)
        out = service.events(
            start=start.isoformat(),
            end=(start + dt.timedelta(days=6)).isoformat(),
            calendars=calendars,
        )
        return [e for e in out["events"] if e["title"] == title]

    @staticmethod
    def changes(**kw):
        from pv_icloud.cal.compose import Changes

        return Changes(**kw)

    def home(self, fake):
        return fake.accounts["alice@icloud.com"].calendars[0]

    def test_create_timed_event(self, service, fake):
        out = service.create_event(
            calendar="home",
            changes=self.changes(
                title="Dentist", start=f"{NEXT_MONDAY.isoformat()}T09:00", timezone="Europe/London"
            ),
        )
        assert out["calendar"] == "Home" and out["calendar_id"] == "home"
        assert out["timezone"] == "Europe/London" and out["all_day"] is False
        assert out["recurring"] is False
        stored = self.home(fake).named[f"{out['uid']}.ics"]
        assert f"DTSTART;TZID=Europe/London:{NEXT_MONDAY:%Y%m%d}T090000" in stored
        assert "BEGIN:VTIMEZONE" in stored
        assert "ORGANIZER" not in stored and "ATTENDEE" not in stored
        detail = service.event(out["id"])
        assert (detail["title"], detail["start"], detail["end"]) == (
            "Dentist",
            out["start"],
            out["end"],
        )
        [write] = fake.writes
        assert (write.method, write.headers["if-none-match"]) == ("PUT", "*")

    def test_create_all_day_recurring_with_alerts(self, service, fake):
        out = service.create_event(
            calendar="Home",
            changes=self.changes(
                title="Trash", start=NEXT_MONDAY.isoformat(), all_day=True, alerts=(60, 15)
            ),
            recurrence={"frequency": "weekly", "count": 3},
        )
        assert out["all_day"] is True and out["recurring"] is True and out["timezone"] is None
        assert (out["start"], out["end"]) == (NEXT_MONDAY.isoformat(), NEXT_MONDAY.isoformat())
        for week in range(3):
            [occ] = self.find(service, "Trash", week)
            assert occ["recurring"] is True
        assert self.find(service, "Trash", 3) == []
        assert service.event(out["id"])["title"] == "Trash"
        stored = self.home(fake).named[f"{out['uid']}.ics"]
        assert stored.count("ACTION:DISPLAY") == 2 and "ACTION:EMAIL" not in stored

    def test_create_in_a_read_write_shared_calendar(self, service, fake):
        out = service.create_event(
            calendar="Team", changes=self.changes(title="Lunch", start=f"{NEXT_MONDAY}T12:00")
        )
        assert out["calendar"] == "Team"
        assert len(fake.accounts["alice@icloud.com"].calendars[1].named) == 1

    def test_update_one_field_keeps_the_rest(self, service, fake):
        [ev] = self.find(service, "Dentist")
        before = icalendar.Calendar.from_ical(self.home(fake).resources[0]).walk("VEVENT")[0]
        tag = etag(self.home(fake).resources[0])
        fake.requests.clear()
        out = service.update_event(ev["id"], changes=self.changes(location="Elm St"))
        assert out["id"] == ev["id"] and out["start"] == ev["start"]
        after = icalendar.Calendar.from_ical(self.home(fake).resources[0]).walk("VEVENT")[0]
        changed = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
        assert changed == {"LOCATION", "SEQUENCE", "LAST-MODIFIED", "DTSTAMP"}
        assert [a["TRIGGER"].dt for a in after.walk("VALARM")] == [-dt.timedelta(minutes=30)]
        # One read of that resource, then one conditional PUT with exactly its ETag.
        assert [m for m, _ in fake.requests if m in ("REPORT", "PUT", "DELETE")] == [
            "REPORT",
            "PUT",
        ]
        assert "calendar-multiget" in fake.report_bodies[-1]
        [write] = fake.writes
        assert write.headers["if-match"] == tag and "if-none-match" not in write.headers

    def test_every_write_uses_the_etag_just_read(self, service, fake):
        [ev] = self.find(service, "Dentist")
        service.update_event(ev["id"], changes=self.changes(title="Dentist 1"))
        service.update_event(ev["id"], changes=self.changes(title="Dentist 2"))
        first, second = fake.writes
        assert first.status == second.status == 204
        assert first.headers["if-match"] != second.headers["if-match"]
        assert "SUMMARY:Dentist 2" in self.home(fake).resources[0]

    def test_edit_one_occurrence(self, service, fake):
        [second] = self.find(service, "Standup", 1)
        out = service.update_event(second["id"], changes=self.changes(start=chi(1, 10)))
        assert out["start"] == chi(1, 10) and out["end"] == chi(1, 10, 15)
        assert out["recurring"] is True
        assert service.event(out["id"])["start"] == chi(1, 10)
        assert [e["start"] for w in (0, 2, 3) for e in self.find(service, "Standup", w)] == [
            chi(0, 9), chi(2, 9), chi(3, 9),
        ]  # fmt: skip
        assert [e["start"] for e in self.find(service, "Standup", 1)] == [chi(1, 10)]
        # Editing it again edits that override; no second one is added.
        service.update_event(out["id"], changes=self.changes(title="Standup (late)"))
        stored = self.home(fake).resources[1]
        assert stored is not None and stored.count("RECURRENCE-ID") == 1
        assert [e["title"] for e in self.find(service, "Standup (late)", 1)] == ["Standup (late)"]

    def test_delete_one_occurrence(self, service, fake):
        [third] = self.find(service, "Standup", 2)
        assert service.delete_event(third["id"]) == {"deleted": "occurrence", "calendar": "Home"}
        assert self.find(service, "Standup", 2) == []
        assert [len(self.find(service, "Standup", w)) for w in (0, 1, 3)] == [1, 1, 1]
        [write] = fake.writes
        assert write.method == "PUT" and "EXDATE" in write.body

    def test_delete_a_single_event(self, service, fake):
        [ev] = self.find(service, "Dentist")
        tag = etag(self.home(fake).resources[0])
        assert service.delete_event(ev["id"]) == {"deleted": "event", "calendar": "Home"}
        [write] = fake.writes
        assert (write.method, write.headers["if-match"], write.status) == ("DELETE", tag, 204)
        from pv_icloud.errors import ToolInputError

        with pytest.raises(ToolInputError, match="not found"):
            service.event(ev["id"])

    def test_conflict_keeps_the_other_version(self, service, fake):
        from pv_icloud.errors import ToolInputError

        [ev] = self.find(service, "Dentist")
        home = self.home(fake)
        other = home.resources[0].replace("SUMMARY:Dentist", "SUMMARY:Dentist (moved by phone)")
        fake.before_write = lambda: home.store("0.ics", other)
        with pytest.raises(ToolInputError, match="^the event changed in Calendar; read it again$"):
            service.update_event(ev["id"], changes=self.changes(title="Mine"))
        assert [(w.method, w.status) for w in fake.writes] == [("PUT", 412)]
        assert home.resources[0] == other

    def test_gone_event_or_occurrence(self, service, fake):
        from pv_icloud.errors import ToolInputError

        [ev] = self.find(service, "Dentist")
        [second] = self.find(service, "Standup", 1)
        self.home(fake).remove("0.ics")
        with pytest.raises(ToolInputError, match="not found"):
            service.update_event(ev["id"], changes=self.changes(title="x"))
        service.delete_event(second["id"])
        with pytest.raises(ToolInputError, match="not found"):
            service.update_event(second["id"], changes=self.changes(title="x"))
        assert [w.method for w in fake.writes] == ["PUT"]  # only the delete's EXDATE

    @pytest.mark.parametrize(
        "name", ["Banking", "Nope", "TripIt", "Family", "Kids", "Club", "SUB1"]
    )
    def test_calendars_that_cannot_be_written(self, service, fake, name):
        from pv_icloud.errors import ToolInputError

        with pytest.raises(ToolInputError, match=f"^unknown calendar: {name}$"):
            service.create_event(
                calendar=name, changes=self.changes(title="x", start=f"{NEXT_MONDAY}T09:00")
            )
        assert fake.writes == []

    def test_token_protected_calendar(self, service, fake):
        from pv_icloud.errors import ToolInputError

        args = {
            "calendar": "Work",
            "changes": self.changes(title="x", start=f"{NEXT_MONDAY}T09:00"),
        }
        with pytest.raises(ToolInputError, match="unknown calendar: Work"):
            service.create_event(**args, protected=("Work",))
        [review] = self.find(service, "Review", 1)
        with pytest.raises(ToolInputError, match="unknown calendar"):
            service.delete_event(review["id"], protected=("Work",))
        assert fake.writes == []
        service.create_event(**args)  # another token, without the fence
        assert len(fake.writes) == 1

    def test_ids_of_unwritable_calendars(self, service, make_service, fake):
        from pv_icloud.errors import ToolInputError

        [club] = self.find(service, "Club night", 1)
        [dinner] = self.find(service, "Dinner", 1)
        [bob] = self.find(make_service("bob"), "Bob", 1)
        seen = len(fake.requests)
        for event_id in (club["id"], dinner["id"], bob["id"]):
            with pytest.raises(ToolInputError, match="unknown calendar"):
                service.delete_event(event_id)
            with pytest.raises(ToolInputError, match="unknown calendar"):
                service.update_event(event_id, changes=self.changes(title="x"))
        assert fake.writes == [] and len(fake.requests) == seen  # refused before any request
        # Still readable.
        assert service.event(club["id"])["title"] == "Club night"
        assert service.event(dinner["id"])["title"] == "Dinner"
        assert not any("/222/" in path for _, path in fake.requests[seen:])

    @pytest.mark.parametrize(
        ("title", "week", "message"),
        [
            ("Board", 1, "^event has attendees; change it in Calendar$"),
            ("Invite", 1, "^this event was organised by someone else; change it in Calendar$"),
            ("Mystery", 2, "^event has attendees; change it in Calendar$"),
            ("Team sync", 2, "^event has attendees; change it in Calendar$"),
        ],
    )
    def test_events_involving_someone_else_are_refused(self, service, fake, title, week, message):
        from pv_icloud.errors import ToolInputError

        [ev] = self.find(service, title, week)
        with pytest.raises(ToolInputError, match=message):
            service.update_event(ev["id"], changes=self.changes(title="x"))
        with pytest.raises(ToolInputError, match=message):
            service.delete_event(ev["id"])
        assert fake.writes == []

    def test_events_with_only_the_account_itself(self, service, fake):
        [ev] = self.find(service, "Planning", 2)
        out = service.update_event(ev["id"], changes=self.changes(notes="agenda"))
        assert service.event(out["id"])["notes"] == "agenda"
        assert service.delete_event(out["id"])["deleted"] == "event"

    def test_input_limits_send_nothing(self, service, fake):
        from pv_icloud.errors import ToolInputError

        [ev] = self.find(service, "Dentist")
        fake.requests.clear()
        with pytest.raises(ToolInputError, match="notes"):
            service.create_event(
                calendar="Home",
                changes=self.changes(title="x", start=f"{NEXT_MONDAY}T09:00", notes="x" * 10_001),
            )
        with pytest.raises(ToolInputError, match="recurrence"):
            service.create_event(
                calendar="Home",
                changes=self.changes(title="x", start=f"{NEXT_MONDAY}T09:00"),
                recurrence={"frequency": "daily", "count": 5, "until": "2030-01-01"},
            )
        with pytest.raises(ToolInputError, match="start"):
            service.update_event(ev["id"], changes=self.changes(start="tomorrow"))
        with pytest.raises(ToolInputError, match="nothing to change"):
            service.update_event(ev["id"], changes=self.changes())
        assert fake.requests == []


# --- series edits in the calendar service (ADR-0007) ----------------------------------------


def day(week: int, weekday: int = 0, hour: int = 8) -> dt.datetime:
    return dt.datetime.combine(NEXT_MONDAY + dt.timedelta(weeks=week, days=weekday), dt.time(hour))


def stamp(value: dt.datetime) -> str:
    return f"{value:%Y%m%dT%H%M%S}"


def until(value: dt.date) -> str:
    """Apple's UNTIL for a series whose last day is `value`."""
    end = dt.datetime.combine(value, dt.time(23, 59, 59), CHI).astimezone(dt.UTC)
    return f"{end:%Y%m%dT%H%M%SZ}"


SET = "RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:SET-1"
EFFECTIVE = (NEXT_MONDAY + dt.timedelta(weeks=3)).isoformat()


def school(
    uid: str = "school", *extra: str, rule: str = "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;COUNT=40"
):
    """Eight weeks of school days, 08:00 to 15:00, with a deleted Tuesday and a moved
    Wednesday in week 1 and again in week 4 (after EFFECTIVE)."""
    return resource(
        vevent(
            uid, "School", day(0), day(0, hour=15),
            extra=(
                f"RRULE:{rule}",
                f"EXDATE;TZID=America/Chicago:{stamp(day(1, 1))},{stamp(day(4, 1))}",
                *extra,
            ),
        ),
        vevent(uid, "School", day(1, 2, 10), day(1, 2, 16),
               extra=(f"RECURRENCE-ID;TZID=America/Chicago:{stamp(day(1, 2))}",)),
        vevent(uid, "Game", day(4, 2, 10), day(4, 2, 16),
               extra=(f"RECURRENCE-ID;TZID=America/Chicago:{stamp(day(4, 2))}",)),
    )  # fmt: skip


def weekly_part(uid: str, first: dt.datetime, weeks: int | None, *extra: str, title="Club"):
    """A weekly part of recurrence set SET-1 (endless when `weeks` is None)."""
    bound = f";UNTIL={until(first.date() + dt.timedelta(weeks=weeks - 1))}" if weeks else ""
    return resource(
        vevent(uid, title, first, first + dt.timedelta(hours=1),
               extra=(f"RRULE:FREQ=WEEKLY{bound}", SET, *extra))
    )  # fmt: skip


class TestSeriesEdits:
    @pytest.fixture
    def fake(self):
        home = FakeCalendar("home", "Home", resources=[school()])
        other = FakeCalendar("other", "Other")
        fam = FakeCalendar("fam", "Family", resources=[school("fam-school")])
        shared = FakeCalendar(
            "club", "Club", types=("collection", "calendar", "shared"), privileges=("read",),
            resources=[school("club-school")],
        )  # fmt: skip
        fake = FakeCalDav(
            {
                "alice@icloud.com": FakeAccount(
                    "111", [home, other, fam, shared], addresses=("alice@icloud.com",)
                )
            }
        )
        yield fake
        assert fake.scheduling_messages == [] and fake.write_attempts == []
        assert all(w.headers["schedule-reply"] == "F" for w in fake.writes)
        for body in fake.report_bodies:  # iCloud has no property filter (2026-10-14)
            for word in ("prop-filter", "param-filter", "collation"):
                assert word not in body, word
        for w in fake.writes:  # a new series carries no one (an old part keeps its own)
            if "if-none-match" in w.headers:
                assert "ORGANIZER" not in w.body and "ATTENDEE" not in w.body

    @pytest.fixture
    def service(self, fake, tmp_path):
        from pv_icloud.cal.caldav import CalDavClient
        from pv_icloud.cal.caldav_write import CalDavWriter
        from pv_icloud.cal.service import CalendarService

        from .conftest import make_config

        cfg = make_config(
            tmp_path,
            calendar_write=True,
            alice_calendar={"write": {"protected_calendars": ["Family"]}},
        )
        acct = cfg.account("alice")
        client = CalDavClient(acct.icloud_user, acct.app_password, transport=fake.transport())
        return CalendarService(acct, client, writer=CalDavWriter(client))

    @staticmethod
    def home(fake) -> FakeCalendar:
        return fake.accounts["alice@icloud.com"].calendars[0]

    @staticmethod
    def seen(service, first_week=0, weeks=8, calendar="home"):
        start = NEXT_MONDAY + dt.timedelta(weeks=first_week)
        out = service.events(
            start=start.isoformat(),
            end=(start + dt.timedelta(weeks=weeks, days=-1)).isoformat(),
            calendars=[calendar],
            limit=200,
        )
        return [(e["start"], e["end"], e["title"], e["location"]) for e in out["events"]]

    @staticmethod
    def at(value: dt.datetime) -> str:
        return value.replace(tzinfo=CHI).isoformat()

    def id_of(self, service, value: dt.datetime, calendar="home") -> str:
        start = value.date()
        out = service.events(start=start.isoformat(), end=start.isoformat(), calendars=[calendar])
        [ev] = [e for e in out["events"] if e["start"] == self.at(value)]
        return ev["id"]

    @staticmethod
    def edit(service, event_id, mode, effective=None, *, recurrence=None, **kw):
        from pv_icloud.cal.compose import Changes

        flags = {k: kw.pop(k) for k in ("drop_unmatched", "preview", "protected") if k in kw}
        return service.update_series(
            event_id, mode=mode, effective=effective, changes=Changes(**kw),
            recurrence=recurrence, **flags,
        )  # fmt: skip

    @staticmethod
    def calls(fake, since=0):
        return [(m, k) for m, _, k in fake.calls[since:] if m in ("REPORT", "PUT", "DELETE")]

    # 4.1: the four modes on a school-day series -------------------------------------------

    def test_replace_all(self, service, fake):
        out = self.edit(service, self.id_of(service, day(0)), "replace_all", title="Term")
        seen = self.seen(service)
        assert len(seen) == 40 and {t for _, _, t, _ in seen} == {"Term"}
        assert out["exceptions"] == {"kept": 0, "discarded": 4, "unmatched": 0}
        assert (out["occurrences"], out["occurrences_through"]) == (40, None)
        assert out["parts"] == {"found": 1, "changed": 1, "split": False, "searched_from": None}
        assert "previous" not in out and "ids_changed_from" not in out
        assert service.event(out["id"])["title"] == "Term"

    def test_update_all(self, service, fake):
        out = self.edit(service, self.id_of(service, day(2)), "update_all", location="Main St")
        seen = self.seen(service)
        assert len(seen) == 38 and {loc for *_, loc in seen} == {"Main St"}  # 40 - 2 deleted
        starts = {s for s, *_ in seen}
        assert self.at(day(1, 1)) not in starts and self.at(day(4, 1)) not in starts
        assert self.at(day(1, 2, 10)) in starts and self.at(day(4, 2, 10)) in starts
        assert ("Game" in {t for _, _, t, _ in seen}) and out["exceptions"]["kept"] == 4
        assert out["id"] == self.id_of(service, day(0))  # the series' first occurrence

    def test_replace_from(self, service, fake):
        before = self.seen(service, 0, 3)
        out = self.edit(
            service, self.id_of(service, day(0)), "replace_from", EFFECTIVE,
            start=f"{EFFECTIVE}T09:00",
        )  # fmt: skip
        assert self.seen(service, 0, 3) == before
        later = self.seen(service, 3, 5)
        assert len(later) == 25 and all("T09:00:00" in s for s, *_ in later)
        assert out["exceptions"] == {"kept": 0, "discarded": 2, "unmatched": 0}
        assert out["ids_changed_from"] == EFFECTIVE
        assert out["previous"] == {"uid": "school", "last_start": self.at(day(2, 4))}
        assert out["parts"]["split"] is True and out["start"] == self.at(day(3, hour=9))
        assert service.event(out["id"])["start"] == self.at(day(3, hour=9))
        old = icalendar.Calendar.from_ical(self.home(fake).resources[0])
        [moved] = [c for c in old.walk("VEVENT") if "RECURRENCE-ID" in c]
        assert moved["DTSTART"].dt == day(1, 2, 10).replace(tzinfo=CHI)

    def test_update_from(self, service, fake):
        before = self.seen(service, 0, 3)
        out = self.edit(
            service, self.id_of(service, day(1, 3)), "update_from", EFFECTIVE, location="Annex"
        )
        assert self.seen(service, 0, 3) == before
        later = self.seen(service, 3, 5)
        assert len(later) == 24 and {loc for *_, loc in later} == {"Annex"}  # 25 - 1 deleted
        assert self.at(day(4, 1)) not in {s for s, *_ in later}
        assert (self.at(day(4, 2, 10)), self.at(day(4, 2, 16)), "Game", "Annex") in later
        [(name, text)] = self.home(fake).named.items()
        assert name == f"{out['uid']}.ics" and out["uid"] != "school"
        assert f"UID:{out['uid']}" in text and text.count("RECURRENCE-ID") == 1
        assert out["exceptions"] == {"kept": 2, "discarded": 0, "unmatched": 0}
        assert out["occurrences"] == 24 and out["ids_changed_from"] == EFFECTIVE

    def test_unmatched_through_the_service(self, service, fake):
        from pv_icloud.errors import ToolInputError

        first = self.id_of(service, day(0))
        rule = {"frequency": "weekly", "weekdays": ["MO"]}
        with pytest.raises(ToolInputError, match="^1 changed occurrences no longer fit"):
            self.edit(service, first, "update_from", EFFECTIVE, recurrence=rule)
        assert fake.writes == []
        preview = self.edit(service, first, "update_from", EFFECTIVE, recurrence=rule, preview=True)
        assert preview["requires_drop_unmatched"] is True and preview["preview"] is True
        assert [(u["kind"], u.get("title")) for u in preview["unmatched"]] == [
            ("deleted", None), ("changed", "Game"),
        ]  # fmt: skip
        assert fake.writes == []
        out = self.edit(
            service, first, "update_from", EFFECTIVE, recurrence=rule, drop_unmatched=True
        )
        assert "requires_drop_unmatched" not in out and len(out["unmatched"]) == 2
        assert "Game" not in {t for _, _, t, _ in self.seen(service)}

    def test_effective_at_the_first_occurrence_changes_in_place(self, service, fake):
        out = self.edit(
            service, self.id_of(service, day(0)), "update_from", NEXT_MONDAY.isoformat(), title="T"
        )
        assert out["parts"]["split"] is False and "ids_changed_from" not in out
        assert [k for _, k in self.calls(fake) if k.startswith("If")] == ["If-Match"]
        assert self.home(fake).named == {}

    # 4.2: split series ---------------------------------------------------------------------

    def two_parts(self, fake, second_extra=()):
        home = self.home(fake)
        home.resources = [
            weekly_part("p1", day(0, hour=18), 4),
            weekly_part("p2", day(6, hour=18), 4, *second_extra),
        ]
        return home

    def test_title_across_a_split_series(self, service, fake):
        self.two_parts(fake)
        tags = [etag(r) for r in self.home(fake).resources]
        event_id = self.id_of(service, day(1, hour=18))
        before = len(fake.calls)
        out = self.edit(service, event_id, "update_all", title="Chess")
        # Only reads until every part is read, then one PUT with If-Match per part.
        assert self.calls(fake, before) == [
            ("REPORT", "multiget"), ("REPORT", "listing"), ("REPORT", "listing"),
            ("PUT", "If-Match"), ("PUT", "If-Match"),
        ]  # fmt: skip
        assert {t for _, _, t, _ in self.seen(service, 0, 10)} == {"Chess"}
        assert len(self.seen(service, 0, 10)) == 8
        puts = [w for w in fake.writes if w.method == "PUT"]
        assert [(w.path.rsplit("/", 1)[1], w.headers["if-match"]) for w in puts] == [
            ("0.ics", tags[0]), ("1.ics", tags[1]),
        ]  # fmt: skip
        assert out["parts"]["found"] == 2 and out["parts"]["changed"] == 2
        assert (
            out["parts"]["searched_from"]
            == (dt.datetime.now(CHI).date() - dt.timedelta(days=366)).isoformat()
        )

    def test_parts_found_through_the_second_part(self, service, fake):
        self.two_parts(fake)
        fake.report_bodies.clear()
        self.edit(service, self.id_of(service, day(7, hour=18)), "update_all", title="Chess")
        listings = [b for b in fake.report_bodies if "calendar-query" in b and "expand" not in b]
        last = dt.datetime.combine(day(9).date(), dt.time(18), CHI).astimezone(dt.UTC)
        first = dt.datetime.combine(day(6).date(), dt.time(18), CHI).astimezone(dt.UTC)
        since = dt.datetime.combine(
            dt.datetime.now(CHI).date() - dt.timedelta(days=366), dt.time(), CHI
        )
        assert listings == [
            self.listing(last, None), self.listing(since.astimezone(dt.UTC), first),
        ]  # fmt: skip
        assert {t for _, _, t, _ in self.seen(service, 0, 10)} == {"Chess"}

    @staticmethod
    def listing(start, end):
        from pv_icloud.cal.caldav import _listing_body

        return _listing_body(start, end)

    @pytest.mark.parametrize(
        ("mode", "kw"),
        [("replace_all", {}), ("update_all", {"start": "2030-01-01T10:00"}),
         ("update_all", {"recurrence": {"frequency": "daily"}})],
    )  # fmt: skip
    def test_split_series_refusals(self, service, fake, mode, kw):
        from pv_icloud.errors import ToolInputError

        self.two_parts(fake)
        with pytest.raises(ToolInputError, match="^this series was split; change it from a date"):
            self.edit(service, self.id_of(service, day(0, hour=18)), mode, **kw)
        assert fake.writes == []

    def test_from_a_date_with_a_later_part(self, service, fake):
        self.two_parts(fake)
        before = len(fake.calls)
        out = self.edit(
            service, self.id_of(service, day(0, hour=18)), "update_from",
            (NEXT_MONDAY + dt.timedelta(weeks=2)).isoformat(), location="Hall",
        )  # fmt: skip
        assert [k for _, k in self.calls(fake, before) if k.startswith("If")] == [
            "If-None-Match", "If-Match", "If-Match",
        ]  # fmt: skip
        seen = self.seen(service, 0, 10)
        assert [loc for *_, loc in seen] == [None, None] + ["Hall"] * 6
        assert out["parts"] == {"found": 2, "changed": 2, "split": True, "searched_from": None}
        listings = [b for b in fake.report_bodies if "calendar-query" in b and "expand" not in b]
        lo = dt.datetime.combine(NEXT_MONDAY + dt.timedelta(weeks=2), dt.time(), CHI)
        assert listings == [self.listing(lo.astimezone(dt.UTC), None)]  # never before the date

    def test_from_the_start_of_a_later_part(self, service, fake):
        self.two_parts(fake)
        before = len(fake.calls)
        out = self.edit(
            service, self.id_of(service, day(0, hour=18)), "update_from",
            day(6).date().isoformat(), recurrence={"frequency": "weekly", "count": 2},
        )  # fmt: skip
        writes = fake.writes
        assert [(w.method, w.path.rsplit("/", 1)[1]) for w in writes] == [("PUT", "1.ics")]
        assert self.home(fake).named == {} and out["parts"]["split"] is False
        assert len(self.seen(service, 0, 10)) == 6
        assert ("REPORT", "listing") in self.calls(fake, before)

    def test_from_a_date_never_looks_back(self, service, fake):
        self.two_parts(fake)
        fake.report_bodies.clear()
        self.edit(
            service, self.id_of(service, day(0, hour=18)), "update_from",
            day(7).date().isoformat(), title="Late",
        )  # fmt: skip
        listings = [b for b in fake.report_bodies if "calendar-query" in b and "expand" not in b]
        lo = dt.datetime.combine(day(7).date(), dt.time(), CHI).astimezone(dt.UTC)
        assert listings == [self.listing(lo, None)]
        assert [w.path.rsplit("/", 1)[1] for w in fake.writes] == [
            next(iter(self.home(fake).named)), "1.ics",
        ]  # fmt: skip

    def test_later_part_fails(self, service, fake):
        from pv_icloud.errors import WriteFailed

        home = self.two_parts(fake)
        fake.put_fail["1.ics"] = 412
        with pytest.raises(WriteFailed) as failed:
            self.edit(service, self.id_of(service, day(0, hour=18)), "update_all", title="Chess")
        text = str(failed.value)
        assert "p1" in text and "p2" in text and text.index("p1") < text.index("p2")
        assert "changed in Calendar" in text
        assert "p1" not in failed.value.log_text and "p2" not in failed.value.log_text
        assert [(w.method, w.status) for w in fake.writes] == [("PUT", 204), ("PUT", 412)]
        assert "SUMMARY:Chess" in home.resources[0] and "SUMMARY:Club" in home.resources[1]

    def test_invited_later_part(self, service, fake):
        from pv_icloud.errors import ToolInputError

        self.two_parts(fake, ("ATTENDEE:mailto:carol@example.com",))
        with pytest.raises(ToolInputError, match="^event has attendees; change it in Calendar$"):
            self.edit(service, self.id_of(service, day(0, hour=18)), "update_all", title="Chess")
        assert fake.writes == []

    def test_eleven_parts(self, service, fake):
        from pv_icloud.errors import ToolInputError

        self.home(fake).resources = [weekly_part(f"p{i}", day(i, hour=18), 1) for i in range(11)]
        with pytest.raises(ToolInputError, match="more than 10 parts"):
            self.edit(service, self.id_of(service, day(0, hour=18)), "update_all", title="x")
        assert fake.writes == []

    def test_parts_in_another_calendar_are_ignored(self, service, fake):
        self.two_parts(fake)
        other = fake.accounts["alice@icloud.com"].calendars[1]
        other.resources = [weekly_part("elsewhere", day(4, hour=18), 2)]
        out = self.edit(service, self.id_of(service, day(0, hour=18)), "update_all", title="Chess")
        assert out["parts"]["found"] == 2 and "SUMMARY:Club" in other.resources[0]

    def test_part_that_ended_more_than_a_year_ago(self, service, fake):
        old_first = day(-110, hour=18)
        self.home(fake).resources = [
            weekly_part("old", old_first, 4),
            weekly_part("current", day(-100, hour=18), None),
        ]
        event_id = self.id_of(service, day(1, hour=18))
        before = len(fake.calls)
        out = self.edit(service, event_id, "update_all", title="Chess")
        assert self.calls(fake, before) == [("REPORT", "multiget"), ("PUT", "If-Match")]
        assert out["parts"]["found"] == 1
        assert (
            out["parts"]["searched_from"]
            == (dt.datetime.now(CHI).date() - dt.timedelta(days=366)).isoformat()
        )
        assert "SUMMARY:Club" in self.home(fake).resources[0]

    # 4.3: request sequences, rollback and refusals -----------------------------------------

    def test_requests_for_a_whole_series(self, service, fake):
        event_id = self.id_of(service, day(0))
        tag = etag(self.home(fake).resources[0])
        before = len(fake.calls)
        self.edit(service, event_id, "update_all", title="T")
        assert self.calls(fake, before) == [("REPORT", "multiget"), ("PUT", "If-Match")]
        assert fake.writes[0].headers["if-match"] == tag

    def test_requests_for_a_split(self, service, fake):
        event_id = self.id_of(service, day(0))
        before = len(fake.calls)
        self.edit(service, event_id, "replace_from", EFFECTIVE, title="T")
        assert self.calls(fake, before) == [
            ("REPORT", "multiget"), ("PUT", "If-None-Match"), ("PUT", "If-Match"),
        ]  # fmt: skip
        new, cut = fake.writes
        assert new.path != cut.path and cut.path.endswith("/0.ics")

    @pytest.mark.parametrize(
        ("status", "message"),
        [(412, "^the event changed in Calendar; read it again$"),
         (404, "^event not found"), (403, "^iCloud refused the change; nothing was changed$")],
    )  # fmt: skip
    def test_rollback_when_the_cut_is_refused(self, service, fake, status, message):
        from pv_icloud.errors import ToolInputError, WriteFailed

        self.two_parts(fake)
        home = self.home(fake)
        stored = list(home.resources)
        fake.put_fail["0.ics"] = status
        before = len(fake.calls)
        with pytest.raises((ToolInputError, WriteFailed), match=message):
            self.edit(
                service, self.id_of(service, day(0, hour=18)), "update_from",
                day(2).date().isoformat(), title="T",
            )  # fmt: skip
        assert home.resources == stored and home.named == {}  # as it was
        calls = self.calls(fake, before)
        assert calls[-4:] == [
            ("PUT", "If-None-Match"), ("PUT", "If-Match"), ("REPORT", "multiget"),
            ("DELETE", "If-Match"),
        ]  # fmt: skip
        new, _, delete = fake.writes
        assert delete.path == new.path and delete.headers["if-match"] == etag(new.body)
        assert "1.ics" not in {w.path.rsplit("/", 1)[1] for w in fake.writes}

    def test_series_changed_meanwhile(self, service, fake):
        from pv_icloud.errors import ToolInputError

        home = self.home(fake)
        event_id = self.id_of(service, day(0))
        other = home.resources[0].replace("SUMMARY:School\r\nDTSTART", "SUMMARY:Phone\r\nDTSTART")
        fake.before_write = lambda: home.store("0.ics", other)
        with pytest.raises(ToolInputError, match="^the event changed in Calendar; read it again$"):
            self.edit(service, event_id, "update_from", EFFECTIVE, location="x")
        assert home.resources == [other] and home.named == {}
        assert [(w.method, w.status) for w in fake.writes] == [
            ("PUT", 201), ("PUT", 412), ("DELETE", 204),
        ]  # fmt: skip

    @pytest.mark.parametrize("failure", [None, 503])  # the answer lost, or throttled
    def test_cut_unconfirmed_may_be_stored_twice(self, service, fake, failure):
        from pv_icloud.errors import WriteFailed

        self.two_parts(fake)
        event_id = self.id_of(service, day(0, hour=18))
        fake.put_fail["0.ics"] = failure
        effective = day(2).date().isoformat()
        with pytest.raises(WriteFailed) as failed:
            self.edit(service, event_id, "update_from", effective, title="T")
        text = str(failed.value)
        assert "may now be stored twice" in text and effective in text
        [name] = self.home(fake).named
        assert name.removesuffix(".ics") not in failed.value.log_text
        assert len(fake.writes) == 2  # no rollback, no later part, nothing retried

    def test_failed_rollback_may_be_stored_twice(self, service, fake):
        from pv_icloud.errors import WriteFailed

        event_id = self.id_of(service, day(0))
        fake.put_fail["0.ics"] = 412
        original = fake._write

        def write(request, acct, cal, name, body):
            if request.method == "DELETE":
                fake.delete_fail[name] = 500
            return original(request, acct, cal, name, body)

        fake._write = write
        with pytest.raises(WriteFailed, match="may now be stored twice") as failed:
            self.edit(service, event_id, "update_from", EFFECTIVE, title="T")
        [name] = self.home(fake).named
        from pv_icloud.cal.ids import decode_event_id

        ids = [w for w in str(failed.value).split() if len(w) > 30]
        assert ids and decode_event_id(ids[0].strip("().;,")).path.endswith(name)
        assert [(w.method, w.status) for w in fake.writes] == [
            ("PUT", 201), ("PUT", 412), ("DELETE", 500),
        ]  # fmt: skip

    def test_new_resource_refused_changes_nothing(self, service, fake):
        from pv_icloud.errors import WriteFailed

        event_id = self.id_of(service, day(0))
        stored = self.home(fake).resources[0]
        fake.write_fail = (403, None)
        with pytest.raises(WriteFailed, match="nothing was changed"):
            self.edit(service, event_id, "replace_from", EFFECTIVE, title="T")
        assert len(fake.writes) == 1 and self.home(fake).resources[0] == stored

    def test_preview_sends_only_reads(self, service, fake):
        stored = self.home(fake).resources[0]
        before = len(fake.calls)
        out = self.edit(service, self.id_of(service, day(0)), "replace_all", preview=True)
        assert out["preview"] is True and out["exceptions"]["discarded"] == 4
        assert out["requires_drop_unmatched"] is False
        assert {m for m, _, _ in fake.calls[before:]} <= {"REPORT", "PROPFIND"}
        assert fake.writes == [] and self.home(fake).resources[0] == stored

    def test_split_preview_lists_parts(self, service, fake):
        self.two_parts(fake)
        out = self.edit(
            service, self.id_of(service, day(0, hour=18)), "update_from",
            day(2).date().isoformat(), title="T", preview=True,
        )  # fmt: skip
        assert (
            out["parts"]["split"] is True and out["ids_changed_from"] == day(2).date().isoformat()
        )
        assert fake.writes == [] and self.home(fake).named == {}

    @pytest.mark.parametrize(
        ("extra", "message"),
        [("ATTENDEE:mailto:carol@example.com", "event has attendees"),
         ("ORGANIZER:mailto:someone@example.org", "organised by someone else")],
    )  # fmt: skip
    def test_people_refusals(self, service, fake, extra, message):
        from pv_icloud.errors import ToolInputError

        self.home(fake).resources = [school("school", extra)]
        for mode, effective in (("update_all", None), ("update_from", EFFECTIVE)):
            with pytest.raises(ToolInputError, match=message):
                self.edit(service, self.id_of(service, day(0)), mode, effective, title="x",
                          preview=True)  # fmt: skip
        assert fake.writes == []

    def test_own_organiser_is_not_copied(self, service, fake):
        own = "ORGANIZER;EMAIL=alice@icloud.com:/111/principal/"
        self.home(fake).resources = [school("school", own)]
        self.edit(service, self.id_of(service, day(0)), "update_from", EFFECTIVE, title="x")
        [new] = self.home(fake).named.values()
        assert "ORGANIZER" not in new and "ORGANIZER" in self.home(fake).resources[0]

    @pytest.mark.parametrize("calendar", ["fam", "club"])
    def test_calendars_that_cannot_be_written(self, service, fake, calendar):
        from pv_icloud.errors import ToolInputError

        event_id = self.id_of(service, day(0), calendar)
        with pytest.raises(ToolInputError, match=f"^unknown calendar: {calendar}$"):
            self.edit(service, event_id, "update_all", title="x")
        assert fake.writes == []

    def test_token_protected_calendar(self, service, fake):
        from pv_icloud.errors import ToolInputError

        with pytest.raises(ToolInputError, match="unknown calendar"):
            self.edit(
                service, self.id_of(service, day(0)), "update_all", title="x", protected=("Home",)
            )
        assert fake.writes == []

    def test_not_a_series_and_inputs(self, service, fake):
        from pv_icloud.errors import ToolInputError

        self.home(fake).resources.append(resource(vevent("one", "One", day(0, hour=20))))
        single = self.id_of(service, day(0, hour=20))
        with pytest.raises(ToolInputError, match="^not a recurring series"):
            self.edit(service, single, "update_all", title="x")
        before = len(fake.requests)
        for args, field in (
            (("this_and_following",), "mode"), (("replace_from",), "effective"),
            (("update_all", "2030-01-01"), "effective"),
        ):  # fmt: skip
            with pytest.raises(ToolInputError, match=field):
                self.edit(service, single, *args, title="x")
        assert len(fake.requests) == before  # refused before any request
