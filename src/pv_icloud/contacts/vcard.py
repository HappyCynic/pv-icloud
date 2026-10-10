# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

"""vCard 3.0 parsing for iCloud contacts.

Covers what iCloud serves (probed 2026-10-09): folded lines (often folded inside a
parameter), Apple item groups that attach a label to a property (`item1.EMAIL` plus
`item1.X-ABLabel:_$!<Other>!$_` or a custom label), `TYPE` lists, birthdays without a year
(`X-APPLE-OMIT-YEAR=1604`), related names, social profiles, and group cards
(`X-ADDRESSBOOKSERVER-KIND:group` with `X-ADDRESSBOOKSERVER-MEMBER:urn:uuid:<UID>`).
Photos and Apple's internal properties are ignored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..text import fold

_FOLD = re.compile(r"\r?\n[ \t]")
_APPLE_LABEL = re.compile(r"^_\$!<(.*)>!\$_$")
_DIGITS = re.compile(r"\D")
_BASIC_DATE = re.compile(r"^(\d{4}|--)(\d{2})(\d{2})$")  # 19800315, --0315
_EXT_DATE = re.compile(r"^(\d{4}|-)-(\d{2})-(\d{2})")  # 1980-03-15, --03-15
# TYPE values that say nothing a reader needs.
_NOISE_TYPES = frozenset({"internet", "pref", "voice", "x400", "msg"})
_TYPE_NAMES = {"cell": "mobile", "iphone": "iPhone"}


class VCardError(ValueError):
    pass


@dataclass
class Prop:
    group: str | None
    name: str
    params: dict[str, list[str]]
    value: str

    def types(self) -> list[str]:
        return [t.lower() for v in self.params.get("TYPE", []) for t in v.split(",") if t]


@dataclass
class Card:
    uid: str
    kind: str  # "individual" or "group"
    name: str
    given: str | None = None
    family: str | None = None
    middle: str | None = None
    prefix: str | None = None
    suffix: str | None = None
    nickname: str | None = None
    maiden_name: str | None = None
    organization: str | None = None
    department: str | None = None
    title: str | None = None
    emails: list[dict[str, Any]] = field(default_factory=list)
    phones: list[dict[str, Any]] = field(default_factory=list)
    addresses: list[dict[str, Any]] = field(default_factory=list)
    urls: list[dict[str, Any]] = field(default_factory=list)
    social_profiles: list[dict[str, Any]] = field(default_factory=list)
    instant_messaging: list[dict[str, Any]] = field(default_factory=list)
    birthday: str | None = None
    dates: list[dict[str, Any]] = field(default_factory=list)
    related_names: list[dict[str, Any]] = field(default_factory=list)
    note: str | None = None
    members: list[str] = field(default_factory=list)  # member UIDs (groups only)

    def email_addresses(self) -> list[str]:
        return [e["value"] for e in self.emails]

    def search_text(self) -> str:
        parts = [
            self.name,
            self.given,
            self.family,
            self.middle,
            self.nickname,
            self.maiden_name,
            self.organization,
            self.department,
            self.title,
            self.note,
            *self.email_addresses(),
            *(p["value"] for p in self.phones),
            *(a["formatted"] for a in self.addresses),
            *(r["value"] for r in self.related_names),
            *(s.get("user") or "" for s in self.social_profiles),
        ]
        return fold(" ".join(p for p in parts if p))

    def name_text(self) -> str:
        parts = (self.name, self.given, self.family, self.middle, self.nickname, self.maiden_name)
        return fold(" ".join(p for p in parts if p))

    def phone_digits(self) -> list[str]:
        return [_DIGITS.sub("", p["value"]) for p in self.phones]


def _split_unquoted(text: str, sep: str) -> list[str]:
    out: list[str] = []
    cur: list[str] = []
    quoted = False
    for ch in text:
        if ch == '"':
            quoted = not quoted
        elif ch == sep and not quoted:
            out.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    out.append("".join(cur))
    return out


def _unescape(value: str) -> str:
    out, i = [], 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            out.append("\n" if nxt in "nN" else nxt)
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _components(value: str) -> list[str]:
    """Split a structured value (N, ADR, ORG) on unescaped ';', then unescape."""
    parts, cur, i = [], [], 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            cur.append(value[i : i + 2])
            i += 2
            continue
        if ch == ";":
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    parts.append("".join(cur))
    return [_unescape(p).strip() for p in parts]


def parse_props(text: str) -> list[Prop]:
    props: list[Prop] = []
    for line in _FOLD.sub("", text).splitlines():
        if not line.strip():
            continue
        head, colon, value = _partition_unquoted(line)
        if not colon:
            continue
        name_part, *param_parts = _split_unquoted(head, ";")
        group, _, name = name_part.rpartition(".")
        params: dict[str, list[str]] = {}
        for raw in param_parts:
            key, eq, val = raw.partition("=")
            if not eq:  # vCard 2.1 bare type: "TEL;HOME:..."
                key, val = "TYPE", key
            vals = [v.strip('"') for v in _split_unquoted(val, ",")]
            params.setdefault(key.strip().upper(), []).extend(vals)
        props.append(Prop(group or None, name.strip().upper(), params, value))
    return props


def _partition_unquoted(line: str) -> tuple[str, str, str]:
    quoted = False
    for i, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
        elif ch == ":" and not quoted:
            return line[:i], ":", line[i + 1 :]
    return line, "", ""


def _label(prop: Prop, labels: dict[str, str]) -> str | None:
    raw = labels.get(prop.group or "", "").strip()
    if raw:
        apple = _APPLE_LABEL.match(raw)
        return apple.group(1).lower() if apple else raw
    types = [t for t in prop.types() if t not in _NOISE_TYPES]
    if "iphone" in types:  # Apple tags iPhone numbers IPHONE,CELL
        types = [t for t in types if t != "cell"]
    return " ".join(dict.fromkeys(_TYPE_NAMES.get(t, t) for t in types)) or None


def _preferred(prop: Prop) -> bool:
    return "pref" in prop.types() or "1" in prop.params.get("PREF", [])


def _date(prop: Prop) -> str | None:
    """ISO date; a date without a year comes out as --MM-DD."""
    value = prop.value.strip()
    m = _EXT_DATE.match(value) or _BASIC_DATE.match(value)
    if not m:
        return value or None
    year, month, day = m.groups()
    omit = prop.params.get("X-APPLE-OMIT-YEAR", [])
    if year in ("-", "--") or year in omit:
        return f"--{month}-{day}"
    return f"{year}-{month}-{day}"


def _address(prop: Prop) -> dict[str, Any]:
    parts = _components(prop.value) + [""] * 7
    po_box, extended, street, city, region, code, country = parts[:7]
    line2 = " ".join(p for p in (region, code) if p)
    lines = [p for p in (po_box, extended, street, city, line2, country) if p]
    out: dict[str, Any] = {
        "street": street or None,
        "city": city or None,
        "region": region or None,
        "postal_code": code or None,
        "country": country or None,
        "formatted": ", ".join(" ".join(x.split()) for x in lines),
    }
    if po_box:
        out["po_box"] = po_box
    if extended:
        out["extended"] = extended
    return out


def parse_vcard(text: str) -> Card:
    props = parse_props(text)
    if not any(p.name == "BEGIN" and p.value.strip().upper() == "VCARD" for p in props):
        raise VCardError("not a vCard")
    labels = {p.group: _unescape(p.value) for p in props if p.name == "X-ABLABEL" and p.group}
    one: dict[str, Prop] = {}
    for p in props:
        one.setdefault(p.name, p)

    def single(name: str) -> str | None:
        p = one.get(name)
        return (_unescape(p.value).strip() or None) if p else None

    uid = single("UID")
    if not uid:
        raise VCardError("vCard has no UID")
    kind = (single("X-ADDRESSBOOKSERVER-KIND") or single("KIND") or "individual").lower()
    card = Card(uid=uid, kind="group" if kind == "group" else "individual", name="")
    if "N" in one:
        n = _components(one["N"].value) + [""] * 5
        card.family, card.given, card.middle, card.prefix, card.suffix = (x or None for x in n[:5])
    card.nickname = single("NICKNAME")
    card.maiden_name = single("X-MAIDENNAME")
    card.title = single("TITLE")
    card.note = single("NOTE")
    if "ORG" in one:
        org = _components(one["ORG"].value)
        card.organization = org[0] or None
        card.department = "; ".join(x for x in org[1:] if x) or None
    if "BDAY" in one:
        card.birthday = _date(one["BDAY"])

    for p in props:
        value = _unescape(p.value).strip()
        if p.name == "EMAIL" and value:
            card.emails.append(
                {"label": _label(p, labels), "value": value, "preferred": _preferred(p)}
            )
        elif p.name == "TEL" and value:
            card.phones.append(
                {"label": _label(p, labels), "value": value, "preferred": _preferred(p)}
            )
        elif p.name == "ADR":
            addr = _address(p)
            if addr["formatted"]:
                card.addresses.append({"label": _label(p, labels), **addr})
        elif p.name == "URL" and value:
            card.urls.append({"label": _label(p, labels), "value": value})
        elif p.name == "X-SOCIALPROFILE":
            service = next((t for t in p.types() if t not in _NOISE_TYPES), None)
            user = next(iter(p.params.get("X-USER", [])), None)
            if value or user:
                card.social_profiles.append(
                    {"service": service, "user": user or None, "url": value or None}
                )
        elif p.name == "IMPP" and value:
            service = next(iter(p.params.get("X-SERVICE-TYPE", [])), None)
            scheme, sep, handle = value.partition(":")
            card.instant_messaging.append(
                {"service": service or (scheme if sep else None), "handle": handle or value}
            )
        elif p.name == "X-ABDATE":
            date = _date(p)
            if date:
                card.dates.append({"label": _label(p, labels), "value": date})
        elif p.name == "X-ABRELATEDNAMES" and value:
            card.related_names.append({"label": _label(p, labels), "value": value})
        elif p.name == "X-ADDRESSBOOKSERVER-MEMBER" and value:
            card.members.append(value.removeprefix("urn:uuid:"))

    full = single("FN")
    parts = (card.prefix, card.given, card.middle, card.family, card.suffix)
    built = " ".join(x for x in parts if x)
    card.name = (
        full
        or built
        or card.organization
        or (card.emails[0]["value"] if card.emails else None)
        or (card.phones[0]["value"] if card.phones else None)
        or "(no name)"
    )
    return card
