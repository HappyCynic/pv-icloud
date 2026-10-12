# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

"""Building a plain-text draft for mail_draft (add-mail-draft). No IMAP or network code.

check_draft() enforces the input limits stated in the tool description; build_draft() turns
checked input into the RFC 5322 bytes that mail/imap_write.py saves in Drafts.
"""

from __future__ import annotations

import datetime as dt
import email.utils
from collections.abc import Sequence
from dataclasses import dataclass, field
from email import policy
from email.headerregistry import Address
from email.message import EmailMessage

from ..errors import ToolInputError

MAX_ADDRESSES = 50  # across to, cc and bcc
MAX_SUBJECT_CHARS = 998  # RFC 5322's line limit
MAX_BODY_CHARS = 100_000
# Never make_msgid()'s default, which is this machine's host name.
MESSAGE_ID_DOMAIN = "pv-icloud.invalid"
NOTHING_TO_DRAFT = "nothing to draft"


@dataclass(frozen=True)
class Original:
    """The threading headers and subject of the message a draft replies to."""

    message_id: str | None
    in_reply_to: str | None
    references: list[str] = field(default_factory=list)
    subject: str | None = None


def _address(value: object) -> Address | None:
    """Exactly one address, `local@domain` or `Name <local@domain>`; None otherwise."""
    if not isinstance(value, str) or "\r" in value or "\n" in value or not value.strip():
        return None
    header = policy.SMTP.header_factory("To", value)
    if header.defects or len(header.groups) != 1:
        return None
    [group] = header.groups
    if group.display_name is not None or len(group.addresses) != 1:
        return None
    [addr] = group.addresses
    # The address itself must be ASCII (the display name may be anything): a plain RFC 5322
    # header cannot carry a non-ASCII local part or domain.
    ok = addr.username and addr.domain and addr.addr_spec.isascii()
    return addr if ok else None


def _addresses(field_name: str, values: Sequence[object]) -> list[Address]:
    out = []
    for i, value in enumerate(values):
        addr = _address(value)
        if addr is None:  # the position, never the value: it could be anything
            raise ToolInputError(f"{field_name}[{i}] is not a single valid address")
        out.append(addr)
    return out


def check_draft(
    *,
    to: Sequence[object],
    cc: Sequence[object],
    bcc: Sequence[object],
    subject: str | None,
    body: str,
    in_reply_to: str | None,
) -> None:
    """Raise ToolInputError naming the first limit the input breaks."""
    if len(to) + len(cc) + len(bcc) > MAX_ADDRESSES:
        raise ToolInputError(f"at most {MAX_ADDRESSES} addresses across to, cc and bcc")
    for name, values in (("to", to), ("cc", cc), ("bcc", bcc)):
        _addresses(name, values)
    if subject is not None and (
        len(subject) > MAX_SUBJECT_CHARS or "\r" in subject or "\n" in subject
    ):
        raise ToolInputError(
            f"subject must be at most {MAX_SUBJECT_CHARS} characters with no line breaks"
        )
    if len(body) > MAX_BODY_CHARS:
        raise ToolInputError(f"body must be at most {MAX_BODY_CHARS} characters")
    if not (to or cc or bcc or subject or body or in_reply_to):
        raise ToolInputError(NOTHING_TO_DRAFT)


def reply_subject(original: str | None) -> str:
    """`Re: ` + the original subject, unless it already starts with `Re:` in any case."""
    original = (original or "").strip()
    if not original:
        return "Re:"
    return original if original[:3].lower() == "re:" else f"Re: {original}"


def build_draft(
    *,
    to: Sequence[str],
    cc: Sequence[str],
    bcc: Sequence[str],
    subject: str | None,
    body: str,
    send_from: str | None = None,
    reply: Original | None = None,
    now: dt.datetime | None = None,
) -> bytes:
    """The draft as bytes with CRLF line endings. Input must have passed check_draft()."""
    msg = EmailMessage(policy=policy.SMTP)
    if send_from:
        msg["From"] = send_from
    for name, values in (("To", to), ("Cc", cc), ("Bcc", bcc)):
        if values:
            msg[name] = _addresses(name.lower(), values)
    if not subject and reply is not None:
        subject = reply_subject(reply.subject)
    if subject:
        msg["Subject"] = subject
    msg["Date"] = email.utils.format_datetime(now or dt.datetime.now(dt.UTC))
    msg["Message-ID"] = email.utils.make_msgid(domain=MESSAGE_ID_DOMAIN)
    if reply is not None and reply.message_id:
        parents = reply.references or ([reply.in_reply_to] if reply.in_reply_to else [])
        msg["In-Reply-To"] = reply.message_id
        msg["References"] = " ".join([*parents, reply.message_id])
    msg.set_content(body)
    return msg.as_bytes()
