# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

"""Contacts tools and find_person against the fake CardDAV and IMAP servers."""

from __future__ import annotations

import datetime as dt

import pytest

from pv_icloud.cal.caldav import CalDavClient
from pv_icloud.config import ConfigError
from pv_icloud.contacts import service as contacts_service
from pv_icloud.contacts.carddav import CardDavClient
from pv_icloud.contacts.service import ContactsService
from pv_icloud.contacts.vcard import parse_vcard
from pv_icloud.dav import (
    DavError,
    DavStatusError,
    DavThrottled,
    Throttle,
    home_may_have_moved,
    retry_after_seconds,
)
from pv_icloud.errors import ToolInputError
from pv_icloud.mail.parse import split_addresses
from pv_icloud.people import PeopleService

from .conftest import Harness, make_config
from .fakecarddav import FakeBook, FakeCardDav, group_card, vcard
from .fakeimap import make_message

NOW = dt.datetime.now(dt.UTC)

JANE = vcard(
    "jane-uid",
    "ORG:Acme\\, Inc.;Research",
    "TITLE:Chief Scientist",
    "item1.EMAIL;type=INTERNET;type=pref:jane@acme.example",
    "item1.X-ABLabel:_$!<Other>!$_",
    "EMAIL;type=INTERNET;type=HOME:jane.doe@home.example",
    "TEL;type=CELL;type=VOICE;type=pref:+1 (312) 555-0134",
    "TEL;type=WORK;type=FAX:312-555-0199",
    "item2.ADR;type=HOME;type=pref:;;1 Main St;Springfield;IL;62701;United States",
    "item3.X-ABRELATEDNAMES;type=pref:John Doe",
    "item3.X-ABLabel:_$!<Spouse>!$_",
    "item4.X-ABDATE;X-APPLE-OMIT-YEAR=1604;type=pref:1604-06-01",
    "item4.X-ABLabel:_$!<Anniversary>!$_",
    "item5.URL;type=pref:https://jane.example",
    "item5.X-ABLabel:blog",
    "X-SOCIALPROFILE;type=linkedin;x-user=janedoe:https://www.linkedin.com/in/janedoe",
    "IMPP;X-SERVICE-TYPE=Skype;type=pref:skype:jane.doe",
    "BDAY;X-APPLE-OMIT-YEAR=1604:1604-03-15",
    "NOTE:Met at the conference\\nLikes chess",
    "PHOTO;ENCODING=b;TYPE=JPEG:" + "A" * 300,
    fn="Jane Doe",
    n="Doe;Jane;Q.;Dr.;",
)
JOSE = vcard(
    "jose-uid",
    "EMAIL;type=INTERNET:jose@example.org",
    "TEL;type=HOME:+34 600 123 456",
    fn="José Álvarez",
    n="Álvarez;José;;;",
)
OWNER = vcard(
    "me-uid", "EMAIL;type=INTERNET:alice@example.com", fn="Alice Owner", n="Owner;Alice;;;"
)
PLUMBER = vcard("plumb-uid", "TEL;type=WORK:555 0101", "NOTE:Jane recommended", fn="Pipes R Us")
DOCTOR = vcard("doc-uid", "EMAIL;type=INTERNET:dr@clinic.example", fn="Dr. Strange")


def make_book() -> FakeBook:
    return FakeBook(
        "111",
        cards={
            "jane-file.vcf": JANE,  # file name differs from UID, as for a third of real cards
            "jose-uid.vcf": JOSE,
            "me-uid.vcf": OWNER,
            "plumb-uid.vcf": PLUMBER,
            "doc-uid.vcf": DOCTOR,
            "g-family.vcf": group_card("g-family", "Family", ["jane-uid", "jose-uid"]),
            "g-medical.vcf": group_card("g-medical", "Medical", ["doc-uid"]),
        },
        me="me-uid.vcf",
    )


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def fake():
    return FakeCardDav({"alice@icloud.com": make_book()})


def make_service(fake, tmp_path, clock=None, throttle=None, **contacts) -> ContactsService:
    clock = clock or Clock()
    acct = make_config(tmp_path, alice_contacts=contacts or None).account("alice")
    client = CardDavClient(
        "alice@icloud.com",
        "app-pw",
        transport=fake.transport(),
        throttle=throttle or Throttle(clock),
    )
    return ContactsService(acct, client, clock=clock)


@pytest.fixture
def service(fake, tmp_path):
    return make_service(fake, tmp_path)


def names(result) -> list[str]:
    return [c["name"] for c in result["contacts"]]


class TestVCard:
    def test_apple_card_fields(self):
        card = parse_vcard(JANE)
        assert card.uid == "jane-uid" and card.kind == "individual"
        assert (card.name, card.given, card.family, card.middle, card.prefix) == (
            "Jane Doe",
            "Jane",
            "Doe",
            "Q.",
            "Dr.",
        )
        assert (card.organization, card.department) == ("Acme, Inc.", "Research")
        assert card.emails == [
            {"label": "other", "value": "jane@acme.example", "preferred": True},
            {"label": "home", "value": "jane.doe@home.example", "preferred": False},
        ]
        assert [(p["label"], p["value"]) for p in card.phones] == [
            ("mobile", "+1 (312) 555-0134"),
            ("work fax", "312-555-0199"),
        ]
        [addr] = card.addresses
        assert addr["label"] == "home" and addr["city"] == "Springfield"
        assert addr["formatted"] == "1 Main St, Springfield, IL 62701, United States"
        assert card.related_names == [{"label": "spouse", "value": "John Doe"}]
        assert card.dates == [{"label": "anniversary", "value": "--06-01"}]
        assert card.birthday == "--03-15"
        assert card.urls == [{"label": "blog", "value": "https://jane.example"}]
        assert card.social_profiles == [
            {"service": "linkedin", "user": "janedoe", "url": "https://www.linkedin.com/in/janedoe"}
        ]
        assert card.instant_messaging == [{"service": "Skype", "handle": "jane.doe"}]
        assert card.note == "Met at the conference\nLikes chess"

    def test_folding_inside_parameters(self):
        long_param = "X-SOCIALPROFILE;type=twitter;x-user=" + "u" * 90 + ":https://x.example/u"
        text = vcard("u1", long_param, "EMAIL;type=INTERNET:a@b.example", fn="Folded")
        assert "\r\n " in text
        card = parse_vcard(text)
        assert card.social_profiles[0]["user"] == "u" * 90
        assert card.emails[0]["value"] == "a@b.example"

    def test_dates_and_name_fallbacks(self):
        card = parse_vcard(vcard("u2", "BDAY:1980-03-15", "ORG:Widgets;"))
        assert card.birthday == "1980-03-15" and card.name == "Widgets"
        assert parse_vcard(vcard("u3", "BDAY;value=date:--0704")).birthday == "--07-04"
        assert parse_vcard(vcard("u4", "EMAIL:x@y.example")).name == "x@y.example"
        assert parse_vcard(vcard("u5", n="Lee;Ann;;;")).name == "Ann Lee"

    def test_phone_labels(self):
        card = parse_vcard(
            vcard(
                "u6",
                "TEL;type=IPHONE;type=CELL;type=VOICE:1",
                "item1.TEL;type=HOME:2",
                "item1.X-ABLabel:",
                "item2.TEL:3",
                "item2.X-ABLabel:_$!<Main>!$_",
                "TEL:4",
            )
        )
        assert [p["label"] for p in card.phones] == ["iPhone", "home", "main", None]

    def test_group_card(self):
        card = parse_vcard(group_card("g1", "Book club", ["a", "b"]))
        assert card.kind == "group" and card.name == "Book club" and card.members == ["a", "b"]

    def test_rejects_non_vcard(self):
        with pytest.raises(ValueError):
            parse_vcard("BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n")
        with pytest.raises(ValueError):
            parse_vcard("BEGIN:VCARD\r\nFN:No uid\r\nEND:VCARD\r\n")


class TestSearch:
    def test_by_name_email_company_and_accents(self, service):
        assert names(service.search(query="jane")) == ["Jane Doe", "Pipes R Us"]
        assert names(service.search(query="acme")) == ["Jane Doe"]
        assert names(service.search(query="jose")) == ["José Álvarez"]
        assert names(service.search(query="ALVAREZ jose")) == ["José Álvarez"]
        assert names(service.search(query="jane.doe@home")) == ["Jane Doe"]
        assert names(service.search(query="springfield")) == ["Jane Doe"]

    def test_name_matches_rank_before_other_fields(self, service):
        # "Jane" is in the plumber's note; Jane Doe's name match comes first anyway.
        assert names(service.search(query="jane"))[0] == "Jane Doe"

    def test_phone_digits_in_any_format(self, service):
        assert names(service.search(query="3125550134")) == ["Jane Doe"]
        assert names(service.search(query="(312) 555-0134")) == ["Jane Doe"]
        assert names(service.search(query="600123")) == ["José Álvarez"]

    def test_no_query_lists_all_alphabetically_with_paging(self, service):
        first = service.search(limit=2)
        assert names(first) == ["Alice Owner", "Dr. Strange"]
        assert first["total"] == 5 and first["next_offset"] == 2
        rest = service.search(limit=10, offset=2)
        assert names(rest) == ["Jane Doe", "José Álvarez", "Pipes R Us"]
        assert rest["next_offset"] is None  # group cards are never listed as contacts

    def test_group_filter(self, service):
        assert names(service.search(group="family")) == ["Jane Doe", "José Álvarez"]
        with pytest.raises(ToolInputError, match="unknown group"):
            service.search(group="Nope")

    def test_limits_validated(self, service):
        with pytest.raises(ToolInputError):
            service.search(limit=0)
        with pytest.raises(ToolInputError):
            service.search(limit=101)
        with pytest.raises(ToolInputError):
            service.search(offset=-1)


class TestGet:
    def test_full_detail(self, service):
        [hit] = service.search(query="acme")["contacts"]
        assert hit["id"] == "jane-file"
        assert hit["phones"][0] == {"label": "mobile", "value": "+1 (312) 555-0134"}
        got = service.get(hit["id"])
        assert got["groups"] == ["Family"] and got["is_me"] is False
        assert got["birthday"] == "--03-15" and got["note"].startswith("Met at")
        assert got["related_names"][0]["label"] == "spouse"
        assert "PHOTO" not in str(got) and "AAAA" not in str(got)

    def test_me_card(self, service):
        [me] = service.search(query="owner")["contacts"]
        assert service.get(me["id"])["is_me"] is True

    def test_unknown_id(self, service):
        with pytest.raises(ToolInputError, match="not found"):
            service.get("../../etc/passwd")

    def test_long_note_is_cut(self, fake, tmp_path):
        fake.books["alice@icloud.com"].put(
            "long.vcf", vcard("long", "NOTE:" + "n" * 25_000, fn="Long Note")
        )
        got = make_service(fake, tmp_path).get("long")
        assert len(got["note"]) == contacts_service.NOTE_MAX_CHARS and got["note_truncated"]

    def test_groups_list(self, service):
        out = service.groups()
        assert out["groups"] == [
            {"name": "Family", "members": 2},
            {"name": "Medical", "members": 1},
        ]
        assert out["contacts"] == 5


class TestSync:
    def test_loads_once_then_syncs_changes_only(self, fake, tmp_path):
        clock = Clock()
        svc = make_service(fake, tmp_path, clock)
        book = fake.books["alice@icloud.com"]
        svc.search()
        assert fake.sync_tokens == [""] and fake.multiget_counts == [7]
        svc.search()  # inside the refresh interval: no requests at all
        assert fake.sync_tokens == [""]

        book.put("new.vcf", vcard("new", fn="Newton Fig"))
        book.put("jose-uid.vcf", JOSE.replace("José Álvarez", "Pepe Álvarez"))
        book.delete("doc-uid.vcf")
        clock.now += contacts_service.REFRESH_SECONDS
        assert names(svc.search()) == [
            "Alice Owner",
            "Jane Doe",
            "Newton Fig",
            "Pepe Álvarez",
            "Pipes R Us",
        ]
        assert fake.sync_tokens[1].startswith("t") and fake.multiget_counts[1] == 2

        clock.now += contacts_service.REFRESH_SECONDS
        svc.search()  # nothing changed: one small sync request, no multiget
        assert len(fake.sync_tokens) == 3 and len(fake.multiget_counts) == 2

    def test_rejected_token_reloads_everything(self, fake, tmp_path):
        clock = Clock()
        svc = make_service(fake, tmp_path, clock)
        svc.search()
        fake.forget_tokens = True
        fake.books["alice@icloud.com"].delete("doc-uid.vcf")
        clock.now += contacts_service.REFRESH_SECONDS
        assert "Dr. Strange" not in names(svc.search())
        assert fake.sync_tokens[-1] == ""

    def test_unparseable_card_is_skipped(self, fake, tmp_path):
        fake.books["alice@icloud.com"].put("bad.vcf", "BEGIN:VCARD\r\nFN:x\r\nEND:VCARD\r\n")
        assert make_service(fake, tmp_path).search()["total"] == 5

    def test_upstream_failure_raises_and_recovers(self, fake, tmp_path):
        clock = Clock()
        svc = make_service(fake, tmp_path, clock)
        fake.books["alice@icloud.com"].fail = 500
        with pytest.raises(DavError):
            svc.search()
        fake.books["alice@icloud.com"].fail = None
        assert svc.search()["total"] == 5  # a failed first load is retried on the next call

    def test_never_writes(self, fake, service):
        service.search()
        service.get("jane-file")
        service.groups()
        assert fake.write_attempts == []
        assert {m for m, _ in fake.requests} <= {"PROPFIND", "REPORT"}
        with pytest.raises(RuntimeError):
            service.client._request("PUT", service.client.home(), "", "0")


class TestThrottle:
    def test_503_pauses_every_request_until_retry_after(self, fake, tmp_path):
        clock = Clock()
        svc = make_service(fake, tmp_path, clock)
        book = fake.books["alice@icloud.com"]
        book.fail, book.retry_after = 503, "120"
        with pytest.raises(DavThrottled) as info:
            svc.search()
        assert info.value.retry_after == 120
        sent = len(fake.requests)
        book.fail = None
        clock.now += 60
        with pytest.raises(DavThrottled) as info:
            svc.search()
        assert info.value.retry_after == 60 and len(fake.requests) == sent  # nothing sent
        clock.now += 61
        assert svc.search()["total"] == 5

    def test_serves_loaded_copy_while_icloud_is_busy(self, fake, tmp_path):
        clock = Clock()
        svc = make_service(fake, tmp_path, clock)
        book = fake.books["alice@icloud.com"]
        assert "note" not in svc.search()
        book.fail, book.retry_after = 503, "1800"
        clock.now += contacts_service.REFRESH_SECONDS
        sent = len(fake.requests)
        out = svc.search()
        assert out["total"] == 5 and out["note"] == contacts_service.STALE_NOTE
        assert svc.get("jane-file")["note"] == contacts_service.STALE_NOTE
        assert len(fake.requests) == sent + 1  # one refused request, then quiet
        clock.now += contacts_service.REFRESH_SECONDS  # still inside Retry-After
        assert svc.groups()["note"] == contacts_service.STALE_NOTE
        assert len(fake.requests) == sent + 1
        book.fail = None
        clock.now += 1800
        assert "note" not in svc.search()
        # The home URL was kept: discovery ran once, at the first load.
        assert sum(1 for _, url in fake.requests if url.startswith("contacts.icloud.com")) == 2

    def test_calendar_shares_the_pause(self, fake, tmp_path):
        clock = Clock()
        throttle = Throttle(clock)
        svc = make_service(fake, tmp_path, clock, throttle)
        fake.books["alice@icloud.com"].fail = 503  # no Retry-After: default pause
        with pytest.raises(DavThrottled):
            svc.search()
        cal = CalDavClient("alice@icloud.com", "app-pw", throttle=throttle)  # real host: unused
        with pytest.raises(DavThrottled):
            cal.calendars()
        clock.now += 61
        assert throttle.remaining() == 0

    def test_retry_after_formats(self):
        now = dt.datetime(2026, 10, 9, 12, 0, tzinfo=dt.UTC)
        assert retry_after_seconds("1227") == 1227
        assert retry_after_seconds("Fri, 09 Oct 2026 12:05:00 GMT", now) == 300
        assert retry_after_seconds("Fri, 09 Oct 2026 11:00:00 GMT", now) == 0
        assert retry_after_seconds("soon") is None and retry_after_seconds(None) is None

    def test_pause_is_capped(self):
        clock = Clock()
        throttle = Throttle(clock)
        assert throttle.pause(10**6) == 3600 and throttle.pause(0) == 3600

    def test_rediscovery_only_when_home_may_have_moved(self):
        assert home_may_have_moved(DavStatusError("x", 404))
        assert home_may_have_moved(DavStatusError("x", 403))
        for exc in (DavStatusError("x", 503), DavStatusError("x", 429), DavThrottled(5)):
            assert not home_may_have_moved(exc)
        assert not home_may_have_moved(DavError("PROPFIND failed: ConnectError"))


class TestConfig:
    def test_excluded_groups_hide_their_members(self, fake, tmp_path):
        svc = make_service(fake, tmp_path, exclude_groups=["medical"])
        assert "Dr. Strange" not in names(svc.search())
        assert [g["name"] for g in svc.groups()["groups"]] == ["Family"]
        with pytest.raises(ToolInputError, match="unknown group"):
            svc.search(group="Medical")
        with pytest.raises(ToolInputError, match="not found"):
            svc.get("doc-uid")

    def test_turned_off(self, fake, tmp_path):
        svc = make_service(fake, tmp_path, enabled=False)
        for call in (svc.search, svc.groups, lambda: svc.get("jane-file")):
            with pytest.raises(ToolInputError, match="turned off"):
                call()
        svc.warm()
        assert fake.requests == []

    @pytest.mark.parametrize(
        "contacts", [{"enabled": "yes"}, {"exclude_groups": "Medical"}, {"exclude_groups": [1]}]
    )
    def test_validated(self, tmp_path, contacts):
        with pytest.raises(ConfigError, match="contacts"):
            make_config(tmp_path, alice_contacts=contacts)


# --- find_person -------------------------------------------------------------------------


@pytest.fixture
def people(server, fake, tmp_path):
    def deliver(folder, days_ago=1, **kw):
        when = NOW - dt.timedelta(days=days_ago)
        server.deliver(folder, make_message(date=when, **kw), when.date())

    deliver(
        "INBOX",
        1,
        subject="Lab results",
        from_="Jane Doe <jane@acme.example>",
        to="Alice Owner <alice@example.com>",
    )
    deliver(
        "INBOX",
        5,
        subject="Dinner?",
        from_='"Doe, Jane" <jane.doe@home.example>',
        to="alice@example.com",
    )
    deliver(
        "Work",
        3,
        subject="Re: grant",
        from_="Alice Owner <alice@example.com>",
        to="Jane Doe <jane@acme.example>",
        cc="Janet Smith <janet@else.example>",
    )
    deliver(
        "INBOX",
        2,
        subject="Invoice",
        from_="Jan Stranger <jan@stranger.example>",
        to="alice@example.com",
    )
    deliver(
        "Junk", 1, subject="Spam", from_="Jane Spammer <jane@spam.example>", to="alice@example.com"
    )
    deliver(
        "INBOX",
        4,
        subject="Pipes",
        from_="Pipes R Us <office@pipes.example>",
        to="alice@example.com",
    )
    deliver(
        "INBOX",
        6,
        subject="Appointment",
        from_="Dr. Strange <dr@clinic.example>",
        to="alice@example.com",
    )

    def build(**contacts):
        cfg = make_config(tmp_path, alice_contacts=contacts or None)
        h = Harness(server, cfg.account("alice"), tmp_path)
        h.run_sync()
        client = CardDavClient("alice@icloud.com", "app-pw", transport=fake.transport())
        return PeopleService(h.service, ContactsService(h.account, client, clock=Clock()))

    return build


def by_name(result) -> dict[str, dict]:
    return {p["name"]: p for p in result["people"]}


class TestFindPerson:
    def test_contact_and_mail_merged(self, people):
        out = people().find("jane")
        found = by_name(out)
        jane = found["Jane Doe"]
        assert jane["contact_id"] == "jane-file" and jane["organization"] == "Acme, Inc."
        # Both of her addresses count, whichever display name the mail used.
        assert jane["mail"]["messages"] == 3
        assert (jane["mail"]["from_them"], jane["mail"]["to_them"]) == (2, 1)
        assert [m["subject"] for m in jane["mail"]["recent"]] == [
            "Lab results",
            "Re: grant",
            "Dinner?",
        ]
        assert jane["mail"]["recent"][0]["id"]
        # Mail-only correspondents whose name matches, with the names they used.
        janet = found["Janet Smith"]
        assert janet["contact_id"] is None and janet["emails"] == ["janet@else.example"]
        assert janet["mail"]["messages"] == 1 and janet["names_seen"] == ["Janet Smith"]
        # Junk is out of scope; the plumber's note mentions Jane, so the card matches.
        assert "Jane Spammer" not in found
        assert set(found) == {"Jane Doe", "Janet Smith", "Pipes R Us"}
        # Most mail first.
        assert out["people"][0]["name"] == "Jane Doe"
        assert out["mail_searched_since"]

    def test_name_prefix_and_address_queries(self, people):
        svc = people()
        assert set(by_name(svc.find("jan"))) >= {"Jane Doe", "Janet Smith", "Jan Stranger"}
        assert list(by_name(svc.find("jan@stranger.example"))) == ["Jan Stranger"]
        # An address that is on a card finds the card, even under another display name.
        assert list(by_name(svc.find("jane.doe@home.example"))) == ["Jane Doe"]

    def test_phone_finds_contact_and_its_mail(self, people):
        [p] = people().find("312 555 0134")["people"]
        assert p["name"] == "Jane Doe" and p["mail"]["messages"] == 3

    def test_mail_only_correspondent(self, people):
        # "office" matches only the mail; the address is on no card, so it stays mail-only.
        [p] = people().find("office")["people"]
        assert p["contact_id"] is None and p["name"] == "Pipes R Us"

    def test_owner_marked_and_last(self, people):
        out = people().find("alice")
        assert out["people"][-1]["is_me"] is True
        assert out["people"][-1]["contact_id"] == "me-uid"

    def test_excluded_group_member_is_mail_only(self, people):
        [shown] = people().find("dr strange")["people"]
        assert shown["contact_id"] == "doc-uid" and shown["mail"]["messages"] == 1
        # Hidden by the config: the card is gone, the mail (scoped separately) remains.
        [hidden] = people(exclude_groups=["Medical"]).find("dr strange")["people"]
        assert hidden["contact_id"] is None and "groups" not in hidden
        assert hidden["mail"]["messages"] == 1

    def test_contacts_off_or_down_still_answers_from_mail(self, people, fake):
        out = people(enabled=False).find("jane")
        assert "turned off" in out["notes"][0]
        assert by_name(out)["Jane Doe"]["contact_id"] is None
        fake.books["alice@icloud.com"].fail = 503
        out = people().find("jane")
        assert "unavailable" in out["notes"][0]
        assert {"Jane Doe", "Janet Smith"} <= set(by_name(out))

    def test_paging_and_validation(self, people):
        svc = people()
        first = svc.find("jan", limit=1)
        assert len(first["people"]) == 1 and first["next_offset"] == 1 and first["total"] >= 3
        with pytest.raises(ToolInputError):
            svc.find("   ")
        with pytest.raises(ToolInputError):
            svc.find("jane", limit=26)


def test_split_addresses_keeps_commas_in_names():
    assert split_addresses("Doe, Jane <j@x.example>, bob@y.example") == [
        ("Doe, Jane", "j@x.example"),
        ("", "bob@y.example"),
    ]
    assert split_addresses("bob@y.example, Jane <j@x.example>") == [
        ("", "bob@y.example"),
        ("Jane", "j@x.example"),
    ]
    assert split_addresses("") == []
