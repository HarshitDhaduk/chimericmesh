"""The proposal announcement is part of the shielded commit-and-mirror unit (M7)."""
import asyncio
import logging
import threading

import pytest

from chimericmesh.events import Event
from chimericmesh.hub import Hub
from chimericmesh.store.errors import StoreError, StoreWriteError
from chimericmesh.store.proposals import get_proposal as stored_proposal
from chimericmesh.store.schema import core_registry
from chimericmesh.store.store import Store

MSG = "could not write block 1 No space left on device"


async def make(path):
    s = Store.open(path, registry=core_registry())
    hub = Hub(store=s)
    for _ in range(3):
        await hub.ingest(Event("syslog", "db01", "pg", 3, MSG, MSG, 1.0))
    return s, hub, hub.incidents()[0]


def transcript(hub):
    return [m.text for m in hub.transcript]


@pytest.mark.parametrize("op", ["create", "decide", "complete"])
def test_cancelled_caller_gets_exactly_one_line(tmp_path, op):
    async def go():
        s, hub, inc = await make(tmp_path / "a.db")
        pid = "p1"
        if op != "create":
            await hub.create_proposal(inc.id, "fix")
            if op == "complete":
                await hub.decide(pid, True, "alice")
        release, started = threading.Event(), threading.Event()

        def hold(tx):
            started.set()
            release.wait(5)

        holder = asyncio.ensure_future(s.transact(hold))
        while not started.is_set():
            await asyncio.sleep(0.005)
        call = {"create": lambda: hub.create_proposal(inc.id, "fix"),
                "decide": lambda: hub.decide(pid, True, "alice"),
                "complete": lambda: hub.complete(pid, "ok")}[op]()
        task = asyncio.ensure_future(call)
        await asyncio.sleep(0.02)
        task.cancel()
        release.set()
        await holder
        await asyncio.gather(task, return_exceptions=True)
        for _ in range(50):
            await asyncio.sleep(0.01)
            if all(t.done() for t in hub._commit_tasks):
                break
        prefix = {"create": "PROPOSAL p1 ", "decide": "p1 APPROVED by alice", "complete": "p1 DONE: ok"}[op]
        assert len([t for t in transcript(hub) if t.startswith(prefix)]) == 1
        stored = await s.read(lambda tx: stored_proposal(tx, pid))
        assert hub.get_proposal(pid).status == stored.status
        await hub.aclose()
        s.close()

    asyncio.run(go())


def test_failing_say_keeps_the_commit(tmp_path, caplog):
    async def go():
        s, hub, inc = await make(tmp_path / "b.db")

        async def boom(channel, text):
            raise RuntimeError("irc down")

        hub.say = boom
        p = await hub.create_proposal(inc.id, "fix")
        assert p is not None and hub.get_proposal(p.id) is p
        assert (await s.read(lambda tx: stored_proposal(tx, p.id))).status == "pending"
        await hub.aclose()
        s.close()

    with caplog.at_level(logging.ERROR):
        asyncio.run(go())
    assert len([r for r in caplog.records if r.levelno >= logging.ERROR and "announce" in r.getMessage()]) == 1


def test_failing_store_writes_no_line(tmp_path):
    async def go():
        s, hub, inc = await make(tmp_path / "c.db")

        async def fail(fn):
            raise StoreWriteError("disk gone")

        real = s.transact
        s.transact = fail
        before = transcript(hub)
        with pytest.raises(StoreError):
            await hub.create_proposal(inc.id, "fix")
        assert transcript(hub) == before
        s.transact = real
        await hub.aclose()
        s.close()

    asyncio.run(go())
