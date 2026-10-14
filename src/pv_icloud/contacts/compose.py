# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

"""vCard text for contact writes (docs/adr/0006-opt-in-contact-write.md).

Imported only when PV_ICLOUD_CONTACTS_WRITE=enabled. It builds new vCard 3.0 cards and edits
stored cards *in place*: the stored text is split into logical lines that keep their exact
bytes (folds and line endings included), only the lines a change names are rewritten (plus
`REV`, and `FN` when it was derived from the old name), and every other line is emitted as
stored, in the stored order. A card is never rebuilt from the parsed model, which drops what
the reader does not show (photos, Apple-internal and unknown properties). Anything that
cannot be applied unambiguously is refused with nothing written.

New values are written as Apple's apps write them (checked on a live address book,
2026-10-14), so `contacts_get` reads back the label that was given: one `TYPE=` parameter per
type, `IPHONE` before `CELL`, `VOICE` only on built-in voice types, `INTERNET` first on
emails, `PREF` last; other labels go in an item group with `X-ABLabel`. Nothing here writes
a photo.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..errors import ToolInputError
from .vcard import _partition_unquoted, _split_unquoted, _unescape, parse_vcard

NAME_MAX_CHARS = 255
NOTE_MAX_CHARS = 20_000
VALUE_MAX_CHARS = 1000
LABEL_MAX_CHARS = 100
LIST_MAX = 20  # entries per list field on create; add plus remove together on update
GROUPS_MAX = 10
PRODID = "-//pv-icloud//EN"

NAME_PARTS = ("family_name", "given_name", "middle_name", "prefix", "suffix")  # N order
TEXT_FIELDS = ("nickname", "maiden_name", "organization", "department", "title")
SCALAR_FIELDS = (*NAME_PARTS, *TEXT_FIELDS, "birthday", "note")
LIST_FIELDS = (
    "emails",
    "phones",
    "addresses",
    "urls",
    "social_profiles",
    "instant_messaging",
    "dates",
    "related_names",
)
_SINGLE = {"nickname": "NICKNAME", "maiden_name": "X-MAIDENNAME", "title": "TITLE", "note": "NOTE"}
_PROP = {
    "emails": "EMAIL",
    "phones": "TEL",
    "addresses": "ADR",
    "urls": "URL",
    "social_profiles": "X-SOCIALPROFILE",
    "instant_messaging": "IMPP",
    "dates": "X-ABDATE",
    "related_names": "X-ABRELATEDNAMES",
}
_ADDRESS_PARTS = ("po_box", "extended", "street", "city", "region", "postal_code", "country")
# Keys an entry may have, as contacts_get shows them; `preferred` and `formatted` are
# accepted (so a read value can be passed back) but never used to match.
_KEYS: dict[str, tuple[str, ...]] = {
    "emails": ("label", "value"),
    "phones": ("label", "value"),
    "addresses": ("label", *_ADDRESS_PARTS),
    "urls": ("label", "value"),
    "social_profiles": ("service", "user", "url"),
    "instant_messaging": ("service", "handle"),
    "dates": ("label", "value"),
    "related_names": ("label", "value"),
}
_IGNORED_KEYS = frozenset({"preferred", "formatted"})

# Design decision 3: built-in labels written as TYPE parameters, in Apple's order.
_PHONE_TYPES = {
    "mobile": ("CELL", "VOICE"),
    "iphone": ("IPHONE", "CELL", "VOICE"),
    "home": ("HOME", "VOICE"),
    "work": ("WORK", "VOICE"),
    "main": ("MAIN",),
    "home fax": ("HOME", "FAX"),
    "work fax": ("WORK", "FAX"),
    "pager": ("PAGER",),
}
_HOME_WORK: dict[str, tuple[str, ...]] = {"home": ("HOME",), "work": ("WORK",)}
_TYPED: dict[str, dict[str, tuple[str, ...]]] = {
    "phones": _PHONE_TYPES,
    "emails": _HOME_WORK,
    "addresses": _HOME_WORK,
    "urls": _HOME_WORK,
}
# Apple's other built-in labels, stored as `X-ABLabel:_$!<Name>!$_`.
_APPLE_LABELS = {
    name.casefold(): name
    for name in (
        "Other", "School", "HomePage", "Anniversary", "Mother", "Father", "Parent", "Brother",
        "Sister", "Child", "Friend", "Spouse", "Partner", "Assistant", "Manager",
    )
}  # fmt: skip
_LABEL_META = frozenset({"X-ABLABEL", "X-ABADR"})  # lines that only describe an item group
_ITEM = re.compile(r"^item(\d+)$", re.IGNORECASE)
_FULL_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_NO_YEAR = re.compile(r"^--(\d{2})-(\d{2})$")
_LINE_BREAK = re.compile(r"[\r\n\x0b\x0c\x85\u2028\u2029]")
_PHYSICAL = re.compile(r"[^\n]*\n|[^\n]+$")


class Unsafe(ToolInputError):
    """The stored card cannot be changed unambiguously; nothing is written."""

    def __init__(self, what: str):
        super().__init__(f"cannot change this contact safely: {what}")


@dataclass(frozen=True)
class Changes:
    """An update: scalar `values` to set, scalar fields to `clear`, list entries to `add`
    and to `remove` (as contacts_get returned them)."""

    values: Mapping[str, str] = field(default_factory=dict)
    clear: frozenset[str] = frozenset()
    add: Mapping[str, Sequence[Mapping[str, Any]]] = field(default_factory=dict)
    remove: Mapping[str, Sequence[Mapping[str, Any]]] = field(default_factory=dict)

    def touches_card(self) -> bool:
        return bool(
            self.values or self.clear or any(self.add.values()) or any(self.remove.values())
        )


# --- input checks ------------------------------------------------------------------------


def _text(value: Any, name: str, limit: int, *, breaks: bool = False) -> str:
    if not isinstance(value, str):
        raise ToolInputError(f"{name} must be text")
    if len(value) > limit:
        raise ToolInputError(f"{name} is longer than {limit} characters")
    if not breaks and _LINE_BREAK.search(value):
        raise ToolInputError(f"{name} must not contain line breaks")
    return value


def _date(value: Any, name: str) -> str:
    text = _text(value, name, 10)
    full, no_year = _FULL_DATE.match(text), _NO_YEAR.match(text)
    try:
        if full:
            dt.date(int(full[1]), int(full[2]), int(full[3]))
        elif no_year:
            dt.date(2000, int(no_year[1]), int(no_year[2]))  # a leap year: --02-29 is valid
        else:
            raise ValueError
    except ValueError:
        raise ToolInputError(f"{name} must be a real date, YYYY-MM-DD or --MM-DD") from None
    return text


def _check_scalar(name: str, value: Any) -> None:
    if name == "note":
        _text(value, name, NOTE_MAX_CHARS, breaks=True)
    elif name == "birthday":
        _date(value, name)
    else:
        _text(value, name, NAME_MAX_CHARS)


def _optional(entry: Mapping[str, Any], key: str, name: str, limit: int) -> str | None:
    value = entry.get(key)
    if value is None:
        return None
    return _text(value, name, limit)


def _web(value: str, name: str) -> None:
    if not re.match(r"(?i)^https?://\S", value):
        raise ToolInputError(f"{name}: URLs must start with http:// or https://")


def _check_entry(name: str, entry: Any, *, adding: bool) -> None:
    if not isinstance(entry, Mapping):
        raise ToolInputError(f"{name}: each entry must be an object")
    unknown = set(entry) - set(_KEYS[name]) - _IGNORED_KEYS
    if unknown:
        raise ToolInputError(f"{name}: unknown key(s) {', '.join(sorted(map(str, unknown)))}")
    if "label" in _KEYS[name]:
        _optional(entry, "label", f"{name} label", LABEL_MAX_CHARS)
    if "preferred" in entry and not isinstance(entry["preferred"], bool | None):
        raise ToolInputError(f"{name}: preferred must be true or false")
    if name == "addresses":
        parts = [_optional(entry, k, f"{name} {k}", VALUE_MAX_CHARS) for k in _ADDRESS_PARTS]
        if adding and not any(p and p.strip() for p in parts):
            raise ToolInputError(f"{name}: an address needs at least one part")
        return
    if name in ("social_profiles", "instant_messaging"):
        service = _optional(entry, "service", f"{name} service", LABEL_MAX_CHARS)
        if service is not None and re.search(r'[",;:]', service):
            raise ToolInputError(f'{name}: service must not contain " , ; or :')
        if adding and not (service and service.strip()):
            raise ToolInputError(f"{name}: service is required")
        if name == "social_profiles":
            user = _optional(entry, "user", f"{name} user", VALUE_MAX_CHARS)
            url = _optional(entry, "url", f"{name} url", VALUE_MAX_CHARS)
            if user is not None and '"' in user:
                raise ToolInputError(f'{name}: user must not contain "')
            if adding and not (user or url):
                raise ToolInputError(f"{name}: give a user or a url")
            if adding and url:
                _web(url, name)
        else:
            handle = _optional(entry, "handle", f"{name} handle", VALUE_MAX_CHARS)
            if adding and not (handle and handle.strip()):
                raise ToolInputError(f"{name}: handle is required")
        return
    value = entry.get("value")
    if adding or value is not None:
        if name == "dates":
            _date(value, name)
            return
        value = _text(value, name, VALUE_MAX_CHARS)
        if not value.strip():
            raise ToolInputError(f"{name}: value is required")
        if name == "emails" and value.count("@") != 1:
            raise ToolInputError(f"{name}: an email address has exactly one @")
        if name == "urls" and adding:
            _web(value, name)


def _check_lists(lists: Mapping[str, Any], *, adding: bool) -> int:
    count = 0
    for name, entries in lists.items():
        if name not in LIST_FIELDS:
            raise ToolInputError(f"unknown field: {name}")
        if not isinstance(entries, list | tuple):
            raise ToolInputError(f"{name} must be a list")
        for entry in entries:
            _check_entry(name, entry, adding=adding)
        count += len(entries)
    return count


def check_new(fields: Mapping[str, Any]) -> None:
    """The arguments of contacts_create, before anything is sent."""
    for name, value in fields.items():
        if name in SCALAR_FIELDS:
            if value is not None:
                _check_scalar(name, value)
        elif name in LIST_FIELDS:
            if value is not None:
                _check_lists({name: value}, adding=True)
                if len(value) > LIST_MAX:
                    raise ToolInputError(f"{name}: at most {LIST_MAX} entries")
        else:
            raise ToolInputError(f"unknown field: {name}")
    ident = (*NAME_PARTS, "organization", "emails", "phones")
    if not any(fields.get(k) for k in ident):
        raise ToolInputError("a new contact needs a name, organization, email or phone")


def check_changes(changes: Changes, *, groups: bool = False) -> None:
    """The arguments of contacts_update, before anything is sent. `groups`: the call also
    changes group membership."""
    for name, value in changes.values.items():
        if name not in SCALAR_FIELDS:
            raise ToolInputError(f"unknown field: {name}")
        if name in changes.clear:
            raise ToolInputError(f"{name} is both given and cleared")
        _check_scalar(name, value)
    for name in changes.clear:
        if name not in SCALAR_FIELDS:
            raise ToolInputError(f"clear: {name} is not a field that can be cleared")
    count = _check_lists(changes.add, adding=True) + _check_lists(changes.remove, adding=False)
    if count > LIST_MAX:
        raise ToolInputError(f"add and remove: at most {LIST_MAX} entries together")
    if not changes.touches_card() and not groups:
        raise ToolInputError("nothing to change")


def check_groups(names: Iterable[str]) -> None:
    names = list(names)
    if len(names) > GROUPS_MAX:
        raise ToolInputError(f"at most {GROUPS_MAX} group names per call")
    for name in names:
        _text(name, "group", NAME_MAX_CHARS)


# --- writing lines -----------------------------------------------------------------------


def _escape(value: str) -> str:
    """vCard 3.0 text value: backslash, comma, semicolon and line breaks escaped."""
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    return value.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")


def _param(value: str) -> str:
    return f'"{value}"' if re.search(r"[,;:]", value) else value


def _fold(content: str, eol: str) -> str:
    """Fold at 75 octets, never inside a UTF-8 character."""
    lines: list[str] = []
    cur, size = "", 0
    for ch in content:
        n = len(ch.encode())
        if size + n > 75:
            lines.append(cur)
            cur, size = " ", 1
        cur += ch
        size += n
    lines.append(cur)
    return eol.join(lines) + eol


def _content(group: str | None, name: str, params: Sequence[str], value: str) -> str:
    head = (f"{group}." if group else "") + name + "".join(f";{p}" for p in params)
    return f"{head}:{value}"


def _rev(now: dt.datetime) -> str:
    return "REV:" + now.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _date_value(value: str) -> tuple[list[str], str]:
    no_year = _NO_YEAR.match(value)
    if no_year:  # as Apple writes a date without a year
        return ["X-APPLE-OMIT-YEAR=1604"], f"1604-{no_year[1]}-{no_year[2]}"
    return [], value


def _label_line(group: str, label: str) -> str:
    apple = _APPLE_LABELS.get(label.casefold())
    return _content(group, "X-ABLabel", [], f"_$!<{apple}>!$_" if apple else _escape(label))


def _entry_lines(name: str, entry: Mapping[str, Any], next_item: list[int]) -> list[str]:
    """Content lines (unfolded) for one new value: the property and, for a label with no
    TYPE form, its item group's X-ABLabel."""
    label = (entry.get("label") or "").strip() or None
    types = _TYPED.get(name, {}).get(label.casefold()) if label else ()
    in_group = name in ("dates", "related_names") or (label is not None and types is None)
    params = [f"TYPE={t}" for t in (types or ())]
    if name == "emails":
        params = ["TYPE=INTERNET", *params]
    if name in ("emails", "phones") and entry.get("preferred"):
        params.append("TYPE=PREF")
    if name == "addresses":
        value = ";".join(_escape((entry.get(k) or "").strip()) for k in _ADDRESS_PARTS)
    elif name == "social_profiles":
        params = [f"TYPE={_param(entry['service'].strip().lower())}"]
        if entry.get("user"):
            params.append(f"X-USER={_param(entry['user'].strip())}")
        value = (entry.get("url") or "").strip()
    elif name == "instant_messaging":
        params = [f"X-SERVICE-TYPE={_param(entry['service'].strip())}"]
        value = "x-apple:" + entry["handle"].strip()
    elif name == "dates":
        date_params, value = _date_value(entry["value"])
        params += date_params
    elif name == "urls":
        value = entry["value"].strip()  # a URI: not escaped
    else:
        value = _escape(entry["value"].strip())
    group = None
    if in_group:
        group = f"item{next_item[0]}"
        next_item[0] += 1
    lines = [_content(group, _PROP[name], params, value)]
    if group and label:
        lines.append(_label_line(group, label))
    return lines


def _built_name(parts: Mapping[str, str | None]) -> str:
    order = ("prefix", "given_name", "middle_name", "family_name", "suffix")
    return " ".join(p for p in (parts.get(k) or "" for k in order) if p.strip()).strip()


def new_card(uid: str, fields: Mapping[str, Any], now: dt.datetime, eol: str = "\r\n") -> str:
    """A new vCard 3.0 card from contacts_create's (checked) arguments."""
    get: dict[str, Any] = {k: (v.strip() if isinstance(v, str) else v) for k, v in fields.items()}
    org, dept = get.get("organization") or "", get.get("department") or ""
    name = _built_name(get)
    emails, phones = get.get("emails") or [], get.get("phones") or []
    display = (
        name
        or org
        or (emails[0]["value"].strip() if emails else "")
        or (phones[0]["value"].strip() if phones else "")
    )
    lines = [
        "BEGIN:VCARD",
        "VERSION:3.0",
        f"PRODID:{PRODID}",
        "N:" + ";".join(_escape(get.get(k) or "") for k in NAME_PARTS),
        f"FN:{_escape(display)}",
    ]
    if org and not name:  # shows as a company, as Apple Contacts does
        lines.append("X-ABShowAs:COMPANY")
    for key in ("nickname", "maiden_name"):
        if get.get(key):
            lines.append(_content(None, _SINGLE[key], [], _escape(get[key])))
    if org or dept:
        lines.append(f"ORG:{_escape(org)};{_escape(dept)}")
    if get.get("title"):
        lines.append(_content(None, "TITLE", [], _escape(get["title"])))
    item = [1]
    for key in ("emails", "phones", "addresses", "urls", "social_profiles", "instant_messaging"):
        for entry in get.get(key) or []:
            lines += _entry_lines(key, entry, item)
    if get.get("birthday"):
        params, value = _date_value(get["birthday"])
        lines.append(_content(None, "BDAY", params, value))
    for key in ("dates", "related_names"):
        for entry in get.get(key) or []:
            lines += _entry_lines(key, entry, item)
    if fields.get("note"):
        lines.append(_content(None, "NOTE", [], _escape(fields["note"])))
    lines += [f"UID:{uid}", _rev(now), "END:VCARD"]
    return "".join(_fold(x, eol) for x in lines)


# --- editing stored text -----------------------------------------------------------------


@dataclass
class _Line:
    raw: str  # the exact stored bytes: the line, its folds and its line ending
    group: str | None
    name: str  # upper case; "" for a blank line
    head: str  # unfolded text before the value's colon, as stored
    value: str  # unfolded value, still escaped
    deleted: bool = False


def _components_raw(value: str) -> list[str]:
    """Split a structured value on unescaped ';', keeping each part escaped as stored."""
    parts, cur, i = [], [], 0
    while i < len(value):
        if value[i] == "\\" and i + 1 < len(value):
            cur.append(value[i : i + 2])
            i += 2
            continue
        if value[i] == ";":
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(value[i])
        i += 1
    parts.append("".join(cur))
    return parts


class _Card:
    """A stored card as logical lines. Lines can be replaced, deleted or added before
    END:VCARD; `text()` emits every other line exactly as stored."""

    def __init__(self, text: str):
        physical = _PHYSICAL.findall(text)
        if not physical or physical[0][:1] in (" ", "\t"):
            raise Unsafe("the stored card cannot be split into lines")
        first = physical[0]
        self.eol = "\n" if first.endswith("\n") and not first.endswith("\r\n") else "\r\n"
        groups: list[list[str]] = []
        for line in physical:
            if line[:1] in (" ", "\t"):
                groups[-1].append(line)
            else:
                groups.append([line])
        self.lines = [self._parse(chunk) for chunk in groups]
        self.added: list[str] = []
        names = [ln.name for ln in self.lines if ln.name]
        if not names or names[0] != "BEGIN" or names[-1] != "END":
            raise Unsafe("the stored card cannot be split into lines")
        if names.count("BEGIN") != 1 or names.count("END") != 1:
            raise Unsafe("the stored card cannot be split into lines")
        versions = [ln.value.strip() for ln in self.lines if ln.name == "VERSION"]
        if versions != ["3.0"]:
            raise Unsafe("only vCard 3.0 cards can be changed")

    @staticmethod
    def _parse(chunk: list[str]) -> _Line:
        raw = "".join(chunk)
        unfolded = "".join((x if i == 0 else x[1:]).rstrip("\r\n") for i, x in enumerate(chunk))
        if not unfolded.strip():
            return _Line(raw, None, "", "", "")
        head, colon, value = _partition_unquoted(unfolded)
        if not colon:
            raise Unsafe("the stored card cannot be split into lines")
        name_part = _split_unquoted(head, ";")[0]
        group, _, name = name_part.rpartition(".")
        return _Line(raw, group or None, name.strip().upper(), head, value)

    def find(self, name: str) -> list[_Line]:
        return [ln for ln in self.lines if ln.name == name and not ln.deleted]

    def one(self, name: str, what: str) -> _Line | None:
        found = self.find(name)
        if len(found) > 1:
            raise Unsafe(what)
        return found[0] if found else None

    def set_value(self, line: _Line, value: str) -> None:
        """Rewrite one line's value, keeping its group, name and parameters as stored."""
        line.value = value
        line.raw = _fold(f"{line.head}:{value}", self.eol)

    def replace(self, line: _Line, content: str) -> None:
        line.raw = _fold(content, self.eol)
        line.head, line.value = content.split(":", 1)[0], content.split(":", 1)[1]

    def add(self, content: str) -> None:
        self.added.append(_fold(content, self.eol))

    def bump_rev(self, now: dt.datetime) -> None:
        rev = self.one("REV", "REV")
        if rev is None:
            self.add(_rev(now))
        else:
            self.replace(rev, _rev(now))

    def next_item(self) -> int:
        numbers = [int(m[1]) for ln in self.lines if ln.group and (m := _ITEM.match(ln.group))]
        return max(numbers, default=0) + 1

    def text(self) -> str:
        out: list[str] = []
        for line in self.lines:
            if line.name == "END":
                out += self.added
            if not line.deleted:
                out.append(line.raw)
        return "".join(out)


def _parts(card: _Card, name: str, size: int, what: str) -> tuple[_Line | None, list[str]]:
    line = card.one(name, what)
    parts = _components_raw(line.value) if line else []
    return line, parts + [""] * (size - len(parts))


def _plain(raw: str) -> str:
    return _unescape(raw).strip()


def _display(n_parts: Sequence[str]) -> str:
    """The display name N's (escaped) parts give: prefix given middle family suffix."""
    return " ".join(p for p in (_plain(n_parts[i]) for i in (3, 1, 2, 0, 4)) if p)


def _entry_of(card: _Card, line: _Line, name: str) -> dict[str, Any] | None:
    """The value as contacts_get shows it, read by the reader itself from just this line and
    its item group's label."""
    labels = [
        f"{ln.head}:{ln.value}"
        for ln in card.lines
        if line.group and ln.group == line.group and ln.name == "X-ABLABEL" and not ln.deleted
    ]
    mini = "\r\n".join(["BEGIN:VCARD", "VERSION:3.0", "UID:x", f"{line.head}:{line.value}"])
    mini += "".join(f"\r\n{x}" for x in labels) + "\r\nEND:VCARD\r\n"
    entries = getattr(parse_vcard(mini), name)
    return entries[0] if len(entries) == 1 else None


def _same(name: str, given: Mapping[str, Any], stored: Mapping[str, Any]) -> bool:
    for key in _KEYS[name]:
        a = given.get(key)
        b = stored.get(key)
        a = a.strip() if isinstance(a, str) else a
        b = b.strip() if isinstance(b, str) else b
        if key in ("label", "service"):
            a, b = (a or "").casefold(), (b or "").casefold()
        if (a or None) != (b or None):
            return False
    return True


def _remove(card: _Card, name: str, entry: Mapping[str, Any]) -> None:
    matches = [
        ln
        for ln in card.find(_PROP[name])
        if (stored := _entry_of(card, ln, name)) is not None and _same(name, entry, stored)
    ]
    if not matches:
        raise ToolInputError(f"not found on this contact: {name}")
    if len(matches) > 1:
        raise Unsafe(f"{name} (more than one stored value matches)")
    [line] = matches
    others = [ln for ln in card.lines if line.group and ln.group == line.group and ln is not line]
    if any(not ln.deleted and ln.name not in _LABEL_META for ln in others):
        raise Unsafe(name)
    for ln in [line, *others]:
        ln.deleted = True


def apply_update(text: str, changes: Changes, now: dt.datetime) -> str:
    """The stored card with `changes` applied in place (see the module docstring)."""
    card = _Card(text)
    if len(card.find("REV")) > 1:
        raise Unsafe("REV")
    touched = set(changes.values) | set(changes.clear)

    def new(key: str, old: str) -> str:
        """The new escaped value of a field, or `old` (escaped as stored) if unchanged."""
        if key in changes.clear:
            return ""
        if key in changes.values:
            return _escape(changes.values[key].strip())
        return old

    company = any(ln.value.strip().upper() == "COMPANY" for ln in card.find("X-ABSHOWAS"))
    names_changed = sorted(touched & set(NAME_PARTS))
    org_changed = sorted(touched & {"organization", "department"})
    orgs = card.find("ORG")
    old_org = _plain(_components_raw(orgs[0].value)[0]) if orgs else ""

    if names_changed:
        # A duplicated N is refused only when the call changes the name.
        n_line, n_parts = _parts(card, "N", 5, names_changed[0])
        old_name = _display(n_parts)
        parts = [new(key, n_parts[i]) for i, key in enumerate(NAME_PARTS)] + n_parts[5:]
        if n_line is None:
            card.add("N:" + ";".join(parts))
        else:
            card.set_value(n_line, ";".join(parts))
        fns = card.find("FN")
        if not company and len(fns) == 1 and _plain(fns[0].value) == old_name:
            display = _display(parts) or old_org
            if display:
                card.set_value(fns[0], _escape(display))

    if org_changed:
        org_line, org_parts = _parts(card, "ORG", 2, org_changed[0])
        if len(org_parts) > 2 and "department" in touched and any(org_parts[2:]):
            raise Unsafe("department")
        parts = [new("organization", org_parts[0]), new("department", org_parts[1])]
        parts += org_parts[2:]
        if not any(_plain(p) for p in parts):
            if org_line is not None:
                org_line.deleted = True
        elif org_line is None:
            card.add("ORG:" + ";".join(parts))
        else:
            card.set_value(org_line, ";".join(parts))
        new_org = _plain(parts[0])
        fns = card.find("FN")
        derived = len(fns) == 1 and _plain(fns[0].value) == old_org
        if company and "organization" in touched and derived and new_org:
            card.set_value(fns[0], _escape(new_org))

    for key, prop in _SINGLE.items():
        if key not in touched:
            continue
        line = card.one(prop, key)
        if key in changes.clear:
            if line is not None:
                line.deleted = True
        elif line is None:
            card.add(_content(None, prop, [], new(key, "")))
        else:
            card.set_value(line, new(key, ""))

    if "birthday" in touched:
        line = card.one("BDAY", "birthday")
        if "birthday" in changes.clear:
            if line is not None:
                line.deleted = True
        else:
            params, value = _date_value(changes.values["birthday"].strip())
            content = _content(None, "BDAY", params, value)
            if line is None:
                card.add(content)
            else:
                card.replace(line, content)

    for name, entries in changes.remove.items():
        for entry in entries:
            _remove(card, name, entry)
    item = [card.next_item()]
    for name, entries in changes.add.items():
        for entry in entries:
            for content in _entry_lines(name, entry, item):
                card.add(content)

    card.bump_rev(now)
    return card.text()


def _member(line: _Line, uid: str) -> bool:
    return _plain(line.value).removeprefix("urn:uuid:") == uid


def add_member(text: str, uid: str, now: dt.datetime) -> str | None:
    """The group card with one member line added for `uid`; None if it is a member."""
    card = _Card(text)
    if len(card.find("REV")) > 1:
        raise Unsafe("REV")
    if any(_member(ln, uid) for ln in card.find("X-ADDRESSBOOKSERVER-MEMBER")):
        return None
    card.add(f"X-ADDRESSBOOKSERVER-MEMBER:urn:uuid:{uid}")
    card.bump_rev(now)
    return card.text()


def remove_member(text: str, uid: str, now: dt.datetime) -> str | None:
    """The group card without `uid`'s member lines; None if it is not a member. Every other
    line, members hidden from the caller included, is kept as stored."""
    card = _Card(text)
    if len(card.find("REV")) > 1:
        raise Unsafe("REV")
    lines = [ln for ln in card.find("X-ADDRESSBOOKSERVER-MEMBER") if _member(ln, uid)]
    if not lines:
        return None
    for line in lines:
        line.deleted = True
    card.bump_rev(now)
    return card.text()
