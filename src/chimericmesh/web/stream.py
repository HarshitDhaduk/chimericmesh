"""The per-service SSE route (the only dashboard file that imports the web framework).

``GET /api/services/{service_id}/stream`` is a read route like ``GET /api/events``: no token, no CORS
headers. Parameters are validated by the handler in a fixed order (service, after, min_severity, tail,
slot), so an unknown service is a 404 whatever else is wrong with the request.
"""
from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .feed import EventFeed, StreamConfig, StreamGate, stream_frames
from .sse import parse_after, parse_last_event_id

# The services module's id rule, repeated here because dashboard code may not import it.
_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
BUILTIN = "unassigned"


def _error(status: int, detail: str, field: str | None = None, headers: dict[str, str] | None = None):
    body: dict[str, str] = {"detail": detail}
    if field is not None:
        body["field"] = field
    return JSONResponse(body, status_code=status, headers=headers)


def build_stream_router(feed: EventFeed, *, service_exists: Callable[[str], bool],
                        gate: StreamGate | None = None, config: StreamConfig | None = None,
                        clock: Callable[[], float] = time.monotonic) -> APIRouter:
    gate = gate if gate is not None else StreamGate()
    config = config if config is not None else StreamConfig()
    router = APIRouter()

    @router.get("/api/services/{service_id}/stream", include_in_schema=False)
    async def stream(service_id: str, request: Request, after: str | None = None,
                     min_severity: int = 7, tail: int = 0):
        if not (service_id == BUILTIN or _SLUG.fullmatch(service_id)) or not service_exists(service_id):
            return _error(404, "service not found")
        try:
            after_id = parse_after(after)
        except ValueError:
            return _error(422, "after must be a non-negative integer", "after")
        if not 0 <= min_severity <= 7:
            return _error(422, "min_severity must be 0 to 7", "min_severity")
        if not 0 <= tail <= 100:
            return _error(422, "tail must be 0 to 100", "tail")
        resume = parse_last_event_id(request.headers.get("last-event-id"))
        if resume is not None:
            after_id = resume  # the browser sets this header itself on a reconnect, so it wins
        lease = gate.acquire(clock())
        if lease is None:
            return _error(429, "too many live logs open (limit 20)", headers={"Retry-After": "5"})
        frames = stream_frames(feed, service_id, after=after_id, tail=tail, min_severity=min_severity,
                               config=config, clock=clock, sleep=asyncio.sleep,
                               service_exists=service_exists, lease=lease)
        return StreamingResponse(frames, media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return router
