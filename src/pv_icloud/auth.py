# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

"""Bearer token -> account resolution (ADR-0003).

Tokens are configured as SHA-256 hashes. The presented token is hashed and compared in
constant time against every configured hash, so lookup time does not depend on which (if
any) hash matched.
"""

from __future__ import annotations

import contextvars
import hashlib
import hmac
import json
from dataclasses import dataclass, field

from starlette.types import ASGIApp, Receive, Scope, Send

from .config import MAX_WRITES_PER_HOUR, Config


@dataclass(frozen=True)
class AccountContext:
    account_id: str
    token_label: str
    # The token's own mail write permissions (ADR-0004); empty for a read-only token.
    mail_write: frozenset[str] = frozenset()
    protected_folders: tuple[str, ...] = ()
    max_writes_per_hour: int = MAX_WRITES_PER_HOUR
    send_to: tuple[str, ...] | None = None  # mail_send's recipient allowlist; None: any
    # The token's own calendar write grant and protected calendars (ADR-0005).
    calendar_write: bool = False
    protected_calendars: tuple[str, ...] = ()
    # Keys the per-token write limit; the configured hash, so never shown in repr or logs.
    token_key: str = field(default="", repr=False)


_current: contextvars.ContextVar[AccountContext | None] = contextvars.ContextVar(
    "pv_icloud_account", default=None
)


def current_account() -> AccountContext:
    ctx = _current.get()
    if ctx is None:
        # Only reachable if a tool is invoked outside the authenticated HTTP path.
        raise PermissionError("no authenticated account for this request")
    return ctx


class TokenTable:
    def __init__(self, config: Config):
        self._entries = [
            (
                bytes.fromhex(t.sha256),
                AccountContext(
                    acct.id,
                    t.label,
                    mail_write=t.mail_write,
                    protected_folders=t.protected_folders,
                    max_writes_per_hour=t.max_writes_per_hour,
                    send_to=t.send_to,
                    calendar_write=t.calendar_write,
                    protected_calendars=t.protected_calendars,
                    token_key=t.sha256,
                ),
            )
            for acct in config.accounts
            for t in acct.tokens
        ]

    def resolve(self, token: str) -> AccountContext | None:
        digest = hashlib.sha256(token.encode()).digest()
        found = None
        for candidate, ctx in self._entries:
            if hmac.compare_digest(digest, candidate):
                found = ctx
        return found


def _bearer(scope: Scope) -> str | None:
    for key, value in scope.get("headers") or []:
        if key == b"authorization":
            scheme, _, token = value.decode("latin-1").partition(" ")
            if scheme.lower() == "bearer" and token.strip():
                return token.strip()
            return None
    return None


class BearerAuthMiddleware:
    """Rejects unauthenticated HTTP requests (except exempt paths) and binds AccountContext."""

    def __init__(self, app: ASGIApp, tokens: TokenTable, exempt_paths: frozenset[str]):
        self.app = app
        self.tokens = tokens
        self.exempt_paths = exempt_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in self.exempt_paths:
            await self.app(scope, receive, send)
            return
        token = _bearer(scope)
        ctx = self.tokens.resolve(token) if token else None
        if ctx is None:
            body = json.dumps({"error": "unauthorized"}).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                        (b"www-authenticate", b'Bearer realm="pv-icloud"'),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        reset = _current.set(ctx)
        try:
            await self.app(scope, receive, send)
        finally:
            _current.reset(reset)


def bind_account(ctx: AccountContext) -> contextvars.Token:
    """Test/CLI helper: bind an account outside the HTTP middleware."""
    return _current.set(ctx)
