"""Proposal write seam on a store-backed hub: extras, hub_transition and its restricted set."""
import asyncio
import itertools
from pathlib import Path

import pytest

from chimericmesh.events import Event
from chimericmesh.hub import Hub
from chimericmesh.store.proposals import PROPOSAL_STATUSES
from chimericmesh.store.proposals import get_proposal as stored_proposal
from chimericmesh.store.schema import core_registry
from chimericmesh.store.store import Store

MSG = "could not write block 1 No space left on device"
EXTRA = {"kind": "edit", "files": ["a.py", "b.py"], "estimated_lines": 5, "verification": "pytest -q"}
SRC = Path(__file__).resolve().parents[2] / "src" / "chimericmesh"


async def make(path):
    s = Store.open(path, registry=core_registry())
    hub = Hub(store=s)
    for _ in range(3):
        await hub.ingest(Event("syslog", "db01", "pg", 3, MSG, MSG, 1.0))
    return s, hub, hub.incidents()[0]


async def close(s, hub):
    await hub.aclose()
    s.close()


async def row(s, pid):
    return await s.read(lambda tx: stored_proposal(tx, pid))


def test_extras_persist_and_reload(tmp_path):
    path = tmp_path / "w.db"

    async def go():
        s, hub, inc = await make(path)
        p = await hub.create_proposal(inc.id, "fix", extra=EXTRA)
        await hub.hub_transition(p.id, "approved", decided_by="rule:r1", approved_by_rule="r1")
        await hub.complete(p.id, "n", extra={"files_changed": ["a.py"], "lines_changed": 3,
                                             "verification_result": "pass"})
        await close(s, hub)
        s = Store.open(path, registry=core_registry())
        hub = Hub(store=s)
        full = hub.get_proposal(p.id).to_full_dict()
        assert EXTRA.items() <= full.items()
        assert (full["decision_origin"], full["approved_by_rule"], full["files_changed"], full["lines_changed"],
                full["verification_result"]) == ("hub", "r1", ["a.py"], 3, "pass")
        await close(s, hub)

    asyncio.run(go())


def test_revoke_and_plain_name(tmp_path):
    async def go():
        s, hub, inc = await make(tmp_path / "r.db")
        p = await hub.create_proposal(inc.id, "fix")
        await hub.decide(p.id, True, "alice")
        assert await hub.hub_transition(p.id, "denied", decided_by="alice") is None
        assert (await row(s, p.id)).status == "approved"
        got = await hub.hub_transition(p.id, "denied", decided_by="rule-revoked:r1")
        r = await row(s, p.id)
        assert got.status == "denied" and (r.status, r.decision_origin) == ("denied", "hub")
        await close(s, hub)

    asyncio.run(go())


def test_unknown_extra_raises_before_the_writer_is_called(tmp_path):
    async def go():
        s, hub, inc = await make(tmp_path / "u.db")
        p = await hub.create_proposal(inc.id, "fix")
        calls = []
        real = s.transact

        async def spy(fn):
            calls.append(fn)
            return await real(fn)

        s.transact = spy
        for call in (hub.create_proposal(inc.id, "x", extra={"nope": 1}),
                     hub.complete(p.id, "n", extra={"nope": 1}),
                     hub.hub_transition(p.id, "denied", decided_by="withdrawn:x", extra={"nope": 1})):
            with pytest.raises(ValueError):
                await call
        assert calls == []
        s.transact = real
        await close(s, hub)

    asyncio.run(go())


def test_concurrent_hub_transitions_give_one_winner(tmp_path):
    async def go():
        s, hub, inc = await make(tmp_path / "c.db")
        p = await hub.create_proposal(inc.id, "fix")
        a, b = await asyncio.gather(
            hub.hub_transition(p.id, "approved", decided_by="rule:r1", approved_by_rule="r1"),
            hub.hub_transition(p.id, "denied", decided_by="withdrawn:x"))
        assert [x is None for x in (a, b)].count(True) == 1
        await close(s, hub)

    asyncio.run(go())


ALLOWED = {
    ("approved", "denied", "rule-revoked:r1"), ("approved", "denied", "rule-expired:r1"),
    ("approved", "denied", "rule-suspended:r1"), ("pending", "denied", "withdrawn:x"),
    ("pending", "denied", "expired:x"), ("pending", "approved", "rule:r1+rule"),
}
DECIDERS = ["alice", "rule:r1", "rule:r1+rule", "rule-revoked:r1", "rule-expired:r1", "rule-suspended:r1",
            "withdrawn:x", "expired:x"]


def test_hub_transition_accepts_exactly_the_restricted_set(tmp_path):
    async def go():
        s, hub, inc = await make(tmp_path / "o2.db")
        for old, new, by in itertools.product(PROPOSAL_STATUSES, PROPOSAL_STATUSES, DECIDERS):
            p = await hub.create_proposal(inc.id, "fix")
            if old in ("approved", "denied", "done"):
                await hub.decide(p.id, old != "denied", "seed")
            if old == "done":
                await hub.complete(p.id)
            before = await row(s, p.id)
            rule = "r1" if by.endswith("+rule") else None
            got = await hub.hub_transition(p.id, new, decided_by=by.replace("+rule", ""), approved_by_rule=rule)
            after = await row(s, p.id)
            if (old, new, by) in ALLOWED:
                assert got is not None and after.status == new and after.decision_origin == "hub", (old, new, by)
            else:
                assert got is None and after == before, (old, new, by)
        await close(s, hub)

    asyncio.run(go())


def test_hub_transition_is_not_reachable_from_rest_mcp_or_irc():
    for name in ("api.py", "mcp_server.py", "commands.py"):
        assert "hub_transition" not in (SRC / name).read_text(encoding="utf-8"), name
