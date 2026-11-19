"""Pure ASGI guards: the Host allow-list and the request body cap (R6.3, R7.2, R7.3).

Plain ASGI callables, not ``BaseHTTPMiddleware``, so a streamed response (the dashboard's event
stream) passes through chunk by chunk. Stdlib only.
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from .access import AccessControl

log = logging.getLogger("chimericmesh.services")

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

MAX_BODY_BYTES = 65536
API_PREFIX = "/api/"
BODY_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
WARN_EVERY = 60.0


async def _respond(send: Send, status: int, detail: str) -> None:
    body = json.dumps({"detail": detail}).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", ()):
        if key.lower() == name:
            return bytes(value).decode("latin-1")
    return None


def _is_api_http(scope: Scope) -> bool:
    return scope.get("type") == "http" and str(scope.get("path", "")).startswith(API_PREFIX)


class BodyLimit:
    """Answers 413 for a write body over ``max_bytes``, before any parsing or token check."""

    def __init__(self, app: ASGIApp, max_bytes: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not _is_api_http(scope) or scope.get("method") not in BODY_METHODS:
            await self.app(scope, receive, send)
            return
        declared = _header(scope, b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
            await _respond(send, 413, "body too large")
            return
        chunks: list[bytes] = []
        total = 0
        while True:                                    # bounded: at most max_bytes plus one chunk
            message = await receive()
            if message["type"] != "http.request":
                return                                 # the client went away
            chunk = message.get("body", b"")
            total += len(chunk)
            if total > self.max_bytes:
                await _respond(send, 413, "body too large")
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        replay = [{"type": "http.request", "body": b"".join(chunks), "more_body": False}]

        async def replay_receive() -> Message:
            if replay:
                return replay.pop()
            return await receive()                     # later messages (disconnect) come from the server

        await self.app(scope, replay_receive, send)


class HostGuard:
    """Answers 403 on ``/api/*`` when the Host header is not on the allow-list (DNS rebinding defence)."""

    def __init__(self, app: ASGIApp, access: AccessControl) -> None:
        self.app = app
        self.access = access
        self._last_warning = float("-inf")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.access.enforce and _is_api_http(scope) and not self.access.host_allowed(_header(scope, b"host")):
            self._warn()
            await _respond(send, 403, "host not allowed")
            return
        await self.app(scope, receive, send)

    def _warn(self) -> None:
        now = time.monotonic()
        if now - self._last_warning >= WARN_EVERY:
            self._last_warning = now
            log.warning("refused a request with a Host header that is not allowed")
