# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

"""IMAP modified UTF-7 (RFC 3501 §5.1.3) for mailbox names."""

from __future__ import annotations

import base64


def _b64_encode(chunk: str) -> str:
    raw = chunk.encode("utf-16-be")
    return "&" + base64.b64encode(raw).decode("ascii").rstrip("=").replace("/", ",") + "-"


def encode(name: str) -> str:
    out: list[str] = []
    pending: list[str] = []
    for ch in name:
        if 0x20 <= ord(ch) <= 0x7E:
            if pending:
                out.append(_b64_encode("".join(pending)))
                pending = []
            out.append("&-" if ch == "&" else ch)
        else:
            pending.append(ch)
    if pending:
        out.append(_b64_encode("".join(pending)))
    return "".join(out)


def decode(name: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(name):
        ch = name[i]
        if ch != "&":
            out.append(ch)
            i += 1
            continue
        end = name.find("-", i)
        if end == -1:  # malformed; keep the rest verbatim
            out.append(name[i:])
            break
        chunk = name[i + 1 : end]
        if not chunk:
            out.append("&")
        else:
            b64 = chunk.replace(",", "/")
            b64 += "=" * (-len(b64) % 4)
            out.append(base64.b64decode(b64).decode("utf-16-be"))
        i = end + 1
    return "".join(out)
