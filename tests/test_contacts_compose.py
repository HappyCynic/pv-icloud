# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

"""contacts/compose.py: new vCards and lossless in-place edits (add-contacts-write)."""

from __future__ import annotations

import datetime as dt
import difflib
import re

import pytest

from pv_icloud.contacts import compose
from pv_icloud.contacts.compose import Changes
from pv_icloud.contacts.vcard import parse_vcard
from pv_icloud.errors import ToolInputError

from .fakecarddav import ANN, BOB, CAROL, DAVE, ERIN, FAMILY, PHOTO_DATA, WIDGETS, WORK, vcard

NOW = dt.datetime(2026, 10, 14, 12, 0, 0, tzinfo=dt.UTC)
REV = "REV:2026-10-14T12:00:00Z"


def logical(text: str) -> list[str]:
    """The card's logical lines, each with its physical bytes (folds included)."""
    return [x for x in re.split(r"(?<=\n)(?![ \t])", text) if x]


def diff(before: str, after: str) -> tuple[list[str], list[str]]:
    """(removed, added) logical lines; every other line is identical and in order."""
    a, b = logical(before), logical(after)
    removed: list[str] = []
    added: list[str] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if tag != "equal":
            removed += a[i1:i2]
            added += b[j1:j2]
    return [x.rstrip("\r\n") for x in removed], [x.rstrip("\r\n") for x in added]


def update(text: str, **kw) -> str:
    out = compose.apply_update(text, Changes(**kw), NOW)
    assert out is not None
    return out


class TestNewCard:
    def test_minimal_person(self):
        text = compose.new_card("NEW-UID", {"given_name": "Ann", "family_name": "Lee"}, NOW)
        assert text == (
            "BEGIN:VCARD\r\nVERSION:3.0\r\nPRODID:-//pv-icloud//EN\r\nN:Lee;Ann;;;\r\n"
            f"FN:Ann Lee\r\nUID:NEW-UID\r\n{REV}\r\nEND:VCARD\r\n"
        )
        card = parse_vcard(text)
        assert (card.uid, card.name, card.given, card.family) == (
            "NEW-UID",
            "Ann Lee",
            "Ann",
            "Lee",
        )

    def test_company_card(self):
        text = compose.new_card("U", {"organization": "Widgets", "phones": [{"value": "1"}]}, NOW)
        assert "X-ABShowAs:COMPANY\r\n" in text and "ORG:Widgets;\r\n" in text
        assert "FN:Widgets\r\n" in text and "N:;;;;\r\n" in text
        assert parse_vcard(text).name == "Widgets"
        person = compose.new_card("U", {"given_name": "Ann", "organization": "Widgets"}, NOW)
        assert "X-ABShowAs" not in person

    def test_every_field_reads_back(self):
        fields = {
            "prefix": "Dr.",
            "given_name": "Ann",
            "middle_name": "Q.",
            "family_name": "Lee",
            "suffix": "Jr.",
            "nickname": "Annie",
            "maiden_name": "Park",
            "organization": "Widgets, Inc.",
            "department": "Sales; East",
            "title": "Buyer",
            "emails": [{"label": "work", "value": "ann@widgets.example", "preferred": True}],
            "phones": [{"label": "mobile", "value": "+1 312 555 0100"}],
            "addresses": [
                {
                    "label": "home",
                    "street": "1 Main St",
                    "city": "Springfield",
                    "region": "IL",
                    "postal_code": "62701",
                    "country": "United States",
                },
            ],  # fmt: skip
            "urls": [{"label": "homepage", "value": "https://ann.example/a,b"}],
            "social_profiles": [{"service": "LinkedIn", "user": "annlee"}],
            "instant_messaging": [{"service": "Skype", "handle": "ann.lee"}],
            "birthday": "--03-15",
            "dates": [{"label": "Graduation", "value": "2001-06-01"}],
            "related_names": [{"label": "sister", "value": "Bo Lee"}],
            "note": "Met at the fair, 2024;\nLikes tea \\ coffee",
        }
        card = parse_vcard(compose.new_card("U", fields, NOW))
        assert card.name == "Dr. Ann Q. Lee Jr."
        assert (card.prefix, card.given, card.middle, card.family, card.suffix) == (
            "Dr.", "Ann", "Q.", "Lee", "Jr.",
        )  # fmt: skip
        assert (card.nickname, card.maiden_name, card.title) == ("Annie", "Park", "Buyer")
        assert (card.organization, card.department) == ("Widgets, Inc.", "Sales; East")
        assert card.emails == [{"label": "work", "value": "ann@widgets.example", "preferred": True}]
        assert card.phones == [{"label": "mobile", "value": "+1 312 555 0100", "preferred": False}]
        [addr] = card.addresses
        assert (addr["label"], addr["street"], addr["postal_code"]) == (
            "home",
            "1 Main St",
            "62701",
        )
        assert card.urls == [{"label": "homepage", "value": "https://ann.example/a,b"}]
        assert card.social_profiles == [{"service": "linkedin", "user": "annlee", "url": None}]
        assert card.instant_messaging == [{"service": "Skype", "handle": "ann.lee"}]
        assert card.birthday == "--03-15"
        assert card.dates == [{"label": "Graduation", "value": "2001-06-01"}]
        assert card.related_names == [{"label": "sister", "value": "Bo Lee"}]
        assert card.note == "Met at the fair, 2024;\nLikes tea \\ coffee"

    def test_birthdays_and_dates(self):
        text = compose.new_card(
            "U",
            {"given_name": "A", "birthday": "1980-03-15", "dates": [{"value": "--02-29"}]},
            NOW,
        )
        assert "BDAY:1980-03-15\r\n" in text
        assert "item1.X-ABDATE;X-APPLE-OMIT-YEAR=1604:1604-02-29\r\n" in text
        card = parse_vcard(text)
        assert card.birthday == "1980-03-15" and card.dates == [{"label": None, "value": "--02-29"}]
        no_year = compose.new_card("U", {"given_name": "A", "birthday": "--03-15"}, NOW)
        assert "BDAY;X-APPLE-OMIT-YEAR=1604:1604-03-15\r\n" in no_year

    def test_folding_at_75_octets(self):
        note = "é" * 100 + "x" * 100
        text = compose.new_card("U", {"given_name": "A", "note": note}, NOW)
        for line in text.split("\r\n"):
            assert len(line.encode()) <= 75, line
        assert parse_vcard(text).note == note

    def test_no_photo_is_ever_written(self):
        text = compose.new_card("U", {"given_name": "A", "note": "PHOTO:x"}, NOW)
        assert not any(x.startswith("PHOTO") for x in text.split("\r\n"))


# (field, entry, the written line up to its value, the label read back)
LABEL_TABLE = [
    ("phones", {"label": "mobile"}, "TEL;TYPE=CELL;TYPE=VOICE:", "mobile"),
    ("phones", {"label": "Mobile"}, "TEL;TYPE=CELL;TYPE=VOICE:", "mobile"),
    ("phones", {"label": "iPhone"}, "TEL;TYPE=IPHONE;TYPE=CELL;TYPE=VOICE:", "iPhone"),
    ("phones", {"label": "home"}, "TEL;TYPE=HOME;TYPE=VOICE:", "home"),
    ("phones", {"label": "work"}, "TEL;TYPE=WORK;TYPE=VOICE:", "work"),
    ("phones", {"label": "main"}, "TEL;TYPE=MAIN:", "main"),
    ("phones", {"label": "home fax"}, "TEL;TYPE=HOME;TYPE=FAX:", "home fax"),
    ("phones", {"label": "work fax"}, "TEL;TYPE=WORK;TYPE=FAX:", "work fax"),
    ("phones", {"label": "pager"}, "TEL;TYPE=PAGER:", "pager"),
    ("phones", {}, "TEL:", None),
    ("phones", {"preferred": True}, "TEL;TYPE=PREF:", None),
    ("phones", {"label": "mobile", "preferred": True}, "TEL;TYPE=CELL;TYPE=VOICE;TYPE=PREF:",
     "mobile"),
    ("phones", {"label": "main", "preferred": True}, "TEL;TYPE=MAIN;TYPE=PREF:", "main"),
    ("phones", {"label": "other"}, "item1.TEL:", "other"),
    ("phones", {"label": "Boat"}, "item1.TEL:", "Boat"),
    ("phones", {"label": "Boat", "preferred": True}, "item1.TEL;TYPE=PREF:", "Boat"),
    ("emails", {"label": "home"}, "EMAIL;TYPE=INTERNET;TYPE=HOME:", "home"),
    ("emails", {"label": "work"}, "EMAIL;TYPE=INTERNET;TYPE=WORK:", "work"),
    ("emails", {}, "EMAIL;TYPE=INTERNET:", None),
    ("emails", {"label": "home", "preferred": True}, "EMAIL;TYPE=INTERNET;TYPE=HOME;TYPE=PREF:",
     "home"),
    ("emails", {"preferred": True}, "EMAIL;TYPE=INTERNET;TYPE=PREF:", None),
    ("emails", {"label": "school"}, "item1.EMAIL;TYPE=INTERNET:", "school"),
    ("emails", {"label": "other", "preferred": True}, "item1.EMAIL;TYPE=INTERNET;TYPE=PREF:",
     "other"),
    ("emails", {"label": "iCloud"}, "item1.EMAIL;TYPE=INTERNET:", "iCloud"),
    ("addresses", {"label": "home"}, "ADR;TYPE=HOME:", "home"),
    ("addresses", {"label": "work"}, "ADR;TYPE=WORK:", "work"),
    ("addresses", {"label": "other"}, "item1.ADR:", "other"),
    ("addresses", {}, "ADR:", None),
    ("urls", {"label": "home"}, "URL;TYPE=HOME:", "home"),
    ("urls", {"label": "work"}, "URL;TYPE=WORK:", "work"),
    ("urls", {"label": "homepage"}, "item1.URL:", "homepage"),
    ("urls", {"label": "blog"}, "item1.URL:", "blog"),
    ("dates", {"label": "anniversary"}, "item1.X-ABDATE:", "anniversary"),
    ("dates", {"label": "Graduation"}, "item1.X-ABDATE:", "Graduation"),
    ("dates", {}, "item1.X-ABDATE:", None),
    ("related_names", {"label": "mother"}, "item1.X-ABRELATEDNAMES:", "mother"),
    ("related_names", {"label": "spouse"}, "item1.X-ABRELATEDNAMES:", "spouse"),
    ("related_names", {"label": "Coach"}, "item1.X-ABRELATEDNAMES:", "Coach"),
]  # fmt: skip
APPLE_LABELS = {
    "other": "Other", "school": "School", "homepage": "HomePage", "anniversary": "Anniversary",
    "mother": "Mother", "father": "Father", "parent": "Parent", "brother": "Brother",
    "sister": "Sister", "child": "Child", "friend": "Friend", "spouse": "Spouse",
    "partner": "Partner", "assistant": "Assistant", "manager": "Manager",
}  # fmt: skip
VALUES = {
    "phones": {"value": "+1 312 555 0100"},
    "emails": {"value": "ann@example.com"},
    "addresses": {"street": "1 Main St", "city": "Springfield"},
    "urls": {"value": "https://ann.example"},
    "dates": {"value": "2001-06-01"},
    "related_names": {"value": "Bo Lee"},
}


@pytest.mark.parametrize(("field", "entry", "written", "label"), LABEL_TABLE)
def test_label_table(field, entry, written, label):
    """Design decision 3: the exact bytes Apple's apps write, and the label read back."""
    value = {**VALUES[field], **entry}
    text = compose.new_card("U", {"given_name": "A", field: [value]}, NOW)
    [line] = [x for x in text.split("\r\n") if x.split(":")[0] + ":" == written]
    group = written.split(".")[0] if written.startswith("item") else None
    if group and label is not None:
        key = label.casefold()
        expected = f"_$!<{APPLE_LABELS[key]}>!$_" if key in APPLE_LABELS else label
        assert f"{group}.X-ABLabel:{expected}" in text.split("\r\n")
    elif group:
        assert f"{group}.X-ABLabel" not in text
    [got] = getattr(parse_vcard(text), field)
    assert got["label"] == label
    if "preferred" in got:
        assert got["preferred"] is bool(entry.get("preferred"))
    assert line


def test_every_apple_label_reads_back():
    for key in APPLE_LABELS:
        text = compose.new_card(
            "U", {"given_name": "A", "related_names": [{"label": key, "value": "X"}]}, NOW
        )
        assert parse_vcard(text).related_names[0]["label"] == key


class TestInPlace:
    def test_change_one_scalar(self):
        after = update(ANN, values={"title": "Head Buyer"})
        assert diff(ANN, after) == (
            ["TITLE:Buyer", "REV:2026-01-02T03:04:05Z"],
            ["TITLE:Head Buyer", REV],
        )
        assert PHOTO_DATA[:40] in after and after.count("PHOTO") == 1

    @pytest.mark.parametrize(
        ("values", "removed", "added"),
        [
            ({"nickname": "Annie"}, [], ["NICKNAME:Annie"]),
            ({"maiden_name": "Park"}, [], ["X-MAIDENNAME:Park"]),
            ({"note": "New, note"}, ["NOTE:Met at the fair\\, 2024\\nLikes tea"],
             ["NOTE:New\\, note"]),
            ({"birthday": "1980-03-15"}, ["BDAY;X-APPLE-OMIT-YEAR=1604:1604-03-15"],
             ["BDAY:1980-03-15"]),
            ({"department": "Purchasing"}, ["ORG:Widgets;Sales"], ["ORG:Widgets;Purchasing"]),
        ],
    )  # fmt: skip
    def test_scalars(self, values, removed, added):
        # A replaced line stays where it was; a new one goes just before END:VCARD.
        after = update(ANN, values=values)
        got_removed, got_added = diff(ANN, after)
        assert got_removed == removed + ["REV:2026-01-02T03:04:05Z"]
        assert sorted(got_added) == sorted(added + [REV])

    def test_clear(self):
        after = update(ANN, clear=frozenset({"title", "birthday"}))
        assert diff(ANN, after)[0] == [
            "TITLE:Buyer",
            "BDAY;X-APPLE-OMIT-YEAR=1604:1604-03-15",
            "REV:2026-01-02T03:04:05Z",
        ]
        card = parse_vcard(after)
        assert card.title is None and card.birthday is None
        org = update(ANN, clear=frozenset({"organization"}))
        assert "ORG:;Sales\r\n" in org
        gone = update(ANN, clear=frozenset({"organization", "department"}))
        assert "ORG" not in gone

    def test_name_part_rewrites_derived_display_name(self):
        after = update(ANN, values={"family_name": "Park"})
        assert diff(ANN, after) == (
            ["N:Lee;Ann;;;", "FN:Ann Lee", "REV:2026-01-02T03:04:05Z"],
            ["N:Park;Ann;;;", "FN:Ann Park", REV],
        )
        assert parse_vcard(after).name == "Ann Park"

    def test_custom_display_name_is_kept(self):
        after = update(BOB, values={"family_name": "Jones"})
        removed, added = diff(BOB, after)
        assert removed == ["N:Smith;Robert;;;"]
        assert added == ["N:Jones;Robert;;;", REV]  # no REV before: one is added
        assert parse_vcard(after).name == "Bob Smith"
        assert after.index(REV) > after.index("UID:bob-uid")  # just before END:VCARD

    def test_two_display_names_are_kept(self):
        two = ANN.replace("FN:Ann Lee\r\n", "FN:Ann Lee\r\nFN:Ann Lee\r\n")
        after = update(two, values={"given_name": "Anne"})
        assert after.count("FN:Ann Lee\r\n") == 2

    def test_company_display_name_follows_organization(self):
        after = update(WIDGETS, values={"organization": "Widgets Inc"})
        assert diff(WIDGETS, after) == (
            ["FN:Widgets", "ORG:Widgets;", "REV:2026-02-03T04:05:06Z"],
            ["FN:Widgets Inc", "ORG:Widgets Inc;", REV],
        )
        assert parse_vcard(after).name == "Widgets Inc"
        named = update(WIDGETS, values={"given_name": "Ann"})
        assert "FN:Widgets\r\n" in named and "N:;Ann;;;\r\n" in named
        custom = WIDGETS.replace("FN:Widgets", "FN:Widgets Shop")
        assert "FN:Widgets Shop\r\n" in update(custom, values={"organization": "W2"})

    def test_n_and_org_keep_parameters_and_other_components(self):
        text = ANN.replace("N:Lee;Ann;;;", "N;X-PV=1:Lee\\;Kim;Ann;;Ms.;").replace(
            "ORG:Widgets;Sales", "ORG;X-PV=2:Widgets\\, Inc.;Sales"
        )
        after = update(text, values={"given_name": "Anne", "department": "Purchasing"})
        assert "N;X-PV=1:Lee\\;Kim;Anne;;Ms.;\r\n" in after
        assert "ORG;X-PV=2:Widgets\\, Inc.;Purchasing\r\n" in after

    def test_rev_is_replaced_in_place(self):
        after = update(ANN, values={"title": "x"})
        assert logical(after).index(REV + "\r\n") == logical(ANN).index(
            "REV:2026-01-02T03:04:05Z\r\n"
        )

    def test_unchanged_text_stays_byte_for_byte(self):
        lf = ANN.replace("\r\n", "\n")
        after = update(lf, values={"title": "x"})
        assert "\r" not in after  # the card's own line ending
        assert diff(lf, after) == (
            ["TITLE:Buyer", "REV:2026-01-02T03:04:05Z"],
            ["TITLE:x", REV],
        )


class TestLists:
    def test_new_item_group_numbers(self):
        after = update(ANN, add={"phones": [{"label": "Boat", "value": "1"}]})
        assert diff(ANN, after)[1] == [REV, "item7.TEL:1", "item7.X-ABLabel:Boat"]

    def test_add_every_list_field(self):
        add = {
            "emails": [{"label": "home", "value": "a@b.example"}],
            "phones": [{"label": "work", "value": "2"}],
            "addresses": [{"label": "work", "city": "Chicago"}],
            "urls": [{"value": "https://x.example"}],
            "social_profiles": [{"service": "twitter", "user": "ann", "url": "https://x.com/ann"}],
            "instant_messaging": [{"service": "Jabber", "handle": "ann@jabber.example"}],
            "dates": [{"label": "Graduation", "value": "--06-01"}],
            "related_names": [{"label": "brother", "value": "Cy Lee"}],
        }
        after = update(ANN, add=add)
        removed, added = diff(ANN, after)
        assert removed == ["REV:2026-01-02T03:04:05Z"]
        assert added == [
            REV,
            "EMAIL;TYPE=INTERNET;TYPE=HOME:a@b.example",
            "TEL;TYPE=WORK;TYPE=VOICE:2",
            "ADR;TYPE=WORK:;;;Chicago;;;",
            "URL:https://x.example",
            "X-SOCIALPROFILE;TYPE=twitter;X-USER=ann:https://x.com/ann",
            "IMPP;X-SERVICE-TYPE=Jabber:x-apple:ann@jabber.example",
            "item7.X-ABDATE;X-APPLE-OMIT-YEAR=1604:1604-06-01",
            "item7.X-ABLabel:Graduation",
            "item8.X-ABRELATEDNAMES:Cy Lee",
            "item8.X-ABLabel:_$!<Brother>!$_",
        ]
        card = parse_vcard(after)
        assert card.instant_messaging[-1] == {"service": "Jabber", "handle": "ann@jabber.example"}
        assert card.addresses[-1]["city"] == "Chicago"

    @pytest.mark.parametrize(
        ("remove", "lines"),
        [
            ({"phones": [{"label": "mobile", "value": "+1 312 555 0100", "preferred": True}]},
             ["TEL;TYPE=CELL;TYPE=VOICE;TYPE=PREF:+1 312 555 0100"]),
            ({"phones": [{"label": "Boat", "value": "+1 312 555 0107"}]},
             ["item2.TEL:+1 312 555 0107", "item2.X-ABLabel:Boat"]),
            ({"emails": [{"label": "other", "value": "ann.lee@mail.example"}]},
             ["item1.EMAIL;TYPE=INTERNET:ann.lee@mail.example", "item1.X-ABLabel:_$!<Other>!$_"]),
            ({"addresses": [{"label": "home", "street": "1 Main St", "city": "Springfield",
                             "region": "IL", "postal_code": "62701", "country": "United States",
                             "formatted": "ignored"}]},
             ["item3.ADR;TYPE=HOME;TYPE=PREF:;;1 Main St;Springfield;IL;62701;United States",
              "item3.X-ABADR:us"]),
            ({"related_names": [{"label": "sister", "value": "Bo Lee"}]},
             ["item4.X-ABRELATEDNAMES;TYPE=PREF:Bo Lee", "item4.X-ABLabel:_$!<Sister>!$_"]),
            ({"dates": [{"label": "anniversary", "value": "--06-01"}]},
             ["item5.X-ABDATE;X-APPLE-OMIT-YEAR=1604:1604-06-01",
              "item5.X-ABLabel:_$!<Anniversary>!$_"]),
            ({"social_profiles": [{"service": "linkedin", "user": "annlee",
                                   "url": "https://www.linkedin.com/in/annlee"}]},
             ["X-SOCIALPROFILE;TYPE=linkedin;X-USER=annlee:https://www.linkedin.com/in/annlee"]),
            ({"instant_messaging": [{"service": "Skype", "handle": "ann.lee"}]},
             ["IMPP;X-SERVICE-TYPE=Skype;TYPE=PREF:skype:ann.lee"]),
        ],
    )  # fmt: skip
    def test_remove_with_its_item_group(self, remove, lines):
        after = update(ANN, remove=remove)
        removed, added = diff(ANN, after)
        assert [x.replace("\r\n ", "") for x in removed] == [*lines, "REV:2026-01-02T03:04:05Z"]
        assert added == [REV]

    def test_remove_a_folded_value(self):
        after = update(
            ANN, remove={"urls": [{"label": "homepage", "value": "https://ann.example"}]}
        )
        removed, _ = diff(ANN, after)
        assert removed[0].startswith("item6.URL;TYPE=PREF;X-PV-NOTE=") and "\r\n " in removed[0]
        assert removed[1] == "item6.X-ABLabel:_$!<HomePage>!$_"

    def test_fix_a_phone(self):
        after = update(
            ANN,
            remove={"phones": [{"label": "mobile", "value": "+1 312 555 0100"}]},
            add={"phones": [{"label": "mobile", "value": "+1 312 555 0199"}]},
        )
        phones = [(p["label"], p["value"]) for p in parse_vcard(after).phones]
        assert ("mobile", "+1 312 555 0199") in phones
        assert ("mobile", "+1 312 555 0100") not in phones and len(phones) == 8

    def test_remove_matches_every_shown_field(self):
        with pytest.raises(ToolInputError, match="not found on this contact: phones"):
            compose.apply_update(
                ANN,
                Changes(remove={"phones": [{"label": "work", "value": "+1 312 555 0100"}]}),
                NOW,
            )
        with pytest.raises(ToolInputError, match="not found on this contact: emails"):
            compose.apply_update(ANN, Changes(remove={"emails": [{"value": "x@y.example"}]}), NOW)
        # Removing the same value twice: the second finds nothing.
        twice = [{"label": "Boat", "value": "+1 312 555 0107"}] * 2
        with pytest.raises(ToolInputError, match="not found on this contact: phones"):
            compose.apply_update(ANN, Changes(remove={"phones": twice}), NOW)

    def test_ambiguous_remove_is_refused(self):
        two = ANN.replace(
            "TEL:+1 312 555 0106\r\n", "TEL:+1 312 555 0106\r\nTEL:+1 312 555 0106\r\n"
        )
        with pytest.raises(ToolInputError, match="cannot change this contact safely: phones"):
            compose.apply_update(
                two, Changes(remove={"phones": [{"value": "+1 312 555 0106"}]}), NOW
            )

    def test_shared_item_group_is_refused(self):
        shared = ANN.replace("item2.X-ABLabel:Boat", "item2.X-ABLabel:Boat\r\nitem2.NOTE:x")
        with pytest.raises(ToolInputError, match="cannot change this contact safely: phones"):
            compose.apply_update(
                shared, Changes(remove={"phones": [{"label": "Boat", "value": "+1 312 555 0107"}]}),
                NOW,
            )  # fmt: skip


class TestRefusals:
    def test_duplicated_single_valued_property(self):
        with pytest.raises(ToolInputError, match="cannot change this contact safely: note"):
            compose.apply_update(CAROL, Changes(values={"note": "x"}), NOW)
        # Other changes to the same card are fine.
        assert "TITLE:x" in update(CAROL, values={"title": "x"})

    def test_duplicated_name_or_org_only_matters_when_changed(self):
        two = ANN.replace("N:Lee;Ann;;;\r\n", "N:Lee;Ann;;;\r\nN:Lee;Ann;;;\r\n").replace(
            "ORG:Widgets;Sales\r\n", "ORG:Widgets;Sales\r\nORG:Other;\r\n"
        )
        assert "TITLE:x" in update(two, values={"title": "x"})
        with pytest.raises(ToolInputError, match="safely: given_name"):
            compose.apply_update(two, Changes(values={"given_name": "x"}), NOW)
        with pytest.raises(ToolInputError, match="safely: department"):
            compose.apply_update(two, Changes(values={"department": "x"}), NOW)

    def test_two_rev_lines(self):
        with pytest.raises(ToolInputError, match="cannot change this contact safely: REV"):
            compose.apply_update(DAVE, Changes(values={"title": "x"}), NOW)

    def test_vcard_4(self):
        with pytest.raises(ToolInputError, match="cannot change this contact safely"):
            compose.apply_update(ERIN, Changes(values={"title": "x"}), NOW)

    @pytest.mark.parametrize(
        "broken",
        [
            ANN.replace("TITLE:Buyer", "TITLE Buyer"),
            " " + ANN,
            ANN.replace("END:VCARD\r\n", ""),
            ANN + ANN,
        ],
    )
    def test_unsplittable_text(self, broken):
        with pytest.raises(ToolInputError, match="cannot change this contact safely"):
            compose.apply_update(broken, Changes(values={"title": "x"}), NOW)


class TestGroups:
    def test_add_member(self):
        after = compose.add_member(WORK, "ann-uid", NOW)
        assert after is not None
        assert diff(WORK, after) == (
            [],
            ["X-ADDRESSBOOKSERVER-MEMBER:urn:uuid:ann-uid", REV],
        )
        assert parse_vcard(after).members == ["gone-uid", "ann-uid"]
        assert compose.add_member(after, "ann-uid", NOW) is None  # already a member

    def test_remove_member_keeps_hidden_members(self):
        after = compose.remove_member(FAMILY, "ann-uid", NOW)
        assert after is not None
        assert diff(FAMILY, after) == (
            ["X-ADDRESSBOOKSERVER-MEMBER:urn:uuid:ann-uid", "REV:2026-01-05T00:00:00Z"],
            [REV],
        )
        assert parse_vcard(after).members == ["doc-uid"]  # hidden from callers, still kept
        assert compose.remove_member(after, "ann-uid", NOW) is None

    def test_group_with_two_rev_lines(self):
        bad = FAMILY.replace("REV:2026-01-05T00:00:00Z", "REV:1\r\nREV:2")
        with pytest.raises(ToolInputError, match="cannot change this contact safely: REV"):
            compose.add_member(bad, "x", NOW)


class TestLimits:
    @pytest.mark.parametrize(
        ("fields", "field"),
        [
            ({"given_name": "x" * 256}, "given_name"),
            ({"title": "a\nb"}, "title"),
            ({"note": "x" * 20_001}, "note"),
            ({"emails": [{"value": "no-at"}]}, "emails"),
            ({"emails": [{"value": "a@b@c"}]}, "emails"),
            ({"phones": [{"value": "1" * 1001}]}, "phones"),
            ({"phones": [{"value": "1", "label": "x" * 101}]}, "phones"),
            ({"phones": [{"value": "1", "label": "a\rb"}]}, "phones"),
            ({"phones": [{"value": ""}]}, "phones"),
            ({"phones": [{"value": "1"}] * 21}, "phones"),
            ({"phones": [{"value": "1", "extra": 1}]}, "phones"),
            ({"phones": ["1"]}, "phones"),
            ({"urls": [{"value": "ftp://x.example"}]}, "urls"),
            ({"addresses": [{"label": "home"}]}, "addresses"),
            ({"addresses": [{"street": "x" * 1001}]}, "addresses"),
            ({"birthday": "--02-30"}, "birthday"),
            ({"birthday": "1980-13-01"}, "birthday"),
            ({"birthday": "15.03.1980"}, "birthday"),
            ({"dates": [{"value": "2001-02-29"}]}, "dates"),
            ({"social_profiles": [{"service": "x"}]}, "social_profiles"),
            ({"social_profiles": [{"service": "x", "url": "javascript:x"}]}, "social_profiles"),
            ({"social_profiles": [{"service": "a,b", "user": "x"}]}, "social_profiles"),
            ({"instant_messaging": [{"service": "x"}]}, "instant_messaging"),
            ({"instant_messaging": [{"service": "x" * 101, "handle": "y"}]}, "instant_messaging"),
        ],
    )
    def test_create_limits(self, fields, field):
        with pytest.raises(ToolInputError, match=field):
            compose.check_new({"given_name": "A", **fields})

    def test_create_needs_a_name_organization_email_or_phone(self):
        with pytest.raises(ToolInputError, match="name, organization, email or phone"):
            compose.check_new({"note": "only a note"})
        for ok in ({"family_name": "A"}, {"organization": "W"}, {"emails": [{"value": "a@b"}]}):
            compose.check_new(ok)

    def test_update_limits(self):
        with pytest.raises(ToolInputError, match="nothing to change"):
            compose.check_changes(Changes())
        compose.check_changes(Changes(), groups=True)  # a group-only update
        with pytest.raises(ToolInputError, match="title"):
            compose.check_changes(Changes(values={"title": "x"}, clear=frozenset({"title"})))
        with pytest.raises(ToolInputError, match="clear"):
            compose.check_changes(Changes(clear=frozenset({"emails"})))
        with pytest.raises(ToolInputError, match="20"):
            compose.check_changes(
                Changes(
                    add={"phones": [{"value": "1"}] * 11}, remove={"phones": [{"value": "1"}] * 10}
                )
            )
        with pytest.raises(ToolInputError, match="unknown field: colour"):
            compose.check_changes(Changes(add={"colour": [{"value": "x"}]}))
        with pytest.raises(ToolInputError, match="10 group"):
            compose.check_groups(["g"] * 11)
        # Entries to remove are matched as read: a social profile with no service is fine.
        compose.check_changes(Changes(remove={"social_profiles": [{"service": None, "user": "x"}]}))


def test_the_parsed_model_is_never_used_to_write():
    # Every line the reader drops survives an update untouched.
    text = vcard(
        "u",
        'X-PV-ONE;X-Q="a;b":one',
        "item9.X-PV-TWO:two",
        f"PHOTO;ENCODING=b;TYPE=JPEG:{PHOTO_DATA}",
        "X-ABUID:u:ABPerson",
        fn="Ann Lee",
        n="Lee;Ann;;;",
    )
    after = update(text, values={"nickname": "A"})
    assert diff(text, after) == ([], ["NICKNAME:A", REV])
