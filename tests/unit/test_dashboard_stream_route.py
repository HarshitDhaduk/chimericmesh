"""The stream router on a small FastAPI app (dashboard task 3.1; R5.1, R5.2, R5.3, R5.5)."""
import asyncio
import inspect
import json
import time

import httpx
import pytest
from fastapi import FastAPI

from chimericmesh.web.feed import StreamConfig, StreamGate
from chimericmesh.web.stream import build_stream_router
from test_dashboard_stream_frames import FakeFeed

KNOWN = {"web", "unassigned"}
CFG = StreamConfig(max_seconds=0.5, poll_interval=0.02, heartbeat=0.2)


def make(feed=None, gate=None, exists=None, config=CFG):
    feed = feed or FakeFeed()
    gate = gate or StreamGate()
    router = build_stream_router(feed, service_exists=exists or (lambda s: s in KNOWN), gate=gate,
                                 config=config, clock=time.monotonic)
    app = FastAPI()
    app.include_router(router)
    return app, feed, gate


def get(app, url, **kw):
    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            return await c.get(url, **kw)

    return asyncio.run(go())


def ids(body):
    return [int(ln[4:]) for ln in body.split("\n") if ln.startswith("id: ")]


def test_stream_headers_body_and_end_frame():
    app, feed, gate = make()
    feed.add("web", n=3)
    r = get(app, "/api/services/web/stream?after=0")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-cache"
    assert not any(h.lower().startswith("access-control") for h in r.headers)
    assert r.text.startswith(": open\n\n")
    assert ids(r.text) == [1, 2, 3]
    assert r.text.rstrip().endswith('data: {"reason": "max_age"}')
    assert gate.active == 0


@pytest.mark.parametrize("sid", ["nope", "Web", "../etc", "a" * 80, "web%2Fx"])
def test_unknown_or_non_slug_service_gives_404_and_takes_no_slot(sid):
    app, _, gate = make()
    r = get(app, f"/api/services/{sid}/stream")
    assert r.status_code == 404 and "detail" in r.json()
    assert gate.active == 0


def test_non_slug_id_is_rejected_without_a_lookup():
    seen = []
    app, _, _ = make(exists=lambda s: seen.append(s) or True)
    assert get(app, "/api/services/Bad_Id/stream").status_code == 404
    assert seen == []


def test_deleted_service_gives_404():
    alive = {"web": False}
    app, _, gate = make(exists=lambda s: alive.get(s, False))
    assert get(app, "/api/services/web/stream").status_code == 404
    assert gate.active == 0


@pytest.mark.parametrize("query,field", [("min_severity=8", "min_severity"), ("min_severity=-1", "min_severity"),
                                         ("tail=101", "tail"), ("tail=-1", "tail"), ("after=abc", "after"),
                                         ("after=-3", "after")])
def test_bad_parameters_give_422_with_the_field(query, field):
    app, _, gate = make()
    r = get(app, f"/api/services/web/stream?{query}")
    assert r.status_code == 422
    assert r.json()["field"] == field and "detail" in r.json()
    assert gate.active == 0


def test_non_integer_parameters_give_422():
    app, _, _ = make()
    assert get(app, "/api/services/web/stream?min_severity=x").status_code == 422
    assert get(app, "/api/services/web/stream?tail=x").status_code == 422


def test_unknown_service_wins_over_a_bad_parameter():
    app, _, _ = make()
    assert get(app, "/api/services/nope/stream?min_severity=8").status_code == 404
    assert get(app, "/api/services/nope/stream?after=abc").status_code == 404


def test_tail_returns_the_newest_matching_rows_first():
    app, feed, _ = make()
    feed.add("web", n=5)
    feed.add("other", n=2)
    r = get(app, "/api/services/web/stream?tail=3")
    assert ids(r.text) == [3, 4, 5]


def test_last_event_id_wins_over_after_and_a_bad_one_is_ignored():
    app, feed, _ = make()
    feed.add("web", n=8)
    assert ids(get(app, "/api/services/web/stream?after=2", headers={"Last-Event-ID": "5"}).text) == [6, 7, 8]
    assert ids(get(app, "/api/services/web/stream?after=6", headers={"Last-Event-ID": "abc"}).text) == [7, 8]
    assert ids(get(app, "/api/services/web/stream?after=6", headers={"Last-Event-ID": "-1"}).text) == [7, 8]


def test_min_severity_filters_rows():
    app, feed, _ = make()
    feed.add("web", severity=6)
    feed.add("web", severity=2)
    assert ids(get(app, "/api/services/web/stream?after=0&min_severity=3").text) == [2]


def test_the_twenty_first_stream_gets_429_with_retry_after():
    app, _, gate = make()
    held = [gate.acquire(time.monotonic()) for _ in range(20)]
    assert all(held)
    r = get(app, "/api/services/web/stream")
    assert r.status_code == 429 and r.headers["retry-after"] == "5"
    held[0].release()
    assert get(app, "/api/services/web/stream?after=0").status_code == 200


def test_the_route_is_async_and_alone_in_its_router():
    app, _, _ = make()
    routes = [r for r in app.routes if getattr(r, "path", "").startswith("/api")]
    assert [r.path for r in routes] == ["/api/services/{service_id}/stream"]
    assert inspect.iscoroutinefunction(routes[0].endpoint)
    assert routes[0].methods == {"GET"}


def test_frames_carry_no_forged_fields():
    app, feed, _ = make()
    feed.add("web", message='x\ndata: {"forged":1}\n\nid: 99\n')
    body = get(app, "/api/services/web/stream?after=0").text
    assert ids(body) == [1]
    datas = [ln for ln in body.split("\n") if ln.startswith("data: ")]
    assert json.loads(datas[0][6:])["message"].startswith("x\ndata:")
