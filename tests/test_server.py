# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

"""End-to-end over Streamable HTTP: auth, account isolation, audit, health."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from pv_icloud.mail.ids import MailId
from pv_icloud.server import Runtime, create_app

from .conftest import TOKENS, make_config
from .fakecaldav import FakeAccount, FakeCalDav, FakeCalendar, resource, vevent
from .fakecarddav import FakeBook, FakeCardDav, vcard
from .fakeimap import FakeServer, make_message

HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


class Upstreams:
    """Routes each Apple ID to its own fake server, like real iCloud would."""

    def __init__(self):
        self.servers = {"alice@icloud.com": FakeServer(), "bob@icloud.com": FakeServer()}

    def factory(self):
        servers = self.servers

        class Conn:
            def __init__(self):
                self._real = None

            def login(self, user, pw):
                self._real = servers[user].connect()
                self.capabilities = self._real.capabilities
                return self._real.login(user, pw)

            def __getattr__(self, name):
                return getattr(self._real, name)

        return Conn()


@pytest.fixture
def app_env(tmp_path):
    yield from _app_env(make_config(tmp_path))


def _app_env(config):
    ups = Upstreams()
    now = dt.datetime.now(dt.UTC)
    for user, subject in (
        ("alice@icloud.com", "alice secret plans"),
        ("bob@icloud.com", "bob secret plans"),
    ):
        s = ups.servers[user]
        for name in ("INBOX", "Work", "Archive", "Deleted Messages", "Taxes"):
            s.folder(name)
        s.deliver("INBOX", make_message(subject=subject, date=now), now.date())
    tomorrow = dt.datetime.combine(now.date() + dt.timedelta(days=1), dt.time(15))
    caldav = FakeCalDav(
        {
            f"{who}@icloud.com": FakeAccount(
                dsid,
                [
                    FakeCalendar(
                        "home",
                        f"{who} home",
                        resources=[resource(vevent(who, f"{who} secret meeting", tomorrow))],
                    )
                ],
            )
            for who, dsid in (("alice", "1"), ("bob", "2"))
        }
    )
    carddav = FakeCardDav(
        {
            f"{who}@icloud.com": FakeBook(
                dsid,
                cards={
                    f"{who}-friend.vcf": vcard(
                        f"{who}-friend",
                        f"EMAIL;type=INTERNET:{who}@example.com",
                        fn=f"{who} secret friend",
                    )
                },
            )
            for who, dsid in (("alice", "1"), ("bob", "2"))
        }
    )
    runtime = Runtime(
        config,
        conn_factory=ups.factory,
        start_sync=False,
        caldav_transport=caldav.transport(),
        carddav_transport=carddav.transport(),
    )
    runtime.caldav = caldav  # type: ignore[attr-defined]  # for tests that break it
    runtime.carddav = carddav  # type: ignore[attr-defined]
    runtime.upstreams = ups  # type: ignore[attr-defined]
    for rt in runtime.accounts.values():
        client = rt.sync._client_factory()
        rt.sync.full_pass(client)
        client.close()
    with TestClient(create_app(runtime)) as client:
        yield client, runtime


def rpc(client, method, params=None, token=None):
    headers = dict(HEADERS)
    if token:
        headers["authorization"] = f"Bearer {token}"
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    return client.post("/mcp", headers=headers, json=body)


def call_tool(client, token, name, args=None):
    resp = rpc(client, "tools/call", {"name": name, "arguments": args or {}}, token)
    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    return result


def test_missing_or_unknown_bearer_is_401(app_env):
    client, _ = app_env
    assert rpc(client, "tools/list").status_code == 401
    assert rpc(client, "tools/list", token="wrong").status_code == 401
    r = client.post("/mcp", headers={**HEADERS, "authorization": "Basic abc"}, json={})
    assert r.status_code == 401


def test_healthz_is_open_but_minimal(app_env):
    client, _ = app_env
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
    # A wrong token gets the same minimal answer, not a 401.
    r = client.get("/healthz", headers={"authorization": "Bearer wrong"})
    assert r.json() == {"status": "ok"}


def test_healthz_detail_only_for_the_callers_account(app_env):
    client, _ = app_env
    r = client.get("/healthz", headers={"authorization": f"Bearer {TOKENS['alice']}"})
    body = r.json()
    assert body["status"] == "ok"
    assert body["account"]["id"] == "alice"
    assert {"mail_sync", "indexed"} <= set(body["account"])
    assert "bob" not in r.text


def test_tool_surface_is_read_only(app_env):
    client, _ = app_env
    tools = rpc(client, "tools/list", token=TOKENS["alice"]).json()["result"]["tools"]
    assert {t["name"] for t in tools} == {
        "mail_folders",
        "mail_search",
        "mail_get",
        "mail_thread",
        "calendar_list",
        "calendar_events",
        "calendar_search",
        "calendar_event",
        "find_person",
        "contacts_search",
        "contacts_get",
        "contacts_groups",
    }
    for t in tools:
        assert t["annotations"]["readOnlyHint"] is True
    search = next(t for t in tools if t["name"] == "mail_search")
    assert "from" in search["inputSchema"]["properties"]
    assert "untrusted" in search["description"]
    # Every capped tool states its limits and how to page past them.
    by_name = {t["name"]: t for t in tools}
    for name, limit_text in (
        ("mail_search", "max 50 results"),
        ("mail_thread", "max 100 messages"),
        ("calendar_events", "max 200 events"),
        ("mail_get", "20000 body characters"),
        ("calendar_event", "20000 notes characters"),
        ("calendar_search", "max 200 events"),
        ("contacts_search", "max 100 contacts"),
        ("find_person", "max 25 people"),
    ):
        desc = by_name[name]["description"]
        assert limit_text in desc and "next_offset" in desc, name
        assert "offset" in by_name[name]["inputSchema"]["properties"], name
    assert "last 730 days" in by_name["mail_search"]["description"]
    assert "at most 90 days" in by_name["calendar_events"]["description"]
    assert "at most 730 days" in by_name["calendar_search"]["description"]


SNAPSHOT = Path(__file__).parent / "snapshots" / "read_only_surface.json"


def surface(client, token) -> dict:
    """What a client learns before calling anything: instructions and the tool list."""
    init = rpc(
        client,
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
        token,
    ).json()["result"]
    tools = rpc(client, "tools/list", token=token).json()["result"]["tools"]
    return {"instructions": init["instructions"], "tools": tools}


def test_read_only_surface_matches_the_snapshot(app_env):
    # Taken before mail write support existed: a token without `mail_write` must keep seeing
    # exactly this. Regenerate (PV_ICLOUD_UPDATE_SNAPSHOT=1) only for a deliberate change to a
    # read tool or the read-only instructions.
    client, _ = app_env
    got = surface(client, TOKENS["alice"])
    if os.environ.get("PV_ICLOUD_UPDATE_SNAPSHOT"):
        SNAPSHOT.parent.mkdir(exist_ok=True)
        SNAPSHOT.write_text(json.dumps(got, indent=2, sort_keys=True) + "\n")
    assert got == json.loads(SNAPSHOT.read_text())


def test_each_token_sees_only_its_account(app_env):
    client, _ = app_env
    a = call_tool(client, TOKENS["alice"], "mail_search", {"query": "secret"})
    b = call_tool(client, TOKENS["bob"], "mail_search", {"query": "secret"})
    a_res = a["structuredContent"]["results"]
    b_res = b["structuredContent"]["results"]
    assert [r["subject"] for r in a_res] == ["alice secret plans"]
    assert [r["subject"] for r in b_res] == ["bob secret plans"]
    # Alice's id presented with Bob's token resolves against Bob's mailbox, never Alice's.
    got = call_tool(client, TOKENS["bob"], "mail_get", {"id": a_res[0]["id"]})
    text = json.dumps(got)
    assert "alice secret plans" not in text


def test_result_envelope_and_from_alias(app_env):
    client, _ = app_env
    out = call_tool(
        client, TOKENS["alice"], "mail_search", {"from": "alice@example.com", "limit": 5}
    )["structuredContent"]
    assert out["content_trust"] == "untrusted"
    assert out["provenance"]["source"] == "icloud-mail"
    assert len(out["results"]) == 1


def test_bad_input_is_tool_error(app_env):
    client, _ = app_env
    out = call_tool(client, TOKENS["alice"], "mail_get", {"id": "%%%"})
    assert out["isError"] is True


def test_audit_line_per_call(app_env, caplog):
    client, _ = app_env
    with caplog.at_level(logging.INFO, logger="pv_icloud.audit"):
        call_tool(client, TOKENS["alice"], "mail_search", {"query": "secret plans"})
    [line] = [json.loads(r.message) for r in caplog.records if r.name == "pv_icloud.audit"]
    assert line["account_id"] == "alice" and line["token_label"] == "laptop"
    assert line["tool"] == "mail_search" and line["result_count"] == 1
    assert "secret" not in json.dumps(line)
    assert TOKENS["alice"] not in json.dumps(line)


def test_calendar_tools_per_account(app_env):
    client, _ = app_env
    for who in ("alice", "bob"):
        cals = call_tool(client, TOKENS[who], "calendar_list")["structuredContent"]
        assert [c["name"] for c in cals["calendars"]] == [f"{who} home"]
        out = call_tool(client, TOKENS[who], "calendar_events", {"query": "secret"})
        events = out["structuredContent"]
        assert [e["title"] for e in events["events"]] == [f"{who} secret meeting"]
        assert events["provenance"]["source"] == "icloud-calendar"
        assert events["content_trust"] == "untrusted"
        one = call_tool(client, TOKENS[who], "calendar_event", {"id": events["events"][0]["id"]})[
            "structuredContent"
        ]
        assert one["title"] == f"{who} secret meeting"
    # An id from Alice's calendar resolves against Bob's account, never Alice's.
    alice_id = call_tool(client, TOKENS["alice"], "calendar_events")["structuredContent"]
    out = call_tool(client, TOKENS["bob"], "calendar_event", {"id": alice_id["events"][0]["id"]})
    assert out["isError"] is True and "alice" not in json.dumps(out)


def test_calendar_upstream_failure_is_generic_tool_error(app_env):
    client, runtime = app_env
    runtime.caldav.password = "rotated"
    out = call_tool(client, TOKENS["alice"], "calendar_list")
    assert out["isError"] is True
    assert "unavailable" in out["content"][0]["text"]


def test_contacts_and_find_person_per_account(app_env):
    client, _ = app_env
    for who in ("alice", "bob"):
        out = call_tool(client, TOKENS[who], "contacts_search", {"query": "secret"})
        found = out["structuredContent"]
        assert [c["name"] for c in found["contacts"]] == [f"{who} secret friend"]
        assert found["provenance"]["source"] == "icloud-contacts"
        assert found["content_trust"] == "untrusted"
        people = call_tool(client, TOKENS[who], "find_person", {"query": "secret friend"})
        [person] = people["structuredContent"]["people"]
        # The card, linked to the account's one test message through the card's address.
        assert person["contact_id"] == f"{who}-friend" and person["mail"]["messages"] == 1
        groups = call_tool(client, TOKENS[who], "contacts_groups")["structuredContent"]
        assert groups["contacts"] == 1
    # Alice's contact id presented with Bob's token resolves against Bob's book only.
    out = call_tool(client, TOKENS["bob"], "contacts_get", {"id": "alice-friend"})
    assert out["isError"] is True and "alice secret" not in json.dumps(out)


def test_contacts_audit_counts(app_env, caplog):
    client, _ = app_env
    with caplog.at_level(logging.INFO, logger="pv_icloud.audit"):
        call_tool(client, TOKENS["alice"], "contacts_groups")
        call_tool(client, TOKENS["alice"], "contacts_get", {"id": "alice-friend"})
        call_tool(client, TOKENS["alice"], "find_person", {"query": "secret friend"})
    lines = [json.loads(r.message) for r in caplog.records if r.name == "pv_icloud.audit"]
    assert [(x["tool"], x["result_count"]) for x in lines] == [
        ("contacts_groups", 0),
        ("contacts_get", 1),
        ("find_person", 1),
    ]
    assert "secret" not in json.dumps(lines) and "alice-friend" not in json.dumps(lines)


def test_throttled_account_gets_a_retry_hint_and_others_are_unaffected(app_env):
    client, runtime = app_env
    book = runtime.carddav.books["alice@icloud.com"]
    book.fail, book.retry_after = 503, "1500"
    for tool in ("contacts_search", "calendar_list"):  # one pause covers CalDAV and CardDAV
        out = call_tool(client, TOKENS["alice"], tool)
        assert out["isError"] is True
        assert "try again in about 25 minutes" in out["content"][0]["text"], tool
    ok = call_tool(client, TOKENS["bob"], "contacts_search")["structuredContent"]
    assert ok["total"] == 1


# --- mail writes (ADR-0004) ---------------------------------------------------------------

READ_TOOLS = {
    "mail_folders", "mail_search", "mail_get", "mail_thread", "calendar_list",
    "calendar_events", "calendar_search", "calendar_event", "find_person",
    "contacts_search", "contacts_get", "contacts_groups",
}  # fmt: skip
WRITE_TOKENS = {
    "agent": {"mail_write": ["move", "archive", "delete"], "max_writes_per_hour": 2},
    "mover": {"mail_write": ["move"], "protected_folders": ["Work"]},
    "archiver": {"mail_write": ["archive"]},
    "deleter": {"mail_write": ["delete"]},
}
WRITE_SEQUENCE = ("SELECT", "UID COPY", "UID STORE", "UID EXPUNGE")


def token(label: str) -> str:
    return f"{label}-token"


@pytest.fixture
def write_env(tmp_path):
    config = make_config(
        tmp_path,
        mail_write=True,
        alice_tokens=list(WRITE_TOKENS.items()),
        alice_write={"protected_folders": ["Taxes"]},
    )
    yield from _app_env(config)


def alice_server(runtime) -> FakeServer:
    return runtime.upstreams.servers["alice@icloud.com"]


def alice_message(client) -> str:
    out = call_tool(client, TOKENS["alice"], "mail_search", {"query": "secret"})
    return out["structuredContent"]["results"][0]["id"]


def write_commands(server: FakeServer) -> list[str]:
    sent = [" ".join(c[:2]) if c[0] == "UID" else c[0] for c in server.commands]
    return [c for c in sent if c in WRITE_SEQUENCE]


def error(out, tool: str) -> str:
    """A tool error's message, without the SDK's `Error executing tool <name>: ` prefix."""
    assert out["isError"] is True
    text = out["content"][0]["text"]
    prefix = f"Error executing tool {tool}: "
    assert text.startswith(prefix), text
    return text.removeprefix(prefix)


def audit_lines(caplog) -> list[dict]:
    return [json.loads(r.message) for r in caplog.records if r.name == "pv_icloud.audit"]


def _imports_write_module(tmp_path, enabled: bool) -> bool:
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "from tests.conftest import make_config\n"
        "from pv_icloud.server import Runtime, build_server\n"
        f"cfg = make_config(Path({str(tmp_path)!r}), mail_write={enabled})\n"
        "build_server(Runtime(cfg, conn_factory=lambda: None, start_sync=False))\n"
        "print('pv_icloud.mail.imap_write' in sys.modules)\n"
    )
    root = Path(__file__).resolve().parent.parent
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=root, capture_output=True, text=True, check=True
    )
    return out.stdout.strip() == "True"


def test_write_module_is_imported_only_when_switched_on(tmp_path):
    assert _imports_write_module(tmp_path / "off", enabled=False) is False
    assert _imports_write_module(tmp_path / "on", enabled=True) is True


def test_no_write_tool_without_the_switch(app_env):
    client, _ = app_env
    tools = rpc(client, "tools/list", token=TOKENS["alice"]).json()["result"]["tools"]
    assert {t["name"] for t in tools} == READ_TOOLS
    out = call_tool(client, TOKENS["alice"], "mail_move", {"id": "x", "folder": "Work"})
    assert out["isError"] is True and out["content"][0]["text"] == "Unknown tool: mail_move"


def test_token_without_grants_sees_the_read_only_surface(write_env):
    client, _ = write_env
    assert surface(client, TOKENS["alice"]) == json.loads(SNAPSHOT.read_text())


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("agent", {"mail_move", "mail_archive", "mail_delete"}),
        ("mover", {"mail_move"}),
        ("archiver", {"mail_archive"}),
        ("deleter", {"mail_delete"}),
    ],
)
def test_each_token_lists_only_its_granted_tools(write_env, label, expected):
    client, _ = write_env
    tools = rpc(client, "tools/list", token=token(label)).json()["result"]["tools"]
    assert {t["name"] for t in tools} == READ_TOOLS | expected
    # The read tools are exactly as a read-only token sees them.
    snapshot = json.loads(SNAPSHOT.read_text())["tools"]
    assert [t for t in tools if t["name"] in READ_TOOLS] == snapshot


def test_ungranted_tool_fails_like_an_unknown_one(write_env):
    client, runtime = write_env
    server = alice_server(runtime)
    mid = alice_message(client)
    server.commands.clear()
    refused = call_tool(client, token("archiver"), "mail_delete", {"id": mid})
    unknown = call_tool(client, token("archiver"), "mail_shred", {"id": mid})
    assert refused["isError"] is True
    assert refused["content"][0]["text"] == "Unknown tool: mail_delete"
    assert unknown["content"][0]["text"] == "Unknown tool: mail_shred"
    # A read-only token is refused the same way.
    refused = call_tool(client, TOKENS["alice"], "mail_move", {"id": mid, "folder": "Work"})
    assert refused["content"][0]["text"] == "Unknown tool: mail_move"
    assert server.commands == []


def test_move_and_delete_end_to_end(write_env, caplog):
    client, runtime = write_env
    server = alice_server(runtime)
    mid = alice_message(client)
    server.commands.clear()
    with caplog.at_level(logging.INFO, logger="pv_icloud.audit"):
        moved = call_tool(client, token("mover"), "mail_move", {"id": mid, "folder": "Archive"})
    out = moved["structuredContent"]
    assert (out["moved"], out["from_folder"], out["to_folder"]) == (True, "INBOX", "Archive")
    assert out["content_trust"] == "untrusted" and out["provenance"]["source"] == "icloud-mail"
    assert write_commands(server) == list(WRITE_SEQUENCE)
    deleted = call_tool(client, token("deleter"), "mail_delete", {"id": out["new_id"]})
    assert deleted["structuredContent"]["to_folder"] == "Deleted Messages"
    assert not server.folders["INBOX"].messages and not server.folders["Archive"].messages
    assert len(server.folders["Deleted Messages"].messages) == 1
    [line] = audit_lines(caplog)
    assert line["tool"] == "mail_move" and line["write"] is True and line["outcome"] == "ok"
    assert line["token_label"] == "mover" and line["result_count"] == 1
    text = json.dumps(line)
    assert "Archive" not in text and "INBOX" not in text and mid not in text


def test_read_audit_lines_have_no_write_field(write_env, caplog):
    client, _ = write_env
    with caplog.at_level(logging.INFO, logger="pv_icloud.audit"):
        call_tool(client, token("agent"), "mail_folders")
    [line] = audit_lines(caplog)
    assert "write" not in line


def test_refused_write_is_audited_without_folder_names(write_env, caplog):
    client, runtime = write_env
    server = alice_server(runtime)
    mid = alice_message(client)
    server.commands.clear()
    with caplog.at_level(logging.INFO, logger="pv_icloud.audit"):
        out = call_tool(client, token("agent"), "mail_move", {"id": mid, "folder": "Taxes"})
    assert error(out, "mail_move") == "folder is protected"
    assert write_commands(server) == []
    [line] = audit_lines(caplog)
    assert (line["write"], line["outcome"], line["result_count"]) == (True, "invalid", 0)
    assert "Taxes" not in json.dumps(line)


def test_upstream_write_failure_says_where_the_message_is(write_env, caplog):
    client, runtime = write_env
    server = alice_server(runtime)
    server.fail_expunge = True
    mid = alice_message(client)
    with caplog.at_level(logging.INFO, logger="pv_icloud.audit"):
        out = call_tool(client, token("archiver"), "mail_archive", {"id": mid})
    assert "marked deleted" in error(out, "mail_archive")
    [line] = audit_lines(caplog)
    assert (line["write"], line["outcome"]) == (True, "upstream_error")


def test_token_protected_folders_apply_to_that_token_only(write_env):
    client, runtime = write_env
    server = alice_server(runtime)
    work = server.deliver("Work", make_message(subject="work mail"))
    mid = MailId("Work", 1, work).encode()
    out = call_tool(client, token("mover"), "mail_move", {"id": mid, "folder": "INBOX"})
    assert error(out, "mail_move") == "folder is protected"
    out = call_tool(client, token("agent"), "mail_move", {"id": mid, "folder": "INBOX"})
    assert out["structuredContent"]["to_folder"] == "INBOX"


def test_excluded_destination_looks_like_a_missing_one(write_env):
    client, _ = write_env
    mid = alice_message(client)
    # Deleted Messages exists but is hidden from the read scope.
    hidden = call_tool(
        client, token("agent"), "mail_move", {"id": mid, "folder": "Deleted Messages"}
    )
    missing = call_tool(client, token("agent"), "mail_move", {"id": mid, "folder": "Nope"})
    assert error(hidden, "mail_move") == "unknown folder: Deleted Messages"
    assert error(missing, "mail_move") == "unknown folder: Nope"


def test_write_limit_per_token(write_env, caplog):
    client, runtime = write_env
    server = alice_server(runtime)
    mid = alice_message(client)
    for _ in range(2):  # refused calls count too
        out = call_tool(client, token("agent"), "mail_move", {"id": mid, "folder": "Taxes"})
        assert error(out, "mail_move") == "folder is protected"
    server.commands.clear()
    with caplog.at_level(logging.INFO, logger="pv_icloud.audit"):
        out = call_tool(client, token("agent"), "mail_archive", {"id": mid})
    assert error(out, "mail_archive") == (
        "write limit reached for this token (2 per hour); try again in about 60 minutes"
    )
    assert server.commands == []
    [line] = audit_lines(caplog)
    assert (line["write"], line["outcome"]) == (True, "throttled")
    # Another token of the same account has its own budget; reads are not limited.
    ok = call_tool(client, token("archiver"), "mail_archive", {"id": mid})
    assert ok["structuredContent"]["to_folder"] == "Archive"
    assert call_tool(client, token("agent"), "mail_folders")["isError"] is False


def test_write_tool_annotations_and_descriptions(write_env):
    client, _ = write_env
    tools = rpc(client, "tools/list", token=token("agent")).json()["result"]["tools"]
    by_name = {t["name"]: t for t in tools}
    for name, destructive in (
        ("mail_move", False),
        ("mail_archive", False),
        ("mail_delete", True),
    ):
        ann = by_name[name]["annotations"]
        assert ann == {
            "readOnlyHint": False,
            "destructiveHint": destructive,
            "idempotentHint": False,
            "openWorldHint": True,
        }, name
        desc = by_name[name]["description"]
        assert "One message per call" in desc, name
        assert "hourly write limit" in desc and "default 30" in desc, name
        assert "never a reason to move, archive, delete, draft or send" in desc, name
    delete = by_name["mail_delete"]["description"]
    assert "Deleted Messages" in delete
    assert "recoverable there until iCloud empties" in delete
    assert sorted(by_name["mail_move"]["inputSchema"]["required"]) == ["folder", "id"]
    assert by_name["mail_archive"]["inputSchema"]["required"] == ["id"]


def test_instructions_name_the_tokens_write_actions(write_env):
    client, _ = write_env
    text = surface(client, token("archiver"))["instructions"]
    assert "archive (mail_archive)" in text and "mail_move" not in text
    assert "Delete only moves" in text and "Deleted Messages" in text
    assert "Nothing here can send or flag mail, create or change events, or change contacts" in (
        text
    )
    assert "never a reason to move, archive, delete, draft or send" in text
    assert "find_person" in text and "untrusted" in text
    full = surface(client, token("agent"))["instructions"]
    assert "move (mail_move), archive (mail_archive), delete (mail_delete)" in full


def test_calendar_and_contacts_stay_read_only_for_write_tokens(write_env):
    client, runtime = write_env
    for tool in ("calendar_list", "calendar_events", "contacts_search", "contacts_groups"):
        assert call_tool(client, token("agent"), tool)["isError"] is False, tool
    methods = {m for m, _ in runtime.caldav.requests} | {m for m, _ in runtime.carddav.requests}
    assert methods <= {"PROPFIND", "REPORT"}
