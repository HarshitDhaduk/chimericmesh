"""The stream through the real create_app on a bare hub and on a store-backed hub (dashboard task 3.2; M2).

Also the page headers (R1.3) and the unchanged GET /api/events (R5.7).
"""
from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from approvals_helpers import collect_garbage  # noqa: F401  (autouse: frees apps and their stores)
import chimericmesh.api as api_module
from chimericmesh.api import build_registry, create_app
from chimericmesh.hub import Hub
from chimericmesh.store import Store
from chimericmesh.web.feed import StreamConfig

BASE = "http://127.0.0.1:8000"
LINE = "<131>Nov 19 10:00:0{n} {host} proxy-worker[42]: upstream timed out {n}"


@pytest.fixture(params=["bare", "store"])
def app(request, tmp_path, monkeypatch):
    real = api_module.build_stream_router

    def short(*a, **k):  # a short stream age, nothing else changes
        return real(*a, **{**k, "config": StreamConfig(max_seconds=5.0, poll_interval=0.02, heartbeat=0.2)})

    monkeypatch.setattr(api_module, "build_stream_router", short)
    if request.param == "bare":
        app = create_app(Hub(), start_listeners=False, enforce_access=False)
    else:
        store = Store.open(tmp_path / "m.db", registry=build_registry())
        app = create_app(store=store, start_listeners=False, enforce_access=False)
    app.state.mode = request.param
    yield app
    app.state.store.close()


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 4000)), base_url=BASE)


async def seed(c):
    r = await c.post("/api/services", json={"id": "web", "name": "web", "hosts": ["web-host"]})
    assert r.status_code == 201, r.text
    for n, host in ((1, "web-host"), (2, "web-host"), (3, "web-host"), (4, "other-host")):
        r = await c.post("/api/ingest", json={"source": "syslog", "line": LINE.format(n=n, host=host)})
        assert r.status_code == 202, r.text
        assert r.json()["service_id"] == ("web" if host == "web-host" else None)


def frames_ids(text):
    return [int(ln[4:]) for ln in text.split("\n") if ln.startswith("id: ")]


def test_same_delivery_in_both_modes(app):
    async def go():
        async with client(app) as c:
            await seed(c)
            # a stream with a short life: end it through the service id check
            web = asyncio.create_task(c.get("/api/services/web/stream?after=0"))
            un = asyncio.create_task(c.get("/api/services/unassigned/stream?after=0"))
            await asyncio.sleep(0.5)
            assert (await c.delete("/api/services/web")).status_code in (200, 204)
            r_web = await asyncio.wait_for(web, 5)
            un.cancel()
            return r_web

    r = asyncio.run(go())
    assert r.status_code == 200 and r.text.startswith(": open\n\n")
    ids = frames_ids(r.text)
    assert len(ids) == 3 and ids == sorted(ids)
    assert "event: closed" in r.text and "service_deleted" in r.text  # R5.5: ends with the service


def test_unassigned_gets_only_events_without_a_service(app):
    async def go():
        async with client(app) as c:
            await seed(c)
            hub = app.state.hub
            ring = [e.id for e in hub.recent(10) if e.service_id is None]
            t = asyncio.create_task(c.get("/api/services/unassigned/stream?after=0"))
            await asyncio.sleep(0.4)
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
            return ring

    ring = asyncio.run(go())
    assert len(ring) == 1


def test_web_stream_delivers_the_same_ids_the_hub_holds(app):
    async def go():
        async with client(app) as c:
            await seed(c)
            hub = app.state.hub
            want = sorted(e.id for e in hub.recent(10) if e.service_id == "web")
            t = asyncio.create_task(c.get("/api/services/web/stream?tail=100"))
            await asyncio.sleep(0.4)
            await c.delete("/api/services/web")
            r = await asyncio.wait_for(t, 5)
            return want, r

    want, r = asyncio.run(go())
    assert frames_ids(r.text) == want


def test_events_route_is_unchanged(app):
    async def go():
        async with client(app) as c:
            await seed(c)
            return (await c.get("/api/events?limit=50")).json()

    got = asyncio.run(go())
    assert got == [e.to_dict() for e in app.state.hub.recent(50)]


def test_stream_gate_is_on_app_state(app):
    assert app.state.stream_gate.active == 0 and app.state.stream_gate.max_streams == 20
    assert app.state.stream_gate.lease_grace == 10.0


def test_index_has_the_security_headers_and_api_does_not(app):
    async def go():
        async with client(app) as c:
            return await c.get("/"), await c.get("/api/stats")

    page, stats = asyncio.run(go())
    assert page.status_code == 200 and "<title>ChimericMesh" in page.text
    assert page.headers["x-content-type-options"] == "nosniff"
    assert page.headers["referrer-policy"] == "no-referrer"
    assert "x-content-type-options" not in stats.headers and "referrer-policy" not in stats.headers
