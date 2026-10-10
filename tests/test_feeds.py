# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

"""Subscribed-calendar feeds: local recurrence expansion and the guarded fetcher."""

from __future__ import annotations

import datetime as dt
import zoneinfo

import httpx
import icalendar
import pytest

from pv_icloud.cal.expand import expand
from pv_icloud.cal.feeds import FEED_MAX_BYTES, FeedClient, FeedError, feed_url

from .fakecaldav import FakeFeeds

CHI = zoneinfo.ZoneInfo("America/Chicago")
UTC = dt.UTC


def cal(*events: str) -> icalendar.Calendar:
    body = "\r\n".join(["BEGIN:VCALENDAR", "VERSION:2.0", *events, "END:VCALENDAR", ""])
    return icalendar.Calendar.from_ical(body)


def ev(*lines: str) -> str:
    return "\r\n".join(["BEGIN:VEVENT", *lines, "END:VEVENT"])


def window(start: str, end: str) -> tuple[dt.datetime, dt.datetime]:
    return (
        dt.datetime.fromisoformat(start).replace(tzinfo=UTC),
        dt.datetime.fromisoformat(end).replace(tzinfo=UTC),
    )


def starts(c: icalendar.Calendar, start: str, end: str) -> list[str]:
    return sorted(o.start.isoformat() for o in expand(c, *window(start, end), CHI))


class TestExpand:
    def test_weekly_rule_keeps_wall_clock_across_dst(self):
        # US DST ends 2026-11-01: 09:00 Chicago is 14:00 UTC before and 15:00 UTC after.
        c = cal(
            ev(
                "UID:w",
                "DTSTART;TZID=America/Chicago:20261022T090000",
                "DTEND;TZID=America/Chicago:20261022T100000",
                "RRULE:FREQ=WEEKLY;BYDAY=TH",
            )
        )
        got = starts(c, "2026-10-20", "2026-11-10")
        assert got == [
            "2026-10-22T09:00:00-05:00",
            "2026-10-29T09:00:00-05:00",
            "2026-11-05T09:00:00-06:00",
        ]
        occ = expand(c, *window("2026-11-05", "2026-11-06"), CHI)[0]
        assert occ.recurring and occ.end - occ.start == dt.timedelta(hours=1)

    def test_until_in_utc_count_exdate_rdate(self):
        c = cal(
            ev(
                "UID:u",
                "DTSTART;TZID=America/Chicago:20261001T090000",
                "RRULE:FREQ=DAILY;UNTIL=20261005T140000Z",  # = 09:00 Chicago on the 5th
                "EXDATE;TZID=America/Chicago:20261003T090000",
                "RDATE:20261010T150000Z",
            ),
            ev("UID:c", "DTSTART:20261001T120000Z", "RRULE:FREQ=DAILY;COUNT=2"),
        )
        assert starts(c, "2026-09-30", "2026-10-31") == [
            "2026-10-01T09:00:00-05:00",
            "2026-10-01T12:00:00+00:00",
            "2026-10-02T09:00:00-05:00",
            "2026-10-02T12:00:00+00:00",
            "2026-10-04T09:00:00-05:00",
            "2026-10-05T09:00:00-05:00",
            "2026-10-10T10:00:00-05:00",
        ]

    def test_overrides_move_and_cancel_occurrences(self):
        c = cal(
            ev(
                "UID:m",
                "SUMMARY:Practice",
                "DTSTART;TZID=America/Chicago:20261005T170000",
                "DTEND;TZID=America/Chicago:20261005T180000",
                "RRULE:FREQ=WEEKLY;COUNT=4",
            ),
            # The 12th moves to the 14th; the 19th moves out of the window entirely.
            ev(
                "UID:m",
                "SUMMARY:Practice (moved)",
                "RECURRENCE-ID;TZID=America/Chicago:20261012T170000",
                "DTSTART;TZID=America/Chicago:20261014T180000",
                "DTEND;TZID=America/Chicago:20261014T190000",
            ),
            ev(
                "UID:m",
                "RECURRENCE-ID;TZID=America/Chicago:20261019T170000",
                "DTSTART;TZID=America/Chicago:20261201T170000",
            ),
        )
        occs = sorted(expand(c, *window("2026-10-01", "2026-10-25"), CHI), key=lambda o: o.start)
        assert [(o.start.isoformat(), str(o.component["SUMMARY"])) for o in occs] == [
            ("2026-10-05T17:00:00-05:00", "Practice"),
            ("2026-10-14T18:00:00-05:00", "Practice (moved)"),
        ]
        assert all(o.recurring for o in occs)

    def test_all_day_yearly_and_floating(self):
        c = cal(
            ev("UID:b", "DTSTART;VALUE=DATE:20200315", "RRULE:FREQ=YEARLY"),
            ev("UID:f", "DTSTART:20261010T080000", "RRULE:FREQ=DAILY;COUNT=2"),
        )
        occs = {o.start.isoformat(): o for o in expand(c, *window("2026-01-01", "2026-12-31"), CHI)}
        assert set(occs) == {"2026-03-15", "2026-10-10T08:00:00", "2026-10-11T08:00:00"}
        assert occs["2026-03-15"].end == dt.date(2026, 3, 16)

    def test_dtstart_counts_even_off_pattern_and_bad_rules_are_contained(self):
        c = cal(
            # DTSTART is a Wednesday but the rule says Fridays.
            ev("UID:x", "DTSTART:20261007T120000Z", "RRULE:FREQ=WEEKLY;BYDAY=FR;COUNT=2"),
            ev("UID:s", "DTSTART:20261007T120000Z", "RRULE:FREQ=MINUTELY"),
            ev("UID:bad", "DTSTART:20261007T120000Z", "RRULE:FREQ=NONSENSE"),
            ev("SUMMARY:no uid", "DTSTART:20261008T120000Z"),
        )
        got = expand(c, *window("2026-10-06", "2026-10-20"), CHI)
        by_uid: dict[str, list[str]] = {}
        for o in got:
            by_uid.setdefault(o.uid, []).append(o.start.date().isoformat())
        assert sorted(by_uid["x"]) == ["2026-10-07", "2026-10-09", "2026-10-16"]
        assert by_uid["s"] == ["2026-10-07"]  # sub-hourly rule not expanded
        assert by_uid["bad"] == ["2026-10-07"]
        [nouid] = [u for u in by_uid if u.startswith("nouid-")]
        assert by_uid[nouid] == ["2026-10-08"]

    def test_only_overlapping_single_events(self):
        c = cal(
            ev("UID:in", "DTSTART:20261010T120000Z", "DTEND:20261010T130000Z"),
            ev("UID:out", "DTSTART:20250101T120000Z"),
            ev("UID:span", "DTSTART;VALUE=DATE:20261001", "DTEND;VALUE=DATE:20261101"),
        )
        assert {o.uid for o in expand(c, *window("2026-10-10", "2026-10-11"), CHI)} == {
            "in",
            "span",
        }


class TestFeedClient:
    URL = "https://cal.example/feed.ics"

    @pytest.fixture
    def feeds(self):
        f = FakeFeeds()
        f.feeds[self.URL] = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n"
        return f

    def client(self, feeds, **kw):
        return FeedClient(transport=feeds.transport(), resolver=feeds.resolve, **kw)

    def test_webcal_becomes_https_and_bad_schemes_refused(self):
        assert str(feed_url("webcal://cal.example/a.ics")) == "https://cal.example/a.ics"
        assert str(feed_url("WEBCALS://cal.example/a.ics")) == "https://cal.example/a.ics"
        assert feed_url("http://cal.example/a.ics").scheme == "http"
        for bad in ("file:///etc/passwd", "ftp://cal.example/a", "webcal://"):
            with pytest.raises(FeedError):
                feed_url(bad)

    @pytest.mark.parametrize(
        "ip", ["127.0.0.1", "10.1.2.3", "192.168.1.10", "169.254.169.254", "::1", "fd00::1"]
    )
    def test_refuses_non_public_addresses(self, feeds, ip):
        feeds.dns["cal.example"] = ip
        with pytest.raises(FeedError, match="non-public"):
            self.client(feeds).get(self.URL)
        assert not feeds.requests

    def test_redirects_are_checked_hop_by_hop(self, feeds):
        feeds.redirects[self.URL] = "https://internal.example/x.ics"
        feeds.dns["internal.example"] = "10.0.0.5"
        with pytest.raises(FeedError, match="non-public"):
            self.client(feeds).get(self.URL)
        feeds.redirects[self.URL] = "http://cal.example/plain.ics"
        with pytest.raises(FeedError, match="off https"):
            self.client(feeds).get(self.URL)
        feeds.redirects[self.URL] = "https://cal.example/moved.ics"
        feeds.feeds["https://cal.example/moved.ics"] = feeds.feeds[self.URL]
        assert self.client(feeds).get(self.URL) is not None

    def test_connects_to_the_checked_address(self, feeds):
        # A rebinding name answers public first, then private: the request still goes to
        # the address that passed the check, with the name in Host and SNI.
        feeds.dns["cal.example"] = "93.184.215.14"
        feeds.rebind["cal.example"] = "10.0.0.5"
        self.client(feeds).get(self.URL)
        [request] = feeds.requests
        assert request.url.host == "93.184.215.14"
        assert request.headers["host"] == "cal.example"
        assert request.extensions["sni_hostname"] == "cal.example"

    def test_tries_next_checked_address_when_one_refuses(self, feeds):
        def resolve(host, port):
            return ["2001:db8::1", "93.184.215.14"]

        def handle(request):
            if request.url.host == "2001:db8::1":
                raise httpx.ConnectError("unreachable")
            assert request.headers["host"] == "cal.example"
            return httpx.Response(200, content=feeds.feeds[self.URL].encode())

        with pytest.raises(FeedError, match="non-public"):  # 2001:db8:: is documentation space
            FeedClient(transport=httpx.MockTransport(handle), resolver=resolve).get(self.URL)

        def resolve_public(host, port):
            return ["2606:4700::1", "93.184.215.14"]

        def handle_public(request):
            if request.url.host == "2606:4700::1":
                raise httpx.ConnectError("unreachable")
            return httpx.Response(200, content=feeds.feeds[self.URL].encode())

        client = FeedClient(transport=httpx.MockTransport(handle_public), resolver=resolve_public)
        assert client.get(self.URL) is not None

    def test_redirect_loop_and_size_cap(self, feeds):
        feeds.redirects[self.URL] = self.URL
        with pytest.raises(FeedError, match="too many redirects"):
            self.client(feeds).get(self.URL)
        del feeds.redirects[self.URL]
        feeds.feeds[self.URL] = "X" * (FEED_MAX_BYTES + 1)
        with pytest.raises(FeedError, match="larger than"):
            self.client(feeds).get(self.URL)

    def test_empty_and_garbage_feeds(self, feeds):
        feeds.feeds[self.URL] = ""
        assert list(self.client(feeds).get(self.URL).walk("VEVENT")) == []
        feeds.feeds[self.URL] = "<html>login required</html>"
        with pytest.raises(FeedError, match="not an iCalendar"):
            self.client(feeds).get(self.URL)

    def test_serves_stale_copy_when_publisher_fails(self, feeds):
        now = [0.0]
        client = self.client(feeds, clock=lambda: now[0])
        first = client.get(self.URL)
        now[0] += 3600
        feeds.status[self.URL] = 503
        assert client.get(self.URL) is first
        assert len(feeds.requests) == 2
        assert client.get(self.URL) is first  # failure also waits out the TTL
        assert len(feeds.requests) == 2


def test_parse_feed_repairs_durations_missing_t():
    from pv_icloud.cal.feeds import parse_feed

    # Seen live: REFRESH-INTERVAL;VALUE=DURATION:P12H made icalendar reject a whole feed.
    c = parse_feed(
        b"BEGIN:VCALENDAR\r\nREFRESH-INTERVAL;VALUE=DURATION:P12H\r\n"
        b"BEGIN:VEVENT\r\nUID:a\r\nDTSTART:20261010T100000Z\r\nDURATION:P90M\r\n"
        b"BEGIN:VALARM\r\nTRIGGER:-P15M\r\nEND:VALARM\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    event = c.walk("VEVENT")[0]
    assert c["REFRESH-INTERVAL"].dt == dt.timedelta(hours=12)
    assert event["DURATION"].dt == dt.timedelta(minutes=90)
    assert event.walk("VALARM")[0]["TRIGGER"].dt == -dt.timedelta(minutes=15)
    with pytest.raises(ValueError):
        parse_feed(b"not a calendar")
