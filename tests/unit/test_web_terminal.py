"""Web terminal, transcript, guided workflow and same-origin middleware."""

import asyncio

import httpx

from chimericmesh.api import create_app
from chimericmesh.events import Event
from chimericmesh.hub import TRANSCRIPT_MAX, Hub
from chimericmesh.listeners import IrcServer


def ev(msg="could not write block 1 No space left on device", host="db01"):
    return Event(source="syslog", host=host, app="pg", severity=3, message=msg)


def make():
    hub = Hub()
    app = create_app(hub, start_listeners=False)
    sent: list[tuple[str, str]] = []

    async def fake(channel, text):
        sent.append((channel, text))

    hub.irc_broadcast = fake  # install after create_app()
    return hub, app, sent


def call(app, method, url, **kw):
    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            return await c.request(method, url, **kw)

    return asyncio.run(run())


async def storm(hub, n=3):
    for _ in range(n):
        await hub.ingest(ev())


# --- transcript ---

def test_say_records_without_broadcaster():
    hub = Hub()
    asyncio.run(hub.say("#ops", "hello"))
    m = hub.transcript[-1]
    assert (m.id, m.sender, m.kind, m.text) == (1, "ChimericBot", "bot", "hello")


def test_record_strips_control_chars_and_caps():
    hub = Hub()
    m = hub.record("a\r\nb", "x\r\ny\x00z\x07" + "q" * 600, "user")
    assert m.sender == "ab" and m.text.startswith("xyzq") and len(m.text) == 500


def test_transcript_eviction_keeps_ids_monotonic():
    hub = Hub()
    for i in range(TRANSCRIPT_MAX + 25):
        hub.record("u", str(i), "user")
    ids = [m.id for m in hub.transcript]
    assert len(ids) == TRANSCRIPT_MAX == 300
    assert ids == sorted(set(ids)) and ids[0] == 26 and ids[-1] == 325


def test_irc_user_command_recorded_as_user():
    async def run():
        hub = Hub()
        irc = IrcServer(hub)
        server = await irc.start("127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            r, w = await asyncio.open_connection("127.0.0.1", port)
            w.write(b"NICK carol\r\nUSER c 0 * :c\r\nJOIN #ops\r\nPRIVMSG #ops :!help\r\n")
            await w.drain()
            await asyncio.sleep(0.3)
            w.close()
            return hub
        finally:
            server.close()

    hub = asyncio.run(run())
    users = [m for m in hub.transcript if m.kind == "user"]
    assert [(m.sender, m.text) for m in users] == [("carol", "!help")]
    assert any(m.kind == "bot" for m in hub.transcript)
    assert hub.total_events == 0  # commands are never ingested


# --- /api/chat ---

def test_chat_empty():
    _, app, _ = make()
    assert call(app, "GET", "/api/chat").json() == {"messages": [], "last_id": 0}


def test_chat_cursor_and_limit():
    hub, app, _ = make()
    for i in range(10):
        hub.record("u", f"m{i}", "user")
    j = call(app, "GET", "/api/chat?after=3&limit=4").json()
    assert [m["id"] for m in j["messages"]] == [4, 5, 6, 7] and j["last_id"] == 10
    assert len(call(app, "GET", "/api/chat?after=-5").json()["messages"]) == 10
    assert len(call(app, "GET", "/api/chat?limit=0").json()["messages"]) == 1
    assert len(call(app, "GET", "/api/chat?limit=9999").json()["messages"]) == 10
    assert call(app, "GET", "/api/chat?after=10").json()["messages"] == []
    assert set(j["messages"][0]) == {"id", "ts", "sender", "kind", "text"}


# --- /api/terminal ---

def term(app, text):
    return call(app, "POST", "/api/terminal", json={"text": text})


def test_terminal_status_and_chat_and_irc():
    hub, app, sent = make()
    r = term(app, "!status")
    j = r.json()
    assert r.status_code == 200 and j["ok"] and j["handled"] and len(j["replies"]) == 1
    assert sent and sent[0][1] == j["replies"][0]  # reaches IRC too
    msgs = call(app, "GET", "/api/chat").json()["messages"]
    assert [(m["sender"], m["kind"]) for m in msgs] == [("dashboard", "user"), ("ChimericBot", "bot")]


def test_terminal_incidents_and_explain():
    hub, app, _ = make()
    asyncio.run(storm(hub))
    inc = hub.incidents()[0]
    assert inc.id in term(app, "!incidents").json()["replies"][0]
    assert inc.id in term(app, f"!explain {inc.id}").json()["replies"][0]


def test_terminal_unknown_command():
    _, app, _ = make()
    j = term(app, "!bogus").json()
    assert j["handled"] and "unknown command" in j["replies"][0]


def test_terminal_non_command_not_ingested():
    hub, app, _ = make()
    j = term(app, "hello there").json()
    assert j["handled"] is False and j["replies"] == ["commands start with ! - try !help"]
    assert hub.total_events == 0
    assert hub.transcript[-1].kind == "system"
    term(app, "!status")
    assert hub.total_events == 0


def test_terminal_validation():
    _, app, _ = make()
    assert term(app, "").status_code == 422
    assert term(app, "   ").status_code == 422
    assert term(app, "!" + "a" * 200).status_code == 422
    assert call(app, "POST", "/api/terminal", json={}).status_code == 422
    assert term(app, "  !help  ").status_code == 200


def _with_proposal(hub):
    async def run():
        await storm(hub)
        return await hub.create_proposal(hub.incidents()[0].id, "free disk")

    return asyncio.run(run())


def test_approve_refused_for_non_operator():
    hub, app, _ = make()
    hub.operators = {"alice"}
    prop = _with_proposal(hub)
    j = term(app, f"!approve {prop.id}").json()
    assert "only operators" in j["replies"][0]
    assert prop.status == "pending"
    inc = hub.incidents()[0]
    assert "only operators" in term(app, f"!resolve {inc.id}").json()["replies"][0]
    assert not hub.resolved


def test_approve_and_resolve_allowed_for_dashboard_or_open():
    for ops in ({"dashboard"}, None):
        hub, app, _ = make()
        hub.operators = ops
        prop = _with_proposal(hub)
        assert term(app, f"!approve {prop.id}").json()["replies"] == [f"[mesh] {prop.id} approved"]
        assert prop.status == "approved"
        term(app, f"!resolve {hub.incidents()[0].id}")
        assert hub.resolved


# --- proposals contract ---

def test_proposals_endpoint_fields():
    hub, app, _ = make()
    _with_proposal(hub)
    items = call(app, "GET", "/api/proposals").json()
    # auto-approval (R9.7): the nine baseline keys plus the extended proposal fields and the approval object
    assert set(items[0]) == {"id", "incident_id", "action", "details", "status", "created",
                             "decided_by", "decided_at", "result_note",
                             "decision_origin", "service_id", "incident_opening", "approved_by_rule", "kind",
                             "files", "estimated_lines", "verification", "files_changed", "lines_changed",
                             "verification_result", "approval"}


# --- workflow ---

def keys_done(hub):
    return [s["key"] for s in hub.workflow()["steps"] if s["done"]]


def test_workflow_full_run():
    hub, app, _ = make()
    w = hub.workflow()
    assert [s["key"] for s in w["steps"]] == ["traffic", "incidents", "ask", "propose", "decide", "resolve"]
    assert w["next"] == "traffic" and not w["complete"] and keys_done(hub) == []
    assert w["you"] == {"nick": "dashboard", "operator": True}

    asyncio.run(hub.ingest(ev()))
    assert keys_done(hub) == ["traffic"] and hub.workflow()["next"] == "incidents"
    asyncio.run(storm(hub, 2))
    assert keys_done(hub) == ["traffic", "incidents"]
    term(app, "!incidents")
    assert keys_done(hub) == ["traffic", "incidents", "ask"]
    prop = asyncio.run(hub.create_proposal(hub.incidents()[0].id, "fix"))
    assert keys_done(hub)[-1] == "propose" and hub.workflow()["next"] == "decide"
    term(app, f"!deny {prop.id}")
    assert "decide" in keys_done(hub) and hub.workflow()["next"] == "resolve"
    term(app, f"!resolve {hub.incidents()[0].id}")
    w = call(app, "GET", "/api/workflow").json()
    assert w["complete"] and w["next"] is None and all(s["done"] for s in w["steps"])


def test_workflow_operator_flag():
    hub = Hub()
    hub.operators = {"alice"}
    assert hub.workflow()["you"]["operator"] is False
    hub.operators = {"dashboard"}
    assert hub.workflow()["you"]["operator"] is True


# --- same-origin middleware ---

def test_origin_middleware():
    _, app, _ = make()
    body = {"text": "!help"}
    r = call(app, "POST", "/api/terminal", json=body, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403 and r.json() == {"detail": "cross-origin request blocked"}
    assert call(app, "POST", "/api/terminal", json=body, headers={"Origin": "http://test"}).status_code == 200
    assert call(app, "POST", "/api/terminal", json=body).status_code == 200
    assert call(app, "GET", "/api/chat", headers={"Origin": "http://evil.example"}).status_code == 200


def test_dashboard_declares_icon_and_avoids_html_sinks():
    _, app, _ = make()
    html = call(app, 'GET', '/').text
    assert 'rel="icon"' in html  # avoids a console-noisy /favicon.ico 404
    for sink in ('innerHTML', 'outerHTML', 'insertAdjacentHTML', 'document.write'):
        assert sink not in html
