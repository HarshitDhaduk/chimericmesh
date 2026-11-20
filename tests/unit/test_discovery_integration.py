"""Success Criteria 1 to 4 end to end through ``create_app`` on a temporary database (R1.6, R4.6, R5.1 to R5.4, R5.7)."""
from __future__ import annotations

import asyncio
import builtins
import json
import os
import socket
import threading
import time

import httpx
import pytest

from chimericmesh import api as api_mod
from chimericmesh.api import create_app
from chimericmesh.discovery.catalog import load_catalog
from chimericmesh.discovery.models import RunResult

FORMATS = frozenset({"syslog-text", "ncsa", "w3c", "xferlog", "plain"})
WIN = os.name == "nt"
ENV = "ProgramData" if WIN else "HOME"
PROC = "zz-fakeproc"


def rule(path, fmt="plain", kind="access"):
    return {"path": path, "format": fmt, "kind": kind}


def catalog_text():
    def entry(app_id, proc, port, name, win, lin):
        return {"app_id": app_id, "name": name, "process_names": [proc], "ports": [port], "protocol_label": "x",
                "justification": "integration test entry",
                "log_paths": {"linux": [rule(lin)], "windows": [rule(win)], "macos": []}}
    return json.dumps({"version": 1, "apps": [
        entry("fakeapp", PROC, 8081, "Fake App", "%ProgramData%\\logs\\fake.log", "$HOME/logs/fake.log"),
        entry("otherapp", "zz-other", 8082, "Other App", "%ProgramData%\\logs\\other.log", "$HOME/logs/other.log"),
    ]})


class FakeRunner:
    calls: list = []

    def __init__(self, os_name, environ):
        pass

    def run(self, tool, *, timeout, max_bytes, cancel):
        FakeRunner.calls.append(tool.name)
        if tool.name == "tasklist":
            return RunResult(True, f'"{PROC}.exe","812","x","0","1 K"\n"zz-other.exe","813","x","0","1 K"\n'
                                   '"unrelated-secret.exe","900","x","0","1 K"\n', "")
        if tool.name == "netstat" and WIN:
            return RunResult(True, "  TCP    0.0.0.0:8081   0.0.0.0:0   LISTENING   812\n", "")
        if tool.name == "ss":
            return RunResult(True, f'tcp LISTEN 0 1 0.0.0.0:8081 0.0.0.0:* users:(("{PROC}",pid=812,fd=3))\n', "")
        return RunResult(False, "", "missing")


class FakeProcFs:
    def __init__(self, *a, **k):
        pass

    def pids(self):
        return [812, 813, 900]

    def comm(self, pid):
        return {812: PROC, 813: "zz-other", 900: "unrelated-secret"}[pid]


@pytest.fixture
def world(tmp_path, monkeypatch):
    FakeRunner.calls = []
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "fake.log").write_text("first line\n", encoding="utf-8")
    (logs / "other.log").write_text("other\n", encoding="utf-8")
    monkeypatch.setenv(ENV, str(tmp_path))
    monkeypatch.setenv("CHIMERICMESH_DB", str(tmp_path / "db" / "m.db"))
    monkeypatch.setattr(api_mod, "SubprocessRunner", FakeRunner)
    monkeypatch.setattr(api_mod, "LinuxProcFs", FakeProcFs)
    real = api_mod.load_catalog
    monkeypatch.setattr(api_mod, "load_catalog", lambda formats: load_catalog(formats, text=catalog_text()))
    assert real is not None
    return tmp_path


def token_of(app):
    return {"X-Admin-Token": app.state.access.tokens.current()}


async def wait_scan(c, scan_id, headers):
    for _ in range(500):
        body = (await c.get(f"/api/discovery/scans/{scan_id}", headers=headers)).json()
        if body["state"] != "running":
            return body
        await asyncio.sleep(0.01)
    raise AssertionError("scan did not finish")


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1")


def test_sc3_without_a_token_the_scan_is_401_and_the_runner_is_never_called(world):
    app = create_app(start_listeners=False)

    async def go():
        async with client(app) as c:
            return await c.post("/api/discovery/scan")
    try:
        assert asyncio.run(go()).status_code == 401 and FakeRunner.calls == []
    finally:
        app.state.store.close()


def test_sc1_and_sc2_two_suggestions_no_candidate_opened_and_no_socket(world, monkeypatch):
    app = create_app(start_listeners=False)
    real_open = builtins.open
    opened = []
    socket_calls = []

    def guarded(file, *a, **k):
        if str(world / "logs") in str(file):
            opened.append(str(file))
            raise AssertionError("a candidate log file was opened")
        return real_open(file, *a, **k)

    async def go():
        async with client(app) as c:
            h = token_of(app)
            monkeypatch.setattr(builtins, "open", guarded)
            real_socket = socket.socket
            # The Windows event loop needs sockets for its own wakeups, so the patch only fails for the scan thread.
            # A subclass, not a function: asyncio itself uses isinstance(x, socket.socket).
            class GuardedSocket(real_socket):
                def __init__(self, *a, **k):
                    if threading.current_thread().name.startswith("discovery-scan"):
                        socket_calls.append(a)
                        raise AssertionError("the scan created a socket")
                    super().__init__(*a, **k)
            monkeypatch.setattr(socket, "socket", GuardedSocket)
            try:
                sid = (await c.post("/api/discovery/scan", headers=h)).json()["scan_id"]
                return await wait_scan(c, sid, h)
            finally:
                monkeypatch.setattr(socket, "socket", real_socket)
                monkeypatch.setattr(builtins, "open", real_open)
    try:
        body = asyncio.run(go())
    finally:
        app.state.store.close()
    assert body["state"] == "done" and opened == [] and socket_calls == []
    got = {s["app_id"]: s for s in body["suggestions"]}
    assert set(got) == {"fakeapp", "otherapp"} and all(s["confidence"] == "high" for s in got.values())
    assert got["fakeapp"]["candidate_log_paths"][0]["exists"] is True
    text = json.dumps(body)
    assert "unrelated-secret" not in text and "code_folder" not in text and "first line" not in text


def test_sc4_accept_then_an_appended_line_reaches_the_hub_within_two_seconds(world):
    app = create_app(start_listeners=False)

    async def go():
        async with app.router.lifespan_context(app):
            async with client(app) as c:
                h = token_of(app)
                sid = (await c.post("/api/discovery/scan", headers=h)).json()["scan_id"]
                body = await wait_scan(c, sid, h)
                sug = next(s for s in body["suggestions"] if s["app_id"] == "fakeapp")
                path = sug["candidate_log_paths"][0]["path"]
                r = await c.post(f"/api/discovery/suggestions/{sug['suggestion_id']}/accept", headers=h,
                                 json={"id": "fake", "name": "Fake", "log_paths": [{"path": path, "format": "plain"}]})
                assert r.status_code == 201, r.text
                assert (await c.get("/api/services/fake")).json()["log_sources"][0]["origin"] == "discovery"
                await asyncio.sleep(0.7)                            # let the tailer attach
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write("ERROR the fake app failed\n")
                start = time.monotonic()
                while time.monotonic() - start < 2.0:
                    events = (await c.get("/api/events", params={"service_id": "fake"})).json()
                    if events:
                        return events, time.monotonic() - start
                    await asyncio.sleep(0.05)
                return [], 2.0
    events, took = asyncio.run(go())
    assert events and events[0]["service_id"] == "fake" and took < 2.0


def test_second_scan_skips_the_accepted_app_and_dismissals_survive_a_restart_until_cleared(world):
    app = create_app(start_listeners=False)

    async def first():
        async with client(app) as c:
            h = token_of(app)
            sid = (await c.post("/api/discovery/scan", headers=h)).json()["scan_id"]
            body = await wait_scan(c, sid, h)
            by = {s["app_id"]: s for s in body["suggestions"]}
            r = await c.post(f"/api/discovery/suggestions/{by['fakeapp']['suggestion_id']}/accept", headers=h,
                             json={"id": "fake", "name": "Fake",
                                   "log_paths": [{"path": by["fakeapp"]["candidate_log_paths"][0]["path"],
                                                  "format": "plain"}]})
            assert r.status_code == 201
            d = await c.post(f"/api/discovery/suggestions/{by['otherapp']['suggestion_id']}/dismiss", headers=h)
            assert d.status_code == 200
            sid2 = (await c.post("/api/discovery/scan", headers=h)).json()["scan_id"]
            second = await wait_scan(c, sid2, h)
            audit = await app.state.store.audit.read(200)
            return second, audit
    try:
        second, audit = asyncio.run(first())
    finally:
        app.state.store.close()
    assert second["suggestions"] == []                                  # accepted app covered, dismissed app hidden
    actions = [a["action"] for a in audit]
    assert {"discovery_scan", "discovery_accepted", "discovery_dismissed"} <= set(actions)
    flat = json.dumps(audit)
    assert PROC not in flat and str(world / "logs") not in flat and "unrelated-secret" not in flat

    app2 = create_app(start_listeners=False)                            # restart on the same database

    async def after_restart():
        async with client(app2) as c:
            h = token_of(app2)
            sid = (await c.post("/api/discovery/scan", headers=h)).json()["scan_id"]
            hidden = await wait_scan(c, sid, h)
            listed = (await c.get("/api/discovery/dismissals", headers=h)).json()
            cleared = (await c.delete("/api/discovery/dismissals", headers=h)).json()
            sid = (await c.post("/api/discovery/scan", headers=h)).json()["scan_id"]
            back = await wait_scan(c, sid, h)
            return hidden, listed, cleared, back
    try:
        hidden, listed, cleared, back = asyncio.run(after_restart())
    finally:
        app2.state.store.close()
    assert "otherapp" not in {s["app_id"] for s in hidden["suggestions"]}
    assert [d["app_id"] for d in listed] == ["otherapp"] and cleared == {"cleared": 1}
    assert [s["app_id"] for s in back["suggestions"]] == ["otherapp"]
