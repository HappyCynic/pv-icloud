# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import datetime as dt

import pytest

from pv_icloud import utf7
from pv_icloud.auth import TokenTable
from pv_icloud.config import ConfigError, parse_config
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


def write_config(tmp_path, *, env=WRITE_ON, token=None, mail=None, defaults=None, tokens=None):
    """One account `x` with token `agent` (plus `token` keys) for the write-config tests."""
    raw = {
        "defaults": {"mail": defaults or {}},
        "accounts": [
            {
                "id": "x",
                "tokens": tokens or [{"label": "agent", "sha256": "0" * 64, **(token or {})}],
                "mail": mail or {},
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
            ({"mail_write": ["move", "expunge"]}, ["mail_write", "expunge", "move, archive"]),
            # send arrives with add-mail-send; until then it is unknown, listing the allowed.
            ({"mail_write": ["draft", "send"]}, ["mail_write", "send", "delete, draft"]),
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
            ({"send_to": ["a@example.com"]}, ["unknown key", "send_to"]),  # add-mail-send
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
