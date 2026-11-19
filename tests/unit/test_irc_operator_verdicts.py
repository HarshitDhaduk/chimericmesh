"""``handle_command`` with operator verdicts, fail closed under enforcement (R9.2, R9.3, review 4)."""
from __future__ import annotations

import asyncio

import pytest

from chimericmesh.commands import Command, handle_command
from chimericmesh.hub import Hub
from chimericmesh.parsers import parse_syslog
from chimericmesh.services.access import OperatorVerdict as V

DISK = "<27>Nov 13 11:10:08 db01 pg[1]: FATAL: could not write block %d: No space left on device"


def setup(enforce, operators=None):
    hub = Hub(enforce_access=enforce)
    hub.operators = operators
    hub.sent = []

    async def fake(channel, text):
        hub.sent.append((channel, text))
    hub.irc_broadcast = fake

    async def seed():
        for i in range(3):
            await hub.ingest(parse_syslog(DISK % i))
        inc = hub.incidents()[0]
        prop = await hub.create_proposal(inc.id, "rotate logs")
        return inc, prop
    inc, prop = asyncio.run(seed())
    return hub, inc, prop


def run(hub, nick, text, **kw):
    name, *args = text[1:].split()
    return asyncio.run(handle_command(hub, nick, "#ops", Command(name, tuple(args)), **kw))


def untouched(hub, inc, prop):
    return prop.status == "pending" and not hub.get_incident(inc.id).resolved


def test_four_arguments_without_enforcement_behave_as_before():
    hub, inc, prop = setup(False, None)
    assert run(hub, "anyone", f"!approve {prop.id}")[0].endswith(f"{prop.id} approved")
    hub2, inc2, prop2 = setup(False, {"bob"})
    assert "only operators" in run(hub2, "alice", f"!approve {prop2.id}")[0]
    assert prop2.status == "pending"


@pytest.mark.parametrize("operators", [None, {"alice"}, {"bob"}])
@pytest.mark.parametrize("cmd", ["!approve {p}", "!deny {p}", "!resolve {i}"])
def test_four_arguments_with_enforcement_fail_closed(operators, cmd):
    hub, inc, prop = setup(True, operators)
    out = run(hub, "alice", cmd.format(p=prop.id, i=inc.id))
    assert "only operators" in out[0]
    assert untouched(hub, inc, prop)


def test_read_only_commands_work_with_enforcement_and_any_verdict():
    hub, inc, prop = setup(True, None)
    for verdict in (None, V.NOT_OPERATOR, V.OPERATORS_UNSET, V.NEEDS_PASS):
        assert run(hub, "alice", "!status", verdict=verdict)[0].startswith("[mesh] events seen")
        assert run(hub, "alice", "!incidents", verdict=verdict)
        assert run(hub, "alice", "!proposals", verdict=verdict)
        assert run(hub, "alice", "!help", verdict=verdict)


def test_allowed_runs_whatever_operators_holds():
    hub, inc, prop = setup(True, {"someone-else"})
    assert run(hub, "alice", f"!approve {prop.id}", verdict=V.ALLOWED)[0].endswith("approved")
    assert prop.status == "approved"
    assert run(hub, "alice", f"!resolve {inc.id}", verdict=V.ALLOWED)[0].endswith(f"resolved {inc.id}")
    hub2, inc2, prop2 = setup(True, None)
    assert run(hub2, "alice", f"!deny {prop2.id}", verdict=V.ALLOWED)[0].endswith("denied")


@pytest.mark.parametrize("enforce", [True, False])
def test_baseline_follows_hub_is_operator(enforce):
    hub, inc, prop = setup(enforce, {"dashboard"})
    assert "resolved" in run(hub, "dashboard", f"!resolve {inc.id}", verdict=V.BASELINE)[0]
    hub2, inc2, prop2 = setup(enforce, {"bob"})
    assert "only operators" in run(hub2, "dashboard", f"!resolve {inc2.id}", verdict=V.BASELINE)[0]
    assert untouched(hub2, inc2, prop2)


def test_refusal_texts_and_nothing_changes():
    hub, inc, prop = setup(True, {"bob"})
    assert "only operators" in run(hub, "alice", f"!approve {prop.id}", verdict=V.NOT_OPERATOR)[0]
    assert "CHIMERICMESH_OPERATORS" in run(hub, "alice", f"!deny {prop.id}", verdict=V.OPERATORS_UNSET)[0]
    assert "PASS" in run(hub, "bob", f"!resolve {inc.id}", verdict=V.NEEDS_PASS)[0]
    assert untouched(hub, inc, prop)


def test_hostile_nick_never_reaches_a_reply_with_control_characters():
    hub, inc, prop = setup(True, None)
    nick = "al\r\nice\x00" + "x" * 100
    for verdict in (None, V.NOT_OPERATOR, V.OPERATORS_UNSET, V.NEEDS_PASS):
        for line in run(hub, nick, f"!approve {prop.id}", verdict=verdict):
            assert "\r" not in line and "\n" not in line and "\x00" not in line
