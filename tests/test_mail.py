# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

"""Sync + tool behaviour against the fake IMAP server."""

from __future__ import annotations

import datetime as dt
import statistics
import time

import pytest

from pv_icloud.mail.ids import MailId
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
