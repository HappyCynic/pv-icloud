# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
OWNER = "@mwklein"
# Owned before they exist: the only modules allowed to change mail (ADR-0004), created by the
# add-mail-write-core change. Remove a path from here once it exists.
PLANNED_PATHS = {"/src/pv_icloud/mail/smtp.py"}


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


# --- IMAP and DAV call sites (CLAUDE.md hard rules, ADR-0004) ----------------------------
#
# These inspect the code's call sites rather than its text: SQL `DELETE` and the response
# code `COPYUID` are legitimate words elsewhere.

MAIL_DIR = ROOT / "src" / "pv_icloud" / "mail"
WRITE_MODULE = "imap_write.py"
# imaplib methods that send a command able to change mail or folders, flagged on any
# receiver. Names that are also everyday Python methods (list.append, dict.copy, our own
# ImapClient.close(), which logs out) are flagged only on an imaplib connection.
_FORBIDDEN_METHODS = frozenset(
    {"store", "expunge", "move", "rename", "subscribe", "unsubscribe", "xatom"}
    | {"_simple_command", "_command"}
)
_FORBIDDEN_ON_CONN = frozenset({"copy", "create", "delete", "close"})
_UID_READ = frozenset({"SEARCH", "FETCH"})
_UID_WRITE = frozenset({"COPY", "STORE", "EXPUNGE"})
# The only flags a new message may be created with: a draft (add-mail-draft).
_APPEND_FLAGS = "(\\Draft \\Seen)"


def _calls(path: Path) -> list[ast.Call]:
    tree = ast.parse(path.read_text(), filename=str(path))
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)]


def _method(call: ast.Call) -> str | None:
    return call.func.attr if isinstance(call.func, ast.Attribute) else None


def _receiver_is_conn(call: ast.Call) -> bool:
    """True for `conn.x()`, `self.conn.x()`, `client.conn.x()` and `self._c().x()`."""
    assert isinstance(call.func, ast.Attribute)
    recv = call.func.value
    if isinstance(recv, ast.Call):
        return isinstance(recv.func, ast.Attribute) and recv.func.attr == "_c"
    name = recv.attr if isinstance(recv, ast.Attribute) else getattr(recv, "id", "")
    return name.endswith("conn")


def _const(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _is_single_uid(node: ast.AST) -> bool:
    """`str(<name>)`: one integer UID, never a set or a range."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "str"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Name | ast.Attribute)
    )


def _imap_violations(path: Path) -> list[str]:
    in_write = path.name == WRITE_MODULE
    out: list[str] = []
    for call in _calls(path):
        method = _method(call)
        where = f"{path.name}:{call.lineno}"
        on_conn = method in _FORBIDDEN_ON_CONN and _receiver_is_conn(call)
        if method in _FORBIDDEN_METHODS or on_conn:
            out.append(f"{where}: .{method}() is forbidden")
        elif method == "select":
            readonly = {k.arg: k.value for k in call.keywords}.get("readonly")
            examine = isinstance(readonly, ast.Constant) and readonly.value is True
            if not examine and not in_write:
                out.append(f"{where}: SELECT outside {WRITE_MODULE}")
        elif method == "uid":
            out.extend(_uid_violations(call, where, in_write))
        elif method == "append" and _receiver_is_conn(call):
            out.extend(_append_violations(call, where, in_write))
    return out


def _append_violations(call: ast.Call, where: str, in_write: bool) -> list[str]:
    """APPEND only creates a draft: in the write module, flags exactly `(\\Draft \\Seen)`."""
    if not in_write:
        return [f"{where}: APPEND outside {WRITE_MODULE}"]
    flags = call.args[1] if len(call.args) > 1 else None
    if len(call.args) != 4 or call.keywords or flags is None or _const(flags) != _APPEND_FLAGS:
        return [f"{where}: APPEND may only save a draft with flags {_APPEND_FLAGS}"]
    return []


def _uid_violations(call: ast.Call, where: str, in_write: bool) -> list[str]:
    command = _const(call.args[0]) if call.args else None
    args = call.args[1:]
    if command is None:
        return [f"{where}: UID command must be a string literal"]
    command = command.upper()
    if command in _UID_READ:
        # After SELECT a non-PEEK body fetch would set \Seen: the write module fetches flags.
        flags_only = len(args) == 2 and _is_single_uid(args[0]) and _const(args[1]) == "(UID FLAGS)"
        if command == "FETCH" and in_write and not flags_only:
            return [f"{where}: {WRITE_MODULE} may only UID FETCH <uid> (UID FLAGS)"]
        return []
    if command not in _UID_WRITE:
        return [f"{where}: UID {command} is forbidden"]
    if not in_write:
        return [f"{where}: UID {command} outside {WRITE_MODULE}"]
    if not args or not _is_single_uid(args[0]):
        return [f"{where}: UID {command} must name exactly one UID as str(uid)"]
    if command == "STORE" and [_const(a) for a in args[1:]] != ["+FLAGS.SILENT", "(\\Deleted)"]:
        return [f"{where}: UID STORE may only add \\Deleted silently"]
    if command == "EXPUNGE" and len(args) != 1:
        return [f"{where}: UID EXPUNGE takes exactly one UID"]
    return []


def test_imap_call_sites_follow_the_write_rules() -> None:
    files = sorted(MAIL_DIR.glob("*.py"))
    assert MAIL_DIR / "imap.py" in files
    violations = [v for path in files for v in _imap_violations(path)]
    assert violations == []


def test_imap_write_module_exists_and_uses_every_write_form() -> None:
    # Guards the checks above against passing vacuously (e.g. after a rename).
    calls = _calls(MAIL_DIR / WRITE_MODULE)
    uid_commands = {_const(c.args[0]) for c in calls if _method(c) == "uid" and c.args}
    assert {"COPY", "STORE", "EXPUNGE", "FETCH"} <= uid_commands
    assert any(_method(c) == "select" and not c.keywords for c in calls)
    appends = [c for c in calls if _method(c) == "append" and _receiver_is_conn(c)]
    assert len(appends) == 1


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("conn.store('1', '+FLAGS', '(\\\\Seen)')", ".store() is forbidden"),
        ("conn.expunge()", ".expunge() is forbidden"),
        ("self.conn.close()", ".close() is forbidden"),
        ("self._c().copy('1', 'X')", ".copy() is forbidden"),
        ("client.conn.delete('X')", ".delete() is forbidden"),
        ("conn.select('INBOX')", "SELECT outside"),
        ("conn.uid('MOVE', '1', 'X')", "UID MOVE is forbidden"),
        ("conn.uid(cmd, '1')", "string literal"),
        ("conn.uid('STORE', str(uid), '+FLAGS.SILENT', '(\\\\Deleted)')", "outside"),
        ("conn.append('Drafts', '(\\\\Draft \\\\Seen)', None, msg)", "APPEND outside"),
        ("self._c().append('Drafts', '(\\\\Draft \\\\Seen)', None, msg)", "APPEND outside"),
    ],
)
def test_imap_policy_catches_read_path_violations(tmp_path: Path, source: str, message: str):
    path = tmp_path / "imap.py"
    path.write_text(source + "\n")
    [violation] = _imap_violations(path)
    assert message in violation


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("conn.uid('COPY', '1:5', dest)", "exactly one UID"),
        ("conn.uid('COPY', '1,2', dest)", "exactly one UID"),
        ("conn.uid('STORE', str(uid), '+FLAGS', '(\\\\Deleted)')", "only add \\Deleted"),
        ("conn.uid('STORE', str(uid), '-FLAGS.SILENT', '(\\\\Deleted)')", "only add \\Deleted"),
        ("conn.uid('STORE', str(uid), '+FLAGS.SILENT', '(\\\\Seen)')", "only add \\Deleted"),
        ("conn.uid('EXPUNGE', uid_set(uids))", "exactly one UID"),
        ("conn.uid('FETCH', str(uid), '(BODY[])')", "UID FETCH <uid> (UID FLAGS)"),
        ("conn.expunge()", ".expunge() is forbidden"),
        ("conn.append('Drafts', None, None, b'')", "only save a draft"),
        ("conn.append('Drafts', '(\\\\Seen)', None, msg)", "only save a draft"),
        ("conn.append('Drafts', '(\\\\Draft)', None, msg)", "only save a draft"),
        ("conn.append('Drafts', '(\\\\Draft \\\\Seen \\\\Flagged)', None, msg)", "only save"),
        ("conn.append('Drafts', flags, None, msg)", "only save a draft"),
        ("conn.append('Drafts')", "only save a draft"),
        ("conn.append('Drafts', flags=FLAGS, date_time=None, message=msg)", "only save"),
    ],
)
def test_imap_policy_catches_write_path_violations(tmp_path: Path, source: str, message: str):
    path = tmp_path / WRITE_MODULE
    path.write_text(source + "\n")
    [violation] = _imap_violations(path)
    assert message in violation


def test_imap_policy_allows_saving_a_draft(tmp_path: Path) -> None:
    path = tmp_path / WRITE_MODULE
    path.write_text("conn.append(quote(DRAFTS), '(\\\\Draft \\\\Seen)', None, message)\n")
    assert _imap_violations(path) == []
    # An everyday list.append() is not an IMAP command.
    path.write_text("rows.append(row)\n")
    assert _imap_violations(path) == []


def test_dav_sends_only_propfind_and_report() -> None:
    from pv_icloud.dav import READ_METHODS

    assert frozenset({"PROPFIND", "REPORT"}) == READ_METHODS
    dav_files = [ROOT / "src" / "pv_icloud" / "dav.py"]
    dav_files += sorted((ROOT / "src" / "pv_icloud" / "cal").glob("*.py"))
    dav_files += sorted((ROOT / "src" / "pv_icloud" / "contacts").glob("*.py"))
    for path in dav_files:
        for call in _calls(path):
            if _method(call) == "_request":
                assert _const(call.args[0]) in READ_METHODS, f"{path.name}:{call.lineno}"


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
