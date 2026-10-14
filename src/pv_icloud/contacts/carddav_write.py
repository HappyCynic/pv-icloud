# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

"""The only code that changes iCloud contacts (docs/adr/0006-opt-in-contact-write.md).

Imported only when PV_ICLOUD_CONTACTS_WRITE=enabled. It sends exactly two methods, each for
one card (`<name>.vcf`, a direct child of the account's address book, which is a direct
child of the account's discovered address book home):

- `PUT` with `If-None-Match: *` (a new card) or `If-Match: <ETag>` (an update, with the ETag
  of a read of that card made in the same call);
- `DELETE` with `If-Match: <ETag>`.

A request without a precondition is refused before it is sent. A 412 (the card changed
since it was read) is reported, never retried; nothing is retried. 429/503 pause the
account's CalDAV and CardDAV requests through the shared `Throttle`, as reads do. Logs carry
the HTTP status only, never a path, UID or card text.
"""

from __future__ import annotations

import logging
import urllib.parse

import httpx

from ..dav import THROTTLE_DEFAULT_SECONDS, DavAuthError, DavThrottled, retry_after_seconds
from ..errors import ToolInputError, WriteFailed
from .carddav import CardDavClient

log = logging.getLogger(__name__)

CHANGED = "the contact changed in Contacts; read it again"
GONE = "contact not found (deleted, hidden, or a stale id)"
REFUSED = "iCloud refused the change; nothing was changed"
NOT_SENT = "iCloud could not be reached; nothing was changed"
UNCONFIRMED = "iCloud did not confirm the change; read the contact again before retrying"


class Changed(ToolInputError):
    """HTTP 412: the card changed in Contacts since it was read."""

    def __init__(self) -> None:
        super().__init__(CHANGED)


class Gone(ToolInputError):
    """HTTP 404: the card no longer exists."""

    def __init__(self) -> None:
        super().__init__(GONE)


class CardDavWriter:
    """Writes for one account, over its CardDavClient's connection, credentials and pause."""

    METHODS = frozenset({"PUT", "DELETE"})

    def __init__(self, client: CardDavClient):
        self._client = client

    def create(self, book: str, name: str, text: str) -> str:
        """Store a new card `name` in `book`; returns its path."""
        path = httpx.URL(book).raw_path.decode("ascii").rstrip("/") + "/" + name
        self._write("PUT", book, path, text, {"If-None-Match": "*"})
        return path

    def update(self, book: str, path: str, text: str, etag: str) -> None:
        self._write("PUT", book, path, text, {"If-Match": etag})

    def delete(self, book: str, path: str, etag: str) -> None:
        self._write("DELETE", book, path, None, {"If-Match": etag})

    def _resource_url(self, book: str, path: str) -> httpx.URL:
        """The card's URL, only for a direct `.vcf` child of an address book that is itself a
        direct child of the account's address book home."""
        home = self._client.home()
        collection = httpx.URL(book)
        home_path = home.raw_path.decode("ascii").rstrip("/") + "/"
        book_path = collection.raw_path.decode("ascii").rstrip("/") + "/"
        book_name = book_path[len(home_path) :].rstrip("/")
        name = urllib.parse.unquote(path[len(book_path) :]) if path.startswith(book_path) else ""
        if (
            collection.host != home.host
            or not book_path.startswith(home_path)
            or not book_name
            or "/" in book_name
            or not name
            or "/" in name
            or "\\" in name
            or not name.endswith(".vcf")
            or name in (".vcf", "..vcf")
            or "?" in path
            or "#" in path
        ):
            raise RuntimeError("refusing a write outside the address book")
        return collection.copy_with(raw_path=path.encode("ascii"))

    def _write(
        self,
        method: str,
        book: str,
        path: str,
        body: str | None,
        precondition: dict[str, str],
    ) -> None:
        if method not in self.METHODS:
            raise RuntimeError(f"refusing {method}: contact writes send only PUT and DELETE")
        if method == "PUT" and "If-None-Match" in precondition:
            allowed = precondition == {"If-None-Match": "*"}
        else:
            allowed = set(precondition) == {"If-Match"} and bool(precondition["If-Match"])
        if not allowed:
            raise RuntimeError(
                f"refusing {method} without one precondition (If-Match, or If-None-Match: *)"
            )
        url = self._resource_url(book, path)
        client = self._client
        client.throttle.check()
        headers = dict(precondition)
        if body is not None:
            headers["Content-Type"] = "text/vcard; charset=utf-8"
        try:
            resp = client._http.request(
                method, url, content=body.encode() if body is not None else None, headers=headers
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            log.warning("contact %s not sent: %s", method, type(exc).__name__)
            raise WriteFailed(NOT_SENT) from exc
        except httpx.HTTPError as exc:  # sent, but the answer was lost
            log.warning("contact %s unconfirmed: %s", method, type(exc).__name__)
            raise WriteFailed(UNCONFIRMED) from exc
        status = resp.status_code
        if status in (429, 503):
            delay = retry_after_seconds(resp.headers.get("retry-after"))
            paused = client.throttle.pause(THROTTLE_DEFAULT_SECONDS if delay is None else delay)
            log.warning("contact %s answered HTTP %d; pausing for %d s", method, status, paused)
            raise DavThrottled(paused)
        if status in (200, 201, 204):
            return
        log.warning("contact %s answered HTTP %d", method, status)
        if status == 401:
            raise DavAuthError("iCloud rejected the credentials")
        if status == 412:
            raise Changed()
        if status == 404:
            raise Gone()
        if 400 <= status < 500:
            raise WriteFailed(REFUSED)
        raise WriteFailed(UNCONFIRMED)
