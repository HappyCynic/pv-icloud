# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

"""In-memory stand-in for an imaplib.IMAP4 connection (the subset pv_icloud uses).

It enforces the read and write rules (CLAUDE.md, ADR-0004). Reads: EXAMINE, and BODY.PEEK
or `(UID FLAGS)` fetches only. Writes, only on a SELECTed folder: `UID COPY` of one UID
(answered with COPYUID), `UID STORE` of one UID with exactly `+FLAGS.SILENT (\\Deleted)`, and
`UID EXPUNGE` of one UID. Every other form (STORE, COPY, EXPUNGE, CLOSE, MOVE, APPEND, UID
sets or ranges, other flags) raises, so a test fails if the server ever sends one.

Like iCloud (checked 2026-10-12) it advertises UIDPLUS and UNSELECT but not MOVE, marks
`Deleted Messages` as \\Trash, and gives `Archive` and `Drafts` no special-use attribute.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import email.utils
import re
import threading
from dataclasses import dataclass, field
from email.message import EmailMessage

from pv_icloud import utf7


@dataclass
class FakeMessage:
    uid: int
    raw: bytes
    internaldate: dt.date
    flags: set[str] = field(default_factory=set)


@dataclass
class FakeFolder:
    name: str
    uidvalidity: int = 1
    uidnext: int = 1
    modseq: int = 1
    messages: dict[int, FakeMessage] = field(default_factory=dict)
    noselect: bool = False
    special_use: str | None = None  # LIST attribute such as \Trash


# iCloud's special-use attributes; Archive and Drafts have none (checked 2026-10-12).
SPECIAL_USE = {"Deleted Messages": "\\Trash", "Sent Messages": "\\Sent"}
CAPABILITIES = (
    "IMAP4REV1", "IDLE", "CONDSTORE", "QRESYNC", "ESEARCH", "ENABLE", "UIDPLUS", "UNSELECT",
)  # fmt: skip
_ONE_UID = re.compile(r"[1-9][0-9]*")


class FakeServer:
    def __init__(self, capabilities=CAPABILITIES):
        self.folders: dict[str, FakeFolder] = {}
        self.capabilities = tuple(capabilities)
        self.commands: list[tuple] = []
        self.lock = threading.Lock()
        self.password = "app-pw"
        self.connections = 0
        self.fail_examine: set[str] = set()
        # Write-path failure switches.
        self.copyuid = "ok"  # "ok", "omit" (no COPYUID) or "mismatch" (names another UID)
        self.fail_copy = False
        self.fail_store = False
        self.fail_expunge = False

    def folder(self, name: str) -> FakeFolder:
        if name not in self.folders:
            self.folders[name] = FakeFolder(name, special_use=SPECIAL_USE.get(name))
        return self.folders[name]

    def deliver(self, folder: str, raw: bytes, date: dt.date | None = None) -> int:
        f = self.folder(folder)
        uid = f.uidnext
        f.uidnext += 1
        f.modseq += 1
        f.messages[uid] = FakeMessage(uid, raw, date or dt.datetime.now(dt.UTC).date())
        return uid

    def expunge(self, folder: str, uid: int) -> None:
        f = self.folders[folder]
        del f.messages[uid]
        f.modseq += 1

    def move(self, src: str, uid: int, dst: str) -> int:
        msg = self.folders[src].messages[uid]
        self.expunge(src, uid)
        new = self.deliver(dst, msg.raw, msg.internaldate)
        self.folders[dst].messages[new].flags = set(msg.flags)
        return new

    def set_uidvalidity(self, folder: str, value: int) -> None:
        f = self.folders[folder]
        f.uidvalidity = value
        f.modseq += 1

    def connect(self) -> FakeConn:
        self.connections += 1
        return FakeConn(self)

    def all_flags(self) -> dict[tuple[str, int], frozenset[str]]:
        return {
            (f.name, m.uid): frozenset(m.flags)
            for f in self.folders.values()
            for m in f.messages.values()
        }


def make_message(
    *,
    subject="Hello",
    from_="Alice Example <alice@example.com>",
    to="Bob <bob@example.com>",
    cc: str | None = None,
    body="Hi Bob",
    date: dt.datetime | None = None,
    message_id: str | None = None,
    in_reply_to: str | None = None,
    references: list[str] | None = None,
    html: str | None = None,
    attachment: tuple[str, bytes] | None = None,
) -> bytes:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Date"] = email.utils.format_datetime(date or dt.datetime.now(dt.UTC))
    if message_id:
        msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = " ".join(references)
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    if attachment:
        name, data = attachment
        msg.add_attachment(data, maintype="application", subtype="pdf", filename=name)
    return msg.as_bytes()


_ARG_RE = re.compile(r'"((?:[^"\\]|\\.)*)"|(\S+)')
_PEEK_RE = re.compile(r"BODY\.PEEK\[\]<0\.(\d+)>")


def _unq(s: str) -> str:
    if s.startswith('"') and s.endswith('"'):
        return re.sub(r"\\(.)", r"\1", s[1:-1])
    return s


def _parse_imap_date(s: str) -> dt.date:
    return dt.datetime.strptime(s, "%d-%b-%Y").date()


class FakeConn:
    def __init__(self, server: FakeServer):
        self.s = server
        self.capabilities = server.capabilities
        self.logged_in = False
        self.selected: FakeFolder | None = None
        self.readonly = True
        self.enabled: set[str] = set()
        self._responses: dict[str, list[bytes]] = {}
        self.idle_events: list[tuple[str, list]] = []
        self._log("CAPABILITY")  # imaplib sends it on connect to fill .capabilities

    def _log(self, *cmd):
        with self.s.lock:
            self.s.commands.append(cmd)

    def login(self, user, password):
        self._log("LOGIN", user)
        if password != self.s.password:
            import imaplib

            raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Authentication failed.")
        self.logged_in = True
        return "OK", [b"LOGIN completed"]

    def enable(self, capability):
        self._log("ENABLE", capability)
        self.enabled.add(capability.upper())
        return "OK", [b""]

    def logout(self):
        self._log("LOGOUT")
        return "BYE", [b""]

    def shutdown(self):
        pass

    def noop(self):
        self._log("NOOP")
        return "OK", [b""]

    def list(self, directory='""', pattern="*"):
        self._log("LIST")
        out = []
        for f in self.s.folders.values():
            flags = "\\Noselect" if f.noselect else "\\HasNoChildren"
            if f.special_use:
                flags += " " + f.special_use
            out.append(f'({flags}) "/" "{utf7.encode(f.name)}"'.encode())
        return "OK", out

    def _folder(self, quoted: str) -> FakeFolder | None:
        return self.s.folders.get(utf7.decode(_unq(quoted)))

    def status(self, mailbox, names):
        self._log("STATUS", mailbox, names)
        f = self._folder(mailbox)
        if f is None:
            return "NO", [b"no such mailbox"]
        line = (
            f"{mailbox} (MESSAGES {len(f.messages)} UIDNEXT {f.uidnext} "
            f"UIDVALIDITY {f.uidvalidity} HIGHESTMODSEQ {f.modseq})"
        )
        return "OK", [line.encode()]

    def select(self, mailbox="INBOX", readonly=False):
        self._log("EXAMINE" if readonly else "SELECT", mailbox)
        f = self._folder(mailbox)
        if f is None or f.noselect or f.name in self.s.fail_examine:
            self.selected = None
            return "NO", [b"Mailbox does not exist"]
        self.selected = f
        self.readonly = readonly
        self._responses = {
            "UIDVALIDITY": [str(f.uidvalidity).encode()],
            "UIDNEXT": [str(f.uidnext).encode()],
        }
        if "CONDSTORE" in self.enabled:  # like iCloud: only after ENABLE CONDSTORE
            self._responses["HIGHESTMODSEQ"] = [str(f.modseq).encode()]
        return "OK", [str(len(f.messages)).encode()]

    def response(self, code):
        return code, self._responses.pop(code, [None])

    def uid(self, command, *args):
        command = command.upper()
        self._log("UID", command, *args)
        if self.selected is None:
            import imaplib

            raise imaplib.IMAP4.error("No mailbox selected")
        if command == "SEARCH":
            return self._search(args)
        if command == "FETCH":
            return self._fetch(*args)
        if command not in ("COPY", "STORE", "EXPUNGE"):
            raise AssertionError(f"UID {command} is forbidden")
        if self.readonly:
            raise AssertionError(f"UID {command} needs a SELECTed (read-write) folder")
        if not args or not _ONE_UID.fullmatch(args[0]):
            raise AssertionError(f"UID {command} must name exactly one UID, got {args[:1]}")
        if command == "COPY":
            return self._copy(int(args[0]), *args[1:])
        if command == "STORE":
            return self._store(int(args[0]), *args[1:])
        return self._expunge(int(args[0]), *args[1:])

    def _copy(self, uid, mailbox):
        dest = self._folder(mailbox)
        msg = self.selected.messages.get(uid)
        if self.s.fail_copy or dest is None:
            return "NO", [b"[TRYCREATE] COPY failed"]
        if msg is None:  # RFC 9051: copying a UID that is gone is not an error
            return "OK", [b"COPY completed"]
        new = self.s.deliver(dest.name, msg.raw, msg.internaldate)
        dest.messages[new].flags = set(msg.flags)
        src = uid + 1 if self.s.copyuid == "mismatch" else uid
        if self.s.copyuid != "omit":
            self._responses["COPYUID"] = [f"{dest.uidvalidity} {src} {new}".encode()]
        return "OK", [b"COPY completed"]

    def _store(self, uid, op, flags):
        if (op, flags) != ("+FLAGS.SILENT", "(\\Deleted)"):
            raise AssertionError(f"UID STORE {op} {flags} is forbidden")
        if self.s.fail_store:
            return "NO", [b"STORE failed"]
        msg = self.selected.messages.get(uid)
        if msg is not None:
            msg.flags.add("\\Deleted")
            self.selected.modseq += 1
        return "OK", [None]

    def _expunge(self, uid, *rest):
        if rest:
            raise AssertionError(f"UID EXPUNGE takes one UID, got {rest}")
        if self.s.fail_expunge:
            return "NO", [b"EXPUNGE failed"]
        msg = self.selected.messages.get(uid)
        if msg is not None and "\\Deleted" in msg.flags:
            self.s.expunge(self.selected.name, uid)
        return "OK", [None]

    # Commands that must never be sent: plain STORE/COPY/EXPUNGE, CLOSE (expunges every
    # \Deleted message), MOVE, APPEND and folder changes.
    def _forbidden(self, name):
        def refuse(*args, **kwargs):
            raise AssertionError(f"{name} is forbidden")

        return refuse

    def __getattr__(self, name):
        if name in (
            "store", "copy", "expunge", "close", "move", "append", "create", "delete", "rename",
            "subscribe", "unsubscribe", "xatom", "_simple_command", "_command",
        ):  # fmt: skip
            return self._forbidden(name.upper())
        raise AttributeError(name)

    def _search(self, args):
        tokens = [m.group(0) for a in args for m in _ARG_RE.finditer(a)]
        msgs = list(self.selected.messages.values())
        i = 0
        while i < len(tokens):
            key = tokens[i].upper()
            val = _unq(tokens[i + 1]) if i + 1 < len(tokens) else ""
            i += 2
            parsed = {m.uid: email.message_from_bytes(m.raw) for m in msgs}
            if key == "SINCE":
                d = _parse_imap_date(val)
                msgs = [m for m in msgs if m.internaldate >= d]
            elif key == "BEFORE":
                d = _parse_imap_date(val)
                msgs = [m for m in msgs if m.internaldate < d]
            elif key in ("FROM", "TO", "SUBJECT"):
                msgs = [m for m in msgs if val.lower() in str(parsed[m.uid][key] or "").lower()]
            elif key == "TEXT":
                msgs = [m for m in msgs if val.lower().encode() in m.raw.lower()]
            else:
                raise AssertionError(f"unsupported SEARCH key {key}")
        return "OK", [" ".join(str(m.uid) for m in msgs).encode()]

    def _fetch(self, uidset, items):
        if items == "(UID FLAGS)":
            return self._fetch_flags(uidset)
        m = _PEEK_RE.search(items)
        if "BODY[" in items.replace("BODY.PEEK[", "") or not m:
            raise AssertionError(f"non-PEEK fetch is forbidden: {items}")
        cap = int(m.group(1))
        wanted = set()
        for part in uidset.split(","):
            if ":" in part:
                a, b = part.split(":")
                wanted.update(range(int(a), int(b) + 1))
            else:
                wanted.add(int(part))
        out = []
        seqs = {uid: n + 1 for n, uid in enumerate(sorted(self.selected.messages))}
        for uid in sorted(wanted):
            msg = self.selected.messages.get(uid)
            if msg is None:
                continue
            body = msg.raw[:cap]
            idate = msg.internaldate.strftime("%d-%b-%Y") + " 12:00:00 +0000"
            head = (
                f"{seqs[uid]} (UID {uid} RFC822.SIZE {len(msg.raw)} "
                f'INTERNALDATE "{idate}" BODY[]<0> {{{len(body)}}}'
            )
            out.append((head.encode(), body))
            out.append(b")")
        return "OK", out

    def _fetch_flags(self, uidset):
        if not _ONE_UID.fullmatch(uidset):
            raise AssertionError(f"flag fetches name one UID, got {uidset}")
        seqs = {uid: n + 1 for n, uid in enumerate(sorted(self.selected.messages))}
        msg = self.selected.messages.get(int(uidset))
        if msg is None:
            return "OK", [None]
        flags = " ".join(sorted(msg.flags))
        return "OK", [f"{seqs[msg.uid]} (UID {msg.uid} FLAGS ({flags}))".encode()]

    @contextlib.contextmanager
    def idle(self, duration=None):
        self._log("IDLE")
        events, self.idle_events = self.idle_events, []
        yield iter(events)
