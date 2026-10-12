# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

"""Sync + tool behaviour against the fake IMAP server."""

from __future__ import annotations

import datetime as dt
import statistics
import time

import pytest

from pv_icloud.mail.ids import MailId, decode_id
from pv_icloud.mail.parse import parse_message
from pv_icloud.mail.service import ToolInputError

from .conftest import Harness, make_config
from .fakeimap import FakeServer, make_message

NOW = dt.datetime.now(dt.UTC)

# The read-only-and-egress spec's "Read-only IMAP commands" requirement.
ALLOWED_IMAP_COMMANDS = frozenset(
    {"CAPABILITY", "LOGIN", "ENABLE", "LIST", "STATUS", "EXAMINE", "UID", "NOOP", "IDLE", "LOGOUT"}
)


def deliver(server, folder, days_ago=1, **kw):
    when = NOW - dt.timedelta(days=days_ago)
    return server.deliver(folder, make_message(date=when, **kw), when.date())


def ids_of(result):
    return [(r["folder"], r["subject"]) for r in result["results"]]


class TestSync:
    def test_backfill_indexes_in_scope_folders_only(self, server, harness):
        deliver(server, "INBOX", subject="inbox mail")
        deliver(server, "Friends & Family", subject="family mail")
        deliver(server, "Junk", subject="junk mail")
        deliver(server, "Drafts", subject="draft mail")
        harness.run_sync()
        names = {f.name for f in harness.index.folders()}
        assert names == {"INBOX", "Work", "Friends & Family"}
        assert harness.index.count() == 2

    def test_special_folders_can_be_turned_on(self, server, tmp_path):
        account = make_config(tmp_path, alice_special={"junk": True, "drafts": True}).account(
            "alice"
        )
        h = Harness(server, account, tmp_path)
        deliver(server, "Junk", subject="junk mail")
        deliver(server, "Drafts", subject="draft mail")
        deliver(server, "Deleted Messages", subject="deleted mail")
        h.run_sync()
        assert {f["name"] for f in h.service.folders()["folders"]} == {
            "INBOX", "Work", "Friends & Family", "Junk", "Drafts",
        }  # fmt: skip
        hits = h.service.search(query="mail")["results"]
        assert {(r["folder"], r["subject"]) for r in hits} == {
            ("Junk", "junk mail"),
            ("Drafts", "draft mail"),
        }
        # Turned off again (config change + restart): purged from the index on the next sync.
        h = Harness(server, make_config(tmp_path).account("alice"), tmp_path)
        h.run_sync()
        assert {f.name for f in h.index.folders()} == {"INBOX", "Work", "Friends & Family"}
        assert h.index.count() == 0

    def test_window_excludes_old_mail(self, server, harness):
        deliver(server, "INBOX", days_ago=800, subject="ancient")
        deliver(server, "INBOX", days_ago=3, subject="recent")
        harness.run_sync()
        assert [m.subject for m in harness.index.search(folders=["INBOX"])] == ["recent"]

    def test_new_mail_deletions_and_moves_reflected(self, server, harness):
        a = deliver(server, "INBOX", subject="alpha")
        deliver(server, "INBOX", subject="beta")
        harness.run_sync()
        server.expunge("INBOX", a)
        b_uid = max(server.folders["INBOX"].messages)
        server.move("INBOX", b_uid, "Work")
        deliver(server, "INBOX", subject="gamma")
        harness.run_sync()
        got = {(m.folder, m.subject) for m in harness.index.search(folders=["INBOX", "Work"])}
        assert got == {("Work", "beta"), ("INBOX", "gamma")}

    def test_unchanged_folders_skip_search(self, server, harness):
        deliver(server, "INBOX", subject="x")
        harness.run_sync()
        server.commands.clear()
        harness.run_sync()
        assert not [c for c in server.commands if c[:2] in (("UID", "SEARCH"), ("UID", "FETCH"))]

    def test_message_without_date_header_is_searchable(self, server, harness):
        raw = b"From: cal@example.com\r\nSubject: Event accepted: standup\r\n\r\nok"
        server.deliver("INBOX", raw, (NOW - dt.timedelta(days=3)).date())
        harness.run_sync()
        [hit] = harness.service.search(query="standup")["results"]
        assert hit["date"].startswith((NOW - dt.timedelta(days=3)).date().isoformat())

    def test_v1_index_drops_dateless_rows_for_refetch(self, server, harness):
        server.deliver("INBOX", b"Subject: no date\r\n\r\nx", NOW.date())
        harness.run_sync()
        db = harness.index._db()
        db.execute("UPDATE messages SET date = NULL")
        db.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
        db.commit()
        from pv_icloud.mail.index import MailIndex

        reopened = MailIndex(harness.index.path)
        assert reopened.count() == 0

    def test_uidvalidity_change_reindexes(self, server, harness):
        deliver(server, "Work", subject="w1")
        harness.run_sync()
        old = harness.index.search(folders=["Work"])[0]
        server.set_uidvalidity("Work", 99)
        harness.run_sync()
        [new] = harness.index.search(folders=["Work"])
        assert new.uidvalidity == 99 and old.uidvalidity == 1

    def test_malformed_message_does_not_abort(self, server, harness, monkeypatch):
        deliver(server, "INBOX", subject="good one")
        bad = server.deliver("INBOX", b"\x00garbage", NOW.date())
        deliver(server, "INBOX", subject="good two")
        import pv_icloud.mail.sync as sync_mod

        real = sync_mod.parse_message

        def flaky(raw, **kw):
            if raw.startswith(b"\x00"):
                raise ValueError("boom")
            return real(raw, **kw)

        monkeypatch.setattr(sync_mod, "parse_message", flaky)
        harness.run_sync()
        subjects = {m.subject for m in harness.index.search(folders=["INBOX"])}
        assert {"good one", "good two"} <= subjects
        assert harness.sync.status.skipped_malformed == 1
        assert harness.index.get("INBOX", bad) is not None  # stub row: not retried forever
        assert harness.service.search(query="good")["results"]

    def test_failing_folder_does_not_abort_pass(self, server, harness):
        deliver(server, "INBOX", subject="fine")
        deliver(server, "Work", subject="unreachable")
        server.fail_examine.add("Work")
        harness.run_sync()
        assert [m.subject for m in harness.index.search(folders=["INBOX", "Work"])] == ["fine"]
        assert "Work" in harness.sync.status.folder_errors

    def test_excluding_folder_purges_index(self, server, tmp_path):
        deliver(server, "Work", subject="secret")
        h = Harness(server, make_config(tmp_path).account("alice"), tmp_path)
        h.run_sync()
        assert h.index.count() == 1
        h2 = Harness(
            server, make_config(tmp_path, alice_exclude=["Work"]).account("alice"), tmp_path
        )
        h2.run_sync()
        assert h2.index.count() == 0
        with pytest.raises(ToolInputError, match="unknown folder"):
            h2.service.search(folders=["Work"])

    def test_idle_triggers_inbox_sync(self, server, harness):
        harness.run_sync()
        client = harness._client
        deliver(server, "INBOX", subject="pushed")
        client.conn.idle_events = [("EXISTS", [b"1"])]
        harness.sync.interval = 0.05
        harness.sync._wait_for_next_pass(client)
        assert harness.sync.inbox_changed.is_set()
        assert harness.index.search(folders=["INBOX"])[0].subject == "pushed"


class TestTools:
    @pytest.fixture
    def populated(self, server, harness):
        deliver(
            server,
            "INBOX",
            days_ago=2,
            subject="Quarterly report",
            from_='"Doe, Jane" <jane@example.com>',
            body="Numbers for Q3 attached",
            attachment=("q3.pdf", b"%PDF"),
            message_id="<root@x>",
        )
        deliver(
            server,
            "Work",
            days_ago=1,
            subject="Re: Quarterly report",
            from_="Bob <bob@example.com>",
            body="Looks good",
            message_id="<r1@x>",
            in_reply_to="<root@x>",
            references=["<root@x>"],
        )
        deliver(
            server,
            "Friends & Family",
            days_ago=1,
            subject="Dinner",
            body="Café on Friday?",
            message_id="<d@x>",
        )
        deliver(server, "Junk", subject="Quarterly report winner", body="spam")
        harness.run_sync()
        return harness

    def test_folders(self, populated):
        out = populated.service.folders()
        assert [f["name"] for f in out["folders"]] == ["INBOX", "Friends & Family", "Work"]
        assert out["folders"][0]["messages"] == 1

    def test_cross_folder_search(self, populated):
        out = populated.service.search(query="quarterly report")
        assert out["source"] == "index"
        assert ids_of(out) == [("Work", "Re: Quarterly report"), ("INBOX", "Quarterly report")]
        assert ids_of(populated.service.search(from_="Doe, Jane")) == [
            ("INBOX", "Quarterly report")
        ]
        assert ids_of(populated.service.search(query="cafe")) == [("Friends & Family", "Dinner")]
        r = populated.service.search(subject="quarterly", limit=1)["results"]
        assert len(r) == 1 and r[0]["snippet"] == "Looks good"

    def test_date_filters(self, populated):
        # A full datetime: a date-only cutoff would depend on the time of day the test runs.
        since = (NOW - dt.timedelta(days=1, hours=12)).isoformat()
        assert {r["subject"] for r in populated.service.search(since=since)["results"]} == {
            "Re: Quarterly report",
            "Dinner",
        }
        with pytest.raises(ToolInputError):
            populated.service.search(since="last tuesday")

    def test_single_folder_search_uses_the_index(self, populated, server):
        server.commands.clear()
        out = populated.service.search(query="numbers", folders=["INBOX"])
        assert out["source"] == "index"
        assert ids_of(out) == [("INBOX", "Quarterly report")]
        assert out["results"][0]["has_attachments"]
        assert not any("SEARCH" in str(c) for c in server.commands)

    def test_filters_ignore_case_and_accents(self, populated, server):
        deliver(
            server,
            "Work",
            subject="Café Müller — PLANS",
            from_="Jürgen Müller <jm@example.de>",
            to="Zoë <zoe@example.com>",
            body="Zürich trip",
        )
        populated.run_sync()
        expected = [("Work", "Café Müller — PLANS")]
        for kw in (
            {"subject": "cafe muller"},
            {"subject": "CAFÉ MÜLLER"},
            {"subject": "plans", "folders": ["Work"]},
            {"from_": "jurgen"},
            {"from_": "MÜLLER"},
            {"to": "zoe"},
            {"query": "zurich"},
            {"query": "Müller", "folders": ["Work"]},
        ):
            assert ids_of(populated.service.search(**kw)) == expected, kw
        assert ids_of(populated.service.search(subject="cafe mueller")) == []
        # LIKE wildcards in a filter are literal characters.
        assert ids_of(populated.service.search(subject="caf_")) == []

    def test_get(self, populated):
        hit = populated.service.search(query="numbers")["results"][0]
        msg = populated.service.get(hit["id"])
        assert msg["headers"]["from"] == "Doe, Jane <jane@example.com>"
        assert msg["body"] == "Numbers for Q3 attached"
        assert msg["attachments"] == [
            {"filename": "q3.pdf", "content_type": "application/pdf", "size": 4}
        ]
        assert msg["web_url"] == "https://www.icloud.com/mail/"
        page = populated.service.get(hit["id"], max_chars=7, offset=8)
        assert page["body"] == "for Q3 " and page["next_offset"] == 15

    def test_get_rejects_stale_excluded_and_bogus_ids(self, populated, server):
        hit = populated.service.search(query="numbers")["results"][0]
        server.set_uidvalidity("INBOX", 77)
        with pytest.raises(ToolInputError, match="stale"):
            populated.service.get(hit["id"])
        junk_uid = next(iter(server.folders["Junk"].messages))
        with pytest.raises(ToolInputError, match="unknown folder"):
            populated.service.get(MailId("Junk", 1, junk_uid).encode())
        with pytest.raises(ToolInputError):
            populated.service.get("%%%")

    def test_thread_spans_folders(self, populated):
        hit = populated.service.search(subject="Re: Quarterly")["results"][0]
        out = populated.service.thread(hit["id"])
        assert [(m["folder"], m["subject"]) for m in out["messages"]] == [
            ("INBOX", "Quarterly report"),
            ("Work", "Re: Quarterly report"),
        ]

    def test_thread_paging(self, populated):
        hit = populated.service.search(subject="Re: Quarterly")["results"][0]
        first = populated.service.thread(hit["id"], limit=1)
        assert first["total"] == 2 and first["next_offset"] == 1
        second = populated.service.thread(hit["id"], limit=1, offset=1)
        assert second["next_offset"] is None
        assert [m["subject"] for m in first["messages"] + second["messages"]] == [
            "Quarterly report",
            "Re: Quarterly report",
        ]
        with pytest.raises(ToolInputError, match="limit"):
            populated.service.thread(hit["id"], limit=101)

    def test_search_paging(self, populated, server):
        for n in range(5):
            deliver(server, "INBOX", days_ago=1, subject=f"Page test {n}", body="pageword")
        populated.run_sync()
        for folders in (None, ["INBOX"]):  # all folders, then one
            got: list[str] = []
            offset = 0
            while offset is not None:
                out = populated.service.search(
                    query="pageword", folders=folders, limit=2, offset=offset
                )
                assert out["offset"] == offset and len(out["results"]) <= 2
                got += [r["subject"] for r in out["results"]]
                offset = out["next_offset"]
            assert sorted(got) == [f"Page test {n}" for n in range(5)], folders
        with pytest.raises(ToolInputError, match="offset"):
            populated.service.search(query="x", offset=-1)

    def test_thread_for_unsynced_message(self, populated, server):
        uid = deliver(
            server,
            "INBOX",
            subject="Re: Re: Quarterly report",
            message_id="<r2@x>",
            references=["<root@x>", "<r1@x>"],
        )
        mid = MailId("INBOX", 1, uid).encode()
        out = populated.service.thread(mid)
        assert len(out["messages"]) == 3

    def test_reads_never_change_flags(self, populated, server):
        for m in server.folders["INBOX"].messages.values():
            m.flags.discard("\\Seen")
        before = server.all_flags()
        ids = [r["id"] for r in populated.service.search(limit=50)["results"]]
        for i in range(100):
            mid = ids[i % len(ids)]
            populated.service.get(mid)
            populated.service.thread(mid)
            populated.service.search(query="report", folders=["INBOX"])
        assert server.all_flags() == before
        assert ("CAPABILITY",) in server.commands  # the log covers connect, too
        assert {c[0] for c in server.commands} <= ALLOWED_IMAP_COMMANDS
        assert {c[1] for c in server.commands if c[0] == "UID"} <= {"SEARCH", "FETCH"}


class TestWrites:
    """mail_move, mail_archive and mail_delete at the service layer (ADR-0004)."""

    WRITE_COMMANDS = ("SELECT", "UID COPY", "UID STORE", "UID EXPUNGE")

    @staticmethod
    def sent(server) -> list[str]:
        return [" ".join(c[:2]) if c[0] == "UID" else c[0] for c in server.commands]

    def no_write_sent(self, server) -> bool:
        return not set(self.sent(server)) & set(self.WRITE_COMMANDS)

    @pytest.fixture
    def make(self, server, tmp_path):
        server.folder("Archive")
        server.folder("Taxes")
        server.folder("Banking")

        def make(**config) -> Harness:
            account = make_config(tmp_path, **config).account("alice")
            h = Harness(server, account, tmp_path, write=True)
            h.run_sync()
            return h

        return make

    @pytest.fixture
    def h(self, make):
        return make(
            default_write={"protected_folders": ["Taxes"]},
            alice_write={"protected_folders": ["Banking"]},
        )

    def test_move_files_an_unread_message(self, server, h):
        uid = deliver(server, "INBOX", subject="file me")
        h.run_sync()
        mid = MailId("INBOX", 1, uid).encode()
        out = h.service.move(mid, "Work")
        assert (out["moved"], out["from_folder"], out["to_folder"]) == (True, "INBOX", "Work")
        assert not server.folders["INBOX"].messages
        [copy] = server.folders["Work"].messages.values()
        assert copy.flags == set()  # still unread
        assert h.service.get(out["new_id"])["headers"]["subject"] == "file me"
        # The old id is gone from the index at once; sync picks up the new one.
        assert h.index.get("INBOX", uid) is None

    def test_writes_ask_sync_to_resync_both_folders(self, server, h, monkeypatch):
        asked = []
        monkeypatch.setattr(h.sync, "request_resync", lambda *names: asked.append(names))
        uid = deliver(server, "INBOX")
        h.service.archive(MailId("INBOX", 1, uid).encode())
        assert asked == [("INBOX", "Archive")]

    @pytest.mark.parametrize(
        ("source", "token_protected"),
        [
            ("Taxes", ()),  # defaults
            ("Banking", ()),  # account
            ("Work", ("Work",)),  # token
            ("INBOX", ("inbox",)),  # INBOX in any case
        ],
    )
    def test_protected_source(self, server, make, source, token_protected):
        h = make(
            default_write={"protected_folders": ["Taxes"]},
            alice_write={"protected_folders": ["Banking"]},
            alice_special={"deleted_messages": True},
        )
        uid = deliver(server, source)
        mid = MailId(source, 1, uid).encode()
        server.commands.clear()
        for call in (
            lambda: h.service.move(mid, "Friends & Family", token_protected),
            lambda: h.service.archive(mid, token_protected),
            lambda: h.service.delete(mid, token_protected),
        ):
            with pytest.raises(ToolInputError, match="folder is protected"):
                call()
        assert self.no_write_sent(server)
        assert uid in server.folders[source].messages

    @pytest.mark.parametrize(
        ("call", "token_protected"),
        [
            (lambda s, mid, tp: s.move(mid, "Taxes", tp), ()),
            (lambda s, mid, tp: s.move(mid, "Work", tp), ("Work",)),
            (lambda s, mid, tp: s.archive(mid, tp), ("Archive",)),
            (lambda s, mid, tp: s.delete(mid, tp), ("Deleted Messages",)),
        ],
    )
    def test_protected_destination(self, server, h, call, token_protected):
        uid = deliver(server, "INBOX")
        server.commands.clear()
        with pytest.raises(ToolInputError, match="folder is protected"):
            call(h.service, MailId("INBOX", 1, uid).encode(), token_protected)
        assert self.no_write_sent(server)
        assert uid in server.folders["INBOX"].messages

    def test_token_protection_applies_only_to_that_token(self, server, h):
        uid = deliver(server, "Work")
        mid = MailId("Work", 1, uid).encode()
        with pytest.raises(ToolInputError, match="protected"):
            h.service.move(mid, "INBOX", ("Work",))
        assert h.service.move(mid, "INBOX")["to_folder"] == "INBOX"

    @pytest.mark.parametrize("folder", ["Private", "Nope"])
    def test_destination_out_of_scope_or_missing(self, server, make, folder):
        server.folder("Private")
        h = make(alice_exclude=["Private"])
        uid = deliver(server, "INBOX")
        server.commands.clear()
        with pytest.raises(ToolInputError, match=f"^unknown folder: {folder}$"):
            h.service.move(MailId("INBOX", 1, uid).encode(), folder)
        assert self.no_write_sent(server)

    def test_hidden_special_folder_is_not_a_move_destination(self, server, h):
        uid = deliver(server, "INBOX")
        with pytest.raises(ToolInputError, match="unknown folder: Deleted Messages"):
            h.service.move(MailId("INBOX", 1, uid).encode(), "Deleted Messages")

    def test_move_to_the_same_folder(self, server, h):
        uid = deliver(server, "INBOX")
        server.commands.clear()
        with pytest.raises(ToolInputError, match="already in that folder"):
            h.service.move(MailId("INBOX", 1, uid).encode(), "INBOX")
        assert self.no_write_sent(server)

    def test_out_of_scope_source_is_unknown(self, server, h):
        uid = deliver(server, "Junk")  # hidden by default
        with pytest.raises(ToolInputError, match="unknown folder: Junk"):
            h.service.delete(MailId("Junk", 1, uid).encode())

    def test_delete_moves_to_hidden_deleted_messages(self, server, h):
        uid = deliver(server, "INBOX", subject="bye")
        server.folders["INBOX"].messages[uid].flags = {"\\Seen"}
        out = h.service.delete(MailId("INBOX", 1, uid).encode())
        assert out["to_folder"] == "Deleted Messages"
        assert not server.folders["INBOX"].messages
        [kept] = server.folders["Deleted Messages"].messages.values()
        assert kept.flags == {"\\Seen"}  # flags unchanged; not marked deleted
        assert decode_id(out["new_id"]) == MailId("Deleted Messages", 1, kept.uid)

    def test_delete_from_deleted_messages(self, server, make):
        h = make(alice_special={"deleted_messages": True})
        uid = deliver(server, "Deleted Messages")
        server.commands.clear()
        with pytest.raises(ToolInputError, match="already in Deleted Messages"):
            h.service.delete(MailId("Deleted Messages", 1, uid).encode())
        assert "SELECT" not in self.sent(server)
        assert uid in server.folders["Deleted Messages"].messages

    def test_delete_from_a_differently_named_trash(self, server, make):
        # iCloud marks Deleted Messages \Trash; a \Trash folder under another name counts too.
        server.folder("Bin").special_use = "\\Trash"
        server.folders["Deleted Messages"].special_use = None
        h = make()
        uid = deliver(server, "Bin")
        with pytest.raises(ToolInputError, match="already in Deleted Messages"):
            h.service.delete(MailId("Bin", 1, uid).encode())
        inbox = deliver(server, "INBOX")
        assert h.service.delete(MailId("INBOX", 1, inbox).encode())["to_folder"] == "Bin"

    def test_delete_without_deleted_messages(self, server, h):
        del server.folders["Deleted Messages"]
        uid = deliver(server, "INBOX")
        with pytest.raises(ToolInputError, match="Deleted Messages folder not found"):
            h.service.delete(MailId("INBOX", 1, uid).encode())
        assert uid in server.folders["INBOX"].messages

    def test_archive_to_hidden_archive_folder(self, server, make):
        h = make(alice_exclude=["Archive"])
        uid = deliver(server, "INBOX")
        server.folders["INBOX"].messages[uid].flags = {"\\Flagged"}
        out = h.service.archive(MailId("INBOX", 1, uid).encode())
        assert out["to_folder"] == "Archive"
        [kept] = server.folders["Archive"].messages.values()
        assert kept.flags == {"\\Flagged"}

    def test_archive_already_archived(self, server, h):
        uid = deliver(server, "Archive")
        server.commands.clear()
        with pytest.raises(ToolInputError, match="already in the archive folder"):
            h.service.archive(MailId("Archive", 1, uid).encode())
        assert self.no_write_sent(server)

    def test_archive_folder_missing(self, server, h):
        del server.folders["Archive"]
        uid = deliver(server, "INBOX")
        with pytest.raises(ToolInputError, match="archive folder not found"):
            h.service.archive(MailId("INBOX", 1, uid).encode())
        assert uid in server.folders["INBOX"].messages

    def test_archive_folder_override(self, server, make):
        server.folder("Old Mail")
        h = make(alice_write={"archive_folder": "Old Mail"})
        uid = deliver(server, "INBOX")
        assert h.service.archive(MailId("INBOX", 1, uid).encode())["to_folder"] == "Old Mail"
        assert len(server.folders["Old Mail"].messages) == 1
        assert not server.folders["Archive"].messages

    def test_stale_and_bogus_ids(self, server, h):
        uid = deliver(server, "INBOX")
        with pytest.raises(ToolInputError, match="stale"):
            h.service.move(MailId("INBOX", 7, uid).encode(), "Work")
        with pytest.raises(ToolInputError, match="not found"):
            h.service.move(MailId("INBOX", 1, 999).encode(), "Work")
        with pytest.raises(ToolInputError, match="malformed"):
            h.service.move("%%%", "Work")
        assert uid in server.folders["INBOX"].messages

    def test_reads_after_a_write_use_a_read_only_connection(self, server, h):
        uid = deliver(server, "INBOX")
        out = h.service.move(MailId("INBOX", 1, uid).encode(), "Work")
        server.commands.clear()
        h.service.get(out["new_id"])
        assert {c[0] for c in server.commands} <= ALLOWED_IMAP_COMMANDS
        assert "SELECT" not in self.sent(server)

    def test_without_a_writer_nothing_is_written(self, server, harness):
        uid = deliver(server, "INBOX")
        harness.run_sync()
        with pytest.raises(ToolInputError, match="not enabled"):
            harness.service.move(MailId("INBOX", 1, uid).encode(), "Work")
        assert self.no_write_sent(server)


class TestDrafts:
    """mail_draft at the service layer (add-mail-draft)."""

    @pytest.fixture(autouse=True)
    def no_smtp(self, monkeypatch):
        import smtplib

        def refuse(*args, **kwargs):
            raise AssertionError("mail_draft must never open an SMTP connection")

        monkeypatch.setattr(smtplib, "SMTP", refuse)
        monkeypatch.setattr(smtplib, "SMTP_SSL", refuse)

    @pytest.fixture
    def make(self, server, tmp_path):
        def make(**config) -> Harness:
            account = make_config(tmp_path, **config).account("alice")
            h = Harness(server, account, tmp_path, write=True)
            h.run_sync()
            server.commands.clear()
            return h

        return make

    @pytest.fixture
    def h(self, make):
        return make()

    @staticmethod
    def drafts(server):
        return list(server.folders["Drafts"].messages.values())

    def test_saves_one_draft(self, server, h):
        before = server.all_flags()
        out = h.service.draft(to=["Bob <bob@example.com>"], subject="Lunch", body="Noon?")
        [draft] = self.drafts(server)
        assert draft.flags == {"\\Draft", "\\Seen"}
        p = parse_message(draft.raw)
        assert (p.to, p.subject, p.body.strip(), p.from_) == (
            "Bob <bob@example.com>", "Lunch", "Noon?", ""
        )  # fmt: skip
        assert out == {
            "saved": True,
            "folder": "Drafts",
            "id": MailId("Drafts", 1, draft.uid).encode(),
            "in_reply_to": None,
        }
        assert {k: v for k, v in server.all_flags().items() if k[0] != "Drafts"} == before

    def test_from_is_the_accounts_send_from(self, server, make):
        h = make(default_write={"send_from": "me@example.com"})
        h.service.draft(subject="x")
        [draft] = self.drafts(server)
        assert parse_message(draft.raw).from_ == "me@example.com"

    def test_protected_drafts_does_not_block(self, server, make):
        # Protected folders fence moves only; the draft grant is the control (PR #19).
        h = make(
            default_write={"protected_folders": ["Drafts"]},
            alice_write={"protected_folders": ["Drafts", "INBOX"]},
        )
        uid = deliver(server, "INBOX", message_id="<p@x>")
        out = h.service.draft(subject="x", in_reply_to=MailId("INBOX", 1, uid).encode())
        assert out["saved"] is True and len(self.drafts(server)) == 1

    @pytest.mark.parametrize(
        "kw",
        [
            {},
            {"to": ["not an address"]},
            {"to": [f"a{i}@example.com" for i in range(51)]},
            {"subject": "a\r\nBcc: x@example.com"},
            {"body": "b" * 100_001},
        ],
    )
    def test_bad_input_sends_no_imap_command(self, server, h, kw):
        with pytest.raises(ToolInputError):
            h.service.draft(**kw)
        assert server.commands == [] and not self.drafts(server)

    def test_reply_keeps_the_original_unread(self, server, h):
        uid = deliver(
            server,
            "INBOX",
            subject="Plans",
            message_id="<m2@x>",
            in_reply_to="<m1@x>",
            references=["<m1@x>"],
        )
        mid = MailId("INBOX", 1, uid).encode()
        out = h.service.draft(to=["alice@example.com"], body="Sounds good", in_reply_to=mid)
        assert server.folders["INBOX"].messages[uid].flags == set()  # still unread
        reads = [c for c in server.commands if c[0] in ("EXAMINE", "SELECT", "UID")]
        assert [c[0] for c in reads] == ["EXAMINE", "UID"]
        assert reads[1][1] == "FETCH" and "BODY.PEEK[]" in reads[1][3]
        [draft] = self.drafts(server)
        p = parse_message(draft.raw)
        assert (p.in_reply_to, p.references, p.subject) == (
            "<m2@x>", ["<m1@x>", "<m2@x>"], "Re: Plans"
        )  # fmt: skip
        assert p.to == "alice@example.com"  # recipients are never filled in from the original
        assert out["in_reply_to"] == mid

    def test_reply_alone_is_enough(self, server, h):
        uid = deliver(server, "INBOX", subject="Plans", message_id="<m2@x>")
        h.service.draft(in_reply_to=MailId("INBOX", 1, uid).encode())
        [draft] = self.drafts(server)
        p = parse_message(draft.raw)
        assert (p.subject, p.to) == ("Re: Plans", "")

    def test_bad_reply_ids_save_nothing(self, server, h):
        uid = deliver(server, "INBOX")
        junk = deliver(server, "Junk")  # hidden from the read scope by default
        for bad, message in (
            ("%%%", "malformed"),
            (MailId("Junk", 1, junk).encode(), "unknown folder: Junk"),
            (MailId("INBOX", 7, uid).encode(), "stale"),
            (MailId("INBOX", 1, 999).encode(), "not found"),
        ):
            with pytest.raises(ToolInputError, match=message):
                h.service.draft(subject="x", in_reply_to=bad)
        assert not self.drafts(server)
        assert "APPEND" not in [c[0] for c in server.commands]

    def test_resync_only_when_drafts_is_in_the_read_scope(self, server, make, monkeypatch):
        for special, expected in (({}, []), ({"drafts": True}, [("Drafts",)])):
            h = make(alice_special=special)
            asked: list = []
            monkeypatch.setattr(h.sync, "request_resync", lambda *names, a=asked: a.append(names))
            h.service.draft(subject="x")
            assert asked == expected

    def test_draft_readable_when_drafts_is_in_scope(self, server, make):
        h = make(alice_special={"drafts": True})
        out = h.service.draft(subject="Lunch", body="Noon?")
        got = h.service.get(out["id"])
        assert got["headers"]["subject"] == "Lunch"
        assert server.folders["Drafts"].messages[got["uid"]].flags == {"\\Draft", "\\Seen"}

    def test_without_a_writer_nothing_is_written(self, server, harness):
        with pytest.raises(ToolInputError, match="not enabled"):
            harness.service.draft(subject="x")
        assert not self.drafts(server)


def test_sync_resyncs_requested_folders(server, harness):
    uid = deliver(server, "INBOX", subject="moving")
    harness.run_sync()
    server.move("INBOX", uid, "Work")  # as a write would, behind the index's back
    harness.sync.request_resync("INBOX", "Work", "Deleted Messages")  # last one is hidden
    harness.sync.interval = 0.05
    server.commands.clear()
    harness.sync._wait_for_next_pass(harness._client)
    assert [m.subject for m in harness.index.search(folders=["Work"])] == ["moving"]
    assert harness.index.get("INBOX", uid) is None
    examined = [c[1] for c in server.commands if c[0] == "EXAMINE"]
    assert '"Deleted Messages"' not in examined  # out of scope: never synced


def test_fake_records_capability_on_connect():
    server = FakeServer()
    server.connect()
    assert server.commands == [("CAPABILITY",)]


def test_cross_folder_search_latency(tmp_path, server):
    """Acceptance: cross-folder search p50 < 1 s (index of 40 folders x 250 messages)."""
    h = Harness(server, make_config(tmp_path).account("alice"), tmp_path)
    words = ["invoice", "travel", "dinner", "project", "report", "budget", "school", "doctor"]
    rows = []
    for f in range(40):
        for i in range(250):
            raw = make_message(
                subject=f"{words[i % 8]} {i}",
                body=f"{words[(i * 3) % 8]} details {f}-{i} " * 20,
                date=NOW - dt.timedelta(hours=i * 7),
                message_id=f"<{f}.{i}@x>",
            )
            rows.append((f"Folder {f}", 1, i + 1, parse_message(raw), len(raw)))
    h.index.add_many(rows)
    for f in range(40):
        h.index.upsert_folder(f"Folder {f}", 1, 1, 251, 250)
    timings = []
    for w in words * 3:
        t = time.perf_counter()
        out = h.service.search(query=w, limit=50)
        timings.append(time.perf_counter() - t)
        assert len(out["results"]) == 50
    assert statistics.median(timings) < 1.0
