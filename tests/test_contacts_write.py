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
