# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

"""Calendar tool implementations for one account. Blocking; the server runs them in threads."""

from __future__ import annotations

import datetime as dt
import threading
import time
import urllib.parse
import zoneinfo
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx

from ..config import AccountConfig
from ..dav import DavThrottled, home_may_have_moved
from ..errors import ToolInputError
from ..text import fold
from .caldav import CalDavClient, CalDavError, CalendarInfo
from .expand import expand
from .feeds import FeedClient, FeedError, feed_url
from .ids import BadEventId, EventId, decode_event_id
from .parse import Event, feed_ref, parse_occurrences, parse_resource

EVENTS_LIMIT_DEFAULT = 100
EVENTS_LIMIT_MAX = 200
SEARCH_EVENTS_LIMIT_DEFAULT = 50
EVENT_NOTES_MAX_CHARS = 20_000
DEFAULT_RANGE_DAYS = 7
LIST_TTL_SECONDS = 300
FETCH_WORKERS = 8
# Window around an occurrence's DTSTART when re-reading it. Wide enough for any zone
# offset, since date and floating starts carry no zone of their own.
_EVENT_WINDOW = dt.timedelta(days=2)
SUBSCRIPTIONS_OFF_NOTE = (
    "Subscribed calendars are listed for reference only: fetching their feeds is turned off "
    "for this account (calendar.subscriptions: false)."
)


class CalendarService:
    def __init__(
        self,
        account: AccountConfig,
        client: CalDavClient,
        feeds: FeedClient | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.account = account
        self.client = client
        self.feeds = feeds if account.calendar.subscriptions else None
        self._clock = clock
        self._lock = threading.Lock()
        self._cached: tuple[float, list[CalendarInfo]] | None = None

    # --- calendars ---------------------------------------------------------------------

    def _all_calendars(self) -> list[CalendarInfo]:
        with self._lock:
            if self._cached and self._clock() - self._cached[0] < LIST_TTL_SECONDS:
                return self._cached[1]
        try:
            cals = self.client.calendars()
        except CalDavError as exc:
            if home_may_have_moved(exc):  # the account may have moved to another pNN host
                self.client.forget_home()
            raise
        with self._lock:
            self._cached = (self._clock(), cals)
        return cals

    def _in_scope(self) -> list[CalendarInfo]:
        scope = self.account.calendar.calendars
        return [c for c in self._all_calendars() if scope.allows(c.name)]

    def _has_events(self, cal: CalendarInfo) -> bool:
        if cal.kind == "calendar":
            return True
        return self.feeds is not None and bool(cal.source)

    def _default_tz(self, cals: Sequence[CalendarInfo]) -> zoneinfo.ZoneInfo:
        """Configured zone, else the default zone of the account's own calendars."""
        candidates = [self.account.calendar.timezone]
        own = [c for c in cals if c.kind == "calendar" and not c.shared_with_me]
        own.sort(key=lambda c: c.id != "home")  # iCloud's original default calendar
        candidates += [c.timezone for c in own]
        for name in candidates:
            if not name:
                continue
            try:
                return zoneinfo.ZoneInfo(name)
            except zoneinfo.ZoneInfoNotFoundError, ValueError:
                continue
        return zoneinfo.ZoneInfo("UTC")

    @staticmethod
    def _source_host(cal: CalendarInfo) -> str | None:
        if not cal.source:
            return None
        try:
            return feed_url(cal.source).host  # host only: the full URL may hold a token
        except FeedError:
            return None

    def calendars(self) -> dict:
        cals = self._in_scope()
        rows: list[dict[str, Any]] = []
        for c in cals:
            row: dict[str, Any] = {
                "id": c.id,
                "name": c.name,
                "color": c.color,
                "kind": c.kind,
                "shared_with_me": c.shared_with_me,
                "timezone": c.timezone,
                "events_available": self._has_events(c),
            }
            if c.kind == "subscribed":
                row["source_host"] = self._source_host(c)
            rows.append(row)
        rows.sort(key=lambda r: (r["kind"] != "calendar", r["name"].lower()))
        out: dict[str, Any] = {"calendars": rows, "default_timezone": self._default_tz(cals).key}
        if self.feeds is None and any(c.kind == "subscribed" for c in cals):
            out["note"] = SUBSCRIPTIONS_OFF_NOTE
        return out

    def _select(self, cals: list[CalendarInfo], wanted: Sequence[str] | None) -> list[CalendarInfo]:
        if not wanted:
            return [c for c in cals if self._has_events(c)]
        chosen: list[CalendarInfo] = []
        for key in wanted:
            match = [c for c in cals if c.id == key or c.name.casefold() == key.casefold()]
            if not match:
                # Same message for an excluded calendar: don't confirm that it exists.
                raise ToolInputError(f"unknown calendar: {key}")
            for c in match:
                if not self._has_events(c):
                    raise ToolInputError(
                        f"{c.name} is a subscribed calendar and fetching subscriptions is "
                        "turned off for this account"
                    )
                if c not in chosen:
                    chosen.append(c)
        return chosen

    # --- fetching ----------------------------------------------------------------------

    def _fetch(
        self, cal: CalendarInfo, start: dt.datetime, end: dt.datetime, tz: dt.tzinfo
    ) -> list[Event]:
        if cal.kind == "subscribed":
            assert self.feeds is not None and cal.source
            feed = self.feeds.get(cal.source)
            return parse_occurrences(
                expand(feed, start.astimezone(dt.UTC), end.astimezone(dt.UTC), tz), cal, tz
            )
        events: list[Event] = []
        for path, text in self.client.events(cal, start, end):
            try:
                events.extend(parse_resource(text, cal, tz, path))
            except Exception:
                continue  # unparseable resource; the rest of the calendar still counts
        return events

    def _collect(
        self,
        targets: list[CalendarInfo],
        start: dt.datetime,
        end: dt.datetime,
        tz: dt.tzinfo,
        query: str | None,
    ) -> tuple[list[Event], list[dict[str, str]]]:
        found: list[Event] = []
        errors: list[dict[str, str]] = []
        throttled: DavThrottled | None = None
        with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
            futures = [(cal, pool.submit(self._fetch, cal, start, end, tz)) for cal in targets]
            for cal, future in futures:
                try:
                    found.extend(future.result())
                except (CalDavError, FeedError) as exc:
                    if isinstance(exc, DavThrottled):
                        throttled = exc
                    errors.append({"calendar": cal.name, "error": str(exc)})
        if errors and len(errors) == len(targets):
            if throttled is not None:
                raise throttled
            raise CalDavError("; ".join(e["error"] for e in errors))

        start_utc, end_utc = start.astimezone(dt.UTC), end.astimezone(dt.UTC)
        terms = fold(query).split() if query else []
        hits = [
            e
            for e in found
            # Overlap test; a zero-length event counts if it starts inside the range.
            if (e.start_utc < end_utc and (e.end_utc > start_utc or e.start_utc >= start_utc))
            and all(t in e.search_text for t in terms)
        ]
        hits.sort(
            key=lambda e: (e.start_utc, e.data["title"], e.data["calendar_id"], e.data["uid"] or "")
        )
        return hits, errors

    # --- ranges ------------------------------------------------------------------------

    @staticmethod
    def _parse_bound(value: str, field: str, tz: dt.tzinfo, *, inclusive_day: bool) -> dt.datetime:
        try:
            if len(value) == 10:  # YYYY-MM-DD
                day = dt.date.fromisoformat(value)
                if inclusive_day:
                    day += dt.timedelta(days=1)
                return dt.datetime.combine(day, dt.time(), tz)
            when = dt.datetime.fromisoformat(value)
        except ValueError as exc:
            raise ToolInputError(f"{field}: expected ISO date (YYYY-MM-DD) or datetime") from exc
        return when if when.tzinfo else when.replace(tzinfo=tz)

    def _range(
        self,
        start: str | None,
        end: str | None,
        tz: dt.tzinfo,
        *,
        max_days: int,
        default_before: int,
        default_after: int,
    ) -> tuple[dt.datetime, dt.datetime]:
        today = dt.datetime.combine(dt.datetime.now(tz).date(), dt.time(), tz)
        if start:
            start_dt = self._parse_bound(start, "start", tz, inclusive_day=False)
        elif end:
            start_dt = self._parse_bound(end, "end", tz, inclusive_day=True) - dt.timedelta(
                days=default_before + default_after
            )
        else:
            start_dt = today - dt.timedelta(days=default_before)
        if end:
            end_dt = self._parse_bound(end, "end", tz, inclusive_day=True)
        elif start:
            end_dt = start_dt + dt.timedelta(days=default_before + default_after)
        else:
            end_dt = today + dt.timedelta(days=default_after)
        if end_dt <= start_dt:
            raise ToolInputError("end must be after start")
        if end_dt - start_dt > dt.timedelta(days=max_days):
            raise ToolInputError(f"range is longer than {max_days} days; narrow it")
        return start_dt, end_dt

    @staticmethod
    def _check_paging(limit: int, offset: int, limit_max: int) -> None:
        if not 1 <= limit <= limit_max:
            raise ToolInputError(f"limit must be between 1 and {limit_max}")
        if offset < 0:
            raise ToolInputError("offset must be >= 0")

    def _result(
        self,
        hits: list[Event],
        errors: list[dict[str, str]],
        targets: list[CalendarInfo],
        start: dt.datetime,
        end: dt.datetime,
        tz: zoneinfo.ZoneInfo,
        limit: int,
        offset: int,
    ) -> dict:
        page = hits[offset : offset + limit]
        out: dict[str, Any] = {
            "events": [e.data for e in page],
            "total": len(hits),
            "offset": offset,
            "next_offset": offset + len(page) if offset + len(page) < len(hits) else None,
            "range": {"start": start.isoformat(), "end": end.isoformat(), "timezone": tz.key},
            "calendars_searched": [c.name for c in targets],
        }
        if errors:
            out["errors"] = errors
        return out

    # --- tools -------------------------------------------------------------------------

    def events(
        self,
        *,
        start: str | None = None,
        end: str | None = None,
        calendars: Sequence[str] | None = None,
        query: str | None = None,
        limit: int = EVENTS_LIMIT_DEFAULT,
        offset: int = 0,
    ) -> dict:
        self._check_paging(limit, offset, EVENTS_LIMIT_MAX)
        cals = self._in_scope()
        tz = self._default_tz(cals)
        start_dt, end_dt = self._range(
            start,
            end,
            tz,
            max_days=self.account.calendar.max_range_days,
            default_before=0,
            default_after=DEFAULT_RANGE_DAYS,
        )
        targets = self._select(cals, calendars)
        hits, errors = self._collect(targets, start_dt, end_dt, tz, query)
        return self._result(hits, errors, targets, start_dt, end_dt, tz, limit, offset)

    def search(
        self,
        *,
        query: str,
        start: str | None = None,
        end: str | None = None,
        calendars: Sequence[str] | None = None,
        limit: int = SEARCH_EVENTS_LIMIT_DEFAULT,
        offset: int = 0,
    ) -> dict:
        if not query or not query.strip():
            raise ToolInputError("query is required")
        self._check_paging(limit, offset, EVENTS_LIMIT_MAX)
        cals = self._in_scope()
        tz = self._default_tz(cals)
        max_days = self.account.calendar.max_search_days
        start_dt, end_dt = self._range(
            start,
            end,
            tz,
            max_days=max_days,
            default_before=max_days // 2,
            default_after=max_days - max_days // 2,
        )
        targets = self._select(cals, calendars)
        hits, errors = self._collect(targets, start_dt, end_dt, tz, query)
        return self._result(hits, errors, targets, start_dt, end_dt, tz, limit, offset)

    def event(self, event_id: str, max_chars: int = EVENT_NOTES_MAX_CHARS, offset: int = 0) -> dict:
        if not 1 <= max_chars <= EVENT_NOTES_MAX_CHARS:
            raise ToolInputError(f"max_chars must be between 1 and {EVENT_NOTES_MAX_CHARS}")
        if offset < 0:
            raise ToolInputError("offset must be >= 0")
        try:
            eid = decode_event_id(event_id)
        except BadEventId as exc:
            raise ToolInputError(str(exc)) from exc
        cals = self._in_scope()
        cal = next((c for c in cals if c.id == eid.calendar_id), None)
        if cal is None:
            raise ToolInputError(f"unknown calendar: {eid.calendar_id}")
        if not self._has_events(cal):
            raise ToolInputError(f"{cal.name}: fetching subscriptions is turned off")
        try:
            when = dt.datetime.fromisoformat(eid.start)
        except ValueError as exc:
            raise ToolInputError("malformed event id") from exc
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.UTC)
        tz = self._default_tz(cals)
        lo, hi = when - _EVENT_WINDOW, when + _EVENT_WINDOW

        found: Event | None = None
        if cal.kind == "subscribed":
            if not eid.path.startswith("uid:"):
                raise ToolInputError("malformed event id")
            candidates = [e for e in self._fetch(cal, lo, hi, tz) if self._ref(e) == eid.path]
        else:
            self._check_resource_path(cal, eid)
            candidates = []
            # A narrow expanded query, not calendar-multiget: iCloud ignores <C:expand> there.
            for path, text in self.client.events(cal, lo, hi):
                if path != eid.path:
                    continue
                try:
                    candidates += parse_resource(text, cal, tz, path)
                except Exception:
                    continue
        found = next((e for e in candidates if e.data["start"] == eid.start), None)
        if found is None:
            raise ToolInputError("event not found (deleted or moved); list events again")

        data = {k: v for k, v in found.data.items() if k not in ("notes", "notes_truncated")}
        data.pop("attendees_total", None)
        if found.attendees:
            data["attendees"] = found.attendees
        notes = found.notes or ""
        page = notes[offset : offset + max_chars]
        end = offset + len(page)
        data.update(
            notes=page if found.notes is not None else None,
            notes_offset=offset,
            notes_total_chars=len(notes),
            next_offset=end if end < len(notes) else None,
        )
        return data

    @staticmethod
    def _ref(event: Event) -> str:
        return feed_ref(event.data["uid"] or "")

    @staticmethod
    def _check_resource_path(cal: CalendarInfo, eid: EventId) -> None:
        # Only accept a direct child of the calendar the id claims, so an edited id cannot
        # reach a calendar outside the account's scope.
        cal_path = httpx.URL(cal.url).raw_path.decode("ascii").rstrip("/") + "/"
        name = urllib.parse.unquote(eid.path[len(cal_path) :])
        if not eid.path.startswith(cal_path) or not name or "/" in name or name in (".", ".."):
            raise ToolInputError("malformed event id")
