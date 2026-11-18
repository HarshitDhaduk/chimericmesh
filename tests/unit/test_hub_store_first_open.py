"""First open is detected from ``inc.opened`` (set by the tracker) and ``_announced``."""
import asyncio
import re
from pathlib import Path

from chimericmesh.events import Event
from chimericmesh.hub import Hub
from chimericmesh.store.schema import core_registry
from chimericmesh.store.store import Store

MSG = "upstream timed out connecting to 10.0.0.1:5432"


def ev():
    return Event("syslog", "web02", "app", 4, MSG, MSG, 1.0)


class CountingStore:
    def __init__(self, real):
        self._real = real
        self.events, self.audit, self.config = real.events, real.audit, real.config
        self.writes = 0

    def health(self):
        return self._real.health()

    async def transact(self, fn):
        self.writes += 1
        return await self._real.transact(fn)

    async def read(self, fn):
        return await self._real.read(fn)

    async def note_last_event_id(self, n):
        return await self._real.note_last_event_id(n)


def test_one_awaited_persist_and_one_announcement_on_first_open(tmp_path):
    path = tmp_path / "f.db"

    async def go():
        s = Store.open(path, registry=core_registry())
        hub = Hub(store=s)
        counting = CountingStore(s)
        hub.store = hub.lifecycle.store = counting
        said: list[str] = []

        async def fake(channel, text):
            said.append(text)

        hub.irc_broadcast = fake
        writes = []
        for _ in range(9):
            await hub.ingest(ev())
            writes.append(counting.writes)
        assert writes[:3] == [0, 0, 1] and writes[8] == 1  # one awaited commit, at the third event
        assert len([t for t in said if t.startswith("[")]) == 1
        hub.store = hub.lifecycle.store = s
        await hub.aclose()
        s.close()
        s = Store.open(path, registry=core_registry())
        hub = Hub(store=s)
        hub.irc_broadcast = fake
        await hub.ingest(ev())  # the 10th event: a milestone UPDATE line is expected, no new opening
        assert len([t for t in said if t.startswith("[")]) == 1
        assert said[-1].startswith("UPDATE ")
        await hub.aclose()
        s.close()

    asyncio.run(go())


def test_hub_never_assigns_opened():
    text = (Path(__file__).resolve().parents[2] / "src" / "chimericmesh" / "hub.py").read_text(encoding="utf-8")
    assert not re.search(r"\.opened\s*=", text)
