# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

"""Read-only WebDAV plumbing shared by the CalDAV and CardDAV clients.

Only PROPFIND and REPORT are ever sent: `_request` refuses any other method, so neither
client can change data on iCloud.

When iCloud is overloaded or rate-limits an account it answers HTTP 503 (or 429) with
`Retry-After`. Seen 2026-10-09 after a day of repeated full address-book loads: every
CalDAV and CardDAV request for that Apple ID got 503 with `Retry-After` of 20-30 minutes.
Clients then pause all requests for the account (`Throttle`) instead of retrying, which would
only extend the block.
"""

from __future__ import annotations

import datetime as dt
import email.utils
import logging
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable

import httpx

log = logging.getLogger(__name__)

TIMEOUT = 30.0
THROTTLE_DEFAULT_SECONDS = 60.0  # 503/429 without a usable Retry-After
THROTTLE_MAX_SECONDS = 3600.0
READ_METHODS = frozenset({"PROPFIND", "REPORT"})
NS = {
    "d": "DAV:",
    "c": "urn:ietf:params:xml:ns:caldav",
    "card": "urn:ietf:params:xml:ns:carddav",
    "a": "http://apple.com/ns/ical/",
    "cs": "http://calendarserver.org/ns/",
}
PRINCIPAL_BODY = (
    '<d:propfind xmlns:d="DAV:"><d:prop><d:current-user-principal/></d:prop></d:propfind>'
)


class DavError(Exception):
    pass


class DavAuthError(DavError):
    pass


class DavStatusError(DavError):
    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


class DavThrottled(DavError):
    """iCloud asked this account to back off; no request is sent until `retry_after` passes."""

    def __init__(self, retry_after: float):
        super().__init__(f"iCloud asked to retry in {round(retry_after)} s")
        self.retry_after = retry_after


def home_may_have_moved(exc: DavError) -> bool:
    """Whether to rediscover the home URL after `exc`: only for a client-side HTTP status
    (an account moved to another pNN host). Never after throttling, server errors or network
    failures, where rediscovery would add requests and fix nothing."""
    return isinstance(exc, DavStatusError) and 400 <= exc.status < 500 and exc.status != 429


def retry_after_seconds(value: str | None, now: dt.datetime | None = None) -> float | None:
    """Retry-After as seconds: delta-seconds or an HTTP date."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except TypeError, ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.UTC)
    return max(0.0, (when - (now or dt.datetime.now(dt.UTC))).total_seconds())


class Throttle:
    """Pause for one Apple ID after iCloud says to back off. One per account, shared by its
    CalDAV and CardDAV clients, since iCloud throttles them together."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._until = 0.0
        self._lock = threading.Lock()

    def remaining(self) -> float:
        with self._lock:
            return max(0.0, self._until - self._clock())

    def check(self) -> None:
        remaining = self.remaining()
        if remaining > 0:
            raise DavThrottled(remaining)

    def pause(self, seconds: float) -> float:
        seconds = min(max(seconds, 1.0), THROTTLE_MAX_SECONDS)
        with self._lock:
            self._until = max(self._until, self._clock() + seconds)
            return self._until - self._clock()


def text(el: ET.Element | None) -> str | None:
    if el is None or el.text is None:
        return None
    return el.text.strip() or None


class DavClient:
    """One account's connection to one iCloud DAV service. Thread-safe; httpx pools HTTPS."""

    def __init__(
        self,
        user: str,
        password: str,
        *,
        root_url: str,
        transport: httpx.BaseTransport | None = None,
        throttle: Throttle | None = None,
    ):
        self._http = httpx.Client(
            auth=(user, password),
            timeout=TIMEOUT,
            follow_redirects=True,
            transport=transport,
            headers={"User-Agent": "pv-icloud"},
        )
        self._root = httpx.URL(root_url)
        self.throttle = throttle or Throttle()

    def close(self) -> None:
        self._http.close()

    def _request(
        self, method: str, url: httpx.URL, body: str, depth: str
    ) -> tuple[ET.Element, httpx.URL]:
        if method not in READ_METHODS:
            raise RuntimeError(f"refusing non-read DAV method {method}")
        self.throttle.check()
        try:
            resp = self._http.request(
                method,
                url,
                content=body.encode(),
                headers={"Depth": depth, "Content-Type": "application/xml; charset=utf-8"},
            )
        except httpx.HTTPError as exc:
            raise DavError(f"{method} failed: {type(exc).__name__}") from exc
        if resp.status_code in (429, 503):
            delay = retry_after_seconds(resp.headers.get("retry-after"))
            paused = self.throttle.pause(THROTTLE_DEFAULT_SECONDS if delay is None else delay)
            log.warning(
                "%s answered HTTP %d; pausing requests for this account for %d s",
                resp.url.host,
                resp.status_code,
                paused,
            )
            raise DavThrottled(paused)
        if resp.status_code == 401:
            raise DavAuthError("iCloud rejected the credentials")
        if resp.status_code != 207:
            raise DavStatusError(
                f"{method} {resp.url.path}: HTTP {resp.status_code}", resp.status_code
            )
        try:
            return ET.fromstring(resp.content), resp.url
        except ET.ParseError as exc:
            raise DavError(f"{method} {resp.url.path}: malformed XML") from exc

    def _href(self, root: ET.Element, path: str, base: httpx.URL) -> httpx.URL:
        href = text(root.find(path, NS))
        if href is None:
            raise DavError(f"DAV discovery: {path} missing")
        return base.join(href)

    def _discover(self, home_prop: str) -> httpx.URL:
        """Principal, then its home-set property (`c:calendar-home-set`, ...)."""
        root, base = self._request("PROPFIND", self._root, PRINCIPAL_BODY, "0")
        principal = self._href(root, ".//d:current-user-principal/d:href", base)
        prefix, name = home_prop.split(":")
        body = (
            f'<d:propfind xmlns:d="DAV:" xmlns:{prefix}="{NS[prefix]}">'
            f"<d:prop><{home_prop}/></d:prop></d:propfind>"
        )
        root, base = self._request("PROPFIND", principal, body, "0")
        return self._href(root, f".//{home_prop}/d:href", base)
