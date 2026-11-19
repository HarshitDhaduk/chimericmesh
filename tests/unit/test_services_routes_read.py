"""Read routes of the services router (R5.1, R5.5, R5.6, R8.1, R8.2)."""
from __future__ import annotations

import asyncio

import httpx
from fastapi import FastAPI

from chimericmesh.hub import Hub
from chimericmesh.services.access import AccessControl
from chimericmesh.services.deps import make_require_admin
from chimericmesh.services.models import LogSourceFields, ServiceFields
from chimericmesh.services.routes import build_router
from chimericmesh.services.token import TokenStore

CANARY = "SECRET-LINE-CANARY-12345"


def build(store, hub=None, *, enforce=True):
    hub = hub or Hub()
    registry_factory = __import__("chimericmesh.services.registry", fromlist=["ServiceRegistry"])
    registry = registry_factory.ServiceRegistry.load(store, state=hub)
    tokens = TokenStore.open(None, in_memory=True, redact=store.add_redaction)
    access = AccessControl(enforce=enforce, tokens=tokens, allowed_hosts=frozenset({"testserver"}),
                           audit=store.audit)
    app = FastAPI()
    app.include_router(build_router(registry, access, make_require_admin(access)))
    return app, registry, access


def get(app, url):
    async def run():
        transport = httpx.ASGITransport(app=app, client=("7.7.7.7", 1))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            return await c.get(url)
    return asyncio.run(run())


def test_list_returns_status_objects_in_creation_order_unassigned_last(svc_mem_store, tmp_path):
    app, registry, _ = build(svc_mem_store)
    folder = tmp_path / "code"
    folder.mkdir()
    asyncio.run(registry.create_service(ServiceFields(id="b-svc", name="B", code_folder=str(folder)), actor="t"))
    asyncio.run(registry.create_service(ServiceFields(id="a-svc", name="A"), actor="t"))
    r = get(app, "/api/services")
    assert r.status_code == 200
    body = r.json()
    assert [s["id"] for s in body] == ["b-svc", "a-svc", "unassigned"]
    first = body[0]
    for key in ("id", "name", "builtin", "hosts", "app_names", "protocols", "code_folder", "created_at",
                "updated_at", "last_seen", "state", "error_rate", "open_incidents", "events_total",
                "log_sources"):
        assert key in first
    assert first["state"] == "never_seen"
    assert first["code_folder"] is not None            # R8.2
    assert body[-1]["builtin"] is True


def test_get_one_unknown_and_unassigned(svc_mem_store):
    app, registry, _ = build(svc_mem_store)
    asyncio.run(registry.create_service(ServiceFields(id="web", name="Web"), actor="t"))
    assert get(app, "/api/services/web").json()["id"] == "web"
    assert get(app, "/api/services/nope").status_code == 404
    un = get(app, "/api/services/unassigned")
    assert un.status_code == 200
    assert un.json()["builtin"] is True and un.json()["created_at"] is None


def test_reads_need_no_token_with_enforcement_on(svc_mem_store):
    app, _, access = build(svc_mem_store, enforce=True)
    assert access.enforce
    assert get(app, "/api/services").status_code == 200


def test_responses_never_contain_token_or_file_contents(svc_mem_store, tmp_path):
    app, registry, access = build(svc_mem_store)
    log_file = tmp_path / "app.log"
    log_file.write_text(CANARY + "\n")
    asyncio.run(registry.create_service_with_sources(
        ServiceFields(id="web", name="Web"), [LogSourceFields(path=str(log_file), format="plain")],
        actor="t"))
    for url in ("/api/services", "/api/services/web"):
        text = get(app, url).text
        assert CANARY not in text
        assert access.tokens.current() not in text
    assert str(log_file).replace("\\", "\\\\") in get(app, "/api/services/web").text or \
        log_file.name in get(app, "/api/services/web").text


def test_names_are_returned_as_stored(svc_mem_store):
    app, registry, _ = build(svc_mem_store)
    asyncio.run(registry.create_service(ServiceFields(id="web", name="We\x00b\x07 app"), actor="t"))
    assert get(app, "/api/services/web").json()["name"] == "Web app"
