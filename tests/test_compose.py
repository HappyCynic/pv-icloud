# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

"""Building drafts (mail/compose.py): input limits, headers and reply threading."""

from __future__ import annotations

import datetime as dt
import email
import socket
from email import policy

import pytest

from pv_icloud.errors import ToolInputError
from pv_icloud.mail.compose import (
    MAX_ADDRESSES,
    MAX_BODY_CHARS,
    MAX_SUBJECT_CHARS,
    Original,
    Outgoing,
    addr_specs,
    build_draft,
    build_outgoing,
    check_draft,
    check_send,
    not_allowed,
    reply_subject,
)
from pv_icloud.mail.parse import parse_message

NOW = dt.datetime(2026, 10, 12, 18, 30, tzinfo=dt.UTC)


def check(**kw) -> None:
    args: dict = {"to": [], "cc": [], "bcc": [], "subject": None, "body": "", "in_reply_to": None}
    check_draft(**{**args, **kw})


def build(**kw) -> email.message.EmailMessage:
    args: dict = {"to": [], "cc": [], "bcc": [], "subject": None, "body": "", "now": NOW}
    raw = build_draft(**{**args, **kw})
    assert isinstance(raw, bytes)
    return email.message_from_bytes(raw, policy=policy.default)  # type: ignore[return-value]


class TestLimits:
    def test_limits_match_the_issue(self):
        assert (MAX_ADDRESSES, MAX_SUBJECT_CHARS, MAX_BODY_CHARS) == (50, 998, 100_000)

    def test_at_the_limits(self):
        check(
            to=[f"a{i}@example.com" for i in range(30)],
            cc=[f"c{i}@example.com" for i in range(10)],
            bcc=[f"b{i}@example.com" for i in range(10)],
            subject="s" * 998,
            body="b" * 100_000,
        )

    def test_too_many_addresses(self):
        with pytest.raises(ToolInputError, match="at most 50 addresses across to, cc and bcc"):
            check(
                to=[f"a{i}@example.com" for i in range(30)],
                bcc=[f"b{i}@example.com" for i in range(21)],
            )

    @pytest.mark.parametrize("value", ["a@example.com", "Bob <b@example.com>", "<c@example.com>"])
    def test_single_addresses_accepted(self, value):
        check(cc=[value])

    def test_display_name_with_a_comma(self):
        check(to=['"Doe, Jane" <jane@example.com>'])

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "   ",
            "a@example.com, b@example.com",
            "a@example.com; b@example.com",
            "friends: a@example.com, b@example.com;",
            "friends: a@example.com;",
            "no-at-sign",
            "@example.com",
            "local@",
            "Bob",
            "Bob <b@example.com> extra@example.com",
            "a@example.com\r\nBcc: evil@example.com",
            "a@example.com\nX: y",
            "a@exa\rmple.com",
            "zoë@example.com",
            "Zoë <z@exämple.com>",
        ],
    )
    def test_invalid_entries_named_by_position_not_value(self, value):
        with pytest.raises(ToolInputError) as err:
            check(to=["ok@example.com"], cc=["ok@example.com", value])
        assert str(err.value) == "cc[1] is not a single valid address"

    def test_non_string_entry(self):
        with pytest.raises(ToolInputError, match=r"^bcc\[0\] is not a single valid address$"):
            check(bcc=[42])

    @pytest.mark.parametrize(
        "subject", ["s" * 999, "Hi\r\nBcc: other@example.com", "Hi\nthere", "Hi\rthere"]
    )
    def test_subject(self, subject):
        with pytest.raises(ToolInputError) as err:
            check(to=["a@example.com"], subject=subject)
        assert str(err.value) == "subject must be at most 998 characters with no line breaks"

    def test_body_too_long(self):
        with pytest.raises(ToolInputError, match="^body must be at most 100000 characters$"):
            check(to=["a@example.com"], body="b" * 100_001)

    def test_nothing_to_draft(self):
        with pytest.raises(ToolInputError, match="^nothing to draft$"):
            check()
        with pytest.raises(ToolInputError, match="^nothing to draft$"):
            check(subject="", body="")

    @pytest.mark.parametrize(
        "kw",
        [{"to": ["a@example.com"]}, {"subject": "x"}, {"body": "x"}, {"in_reply_to": "id"}],
    )
    def test_any_one_input_is_enough(self, kw):
        check(**kw)

    def test_limits_checked_before_nothing_to_draft(self):
        with pytest.raises(ToolInputError, match="to\\[0\\]"):
            check(to=[""])


class TestHeaders:
    def test_plain_text_draft(self):
        msg = build(
            to=["Bob <bob@example.com>", "carol@example.com"],
            cc=["dave@example.com"],
            bcc=["eve@example.com"],
            subject="Lunch",
            body="See you at noon.\n",
        )
        assert msg["To"] == "Bob <bob@example.com>, carol@example.com"
        assert msg["Cc"] == "dave@example.com"
        assert msg["Bcc"] == "eve@example.com"  # kept in the draft, as Apple Mail does
        assert msg["Subject"] == "Lunch"
        assert msg["Date"].datetime == NOW
        assert msg.get_content_type() == "text/plain"
        assert msg.get_content_charset() == "utf-8"
        assert msg.get_content() == "See you at noon.\r\n"  # CRLF on the wire
        assert msg["From"] is None  # no send_from: Mail fills in the default sender
        assert msg["In-Reply-To"] is None and msg["References"] is None

    def test_from_is_send_from(self):
        assert build(subject="x", send_from="me@example.com")["From"] == "me@example.com"

    def test_message_id_never_names_this_host(self):
        msg = build(subject="x")
        mid = msg["Message-ID"]
        assert mid.startswith("<") and mid.endswith("@pv-icloud.invalid>")
        assert socket.getfqdn() not in mid and socket.gethostname() not in mid
        assert build(subject="x")["Message-ID"] != mid

    def test_crlf_line_endings(self):
        raw = build_draft(
            to=["a@example.com"], cc=[], bcc=[], subject="x", body="one\ntwo\n", now=NOW
        )
        assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")

    def test_omitted_headers(self):
        msg = build(body="only a body")
        for name in ("To", "Cc", "Bcc", "Subject", "From"):
            assert msg[name] is None

    def test_non_ascii_round_trips_through_the_parser(self):
        raw = build_draft(
            to=["Zoë Ünal <zoe@example.com>"],
            cc=[],
            bcc=[],
            subject="Grüße aus Köln — " + "ü" * 200,
            body="Schöne Grüße,\nJörg ☕\n" + "lang " * 100,
            now=NOW,
        )
        assert max(len(line) for line in raw.split(b"\r\n")) <= 998
        p = parse_message(raw)
        assert p.subject == "Grüße aus Köln — " + "ü" * 200
        assert p.body.startswith("Schöne Grüße,\nJörg ☕\n")
        assert p.to == "Zoë Ünal <zoe@example.com>"
        assert p.message_id is not None and p.message_id.endswith("@pv-icloud.invalid>")

    def test_empty_subject_is_omitted(self):
        assert build(subject="", body="x")["Subject"] is None


class TestReplies:
    def original(self, **kw) -> Original:
        args: dict = {
            "message_id": "<m2@x>",
            "in_reply_to": "<m1@x>",
            "references": ["<m0@x>", "<m1@x>"],
            "subject": "Plans",
        }
        return Original(**{**args, **kw})

    def test_threading_headers_from_references(self):
        msg = build(reply=self.original())
        assert msg["In-Reply-To"] == "<m2@x>"
        assert msg["References"] == "<m0@x> <m1@x> <m2@x>"
        assert msg["Subject"] == "Re: Plans"

    def test_references_fall_back_to_in_reply_to(self):
        msg = build(reply=self.original(references=[]))
        assert msg["References"] == "<m1@x> <m2@x>"

    def test_first_reply(self):
        msg = build(reply=self.original(references=[], in_reply_to=None))
        assert msg["In-Reply-To"] == "<m2@x>"
        assert msg["References"] == "<m2@x>"

    def test_original_without_message_id(self):
        msg = build(reply=self.original(message_id=None))
        assert msg["In-Reply-To"] is None and msg["References"] is None
        assert msg["Subject"] == "Re: Plans"

    def test_callers_subject_wins(self):
        assert build(subject="New topic", reply=self.original())["Subject"] == "New topic"

    def test_parsed_original_round_trip(self):
        raw = build_draft(to=[], cc=[], bcc=[], subject=None, body="x", now=NOW,
                          reply=self.original())  # fmt: skip
        p = parse_message(raw)
        assert (p.in_reply_to, p.references) == ("<m2@x>", ["<m0@x>", "<m1@x>", "<m2@x>"])

    @pytest.mark.parametrize(
        ("original", "expected"),
        [
            ("Plans", "Re: Plans"),
            ("Re: Plans", "Re: Plans"),
            ("RE: Plans", "RE: Plans"),
            ("re:Plans", "re:Plans"),
            ("Fwd: Plans", "Re: Fwd: Plans"),
            ("", "Re:"),
            (None, "Re:"),
            ("Reply needed", "Re: Reply needed"),
        ],
    )
    def test_re_subject(self, original, expected):
        assert reply_subject(original) == expected


# --- mail_send (add-mail-send) -------------------------------------------------------------


def check_s(**kw) -> None:
    args: dict = {"to": [], "cc": [], "bcc": [], "subject": None, "body": ""}
    check_send(**{**args, **kw})


def outgoing(**kw) -> Outgoing:
    args: dict = {
        "to": ["a@example.com"],
        "cc": [],
        "bcc": [],
        "subject": "Hi",
        "body": "Hello",
        "send_from": "me@example.net",
        "now": NOW,
    }
    return build_outgoing(**{**args, **kw})


class TestSendInput:
    def test_needs_a_recipient(self):
        for kw in ({}, {"subject": "x", "body": "y"}):
            with pytest.raises(ToolInputError, match="^at least one recipient is required"):
                check_s(**kw)
        check_s(bcc=["a@example.com"])

    @pytest.mark.parametrize(
        ("kw", "message"),
        [
            ({"to": [f"a{i}@example.com" for i in range(51)]}, "at most 50 addresses"),
            ({"to": ["a@example.com"], "cc": ["nope"]}, r"cc\[0\]"),
            ({"to": ["a@example.com"], "subject": "x" * 999}, "subject"),
            ({"to": ["a@example.com"], "subject": "a\nBcc: b@x.org"}, "subject"),
            ({"to": ["a@example.com"], "body": "b" * 100_001}, "100000"),
        ],
    )
    def test_shares_the_draft_limits(self, kw, message):
        with pytest.raises(ToolInputError, match=message):
            check_s(**kw)

    def test_addr_specs(self):
        got = addr_specs(["Bob <Bob@Example.COM>", "bob@example.com", "c@example.org"])
        assert got == ["bob@example.com", "c@example.org"]

    def test_not_allowed(self):
        allow = ("a@example.com", "@example.org")
        addresses = ["a@example.com", "b@example.org", "c@mail.example.org", "d@example.com"]
        assert not_allowed(addresses, allow) == ["c@mail.example.org", "d@example.com"]
        assert not_allowed(["x@example.org.evil.test"], allow) == ["x@example.org.evil.test"]
        assert not_allowed([], allow) == []


class TestOutgoing:
    def test_sent_bytes_have_no_bcc_and_the_copy_has_it(self):
        out = outgoing(cc=["c@example.com"], bcc=["Hidden <hidden@example.org>"])
        sent = email.message_from_bytes(out.sent, policy=policy.default)
        copy = email.message_from_bytes(out.copy, policy=policy.default)
        assert sent["Bcc"] is None and b"hidden" not in out.sent.lower()
        assert copy["Bcc"] == "Hidden <hidden@example.org>"
        for name in ("From", "To", "Cc", "Subject", "Date", "Message-ID"):
            assert sent[name] == copy[name], name
        assert sent["From"] == "me@example.net"
        assert out.message_id == sent["Message-ID"]

    def test_message_id_uses_the_senders_domain_never_the_host_name(self):
        out = outgoing()
        assert out.message_id.startswith("<") and out.message_id.endswith("@example.net>")
        assert socket.gethostname() not in out.message_id

    def test_body_is_seven_bit_clean(self):
        out = outgoing(subject="Grüße", body="Grüße aus Köln\n" * 3)
        assert out.sent.isascii() and out.copy.isascii()
        p = parse_message(out.sent)
        assert (p.subject, p.body.strip().splitlines()[0]) == ("Grüße", "Grüße aus Köln")

    def test_reply_threading(self):
        reply = Original("<m2@x>", "<m1@x>", ["<m1@x>"], "Plans")
        p = parse_message(outgoing(subject=None, reply=reply).sent)
        assert (p.in_reply_to, p.references, p.subject) == (
            "<m2@x>", ["<m1@x>", "<m2@x>"], "Re: Plans"
        )  # fmt: skip
