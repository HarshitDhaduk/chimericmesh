"""Dashboard integration module (task 10.2): page, headers, stream, the privileged table, the guard hook."""
from __future__ import annotations
import asyncio
import json
import re
import subprocess
import sys
import time
from pathlib import Path
import httpx
import pytest
from approvals_helpers import collect_garbage  # noqa: F401  (autouse: frees apps and their stores)
import chimericmesh.api as api_module
from chimericmesh.api import create_app
from chimericmesh.hub import Hub
from chimericmesh.web.feed import StreamConfig
from dashboard_static import code_only, script_text, load_page
BASE = "http://127.0.0.1:8000"
HOOK = Path(__file__).resolve().parents[2] / ".kiro" / "hooks" / "guard_innerhtml.py"
FORGED = 'boom\n\ndata: {"forged":1}\n\nid: 99\n'
EXISTING_IDS = {"termForm", "termInput", "transcript", "proposals", "events", "sendForm", "identity",
                "tokenForm", "tokenInput", "tokenNote"}
SINKS = ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "DOMParser", "srcdoc",
         "createContextualFragment", "eval(", "new Function")


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        self.now += 0.001
        return self.now


@pytest.fixture
def app(monkeypatch):
    real = api_module.build_stream_router

    def short(*a, **k):
        return real(*a, **{**k, "config": StreamConfig(max_seconds=5.0, poll_interval=0.02, heartbeat=0.2)})

    monkeypatch.setattr(api_module, "build_stream_router", short)
    app = create_app(Hub(clock=Clock()), start_listeners=False, enforce_access=True)
    yield app
    app.state.store.close()


def client(app, ip="127.0.0.1"):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(ip, 4000)), base_url=BASE)


def run(app, fn):
    async def go():
        async with client(app) as c:
            return await fn(c)
    return asyncio.run(go())


def data_lines(text):
    return [ln[6:] for ln in text.split("\n") if ln.startswith("data: ")]


def test_index_has_headers_existing_ids_and_no_sink(app):
    r = run(app, lambda c: c.get("/"))
    assert r.status_code == 200
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"
    page = r.text
    for i in EXISTING_IDS:
        assert f'id="{i}"' in page, i
    code = code_only(page)
    for sink in SINKS:
        assert sink not in code, sink
    assert not re.search(r"\[\s*['\"](innerHTML|outerHTML)['\"]\s*\]", code)
    assert "const ACTIONS = {" in page and "const PRIVILEGED = [" in page  # the lock registry


def test_stream_delivers_a_storm_event_and_one_data_line_per_forged_message(app):
    async def go(c):
        r = await c.post("/api/services", json={"id": "web", "name": "web", "hosts": ["db01", "web-host"]},
                         headers={"X-Admin-Token": app.state.access.tokens.current()})
        assert r.status_code == 201, r.text
        t = asyncio.create_task(c.get("/api/services/web/stream?after=0"))
        await asyncio.sleep(0.3)
        assert (await c.post("/api/demo/storm")).status_code == 202
        line = "<131>Nov 19 10:00:01 web-host app[1]: " + FORGED
        assert (await c.post("/api/ingest", json={"source": "syslog", "line": line})).status_code == 202
        await asyncio.sleep(0.5)
        assert (await c.delete("/api/services/web",
                               headers={"X-Admin-Token": app.state.access.tokens.current()})).status_code in (200, 204)
        return await asyncio.wait_for(t, 6)
    r = run(app, go)
    assert r.status_code == 200
    events = [json.loads(d) for d in data_lines(r.text) if d.startswith("{")]
    assert any(e.get("host") == "db01" or "db01" in json.dumps(e) for e in events), "storm event missing"
    forged = [e for e in events if "forged" in json.dumps(e)]
    assert len(forged) == 1  # one data: line, so the forged lines never became frames of their own
    assert "\nid: 99" not in r.text.replace("\\n", "")
    assert [ln for ln in r.text.split("\n") if ln.startswith("id: 99")] == []
    # the decoded message equals the one the hub holds (the original text)
    held = [e for e in app.state.hub.recent(50) if "forged" in e.message]
    assert len(held) == 1 and forged[0]["message"] == held[0].message


def privileged_rows():
    m = re.search(r"const PRIVILEGED = \[(.*?)\n\];", script_text(), re.S)
    return re.findall(r"\[\s*'([A-Z]+)'\s*,\s*'([^']+)'\s*\]", m.group(1))


def concrete(rx: str) -> str:
    return re.sub(r"\[\^/\]\+", "x1", rx.strip("^$"))


TOKEN_FREE = [
    ("POST", "/api/rules/pause"), ("POST", "/api/rules/x1/pause"), ("POST", "/api/rules/x1/revoke"),
    ("POST", "/api/proposals/x1/withdraw"), ("GET", "/api/rules"), ("GET", "/api/audit"),
    ("GET", "/api/services"), ("GET", "/api/proposals"), ("GET", "/api/incidents"), ("GET", "/api/stats"),
    ("GET", "/api/approvals/status"), ("GET", "/api/ui-config"), ("GET", "/api/events"),
    ("POST", "/api/ingest"), ("POST", "/api/demo/storm"), ("POST", "/api/proposals"),
    ("GET", "/api/services/x1/stream"),
]


def test_privileged_table_equals_the_routes_that_answer_401(app):
    rows = [(m, concrete(rx)) for m, rx in privileged_rows() if rx != "^/api/terminal$"]
    assert len(rows) >= 18

    async def go(c):
        out = {}
        for n, (m, p) in enumerate(rows):  # one source address per call: the lockout counts bad tokens per address
            async with client(app, f"127.0.1.{n + 1}") as cc:
                r = await cc.request(m, p, json={}) if m in ("POST", "PATCH", "DELETE") else await cc.request(m, p)
            out[(m, p)] = r.status_code
        return out
    got = run(app, go)
    for key, status in got.items():
        assert status in (401, 422), (key, status)
        assert status == 401, key  # the dependency answers before the body is read
    # every admin route of the server is in the table (the page cannot drift)
    table = {(m, p) for m, p in rows}
    admin = set()
    for r in app.routes:
        deps = [getattr(d.call, "__name__", "") for d in getattr(getattr(r, "dependant", None), "dependencies", [])]
        if r.path.startswith("/api") and any(n in ("require_admin", "admin") for n in deps):
            for method in r.methods - {"HEAD"}:
                admin.add((method, r.path))
    def covered(method, path):
        sample = re.sub(r"\{[^}]+\}", "x1", path)
        return (method, sample) in table or any(m == method and re.fullmatch(concrete_rx(rx), sample)
                                                for m, rx in privileged_rows())
    for method, path in sorted(admin):
        if (method, path) == ("POST", "/api/terminal"):
            continue
        assert covered(method, path), (method, path)


def concrete_rx(rx: str) -> str:
    return rx


def test_token_free_routes_never_answer_401(app):
    async def go(c):
        out = {}
        for m, p in TOKEN_FREE:
            if p.endswith("/stream"):
                continue  # covered by the stream tests
            r = await c.request(m, p, json={}) if m == "POST" else await c.request(m, p)
            out[(m, p)] = r.status_code
        return out
    for key, status in run(app, go).items():
        assert status != 401, key


def test_terminal_row_m1_approve_needs_the_token_help_does_not(app):
    async def go(c):
        a = await c.post("/api/terminal", json={"text": "!approve p1"})
        h = await c.post("/api/terminal", json={"text": "!help"})
        return a.status_code, h.status_code
    a, h = run(app, go)
    assert a == 401 and h != 401


def test_ui_config_is_open_on_an_enforcing_hub(app):
    r = run(app, lambda c: c.get("/api/ui-config"))
    assert r.status_code == 200 and r.json()["enforce_access"] is True


def test_the_guard_hook_still_blocks_a_sink_in_a_web_path(tmp_path):
    target = tmp_path / "src" / "chimericmesh" / "web" / "index.html"
    target.parent.mkdir(parents=True)
    target.write_text("<p>ok</p>", encoding="utf-8")
    payload = json.dumps({"tool_name": "fs_write", "tool_input": {"path": str(target), "text": "x.innerHTML += y"}})
    r = subprocess.run([sys.executable, str(HOOK)], input=payload, capture_output=True, text=True)
    assert r.returncode == 2
    ok = json.dumps({"tool_name": "fs_write", "tool_input": {"path": str(target), "text": "x.textContent = y"}})
    assert subprocess.run([sys.executable, str(HOOK)], input=ok, capture_output=True, text=True).returncode == 0
