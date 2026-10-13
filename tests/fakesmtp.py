# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

"""In-process ESMTP server on 127.0.0.1 for mail_send tests (add-mail-send).

It speaks just enough ESMTP for smtplib: EHLO, STARTTLS, AUTH PLAIN, MAIL, RCPT, DATA, RSET,
NOOP and QUIT. STARTTLS uses a self-signed certificate for `localhost`, made at test time
(no private key is checked in). AUTH, MAIL, RCPT and DATA before STARTTLS are refused and
recorded in `violations`, so a test fails if the client ever sends them in the clear.

Switches: no STARTTLS offered, refuse AUTH, refuse given recipients, drop the connection after
the message data, and file a copy of each accepted message into a FakeServer's Sent Messages
(as iCloud may do on its own).
"""

from __future__ import annotations

import base64
import contextlib
import datetime as dt
import ipaddress
import socket
import socketserver
import ssl
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from pv_icloud.mail.smtp import Endpoint

from .fakeimap import FakeServer

USER = "alice@icloud.com"
PASSWORD = "app-pw"


@dataclass(frozen=True)
class TestCert:
    cert_pem: bytes
    key_pem: bytes

    def server_context(self) -> ssl.SSLContext:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        with tempfile.TemporaryDirectory() as tmp:
            cert, key = Path(tmp) / "cert.pem", Path(tmp) / "key.pem"
            cert.write_bytes(self.cert_pem)
            key.write_bytes(self.key_pem)
            ctx.load_cert_chain(cert, key)
        return ctx

    def client_context(self) -> ssl.SSLContext:
        """A verifying context that trusts only this certificate."""
        return ssl.create_default_context(cadata=self.cert_pem.decode())


def make_cert() -> TestCert:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return TestCert(
        cert.public_bytes(serialization.Encoding.PEM),
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )


@dataclass
class Received:
    mail_from: str
    rcpt_to: list[str]
    data: bytes  # CRLF line endings, dot-stuffing undone


@dataclass
class FakeSmtp:
    """The server's state and switches; `start()` begins listening on 127.0.0.1."""

    cert: TestCert
    offer_starttls: bool = True
    refuse_auth: bool = False
    refuse_recipients: set[str] = field(default_factory=set)  # lower-cased addresses
    drop_after_data: bool = False
    file_into: FakeServer | None = None  # file each accepted message in its Sent Messages
    commands: list[tuple[str, bool]] = field(default_factory=list)  # (verb, under TLS)
    violations: list[str] = field(default_factory=list)
    received: list[Received] = field(default_factory=list)
    connections: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def start(self) -> None:
        fake = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                _Session(fake, self).run()

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True

        self._server = Server(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def endpoint(self, context: ssl.SSLContext | None = None) -> Endpoint:
        """Where the client connects; by default it trusts the test certificate."""
        ctx = context if context is not None else self.cert.client_context()
        return Endpoint("localhost", self.port, ctx)

    def verbs(self) -> list[str]:
        return [verb for verb, _ in self.commands]


class _Session:
    def __init__(self, fake: FakeSmtp, handler: socketserver.StreamRequestHandler):
        self.fake = fake
        self.h = handler
        self.tls = False
        self.authed = False
        self.mail_from: str | None = None
        self.rcpt: list[str] = []

    def send(self, line: str) -> None:
        self.h.wfile.write(line.encode() + b"\r\n")
        self.h.wfile.flush()

    def run(self) -> None:
        with self.fake.lock:
            self.fake.connections += 1
        self.send("220 localhost ESMTP fake")
        with contextlib.suppress(OSError, ssl.SSLError):
            while True:
                raw = self.h.rfile.readline()
                if not raw:
                    return
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                verb, _, arg = line.partition(" ")
                verb = verb.upper()
                with self.fake.lock:
                    self.fake.commands.append((verb, self.tls))
                if not self.dispatch(verb, arg):
                    return

    def needs_tls(self, verb: str) -> bool:
        if self.tls:
            return False
        with self.fake.lock:
            self.fake.violations.append(f"{verb} before STARTTLS")
        self.send("530 5.7.0 Must issue a STARTTLS command first")
        return True

    def dispatch(self, verb: str, arg: str) -> bool:
        fake = self.fake
        if verb in ("EHLO", "HELO"):
            ext = []
            if not self.tls and fake.offer_starttls:
                ext.append("STARTTLS")
            if self.tls:
                ext.append("AUTH PLAIN")
            ext.append("8BITMIME")
            lines = ["localhost", *ext]
            for i, text in enumerate(lines):
                self.send(f"250{'-' if i < len(lines) - 1 else ' '}{text}")
        elif verb == "STARTTLS":
            if self.tls or not fake.offer_starttls:
                self.send("502 5.5.1 Not supported")
                return True
            self.send("220 2.0.0 Ready to start TLS")
            ctx = fake.cert.server_context()
            try:
                conn = ctx.wrap_socket(self.h.connection, server_side=True)
            except ssl.SSLError, OSError:
                return False  # the client refused the certificate
            self.h.connection = conn
            self.h.rfile = conn.makefile("rb")
            self.h.wfile = conn.makefile("wb")
            self.tls = True
        elif verb == "AUTH":
            if self.needs_tls(verb):
                return True
            mech, _, initial = arg.partition(" ")
            if mech.upper() != "PLAIN" or not initial:
                self.send("504 5.5.4 Unrecognized authentication type")
                return True
            parts = base64.b64decode(initial).split(b"\0")
            ok = parts[1:] == [USER.encode(), PASSWORD.encode()] and not fake.refuse_auth
            if ok:
                self.authed = True
                self.send("235 2.7.0 Authentication successful")
            else:
                self.send("535 5.7.8 Authentication credentials invalid")
        elif verb == "MAIL":
            if self.needs_tls(verb):
                return True
            if not self.authed:
                self.send("530 5.7.0 Authentication required")
                return True
            self.mail_from = _path(arg, "FROM:")
            self.rcpt = []
            self.send("250 2.1.0 OK")
        elif verb == "RCPT":
            if self.needs_tls(verb):
                return True
            if self.mail_from is None:
                self.send("503 5.5.1 Need MAIL first")
                return True
            addr = _path(arg, "TO:")
            if addr.lower() in fake.refuse_recipients:
                self.send(f"550 5.1.1 <{addr}> mailbox unavailable")
            else:
                self.rcpt.append(addr)
                self.send("250 2.1.5 OK")
        elif verb == "DATA":
            if self.needs_tls(verb):
                return True
            if self.mail_from is None or not self.rcpt:
                self.send("503 5.5.1 Need RCPT first")
                return True
            self.send("354 End data with <CR><LF>.<CR><LF>")
            data = self.read_data()
            if fake.drop_after_data:  # no reply: the client cannot tell whether it was sent
                self.h.connection.shutdown(socket.SHUT_RDWR)
                return False
            with fake.lock:
                fake.received.append(Received(self.mail_from, list(self.rcpt), data))
            if fake.file_into is not None:
                uid = fake.file_into.deliver("Sent Messages", data)
                fake.file_into.folders["Sent Messages"].messages[uid].flags = {"\\Seen"}
            self.mail_from, self.rcpt = None, []
            self.send("250 2.0.0 OK queued")
        elif verb == "RSET":
            self.mail_from, self.rcpt = None, []
            self.send("250 2.0.0 OK")
        elif verb == "NOOP":
            self.send("250 2.0.0 OK")
        elif verb == "QUIT":
            self.send("221 2.0.0 Bye")
            return False
        else:
            self.send("502 5.5.2 Command not recognized")
        return True

    def read_data(self) -> bytes:
        lines = []
        while True:
            raw = self.h.rfile.readline()
            if not raw or raw == b".\r\n":
                break
            if raw.startswith(b".."):
                raw = raw[1:]
            lines.append(raw)
        return b"".join(lines)


def _path(arg: str, prefix: str) -> str:
    rest = arg[len(prefix) :] if arg.upper().startswith(prefix) else arg
    addr = rest.strip().split(" ")[0]
    return addr.strip("<>")


def free_port() -> int:
    """A local port nothing listens on (for connection-refused tests)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
