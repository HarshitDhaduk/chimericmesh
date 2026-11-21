"""IRC and terminal commands of auto-approval (task 7.1): !rules !revoke !pause !withdraw."""
from __future__ import annotations

from approvals_helpers import collect_garbage  # noqa: F401  (autouse: frees apps and their stores)

import asyncio
import os
import tempfile

import httpx
import pytest
from approvals_helpers import make_manager, open_incident
from test_approvals_reduce import fields

from chimericmesh import commands
from chimericmesh.api import build_registry, create_app
from chimericmesh.approvals.models import RuleFields
from chimericmesh.commands import HELP_LINES, handle_command, parse_command
from chimericmesh.hub import Hub
from chimericmesh.services.limiter import ReduceAuditLimiter, reduce_key
from chimericmesh.store import Store, StoreError

MODES = ["bare", "store"]


@pytest.fixture(params=MODES)
def rig(request):
    r = make_manager(request.param)
    yield r
    r.close()


def run(rig, nick, text, **kw):
    cmd = parse_command(text)
    return asyncio.run(handle_command(rig.hub, nick, "#ops", cmd, **kw))


async def rules(rig, n=1):
    await rig.add_service()
    for i in range(n):
        await rig.manager.create_rule(fields(match_value=f"signature number {i}"), actor="a")


def test_reduce_key_accepts_the_three_kinds():
    assert reduce_key("terminal", "1.2.3.4") != reduce_key("irc", "1.2.3.4")
    assert reduce_key("terminal", "1.2.3.4") != reduce_key("rest", "1.2.3.4")
    assert reduce_key("irc", "alice") == reduce_key("irc", "alice")
    assert reduce_key("rest", "1.2.3.4") == reduce_key("rest", "1.2.3.4")
    with pytest.raises(ValueError):
        reduce_key("smoke", "x")


def test_rules_lists_at_most_five_active_rules(rig):
    asyncio.run(rules(rig, 7))
    asyncio.run(rig.manager.revoke_rule("r2", actor="a", key="k"))
    lines = run(rig, "alice", "!rules")
    assert len(lines) == 5 and all(line.startswith("[mesh] r") for line in lines)
    assert "r2 " not in " ".join(lines)
    first = lines[0]
    assert first.startswith('[mesh] r1 api-gw signature "signature number 0" expires ')
    assert first.split("expires ")[1].count("-") == 2


def test_rules_with_none_and_with_hostile_text(rig):
    assert run(rig, "alice", "!rules") == ["[mesh] no active rules"]
    asyncio.run(rig.add_service())
    asyncio.run(rig.manager.create_rule(RuleFields("api-gw", "signature", "evil\x1b[31m" + "x" * 80), actor="a"))
    (line,) = run(rig, "alice", "!rules")
    assert "\x1b" not in line and len(line) < 150


def test_revoke_pause_withdraw_need_no_operator_status_no_pass_and_work_with_enforcement_on_or_off(rig):
    asyncio.run(rules(rig, 3))
    iid = asyncio.run(open_incident(rig.hub))
    prop = asyncio.run(rig.hub.create_proposal(iid, "fix"))
    rig.hub.operators = {"someoneelse"}
    for enforce in (False, True):
        rig.hub.enforce_access = enforce
        assert "only operators" in run(rig, "mallory", "!approve " + prop.id)[0] or enforce   # approve is gated
        assert run(rig, "mallory", "!rules")[0].startswith("[mesh] r")                         # reduce commands are not
    assert run(rig, "mallory", "!revoke r1") == ["[mesh] revoked r1"]
    assert rig.manager.rule("r1").status == "revoked"
    assert run(rig, "mallory", "!pause") == ["[mesh] auto-approval paused"]
    assert rig.manager.paused
    assert run(rig, "mallory", f"!withdraw {prop.id}") == [f"[mesh] withdrawn {prop.id}"]
    assert rig.hub.get_proposal(prop.id).status == "denied"
    assert rig.hub.get_proposal(prop.id).decided_by.startswith("withdrawn:mallory")


def test_plain_replies_for_unknown_ids_and_final_states(rig):
    asyncio.run(rules(rig, 1))
    assert run(rig, "a", "!revoke r99") == ["[mesh] no rule with id r99"]
    assert run(rig, "a", "!revoke") == ["[mesh] usage: !revoke <rule id>"]
    assert run(rig, "a", "!withdraw p99") == ["[mesh] no proposal with id p99"]
    assert run(rig, "a", "!withdraw") == ["[mesh] usage: !withdraw <proposal id>"]
    run(rig, "a", "!revoke r1")
    assert run(rig, "a", "!revoke r1") == ["[mesh] r1 is already revoked"]
    iid = asyncio.run(open_incident(rig.hub))
    prop = asyncio.run(rig.hub.create_proposal(iid, "fix"))
    asyncio.run(rig.hub.decide(prop.id, True, "alice"))
    asyncio.run(rig.hub.complete(prop.id, "ok"))
    assert run(rig, "a", f"!withdraw {prop.id}") == [f"[mesh] {prop.id} is already done, nothing to withdraw"]
    run(rig, "a", "!pause")
    assert run(rig, "a", "!pause") == ["[mesh] auto-approval is already paused"]


def test_the_limiter_key_is_the_nick_and_the_terminal_can_pass_its_own(rig):
    asyncio.run(rules(rig, 2))
    run(rig, "alice", "!revoke r1")
    run(rig, "dashboard", "!revoke r2", reduce_key=reduce_key("terminal", "127.0.0.1"))
    assert [c["key"] for c in rig.limiter.calls] == [reduce_key("irc", "alice"),
                                                    reduce_key("terminal", "127.0.0.1")]


def test_eleven_commands_in_a_minute_all_take_effect(rig):
    rig.manager._limiter = ReduceAuditLimiter(rig.store.audit, now=rig.clock)
    asyncio.run(rules(rig, 11))
    for n in range(11):
        assert run(rig, "bob", f"!revoke r{n + 1}") == [f"[mesh] revoked r{n + 1}"]
    asyncio.run(rig.manager._limiter.aclose())
    actions = asyncio.run(rig.actions())
    assert actions.count("rule_revoked") == 10 and actions.count("reduce_ops_suppressed") == 1


def test_no_command_creates_or_widens_a_rule():
    assert commands.KNOWN_COMMANDS == {"help", "status", "incidents", "explain", "proposals", "resolve", "approve",
                                       "deny", "rules", "revoke", "pause", "withdraw"}
    rig = make_manager("bare")
    try:
        asyncio.run(rules(rig, 1))
        run(rig, "a", "!pause")
        for name in ("resume", "enable", "rule", "create", "widen", "edit", "unpause", "unrevoke"):
            assert run(rig, "a", f"!{name} r1")[0].startswith(f"[mesh] unknown command !{name}")
        assert rig.manager.paused and len(rig.manager.rules()) == 1
    finally:
        rig.close()


def test_help_has_a_third_line_for_the_new_commands():
    assert len(HELP_LINES) == 3
    assert HELP_LINES[2] == "rules: !rules !revoke <rid> !pause !withdraw <pid> (any registered nick)"
    assert any("!incidents" in line for line in HELP_LINES)


def test_a_bare_hub_without_approvals_answers_unknown_command():
    hub = Hub()
    for name in ("rules", "revoke", "pause", "withdraw"):
        lines = asyncio.run(handle_command(hub, "alice", "#ops", parse_command(f"!{name} x")))
        assert lines[0].startswith(f"[mesh] unknown command !{name}")


def test_a_reserved_nick_cannot_approve_or_deny(rig):
    asyncio.run(rules(rig, 1))
    iid = asyncio.run(open_incident(rig.hub))
    prop = asyncio.run(rig.hub.create_proposal(iid, "fix"))
    for nick in ("rule:r1", "RULE-x", "withdrawn:me", "expired:x", "Expired-y"):
        for verb in ("approve", "deny"):
            assert run(rig, nick, f"!{verb} {prop.id}") == ["[mesh] sorry, that nick is reserved"]
    assert rig.hub.get_proposal(prop.id).status == "pending"
    assert run(rig, "ruleman", f"!approve {prop.id}") == [f"[mesh] {prop.id} approved"]


def test_a_store_error_in_a_command_is_reported_not_raised(rig, monkeypatch):
    asyncio.run(rules(rig, 1))

    async def broken(fn):
        raise StoreError("down")

    # a revoke changes the mirror first and the sweep repairs the persist: the reply is the normal one
    real = rig.store.transact
    rig.store.transact = broken
    assert run(rig, "a", "!revoke r1") == ["[mesh] revoked r1"]
    rig.store.transact = real

    iid = asyncio.run(open_incident(rig.hub))
    prop = asyncio.run(rig.hub.create_proposal(iid, "fix"))

    async def boom(*a, **k):
        raise StoreError("down")

    monkeypatch.setattr(rig.manager, "withdraw", boom)
    assert run(rig, "a", f"!withdraw {prop.id}") == ["[mesh] store unavailable, try again"]


def test_the_terminal_revoke_needs_no_token_but_approve_still_does(monkeypatch):
    tmp = tempfile.TemporaryDirectory()
    monkeypatch.setenv("CHIMERICMESH_HOME", os.path.join(tmp.name, "home"))
    hub = Hub()
    app = create_app(hub, start_listeners=False, enforce_access=True)
    token = app.state.access.tokens.current()
    folder = os.path.join(tmp.name, "code")
    os.makedirs(folder)

    def call(method, url, **kw):
        async def go():
            transport = httpx.ASGITransport(app=app, client=("7.7.7.7", 1))
            async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as c:
                return await c.request(method, url, **kw)
        return asyncio.run(go())

    try:
        auth = {"X-Admin-Token": token}
        assert call("POST", "/api/services", headers=auth,
                    json={"id": "api-gw", "name": "gw", "hosts": ["web01"], "code_folder": folder}).status_code == 201
        assert call("POST", "/api/rules", headers=auth, json={"service_id": "api-gw", "match_type": "signature",
                                                              "match_value": "x"}).status_code == 201
        revoked = call("POST", "/api/terminal", json={"text": "!revoke r1"})
        assert revoked.status_code == 200 and revoked.json()["replies"] == ["[mesh] revoked r1"]
        assert call("POST", "/api/terminal", json={"text": "!approve p1"}).status_code == 401
        assert call("POST", "/api/terminal", json={"text": "!pause"}).json()["replies"] == \
            ["[mesh] auto-approval paused"]
    finally:
        tmp.cleanup()
