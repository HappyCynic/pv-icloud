# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

"""Sending mail for mail_send (ADR-0004, add-mail-send): the only module that imports smtplib.

Imported only when PV_ICLOUD_MAIL_WRITE=enabled, and used only by a granted mail_send call.
One message per connection, never retried:

    EHLO, STARTTLS (certificate and host name verified), EHLO, AUTH,
    MAIL FROM, RCPT TO (each recipient), DATA, QUIT

Nothing that can carry a credential or the message leaves the process before STARTTLS has
succeeded; a server that does not offer STARTTLS gets nothing more. Errors never include the
server's reply text or an address (replies can echo addresses).
"""

from __future__ import annotations

import contextlib
import logging
import re
import smtplib
import ssl
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from ..errors import WriteFailed

log = logging.getLogger(__name__)

SMTP_HOST = "smtp.mail.me.com"
SMTP_PORT = 587
TIMEOUT = 30.0

NOT_SENT = "mail was not sent: {}"
MAY_HAVE_BEEN_SENT = "mail may have been sent; check Sent Messages before trying again"
_BCC_LINE = re.compile(rb"^bcc[ \t]*:", re.IGNORECASE | re.MULTILINE)


@dataclass(frozen=True)
class Endpoint:
    """Where to connect, and the context STARTTLS verifies the server's certificate with."""

    host: str
    port: int
    context: ssl.SSLContext


def default_endpoint() -> Endpoint:
    return Endpoint(SMTP_HOST, SMTP_PORT, ssl.create_default_context())


@dataclass(frozen=True)
class Sent:
    refused: tuple[str, ...]  # recipients the server refused while accepting others


def _header_has_bcc(message: bytes) -> bool:
    head = re.split(rb"\r?\n\r?\n", message, maxsplit=1)[0]
    return _BCC_LINE.search(head) is not None


class SmtpSender:
    """Sends one message per call as one account."""

    def __init__(
        self,
        user: str,
        password: str,
        endpoint: Callable[[], Endpoint] = default_endpoint,
    ):
        self._user = user
        self._password = password
        self._endpoint = endpoint

    def send(self, from_addr: str, to_addrs: Sequence[str], message: bytes) -> Sent:
        """Send `message` (bytes, no Bcc header) to the envelope recipients `to_addrs`.

        Raises WriteFailed: `mail was not sent: <reason>` when nothing can have been
        delivered, or MAY_HAVE_BEEN_SENT when the connection failed during DATA."""
        if not to_addrs:
            raise ValueError("no recipients")
        if _header_has_bcc(message):  # Bcc goes in the envelope only, never in the data
            raise ValueError("refusing to send a message with a Bcc header")
        ep = self._endpoint()
        ctx = ep.context
        if ctx.verify_mode != ssl.CERT_REQUIRED or not ctx.check_hostname:
            raise ValueError("SMTP TLS context must verify the certificate and host name")
        try:
            smtp = smtplib.SMTP(ep.host, ep.port, timeout=TIMEOUT)
        except (OSError, smtplib.SMTPException) as exc:
            raise _not_sent("could not connect", exc) from exc
        try:
            return self._session(smtp, ctx, from_addr, list(to_addrs), message)
        finally:
            smtp.close()

    def _session(
        self,
        smtp: smtplib.SMTP,
        ctx: ssl.SSLContext,
        from_addr: str,
        to_addrs: list[str],
        message: bytes,
    ) -> Sent:
        try:
            smtp.ehlo()
            if not smtp.has_extn("starttls"):
                raise _not_sent("secure connection failed", None)
            smtp.starttls(context=ctx)
            smtp.ehlo()
        except WriteFailed:
            raise
        except (OSError, smtplib.SMTPException) as exc:  # ssl.SSLError is an OSError
            raise _not_sent("secure connection failed", exc) from exc
        try:
            smtp.login(self._user, self._password)
        except smtplib.SMTPAuthenticationError as exc:
            raise _not_sent("iCloud rejected the credentials", exc) from exc
        except (OSError, smtplib.SMTPException) as exc:
            raise _not_sent("could not log in", exc) from exc
        try:
            code, _ = smtp.mail(from_addr)
            if code != 250:
                raise _not_sent("the sender address was refused", None)
            refused = []
            for addr in to_addrs:
                code, _ = smtp.rcpt(addr)
                if code not in (250, 251):
                    refused.append(addr)
        except WriteFailed:
            raise
        except (OSError, smtplib.SMTPException) as exc:
            raise _not_sent("the connection failed", exc) from exc
        if len(refused) == len(to_addrs):
            raise _not_sent("recipients refused", None)
        try:
            smtp.data(message)
        except smtplib.SMTPDataError as exc:  # a definite refusal, before or after the data
            raise _not_sent("the message was refused", exc) from exc
        except (OSError, smtplib.SMTPException) as exc:
            # The data may have reached the server before the connection failed.
            log.warning("SMTP connection failed during DATA (%s)", type(exc).__name__)
            raise WriteFailed(MAY_HAVE_BEEN_SENT) from exc
        with contextlib.suppress(OSError, smtplib.SMTPException):
            smtp.quit()  # a failure here is ignored: the server has accepted the message
        return Sent(tuple(refused))


def _not_sent(reason: str, exc: BaseException | None) -> WriteFailed:
    if exc is not None:  # the class only: the text can echo addresses
        log.warning("SMTP: %s (%s)", reason, type(exc).__name__)
    return WriteFailed(NOT_SENT.format(reason))
