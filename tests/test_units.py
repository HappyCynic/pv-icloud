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
