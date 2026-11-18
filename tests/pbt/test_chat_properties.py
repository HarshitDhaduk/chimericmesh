import asyncio
import re

import httpx
from hypothesis import given, settings, strategies as st

from chimericmesh.api import create_app
from chimericmesh.hub import Hub

CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


@given(st.lists(st.text(max_size=700), max_size=30))
@settings(max_examples=40)
def test_record_is_clean_and_bounded(texts):
    hub = Hub()
    for t in texts:
        m = hub.record("nick", t, "user")
        assert not CTRL.search(m.text) and len(m.text) <= 500
    ids = [m.id for m in hub.transcript]
    assert ids == sorted(set(ids)) and len(ids) <= 300


@given(st.text(max_size=300))
@settings(max_examples=40, deadline=None)
def test_terminal_never_500s(text):
    hub = Hub()
    app = create_app(hub, start_listeners=False)

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            return await c.post("/api/terminal", json={"text": text})

    r = asyncio.run(run())
    assert r.status_code in (200, 422)
    assert all(not CTRL.search(m.text) for m in hub.transcript)
