# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

"""The only code that changes iCloud calendars (docs/adr/0005-opt-in-calendar-write.md).

Imported only when PV_ICLOUD_CALENDAR_WRITE=enabled. It sends exactly two methods, each for
one event resource (`<name>.ics`, a direct child of a calendar whose
current-user-privilege-set includes write-content):

- `PUT` with `If-None-Match: *` (a new resource) or `If-Match: <ETag>` (an update, with the
  ETag of a read of that resource made in the same call);
- `DELETE` with `If-Match: <ETag>`.

Every request carries `Schedule-Reply: F`. A request without a precondition is refused
before it is sent. A 412 (the event changed since it was read) is reported, never retried;
nothing is retried. 429/503 pause the account's CalDAV and CardDAV requests through the
shared `Throttle`, as reads do. Logs carry the HTTP status only, never a path, UID or body.
"""

from __future__ import annotations

import logging
import urllib.parse

import httpx

from ..dav import THROTTLE_DEFAULT_SECONDS, DavAuthError, DavThrottled, retry_after_seconds
from ..errors import ToolInputError, WriteFailed
from .caldav import CalDavClient, CalendarInfo

log = logging.getLogger(__name__)

CHANGED = "the event changed in Calendar; read it again"
GONE = "event not found (deleted or moved); list events again"
REFUSED = "iCloud refused the change; nothing was changed"
NOT_SENT = "iCloud could not be reached; nothing was changed"
UNCONFIRMED = "iCloud did not confirm the change; read the event again before retrying"


class CalDavWriter:
    """Writes for one account, over its CalDavClient's connection, credentials and pause."""

    METHODS = frozenset({"PUT", "DELETE"})

    def __init__(self, client: CalDavClient):
        self._client = client

    def create(self, calendar: CalendarInfo, name: str, text: str) -> str:
        """Store a new resource `name` in `calendar`; returns its path."""
        path = httpx.URL(calendar.url).raw_path.decode("ascii").rstrip("/") + "/" + name
        self._write("PUT", calendar, path, text, {"If-None-Match": "*"})
        return path

    def update(self, calendar: CalendarInfo, path: str, text: str, etag: str) -> None:
        self._write("PUT", calendar, path, text, {"If-Match": etag})

    def delete(self, calendar: CalendarInfo, path: str, etag: str) -> None:
        self._write("DELETE", calendar, path, None, {"If-Match": etag})

    @staticmethod
    def _resource_url(calendar: CalendarInfo, path: str) -> httpx.URL:
        """The resource's URL, only for a direct `.ics` child of the calendar collection."""
        collection = httpx.URL(calendar.url)
        prefix = collection.raw_path.decode("ascii").rstrip("/") + "/"
        name = urllib.parse.unquote(path[len(prefix) :]) if path.startswith(prefix) else ""
        if (
            not name
            or "/" in name
            or "\\" in name
            or name in (".", "..")
            or not name.endswith(".ics")
            or "?" in path
            or "#" in path
        ):
            raise ToolInputError("malformed event id")
        return collection.copy_with(raw_path=path.encode("ascii"))

    def _write(
        self,
        method: str,
        calendar: CalendarInfo,
        path: str,
        body: str | None,
        precondition: dict[str, str],
    ) -> None:
        if method not in self.METHODS:
            raise RuntimeError(f"refusing {method}: calendar writes send only PUT and DELETE")
        if method == "PUT" and "If-None-Match" in precondition:
            allowed = precondition == {"If-None-Match": "*"}
        else:
            allowed = set(precondition) == {"If-Match"} and bool(precondition["If-Match"])
        if not allowed:
            raise RuntimeError(
                f"refusing {method} without one precondition (If-Match, or If-None-Match: *)"
            )
        if not calendar.writable or calendar.kind != "calendar":
            raise RuntimeError("refusing a write to a calendar that is not writable")
        url = self._resource_url(calendar, path)
        client = self._client
        client.throttle.check()
        headers = {**precondition, "Schedule-Reply": "F"}
        if body is not None:
            headers["Content-Type"] = "text/calendar; charset=utf-8"
        try:
            resp = client._http.request(
                method, url, content=body.encode() if body is not None else None, headers=headers
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            log.warning("calendar %s not sent: %s", method, type(exc).__name__)
            raise WriteFailed(NOT_SENT) from exc
        except httpx.HTTPError as exc:  # sent, but the answer was lost
            log.warning("calendar %s unconfirmed: %s", method, type(exc).__name__)
            raise WriteFailed(UNCONFIRMED) from exc
        status = resp.status_code
        if status in (429, 503):
            delay = retry_after_seconds(resp.headers.get("retry-after"))
            paused = client.throttle.pause(THROTTLE_DEFAULT_SECONDS if delay is None else delay)
            log.warning("calendar %s answered HTTP %d; pausing for %d s", method, status, paused)
            raise DavThrottled(paused)
        if status in (200, 201, 204):
            return
        log.warning("calendar %s answered HTTP %d", method, status)
        if status == 401:
            raise DavAuthError("iCloud rejected the credentials")
        if status == 412:
            raise ToolInputError(CHANGED)
        if status == 404:
            raise ToolInputError(GONE)
        if 400 <= status < 500:
            raise WriteFailed(REFUSED)
        raise WriteFailed(UNCONFIRMED)
