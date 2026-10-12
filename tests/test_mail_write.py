# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

"""The mail write path (mail/imap_write.py) and the fake's write rules (ADR-0004)."""

from __future__ import annotations

import datetime as dt
import imaplib

import pytest

from pv_icloud.errors import ToolInputError, WriteFailed
from pv_icloud.mail.ids import MailId, decode_id
from pv_icloud.mail.imap_write import MailWriter, find_archive, find_trash

from .conftest import Harness, make_config
from .fakeimap import FakeConn, FakeServer, make_message

NOW = dt.datetime.now(dt.UTC)
# Commands that only open or inspect the connection; everything else is the write sequence.
_SETUP = frozenset({"CAPABILITY", "LOGIN", "ENABLE", "LIST", "STATUS"})


def deliver(server, folder, **kw) -> int:
    return server.deliver(folder, make_message(date=NOW, **kw), NOW.date())


def sequence(server) -> list[str]:
    return [f"UID {c[1]}" if c[0] == "UID" else c[0] for c in server.commands if c[0] not in _SETUP]


def to(name):
    return lambda folders: name


@pytest.fixture
def h(server, tmp_path):
    server.folder("Archive")
    return Harness(server, make_config(tmp_path).account("alice"), tmp_path)


@pytest.fixture
def writer(h):
    return MailWriter(h.pool)


class TestFakeWriteRules:
    """The fake refuses every write form except the guarded single-UID ones."""

    @pytest.fixture
    def conn(self, server):
        deliver(server, "INBOX")
        deliver(server, "INBOX")
        c = server.connect()
        c.login("alice@icloud.com", "app-pw")
        return c

    @pytest.mark.parametrize(
        "method", ["store", "copy", "expunge", "close", "move", "create", "delete"]
    )
    def test_plain_commands_are_refused(self, conn, method):
        with pytest.raises(AssertionError, match="forbidden"):
            getattr(conn, method)("1")

    @pytest.mark.parametrize(
        ("flags", "date_time"),
        [
            (None, None),
            ("(\\Seen)", None),
            ("(\\Draft)", None),
            ("(\\Draft \\Seen \\Flagged)", None),
            ("(\\Seen \\Draft)", None),
            ("(\\Draft \\Seen)", '"12-Oct-2026 12:00:00 +0000"'),
        ],
    )
    def test_append_only_saves_a_draft(self, conn, server, flags, date_time):
        with pytest.raises(AssertionError, match="forbidden"):
            conn.append('"Drafts"', flags, date_time, b"Subject: x\r\n\r\nx\r\n")
        assert not server.folders["Drafts"].messages

    def test_append_draft(self, conn, server):
        typ, _ = conn.append('"Drafts"', "(\\Draft \\Seen)", None, b"Subject: x\n\nx\n")
        assert typ == "OK"
        [msg] = server.folders["Drafts"].messages.values()
        assert msg.flags == {"\\Draft", "\\Seen"}
        assert msg.raw == b"Subject: x\r\n\r\nx\r\n"
        assert conn.response("APPENDUID") == ("APPENDUID", [f"1 {msg.uid}".encode()])

    def test_append_switches_and_missing_folder(self, conn, server):
        assert conn.append('"Nope"', "(\\Draft \\Seen)", None, b"x")[0] == "NO"
        server.fail_append = True
        assert conn.append('"Drafts"', "(\\Draft \\Seen)", None, b"x")[0] == "NO"
        assert not server.folders["Drafts"].messages
        server.fail_append, server.appenduid = False, "omit"
        assert conn.append('"Drafts"', "(\\Draft \\Seen)", None, b"x")[0] == "OK"
        assert conn.response("APPENDUID") == ("APPENDUID", [None])
        assert len(server.folders["Drafts"].messages) == 1

    @pytest.mark.parametrize(
        "args",
        [
            ("COPY", "1:2", '"Work"'),
            ("COPY", "1,2", '"Work"'),
            ("COPY", "*", '"Work"'),
            ("STORE", "1:2", "+FLAGS.SILENT", "(\\Deleted)"),
            ("STORE", "1", "+FLAGS", "(\\Deleted)"),
            ("STORE", "1", "-FLAGS.SILENT", "(\\Deleted)"),
            ("STORE", "1", "FLAGS.SILENT", "(\\Deleted)"),
            ("STORE", "1", "+FLAGS.SILENT", "(\\Seen)"),
            ("STORE", "1", "+FLAGS.SILENT", "(\\Deleted \\Seen)"),
            ("EXPUNGE", "1:2"),
            ("EXPUNGE", "1", "2"),
            ("MOVE", "1", '"Work"'),
        ],
    )
    def test_other_uid_forms_are_refused(self, conn, args):
        conn.select('"INBOX"')
        with pytest.raises(AssertionError):
            conn.uid(*args)

    def test_writes_need_select(self, conn):
        conn.select('"INBOX"', readonly=True)
        with pytest.raises(AssertionError, match="SELECT"):
            conn.uid("COPY", "1", '"Work"')

    def test_non_peek_fetch_still_refused_after_select(self, conn):
        conn.select('"INBOX"')
        with pytest.raises(AssertionError, match="non-PEEK"):
            conn.uid("FETCH", "1", "(BODY[])")

    def test_capabilities_and_special_use_like_icloud(self, server):
        assert {"UIDPLUS", "UNSELECT"} <= set(server.capabilities)
        assert "MOVE" not in server.capabilities
        server.folder("Archive")
        _, lines = server.connect().list()
        attrs = {}
        for line in lines:
            flags, _, name = line.decode().partition(' "/" ')
            attrs[name.strip('"')] = flags
        assert attrs["Deleted Messages"] == "(\\HasNoChildren \\Trash)"
        assert attrs["Archive"] == attrs["Drafts"] == "(\\HasNoChildren)"


class TestGuardedMove:
    def test_exact_command_sequence(self, server, h, writer):
        uid = deliver(server, "INBOX", subject="file me")
        server.commands.clear()
        moved = writer.guarded_move(MailId("INBOX", 1, uid), to("Work"))
        assert sequence(server) == [
            "SELECT", "UID FETCH", "UID COPY", "UID STORE", "UID EXPUNGE", "LOGOUT",
        ]  # fmt: skip
        write_cmds = [c for c in server.commands if c[0] in ("SELECT", "UID")]
        assert write_cmds == [
            ("SELECT", '"INBOX"'),
            ("UID", "FETCH", str(uid), "(UID FLAGS)"),
            ("UID", "COPY", str(uid), '"Work"'),
            ("UID", "STORE", str(uid), "+FLAGS.SILENT", "(\\Deleted)"),
            ("UID", "EXPUNGE", str(uid)),
        ]
        assert not server.folders["INBOX"].messages
        [copy] = server.folders["Work"].messages.values()
        assert (moved.from_folder, moved.to_folder) == ("INBOX", "Work")
        assert moved.new_id == MailId("Work", 1, copy.uid)

    def test_flags_kept_and_seen_not_added(self, server, writer):
        unread = deliver(server, "INBOX")
        read = deliver(server, "INBOX")
        server.folders["INBOX"].messages[read].flags = {"\\Seen", "\\Flagged"}
        a = writer.guarded_move(MailId("INBOX", 1, unread), to("Work"))
        b = writer.guarded_move(MailId("INBOX", 1, read), to("Work"))
        work = server.folders["Work"].messages
        assert work[a.new_id.uid].flags == set()
        assert work[b.new_id.uid].flags == {"\\Seen", "\\Flagged"}

    def test_other_deleted_messages_survive(self, server, writer):
        other = deliver(server, "INBOX", subject="someone else's")
        server.folders["INBOX"].messages[other].flags.add("\\Deleted")
        uid = deliver(server, "INBOX")
        writer.guarded_move(MailId("INBOX", 1, uid), to("Archive"))
        assert list(server.folders["INBOX"].messages) == [other]

    def test_without_uidplus_nothing_is_sent(self, tmp_path):
        server = FakeServer(capabilities=("IMAP4REV1", "IDLE", "CONDSTORE", "ENABLE"))
        server.folder("INBOX")
        server.folder("Work")
        uid = deliver(server, "INBOX")
        h = Harness(server, make_config(tmp_path).account("alice"), tmp_path)
        server.commands.clear()
        with pytest.raises(WriteFailed, match="cannot move messages safely"):
            MailWriter(h.pool).guarded_move(MailId("INBOX", 1, uid), to("Work"))
        assert sequence(server) == ["LOGOUT"]
        assert uid in server.folders["INBOX"].messages

    def test_stale_uidvalidity_refused_before_copy(self, server, writer):
        uid = deliver(server, "INBOX")
        server.set_uidvalidity("INBOX", 9)
        server.commands.clear()
        with pytest.raises(ToolInputError, match="stale"):
            writer.guarded_move(MailId("INBOX", 1, uid), to("Work"))
        assert sequence(server) == ["SELECT", "LOGOUT"]

    def test_missing_uid_refused_before_copy(self, server, writer):
        server.commands.clear()
        with pytest.raises(ToolInputError, match="not found"):
            writer.guarded_move(MailId("INBOX", 1, 42), to("Work"))
        assert sequence(server) == ["SELECT", "UID FETCH", "LOGOUT"]

    def test_missing_source_folder_is_not_found(self, server, writer):
        server.commands.clear()
        with pytest.raises(ToolInputError, match="not found"):
            writer.guarded_move(MailId("Gone", 1, 1), to("Work"))
        assert sequence(server) == ["LOGOUT"]

    @pytest.mark.parametrize("mode", ["omit", "mismatch"])
    def test_unconfirmed_copy_stops_before_store(self, server, writer, mode):
        server.copyuid = mode
        uid = deliver(server, "INBOX")
        server.commands.clear()
        with pytest.raises(WriteFailed) as err:
            writer.guarded_move(MailId("INBOX", 1, uid), to("Work"))
        assert "could not be confirmed" in str(err.value)
        assert "left in its source folder" in str(err.value)
        assert "copy may also be in the destination" in str(err.value)
        assert sequence(server) == ["SELECT", "UID FETCH", "UID COPY", "LOGOUT"]
        assert server.folders["INBOX"].messages[uid].flags == set()

    @pytest.mark.parametrize("code", ["99 {uid} 1", "1 {uid}:9 1:2", "1 {uid} 1,2"])
    def test_copyuid_must_name_one_uid_in_the_destination(self, server, writer, monkeypatch, code):
        uid = deliver(server, "INBOX")
        real = FakeConn._copy

        def copy(conn, u, mailbox):
            out = real(conn, u, mailbox)
            conn._responses["COPYUID"] = [code.format(uid=u).encode()]
            return out

        monkeypatch.setattr(FakeConn, "_copy", copy)
        with pytest.raises(WriteFailed, match="could not be confirmed"):
            writer.guarded_move(MailId("INBOX", 1, uid), to("Work"))
        assert server.folders["INBOX"].messages[uid].flags == set()

    def test_failed_copy_changes_nothing(self, server, writer):
        server.fail_copy = True
        uid = deliver(server, "INBOX")
        server.commands.clear()
        with pytest.raises(WriteFailed, match="left unchanged"):
            writer.guarded_move(MailId("INBOX", 1, uid), to("Work"))
        assert sequence(server) == ["SELECT", "UID FETCH", "UID COPY", "LOGOUT"]
        assert not server.folders["Work"].messages

    def test_failed_store_leaves_the_message_in_both_folders(self, server, writer):
        server.fail_store = True
        uid = deliver(server, "INBOX")
        with pytest.raises(WriteFailed) as err:
            writer.guarded_move(MailId("INBOX", 1, uid), to("Work"))
        assert "in the destination" in str(err.value) and "both folders" in str(err.value)
        assert server.folders["INBOX"].messages[uid].flags == set()
        assert len(server.folders["Work"].messages) == 1

    def test_failed_expunge_leaves_the_source_marked_deleted(self, server, writer):
        server.fail_expunge = True
        uid = deliver(server, "INBOX")
        with pytest.raises(WriteFailed) as err:
            writer.guarded_move(MailId("INBOX", 1, uid), to("Work"))
        assert "in the destination" in str(err.value) and "marked deleted" in str(err.value)
        assert server.folders["INBOX"].messages[uid].flags == {"\\Deleted"}
        assert len(server.folders["Work"].messages) == 1

    def test_connection_lost_during_copy_is_reported_as_unconfirmed(
        self, server, writer, monkeypatch
    ):
        uid = deliver(server, "INBOX")

        def drop(conn, u, mailbox):
            raise imaplib.IMAP4.abort("socket error: EOF")

        monkeypatch.setattr(FakeConn, "_copy", drop)
        with pytest.raises(WriteFailed, match="could not be confirmed"):
            writer.guarded_move(MailId("INBOX", 1, uid), to("Work"))

    def test_write_connection_never_returns_to_the_read_pool(self, server, h, writer):
        uid = deliver(server, "INBOX")
        h.service.folders()  # warm the read pool with one connection
        before = server.connections
        writer.guarded_move(MailId("INBOX", 1, uid), to("Work"))
        assert server.connections == before + 1  # a fresh connection, then logged out
        assert h.pool._free.qsize() == 1  # only the read connection is pooled
        reader = h.pool._free.queue[0]
        assert reader.conn is not None and reader.conn.readonly is True

    def test_destination_resolver_sees_folder_attributes(self, server, writer):
        uid = deliver(server, "INBOX")
        seen = {}

        def resolve(folders):
            seen.update(folders)
            return find_trash(folders) or "?"

        moved = writer.guarded_move(MailId("INBOX", 1, uid), resolve)
        assert moved.to_folder == "Deleted Messages"
        assert "\\trash" in seen["Deleted Messages"]
        assert decode_id(moved.new_id.encode()) == moved.new_id

    def test_resolver_refusal_sends_no_write_command(self, server, writer):
        uid = deliver(server, "INBOX")
        server.commands.clear()

        def refuse(folders):
            raise ToolInputError("folder is protected")

        with pytest.raises(ToolInputError, match="protected"):
            writer.guarded_move(MailId("INBOX", 1, uid), refuse)
        assert sequence(server) == ["LOGOUT"]


DRAFT = b"Subject: Lunch\r\nTo: bob@example.com\r\n\r\nNoon?\r\n"


class TestAppendDraft:
    def test_exact_command_sequence(self, server, writer):
        server.commands.clear()
        saved = writer.append_draft(DRAFT)
        opened = {"CAPABILITY", "LOGIN", "ENABLE"}
        assert [c[0] for c in server.commands if c[0] not in opened] == ["LIST", "APPEND", "LOGOUT"]
        assert ("APPEND", '"Drafts"', "(\\Draft \\Seen)") in server.commands
        [msg] = server.folders["Drafts"].messages.values()
        assert msg.flags == {"\\Draft", "\\Seen"}
        assert msg.raw == DRAFT
        assert saved.folder == "Drafts"
        assert saved.new_id == MailId("Drafts", 1, msg.uid)

    def test_other_mail_is_untouched(self, server, writer):
        deliver(server, "INBOX")
        before = server.all_flags()
        writer.append_draft(DRAFT)
        after = server.all_flags()
        assert {k: v for k, v in after.items() if k[0] != "Drafts"} == before

    def test_drafts_found_while_hidden_from_the_read_scope(self, server, h, writer):
        assert not h.account.mail.folders.allows("Drafts")  # hidden by default
        assert writer.append_draft(DRAFT).new_id is not None

    def test_saved_without_appenduid(self, server, writer):
        server.appenduid = "omit"
        saved = writer.append_draft(DRAFT)
        assert saved.new_id is None and saved.folder == "Drafts"
        assert len(server.folders["Drafts"].messages) == 1  # saved once, never retried

    @pytest.mark.parametrize("code", [b"1 2:3", b"1 2,3", b"x 2", b"1"])
    def test_appenduid_must_name_one_uid(self, server, writer, monkeypatch, code):
        real = FakeConn.append

        def append(conn, *args):
            out = real(conn, *args)
            conn._responses["APPENDUID"] = [code]
            return out

        monkeypatch.setattr(FakeConn, "append", append)
        assert writer.append_draft(DRAFT).new_id is None
        assert len(server.folders["Drafts"].messages) == 1

    def test_without_uidplus_nothing_is_sent(self, tmp_path):
        server = FakeServer(capabilities=("IMAP4REV1", "IDLE", "CONDSTORE", "ENABLE"))
        server.folder("Drafts")
        h = Harness(server, make_config(tmp_path).account("alice"), tmp_path)
        server.commands.clear()
        with pytest.raises(WriteFailed, match="no UIDPLUS.*nothing was saved"):
            MailWriter(h.pool).append_draft(DRAFT)
        assert sequence(server) == ["LOGOUT"]  # not even LIST
        assert not any(c[0] == "LIST" for c in server.commands)
        assert not server.folders["Drafts"].messages

    def test_missing_drafts_refused_before_append(self, server, writer):
        del server.folders["Drafts"]
        server.commands.clear()
        with pytest.raises(ToolInputError, match="^Drafts folder not found; nothing was saved$"):
            writer.append_draft(DRAFT)
        assert "APPEND" not in [c[0] for c in server.commands]

    def test_drafts_found_by_name_only(self, server, writer):
        # A folder marked \Drafts but named otherwise is never used (iCloud has no attribute).
        del server.folders["Drafts"]
        server.folder("Entwürfe").special_use = "\\Drafts"
        with pytest.raises(ToolInputError, match="Drafts folder not found"):
            writer.append_draft(DRAFT)
        assert not server.folders["Entwürfe"].messages

    def test_failed_append_saves_nothing(self, server, writer):
        server.fail_append = True
        with pytest.raises(WriteFailed, match="^saving the draft failed; nothing was saved$"):
            writer.append_draft(DRAFT)
        assert not server.folders["Drafts"].messages

    def test_connection_lost_during_append(self, server, writer, monkeypatch):
        def drop(conn, *args):
            raise imaplib.IMAP4.abort("socket error: EOF")

        monkeypatch.setattr(FakeConn, "append", drop)
        with pytest.raises(WriteFailed, match="may or may not have been saved"):
            writer.append_draft(DRAFT)

    def test_write_connection_never_returns_to_the_read_pool(self, server, h, writer):
        h.service.folders()
        before = server.connections
        writer.append_draft(DRAFT)
        assert server.connections == before + 1
        assert h.pool._free.qsize() == 1


class TestFolderResolution:
    def test_trash_by_attribute_then_by_name(self):
        assert find_trash({"Bin": frozenset({"\\trash"}), "Deleted Messages": frozenset()}) == (
            "Bin"
        )
        assert find_trash({"INBOX": frozenset(), "Deleted Messages": frozenset()}) == (
            "Deleted Messages"
        )
        assert find_trash({"INBOX": frozenset()}) is None

    def test_archive_by_name_only(self):
        folders = {"INBOX": frozenset(), "Archive": frozenset(), "Old": frozenset({"\\archive"})}
        assert find_archive(folders, "Archive") == "Archive"
        assert find_archive(folders, "Old Mail") is None  # never by attribute
        assert find_archive({"Old Mail": frozenset()}, "Old Mail") == "Old Mail"
