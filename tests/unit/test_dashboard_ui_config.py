"""GET /api/ui-config (dashboard task 3.3; H1)."""
from __future__ import annotations

import asyncio
import inspect

import httpx

from approvals_helpers import collect_garbage  # noqa: F401  (autouse: frees apps and their stores)
from chimericmesh.api import build_registry, create_app
from chimericmesh.hub import Hub
from chimericmesh.parsers.formats import FORMAT_NAMES
from chimericmesh.services.validate import KNOWN_PROTOCOLS
from chimericmesh.store import Store

BASE = "http://127.0.0.1:8000"


def call(app, path="/api/ui-config", **kw):
    async def go():
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 4000))
        async with httpx.AsyncClient(transport=transport, base_url=BASE) as c:
            return await c.get(path, **kw)

    return asyncio.run(go())


def row_counts(app) -> dict[str, int]:
    def read(tx):
        names = [r[0] for r in tx.all("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {n: tx.one(f'SELECT count(*) FROM "{n}"')[0] for n in names}

    return app.state.store.read_sync(read, allow_blocking=True)


def test_off_by_default_for_a_bare_hub():
    app = create_app(Hub(), start_listeners=False)
    r = call(app)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"enforce_access", "token_file", "formats", "protocols"}
    assert body["enforce_access"] is False and body["token_file"] is None
    assert body["formats"] == list(FORMAT_NAMES)
    assert body["protocols"] == list(KNOWN_PROTOCOLS)
    app.state.store.close()


def test_enforced_gives_the_token_file_path_and_never_the_token(tmp_path):
    store = Store.open(tmp_path / "u.db", registry=build_registry())
    app = create_app(Hub(store=store), store=store, start_listeners=False, enforce_access=True)
    token = app.state.access.tokens.current()
    r = call(app)
    assert r.status_code == 200
    body = r.json()
    assert body["enforce_access"] is True
    assert body["token_file"] == str(app.state.access.token_path)
    assert isinstance(body["token_file"], str) and body["token_file"]
    assert token not in r.text
    path = app.state.access.token_path
    if path is not None and path.exists():
        assert path.read_text(encoding="utf-8").strip() not in r.text
    app.state.store.close()


def test_enforced_with_a_memory_only_token_gives_null_token_file():
    app = create_app(Hub(), start_listeners=False, enforce_access=True)
    body = call(app).json()
    assert body["enforce_access"] is True and body["token_file"] is None
    app.state.store.close()


def test_the_route_needs_no_token_and_writes_nothing():
    app = create_app(Hub(), start_listeners=False, enforce_access=True)
    before = row_counts(app)
    r = call(app)  # no X-Admin-Token header
    assert r.status_code == 200
    assert row_counts(app) == before
    app.state.store.close()


def test_the_route_is_async():
    app = create_app(Hub(), start_listeners=False)
    route = next(r for r in app.routes if getattr(r, "path", "") == "/api/ui-config")
    assert inspect.iscoroutinefunction(route.endpoint) and route.methods == {"GET"}
    app.state.store.close()
