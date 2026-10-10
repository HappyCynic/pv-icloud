# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

"""Errors shared by the mail and calendar tools."""

from __future__ import annotations


class ToolInputError(ValueError):
    """Bad arguments or an id that cannot be served; message is safe to show the caller."""
