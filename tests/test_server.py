# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

"""End-to-end over Streamable HTTP: auth, account isolation, audit, health."""

from __future__ import annotations

import datetime as dt
import json
import logging

import pytest
from starlette.testclient import TestClient

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
    ups = Upstreams()
    now = dt.datetime.now(dt.UTC)
    for user, subject in (
        ("alice@icloud.com", "alice secret plans"),
        ("bob@icloud.com", "bob secret plans"),
    ):
        s = ups.servers[user]
        s.folder("INBOX")
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
        make_config(tmp_path),
        conn_factory=ups.factory,
        start_sync=False,
        caldav_transport=caldav.transport(),
        carddav_transport=carddav.transport(),
    )
    runtime.caldav = caldav  # type: ignore[attr-defined]  # for tests that break it
    runtime.carddav = carddav  # type: ignore[attr-defined]
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
