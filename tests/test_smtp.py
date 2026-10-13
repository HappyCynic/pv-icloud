# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

"""mail/smtp.py against the in-process fake SMTP server (add-mail-send)."""

from __future__ import annotations

import logging
import smtplib
import ssl

import pytest

from pv_icloud.errors import WriteFailed
from pv_icloud.mail.smtp import (
    SMTP_HOST,
    SMTP_PORT,
    TIMEOUT,
    Endpoint,
    SmtpSender,
    default_endpoint,
)

from .fakesmtp import PASSWORD, USER, free_port

MESSAGE = b"From: me@example.com\r\nTo: a@example.com\r\nSubject: Hi\r\n\r\nHello\r\n.dot\r\n"


def sender(smtp, *, password=PASSWORD, context=None) -> SmtpSender:
    return SmtpSender(USER, password, lambda: smtp.endpoint(context))


def test_defaults_are_icloud_with_verification():
    ep = default_endpoint()
    assert (ep.host, ep.port, SMTP_HOST, SMTP_PORT, TIMEOUT) == (
        "smtp.mail.me.com", 587, "smtp.mail.me.com", 587, 30.0
    )  # fmt: skip
    assert ep.context.verify_mode == ssl.CERT_REQUIRED and ep.context.check_hostname


def test_sends_over_starttls(smtp):
    out = sender(smtp).send("me@example.com", ["a@example.com", "b@example.org"], MESSAGE)
    assert out.refused == ()
    [got] = smtp.received
    assert got.mail_from == "me@example.com"
    assert got.rcpt_to == ["a@example.com", "b@example.org"]
    assert got.data == MESSAGE  # dot-stuffing undone by the server
    assert smtp.verbs() == [
        "EHLO", "STARTTLS", "EHLO", "AUTH", "MAIL", "RCPT", "RCPT", "DATA", "QUIT",
    ]  # fmt: skip
    assert [tls for verb, tls in smtp.commands if verb in ("AUTH", "MAIL", "DATA")] == [True] * 3


def test_starttls_not_offered_stops_before_auth(smtp):
    smtp.offer_starttls = False
    with pytest.raises(WriteFailed, match="^mail was not sent: secure connection failed$"):
        sender(smtp).send("me@example.com", ["a@example.com"], MESSAGE)
    assert smtp.verbs() == ["EHLO"]
    assert smtp.received == []


def test_untrusted_certificate_stops_before_auth(smtp):
    # The default context does not trust the test certificate, as for an impostor server.
    with pytest.raises(WriteFailed, match="^mail was not sent: secure connection failed$"):
        sender(smtp, context=ssl.create_default_context()).send(
            "me@example.com", ["a@example.com"], MESSAGE
        )
    assert "AUTH" not in smtp.verbs() and "MAIL" not in smtp.verbs()
    assert smtp.received == []


def test_wrong_host_name_stops_before_auth(smtp, test_cert):
    ep = Endpoint("127.0.0.2", smtp.port, test_cert.client_context())  # cert is for localhost
    with pytest.raises(WriteFailed, match="not sent"):
        SmtpSender(USER, PASSWORD, lambda: ep).send("me@example.com", ["a@example.com"], MESSAGE)
    assert "AUTH" not in smtp.verbs()


@pytest.mark.parametrize(
    "weaken",
    [
        lambda ctx: setattr(ctx, "check_hostname", False),
        lambda ctx: (setattr(ctx, "check_hostname", False), setattr(ctx, "verify_mode", 0)),
    ],
)
def test_a_context_without_verification_is_refused(smtp, test_cert, weaken):
    ctx = test_cert.client_context()
    weaken(ctx)
    with pytest.raises(ValueError, match="must verify"):
        sender(smtp, context=ctx).send("me@example.com", ["a@example.com"], MESSAGE)
    assert smtp.connections == 0


def test_rejected_credentials(smtp):
    with pytest.raises(WriteFailed, match="^mail was not sent: iCloud rejected the credentials$"):
        sender(smtp, password="wrong").send("me@example.com", ["a@example.com"], MESSAGE)
    assert "MAIL" not in smtp.verbs()


def test_cannot_connect():
    ep = Endpoint("127.0.0.1", free_port(), ssl.create_default_context())
    with pytest.raises(WriteFailed, match="^mail was not sent: could not connect$"):
        SmtpSender(USER, PASSWORD, lambda: ep).send("me@example.com", ["a@example.com"], MESSAGE)


def test_all_recipients_refused(smtp):
    smtp.refuse_recipients = {"a@example.com", "b@example.com"}
    with pytest.raises(WriteFailed, match="^mail was not sent: recipients refused$"):
        sender(smtp).send("me@example.com", ["a@example.com", "B@example.com"], MESSAGE)
    assert "DATA" not in smtp.verbs()


def test_some_recipients_refused_are_returned(smtp):
    smtp.refuse_recipients = {"b@example.com"}
    out = sender(smtp).send("me@example.com", ["a@example.com", "b@example.com"], MESSAGE)
    assert out.refused == ("b@example.com",)
    [got] = smtp.received
    assert got.rcpt_to == ["a@example.com"]


def test_dropped_after_data_may_have_been_sent(smtp):
    smtp.drop_after_data = True
    with pytest.raises(WriteFailed) as err:
        sender(smtp).send("me@example.com", ["a@example.com"], MESSAGE)
    assert str(err.value) == "mail may have been sent; check Sent Messages before trying again"


def test_bcc_header_is_never_sent(smtp):
    with pytest.raises(ValueError, match="Bcc"):
        sender(smtp).send(
            "me@example.com", ["a@example.com"], b"To: a@x.org\r\nbcc: b@x.org\r\n\r\nx"
        )
    assert smtp.connections == 0
    # "Bcc:" in the body is just text.
    sender(smtp).send("me@example.com", ["a@example.com"], b"To: a@x.org\r\n\r\nBcc: b@x.org\r\n")
    assert len(smtp.received) == 1


def test_errors_and_logs_carry_no_address_or_reply_text(smtp, caplog):
    smtp.refuse_recipients = {"secret@example.com"}
    with caplog.at_level(logging.DEBUG), pytest.raises(WriteFailed) as err:
        sender(smtp).send("me@example.com", ["secret@example.com"], MESSAGE)
    smtp.refuse_recipients = set()
    smtp.drop_after_data = True
    with caplog.at_level(logging.DEBUG), pytest.raises(WriteFailed) as err2:
        sender(smtp).send("me@example.com", ["secret@example.com"], MESSAGE)
    text = str(err.value) + str(err2.value) + caplog.text
    for secret in ("secret", "example.com", "mailbox unavailable", PASSWORD, "Hello"):
        assert secret not in text, secret


def test_quit_failure_after_acceptance_is_ignored(smtp, monkeypatch):
    def broken_quit(self):
        raise smtplib.SMTPServerDisconnected("gone")

    monkeypatch.setattr(smtplib.SMTP, "quit", broken_quit)
    out = sender(smtp).send("me@example.com", ["a@example.com"], MESSAGE)
    assert out.refused == () and len(smtp.received) == 1
