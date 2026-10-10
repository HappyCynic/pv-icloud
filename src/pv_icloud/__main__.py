# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

"""Entry point: `pv-icloud` (serve) or `pv-icloud hash-token` (new bearer token + hash)."""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import os
import secrets
import sys

from .config import ConfigError, load_config

log = logging.getLogger("pv_icloud")


def bind_host() -> str:
    """PV_ICLOUD_HOST, default loopback; warns when plain HTTP is reachable from elsewhere."""
    host = os.environ.get("PV_ICLOUD_HOST", "127.0.0.1")
    try:
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        log.warning(
            "listening on %s over plain HTTP (no TLS): reach it only through a private network "
            "(LAN, VPN) or a TLS reverse proxy, never directly from the internet. "
            "See the README's Security model.",
            host,
        )
    return host


def main() -> None:
    if sys.argv[1:2] == ["hash-token"]:
        token = secrets.token_urlsafe(32)
        print(f"token:  {token}\nsha256: {hashlib.sha256(token.encode()).hexdigest()}")
        return

    import uvicorn

    from .server import Runtime, configure_logging, create_app

    configure_logging(os.environ.get("PV_ICLOUD_LOG_LEVEL", "INFO"))
    try:
        config = load_config()
    except ConfigError as exc:
        sys.exit(f"pv-icloud: config error: {exc}")
    app = create_app(Runtime(config))
    uvicorn.run(
        app,
        host=bind_host(),
        port=int(os.environ.get("PV_ICLOUD_PORT", "8771")),
        log_level="warning",
    )


if __name__ == "__main__":
    main()
