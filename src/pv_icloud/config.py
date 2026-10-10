# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

"""Load config/pv-icloud.yaml plus per-account credentials from the environment.

The file is per deployment and gitignored; config/pv-icloud.example.yaml is the template.

See docs/adr/0003-multi-account.md for the shape and the env var convention.
"""

from __future__ import annotations

import fnmatch
import os
import re
import zoneinfo
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ACCOUNT_ID_RE = re.compile(r"^[a-z0-9_]+$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# iCloud system folders, hidden unless `mail.special_folders` turns them on.
# Config key -> folder display name. When hidden they are hidden whatever include says.
SPECIAL_FOLDERS = {"junk": "Junk", "deleted_messages": "Deleted Messages", "drafts": "Drafts"}


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Scope:
    """Include/exclude filter over display names. include is "*" or a list of names/globs."""

    include: str | tuple[str, ...] = "*"
    exclude: tuple[str, ...] = ()
    hidden: frozenset[str] = frozenset()

    def allows(self, name: str) -> bool:
        if name in self.hidden:
            return False
        if any(fnmatch.fnmatchcase(name, pat) for pat in self.exclude):
            return False
        if self.include == "*":
            return True
        return any(fnmatch.fnmatchcase(name, pat) for pat in self.include)


@dataclass(frozen=True)
class MailConfig:
    folders: Scope  # its `hidden` holds the special folders not turned on
    max_age_days: int = 730
    max_connections: int = 2


@dataclass(frozen=True)
class CalendarConfig:
    calendars: Scope
    max_range_days: int = 90
    timezone: str | None = None  # IANA zone for date-only ranges; default: from iCloud
    subscriptions: bool = True  # fetch subscribed (webcal) feeds from their publishers
    max_search_days: int = 730  # longest calendar_search range


@dataclass(frozen=True)
class ContactsConfig:
    enabled: bool = True
    # Contacts in any of these groups (names, globs allowed) are hidden everywhere.
    exclude_groups: tuple[str, ...] = ()

    def hides_group(self, name: str) -> bool:
        folded = name.casefold()
        return any(fnmatch.fnmatchcase(folded, pat.casefold()) for pat in self.exclude_groups)


@dataclass(frozen=True)
class TokenEntry:
    label: str
    sha256: str


@dataclass(frozen=True)
class AccountConfig:
    id: str
    tokens: tuple[TokenEntry, ...]
    mail: MailConfig
    calendar: CalendarConfig
    contacts: ContactsConfig
    icloud_user: str = field(repr=False)
    app_password: str = field(repr=False)


@dataclass(frozen=True)
class Config:
    accounts: tuple[AccountConfig, ...]
    data_dir: Path

    def account(self, account_id: str) -> AccountConfig:
        for acct in self.accounts:
            if acct.id == account_id:
                return acct
        raise KeyError(account_id)


def _merge(base: Mapping[str, Any], override: Mapping[str, Any] | None) -> dict[str, Any]:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, Mapping) and isinstance(out.get(key), Mapping):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _scope(raw: Mapping[str, Any] | None, *, hidden: frozenset[str], where: str) -> Scope:
    raw = raw or {}
    include = raw.get("include", "*")
    exclude = raw.get("exclude") or []
    if include != "*":
        if not isinstance(include, list) or not all(isinstance(x, str) for x in include):
            raise ConfigError(f'{where}.include must be "*" or a list of names')
        include = tuple(include)
    if not isinstance(exclude, list) or not all(isinstance(x, str) for x in exclude):
        raise ConfigError(f"{where}.exclude must be a list of names")
    return Scope(include=include, exclude=tuple(exclude), hidden=hidden)


def _hidden_special(raw: Any, where: str) -> frozenset[str]:
    """Special folders left hidden: every one not set to true (default: all hidden)."""
    raw = raw or {}
    if not isinstance(raw, Mapping):
        raise ConfigError(f"{where} must be a mapping of folder: true/false")
    unknown = set(raw) - set(SPECIAL_FOLDERS)
    if unknown:
        known = ", ".join(SPECIAL_FOLDERS)
        raise ConfigError(f"{where}: unknown key(s) {', '.join(sorted(unknown))}; use {known}")
    return frozenset(
        name
        for key, name in SPECIAL_FOLDERS.items()
        if not _bool(raw.get(key, False), f"{where}.{key}")
    )


def _positive_int(value: Any, where: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ConfigError(f"{where} must be a positive integer")
    return value


def _bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{where} must be true or false")
    return value


def _timezone(value: Any, where: str) -> str | None:
    if value is None:
        return None
    try:
        zoneinfo.ZoneInfo(str(value))
    except (zoneinfo.ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"{where}: unknown time zone {value!r}") from exc
    return str(value)


def parse_config(raw: Mapping[str, Any], env: Mapping[str, str], data_dir: Path) -> Config:
    defaults = raw.get("defaults") or {}
    accounts_raw = raw.get("accounts") or []
    if not isinstance(accounts_raw, list) or not accounts_raw:
        raise ConfigError("config must list at least one account")

    seen_ids: set[str] = set()
    seen_hashes: set[str] = set()
    accounts: list[AccountConfig] = []
    for i, acct in enumerate(accounts_raw):
        where = f"accounts[{i}]"
        acct_id = acct.get("id")
        if not isinstance(acct_id, str) or not ACCOUNT_ID_RE.match(acct_id):
            raise ConfigError(f"{where}.id must match [a-z0-9_]+")
        if acct_id in seen_ids:
            raise ConfigError(f"duplicate account id {acct_id!r}")
        seen_ids.add(acct_id)
        where = f"account {acct_id}"

        tokens: list[TokenEntry] = []
        for t in acct.get("tokens") or []:
            label, digest = t.get("label"), str(t.get("sha256", "")).lower()
            if not isinstance(label, str) or not label:
                raise ConfigError(f"{where}: every token needs a label")
            if not SHA256_RE.match(digest):
                raise ConfigError(f"{where}: token {label!r} sha256 must be 64 hex chars")
            if digest in seen_hashes:
                raise ConfigError(f"{where}: token {label!r} hash is used by another token")
            seen_hashes.add(digest)
            tokens.append(TokenEntry(label=label, sha256=digest))
        if not tokens:
            raise ConfigError(f"{where}: at least one token is required")

        mail_raw = _merge(defaults.get("mail") or {}, acct.get("mail"))
        cal_raw = _merge(defaults.get("calendar") or {}, acct.get("calendar"))
        contacts_raw = _merge(defaults.get("contacts") or {}, acct.get("contacts"))
        mail = MailConfig(
            folders=_scope(
                mail_raw.get("folders"),
                hidden=_hidden_special(
                    mail_raw.get("special_folders"), f"{where}.mail.special_folders"
                ),
                where=f"{where}.mail.folders",
            ),
            max_age_days=_positive_int(mail_raw.get("max_age_days", 730), f"{where}.max_age_days"),
            max_connections=_positive_int(
                mail_raw.get("max_connections", 2), f"{where}.max_connections"
            ),
        )
        if mail.max_connections < 2:
            # One connection is reserved for the sync worker (ADR-0003).
            raise ConfigError(f"{where}.mail.max_connections must be at least 2")
        calendar = CalendarConfig(
            calendars=_scope(
                cal_raw.get("calendars"), hidden=frozenset(), where=f"{where}.calendar.calendars"
            ),
            max_range_days=_positive_int(
                cal_raw.get("max_range_days", 90), f"{where}.calendar.max_range_days"
            ),
            timezone=_timezone(cal_raw.get("timezone"), f"{where}.calendar.timezone"),
            subscriptions=_bool(
                cal_raw.get("subscriptions", True), f"{where}.calendar.subscriptions"
            ),
            max_search_days=_positive_int(
                cal_raw.get("max_search_days", 730), f"{where}.calendar.max_search_days"
            ),
        )
        exclude_groups = contacts_raw.get("exclude_groups") or []
        if not isinstance(exclude_groups, list) or not all(
            isinstance(x, str) for x in exclude_groups
        ):
            raise ConfigError(f"{where}.contacts.exclude_groups must be a list of group names")
        contacts = ContactsConfig(
            enabled=_bool(contacts_raw.get("enabled", True), f"{where}.contacts.enabled"),
            exclude_groups=tuple(exclude_groups),
        )

        prefix = f"PV_ICLOUD_{acct_id.upper()}_"
        user = env.get(prefix + "ICLOUD_USER", "").strip()
        password = env.get(prefix + "APP_PASSWORD", "").strip()
        if not user or not password:
            raise ConfigError(
                f"{where}: set {prefix}ICLOUD_USER and {prefix}APP_PASSWORD in the environment"
            )
        accounts.append(
            AccountConfig(
                id=acct_id,
                tokens=tuple(tokens),
                mail=mail,
                calendar=calendar,
                contacts=contacts,
                icloud_user=user,
                app_password=password,
            )
        )
    return Config(accounts=tuple(accounts), data_dir=data_dir)


def load_config(
    path: str | os.PathLike[str] | None = None, env: Mapping[str, str] | None = None
) -> Config:
    env = os.environ if env is None else env
    path = Path(path or env.get("PV_ICLOUD_CONFIG", "config/pv-icloud.yaml"))
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    data_dir = Path(env.get("PV_ICLOUD_DATA_DIR", "data"))
    return parse_config(raw, env, data_dir)
