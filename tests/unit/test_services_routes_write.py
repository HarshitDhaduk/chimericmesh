"""Write routes of the services router: services and log sources (R1, R2.5, R3, R6.3, R8.3)."""
from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI

from chimericmesh.hub import Hub
from chimericmesh.services.access import AccessControl
from chimericmesh.services.deps import make_require_admin
from chimericmesh.services.registry import ServiceRegistry
from chimericmesh.services.routes import build_router
from chimericmesh.services.token import TokenStore


class Env:
    def __init__(self, store, hub=None):
        self.store = store
        self.hub = hub or Hub()
        self.registry = ServiceRegistry.load(store, state=self.hub)
        tokens = TokenStore.open(None, in_memory=True, redact=store.add_redaction)
        self.access = AccessControl(enforce=True, tokens=tokens, allowed_hosts=frozenset({"testserver"}),
                                    audit=store.audit)
        self.app = FastAPI()
        self.app.include_router(build_router(self.registry, self.access, make_require_admin(self.access)))
        self.token = tokens.current()

    @property
    def auth(self):
        return {"X-Admin-Token": self.token}

    def call(self, method, url, *, authed=True, **kw):
        headers = dict(kw.pop("headers", {}))
        if authed:
            headers.update(self.auth)

        async def run():
            transport = httpx.ASGITransport(app=self.app, client=("7.7.7.7", 1))
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
                return await c.request(method, url, headers=headers, **kw)
        return asyncio.run(run())

    def audit(self):
        return asyncio.run(self.store.audit.read(limit=200))


@pytest.fixture
def env(svc_mem_store):
    return Env(svc_mem_store)


def make(env, sid="web", **extra):
    r = env.call("POST", "/api/services", json={"id": sid, "name": sid.upper(), **extra})
    assert r.status_code == 201, r.text
    return r.json()


# ---- services ----------------------------------------------------------------------------------

WRITES = [
    ("POST", "/api/services", {"id": "x", "name": "X"}),
    ("PATCH", "/api/services/web", {"name": "Y"}),
    ("DELETE", "/api/services/web", None),
    ("POST", "/api/services/web/log-sources", {"path": "/tmp/a.log", "format": "plain"}),
    ("PATCH", "/api/services/web/log-sources/ls1", {"enabled": False}),
    ("DELETE", "/api/services/web/log-sources/ls1", None),
]


@pytest.mark.parametrize("method,url,body", WRITES)
def test_every_write_needs_the_token_and_changes_nothing(env, method, url, body):
    make(env, "web")
    before = env.registry.list()
    kw = {} if body is None else {"json": body}
    assert env.call(method, url, authed=False, **kw).status_code == 401
    assert env.registry.list() == before
    if body is not None:                      # valid JSON that fails validation still gets 401
        assert env.call(method, url, authed=False, json={"zzz": 1}).status_code == 401


def test_create_201_duplicate_409_and_bad_slug_422(env):
    body = make(env, "web")
    assert body["id"] == "web" and body["name"] == "WEB"
    assert env.call("POST", "/api/services", json={"id": "web", "name": "again"}).status_code == 409
    bad = env.call("POST", "/api/services", json={"id": "Bad Id", "name": "x"})
    assert bad.status_code == 422 and bad.json()["field"] == "id"


def test_deleted_id_cannot_be_reused(env):
    make(env, "web")
    assert env.call("DELETE", "/api/services/web").status_code == 200
    assert env.call("POST", "/api/services", json={"id": "web", "name": "x"}).status_code == 409
    assert env.call("POST", "/api/services", json={"id": "web2", "name": "x"}).status_code == 201


def test_101st_service_is_422_with_field(env):
    for i in range(100):
        asyncio.run(env.registry.create_service(
            __import__("chimericmesh.services.models", fromlist=["ServiceFields"]).ServiceFields(
                id=f"s{i}", name="n"), actor="t"))
    r = env.call("POST", "/api/services", json={"id": "extra", "name": "n"})
    assert r.status_code == 422 and r.json()["field"] == "services"


def test_bad_code_folder_is_422_field_code_folder(env, tmp_path):
    r = env.call("POST", "/api/services", json={"id": "web", "name": "n", "code_folder": str(tmp_path / "nope")})
    assert r.status_code == 422 and r.json()["field"] == "code_folder"
    r = env.call("POST", "/api/services", json={"id": "web", "name": "n", "code_folder": "relative/dir"})
    assert r.status_code == 422 and r.json()["field"] == "code_folder"


def test_unknown_field_is_422(env):
    assert env.call("POST", "/api/services", json={"id": "web", "name": "n", "evil": 1}).status_code == 422


def test_patch_200_404_403_and_null_semantics(env, tmp_path):
    folder = tmp_path / "code"
    folder.mkdir()
    make(env, "web", code_folder=str(folder))
    r = env.call("PATCH", "/api/services/web", json={"name": "New"})
    assert r.status_code == 200 and r.json()["name"] == "New" and r.json()["code_folder"] is not None
    assert env.call("PATCH", "/api/services/nope", json={"name": "x"}).status_code == 404
    assert env.call("PATCH", "/api/services/unassigned", json={"name": "x"}).status_code == 403
    assert env.call("PATCH", "/api/services/web", json={"name": None}).status_code == 422
    r = env.call("PATCH", "/api/services/web", json={"code_folder": None})
    assert r.status_code == 200 and r.json()["code_folder"] is None


def test_patch_without_code_folder_key_leaves_it(env, tmp_path):
    folder = tmp_path / "code"
    folder.mkdir()
    make(env, "web", code_folder=str(folder))
    r = env.call("PATCH", "/api/services/web", json={"hosts": ["h1"]})
    assert r.json()["code_folder"] is not None and r.json()["hosts"] == ["h1"]


def test_delete_200_404_403_and_events_survive(env):
    make(env, "web")
    r = env.call("DELETE", "/api/services/web")
    assert r.status_code == 200 and r.json() == {"deleted": "web"}
    assert env.call("DELETE", "/api/services/web").status_code == 404
    assert env.call("DELETE", "/api/services/unassigned").status_code == 403
    assert env.call("GET", "/api/services/web", authed=False).status_code == 404


def test_failing_subscriber_is_500_with_name_and_rolls_back(env):
    def boom(tx, change):
        raise RuntimeError("no")
    env.registry.subscribe(on_service_changed=boom, name="badsub")
    r = env.call("POST", "/api/services", json={"id": "web", "name": "n"})
    assert r.status_code == 500 and r.json() == {"detail": "subscriber badsub failed"}
    assert env.registry.get("web") is None


def test_audit_actor_is_rest_address_and_token_never_in_responses(env):
    make(env, "web")
    rows = [r for r in env.audit() if r["action"] == "service_created"]
    assert rows and rows[0]["actor"] == "rest:7.7.7.7"
    r = env.call("POST", "/api/services", json={"id": "web", "name": "n"})
    assert env.token not in r.text


# ---- log sources -------------------------------------------------------------------------------

def test_log_source_create_defaults_and_errors(env, tmp_path):
    make(env, "web")
    log = tmp_path / "a.log"
    r = env.call("POST", "/api/services/web/log-sources", json={"path": str(log), "format": "plain"})
    assert r.status_code == 201
    body = r.json()
    assert body["id"] == "ls1" and body["backfill_lines"] == 0 and body["origin"] == "user"
    assert not log.exists()                                    # never created or opened
    assert env.call("POST", "/api/services/nope/log-sources",
                    json={"path": str(log), "format": "plain"}).status_code == 404
    assert env.call("POST", "/api/services/unassigned/log-sources",
                    json={"path": str(log), "format": "plain"}).status_code == 403
    d = tmp_path / "adir"
    d.mkdir()
    r = env.call("POST", "/api/services/web/log-sources", json={"path": str(d), "format": "plain"})
    assert r.status_code == 422 and r.json()["field"] == "path"
    r = env.call("POST", "/api/services/web/log-sources", json={"path": "rel.log", "format": "plain"})
    assert r.status_code == 422 and r.json()["field"] == "path"
    r = env.call("POST", "/api/services/web/log-sources", json={"path": str(log), "format": "nope"})
    assert r.status_code == 422 and r.json()["field"] == "format"
    r = env.call("POST", "/api/services/web/log-sources",
                 json={"path": str(log), "format": "plain", "backfill_lines": 1001})
    assert r.status_code == 422 and r.json()["field"] == "backfill_lines"


def test_same_path_twice_is_accepted_and_origin_cannot_be_set(env, tmp_path):
    make(env, "web")
    log = str(tmp_path / "a.log")
    body = {"path": log, "format": "plain"}
    assert env.call("POST", "/api/services/web/log-sources", json=body).status_code == 201
    assert env.call("POST", "/api/services/web/log-sources", json=body).status_code == 201
    r = env.call("POST", "/api/services/web/log-sources", json={**body, "origin": "discovery"})
    assert r.status_code == 422
    assert {s.origin for s in env.registry.get("web").log_sources} == {"user"}


def test_log_source_patch_and_delete(env, tmp_path):
    make(env, "web")
    env.call("POST", "/api/services/web/log-sources", json={"path": str(tmp_path / "a.log"), "format": "plain"})
    r = env.call("PATCH", "/api/services/web/log-sources/ls1",
                 json={"enabled": False, "format": "ncsa", "host": "h9"})
    assert r.status_code == 200
    assert r.json()["enabled"] is False and r.json()["format"] == "ncsa" and r.json()["host"] == "h9"
    assert env.call("PATCH", "/api/services/web/log-sources/ls9", json={"enabled": True}).status_code == 404
    r = env.call("PATCH", "/api/services/web/log-sources/ls1", json={"host": None})
    assert r.status_code == 200 and r.json()["host"] is None
    assert env.call("PATCH", "/api/services/web/log-sources/ls1", json={"enabled": None}).status_code == 422
    r = env.call("DELETE", "/api/services/web/log-sources/ls1")
    assert r.status_code == 200 and r.json() == {"deleted": "ls1"}
    assert env.call("DELETE", "/api/services/web/log-sources/ls1").status_code == 404


def test_status_provider_is_consulted_only_by_read_routes(env, tmp_path):
    calls = []
    env.registry.set_status_provider(lambda sid: calls.append(sid))
    make(env, "web")
    env.call("POST", "/api/services/web/log-sources", json={"path": str(tmp_path / "a.log"), "format": "plain"})
    env.call("PATCH", "/api/services/web/log-sources/ls1", json={"enabled": False})
    env.call("DELETE", "/api/services/web/log-sources/ls1")
    assert calls == []
    env.call("GET", "/api/services", authed=False)
    env.call("POST", "/api/services/web/log-sources", json={"path": str(tmp_path / "b.log"), "format": "plain"})
    env.call("GET", "/api/services", authed=False)
    assert calls == ["ls2"]
