# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

"""Fetch subscribed (webcal) calendar feeds.

iCloud lists subscriptions over CalDAV with their source URL but serves no events for them, so
the server fetches the feed itself, like Apple clients do. Feed URLs often embed a private
token (TripIt, Google "secret address"), so they are never logged or returned; only the host
is. Requests carry no iCloud credentials, only https (and plain http when the subscription
itself says http) is allowed, and every hop must resolve to a public address so a feed URL
cannot reach the host's LAN. The connection goes to the address that was checked (Host header
and TLS SNI carry the name, so the certificate is still verified against it): a second DNS
answer cannot swap in a private address (DNS rebinding). Parsed feeds are cached in memory
and revalidated with ETag/Last-Modified when the server supports it.
"""

from __future__ import annotations

import contextlib
import ipaddress
import logging
import re
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx
import icalendar

log = logging.getLogger(__name__)

FEED_TTL_SECONDS = 15 * 60
FEED_MAX_BYTES = 20 * 1024 * 1024
FEED_TIMEOUT = 20.0
MAX_REDIRECTS = 5

Resolver = Callable[[str, int], list[str]]


class FeedError(Exception):
    pass


def feed_url(source: str) -> httpx.URL:
    """webcal(s):// → https://; http stays http (the subscriber chose it)."""
    lowered = source.lower()
    if lowered.startswith(("webcal://", "webcals://")):
        source = "https://" + source.split("://", 1)[1]
    url = httpx.URL(source)
    if url.scheme not in ("https", "http") or not url.host:
        raise FeedError("unsupported subscription URL")
    return url


# Durations without the T before hours/minutes/seconds ("P12H" for PT12H). Seen live in a
# feed's REFRESH-INTERVAL (2026-10-09); Apple tolerates it, icalendar rejects the whole feed.
_BAD_DURATION = re.compile(
    rb"^((?:DURATION|TRIGGER|REFRESH-INTERVAL|X-PUBLISHED-TTL)(?:;[^:\r\n]*)?:[+-]?P)"
    rb"(\d+[HMS])",
    re.M | re.I,
)


def parse_feed(body: bytes) -> icalendar.Calendar:
    """Parse a feed, repairing common publisher mistakes. Raises ValueError."""
    if not body.strip():
        return icalendar.Calendar()  # a subscription with nothing scheduled
    body = _BAD_DURATION.sub(rb"\1T\2", body)
    return icalendar.Calendar.from_ical(body)


def _default_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def _check_public(url: httpx.URL, resolve: Resolver) -> list[str]:
    """The host's addresses, all public; raises FeedError otherwise."""
    try:
        addrs = resolve(url.host, url.port or (443 if url.scheme == "https" else 80))
    except OSError as exc:
        raise FeedError(f"{url.host}: DNS lookup failed") from exc
    if not addrs:
        raise FeedError(f"{url.host}: DNS lookup failed")
    public: list[str] = []
    for addr in addrs:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
        if not ip.is_global or ip.is_multicast:
            raise FeedError(f"{url.host}: resolves to a non-public address; refused")
        if str(ip) not in public:
            public.append(str(ip))
    return public


@dataclass
class _Entry:
    checked_at: float
    calendar: icalendar.Calendar
    etag: str | None
    last_modified: str | None


class FeedClient:
    """Shared by all accounts: holds no credentials, and cache keys are full feed URLs."""

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        resolver: Resolver = _default_resolver,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._http = httpx.Client(
            timeout=FEED_TIMEOUT,
            follow_redirects=False,  # each hop is checked before it is followed
            trust_env=False,  # no proxy: the connection must go to the checked address
            transport=transport,
            headers={"User-Agent": "pv-icloud", "Accept": "text/calendar, */*;q=0.5"},
        )
        self._resolve = resolver
        self._clock = clock
        self._cache: dict[str, _Entry] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def close(self) -> None:
        self._http.close()

    def _lock(self, key: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    def get(self, source: str) -> icalendar.Calendar:
        url = feed_url(source)
        key = str(url)
        with self._lock(key):  # one fetch per feed at a time; others wait for the result
            entry = self._cache.get(key)
            if entry and self._clock() - entry.checked_at < FEED_TTL_SECONDS:
                return entry.calendar
            try:
                fresh = self._fetch(url, entry)
            except FeedError:
                if entry is not None:  # serve the last good copy; retry after another TTL
                    entry.checked_at = self._clock()
                    log.warning("feed %s failed; serving cached copy", url.host)
                    return entry.calendar
                raise
            self._cache[key] = fresh
            return fresh.calendar

    def _fetch(self, url: httpx.URL, cached: _Entry | None) -> _Entry:
        headers: dict[str, str] = {}
        if cached and cached.etag:
            headers["If-None-Match"] = cached.etag
        if cached and cached.last_modified:
            headers["If-Modified-Since"] = cached.last_modified
        for _ in range(MAX_REDIRECTS + 1):
            addrs = _check_public(url, self._resolve)
            try:
                with contextlib.closing(self._open(url, addrs, headers)) as resp:
                    if resp.is_redirect and "location" in resp.headers:
                        nxt = url.join(resp.headers["location"])
                        if nxt.scheme not in ("https", "http") or (
                            url.scheme == "https" and nxt.scheme == "http"
                        ):
                            raise FeedError(f"{url.host}: refused redirect off https")
                        url = nxt
                        continue
                    if resp.status_code == 304 and cached is not None:
                        cached.checked_at = self._clock()
                        return cached
                    if resp.status_code != 200:
                        raise FeedError(f"{url.host}: HTTP {resp.status_code}")
                    body = bytearray()
                    for chunk in resp.iter_bytes():
                        body += chunk
                        if len(body) > FEED_MAX_BYTES:
                            raise FeedError(f"{url.host}: feed larger than 20 MB")
                    etag = resp.headers.get("etag")
                    last_modified = resp.headers.get("last-modified")
            except httpx.HTTPError as exc:
                raise FeedError(f"{url.host}: {type(exc).__name__}") from exc
            try:
                calendar = parse_feed(bytes(body))
            except ValueError as exc:
                raise FeedError(f"{url.host}: not an iCalendar feed") from exc
            return _Entry(self._clock(), calendar, etag, last_modified)
        raise FeedError(f"{url.host}: too many redirects")

    def _open(self, url: httpx.URL, addrs: list[str], headers: dict[str, str]) -> httpx.Response:
        """Stream a GET from the first checked address that accepts a connection."""
        failure: httpx.HTTPError | None = None
        for addr in addrs:
            request = self._http.build_request(
                "GET",
                url.copy_with(host=addr),
                headers={**headers, "Host": url.netloc.decode("ascii")},
                extensions={"sni_hostname": url.host},
            )
            try:
                return self._http.send(request, stream=True)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                failure = exc
        assert failure is not None
        raise failure
