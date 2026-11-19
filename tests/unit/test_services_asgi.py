"""Pure ASGI guards: Host check and body size cap (R6.3, R7.2, R7.3)."""
from __future__ import annotations

import asyncio
import json
import logging

from chimericmesh.services.access import AccessControl
from chimericmesh.services.asgi import BodyLimit, HostGuard
from chimericmesh.services.token import TokenStore


def scope(path="/api/services", method="POST", headers=(), type_="http"):
    return {"type": type_, "method": method, "path": path, "headers": [(k.encode(), v.encode()) for k, v in headers]}


class Harness:
    def __init__(self, chunks=(), app=None):
        self.chunks = list(chunks)
        self.sent: list[dict] = []
        self.reads = 0
        self.app_called = False
        self.app_body = b""
        self._app = app

    async def receive(self):
        self.reads += 1
        if self.chunks:
            data = self.chunks.pop(0)
            return {"type": "http.request", "body": data, "more_body": bool(self.chunks)}
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(self, message):
        self.sent.append(message)

    async def app(self, scope_, receive, send):
        self.app_called = True
        if self._app is not None:
            return await self._app(scope_, receive, send)
        body = b""
        while True:
            msg = await receive()
            body += msg.get("body", b"")
            if not msg.get("more_body"):
                break
        self.app_body = body
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    @property
    def status(self):
        return next((m["status"] for m in self.sent if m["type"] == "http.response.start"), None)

    @property
    def body(self):
        return b"".join(m.get("body", b"") for m in self.sent if m["type"] == "http.response.body")


def run(coro):
    return asyncio.run(coro)


# ---- BodyLimit ---------------------------------------------------------------------------------

def test_content_length_over_cap_is_413_without_reading_or_calling_app():
    h = Harness([b"x" * 10])
    run(BodyLimit(h.app, max_bytes=65536)(scope(headers=[("content-length", "65537")]), h.receive, h.send))
    assert h.status == 413 and json.loads(h.body) == {"detail": "body too large"}
    assert not h.app_called and h.reads == 0


def test_chunked_body_over_cap_is_413_after_at_most_cap_plus_one_chunk():
    chunk = b"y" * 20000
    h = Harness([chunk] * 10)
    run(BodyLimit(h.app, max_bytes=65536)(scope(), h.receive, h.send))
    assert h.status == 413 and not h.app_called
    assert h.reads * 20000 <= 65536 + 20000


def test_body_of_exactly_cap_passes_intact():
    h = Harness([b"a" * 30000, b"b" * 35536])
    run(BodyLimit(h.app, max_bytes=65536)(scope(), h.receive, h.send))
    assert h.status == 200 and len(h.app_body) == 65536


def test_get_lifespan_websocket_and_other_paths_pass_untouched():
    for sc in (scope(method="GET", headers=[("content-length", "999999")]),
               scope(type_="lifespan"), scope(type_="websocket"),
               scope(path="/other", headers=[("content-length", "999999")])):
        h = Harness()
        run(BodyLimit(h.app, max_bytes=10)(sc, h.receive, h.send))
        assert h.app_called


def test_invalid_content_length_is_not_trusted_but_stream_is_still_capped():
    h = Harness([b"z" * 70000])
    run(BodyLimit(h.app, max_bytes=65536)(scope(headers=[("content-length", "abc")]), h.receive, h.send))
    assert h.status == 413


# ---- HostGuard ---------------------------------------------------------------------------------

def access(enforce=True, store=None):
    tokens = TokenStore.open(None, in_memory=True)
    return AccessControl(enforce=enforce, tokens=tokens,
                         allowed_hosts=frozenset({"127.0.0.1", "localhost", "::1"}), audit=None)


def call_guard(ac, path, host):
    h = Harness()
    headers = [] if host is None else [("host", host)]
    run(HostGuard(h.app, ac)(scope(path=path, method="GET", headers=headers), h.receive, h.send))
    return h


def test_refuses_bad_host_on_api_before_calling_app():
    h = call_guard(access(), "/api/stats", "evil.example")
    assert h.status == 403 and json.loads(h.body) == {"detail": "host not allowed"}
    assert not h.app_called


def test_allows_loopback_hosts_and_non_api_paths_and_non_http():
    for host in ("127.0.0.1:8000", "localhost", "[::1]"):
        assert call_guard(access(), "/api/stats", host).app_called
    assert call_guard(access(), "/", "evil.example").app_called
    h = Harness()
    run(HostGuard(h.app, access())(scope(type_="lifespan"), h.receive, h.send))
    assert h.app_called


def test_missing_host_is_403_on_api_and_disabled_passes_everything():
    assert call_guard(access(), "/api/stats", None).status == 403
    assert call_guard(AccessControl.disabled(), "/api/stats", "evil.example").app_called


def test_streamed_chunks_reach_send_before_the_app_returns():
    release = asyncio.Event()

    async def streaming(sc, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"one", "more_body": True})
        await release.wait()
        await send({"type": "http.response.body", "body": b"two", "more_body": False})

    async def scenario():
        h = Harness(app=streaming)
        for guard in (HostGuard(h.app, access()), BodyLimit(h.app)):
            h.sent.clear()
            release.clear()
            task = asyncio.ensure_future(guard(scope(path="/api/events", method="GET",
                                                     headers=[("host", "localhost")]), h.receive, h.send))
            for _ in range(50):
                await asyncio.sleep(0)
                if h.body == b"one":
                    break
            assert h.body == b"one" and not task.done()
            release.set()
            await task
            assert h.body == b"onetwo"
    run(scenario())


def test_refused_host_warns_at_most_once_a_minute_and_writes_no_audit_row(caplog):
    ac = access()
    guard_app = Harness()
    guard = HostGuard(guard_app.app, ac)
    with caplog.at_level(logging.WARNING, logger="chimericmesh.services"):
        for _ in range(5):
            h = Harness()
            run(guard(scope(path="/api/stats", method="GET", headers=[("host", "evil.example")]),
                      h.receive, h.send))
    assert sum("host" in r.getMessage().lower() for r in caplog.records) == 1
