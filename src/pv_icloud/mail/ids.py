# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

"""Opaque mail ids: urlsafe base64 of 'folder|UIDVALIDITY|UID'."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass


class BadMailId(ValueError):
    pass


@dataclass(frozen=True)
class MailId:
    folder: str
    uidvalidity: int
    uid: int

    def encode(self) -> str:
        raw = f"{self.folder}|{self.uidvalidity}|{self.uid}".encode()
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_id(value: str) -> MailId:
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("utf-8")
        # Folder names may contain '|'; the two numeric fields are always last.
        folder, uidvalidity, uid = raw.rsplit("|", 2)
        return MailId(folder, int(uidvalidity), int(uid))
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise BadMailId("malformed mail id") from exc
