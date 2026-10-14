# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

"""Contact writes (add-contacts-write, ADR-0006) against the fake CardDAV server."""

from __future__ import annotations

import httpx
import pytest

from pv_icloud.contacts.carddav import CardDavClient
from pv_icloud.contacts.vcard import parse_vcard

from .fakecarddav import ANN, HOME_HOST, FakeCardDav, etag, write_book

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
