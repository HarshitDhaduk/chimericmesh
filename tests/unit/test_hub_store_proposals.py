"""Store-backed hub: proposals persist before returning, survive restarts and survive cancellation."""
import asyncio
import logging
import threading

import pytest

from chimericmesh.commands import Command, handle_command
from chimericmesh.events import Event
from chimericmesh.hub import Hub
from chimericmesh.store.errors import StoreError, StoreWriteError
from chimericmesh.store.proposals import get_proposal as stored_proposal
from chimericmesh.store.schema import core_registry
from chimericmesh.store.store import Store

MSG = "could not write block 1 No space left on device"


def ev():
    return Event("syslog", "db01", "pg", 3, MSG, MSG, 1.0, service_id="svc1")


def open_store(path):
    return Store.open(path, registry=core_registry())


async def with_incident(store):
    hub = Hub(store=store)
    said: list[str] = []

    async def fake(channel, text):
        said.append(text)

    hub.irc_broadcast = fake
    for _ in range(3):
        await hub.ingest(ev())
    return hub, hub.incidents()[0], said


async def db_row(store, pid):
    return await store.read(lambda tx: stored_proposal(tx, pid))


@pytest.fixture
def db(tmp_path):
    return tmp_path / "p.db"


def test_create_decide_complete_persist_before_returning(db):
    async def go():
        s = open_store(db)
        hub, inc, _ = await with_incident(s)
        p = await hub.create_proposal(inc.id, "fix it", "d")
        assert (await db_row(s, p.id)).status == "pending"
        await hub.decide(p.id, True, "alice")
        r = await db_row(s, p.id)
        assert (r.status, r.decided_by, r.decision_origin) == ("approved", "alice", "user")
        await hub.complete(p.id, "done ok")
        assert (await db_row(s, p.id)).status == "done"
        assert hub.get_proposal(p.id).status == "done"
        await hub.aclose()
        s.close()

    asyncio.run(go())


def test_ids_continue_and_all_proposals_are_kept(db):
    async def go():
        s = open_store(db)
        hub, inc, _ = await with_incident(s)
        for _ in range(205):
            await hub.create_proposal(inc.id, "x")
        assert len(hub.list_proposals()) == 205  # no cap in store-backed mode
        await hub.aclose()
        s.close()
        s = open_store(db)
        hub = Hub(store=s)
        assert len(hub.list_proposals()) == 205
        inc = hub.incidents()[0]
        assert (await hub.create_proposal(inc.id, "y")).id == "p206"
        listed = hub.list_proposals(limit=1)[0]
        assert listed.incident_opening == 1 and listed.service_id == "svc1"
        await hub.aclose()
        s.close()

    asyncio.run(go())


def test_decision_origin_reloads_and_invalid_transition_is_none(db):
    async def go():
        s = open_store(db)
        hub, inc, _ = await with_incident(s)
        p = await hub.create_proposal(inc.id, "fix")
        await hub.decide(p.id, False, "bob")
        assert await hub.decide(p.id, True, "bob") is None
        assert await hub.complete(p.id) is None
        assert await hub.decide("p999", True, "bob") is None
        await hub.aclose()
        s.close()
        s = open_store(db)
        hub = Hub(store=s)
        assert hub.get_proposal(p.id).decision_origin == "user"
        assert hub.get_proposal(p.id).status == "denied"
        await hub.aclose()
        s.close()

    asyncio.run(go())


def test_concurrent_decides_give_one_winner(db):
    async def go():
        s = open_store(db)
        hub, inc, _ = await with_incident(s)
        p = await hub.create_proposal(inc.id, "fix")
        a, b = await asyncio.gather(hub.decide(p.id, True, "a"), hub.decide(p.id, False, "b"))
        assert [x is None for x in (a, b)].count(True) == 1
        assert hub.get_proposal(p.id).status == (await db_row(s, p.id)).status
        await hub.aclose()
        s.close()

    asyncio.run(go())


def block_writer(store):
    """Submit a job that holds the writer thread until the returned event is set."""
    release = threading.Event()
    started = threading.Event()

    def hold(tx):
        started.set()
        release.wait(5)

    async def submit():
        task = asyncio.ensure_future(store.transact(hold))
        while not started.is_set():
            await asyncio.sleep(0.005)
        return task

    return release, submit


@pytest.mark.parametrize("op", ["create", "decide", "complete"])
def test_cancelled_caller_still_commits_and_mirrors(db, op, caplog):
    async def go():
        s = open_store(db)
        hub, inc, said = await with_incident(s)
        pid = None
        if op != "create":
            pid = (await hub.create_proposal(inc.id, "fix")).id
            if op == "complete":
                await hub.decide(pid, True, "alice")
        release, submit = block_writer(s)
        holder = await submit()
        if op == "create":
            call = hub.create_proposal(inc.id, "fix")
        elif op == "decide":
            call = hub.decide(pid, True, "alice")
        else:
            call = hub.complete(pid, "ok")
        task = asyncio.ensure_future(call)
        await asyncio.sleep(0.02)
        task.cancel()
        release.set()
        await holder
        with pytest.raises(asyncio.CancelledError):
            await task
        for _ in range(50):
            await asyncio.sleep(0.01)
            if not [t for t in hub._commit_tasks if not t.done()]:
                break
        pid = pid or "p1"
        mirrored, stored = hub.get_proposal(pid), await db_row(s, pid)
        assert mirrored is not None and mirrored.status == stored.status
        expected = {"create": "pending", "decide": "approved", "complete": "done"}[op]
        assert stored.status == expected
        if op == "decide":
            assert await hub.decide(pid, True, "alice") is None  # because the first one happened
        await hub.aclose()
        s.close()

    with caplog.at_level(logging.ERROR):
        asyncio.run(go())
    assert "never retrieved" not in caplog.text and "InvalidStateError" not in caplog.text


class FailingStore:
    def __init__(self, real):
        self._real = real
        self.events, self.audit, self.config = real.events, real.audit, real.config

    def health(self):
        return self._real.health()

    async def transact(self, fn):
        raise StoreWriteError("disk gone")

    async def read(self, fn):
        return await self._real.read(fn)

    async def note_last_event_id(self, n):
        return None


def test_failing_store_raises_before_memory_changes_and_commands_survive(db):
    async def go():
        s = open_store(db)
        hub, inc, said = await with_incident(s)
        p = await hub.create_proposal(inc.id, "fix")
        hub.store = FailingStore(s)
        n = len(hub.list_proposals())
        with pytest.raises(StoreError):
            await hub.create_proposal(inc.id, "fix 2")
        with pytest.raises(StoreError):
            await hub.decide(p.id, True, "alice")
        assert len(hub.list_proposals()) == n and hub.get_proposal(p.id).status == "pending"
        replies = await handle_command(hub, "alice", "#ops", Command("approve", (p.id,)))
        assert replies == ["[mesh] store unavailable, try again"]
        hub.store = s
        await hub.aclose()
        s.close()

    asyncio.run(go())
