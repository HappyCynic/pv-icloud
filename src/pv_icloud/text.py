# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

"""Text matching shared by mail, calendar and contacts."""

from __future__ import annotations

import unicodedata


def fold(text: str) -> str:
    """Casefold and drop accents, so "José" matches "jose" and "MÜLLER" matches "muller"."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))
