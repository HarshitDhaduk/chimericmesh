"""REST routes of discovery (R1.2 to R1.5, R3.1, R5.2, R5.4 to R5.6, O1, O4)."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from chimericmesh.discovery.accept import Acceptor, RegistrySink
from chimericmesh.discovery.catalog import CatalogLoad, load_catalog
from chimericmesh.discovery.manager import DiscoveryService, ScanManager
from chimericmesh.discovery.models import CandidatePath, Evidence, ScanOutcome, Suggestion
from chimericmesh.discovery.routes import build_router
from chimericmesh.discovery.schema import discovery_registry
from chimericmesh.hub import Hub
from chimericmesh.services.access import AccessControl
from chimericmesh.services.asgi import MAX_BODY_BYTES, BodyLimit
from chimericmesh.services.deps import make_require_admin
from chimericmesh.services.registry import ServiceRegistry
from chimericmesh.services.token import TokenStore
from chimericmesh.store import Store

FORMATS = frozenset({"syslog-text", "ncsa", "w3c", "xferlog", "plain"})
APP = ('{"version":1,"apps":[{"app_id":"nginx","name":"nginx","process_names":["nginx"],"ports":[80],'
       '"protocol_label":"http","justification":null,"log_paths":{"linux":[{"path":"/var/log/nginx/a.log",'
       '"format":"plain","kind":"access"}],"windows":[],"macos":[]}}]}')
LOAD = load_catalog(FORMATS, text=APP)


class Env:
    def __init__(self, tmp_path, *, load=LOAD, enforce=True, hold=None):
        self.tmp = tmp_path
        self.loop = asyncio.new_event_loop()          # one loop: a scan task must survive between requests
        self.logfile = tmp_path / "a.log"
        self.logfile.write_text("x", encoding="utf-8")
        self.store = Store.open(":memory:", registry=discovery_registry())
        home = tmp_path / "home"
        home.mkdir()
        self.hub = Hub(store=self.store)
        self.registry = ServiceRegistry.load(self.store, state=self.hub, protected=[str(home / ".c")], home=str(home))
        self.registry.set_known_formats(FORMATS)
        self.runner_calls = 0
        env = self

        def engine(inputs, runner, procfs, fs, next_id, cancel):
            env.runner_calls += 1
            if hold is not None:
                hold.wait(5)
            cand = CandidatePath(str(env.logfile), "plain", "access", True, True, 1, None)
            return ScanOutcome("done", False, "", (), (Suggestion(
                next_id(), "nginx", "nginx", "http", "high", Evidence("nginx", 1, 1, (80,)), (cand,), "a" * 16),))

        self.manager = ScanManager(load=load, engine=engine, runner=None, procfs=None, fs=None, store=self.store,
                                   registry=self.registry, known_formats=FORMATS, now=time.time, os_name="linux",
                                   is_admin=False, own_pid=1, env={})
        self.acceptor = Acceptor(self.manager, RegistrySink(self.registry), self.registry, self.store, FORMATS)
        tokens = TokenStore.open(None, in_memory=True, redact=self.store.add_redaction)
        self.access = (AccessControl(enforce=True, tokens=tokens, allowed_hosts=frozenset({"testserver"}),
                                     audit=self.store.audit) if enforce else AccessControl.disabled())
        self.app = FastAPI()
        self.app.add_middleware(BodyLimit, max_bytes=MAX_BODY_BYTES)
        self.app.include_router(build_router(DiscoveryService(self.manager, self.acceptor, load),
                                             make_require_admin(self.access)))
        self.token = tokens.current()

    def call(self, method, url, *, authed=True, **kw):
        headers = dict(kw.pop("headers", {}))
        if authed:
            headers["X-Admin-Token"] = self.token

        async def go():
            transport = httpx.ASGITransport(app=self.app, client=("7.7.7.7", 1))
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
                return await c.request(method, url, headers=headers, **kw)
        return self.loop.run_until_complete(go())

    def scan_and_wait(self):
        sid = self.call("POST", "/api/discovery/scan").json()["scan_id"]
        for _ in range(300):
            body = self.call("GET", f"/api/discovery/scans/{sid}").json()
            if body["state"] != "running":
                return body
            time.sleep(0.01)
        raise AssertionError("scan did not finish")

    def accept_body(self, **over):
        body = {"id": "web", "name": "Web", "log_paths": [{"path": str(self.logfile), "format": "plain"}]}
        body.update(over)
        return body

    def audit(self):
        return self.loop.run_until_complete(self.store.audit.read(300))

    def close(self):
        self.loop.run_until_complete(self.manager.aclose())
        self.loop.close()
        self.store.close()


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.close()


ROUTES = [
    ("POST", "/api/discovery/scan", None),
    ("GET", "/api/discovery/scans/sc1", None),
    ("POST", "/api/discovery/suggestions/sg1/accept", {"id": "web", "name": "W", "log_paths": []}),
    ("POST", "/api/discovery/suggestions/sg1/dismiss", None),
    ("GET", "/api/discovery/dismissals", None),
    ("DELETE", "/api/discovery/dismissals", None),
]


@pytest.mark.parametrize("method,url,body", ROUTES)
def test_without_a_token_every_route_is_401_and_nothing_runs(env, method, url, body):
    kw = {} if body is None else {"json": body}
    assert env.call(method, url, authed=False, **kw).status_code == 401
    assert env.runner_calls == 0 and env.registry.list() == []
    assert len([r for r in env.audit() if r["action"] == "auth_failed"]) == 1


def test_a_wrong_token_is_401_too(env):
    r = env.call("POST", "/api/discovery/scan", authed=False, headers={"X-Admin-Token": "nope"})
    assert r.status_code == 401 and env.runner_calls == 0


def test_scan_returns_202_and_the_result_has_the_documented_fields(env):
    r = env.call("POST", "/api/discovery/scan")
    assert r.status_code == 202 and r.json() == {"scan_id": "sc1", "state": "running"}
    for _ in range(300):
        body = env.call("GET", "/api/discovery/scans/sc1").json()
        if body["state"] != "running":
            break
        time.sleep(0.01)
    assert set(body) >= {"id", "state", "started_at", "finished_at", "limited", "os", "reason", "suggestions"}
    assert body["state"] == "done" and body["os"] == "linux"
    sug = body["suggestions"][0]
    assert sug["state"] == "new" and sug["candidate_log_paths"][0]["path"] == str(env.logfile)
    assert "code_folder" not in str(body)


def test_a_second_scan_while_running_is_409_with_the_running_id(tmp_path):
    import threading
    hold = threading.Event()
    e = Env(tmp_path, hold=hold)

    async def go():                      # one event loop: the scan task must outlive the first request
        transport = httpx.ASGITransport(app=e.app, client=("7.7.7.7", 1))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            h = {"X-Admin-Token": e.token}
            first = await c.post("/api/discovery/scan", headers=h)
            second = await c.post("/api/discovery/scan", headers=h)
            hold.set()
            return first, second
    try:
        first, second = asyncio.run(go())
        assert first.status_code == 202
        assert second.status_code == 409 and second.json()["scan_id"] == "sc1" and "detail" in second.json()
    finally:
        hold.set()
        e.close()


def test_a_disabled_catalog_gives_503_with_a_plain_sentence(tmp_path):
    e = Env(tmp_path, load=CatalogLoad(None, "apps[0].log_paths.linux[0].path: leaked parser text"))
    try:
        r = e.call("POST", "/api/discovery/scan")
        assert r.status_code == 503
        assert r.json()["detail"] == "discovery is disabled: the catalog could not be loaded"
        assert "parser" not in r.text and "apps[0]" not in r.text and e.runner_calls == 0
    finally:
        e.close()


def test_get_scan_unknown_id_is_404(env):
    assert env.call("GET", "/api/discovery/scans/sc99").status_code == 404


def test_accept_returns_201_with_the_service_and_its_log_sources(env):
    env.scan_and_wait()
    r = env.call("POST", "/api/discovery/suggestions/sg1/accept", json=env.accept_body())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["suggestion_id"] == "sg1" and body["service"]["id"] == "web"
    assert [s["path"] for s in body["service"]["log_sources"]] == [str(env.logfile)]
    assert body["service"]["log_sources"][0]["origin"] == "discovery"
    again = env.call("POST", "/api/discovery/suggestions/sg1/accept", json=env.accept_body(id="web2"))
    assert again.status_code == 409
    assert env.call("GET", "/api/discovery/scans/sc1").json()["suggestions"][0]["state"] == "accepted"


def test_accept_with_an_invalid_path_is_422_with_a_field_and_creates_nothing(env):
    env.scan_and_wait()
    r = env.call("POST", "/api/discovery/suggestions/sg1/accept",
                 json=env.accept_body(log_paths=[{"path": "relative.log", "format": "plain"}]))
    assert r.status_code == 422 and r.json()["field"] == "path" and r.json()["detail"]
    assert env.registry.list() == []


def test_accept_with_the_home_directory_as_code_folder_is_422_and_creates_nothing(env):
    env.scan_and_wait()
    r = env.call("POST", "/api/discovery/suggestions/sg1/accept", json=env.accept_body(code_folder=str(env.tmp / "home")))
    assert r.status_code == 422 and r.json()["field"] == "code_folder" and env.registry.list() == []


def test_accept_unknown_suggestion_is_404_and_existing_service_id_is_409(env):
    assert env.call("POST", "/api/discovery/suggestions/sg9/accept", json=env.accept_body()).status_code == 404
    env.scan_and_wait()
    asyncio.run(env.registry.create_service(__import__("chimericmesh.services.models", fromlist=["x"]).ServiceFields(
        id="web", name="W"), actor="t"))
    assert env.call("POST", "/api/discovery/suggestions/sg1/accept", json=env.accept_body()).status_code == 409


def test_dismiss_hides_the_app_from_the_next_scan_and_repeats_are_harmless(env):
    env.scan_and_wait()
    assert env.call("POST", "/api/discovery/suggestions/sg1/dismiss").json() == {"suggestion_id": "sg1", "dismissed": True}
    assert env.call("POST", "/api/discovery/suggestions/sg1/dismiss").status_code == 200
    rows = env.call("GET", "/api/discovery/dismissals").json()
    assert [r["app_id"] for r in rows] == ["nginx"]
    assert env.call("POST", "/api/discovery/suggestions/sg77/dismiss").status_code == 404


def test_dismissals_can_be_listed_and_cleared(env):
    env.scan_and_wait()
    env.call("POST", "/api/discovery/suggestions/sg1/dismiss")
    r = env.call("DELETE", "/api/discovery/dismissals")
    assert r.status_code == 200 and r.json() == {"cleared": 1}
    assert env.call("GET", "/api/discovery/dismissals").json() == []


def test_dismiss_of_an_accepted_suggestion_is_409(env):
    env.scan_and_wait()
    env.call("POST", "/api/discovery/suggestions/sg1/accept", json=env.accept_body())
    assert env.call("POST", "/api/discovery/suggestions/sg1/dismiss").status_code == 409


@pytest.mark.parametrize("body", [
    {"id": "web", "name": "W", "log_paths": [{"path": "/x", "format": "plain"}], "extra": 1},
    {"id": 5, "name": "W", "log_paths": []},
    {"id": "web", "name": "W", "log_paths": "no"},
    {"id": "web", "name": "W", "log_paths": [{"path": "/x", "format": "plain", "more": 1}]},
    {"id": "web", "name": "W", "log_paths": [{"path": "/x" * 600, "format": "plain"}]},
])
def test_hostile_bodies_are_422_and_create_nothing(env, body):
    env.scan_and_wait()
    r = env.call("POST", "/api/discovery/suggestions/sg1/accept", json=body)
    assert r.status_code == 422 and env.registry.list() == []


def test_a_one_megabyte_body_is_413(env):
    env.scan_and_wait()
    r = env.call("POST", "/api/discovery/suggestions/sg1/accept", content=b"x" * 1_000_000,
                 headers={"content-type": "application/json"})
    assert r.status_code == 413


def test_no_mcp_tool_exposes_discovery():
    """R1.5: reuse the tool-listing helper of test_kiro_assets (``mcp.list_tools``)."""
    from chimericmesh.mcp_server import mcp
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert names
    assert not [n for n in names if n.startswith(("discovery_", "scan_", "suggest"))]
    text = (Path(__file__).resolve().parents[2] / "src" / "chimericmesh" / "mcp_server.py").read_text(encoding="utf-8")
    assert "/api/discovery" not in text and "discovery" not in text.lower()


def test_with_enforcement_off_the_routes_work_without_a_token(tmp_path):
    e = Env(tmp_path, enforce=False)
    try:
        assert e.call("POST", "/api/discovery/scan", authed=False).status_code == 202
    finally:
        e.close()
