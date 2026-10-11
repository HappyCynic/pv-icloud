# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
OWNER = "@mwklein"


def _codeowners() -> dict[str, list[str]]:
    rules: dict[str, list[str]] = {}
    for line in (ROOT / ".github" / "CODEOWNERS").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            pattern, *owners = line.split()
            rules[pattern] = owners
    return rules


@pytest.mark.parametrize(
    "pattern",
    [
        "*",
        "/.github/",
        "/Dockerfile",
        "/pyproject.toml",
        "/uv.lock",
        "/SECURITY.md",
        "/src/pv_icloud/dav.py",
        "/src/pv_icloud/mail/imap.py",
    ],
)
def test_codeowners_lists_sensitive_paths(pattern: str) -> None:
    assert _codeowners().get(pattern) == [OWNER]


def test_codeowners_paths_exist() -> None:
    for pattern in _codeowners():
        if pattern != "*":
            assert (ROOT / pattern.lstrip("/")).exists(), pattern


def test_claude_md_states_checks() -> None:
    text = (ROOT / "CLAUDE.md").read_text()
    for command in (
        "uv sync --frozen --all-groups",
        "uv run pytest",
        "uv run ruff check .",
        "uv run ruff format --check .",
        "uv run mypy",
    ):
        assert command in text, command
