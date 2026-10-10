# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

"""RFC 5322 message -> plain fields. Tolerant: bad headers or parts degrade, never raise."""

from __future__ import annotations

import email
import email.header
import email.utils
import re
from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage, MIMEPart

from .htmltext import html_to_text

_WS = re.compile(r"\s+")
_MSGID = re.compile(r"<[^<>\s]+>")


@dataclass
class Attachment:
    filename: str | None
    content_type: str
    size: int | None


@dataclass
class ParsedMessage:
    message_id: str | None
    in_reply_to: str | None
    references: list[str]
    date: float | None  # epoch seconds, UTC
    date_header: str | None
    from_: str
    to: str
    cc: str
    subject: str
    body: str
    attachments: list[Attachment] = field(default_factory=list)
    has_attachments: bool = False
    truncated: bool = False


def _decode_header(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        value = str(email.header.make_header(email.header.decode_header(raw)))
    except Exception:
        value = raw
    return _WS.sub(" ", value).strip()


def _addresses(raw: str | None) -> str:
    """Decoded 'Name <addr>, ...'. getaddresses handles quoted commas in display names."""
    if not raw:
        return ""
    try:
        pairs = email.utils.getaddresses([raw])
    except Exception:
        return _decode_header(raw)
    out = []
    for name, addr in pairs:
        name = _decode_header(name)
        if name and addr:
            out.append(f"{name} <{addr}>")
        elif addr or name:
            out.append(addr or name)
    return ", ".join(out)


# One entry of a stored address list: a bare address, or "Display Name <address>" where the
# name may contain commas (the stored form is decoded, so its quotes are gone).
_STORED_ADDR = re.compile(
    r"\s*(?:([^\s,<>]+@[^\s,<>]+)|(.*?)\s*<([^<>]*)>|[^,]*)\s*(?:,|$)"  # last: name only
)


def split_addresses(stored: str) -> list[tuple[str, str]]:
    """(display name, address) pairs from a list stored by `_addresses`."""
    out: list[tuple[str, str]] = []
    pos = 0
    while pos < len(stored):
        m = _STORED_ADDR.match(stored, pos)
        if not m or m.end() == pos:
            break
        bare, name, addr = m.groups()
        if bare or addr:
            out.append(((name or "").strip(), (bare or addr).strip()))
        pos = m.end()
    return out


def _msgids(raw: str | None) -> list[str]:
    return _MSGID.findall(raw or "")


def _part_text(part: MIMEPart) -> str:
    try:
        content = part.get_content()
        if isinstance(content, str):
            return content
    except Exception:
        pass
    payload = part.get_payload(decode=True)
    if not isinstance(payload, bytes):
        payload = b""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _body(msg: EmailMessage) -> str:
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
    except Exception:
        part = None
    if part is None:
        if msg.get_content_maintype() == "text":
            part = msg
        else:
            return ""
    text = _part_text(part)
    if part.get_content_subtype() == "html":
        text = html_to_text(text)
    return text.replace("\r\n", "\n").strip()


def _attachments(msg: EmailMessage) -> list[Attachment]:
    out: list[Attachment] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        try:
            disposition = part.get_content_disposition()
            filename = part.get_filename()
        except Exception:
            disposition, filename = None, None
        if disposition != "attachment" and not (filename and disposition != "inline"):
            continue
        try:
            payload = part.get_payload(decode=True)
            size = len(payload) if payload is not None else None
        except Exception:
            size = None
        out.append(
            Attachment(
                filename=_decode_header(filename) or None,
                content_type=part.get_content_type(),
                size=size,
            )
        )
    return out


def parse_message(
    raw: bytes, *, truncated: bool = False, fallback_date: float | None = None
) -> ParsedMessage:
    """`fallback_date` (the server's INTERNALDATE) is used when the Date header is missing
    or unparseable, so the message still falls inside date-filtered searches."""
    msg: EmailMessage = email.message_from_bytes(raw, policy=policy.default)
    headers = {k.lower(): v for k, v in reversed(list(msg.raw_items()))}  # first occurrence wins

    def h(name: str) -> str | None:
        value = headers.get(name)
        return str(value) if value is not None else None

    date_header = h("date")
    date = None
    if date_header:
        try:
            date = email.utils.parsedate_to_datetime(date_header).timestamp()
        except Exception:
            date = None
    if date is None:
        date = fallback_date

    message_ids = _msgids(h("message-id"))
    in_reply_to = _msgids(h("in-reply-to"))
    try:
        body = _body(msg)
    except Exception:
        body = ""
    try:
        attachments = _attachments(msg)
    except Exception:
        attachments = []
    has_attachments = bool(attachments)
    if truncated and not has_attachments and msg.get_content_type() == "multipart/mixed":
        # Partial fetch cut the message before its attachment parts.
        has_attachments = True

    return ParsedMessage(
        message_id=message_ids[0] if message_ids else None,
        in_reply_to=in_reply_to[0] if in_reply_to else None,
        references=_msgids(h("references")),
        date=date,
        date_header=_decode_header(date_header) or None,
        from_=_addresses(h("from")),
        to=_addresses(h("to")),
        cc=_addresses(h("cc")),
        subject=_decode_header(h("subject")),
        body=body,
        attachments=attachments,
        has_attachments=has_attachments,
        truncated=truncated,
    )


SNIPPET_MAX_CHARS = 300


def snippet(text: str, limit: int = SNIPPET_MAX_CHARS) -> str:
    text = _WS.sub(" ", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
