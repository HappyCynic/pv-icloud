# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import datetime as dt

import pytest

from pv_icloud import utf7
from pv_icloud.auth import TokenTable
from pv_icloud.config import ConfigError, parse_config, send_warnings
from pv_icloud.mail.ids import BadMailId, MailId, decode_id
from pv_icloud.mail.imap import _parse_fetch, uid_set
from pv_icloud.mail.index import fts_query
from pv_icloud.mail.parse import parse_message
from pv_icloud.ratelimit import WriteLimiter, WriteLimitReached

from .conftest import TOKENS, make_config, sha
from .fakeimap import make_message


@pytest.mark.parametrize(
    "name,wire",
    [
        ("INBOX", "INBOX"),
        ("Friends & Family", "Friends &- Family"),
        ("Bücher", "B&APw-cher"),
        ("日本語", "&ZeVnLIqe-"),
    ],
)
def test_utf7_roundtrip(name, wire):
    assert utf7.encode(name) == wire
    assert utf7.decode(wire) == name


def test_uid_set():
    assert uid_set([5, 1, 2, 3, 9, 10]) == "1:3,5,9:10"
    assert uid_set([7]) == "7"


def test_mail_id_roundtrip_with_pipe_in_folder():
    mid = MailId("A|B Folder", 42, 7)
    assert decode_id(mid.encode()) == mid
    with pytest.raises(BadMailId):
        decode_id("not-an-id!!")


def test_fts_query_quotes_terms():
    assert fts_query('invoice "tax return" OR') == '"invoice" "tax return" "OR"'
    assert fts_query("   ") is None


def test_parse_fetch_size_after_literal():
    data = [(b"1 (UID 9 BODY[]<0> {5}", b"hello"), b" RFC822.SIZE 900)"]
    [m] = list(_parse_fetch(data, 5))
    assert (m.uid, m.size, m.raw, m.truncated) == (9, 900, b"hello", True)


class TestParse:
    def test_quoted_commas_and_encoded_names(self):
        raw = make_message(
            from_='"Doe, Jane" <jane@example.com>',
            to='"Smith, Bob" <bob@example.com>, carol@example.com',
            subject="=?utf-8?q?Caf=C3=A9_plans?=",
        )
        p = parse_message(raw)
        assert p.from_ == "Doe, Jane <jane@example.com>"
        assert p.to == "Smith, Bob <bob@example.com>, carol@example.com"
        assert p.subject == "Café plans"

    def test_html_only_strips_images(self):
        raw = (
            b"From: a@example.com\r\nSubject: s\r\nContent-Type: text/html\r\n\r\n"
            b'<p>Hello <b>world</b></p><img src="https://track.example/pixel.gif">'
        )
        p = parse_message(raw)
        assert "Hello world" in p.body
        assert "track.example" not in p.body

    def test_html_structure_links_and_hidden_text(self):
        from pv_icloud.mail.htmltext import html_to_text

        html = (
            "<html><head><title>T</title><style>p{}</style></head><body>"
            '<div style="display: none">preheader&zwnj;&#847;</div><span hidden>x</span>'
            "<h1>News</h1><p>Hi <b>Jane</b>,<br>order "
            '<a href="https://shop.example/o/1">#123</a> shipped.</p>'
            "<table><tr><td>Item</td><td>Qty</td></tr><tr><td>Mug</td><td>2</td></tr></table>"
            "<ul><li>One<li>Two<ul><li>Two-a</ul></ul><ol><li>a</li><li>b</li></ol>"
            '<p><a href="mailto:help@shop.example">help@shop.example</a> '
            '<a href="javascript:steal()">js</a><a href="https://x.example"><img src="l.png"></a>'
            "</p><blockquote><p>q1</p><p>q2</p></blockquote><pre>  a\n   b</pre>"
            "<script>alert(1)</script><p>Bye &amp; thanks"
        )
        assert html_to_text(html) == "\n".join(
            [
                "News",
                "",
                "Hi Jane,",
                "order #123 (https://shop.example/o/1) shipped.",
                "",
                "Item | Qty",
                "Mug | 2",
                "",
                "- One",
                "- Two",
                "  - Two-a",
                "",
                "1. a",
                "2. b",
                "",
                "help@shop.example js",
                "",
                "> q1",
                ">",
                "> q2",
                "",
                "  a",
                "   b",
                "",
                "Bye & thanks",
            ]
        )

    def test_html_tolerates_malformed_input(self):
        from pv_icloud.mail.htmltext import html_to_text

        assert html_to_text("<p>a <b>b <p>c</div></div><li>d") == "a b\n\nc\n- d"
        assert html_to_text("1 &lt; 2 </p></a><!--[if mso]>x<![endif]-->") == "1 < 2"
        assert html_to_text("") == ""

    def test_prefers_plain_and_lists_attachments(self):
        raw = make_message(
            body="plain body", html="<p>html body</p>", attachment=("report.pdf", b"%PDF-1.4 x")
        )
        p = parse_message(raw)
        assert p.body == "plain body"
        assert p.has_attachments
        [a] = p.attachments
        assert (a.filename, a.content_type, a.size) == ("report.pdf", "application/pdf", 10)

    def test_missing_message_id_and_garbage(self):
        p = parse_message(b"\x00\xff garbage without headers")
        assert p.message_id is None
        p = parse_message(b"Date: not a date\r\nFrom: ,,,\r\n\r\nbody")
        assert p.date is None and p.body == "body"

    def test_threading_headers(self):
        raw = make_message(message_id="<b@x>", in_reply_to="<a@x>", references=["<r@x>", "<a@x>"])
        p = parse_message(raw)
        assert (p.message_id, p.in_reply_to, p.references) == ("<b@x>", "<a@x>", ["<r@x>", "<a@x>"])


class TestConfig:
    def test_loads_and_merges_defaults(self, tmp_path):
        cfg = make_config(tmp_path, alice_exclude=["Banking"])
        alice = cfg.account("alice")
        assert alice.mail.max_connections == 2
        assert not alice.mail.folders.allows("Banking")
        assert not alice.mail.folders.allows("Junk")
        assert alice.mail.folders.allows("Work")
        assert cfg.account("bob").mail.folders.allows("Banking")

    def test_special_folders_hidden_unless_turned_on(self, tmp_path):
        bob = make_config(tmp_path).account("bob")
        assert not any(bob.mail.folders.allows(n) for n in ("Junk", "Deleted Messages", "Drafts"))
        alice = make_config(
            tmp_path, alice_special={"junk": True, "drafts": False}, alice_exclude=["Drafts"]
        ).account("alice")
        assert alice.mail.folders.allows("Junk")
        assert not alice.mail.folders.allows("Deleted Messages")  # key omitted: hidden
        assert not alice.mail.folders.allows("Drafts")

    def test_special_folders_merge_and_include_list_still_applies(self, tmp_path):
        raw = {
            "defaults": {"mail": {"special_folders": {"deleted_messages": True}}},
            "accounts": [
                {
                    "id": "x",
                    "tokens": [{"label": "a", "sha256": "0" * 64}],
                    "mail": {"special_folders": {"junk": True}, "folders": {"include": ["IN*"]}},
                }
            ],
        }
        env = {"PV_ICLOUD_X_ICLOUD_USER": "x", "PV_ICLOUD_X_APP_PASSWORD": "y"}
        folders = parse_config(raw, env, tmp_path).account("x").mail.folders
        assert folders.hidden == frozenset({"Drafts"})
        assert folders.allows("INBOX") and not folders.allows("Junk")  # not in include

    @pytest.mark.parametrize(
        ("special", "message"),
        [
            ({"spam": True}, "unknown key"),
            ({"junk": "yes"}, "true or false"),
            (["junk"], "mapping"),
        ],
    )
    def test_special_folders_validated(self, tmp_path, special, message):
        with pytest.raises(ConfigError, match=message):
            make_config(tmp_path, alice_special=special)

    def test_missing_credentials_fail_loudly(self, tmp_path):
        raw = {"accounts": [{"id": "x", "tokens": [{"label": "a", "sha256": "0" * 64}]}]}
        with pytest.raises(ConfigError, match="PV_ICLOUD_X_ICLOUD_USER"):
            parse_config(raw, {}, tmp_path)

    def test_rejects_placeholder_hash(self, tmp_path):
        raw = {"accounts": [{"id": "x", "tokens": [{"label": "a", "sha256": "REPLACE_ME"}]}]}
        with pytest.raises(ConfigError, match="64 hex"):
            parse_config(raw, {}, tmp_path)

    def test_token_table(self, tmp_path):
        table = TokenTable(make_config(tmp_path))
        assert table.resolve(TOKENS["alice"]).account_id == "alice"
        assert table.resolve(TOKENS["bob"]).account_id == "bob"
        assert table.resolve("nope") is None
        assert sha("x") != sha("y")


WRITE_ON = {"PV_ICLOUD_MAIL_WRITE": "enabled"}


def write_config(
    tmp_path,
    *,
    env=WRITE_ON,
    token=None,
    mail=None,
    defaults=None,
    tokens=None,
    calendar=None,
    calendar_defaults=None,
    contacts=None,
):
    """One account `x` with token `agent` (plus `token` keys) for the write-config tests."""
    raw = {
        "defaults": {"mail": defaults or {}, "calendar": calendar_defaults or {}},
        "accounts": [
            {
                "id": "x",
                "tokens": tokens or [{"label": "agent", "sha256": "0" * 64, **(token or {})}],
                "mail": mail or {},
                "calendar": calendar or {},
                "contacts": contacts or {},
            }
        ],
    }
    creds = {"PV_ICLOUD_X_ICLOUD_USER": "x", "PV_ICLOUD_X_APP_PASSWORD": "y"}
    return parse_config(raw, {**creds, **env}, tmp_path)


class TestMailWriteSwitch:
    @pytest.mark.parametrize("value", [None, "", "disabled"])
    def test_off_values(self, tmp_path, value):
        env = {} if value is None else {"PV_ICLOUD_MAIL_WRITE": value}
        assert write_config(tmp_path, env=env).mail_write is False

    def test_enabled(self, tmp_path):
        assert write_config(tmp_path).mail_write is True

    @pytest.mark.parametrize("value", ["true", "Enabled", "1", "yes"])
    def test_other_values_are_an_error(self, tmp_path, value):
        with pytest.raises(ConfigError, match="PV_ICLOUD_MAIL_WRITE must be `enabled` or"):
            write_config(tmp_path, env={"PV_ICLOUD_MAIL_WRITE": value})

    def test_grant_without_the_switch(self, tmp_path):
        with pytest.raises(ConfigError) as err:
            write_config(tmp_path, env={}, token={"mail_write": ["archive"]})
        for part in ("account x", "'agent'", "mail_write", "PV_ICLOUD_MAIL_WRITE"):
            assert part in str(err.value)

    def test_other_write_keys_are_still_validated_when_off(self, tmp_path):
        with pytest.raises(ConfigError, match="max_writes_per_hour"):
            write_config(tmp_path, env={}, token={"max_writes_per_hour": 0})
        cfg = write_config(tmp_path, env={}, token={"protected_folders": ["Taxes"]})
        assert cfg.account("x").tokens[0].protected_folders == ("Taxes",)


class TestMailWriteConfig:
    def test_token_without_write_keys(self, tmp_path):
        [tok] = write_config(tmp_path).account("x").tokens
        assert tok.mail_write == frozenset()
        assert tok.protected_folders == ()
        assert tok.max_writes_per_hour == 30

    def test_token_write_keys(self, tmp_path):
        token = {
            "mail_write": ["move", "archive", "delete", "archive"],
            "protected_folders": ["Work"],
            "max_writes_per_hour": 10,
        }
        [tok] = write_config(tmp_path, token=token).account("x").tokens
        assert tok.mail_write == frozenset({"move", "archive", "delete"})
        assert tok.protected_folders == ("Work",)
        assert tok.max_writes_per_hour == 10

    @pytest.mark.parametrize(
        ("token", "parts"),
        [
            ({"mail_write": ["move", "expunge"]}, ["mail_write", "expunge", "draft, send"]),
            ({"mail_write": "move"}, ["mail_write", "list"]),
            ({"mail_write": [1]}, ["mail_write"]),
            ({"protected_folders": "Work"}, ["protected_folders", "list"]),
            ({"protected_folders": [1]}, ["protected_folders"]),
            ({"max_writes_per_hour": 0}, ["max_writes_per_hour", "positive integer"]),
            ({"max_writes_per_hour": "30"}, ["max_writes_per_hour"]),
            ({"max_writes_per_hour": True}, ["max_writes_per_hour"]),
        ],
    )
    def test_token_keys_validated(self, tmp_path, token, parts):
        with pytest.raises(ConfigError) as err:
            write_config(tmp_path, token=token)
        for part in ["account x", "'agent'", *parts]:
            assert part in str(err.value)

    def test_draft_grant(self, tmp_path):
        [tok] = write_config(tmp_path, token={"mail_write": ["draft"]}).account("x").tokens
        assert tok.mail_write == frozenset({"draft"})  # nothing else, and never send

    def test_draft_grant_without_the_switch(self, tmp_path):
        with pytest.raises(ConfigError, match="PV_ICLOUD_MAIL_WRITE"):
            write_config(tmp_path, env={}, token={"mail_write": ["draft"]})

    def test_account_write_settings_default(self, tmp_path):
        write = write_config(tmp_path).account("x").mail.write
        assert write.protected_folders == ()
        assert write.archive_folder == "Archive"
        assert write.send_from is None

    def test_send_from(self, tmp_path):
        defaults = {"write": {"send_from": "Team@Example.com"}}
        cfg = write_config(tmp_path, defaults=defaults)
        assert cfg.account("x").mail.write.send_from == "Team@Example.com"
        cfg = write_config(tmp_path, defaults=defaults, mail={"write": {"send_from": "me@x.org"}})
        assert cfg.account("x").mail.write.send_from == "me@x.org"
        # Validated, with no effect, while the switch is off.
        assert write_config(tmp_path, env={}, defaults=defaults).mail_write is False

    def test_protected_folders_add_to_the_defaults(self, tmp_path):
        cfg = write_config(
            tmp_path,
            defaults={"write": {"protected_folders": ["Taxes"]}},
            mail={"write": {"protected_folders": ["Banking"], "archive_folder": "Old Mail"}},
        )
        write = cfg.account("x").mail.write
        assert write.protected_folders == ("Taxes", "Banking")
        assert write.archive_folder == "Old Mail"

    def test_defaults_alone_apply(self, tmp_path):
        cfg = write_config(tmp_path, defaults={"write": {"protected_folders": ["Taxes"]}})
        assert cfg.account("x").mail.write.protected_folders == ("Taxes",)

    @pytest.mark.parametrize(
        ("write", "parts"),
        [
            ({"send_to": ["a@example.com"]}, ["unknown key", "send_to"]),  # a token key
            ({"send_from": "not-an-address"}, ["mail.write.send_from", "bare address"]),
            ({"send_from": "Me <me@example.com>"}, ["mail.write.send_from"]),
            ({"send_from": "a@example.com, b@example.com"}, ["mail.write.send_from"]),
            ({"send_from": "a@b@example.com"}, ["mail.write.send_from"]),
            ({"send_from": "@example.com"}, ["mail.write.send_from"]),
            ({"send_from": "me@localhost"}, ["mail.write.send_from"]),
            ({"send_from": ["me@example.com"]}, ["mail.write.send_from"]),
            ({"send_from": ""}, ["mail.write.send_from"]),
            ({"protected_folders": "Taxes"}, ["protected_folders"]),
            ({"archive_folder": ""}, ["archive_folder"]),
            ({"archive_folder": ["Archive"]}, ["archive_folder"]),
            (["Taxes"], ["mail.write", "mapping"]),
        ],
    )
    def test_account_write_settings_validated(self, tmp_path, write, parts):
        with pytest.raises(ConfigError) as err:
            write_config(tmp_path, mail={"write": write})
        for part in ["account x", *parts]:
            assert part in str(err.value)

    def test_defaults_write_settings_validated(self, tmp_path):
        with pytest.raises(ConfigError, match="unknown key"):
            write_config(tmp_path, defaults={"write": {"protect": ["Taxes"]}})

    def test_tokens_of_one_account_resolve_to_their_own_permissions(self, tmp_path):
        tokens = [
            {
                "label": "laptop",
                "sha256": sha("laptop"),
                "mail_write": ["move", "delete"],
                "protected_folders": ["Work"],
                "max_writes_per_hour": 5,
            },
            {"label": "phone", "sha256": sha("phone")},
        ]
        table = TokenTable(write_config(tmp_path, tokens=tokens))
        laptop, phone = table.resolve("laptop"), table.resolve("phone")
        assert laptop is not None and phone is not None
        assert (laptop.account_id, laptop.token_label) == ("x", "laptop")
        assert laptop.mail_write == frozenset({"move", "delete"})
        assert laptop.protected_folders == ("Work",)
        assert laptop.max_writes_per_hour == 5
        assert (phone.mail_write, phone.protected_folders) == (frozenset(), ())
        assert phone.max_writes_per_hour == 30
        assert laptop.token_key != phone.token_key
        assert sha("laptop") not in repr(laptop)  # the hash never reaches a log line
        assert laptop.send_to is None and phone.send_to is None


SENDER = {"write": {"send_from": "me@example.com"}}


class TestSendConfig:
    """`send`, `send_to` and `mail.write.send_from` (add-mail-send)."""

    def test_send_grant(self, tmp_path):
        cfg = write_config(tmp_path, token={"mail_write": ["send"]}, mail=SENDER)
        [tok] = cfg.account("x").tokens
        assert tok.mail_write == frozenset({"send"})  # and never draft
        assert tok.send_to is None
        assert cfg.account("x").mail.write.send_from == "me@example.com"

    def test_send_and_draft_are_separate(self, tmp_path):
        token = {"mail_write": ["draft"]}
        [tok] = write_config(tmp_path, token=token, mail=SENDER).account("x").tokens
        assert "send" not in tok.mail_write

    def test_send_without_send_from(self, tmp_path):
        with pytest.raises(ConfigError) as err:
            write_config(tmp_path, token={"mail_write": ["send"]})
        for part in ("account x", "'agent'", "send", "mail.write.send_from"):
            assert part in str(err.value)

    def test_send_from_from_the_defaults_is_enough(self, tmp_path):
        cfg = write_config(tmp_path, token={"mail_write": ["send"]}, defaults=SENDER)
        assert cfg.account("x").mail.write.send_from == "me@example.com"

    def test_send_without_the_switch(self, tmp_path):
        with pytest.raises(ConfigError, match="PV_ICLOUD_MAIL_WRITE"):
            write_config(tmp_path, env={}, token={"mail_write": ["send"]}, mail=SENDER)

    def test_send_to_entries_are_lower_cased(self, tmp_path):
        token = {
            "mail_write": ["send"],
            "send_to": ["Bob@Example.COM", "@Example.org", "bob@example.com"],
        }
        [tok] = write_config(tmp_path, token=token, mail=SENDER).account("x").tokens
        assert tok.send_to == ("bob@example.com", "@example.org")

    @pytest.mark.parametrize(
        "send_to",
        [
            [],
            "a@example.com",
            [1],
            [""],
            ["Bob <bob@example.com>"],
            ["bob@example.com, carol@example.com"],
            ["example.org"],
            ["@example"],
            ["@"],
            ["@@example.org"],
            ["a@b@example.org"],
            ["bob @example.com"],
            ["bob@localhost"],
        ],
    )
    def test_malformed_send_to(self, tmp_path, send_to):
        token = {"mail_write": ["send"], "send_to": send_to}
        with pytest.raises(ConfigError) as err:
            write_config(tmp_path, token=token, mail=SENDER)
        for part in ("account x", "'agent'", "send_to"):
            assert part in str(err.value)

    @pytest.mark.parametrize("actions", [[], ["draft"], ["move", "archive"]])
    def test_send_to_needs_send(self, tmp_path, actions):
        token = {"mail_write": actions, "send_to": ["a@example.com"]}
        with pytest.raises(ConfigError) as err:
            write_config(tmp_path, token=token, mail=SENDER)
        for part in ("account x", "'agent'", "send_to", "send"):
            assert part in str(err.value)

    def test_send_to_reaches_the_token_context(self, tmp_path):
        tokens = [
            {
                "label": "mailer",
                "sha256": sha("mailer"),
                "mail_write": ["send"],
                "send_to": ["@example.org"],
            },
            {"label": "open", "sha256": sha("open"), "mail_write": ["send"]},
        ]
        table = TokenTable(write_config(tmp_path, tokens=tokens, mail=SENDER))
        mailer, other = table.resolve("mailer"), table.resolve("open")
        assert mailer is not None and other is not None
        assert mailer.send_to == ("@example.org",)
        assert other.send_to is None

    def test_warning_for_send_without_send_to(self, tmp_path):
        tokens = [
            {"label": "mailer", "sha256": sha("mailer"), "mail_write": ["send"]},
            {
                "label": "fenced",
                "sha256": sha("fenced"),
                "mail_write": ["send"],
                "send_to": ["a@example.com"],
            },
            {"label": "drafter", "sha256": sha("drafter"), "mail_write": ["draft"]},
        ]
        cfg = write_config(tmp_path, tokens=tokens, mail=SENDER)
        warnings = send_warnings(cfg)
        assert warnings == ["token mailer of account x may send mail to any recipient (no send_to)"]
        assert not any("@" in w for w in warnings)

    def test_no_warning_without_send(self, tmp_path):
        assert send_warnings(write_config(tmp_path, token={"mail_write": ["draft"]})) == []


CAL_ON = {"PV_ICLOUD_CALENDAR_WRITE": "enabled"}


class TestCalendarWriteSwitch:
    """`PV_ICLOUD_CALENDAR_WRITE` (add-calendar-write, ADR-0005)."""

    @pytest.mark.parametrize("value", [None, "", "disabled"])
    def test_off_values(self, tmp_path, value):
        env = {} if value is None else {"PV_ICLOUD_CALENDAR_WRITE": value}
        assert write_config(tmp_path, env=env).calendar_write is False

    def test_enabled(self, tmp_path):
        assert write_config(tmp_path, env=CAL_ON).calendar_write is True

    @pytest.mark.parametrize("value", ["true", "Enabled", "1", "yes"])
    def test_other_values_are_an_error(self, tmp_path, value):
        with pytest.raises(ConfigError, match="PV_ICLOUD_CALENDAR_WRITE must be `enabled` or"):
            write_config(tmp_path, env={"PV_ICLOUD_CALENDAR_WRITE": value})

    def test_grant_without_the_switch(self, tmp_path):
        with pytest.raises(ConfigError) as err:
            write_config(tmp_path, env=WRITE_ON, token={"calendar_write": True})
        for part in ("account x", "'agent'", "calendar_write", "PV_ICLOUD_CALENDAR_WRITE"):
            assert part in str(err.value)

    def test_switches_are_independent(self, tmp_path):
        cfg = write_config(tmp_path, env=WRITE_ON, token={"mail_write": ["draft"]})
        assert (cfg.mail_write, cfg.calendar_write) == (True, False)
        cfg = write_config(tmp_path, env=CAL_ON, token={"calendar_write": True})
        assert (cfg.mail_write, cfg.calendar_write) == (False, True)
        with pytest.raises(ConfigError, match="PV_ICLOUD_MAIL_WRITE"):
            write_config(tmp_path, env=CAL_ON, token={"mail_write": ["draft"]})

    def test_false_grant_needs_no_switch(self, tmp_path):
        [tok] = write_config(tmp_path, env={}, token={"calendar_write": False}).account("x").tokens
        assert tok.calendar_write is False


class TestCalendarWriteConfig:
    def test_token_defaults(self, tmp_path):
        [tok] = write_config(tmp_path, env={}).account("x").tokens
        assert (tok.calendar_write, tok.protected_calendars) == (False, ())

    def test_token_keys(self, tmp_path):
        token = {"calendar_write": True, "protected_calendars": ["Work", "Family"]}
        [tok] = write_config(tmp_path, env=CAL_ON, token=token).account("x").tokens
        assert tok.calendar_write is True and tok.mail_write == frozenset()
        assert tok.protected_calendars == ("Work", "Family")

    @pytest.mark.parametrize(
        ("token", "parts"),
        [
            ({"calendar_write": ["create"]}, ["calendar_write", "true or false"]),
            ({"calendar_write": "yes"}, ["calendar_write"]),
            ({"calendar_write": 1}, ["calendar_write"]),
            ({"protected_calendars": "Work"}, ["protected_calendars", "list"]),
            ({"protected_calendars": [1]}, ["protected_calendars"]),
            ({"protected_calendars": [""]}, ["protected_calendars"]),
        ],
    )
    def test_token_keys_validated(self, tmp_path, token, parts):
        with pytest.raises(ConfigError) as err:
            write_config(tmp_path, env=CAL_ON, token=token)
        for part in ["account x", "'agent'", *parts]:
            assert part in str(err.value)

    def test_protected_calendars_add_up_and_keep_their_case(self, tmp_path):
        cfg = write_config(
            tmp_path,
            env={},
            calendar_defaults={"write": {"protected_calendars": ["Family"]}},
            calendar={"write": {"protected_calendars": ["work", "Family"]}},
        )
        acct = cfg.account("x")
        assert acct.calendar.write.protected_calendars == ("Family", "work")
        # The read settings are merged as before.
        assert acct.calendar.calendars.allows("Family")

    def test_defaults_alone_apply(self, tmp_path):
        cfg = write_config(
            tmp_path, env={}, calendar_defaults={"write": {"protected_calendars": ["Family"]}}
        )
        assert cfg.account("x").calendar.write.protected_calendars == ("Family",)
        assert write_config(tmp_path, env={}).account("x").calendar.write.protected_calendars == ()

    @pytest.mark.parametrize(
        ("write", "parts"),
        [
            ({"protected": ["Work"]}, ["calendar.write", "unknown key", "protected"]),
            ({"protected_calendars": "Work"}, ["calendar.write.protected_calendars", "list"]),
            ({"protected_calendars": [3]}, ["calendar.write.protected_calendars"]),
            (["Work"], ["calendar.write", "mapping"]),
        ],
    )
    def test_account_settings_validated(self, tmp_path, write, parts):
        with pytest.raises(ConfigError) as err:
            write_config(tmp_path, env={}, calendar={"write": write})
        for part in ["account x", *parts]:
            assert part in str(err.value)

    def test_defaults_settings_validated(self, tmp_path):
        with pytest.raises(ConfigError, match="defaults.calendar.write: unknown key"):
            write_config(tmp_path, env={}, calendar_defaults={"write": {"protect": ["Work"]}})

    def test_tokens_of_one_account_resolve_to_their_own_calendar_permissions(self, tmp_path):
        tokens = [
            {
                "label": "laptop",
                "sha256": sha("laptop"),
                "calendar_write": True,
                "protected_calendars": ["Work"],
            },
            {"label": "phone", "sha256": sha("phone")},
        ]
        table = TokenTable(write_config(tmp_path, env=CAL_ON, tokens=tokens))
        laptop, phone = table.resolve("laptop"), table.resolve("phone")
        assert laptop is not None and phone is not None
        assert (laptop.calendar_write, laptop.protected_calendars) == (True, ("Work",))
        assert (phone.calendar_write, phone.protected_calendars) == (False, ())
        assert laptop.mail_write == frozenset()


CONTACTS_ON = {"PV_ICLOUD_CONTACTS_WRITE": "enabled"}


class TestContactsWriteSwitch:
    """`PV_ICLOUD_CONTACTS_WRITE` (add-contacts-write, ADR-0006)."""

    @pytest.mark.parametrize("value", [None, "", "disabled"])
    def test_off_values(self, tmp_path, value):
        env = {} if value is None else {"PV_ICLOUD_CONTACTS_WRITE": value}
        assert write_config(tmp_path, env=env).contacts_write is False

    def test_enabled(self, tmp_path):
        assert write_config(tmp_path, env=CONTACTS_ON).contacts_write is True

    @pytest.mark.parametrize("value", ["true", "Enabled", "on", "yes"])
    def test_other_values_are_an_error(self, tmp_path, value):
        with pytest.raises(ConfigError, match="PV_ICLOUD_CONTACTS_WRITE must be `enabled` or"):
            write_config(tmp_path, env={"PV_ICLOUD_CONTACTS_WRITE": value})

    def test_grant_without_the_switch(self, tmp_path):
        with pytest.raises(ConfigError) as err:
            write_config(tmp_path, env=CAL_ON, token={"contacts_write": ["update"]})
        for part in ("account x", "'agent'", "contacts_write", "PV_ICLOUD_CONTACTS_WRITE"):
            assert part in str(err.value)

    def test_grant_with_contacts_turned_off(self, tmp_path):
        with pytest.raises(ConfigError) as err:
            write_config(
                tmp_path,
                env=CONTACTS_ON,
                token={"contacts_write": ["create"]},
                contacts={"enabled": False},
            )
        for part in ("account x", "'agent'", "contacts_write", "contacts.enabled"):
            assert part in str(err.value)
        # Without a grant, contacts may be off with the switch on.
        cfg = write_config(tmp_path, env=CONTACTS_ON, contacts={"enabled": False})
        assert cfg.account("x").contacts.enabled is False

    def test_switches_are_independent(self, tmp_path):
        cfg = write_config(tmp_path, env=CONTACTS_ON, token={"contacts_write": ["delete"]})
        assert (cfg.mail_write, cfg.calendar_write, cfg.contacts_write) == (False, False, True)
        cfg = write_config(tmp_path, env={**WRITE_ON, **CAL_ON}, token={"calendar_write": True})
        assert (cfg.mail_write, cfg.calendar_write, cfg.contacts_write) == (True, True, False)
        with pytest.raises(ConfigError, match="PV_ICLOUD_CALENDAR_WRITE"):
            write_config(tmp_path, env=CONTACTS_ON, token={"calendar_write": True})
        with pytest.raises(ConfigError, match="PV_ICLOUD_MAIL_WRITE"):
            write_config(tmp_path, env=CONTACTS_ON, token={"mail_write": ["draft"]})

    def test_empty_grant_needs_no_switch(self, tmp_path):
        [tok] = write_config(tmp_path, env={}, token={"contacts_write": []}).account("x").tokens
        assert tok.contacts_write == frozenset()


class TestContactsWriteConfig:
    def test_token_defaults(self, tmp_path):
        [tok] = write_config(tmp_path, env={}).account("x").tokens
        assert tok.contacts_write == frozenset()
        assert tok.max_writes_per_hour == 30

    @pytest.mark.parametrize(
        "actions",
        [["create"], ["update", "delete"], ["create", "update", "delete"], ["update", "update"]],
    )
    def test_token_actions(self, tmp_path, actions):
        token = {"contacts_write": actions}
        [tok] = write_config(tmp_path, env=CONTACTS_ON, token=token).account("x").tokens
        assert tok.contacts_write == frozenset(actions)
        assert tok.mail_write == frozenset() and tok.calendar_write is False

    @pytest.mark.parametrize(
        ("token", "parts"),
        [
            ({"contacts_write": True}, ["contacts_write", "list"]),
            ({"contacts_write": "update"}, ["contacts_write", "list"]),
            ({"contacts_write": [1]}, ["contacts_write", "list"]),
            (
                {"contacts_write": ["update", "merge"]},
                ["contacts_write", "merge", "create, update, delete"],
            ),
        ],
    )
    def test_token_key_validated(self, tmp_path, token, parts):
        with pytest.raises(ConfigError) as err:
            write_config(tmp_path, env=CONTACTS_ON, token=token)
        for part in ["account x", "'agent'", *parts]:
            assert part in str(err.value)

    def test_tokens_of_one_account_resolve_to_their_own_contact_permissions(self, tmp_path):
        tokens = [
            {"label": "laptop", "sha256": sha("laptop"), "contacts_write": ["update", "delete"]},
            {"label": "agent", "sha256": sha("agent"), "contacts_write": ["update"]},
            {"label": "phone", "sha256": sha("phone")},
        ]
        table = TokenTable(write_config(tmp_path, env=CONTACTS_ON, tokens=tokens))
        laptop, agent, phone = (table.resolve(x) for x in ("laptop", "agent", "phone"))
        assert laptop is not None and agent is not None and phone is not None
        assert laptop.contacts_write == frozenset({"update", "delete"})
        assert agent.contacts_write == frozenset({"update"})
        assert phone.contacts_write == frozenset()
        assert laptop.calendar_write is False and laptop.mail_write == frozenset()


class TestWriteLimiter:
    @pytest.fixture
    def clock(self):
        class Clock:
            now = 1000.0

            def __call__(self):
                return self.now

        return Clock()

    def test_sliding_window(self, clock):
        limiter = WriteLimiter(clock=clock)
        limiter.check("a", 2)
        clock.now += 600
        limiter.check("a", 2)
        clock.now += 60
        with pytest.raises(WriteLimitReached) as err:
            limiter.check("a", 2)
        # The first call leaves the window 3600 s after it was made: 2940 s from now.
        assert str(err.value) == (
            "write limit reached for this token (2 per hour); try again in about 49 minutes"
        )
        clock.now = 1000.0 + 3600
        limiter.check("a", 2)  # the first call has left the window
        with pytest.raises(WriteLimitReached):
            limiter.check("a", 2)

    def test_minutes_are_at_least_one(self, clock):
        limiter = WriteLimiter(clock=clock)
        limiter.check("a", 1)
        clock.now += 3600 - 5
        with pytest.raises(WriteLimitReached, match="about 1 minute$"):
            limiter.check("a", 1)

    def test_refused_calls_do_not_extend_the_wait(self, clock):
        limiter = WriteLimiter(clock=clock)
        limiter.check("a", 1)
        for _ in range(5):
            clock.now += 600
            with pytest.raises(WriteLimitReached):
                limiter.check("a", 1)
        clock.now = 1000.0 + 3600
        limiter.check("a", 1)

    def test_separate_budgets_per_token(self, clock):
        limiter = WriteLimiter(clock=clock)
        limiter.check("agent", 1)
        with pytest.raises(WriteLimitReached):
            limiter.check("agent", 1)
        limiter.check("laptop", 1)

    def test_a_new_limiter_starts_empty(self, clock):
        # Counts live only in memory: a restart (a new limiter) forgets them.
        limiter = WriteLimiter(clock=clock)
        limiter.check("a", 1)
        WriteLimiter(clock=clock).check("a", 1)


def test_imap_date_format():
    from pv_icloud.mail.sync import imap_date

    assert imap_date(dt.date(2026, 3, 5)) == "05-Mar-2026"


def test_example_config_is_valid(tmp_path):
    """The committed template must stay loadable once its placeholder hash is filled in."""
    from pathlib import Path

    import yaml

    text = (Path(__file__).parent.parent / "config/pv-icloud.example.yaml").read_text()
    assert "REPLACE_ME" in text
    raw = yaml.safe_load(text.replace("REPLACE_ME", sha("example-token")))
    env = {}
    for acct in raw["accounts"]:
        env[f"PV_ICLOUD_{acct['id'].upper()}_ICLOUD_USER"] = "user@icloud.com"
        env[f"PV_ICLOUD_{acct['id'].upper()}_APP_PASSWORD"] = "pw"
    cfg = parse_config(raw, env, tmp_path)
    assert cfg.accounts
    # As shipped, write support is off and no token has a grant; with the switch on it loads
    # the same way.
    assert cfg.mail_write is False
    assert all(not t.mail_write for a in cfg.accounts for t in a.tokens)
    on = parse_config(raw, {**env, "PV_ICLOUD_MAIL_WRITE": "enabled"}, tmp_path)
    assert on.accounts[0].mail.write.archive_folder == "Archive"
    assert cfg.calendar_write is False
    assert all(not t.calendar_write for a in cfg.accounts for t in a.tokens)
    on = parse_config(raw, {**env, "PV_ICLOUD_CALENDAR_WRITE": "enabled"}, tmp_path)
    assert on.calendar_write and on.accounts[0].calendar.write.protected_calendars == ()
    assert cfg.contacts_write is False
    assert all(not t.contacts_write for a in cfg.accounts for t in a.tokens)
    on = parse_config(raw, {**env, "PV_ICLOUD_CONTACTS_WRITE": "enabled"}, tmp_path)
    assert on.contacts_write is True
    # The commented-out grant in the example is one a token could use as written.
    assert "contacts_write: [create, update, delete]" in text


def test_per_account_limits_in_descriptions():
    from pv_icloud.server import _days

    assert _days([90, 90]) == "90 days"
    assert _days([30, 90, 60]) == "30 to 90 days, depending on the account"


class TestBindHost:
    def test_defaults_to_loopback_without_warning(self, monkeypatch, caplog):
        from pv_icloud.__main__ import bind_host

        monkeypatch.delenv("PV_ICLOUD_HOST", raising=False)
        assert bind_host() == "127.0.0.1"
        monkeypatch.setenv("PV_ICLOUD_HOST", "::1")
        assert bind_host() == "::1"
        assert not caplog.records

    def test_warns_when_reachable_over_plain_http(self, monkeypatch, caplog):
        from pv_icloud.__main__ import bind_host

        monkeypatch.setenv("PV_ICLOUD_HOST", "0.0.0.0")
        assert bind_host() == "0.0.0.0"
        assert "plain HTTP" in caplog.text
