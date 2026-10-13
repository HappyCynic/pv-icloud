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

# Mail writes (docs/adr/0004-opt-in-mail-write.md): off unless this variable is `enabled`.
MAIL_WRITE_ENV = "PV_ICLOUD_MAIL_WRITE"
MAIL_WRITE_ACTIONS = ("move", "archive", "delete", "draft", "send")
MAX_WRITES_PER_HOUR = 30
_MAIL_WRITE_KEYS = ("protected_folders", "archive_folder", "send_from")
# One bare address, local@domain: no display name, spaces, commas or angle brackets, and a
# domain with a dot.
_DOMAIN = r"[^@\s,;<>()\".]+(\.[^@\s,;<>()\".]+)+"
BARE_ADDRESS_RE = re.compile(rf"^[^@\s,;<>()\"]+@{_DOMAIN}$")
# A send_to entry is a bare address or `@` followed by a domain.
SEND_TO_DOMAIN_RE = re.compile(rf"^@{_DOMAIN}$")


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
class MailWriteConfig:
    """Per-account `mail.write` settings (ADR-0004); used only when writes are switched on."""

    # Defaults and account lists together: an account adds to the defaults, never replaces.
    protected_folders: tuple[str, ...] = ()
    archive_folder: str = "Archive"
    # From of drafts (unset: no From, Mail fills it in) and of sent mail (required for send).
    send_from: str | None = None


@dataclass(frozen=True)
class MailConfig:
    folders: Scope  # its `hidden` holds the special folders not turned on
    max_age_days: int = 730
    max_connections: int = 2
    write: MailWriteConfig = MailWriteConfig()


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
    mail_write: frozenset[str] = frozenset()  # granted actions from MAIL_WRITE_ACTIONS
    protected_folders: tuple[str, ...] = ()  # added to the account's protected folders
    max_writes_per_hour: int = MAX_WRITES_PER_HOUR
    # Recipients mail_send may reach: lower-cased addresses and `@domain` entries; None: any.
    send_to: tuple[str, ...] | None = None


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
    mail_write: bool = False  # PV_ICLOUD_MAIL_WRITE=enabled

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


def _names(value: Any, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(x, str) and x for x in value):
        raise ConfigError(f"{where} must be a list of folder names")
    return tuple(value)


def _mail_write_switch(env: Mapping[str, str]) -> bool:
    value = env.get(MAIL_WRITE_ENV, "").strip()
    if value not in ("", "disabled", "enabled"):
        raise ConfigError(f"{MAIL_WRITE_ENV} must be `enabled` or `disabled` (or unset)")
    return value == "enabled"


def _token(t: Mapping[str, Any], label: str, digest: str, where: str) -> TokenEntry:
    where = f"{where}: token {label!r}"
    actions = t.get("mail_write", [])
    if not isinstance(actions, list) or not all(isinstance(a, str) for a in actions):
        raise ConfigError(f"{where} mail_write must be a list of actions")
    unknown = [a for a in actions if a not in MAIL_WRITE_ACTIONS]
    if unknown:
        raise ConfigError(
            f"{where} mail_write: unknown action(s) {', '.join(unknown)}; "
            f"allowed: {', '.join(MAIL_WRITE_ACTIONS)}"
        )
    return TokenEntry(
        label=label,
        sha256=digest,
        mail_write=frozenset(actions),
        protected_folders=_names(t.get("protected_folders", []), f"{where} protected_folders"),
        max_writes_per_hour=_positive_int(
            t.get("max_writes_per_hour", MAX_WRITES_PER_HOUR), f"{where} max_writes_per_hour"
        ),
        send_to=_send_to(t, "send" in actions, where),
    )


def _send_to(t: Mapping[str, Any], send: bool, where: str) -> tuple[str, ...] | None:
    if "send_to" not in t:
        return None
    if not send:
        raise ConfigError(f"{where} send_to needs the send action in mail_write")
    value = t["send_to"]
    if (
        not isinstance(value, list)
        or not value
        or not all(
            isinstance(x, str) and (BARE_ADDRESS_RE.match(x) or SEND_TO_DOMAIN_RE.match(x))
            for x in value
        )
    ):
        raise ConfigError(
            f"{where} send_to must be a non-empty list of bare addresses (local@domain) "
            "or @domain entries"
        )
    return tuple(dict.fromkeys(x.lower() for x in value))


def _write_section(raw: Any, where: str) -> Mapping[str, Any]:
    raw = raw or {}
    if not isinstance(raw, Mapping):
        raise ConfigError(f"{where} must be a mapping")
    unknown = set(raw) - set(_MAIL_WRITE_KEYS)
    if unknown:
        known = ", ".join(_MAIL_WRITE_KEYS)
        raise ConfigError(f"{where}: unknown key(s) {', '.join(sorted(unknown))}; use {known}")
    return raw


def _mail_write(defaults: Any, account: Any, where: str) -> MailWriteConfig:
    base = _write_section(defaults, "defaults.mail.write")
    own = _write_section(account, f"{where}.mail.write")
    protected = _names(base.get("protected_folders", []), "defaults.mail.write.protected_folders")
    protected += _names(own.get("protected_folders", []), f"{where}.mail.write.protected_folders")
    archive = own.get("archive_folder", base.get("archive_folder", "Archive"))
    if not isinstance(archive, str) or not archive:
        raise ConfigError(f"{where}.mail.write.archive_folder must be a folder name")
    send_from = own.get("send_from", base.get("send_from"))
    if send_from is not None and (
        not isinstance(send_from, str) or not BARE_ADDRESS_RE.match(send_from)
    ):
        raise ConfigError(f"{where}.mail.write.send_from must be one bare address (local@domain)")
    return MailWriteConfig(tuple(dict.fromkeys(protected)), archive, send_from)


def parse_config(raw: Mapping[str, Any], env: Mapping[str, str], data_dir: Path) -> Config:
    mail_write = _mail_write_switch(env)
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
            token = _token(t, label, digest, where)
            if token.mail_write and not mail_write:
                raise ConfigError(
                    f"{where}: token {label!r} has mail_write but {MAIL_WRITE_ENV} is not "
                    "`enabled`; set it or remove the grant"
                )
            tokens.append(token)
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
            write=_mail_write(
                (defaults.get("mail") or {}).get("write"),
                (acct.get("mail") or {}).get("write"),
                where,
            ),
        )
        if mail.max_connections < 2:
            # One connection is reserved for the sync worker (ADR-0003).
            raise ConfigError(f"{where}.mail.max_connections must be at least 2")
        for token in tokens:
            if "send" in token.mail_write and mail.write.send_from is None:
                raise ConfigError(
                    f"{where}: token {token.label!r} has send in mail_write but "
                    "mail.write.send_from is not set"
                )
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
    return Config(accounts=tuple(accounts), data_dir=data_dir, mail_write=mail_write)


def send_warnings(config: Config) -> list[str]:
    """One startup warning per token that may send mail to anyone (send without send_to).
    Names only the token label and account id, never an address."""
    return [
        f"token {t.label} of account {acct.id} may send mail to any recipient (no send_to)"
        for acct in config.accounts
        for t in acct.tokens
        if "send" in t.mail_write and t.send_to is None
    ]


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
