# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

"""Opaque event ids: urlsafe base64 of 'calendar_id|start|resource path or uid:<UID>'.

`start` is the occurrence's DTSTART as output by calendar_events (ISO date or datetime as
written in the event), which tells occurrences of one recurring resource apart.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass


class BadEventId(ValueError):
    pass


@dataclass(frozen=True)
class EventId:
    calendar_id: str
    start: str
    path: str  # CalDAV resource path ("/..."), or "uid:<UID>" for a subscribed feed

    def encode(self) -> str:
        raw = f"{self.calendar_id}|{self.start}|{self.path}".encode()
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_event_id(value: str) -> EventId:
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("utf-8")
        calendar_id, start, path = raw.split("|", 2)
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise BadEventId("malformed event id") from exc
    if not calendar_id or not start or not (path.startswith("/") or path.startswith("uid:")):
        raise BadEventId("malformed event id")
    return EventId(calendar_id, start, path)
