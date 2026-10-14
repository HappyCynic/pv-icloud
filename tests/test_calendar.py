# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

"""Calendar tools against the fake CalDAV server."""

from __future__ import annotations

import datetime as dt
import json
import zoneinfo

import pytest

from pv_icloud.cal.caldav import CalDavClient, CalDavError
from pv_icloud.cal.feeds import FeedClient
from pv_icloud.cal.service import CalendarService
from pv_icloud.config import ConfigError
from pv_icloud.errors import ToolInputError

from .conftest import make_config
from .fakecaldav import FakeAccount, FakeCalDav, FakeCalendar, FakeFeeds, resource, vevent

CHI = zoneinfo.ZoneInfo("America/Chicago")
TODAY = dt.datetime.now(CHI).date()


def day(n: int) -> dt.date:
    return TODAY + dt.timedelta(days=n)


def at(n: int, hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime.combine(day(n), dt.time(hour, minute))


LONG_NOTES = "Agenda " + "x" * 1500
TRIPIT_SOURCE = "webcal://www.tripit.example/feed/ical/private/SECRET-TOKEN-123/tripit.ics"
TRIPIT_URL = "https://www.tripit.example/feed/ical/private/SECRET-TOKEN-123/tripit.ics"


def tripit_feed() -> str:
    # Outside the default 7-day window, so calendar_events' defaults see only CalDAV events.
    return resource(
        vevent(
            "trip-flight",
            "Flight NH 11 to Tokyo",
            at(20, 15),
            at(21, 19),
            tz="UTC",
            extra=("LOCATION:ORD", "DESCRIPTION:Confirmation ABC123"),
        ),
        vevent("trip-hotel", "Hotel Okura", day(21), day(24)),
    )


def alice_calendars() -> list[FakeCalendar]:
    standup = [
        vevent("standup", "Standup", at(n, 9), at(n, 9, 15), recurrence_id=n != 1)
        for n in (1, 8, 15)  # one resource, expanded; first instance has no RECURRENCE-ID
    ]
    home = FakeCalendar(
        "home",
        "Home",
        resources=[
            resource(*standup),
            resource(vevent("vac", "Vacation", day(3), day(5))),
            resource(vevent("call", "Call mom", at(2, 18), at(2, 19), tz=None)),
            resource(
                vevent(
                    "flight",
                    "Flight to LHR",
                    at(5, 8),
                    at(5, 10),
                    tz="Europe/London",
                    extra=(
                        f"DESCRIPTION:{LONG_NOTES}",
                        "LOCATION:Heathrow\\, Terminal 5",
                        'ORGANIZER;CN="Doe, Jane":mailto:jane@example.com',
                        "ATTENDEE;CN=Bob;PARTSTAT=ACCEPTED:mailto:bob@example.com",
                        "ATTENDEE;PARTSTAT=NEEDS-ACTION:mailto:carol@example.com",
                        "STATUS:CONFIRMED",
                    ),
                )
            ),
            resource(vevent("old", "Long ago", at(-30, 9), at(-30, 10))),
            resource(vevent("diner", "Dîner chez Zoë", at(40, 19), at(40, 21))),
            resource(vevent("nostart", "No start", at(1, 9)).replace("DTSTART", "X-DTSTART")),
            "this is not iCalendar",
        ],
    )
    work = FakeCalendar(
        "1b77shared",
        "Work",
        types=("collection", "calendar", "shared"),
        resources=[resource(vevent("board", "Board meeting", at(1, 18), at(1, 19), tz="UTC"))],
    )
    return [
        home,
        work,
        FakeCalendar("BANK", "Banking", resources=[resource(vevent("b", "Loan", at(1, 10)))]),
        FakeCalendar("rem", "Reminders ⚠️", comps=("VTODO",)),
        FakeCalendar("SUB1", "TripIt", types=("collection", "subscribed"), source=TRIPIT_SOURCE),
    ]


@pytest.fixture
def fake():
    return FakeCalDav(
        {
            "alice@icloud.com": FakeAccount("111", alice_calendars()),
            "bob@icloud.com": FakeAccount(
                "222",
                [
                    FakeCalendar(
                        "home",
                        "Bob Home",
                        resources=[resource(vevent("d", "bob secret dentist", at(1, 9)))],
                    )
                ],
            ),
        }
    )


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def feeds():
    f = FakeFeeds()
    f.feeds[TRIPIT_URL] = tripit_feed()
    return f


@pytest.fixture
def service(fake, feeds, tmp_path, clock):
    cfg = make_config(tmp_path, alice_calendar={"calendars": {"exclude": ["Banking"]}})
    acct = cfg.account("alice")
    client = CalDavClient(acct.icloud_user, acct.app_password, transport=fake.transport())
    feed_client = FeedClient(transport=feeds.transport(), resolver=feeds.resolve, clock=clock)
    return CalendarService(acct, client, feed_client, clock=clock)


def titles(out: dict) -> list[str]:
    return [e["title"] for e in out["events"]]


class TestCalendarList:
    def test_lists_event_calendars_and_subscriptions_in_scope(self, service, fake):
        out = service.calendars()
        rows = {c["name"]: c for c in out["calendars"]}
        assert list(rows) == ["Home", "Work", "TripIt"]  # no Banking, Reminders, inbox/outbox
        assert rows["Home"] == {
            "id": "home",
            "name": "Home",
            "color": "#0088FF",
            "kind": "calendar",
            "shared_with_me": False,
            "timezone": "America/Chicago",
            "events_available": True,
        }
        assert rows["Work"]["shared_with_me"] is True
        assert rows["TripIt"]["kind"] == "subscribed"
        assert rows["TripIt"]["events_available"] is True
        assert rows["TripIt"]["source_host"] == "www.tripit.example"
        assert "note" not in out
        assert "SECRET-TOKEN" not in json.dumps(out)  # feed URLs can embed private tokens
        assert out["default_timezone"] == "America/Chicago"
        # Discovery moved to the per-account host; nothing but PROPFIND was sent.
        assert {m for m, _ in fake.requests} == {"PROPFIND"}
        assert any(path.startswith("p99-caldav.icloud.com") for _, path in fake.requests)

    def test_list_is_cached_for_five_minutes(self, service, fake, clock):
        service.calendars()
        n = len(fake.requests)
        service.calendars()
        assert len(fake.requests) == n
        clock.now += 301
        service.calendars()
        assert len(fake.requests) == n + 1  # home URL stays cached; only the listing repeats


class TestEvents:
    def test_default_range_is_next_seven_days(self, service, fake):
        out = service.events()
        assert titles(out) == [
            "Standup",
            "Board meeting",
            "Call mom",
            "Vacation",
            "Flight to LHR",
        ]
        assert out["range"]["timezone"] == "America/Chicago"
        assert out["range"]["start"] == dt.datetime.combine(TODAY, dt.time(), CHI).isoformat()
        assert out["calendars_searched"] == ["Home", "Work", "TripIt"]
        assert out["total"] == 5 and out["next_offset"] is None
        assert all("<c:expand" in body for body in fake.report_bodies)
        assert not fake.write_attempts

    def test_event_shapes(self, service):
        ev = {e["title"]: e for e in service.events()["events"]}
        standup = ev["Standup"]
        assert standup["start"] == at(1, 9).replace(tzinfo=CHI).isoformat()
        assert standup["timezone"] == "America/Chicago"
        assert standup["recurring"] is True and standup["all_day"] is False

        vacation = ev["Vacation"]
        assert vacation["all_day"] is True
        assert (vacation["start"], vacation["end"]) == (day(3).isoformat(), day(4).isoformat())
        assert "timezone" not in vacation and vacation["recurring"] is False

        call = ev["Call mom"]
        assert call["start"] == at(2, 18).isoformat() and call["timezone"] is None
        assert "floating" in call["timezone_note"]

        flight = ev["Flight to LHR"]
        assert flight["timezone"] == "Europe/London"
        assert flight["start"].endswith(("+00:00", "+01:00"))
        assert flight["location"] == "Heathrow, Terminal 5"
        assert len(flight["notes"]) == 1000 and flight["notes_truncated"] is True
        assert flight["organizer"] == {"name": "Doe, Jane", "email": "jane@example.com"}
        assert flight["attendees"] == [
            {"name": "Bob", "email": "bob@example.com", "status": "accepted"},
            {"name": None, "email": "carol@example.com", "status": "needs-action"},
        ]
        assert flight["status"] == "confirmed"

        board = ev["Board meeting"]
        assert board["timezone"] == "UTC" and board["calendar"] == "Work"

    def test_date_only_end_is_inclusive(self, service):
        one_day = service.events(start=day(3).isoformat(), end=day(3).isoformat())
        assert titles(one_day) == ["Vacation"]
        assert titles(service.events(start=day(8).isoformat(), end=day(8).isoformat())) == [
            "Standup"
        ]

    def test_datetime_end_is_exclusive_and_naive_means_default_zone(self, service):
        out = service.events(
            start=at(1, 0).isoformat(), end=at(1, 9).isoformat()
        )  # standup starts exactly at the exclusive end
        assert titles(out) == []
        out = service.events(start=at(1, 9).isoformat(), end=at(1, 9, 1).isoformat())
        assert titles(out) == ["Standup"]
        assert out["range"]["start"] == at(1, 9).replace(tzinfo=CHI).isoformat()

    def test_range_validation(self, service):
        with pytest.raises(ToolInputError, match="after start"):
            service.events(start=day(3).isoformat(), end=day(1).isoformat())
        with pytest.raises(ToolInputError, match="90 days"):
            service.events(start=day(0).isoformat(), end=day(91).isoformat())
        with pytest.raises(ToolInputError, match="ISO"):
            service.events(start="next tuesday")
        with pytest.raises(ToolInputError, match="limit"):
            service.events(limit=0)

    def test_query_and_limit(self, service):
        assert titles(service.events(query="heathrow")) == ["Flight to LHR"]
        assert titles(service.events(query="AGENDA lhr")) == ["Flight to LHR"]
        assert titles(service.events(query="heathrow dentist")) == []

    def test_paging_walks_all_events_once(self, service):
        everything = titles(service.events())
        seen: list[str] = []
        offset: int | None = 0
        while offset is not None:
            out = service.events(limit=2, offset=offset)
            assert out["total"] == 5 and out["offset"] == offset
            seen += titles(out)
            offset = out["next_offset"]
        assert seen == everything
        assert service.events(offset=99)["events"] == []
        with pytest.raises(ToolInputError, match="offset"):
            service.events(offset=-1)

    def test_calendar_filter(self, service):
        assert titles(service.events(calendars=["work"])) == ["Board meeting"]
        assert titles(service.events(calendars=["1b77shared"])) == ["Board meeting"]
        with pytest.raises(ToolInputError, match="unknown calendar: Nope"):
            service.events(calendars=["Nope"])
        with pytest.raises(ToolInputError, match="unknown calendar: Banking"):
            service.events(calendars=["Banking"])  # excluded looks the same as unknown
        assert titles(service.events(calendars=["TripIt"])) == []
        trip = service.events(
            calendars=["tripit"], start=day(20).isoformat(), end=day(22).isoformat()
        )
        assert titles(trip) == ["Flight NH 11 to Tokyo", "Hotel Okura"]

    def test_excluded_calendar_is_never_queried(self, service, fake):
        service.events()
        assert not any("/BANK/" in path for m, path in fake.requests if m == "REPORT")

    def test_one_failing_calendar_is_reported_not_fatal(self, service, fake, feeds):
        fake.accounts["alice@icloud.com"].calendars[1].fail = 500
        out = service.events()
        assert "Board meeting" not in titles(out)
        assert out["errors"] == [
            {"calendar": "Work", "error": "REPORT /111/calendars/1b77shared/: HTTP 500"}
        ]

    def test_all_calendars_failing_is_an_error(self, service, fake, feeds):
        for cal in fake.accounts["alice@icloud.com"].calendars[:2]:
            cal.fail = 503
        feeds.status[TRIPIT_URL] = 502  # never fetched before, so no cached copy to serve
        with pytest.raises(CalDavError):
            service.events()


class TestSubscriptions:
    def test_feed_events_shape(self, service, feeds):
        out = service.events(start=day(20).isoformat(), end=day(25).isoformat())
        ev = {e["title"]: e for e in out["events"]}
        flight = ev["Flight NH 11 to Tokyo"]
        assert flight["calendar"] == "TripIt" and flight["timezone"] == "UTC"
        assert flight["location"] == "ORD" and flight["recurring"] is False
        hotel = ev["Hotel Okura"]
        assert (hotel["start"], hotel["end"]) == (day(21).isoformat(), day(23).isoformat())
        # No iCloud credentials go to the feed publisher.
        assert all("authorization" not in r.headers for r in feeds.requests)

    def test_feed_cached_then_revalidated(self, service, feeds, clock):
        feeds.etags[TRIPIT_URL] = '"v1"'
        window = {"start": day(20).isoformat(), "end": day(22).isoformat()}
        service.events(**window)
        service.events(**window)
        assert len(feeds.requests) == 1  # cached for 15 minutes
        clock.now += 15 * 60 + 1
        assert "Flight NH 11 to Tokyo" in titles(service.events(**window))
        assert len(feeds.requests) == 2
        assert feeds.requests[-1].headers["if-none-match"] == '"v1"'  # answered 304

    def test_feed_failure_is_reported_not_fatal(self, service, feeds):
        feeds.status[TRIPIT_URL] = 500
        out = service.events()
        assert titles(out)[0] == "Standup"
        assert out["errors"] == [{"calendar": "TripIt", "error": "www.tripit.example: HTTP 500"}]
        assert "SECRET-TOKEN" not in json.dumps(out)

    def test_feed_event_detail(self, service):
        out = service.events(
            calendars=["TripIt"], start=day(20).isoformat(), end=day(21).isoformat()
        )
        detail = service.event(out["events"][0]["id"])
        assert detail["title"] == "Flight NH 11 to Tokyo"
        assert detail["notes"] == "Confirmation ABC123" and detail["next_offset"] is None

    def test_subscriptions_off(self, fake, feeds, tmp_path):
        cfg = make_config(tmp_path, alice_calendar={"subscriptions": False})
        acct = cfg.account("alice")
        svc = CalendarService(
            acct,
            CalDavClient(acct.icloud_user, acct.app_password, transport=fake.transport()),
            FeedClient(transport=feeds.transport(), resolver=feeds.resolve),
        )
        rows = {c["name"]: c for c in svc.calendars()["calendars"]}
        assert rows["TripIt"]["events_available"] is False and "note" in svc.calendars()
        assert "TripIt" not in svc.events()["calendars_searched"]
        with pytest.raises(ToolInputError, match="turned off"):
            svc.events(calendars=["TripIt"])
        assert not feeds.requests


class TestSearch:
    def test_searches_all_calendars_over_a_long_range(self, service):
        out = service.search(query="tokyo")
        assert titles(out) == ["Flight NH 11 to Tokyo"]
        assert (
            out["range"]["start"]
            == dt.datetime.combine(TODAY - dt.timedelta(days=365), dt.time(), CHI).isoformat()
        )
        assert "TripIt" in out["calendars_searched"] and "Home" in out["calendars_searched"]
        # Recurring series are expanded across the whole range.
        assert titles(service.search(query="standup")) == ["Standup"] * 3

    def test_matches_people_notes_and_calendar_name(self, service):
        assert titles(service.search(query="jane")) == ["Flight to LHR"]  # organizer name
        assert titles(service.search(query="carol@example.com")) == ["Flight to LHR"]
        assert titles(service.search(query="abc123")) == ["Flight NH 11 to Tokyo"]
        assert titles(service.search(query="work")) == ["Board meeting"]  # calendar name
        assert titles(service.search(query="tokyo hotel")) == []  # every word must match
        # Case and accents are ignored, both ways.
        assert titles(service.search(query="DINER zoe")) == ["Dîner chez Zoë"]
        assert titles(service.search(query="dîner ZOË")) == ["Dîner chez Zoë"]

    def test_range_and_paging_rules(self, service):
        upcoming = service.search(query="standup", start=day(2).isoformat())
        assert len(upcoming["events"]) == 2  # day 8 and day 15
        page = service.search(query="standup", limit=1, offset=1)
        assert page["total"] == 3 and page["next_offset"] == 2
        with pytest.raises(ToolInputError, match="730 days"):
            service.search(query="x", start=day(0).isoformat(), end=day(731).isoformat())
        with pytest.raises(ToolInputError, match="query"):
            service.search(query="  ")


class TestEvent:
    def by_title(self, service, title, **kw):
        return next(e for e in service.events(**kw)["events"] if e["title"] == title)

    def test_full_notes_paged_and_all_attendees(self, service, fake):
        flight = self.by_title(service, "Flight to LHR")
        out = service.event(flight["id"])
        assert out["notes"] == LONG_NOTES and out["next_offset"] is None
        assert out["notes_total_chars"] == len(LONG_NOTES)
        assert out["title"] == "Flight to LHR" and out["timezone"] == "Europe/London"
        pages, offset = [], 0
        while offset is not None:
            page = service.event(flight["id"], max_chars=600, offset=offset)
            pages.append(page["notes"])
            offset = page["next_offset"]
        assert "".join(pages) == LONG_NOTES and len(pages) == 3
        assert {m for m, _ in fake.requests} <= {"PROPFIND", "REPORT"}

    def test_attendees_capped_in_list_but_complete_in_detail(self, service, fake):
        people = tuple(f"ATTENDEE;CN=P{i}:mailto:p{i}@example.com" for i in range(60))
        fake.accounts["alice@icloud.com"].calendars[0].resources.append(
            resource(vevent("big", "All hands", at(2, 9), at(2, 10), extra=people))
        )
        listed = self.by_title(service, "All hands")
        assert len(listed["attendees"]) == 50 and listed["attendees_total"] == 60
        detail = service.event(listed["id"])
        assert len(detail["attendees"]) == 60 and "attendees_total" not in detail

    def test_picks_the_right_occurrence(self, service):
        week = self.by_title(service, "Standup", start=day(8).isoformat(), end=day(8).isoformat())
        first = self.by_title(service, "Standup")
        assert week["id"] != first["id"]
        assert service.event(week["id"])["start"] == at(8, 9).replace(tzinfo=CHI).isoformat()
        assert service.event(first["id"])["start"] == at(1, 9).replace(tzinfo=CHI).isoformat()

    def test_all_day_and_no_notes(self, service):
        vac = service.event(self.by_title(service, "Vacation")["id"])
        assert vac["all_day"] and vac["notes"] is None and vac["next_offset"] is None

    def test_tampered_ids_are_rejected(self, service):
        from pv_icloud.cal.ids import EventId, decode_event_id

        good = decode_event_id(self.by_title(service, "Vacation")["id"])
        cases = {
            "unknown calendar": EventId("BANK", good.start, "/111/calendars/BANK/0.ics"),
            "malformed": EventId("home", good.start, "/111/calendars/1b77shared/0.ics"),
        }
        for msg, eid in cases.items():
            with pytest.raises(ToolInputError, match=msg):
                service.event(eid.encode())
        for path in (
            "/111/calendars/home/../BANK/0.ics",
            "/111/calendars/home/x%2F..%2F..%2FBANK%2F0.ics",
            "/111/calendars/home/",
        ):
            with pytest.raises(ToolInputError, match="malformed"):
                service.event(EventId("home", good.start, path).encode())
        for bad in ("%%%", "bm9waXBlcw"):
            with pytest.raises(ToolInputError, match="malformed"):
                service.event(bad)

    def test_deleted_event_not_found(self, service, fake):
        vac = self.by_title(service, "Vacation")
        fake.accounts["alice@icloud.com"].calendars[0].resources[1] = resource(
            vevent("other", "Other", day(20), day(21))
        )
        with pytest.raises(ToolInputError, match="not found"):
            service.event(vac["id"])

    def test_argument_limits(self, service):
        vid = self.by_title(service, "Vacation")["id"]
        with pytest.raises(ToolInputError, match="max_chars"):
            service.event(vid, max_chars=20001)
        with pytest.raises(ToolInputError, match="offset"):
            service.event(vid, offset=-1)


class TestPrincipalPathPeople:
    """iCloud stores the account's own address as its principal path with `EMAIL=`."""

    @pytest.fixture
    def principal_event(self, fake):
        fake.accounts["alice@icloud.com"].calendars[0].resources.append(
            resource(
                vevent(
                    "own",
                    "Planning",
                    at(2, 9),
                    at(2, 10),
                    extra=(
                        "ORGANIZER;CN=Alice;EMAIL=alice@example.net:/111/principal/",
                        "ATTENDEE;CN=Alice;PARTSTAT=ACCEPTED;EMAIL=alice@example.net:"
                        "/111/principal/",
                        "ATTENDEE;CN=Nobody:urn:uuid:00000000-0000-0000-0000-000000000000",
                    ),
                )
            )
        )

    def test_email_comes_from_the_email_parameter(self, service, principal_event):
        [ev] = [e for e in service.events()["events"] if e["title"] == "Planning"]
        assert ev["organizer"] == {"name": "Alice", "email": "alice@example.net"}
        assert ev["attendees"] == [
            {"name": "Alice", "email": "alice@example.net", "status": "accepted"},
            {"name": "Nobody", "email": None},
        ]
        detail = service.event(ev["id"])
        assert detail["organizer"]["email"] == "alice@example.net"
        assert titles(service.search(query="alice@example.net")) == ["Planning"]


class TestWriteReads:
    """Read-only additions for calendar writes: privileges, one stored resource, addresses."""

    def test_calendar_list_carries_privileges_without_extra_requests(self, service, fake):
        fake.accounts["alice@icloud.com"].calendars[1].privileges = ("read",)
        before = service.calendars()
        cals = {c.name: c for c in service.client.calendars()}
        assert cals["Home"].writable is True
        assert cals["Work"].writable is False  # shared read-only
        assert cals["TripIt"].writable is False  # subscribed
        # The listing asks for the privileges in its one PROPFIND; calendar_list is unchanged.
        assert "writable" not in json.dumps(before)
        assert {m for m, _ in fake.requests} == {"PROPFIND"}

    def test_privileges_imply_write_content(self):
        from pv_icloud.cal.caldav import _writable

        assert _writable({"write-content"}) and _writable({"write"}) and _writable({"all"})
        assert not _writable({"read", "write-properties", "bind"})

    def test_one_resource_read_unexpanded_with_its_etag(self, service, fake):
        from .fakecaldav import etag

        cal = next(c for c in service.client.calendars() if c.id == "home")
        home = fake.accounts["alice@icloud.com"].calendars[0]
        fake.requests.clear()
        got = service.client.event_resource(cal, "/111/calendars/home/1.ics")
        stored = home.resources[1]
        assert got is not None and stored is not None
        # XML parsing turns the CRLF line ends into LF; icalendar reads either.
        assert got == (stored.replace("\r\n", "\n"), etag(stored))
        assert service.client.event_resource(cal, "/111/calendars/home/99.ics") is None
        assert [m for m, _ in fake.requests] == ["REPORT", "REPORT"]
        assert all("calendar-multiget" in b and "<c:expand" not in b for b in fake.report_bodies)

    def test_account_addresses_read_once(self, service, fake):
        fake.accounts["alice@icloud.com"].addresses = ("alice@icloud.com", "Alice@Example.net")
        first = service.client.account_addresses()
        n = len(fake.requests)
        assert service.client.account_addresses() == first
        assert len(fake.requests) == n
        assert first.emails == frozenset({"alice@icloud.com", "alice@example.net"})
        assert first.principals == frozenset({"/111/principal/"})
        assert {m for m, _ in fake.requests} == {"PROPFIND"}


class TestClient:
    def test_refuses_write_methods(self, service):
        import httpx

        with pytest.raises(RuntimeError, match="non-read"):
            service.client._request("PUT", httpx.URL("https://caldav.icloud.com/"), "", "0")

    def test_bad_credentials(self, fake):
        client = CalDavClient("alice@icloud.com", "wrong", transport=fake.transport())
        with pytest.raises(CalDavError, match="rejected"):
            client.calendars()


class TestConfig:
    def test_timezone_override(self, service, tmp_path, fake):
        cfg = make_config(tmp_path, alice_calendar={"timezone": "Europe/London"})
        acct = cfg.account("alice")
        svc = CalendarService(
            acct, CalDavClient(acct.icloud_user, acct.app_password, transport=fake.transport())
        )
        assert svc.events()["range"]["timezone"] == "Europe/London"

    def test_bad_timezone_rejected(self, tmp_path):
        with pytest.raises(ConfigError, match="time zone"):
            make_config(tmp_path, alice_calendar={"timezone": "Mars/Olympus"})
