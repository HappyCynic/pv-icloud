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
