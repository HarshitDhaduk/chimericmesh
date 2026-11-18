"""Success Criteria 1, 2, 4 and 6 end to end, through create_app and httpx.ASGITransport."""
import asyncio
import os

import httpx

from chimericmesh.api import create_app
from chimericmesh.hub import Hub
from chimericmesh.store.config import StoreConfig, resolve_db_path
from chimericmesh.store.schema import core_registry
from chimericmesh.store.store import Store

DISK = "<27>Nov 13 11:10:08 db01 pg[1]: FATAL: could not write block %d: No space left on device"


class Clock:
    def __init__(self):
        self.t = 50_000.0

    def __call__(self):
        return self.t


async def run_app(app, body):
    """Run ``body(client)`` inside the app lifespan."""
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await body(client)


async def ingest(client, n, start=0):
    for i in range(n):
        r = await client.post("/api/ingest", json={"source": "syslog", "line": DISK % (start + i)})
        assert r.status_code == 202


def sent_lines(hub):
    return [m.text for m in hub.transcript if m.sender == "ChimericBot"]


def test_sc1_proposal_ids_continue_and_are_never_reused(tmp_path):
    path = tmp_path / "sc1.db"

    async def first(client):
        await ingest(client, 3)
        iid = (await client.get("/api/incidents")).json()[0]["id"]
        ids = [(await client.post("/api/proposals", json={"incident_id": iid, "action": "a"})).json()["id"]
               for _ in range(2)]
        assert ids == ["p1", "p2"]
        return iid

    def app_on(path):
        s = Store.open(path, registry=core_registry())
        return s, create_app(store=s, start_listeners=False)

    s, app = app_on(path)
    iid = asyncio.run(run_app(app, first))
    s.close()

    async def create(client):
        return (await client.post("/api/proposals", json={"incident_id": iid, "action": "b"})).json()["id"]

    s, app = app_on(path)
    assert asyncio.run(run_app(app, create)) == "p3"
    s.transact_sync(lambda tx: tx.execute("DELETE FROM proposals WHERE id = 'p3'"))
    s.close()
    s, app = app_on(path)
    assert asyncio.run(run_app(app, create)) == "p4"
    s.close()


def test_sc2_incident_count_survives_a_restart_without_reannouncing(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIMERICMESH_DB", str(tmp_path / "sc2.db"))

    async def first(client):
        await ingest(client, 50)
        return (await client.get("/api/incidents")).json()[0]

    app = create_app(start_listeners=False)
    inc = asyncio.run(run_app(app, first))
    assert inc["count"] == 50

    app = create_app(start_listeners=False)
    hub = app.state.hub

    async def second(client):
        await ingest(client, 5, start=100)
        return (await client.get(f"/api/incidents/{inc['id']}")).json()

    assert asyncio.run(run_app(app, second))["count"] == 55
    assert sent_lines(hub) == []


def test_sc4_event_table_is_bounded_and_incidents_untouched(tmp_path):
    cfg = StoreConfig.from_env({"CHIMERICMESH_MAX_EVENTS": "1000"}, batch_rows=100, trim_interval=0.0)
    s = Store.open(tmp_path / "sc4.db", registry=core_registry(), config=cfg)
    app = create_app(store=s, start_listeners=False)
    hub = app.state.hub

    async def body(client):
        sizes = []
        for chunk in range(6):
            for i in range(500):
                await hub.ingest(hub_event(chunk * 500 + i))
            await s.events.barrier()
            sizes.append(await s.read(lambda tx: tx.one("SELECT COUNT(*) FROM events")[0]))
        await s.events.barrier()
        incidents = (await client.get("/api/incidents")).json()
        return sizes, incidents

    sizes, incidents = asyncio.run(run_app(app, body))
    assert max(sizes) <= 1000 + 500 + 100
    final = s.read_sync(lambda tx: tx.one("SELECT COUNT(*) FROM events")[0])
    assert final <= 1000 + 100
    stored = s.read_sync(lambda tx: tx.one("SELECT COUNT(*) FROM incidents")[0])
    assert stored == len(incidents) == 1 and incidents[0]["count"] == 3000
    s.close()


def hub_event(n):
    from chimericmesh.parsers import parse_syslog

    return parse_syslog(DISK % n)


def test_sc6_reopen_window_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIMERICMESH_REOPEN_AFTER_SECONDS", "300")
    clock = Clock()
    s = Store.open(tmp_path / "sc6.db", registry=core_registry())
    hub = Hub(store=s, clock=clock)
    app = create_app(hub, start_listeners=False)

    async def body(client):
        await ingest(client, 3)
        iid = (await client.get("/api/incidents")).json()[0]["id"]
        assert (await client.post(f"/api/incidents/{iid}/resolve", json={})).status_code == 200
        clock.t += 10
        await ingest(client, 1, start=10)
        inc = (await client.get(f"/api/incidents/{iid}")).json()
        assert inc["resolved"] and inc["count"] == 4
        clock.t += 301
        await ingest(client, 1, start=20)
        inc = (await client.get(f"/api/incidents/{iid}")).json()
        assert not inc["resolved"] and inc["count"] == 1 and inc["opening"] == 2
        return iid

    iid = asyncio.run(run_app(app, body))
    assert len([t for t in sent_lines(hub) if t.startswith("REOPENED")]) == 1
    rows = asyncio.run(s.audit.read(50))
    assert [r["action"] for r in rows if r["subject"] == iid] == ["incident_reopened"]
    s.close()


def test_two_working_directories_open_the_same_file(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    paths = []
    for d in (a, b):
        monkeypatch.chdir(d)
        app = create_app(start_listeners=False)
        paths.append(app.state.store.path)
        asyncio.run(run_app(app, lambda client: asyncio.sleep(0)))
    assert paths[0] == paths[1] == resolve_db_path() and os.path.isabs(paths[0])
    assert os.listdir(a) == [] and os.listdir(b) == []
