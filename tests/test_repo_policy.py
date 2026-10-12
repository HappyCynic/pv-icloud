# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
OWNER = "@mwklein"
# Owned before they exist: the only modules allowed to change mail (ADR-0004), created by the
# add-mail-write-core change. Remove a path from here once it exists.
PLANNED_PATHS = {"/src/pv_icloud/mail/imap_write.py", "/src/pv_icloud/mail/smtp.py"}


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
        "/src/pv_icloud/mail/imap_write.py",
        "/src/pv_icloud/mail/smtp.py",
        "/CLAUDE.md",
        "/.claude/",
        "/openspec/config.yaml",
    ],
)
def test_codeowners_lists_sensitive_paths(pattern: str) -> None:
    assert _codeowners().get(pattern) == [OWNER]


def test_codeowners_paths_exist() -> None:
    for pattern in _codeowners():
        if pattern != "*" and pattern not in PLANNED_PATHS:
            assert (ROOT / pattern.lstrip("/")).exists(), pattern


def test_planned_paths_are_still_planned() -> None:
    # Once a planned module exists, it must be dropped from PLANNED_PATHS so the check above
    # covers it again.
    for pattern in PLANNED_PATHS:
        exists = (ROOT / pattern.lstrip("/")).exists()
        assert not exists, f"{pattern} exists; drop it from PLANNED_PATHS"


def test_claude_md_keeps_the_write_rule() -> None:
    text = " ".join(_claude_md().split())  # independent of line wrapping
    for phrase in (
        "Read path stays read-only",
        "`PROPFIND` and `REPORT`",
        "PV_ICLOUD_MAIL_WRITE=enabled",
        "src/pv_icloud/mail/imap_write.py",
        "src/pv_icloud/mail/smtp.py",
        "`UID STORE <uid> +FLAGS.SILENT (\\Deleted)`",
        "`UID EXPUNGE <uid>`",
        "Never permanently delete mail",
        "Changing this list needs a new ADR first",
        "docs/adr/0004-opt-in-mail-write.md",
    ):
        assert phrase in text, phrase


def test_write_rule_is_summarised_and_documented() -> None:
    config = (ROOT / "openspec" / "config.yaml").read_text()
    assert "PV_ICLOUD_MAIL_WRITE=enabled" in config and "0004-opt-in-mail-write" in config
    security = (ROOT / "SECURITY.md").read_text()
    assert "Read-only by default" in security and "PV_ICLOUD_MAIL_WRITE=enabled" in security
    assert (ROOT / "docs" / "adr" / "0004-opt-in-mail-write.md").exists()


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


def _claude_md() -> str:
    return (ROOT / "CLAUDE.md").read_text()


def _openspec_pin() -> str:
    match = re.search(r"@fission-ai/openspec@(\d+\.\d+\.\d+)", _claude_md())
    assert match, "CLAUDE.md must pin the OpenSpec version"
    return match.group(1)


def test_claude_md_describes_the_issue_workflow() -> None:
    text = _claude_md()
    for phrase in ("agent:propose", "agent:implement", "draft", "Never** merge, approve"):
        assert phrase in text, phrase
    # Tests for code changes; changelog only for what users notice (as in CONTRIBUTING.md).
    assert "Every code change includes tests" in text
    assert "Anything a user would notice" in text


def test_generated_openspec_files_match_the_pinned_version() -> None:
    pin = _openspec_pin()
    generated = sorted((ROOT / ".claude").glob("skills/openspec-*/SKILL.md"))
    generated += sorted((ROOT / ".claude" / "commands" / "opsx").glob("*.md"))
    assert generated, "openspec init output is missing"
    for path in generated:
        text = path.read_text()
        versions = set(re.findall(r'generatedBy: "([^"]+)"', text))
        assert versions <= {pin}, (path, versions)
        # Generated workflows may only pre-approve the openspec CLI, nothing broader.
        tools = re.findall(r"^allowed-tools: (.+)$", text, re.M)
        assert set(tools) <= {"Bash(openspec:*)"}, (path, tools)


def test_openspec_config_has_context_and_spdx() -> None:
    text = (ROOT / "openspec" / "config.yaml").read_text()
    assert "SPDX-License-Identifier: Apache-2.0" in text
    assert "schema: spec-driven" in text
    assert "\ncontext: |" in text and "CLAUDE.md" in text


def test_session_start_hook_only_syncs_dependencies() -> None:
    settings = json.loads((ROOT / ".claude" / "settings.json").read_text())
    assert set(settings) == {"hooks"} and set(settings["hooks"]) == {"SessionStart"}
    commands = [h["command"] for g in settings["hooks"]["SessionStart"] for h in g["hooks"]]
    assert commands and all("uv sync --frozen --all-groups" in c for c in commands)
    assert all(c.rstrip().endswith("exit 0") for c in commands)
