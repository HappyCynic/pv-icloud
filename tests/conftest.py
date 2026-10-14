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
    alice_tokens=(),
    alice_write=None,
    default_write=None,
    mail_write=False,
    calendar_write=False,
    default_calendar=None,
    contacts_write=False,
):
    """Accounts alice and bob. `alice_tokens` adds tokens to alice's (label -> extra keys,
    hashed from `<label>-token`); `mail_write` sets PV_ICLOUD_MAIL_WRITE=enabled,
    `calendar_write` PV_ICLOUD_CALENDAR_WRITE=enabled and `contacts_write`
    PV_ICLOUD_CONTACTS_WRITE=enabled."""
    raw = {
        "defaults": {
            "mail": {
                "folders": {"include": "*", "exclude": []},
                "max_age_days": max_age_days,
                **({"write": default_write} if default_write is not None else {}),
            },
            **({"calendar": default_calendar} if default_calendar is not None else {}),
        },
        "accounts": [
            {
                "id": "alice",
                "tokens": [
                    {"label": "laptop", "sha256": sha(TOKENS["alice"])},
                    *(
                        {"label": label, "sha256": sha(f"{label}-token"), **extra}
                        for label, extra in alice_tokens
                    ),
                ],
                "mail": {
                    "folders": {"exclude": list(alice_exclude)},
                    **({"special_folders": alice_special} if alice_special is not None else {}),
                    **({"write": alice_write} if alice_write is not None else {}),
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
        **({"PV_ICLOUD_MAIL_WRITE": "enabled"} if mail_write else {}),
        **({"PV_ICLOUD_CALENDAR_WRITE": "enabled"} if calendar_write else {}),
        **({"PV_ICLOUD_CONTACTS_WRITE": "enabled"} if contacts_write else {}),
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

    def __init__(self, server: FakeServer, account, data_dir, *, write=False, smtp=None):
        """`smtp`: a FakeSmtp that mail_send delivers to (needs `write`)."""
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
        writer = sender = None
        if write:
            from pv_icloud.mail.imap_write import MailWriter

            writer = MailWriter(self.pool)
        if smtp is not None:
            from pv_icloud.mail.smtp import SmtpSender

            sender = SmtpSender(account.icloud_user, account.app_password, smtp.endpoint)
        self.service = MailService(
            account, self.index, self.pool, self.sync, writer=writer, sender=sender
        )
        self._client = None

    def run_sync(self) -> int:
        if self._client is None:
            self._client = self.sync._client_factory()
        return self.sync.full_pass(self._client)


@pytest.fixture
def harness(server, tmp_path):
    cfg = make_config(tmp_path)
    return Harness(server, cfg.account("alice"), tmp_path)


@pytest.fixture(scope="session")
def test_cert():
    from .fakesmtp import make_cert

    return make_cert()


@pytest.fixture
def smtp(test_cert):
    """A running fake SMTP server; the test fails if anything was sent before STARTTLS."""
    from .fakesmtp import FakeSmtp

    fake = FakeSmtp(test_cert)
    fake.start()
    yield fake
    fake.stop()
    assert fake.violations == []
