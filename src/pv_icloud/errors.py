# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

"""Errors shared by the mail and calendar tools."""

from __future__ import annotations


class ToolInputError(ValueError):
    """Bad arguments or an id that cannot be served; message is safe to show the caller."""


class WriteFailed(Exception):
    """A mail or calendar write failed upstream. The message says where the message (or
    event) was left and is safe to show the caller (and to log): it names no folder, address,
    calendar or content."""

    @property
    def log_text(self) -> str:
        return str(self)


class SeriesWriteFailed(WriteFailed):
    """A calendar series edit stopped after it had changed something. The message names
    what was changed (by uid and first occurrence, or event id) for the caller; `log_text`
    says only how far it got, for the log."""

    def __init__(self, message: str, log_text: str):
        super().__init__(message)
        self._log_text = log_text

    @property
    def log_text(self) -> str:
        return self._log_text
