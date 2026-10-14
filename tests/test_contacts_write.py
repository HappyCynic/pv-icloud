# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

"""Contact writes (add-contacts-write, ADR-0006) against the fake CardDAV server."""

from __future__ import annotations

import datetime as dt
import re
import uuid

import httpx
import pytest

from pv_icloud.contacts.carddav import CardDavClient
from pv_icloud.contacts.compose import Changes
from pv_icloud.contacts.service import ContactsService
from pv_icloud.contacts.vcard import parse_vcard
from pv_icloud.dav import Throttle
from pv_icloud.errors import ToolInputError, WriteFailed

from .conftest import make_config
from .fakecarddav import ANN, BOB, HOME_HOST, FakeCardDav, etag, group_card, write_book
from .test_contacts_compose import diff

BOOK_URL = f"https://{HOME_HOST}/111/carddavhome/card/"


class TestFake:
    """The fake behaves as the ADR-0006 live checks saw iCloud behave."""

    @pytest.fixture
    def fake(self):
        return FakeCardDav({"alice@icloud.com": write_book()})

    @pytest.fixture
    def http(self, fake):
        return httpx.Client(auth=("alice@icloud.com", "app-pw"), transport=fake.transport())

    CT = {"content-type": "text/vcard; charset=utf-8"}

    def test_fixture_cards_parse_with_apple_labels(self):
        card = parse_vcard(ANN)
        assert [(p["label"], p["preferred"]) for p in card.phones] == [
            ("mobile", True), ("iPhone", False), ("work", False), ("main", False),
            ("home fax", False), ("pager", False), (None, False), ("Boat", False),
        ]  # fmt: skip
        assert [(e["label"], e["preferred"]) for e in card.emails] == [
            ("work", True),
            ("other", False),
        ]
        assert card.birthday == "--03-15" and card.dates[0]["label"] == "anniversary"
        assert card.urls == [{"label": "homepage", "value": "https://ann.example"}]
        # The long parameter is folded, as iCloud folds anywhere.
        param = ANN[ANN.index("X-PV-NOTE=") : ANN.index(":https://ann.example")]
        assert "\r\n " in param

    def test_conditional_put_and_delete(self, fake, http):
        book = fake.books["alice@icloud.com"]
        body = "BEGIN:VCARD\r\nVERSION:3.0\r\nUID:new\r\nFN:New\r\nEND:VCARD\r\n"
        create = {**self.CT, "if-none-match": "*"}
        assert http.put(BOOK_URL + "new.vcf", content=body, headers=create).status_code == 201
        assert book.cards["new.vcf"] == body  # byte for byte
        assert http.put(BOOK_URL + "new.vcf", content=body, headers=create).status_code == 412
        tag = etag(body)
        changed = body.replace("FN:New", "FN:Newer")
        stale = {**self.CT, "if-match": '"old"'}
        assert http.put(BOOK_URL + "new.vcf", content=changed, headers=stale).status_code == 412
        resp = http.put(BOOK_URL + "new.vcf", content=changed, headers={**self.CT, "if-match": tag})
        assert resp.status_code == 204 and resp.headers["etag"] == etag(changed) != tag
        assert http.delete(BOOK_URL + "new.vcf", headers={"if-match": tag}).status_code == 412
        new_tag = resp.headers["etag"]
        assert http.delete(BOOK_URL + "new.vcf", headers={"if-match": new_tag}).status_code == 204
        assert http.delete(BOOK_URL + "new.vcf", headers={"if-match": new_tag}).status_code == 404
        assert fake.write_attempts == []
        assert [w.status for w in fake.writes] == [201, 412, 412, 204, 412, 204, 404]

    def test_forbidden_writes_are_recorded(self, fake, http):
        body = "BEGIN:VCARD\r\nUID:x\r\nEND:VCARD\r\n"
        book = fake.books["alice@icloud.com"]
        http.put(BOOK_URL + "x.vcf", content=body, headers=self.CT)  # no precondition
        http.put(BOOK_URL, content=body, headers={**self.CT, "if-none-match": "*"})  # collection
        http.put(BOOK_URL + "a/x.vcf", content=body, headers={**self.CT, "if-none-match": "*"})
        http.put(BOOK_URL + "x.txt", content=body, headers={**self.CT, "if-none-match": "*"})
        http.delete(
            f"https://{HOME_HOST}/111/carddavhome/", headers={"if-match": '"x"'}
        )  # the home
        http.delete(BOOK_URL + "me-uid.vcf", headers={"if-match": etag(book.cards["me-uid.vcf"])})
        http.delete(BOOK_URL + "g-work.vcf", headers={"if-match": etag(book.cards["g-work.vcf"])})
        for method in ("MKCOL", "PROPPATCH", "MOVE", "POST", "COPY"):
            http.request(method, BOOK_URL + "ann-file.vcf")
        assert len(fake.write_attempts) == 12
        assert fake.writes == [] and "me-uid.vcf" in book.cards and "g-work.vcf" in book.cards

    def test_sync_and_multiget_see_writes_with_etags(self, fake, http):
        book = fake.books["alice@icloud.com"]
        sync = (
            '<d:sync-collection xmlns:d="DAV:"><d:sync-token>{}</d:sync-token>'
            "<d:sync-level>1</d:sync-level><d:prop><d:getetag/></d:prop></d:sync-collection>"
        )
        first = http.request("REPORT", BOOK_URL, content=sync.format(""))
        token = first.text.split("<sync-token>")[1].split("<")[0]
        tag = etag(book.cards["bob-uid.vcf"])
        assert tag in first.text
        changed = book.cards["bob-uid.vcf"].replace("Bob Smith", "Bobby Smith")
        http.put(BOOK_URL + "bob-uid.vcf", content=changed, headers={**self.CT, "if-match": tag})
        http.delete(
            BOOK_URL + "carol-uid.vcf", headers={"if-match": etag(book.cards["carol-uid.vcf"])}
        )
        after = http.request("REPORT", BOOK_URL, content=sync.format(token)).text
        assert "bob-uid.vcf" in after and etag(changed) in after
        assert "carol-uid.vcf" in after and "404 Not Found" in after
        multiget = (
            '<card:addressbook-multiget xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">'
            "<d:prop><d:getetag/><card:address-data/></d:prop>"
            "<d:href>/111/carddavhome/card/bob-uid.vcf</d:href></card:addressbook-multiget>"
        )
        got = http.request("REPORT", BOOK_URL, content=multiget).text
        assert etag(changed) in got and "Bobby Smith" in got

    def test_hook_and_write_failures(self, fake, http):
        book = fake.books["alice@icloud.com"]
        tag = etag(book.cards["bob-uid.vcf"])
        fake.before_write = lambda: book.put(
            "bob-uid.vcf", book.cards["bob-uid.vcf"].replace("Bob", "Rob")
        )
        headers = {**self.CT, "if-match": tag}
        assert http.put(BOOK_URL + "bob-uid.vcf", content="x", headers=headers).status_code == 412
        fake.write_fail = (503, "120")
        resp = http.delete(BOOK_URL + "bob-uid.vcf", headers={"if-match": '"x"'})
        assert resp.status_code == 503 and resp.headers["retry-after"] == "120"


class TestFreshRead:
    """CardDavClient.cards: stored text and ETag of a few cards, in one REPORT."""

    def test_text_and_etag_per_href(self):
        fake = FakeCardDav({"alice@icloud.com": write_book()})
        client = CardDavClient("alice@icloud.com", "app-pw", transport=fake.transport())
        book = fake.books["alice@icloud.com"]
        hrefs = [
            "/111/carddavhome/card/ann-file.vcf",
            "/111/carddavhome/card/g-family.vcf",
            "/111/carddavhome/card/missing.vcf",
        ]
        got = client.cards(BOOK_URL, hrefs)
        assert got == {
            hrefs[0]: (book.cards["ann-file.vcf"], etag(book.cards["ann-file.vcf"])),
            hrefs[1]: (book.cards["g-family.vcf"], etag(book.cards["g-family.vcf"])),
        }
        assert [m for m, _ in fake.requests] == ["REPORT"]
        assert fake.multiget_counts == [3]
        assert client.cards(BOOK_URL, []) == {}
        assert len(fake.requests) == 1


CARD = "/111/carddavhome/card/"
NEW = "BEGIN:VCARD\r\nVERSION:3.0\r\nUID:NEW\r\nFN:New\r\nEND:VCARD\r\n"


class TestWriter:
    """contacts/carddav_write.py: only PUT and DELETE, always conditional, only on a `.vcf`
    directly in the account's address book."""

    @pytest.fixture
    def fake(self):
        return FakeCardDav({"alice@icloud.com": write_book()})

    @pytest.fixture
    def client(self, fake):
        client = CardDavClient("alice@icloud.com", "app-pw", transport=fake.transport())
        client.home()  # discovered, as the service's refresh does before any write
        return client

    @pytest.fixture
    def writer(self, client):
        from pv_icloud.contacts.carddav_write import CardDavWriter

        return CardDavWriter(client)

    def test_create_update_delete(self, fake, writer):
        book = fake.books["alice@icloud.com"]
        href = writer.create(BOOK_URL, "NEW.vcf", NEW)
        assert href == CARD + "NEW.vcf" and book.cards["NEW.vcf"] == NEW
        changed = NEW.replace("FN:New", "FN:Newer")
        writer.update(BOOK_URL, href, changed, etag(NEW))
        writer.delete(BOOK_URL, href, etag(changed))
        assert "NEW.vcf" not in book.cards
        assert [(w.method, w.status) for w in fake.writes] == [
            ("PUT", 201), ("PUT", 204), ("DELETE", 204),
        ]  # fmt: skip
        first, second, third = (w.headers for w in fake.writes)
        assert first["if-none-match"] == "*" and "if-match" not in first
        assert second["if-match"] == etag(NEW) and "if-none-match" not in second
        assert third["if-match"] == etag(changed)
        assert first["content-type"] == "text/vcard; charset=utf-8"
        assert fake.write_attempts == []

    def test_refuses_other_methods_and_missing_preconditions(self, fake, writer):
        for method in ("MKCOL", "PROPPATCH", "MOVE", "POST", "COPY"):
            with pytest.raises(RuntimeError, match="only PUT and DELETE"):
                writer._write(method, BOOK_URL, CARD + "x.vcf", None, {"If-Match": '"x"'})
        for precondition in ({}, {"If-Match": ""}, {"If-None-Match": "x"},
                             {"If-Match": '"x"', "If-None-Match": "*"}):  # fmt: skip
            with pytest.raises(RuntimeError, match="precondition"):
                writer._write("PUT", BOOK_URL, CARD + "x.vcf", NEW, precondition)
        with pytest.raises(RuntimeError, match="precondition"):
            writer._write("DELETE", BOOK_URL, CARD + "x.vcf", None, {"If-None-Match": "*"})
        assert {m for m, _ in fake.requests} == {"PROPFIND"}  # discovery only
        assert fake.writes == [] and fake.write_attempts == []

    @pytest.mark.parametrize(
        ("book", "href"),
        [
            (BOOK_URL, CARD),  # the collection
            (BOOK_URL, "/111/carddavhome/"),  # the home
            (BOOK_URL, CARD + "a/b.vcf"),  # nested
            (BOOK_URL, CARD + "a%2Fb.vcf"),  # nested, encoded
            (BOOK_URL, CARD + "x.txt"),
            (BOOK_URL, CARD + "x.vcf?x=1"),
            (BOOK_URL, "/111/carddavhome/other/x.vcf"),
            (BOOK_URL, "/111/calendars/home/x.vcf"),
            (f"https://{HOME_HOST}/111/carddavhome/", "/111/carddavhome/x.vcf"),  # not a book
            (f"https://{HOME_HOST}/222/carddavhome/card/", "/222/carddavhome/card/x.vcf"),
            ("https://p01-caldav.icloud.com/111/carddavhome/card/", CARD + "x.vcf"),
        ],
    )
    def test_refuses_anything_but_a_card_in_the_address_book(self, fake, writer, book, href):
        with pytest.raises(RuntimeError, match="outside the address book"):
            writer.update(book, href, NEW, '"x"')
        assert fake.writes == [] and fake.write_attempts == []

    def test_conflict_is_reported_and_not_retried(self, fake, writer):
        from pv_icloud.contacts.carddav_write import CHANGED, Changed

        with pytest.raises(Changed) as err:
            writer.update(BOOK_URL, CARD + "bob-uid.vcf", NEW, '"stale"')
        assert str(err.value) == CHANGED == "the contact changed in Contacts; read it again"
        with pytest.raises(Changed):
            writer.create(BOOK_URL, "bob-uid.vcf", NEW)
        assert [w.status for w in fake.writes] == [412, 412]

    @pytest.mark.parametrize(
        ("status", "error", "message"),
        [
            (404, "Gone", "contact not found"),
            (403, "WriteFailed", "iCloud refused the change; nothing was changed"),
            (409, "WriteFailed", "iCloud refused the change"),
            (500, "WriteFailed", "iCloud did not confirm the change"),
        ],
    )
    def test_status_errors(self, fake, writer, status, error, message):
        import pv_icloud.contacts.carddav_write as mod
        from pv_icloud.errors import WriteFailed

        fake.write_fail = (status, None)
        cls = WriteFailed if error == "WriteFailed" else getattr(mod, error)
        with pytest.raises(cls, match=message):
            writer.delete(BOOK_URL, CARD + "bob-uid.vcf", '"x"')
        assert len(fake.writes) == 1

    def test_throttling_pauses_the_account(self, fake, client, writer):
        from pv_icloud.dav import DavThrottled

        fake.write_fail = (503, "120")
        with pytest.raises(DavThrottled):
            writer.delete(BOOK_URL, CARD + "bob-uid.vcf", '"x"')
        assert 100 < client.throttle.remaining() <= 120
        fake.write_fail = None
        sent = len(fake.requests)
        with pytest.raises(DavThrottled):
            writer.delete(BOOK_URL, CARD + "bob-uid.vcf", '"x"')
        assert len(fake.requests) == sent  # a paused account sends nothing

    def test_unconfirmed_and_unsent(self, caplog):
        from pv_icloud.contacts.carddav_write import NOT_SENT, UNCONFIRMED, CardDavWriter
        from pv_icloud.errors import WriteFailed

        fake = FakeCardDav({"alice@icloud.com": write_book()})
        failure: list[Exception] = []

        def handle(request: httpx.Request) -> httpx.Response:
            if request.method in ("PUT", "DELETE") and failure:
                raise failure[0]
            return fake.handle(request)

        client = CardDavClient("alice@icloud.com", "app-pw", transport=httpx.MockTransport(handle))
        client.home()
        writer = CardDavWriter(client)
        failure.append(httpx.ReadTimeout("slow"))
        with caplog.at_level("DEBUG"), pytest.raises(WriteFailed) as err:
            writer.update(BOOK_URL, CARD + "ann-file.vcf", ANN.replace("Buyer", "Seller"), '"x"')
        assert str(err.value) == UNCONFIRMED
        assert "read the contact again before retrying" in UNCONFIRMED
        failure[0] = httpx.ConnectError("down")
        with pytest.raises(WriteFailed) as err:
            writer.delete(BOOK_URL, CARD + "ann-file.vcf", '"x"')
        assert str(err.value) == NOT_SENT
        for record in caplog.records:  # no path, UID or card content in any log line
            text = record.getMessage()
            assert "ann-file" not in text and "ann-uid" not in text and "Seller" not in text


# --- the contacts service ------------------------------------------------------------------

NOW = dt.datetime(2026, 10, 14, 12, 0, 0, tzinfo=dt.UTC)
REV = "REV:2026-10-14T12:00:00Z"
NOT_FOUND = "contact not found (deleted, hidden, or a stale id)"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def fake():
    fake = FakeCardDav({"alice@icloud.com": write_book()})
    yield fake
    assert fake.write_attempts == []  # no forbidden write in any test


@pytest.fixture
def book(fake):
    return fake.books["alice@icloud.com"]


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def service(fake, tmp_path, clock):
    from pv_icloud.contacts.carddav_write import CardDavWriter

    acct = make_config(tmp_path, alice_contacts={"exclude_groups": ["Medical"]}).account("alice")
    client = CardDavClient(
        "alice@icloud.com", "app-pw", transport=fake.transport(), throttle=Throttle(clock)
    )
    svc = ContactsService(acct, client, clock=clock, writer=CardDavWriter(client), now=lambda: NOW)
    svc.snapshot()  # loaded, as on a running server
    return svc


def ids(service) -> dict[str, str]:
    """Name -> id of every visible contact."""
    return {c.name: cid for cid, c in service.snapshot().contacts.items()}


def puts(fake) -> list[str]:
    return [f"{w.method} {w.path.rsplit('/', 1)[-1]}" for w in fake.writes]


class TestCreate:
    def test_every_field_with_labels_is_readable_at_once(self, service, fake):
        out = service.create(
            {
                "given_name": "Zoe",
                "family_name": "Quinn",
                "organization": "Widgets",
                "phones": [{"label": "mobile", "value": "+1 312 555 0900"},
                           {"label": "iPhone", "value": "+1 312 555 0901"}],
                "emails": [{"label": "work", "value": "zoe@widgets.example", "preferred": True}],
                "addresses": [{"label": "home", "street": "2 Oak St", "city": "Springfield"}],
                "birthday": "--03-15",
                "related_names": [{"label": "sister", "value": "Ann Lee"}],
                "dates": [{"label": "Graduation", "value": "2010-06-01"}],
            }
        )  # fmt: skip
        assert out["name"] == "Zoe Quinn" and out["groups"] == []
        assert out["contacts_as_of"] is not None
        got = service.get(out["id"])
        assert [(p["label"], p["value"]) for p in got["phones"]] == [
            ("mobile", "+1 312 555 0900"), ("iPhone", "+1 312 555 0901"),
        ]  # fmt: skip
        assert got["emails"] == [
            {"label": "work", "value": "zoe@widgets.example", "preferred": True}
        ]
        assert got["addresses"][0]["label"] == "home" and got["birthday"] == "--03-15"
        assert got["related_names"] == [{"label": "sister", "value": "Ann Lee"}]
        assert got["dates"] == [{"label": "Graduation", "value": "2010-06-01"}]
        found = service.search(query="555 0901")["contacts"]
        assert [c["id"] for c in found] == [out["id"]]
        [write] = fake.writes
        assert write.method == "PUT" and write.headers["if-none-match"] == "*"
        name = write.path.rsplit("/", 1)[-1]
        assert name == f"{out['id']}.vcf" and re.fullmatch(r"[0-9A-F-]{36}\.vcf", name)
        assert f"UID:{out['id']}\r\n" in write.body

    def test_labels_in_apple_form(self, service, fake):
        service.create(
            {
                "given_name": "Zoe",
                "phones": [{"label": "mobile", "value": "1"}, {"label": "iPhone", "value": "2"},
                           {"label": "work fax", "value": "3"}, {"value": "4", "preferred": True},
                           {"label": "Boat", "value": "5"}],
                "emails": [{"label": "home", "value": "z@h.example", "preferred": True},
                           {"label": "school", "value": "z@s.example", "preferred": True}],
            }
        )  # fmt: skip
        lines = fake.writes[0].body.split("\r\n")
        for line in (
            "TEL;TYPE=CELL;TYPE=VOICE:1",
            "TEL;TYPE=IPHONE;TYPE=CELL;TYPE=VOICE:2",
            "TEL;TYPE=WORK;TYPE=FAX:3",
            "TEL;TYPE=PREF:4",
            "EMAIL;TYPE=INTERNET;TYPE=HOME;TYPE=PREF:z@h.example",
            "item1.EMAIL;TYPE=INTERNET;TYPE=PREF:z@s.example",  # emails come first
            "item1.X-ABLabel:_$!<School>!$_",
            "item2.TEL:5",
            "item2.X-ABLabel:Boat",
        ):
            assert line in lines, line

    def test_company_card(self, service):
        out = service.create({"organization": "Gadgets", "phones": [{"value": "1"}]})
        assert out["name"] == "Gadgets"

    def test_in_a_group(self, service, fake, book):
        before = book.cards["g-work.vcf"]
        out = service.create({"given_name": "Zoe"}, groups=["work"])
        assert out["groups"] == ["Work"] and service.get(out["id"])["groups"] == ["Work"]
        assert puts(fake) == [f"PUT {out['id']}.vcf", "PUT g-work.vcf"]
        removed, added = diff(before, book.cards["g-work.vcf"])
        assert removed == [] and added == [f"X-ADDRESSBOOKSERVER-MEMBER:urn:uuid:{out['id']}", REV]
        assert fake.writes[1].headers["if-match"] == etag(before)

    def test_unknown_and_excluded_groups_look_the_same(self, service, fake):
        for group in ("Medical", "Nope"):
            with pytest.raises(ToolInputError) as err:
                service.create({"given_name": "Zoe"}, groups=[group])
            assert str(err.value) == f"unknown group: {group}"
        assert fake.writes == []

    def test_input_errors_send_nothing(self, service, fake):
        requests = len(fake.requests)
        for fields in ({"note": "only"}, {"given_name": "Z", "note": "x" * 20_001}):
            with pytest.raises(ToolInputError):
                service.create(fields)
        assert len(fake.requests) == requests

    def test_name_taken(self, service, fake, book, monkeypatch):
        monkeypatch.setattr(uuid, "uuid4", lambda: uuid.UUID(int=7))
        taken = f"{str(uuid.UUID(int=7)).upper()}.vcf"
        book.put(taken, BOB)
        with pytest.raises(ToolInputError, match="changed in Contacts"):
            service.create({"given_name": "Zoe"})
        assert book.cards[taken] == BOB and [w.status for w in fake.writes] == [412]


class TestUpdate:
    def test_one_field_keeps_every_other_line(self, service, fake, book):
        out = service.update(ids(service)["Ann Lee"], Changes(values={"title": "Head Buyer"}))
        assert out["name"] == "Ann Lee" and out["groups"] == ["Family"]
        assert diff(ANN, book.cards["ann-file.vcf"]) == (
            ["TITLE:Buyer", "REV:2026-01-02T03:04:05Z"],
            ["TITLE:Head Buyer", REV],
        )
        assert puts(fake) == ["PUT ann-file.vcf"]
        assert fake.writes[0].headers["if-match"] == etag(ANN)

    def test_fix_a_phone_and_find_it_at_once(self, service, clock):
        ann = ids(service)["Ann Lee"]
        clock.now += 10  # within the refresh interval
        service.update(
            ann,
            Changes(
                remove={"phones": [{"label": "mobile", "value": "+1 312 555 0100"}]},
                add={"phones": [{"label": "mobile", "value": "+1 312 555 0199"}]},
            ),
        )
        phones = [(p["label"], p["value"]) for p in service.get(ann)["phones"]]
        assert ("mobile", "+1 312 555 0199") in phones
        assert ("mobile", "+1 312 555 0100") not in phones
        assert [c["id"] for c in service.search(query="555 0199")["contacts"]] == [ann]
        assert service.search(query="555 0100")["contacts"] == []

    def test_name_change(self, service, book):
        out = service.update(ids(service)["Ann Lee"], Changes(values={"family_name": "Park"}))
        assert out["name"] == "Ann Park"
        assert diff(ANN, book.cards["ann-file.vcf"]) == (
            ["N:Lee;Ann;;;", "FN:Ann Lee", "REV:2026-01-02T03:04:05Z"],
            ["N:Park;Ann;;;", "FN:Ann Park", REV],
        )
        bob = service.update(ids(service)["Bob Smith"], Changes(values={"family_name": "Jones"}))
        assert bob["name"] == "Bob Smith"
        assert "FN:Bob Smith\r\n" in book.cards["bob-uid.vcf"]

    def test_move_between_groups_writes_only_group_cards(self, service, fake, book):
        ann = ids(service)["Ann Lee"]
        family, work = book.cards["g-family.vcf"], book.cards["g-work.vcf"]
        out = service.update(ann, Changes(), add_groups=["Work"], remove_groups=["family"])
        assert out["groups"] == ["Work"] and service.get(ann)["groups"] == ["Work"]
        assert puts(fake) == ["PUT g-family.vcf", "PUT g-work.vcf"]  # in name order
        assert book.cards["ann-file.vcf"] == ANN  # the contact card is not written
        assert diff(family, book.cards["g-family.vcf"]) == (
            ["X-ADDRESSBOOKSERVER-MEMBER:urn:uuid:ann-uid", "REV:2026-01-05T00:00:00Z"],
            [REV],
        )
        assert "X-ADDRESSBOOKSERVER-MEMBER:urn:uuid:doc-uid" in book.cards["g-family.vcf"]
        assert diff(work, book.cards["g-work.vcf"]) == (
            [],
            ["X-ADDRESSBOOKSERVER-MEMBER:urn:uuid:ann-uid", REV],
        )
        assert service.groups()["groups"] == [
            {"name": "Family", "members": 0}, {"name": "Work", "members": 1},
        ]  # fmt: skip

    def test_group_no_op_and_nothing_to_change(self, service, fake):
        ann = ids(service)["Ann Lee"]
        with pytest.raises(ToolInputError, match="nothing to change"):
            service.update(ann, Changes(), add_groups=["Family"], remove_groups=["Work"])
        with pytest.raises(ToolInputError, match="nothing to change"):
            service.update(ann, Changes())
        with pytest.raises(ToolInputError, match="both"):
            service.update(ann, Changes(), add_groups=["Work"], remove_groups=["work"])
        assert fake.writes == []

    def test_always_reads_the_card_again(self, service, fake, book):
        ann = ids(service)["Ann Lee"]
        edited = ANN.replace("TITLE:Buyer", "TITLE:Buyer\r\nNICKNAME:Annie")
        book.put("ann-file.vcf", edited)  # changed in Contacts after the last refresh
        service.update(ann, Changes(values={"title": "Seller"}))
        assert fake.writes[0].headers["if-match"] == etag(edited)
        stored = book.cards["ann-file.vcf"]
        assert "NICKNAME:Annie" in stored and "TITLE:Seller" in stored
        service.update(ann, Changes(values={"title": "Boss"}))
        assert fake.writes[1].headers["if-match"] == etag(stored)  # never an earlier ETag

    def test_conflict_keeps_the_other_version(self, service, fake, book):
        ann = ids(service)["Ann Lee"]
        other = ANN.replace("TITLE:Buyer", "TITLE:Elsewhere")
        fake.before_write = lambda: book.put("ann-file.vcf", other)
        with pytest.raises(ToolInputError) as err:
            service.update(ann, Changes(values={"title": "Seller"}))
        assert str(err.value) == "the contact changed in Contacts; read it again"
        assert book.cards["ann-file.vcf"] == other and [w.status for w in fake.writes] == [412]

    def test_group_card_changed_part_way(self, service, fake, book):
        bob = ids(service)["Bob Smith"]

        def third():
            book.put("g-work.vcf", book.cards["g-work.vcf"].replace("kept", "edited"))

        def second():
            fake.before_write = third

        fake.before_write = lambda: setattr(fake, "before_write", second)
        with pytest.raises(ToolInputError) as err:
            service.update(bob, Changes(values={"title": "CEO"}), add_groups=["Work", "Family"])
        assert str(err.value) == (
            "the contact was saved and added to 1 of 2 groups; group Work changed in Contacts; "
            "read the contact again"
        )
        assert [(w.path.rsplit("/", 1)[-1], w.status) for w in fake.writes] == [
            ("bob-uid.vcf", 204), ("g-family.vcf", 204), ("g-work.vcf", 412),
        ]  # fmt: skip
        got = service.get(bob)  # the cache shows what was written
        assert got["title"] == "CEO" and got["groups"] == ["Family"]

    def test_partial_failure_other_than_a_conflict(self, service, fake):
        bob = ids(service)["Bob Smith"]
        fake.before_write = lambda: setattr(fake, "write_fail", (500, None))
        with pytest.raises(WriteFailed) as err:
            service.update(bob, Changes(), add_groups=["Family", "Work"])
        assert str(err.value) == (
            "added to 0 of 2 groups; iCloud did not confirm the change to group Family; read "
            "the contact again"
        )
        fake.write_fail = (403, None)
        with pytest.raises(WriteFailed) as err:
            ann = ids(service)["Ann Lee"]
            service.update(ann, Changes(), remove_groups=["Family"], add_groups=["Work"])
        assert str(err.value) == (
            "0 of 2 group changes were saved; group Family could not be changed; read the "
            "contact again"
        )

    def test_contact_already_gone(self, service, fake, book):
        bob = ids(service)["Bob Smith"]
        book.delete("bob-uid.vcf")
        with pytest.raises(ToolInputError, match="not found"):
            service.update(bob, Changes(values={"title": "x"}))
        assert fake.writes == []

    def test_unsafe_group_card(self, service, fake, book):
        book.put("g-work.vcf", book.cards["g-work.vcf"].replace("X-PV", "REV:1\r\nREV:2\r\nX-PV"))
        with pytest.raises(ToolInputError, match="cannot change this contact safely: group Work"):
            service.update(
                ids(service)["Bob Smith"], Changes(values={"title": "x"}), add_groups=["Work"]
            )
        assert fake.writes == []  # everything is checked before the first write


class TestVisibility:
    @pytest.mark.parametrize(
        "cid", ["nope", "g-family", "g-medical", "doc-uid", "../card/ann-file", "a/b", "bob-friend"]
    )
    def test_same_not_found_for_every_invisible_id(self, service, fake, cid):
        for call in (
            lambda: service.update(cid, Changes(values={"title": "x"})),
            lambda: service.delete(cid),
        ):
            with pytest.raises(ToolInputError) as err:
                call()
            assert str(err.value) == NOT_FOUND
        assert fake.writes == []

    def test_excluded_groups_look_missing(self, service, fake):
        ann = ids(service)["Ann Lee"]
        for kw in ({"add_groups": ["Medical"]}, {"remove_groups": ["MEDICAL"]}):
            with pytest.raises(ToolInputError, match="unknown group: "):
                service.update(ann, Changes(values={"title": "x"}), **kw)
        assert fake.writes == []

    def test_member_of_an_excluded_group(self, service, fake, book, clock):
        # Membership of excluded groups comes from the copy in memory (at most 60 s old).
        bob = ids(service)["Bob Smith"]
        book.put("g-medical.vcf", group_card("g-medical", "Medical", ["doc-uid", "bob-uid"]))
        clock.now += 61
        with pytest.raises(ToolInputError) as err:
            service.update(bob, Changes(values={"title": "x"}))
        assert str(err.value) == NOT_FOUND and fake.writes == []

    def test_group_card_renamed_into_an_excluded_group(self, service, fake, book):
        # A group card read again for the call wins over its copy in memory.
        ann = ids(service)["Ann Lee"]
        book.put("g-work.vcf", group_card("g-work", "Medical", []))
        with pytest.raises(ToolInputError) as err:
            service.update(ann, Changes(), add_groups=["Work"])
        assert str(err.value) == "unknown group: Work" and fake.writes == []

    def test_cards_that_cannot_be_changed_safely(self, service, fake):
        with pytest.raises(ToolInputError, match="cannot change this contact safely"):
            service.update(ids(service)["Erin Gale"], Changes(values={"title": "x"}))
        with pytest.raises(ToolInputError, match="cannot change this contact safely: note"):
            service.update(ids(service)["Carol Diaz"], Changes(values={"note": "x"}))
        assert fake.writes == []


class TestDelete:
    def test_delete(self, service, fake, book, clock):
        ann = ids(service)["Ann Lee"]
        clock.now += 5
        out = service.delete(ann)
        assert out == {"deleted": True, "id": ann}
        [write] = fake.writes
        assert (write.method, write.headers["if-match"]) == ("DELETE", etag(ANN))
        assert "ann-file.vcf" not in book.cards
        with pytest.raises(ToolInputError, match="not found"):
            service.get(ann)
        assert service.search(query="Ann")["contacts"] == []
        assert service.groups()["groups"][0] == {"name": "Family", "members": 0}
        # The group card keeps its member entry, as Apple's own clients do.
        assert "urn:uuid:ann-uid" in book.cards["g-family.vcf"]

    def test_the_me_card(self, service, fake):
        me = ids(service)["Alice Owner"]
        with pytest.raises(ToolInputError) as err:
            service.delete(me)
        assert str(err.value) == "this is the account owner's own card; it cannot be deleted"
        assert fake.writes == []
        assert service.update(me, Changes(values={"title": "Owner"}))["name"] == "Alice Owner"

    def test_delete_of_a_changed_card(self, service, fake, book):
        bob = ids(service)["Bob Smith"]
        fake.before_write = lambda: book.put("bob-uid.vcf", BOB.replace("Bob", "Rob"))
        with pytest.raises(ToolInputError, match="changed in Contacts"):
            service.delete(bob)
        assert "bob-uid.vcf" in book.cards


class TestRefreshAfterWrite:
    def test_one_sync_right_after_the_write(self, service, fake, clock):
        ann = ids(service)["Ann Lee"]
        clock.now += 10
        syncs = len(fake.sync_tokens)
        service.update(ann, Changes(values={"title": "Seller"}))
        assert len(fake.sync_tokens) == syncs + 1
        requests = len(fake.requests)
        assert service.get(ann)["title"] == "Seller"
        assert len(fake.requests) == requests  # no request: the copy is current

    def test_refresh_fails_after_the_write(self, service, fake):
        def fail_reads():
            fake.fail_reads = True

        ann = ids(service)["Ann Lee"]
        fake.before_write = fail_reads
        out = service.update(ann, Changes(values={"title": "Seller"}))
        assert out["name"] == "Ann Lee" and "note" in out
        got = service.get(ann)
        assert got["title"] == "Seller" and "not answering" in got["note"]
        fake.fail_reads = False
        fake.before_write = fail_reads
        created = service.create({"given_name": "Zoe"})
        assert service.get(created["id"])["name"] == "Zoe"
        fake.fail_reads = False
        fake.before_write = fail_reads
        service.delete(created["id"])
        with pytest.raises(ToolInputError, match="not found"):
            service.get(created["id"])
