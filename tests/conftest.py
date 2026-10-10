# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib

import pytest

from pv_icloud.config import parse_config
from pv_icloud.mail.imap import ImapClient, ImapPool
from pv_icloud.mail.index import MailIndex
from pv_icloud.mail.service import MailService
from pv_icloud.mail.sync import MailSync

from .fakeimap import FakeServer

TOKENS = {"alice": "token-alice-laptop", "bob": "token-bob-laptop"}


def sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def make_config(
    tmp_path,
    *,
    alice_exclude=(),
    max_age_days=730,
    alice_calendar=None,
    alice_special=None,
    alice_contacts=None,
):
    raw = {
        "defaults": {
            "mail": {"folders": {"include": "*", "exclude": []}, "max_age_days": max_age_days},
        },
        "accounts": [
            {
                "id": "alice",
                "tokens": [{"label": "laptop", "sha256": sha(TOKENS["alice"])}],
                "mail": {
                    "folders": {"exclude": list(alice_exclude)},
                    **({"special_folders": alice_special} if alice_special is not None else {}),
                },
                "calendar": alice_calendar or {},
                "contacts": alice_contacts or {},
            },
            {"id": "bob", "tokens": [{"label": "laptop", "sha256": sha(TOKENS["bob"])}]},
        ],
    }
    env = {
        "PV_ICLOUD_ALICE_ICLOUD_USER": "alice@icloud.com",
        "PV_ICLOUD_ALICE_APP_PASSWORD": "app-pw",
        "PV_ICLOUD_BOB_ICLOUD_USER": "bob@icloud.com",
        "PV_ICLOUD_BOB_APP_PASSWORD": "app-pw",
    }
    return parse_config(raw, env, tmp_path / "data")


@pytest.fixture
def server():
    s = FakeServer()
    for name in ("INBOX", "Work", "Friends & Family", "Junk", "Drafts", "Deleted Messages"):
        s.folder(name)
    return s


class Harness:
    """One account wired to a FakeServer: index, sync, pool, service."""

    def __init__(self, server: FakeServer, account, data_dir):
        self.server = server
        self.account = account
        self.index = MailIndex(data_dir / account.id / "mail.sqlite3")
        factory = server.connect

        def sync_client():
            c = ImapClient(account.icloud_user, account.app_password, factory)
            c.connect()
            return c

        self.sync = MailSync(account, self.index, sync_client)
        self.pool = ImapPool(account.icloud_user, account.app_password, 1, factory)
        self.service = MailService(account, self.index, self.pool, self.sync)
        self._client = None

    def run_sync(self) -> int:
        if self._client is None:
            self._client = self.sync._client_factory()
        return self.sync.full_pass(self._client)


@pytest.fixture
def harness(server, tmp_path):
    cfg = make_config(tmp_path)
    return Harness(server, cfg.account("alice"), tmp_path)
