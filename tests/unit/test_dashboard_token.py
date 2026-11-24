"""Interim dashboard token prompt in ``web/index.html`` (R6.9, Success Criterion 9, task 7.10)."""
from __future__ import annotations

import asyncio
import re
from html.parser import HTMLParser
from pathlib import Path

import httpx

from chimericmesh.api import create_app
from chimericmesh.hub import Hub
from chimericmesh.parsers import parse_syslog

PAGE = Path(__file__).resolve().parents[2] / "src" / "chimericmesh" / "web" / "index.html"
DISK = "<27>Nov 13 11:10:08 db01 pg[1]: FATAL: could not write block %d: No space left on device"


def html() -> str:
    return PAGE.read_text(encoding="utf-8")


def script() -> str:
    return "\n".join(re.findall(r"<script>(.*?)</script>", html(), re.S))


class Ids(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids: set[str] = set()

    def handle_starttag(self, tag, attrs):
        for k, v in attrs:
            if k == "id" and v:
                self.ids.add(v)


def ids() -> set[str]:
    p = Ids()
    p.feed(html())
    p.close()
    return p.ids


def test_prompt_exists_and_page_still_parses_with_existing_ids():
    found = ids()
    assert {"tokenForm", "tokenInput", "tokenNote"} <= found
    assert {"termForm", "termInput", "transcript", "proposals", "events", "sendForm", "identity"} <= found


def test_token_lives_in_session_storage_only():
    js = script()
    assert "sessionStorage.setItem(TOKEN_KEY" in js and "sessionStorage.getItem(TOKEN_KEY" in js
    # localStorage holds only the guide preference (PREF_KEY); the token key never goes there.
    assert not re.search(r"localStorage\.\w+\((TOKEN_KEY|value|token)", js)
    assert "document.cookie" not in js
    assert not re.search(r"[?&](token|admin)", js)
    assert "URLSearchParams" not in js and "location.hash" not in js


def test_header_only_for_privileged_routes_and_only_when_stored():
    # Superseded by the dashboard module (task 4.3, design O7): the interim two-route regex became the
    # PRIVILEGED table; the header is added only by authHeaders, only for a route in that table (or the
    # send-if-present permissions snippet), and only when a token is stored.
    js = script()
    m = re.search(r"const PRIVILEGED = \[(.*?)\n\];", js, re.S)
    assert m, "PRIVILEGED table not found"
    rows = re.findall(r"\[\s*'([A-Z]+)'\s*,\s*'([^']+)'\s*\]", m.group(1))

    def privileged(method, path):
        return any(mm == method and re.search(rx, path) for mm, rx in rows)

    assert privileged("POST", "/api/terminal") and privileged("POST", "/api/proposals/p1/decide")
    assert not privileged("POST", "/api/proposals") and not privileged("POST", "/api/ingest")
    assert not privileged("POST", "/api/demo/storm") and not privileged("POST", "/api/proposals/p1/complete")
    assert not privileged("GET", "/api/proposals") and not privileged("GET", "/api/stats")
    assert js.count("X-Admin-Token") == 1
    body = re.search(r"function authHeaders\(method, path\) \{(.*?)\n\}", js, re.S)
    assert body, "authHeaders not found"
    assert "if (!token || !(needsToken(method, path) || sendsTokenIfPresent(method, path))) return {};" in body.group(1)
    assert "X-Admin-Token" in body.group(1)
    assert "TOKEN_ROUTES" not in js


def test_401_clears_token_and_shows_fixed_text():
    js = script()
    assert "r.status === 401" in js
    assert "sessionStorage.removeItem" in js
    assert "TOKEN_PROMPT_TEXT" in js
    m = re.search(r"const TOKEN_PROMPT_TEXT = '([^']+)';", js)
    assert m and "token" in m.group(1).lower()
    # the prompt text is never taken from the server response
    assert not re.search(r"tokenNote[^;]*(r\.text|await r\.json|detail)", js)


def test_no_html_sinks_and_token_never_logged_or_written():
    text = html()
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert sink not in text
    js = script()
    for call in re.findall(r"console\.\w+\(([^)]*)\)", js):
        assert "token" not in call.lower()
    assert not re.search(r"textContent\s*=\s*(token|readToken\(\))", js)
    assert not re.search(r"(runCommand|record|appendMessages)\([^)]*token", js, re.I)


def call(app, method, url, **kw):
    async def run():
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as c:
            return await c.request(method, url, **kw)
    return asyncio.run(run())


def test_app_terminal_approve_with_the_header_from_app_state():
    hub = Hub()
    hub.operators = None
    app = create_app(hub, start_listeners=False, enforce_access=True)

    async def quiet(channel, text):
        pass
    hub.irc_broadcast = quiet

    async def seed():
        for i in range(3):
            await hub.ingest(parse_syslog(DISK % i))
        return await hub.create_proposal(hub.incidents()[0].id, "rotate logs")
    prop = asyncio.run(seed())
    try:
        assert call(app, "POST", "/api/terminal", json={"text": f"!approve {prop.id}"}).status_code == 401
        token = app.state.access.tokens.current()
        r = call(app, "POST", "/api/terminal", json={"text": f"!approve {prop.id}"},
                 headers={"X-Admin-Token": token})
        assert r.status_code == 200 and prop.status == "approved"
    finally:
        app.state.store.close()
