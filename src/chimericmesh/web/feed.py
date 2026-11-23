"""Stream configuration, slot gate and frame generator (no web framework).

Stdlib and ``core`` only. Clocks and sleep are injected, so the generator runs against a fake feed and
a manual clock in tests, and against the hub (``StateView.events_after`` and ``recent``) in the app.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from chimericmesh.events import Event

from .sse import event_payload, frame_comment, frame_event, frame_named

log = logging.getLogger(__name__)

MAX_STREAMS = 20
RECENT_WINDOW = 500


@dataclass(frozen=True, slots=True)
class StreamConfig:
    poll_interval: float = 0.25
    heartbeat: float = 15.0
    queue_max: int = 1000  # R5.4
    page: int = 500  # events_after limit per call
    max_pages_per_cycle: int = 20
    max_seconds: float = 3600.0  # the browser reconnects with Last-Event-ID
    max_feed_failures: int = 5
    # lease_grace and stall_after live in StreamGate only (L5)


class Lease:
    """One stream slot. ``release`` is idempotent; a reclaimed lease is already released."""

    __slots__ = ("_gate", "created", "last_touch", "started", "released")

    def __init__(self, gate: "StreamGate", now: float) -> None:
        self._gate = gate
        self.created = now
        self.last_touch = now
        self.started = False
        self.released = False

    def start(self, now: float) -> None:
        if not self.released:
            self.started = True
            self.last_touch = now

    def touch(self, now: float) -> None:
        if not self.released:
            self.last_touch = now

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        self._gate._leases.discard(self)


class StreamGate:
    """At most ``max_streams`` concurrent streams. Used from the event loop thread only, so no lock."""

    def __init__(self, max_streams: int = MAX_STREAMS, *, lease_grace: float = 10.0,
                 stall_after: float = 10.0) -> None:
        self.max_streams = max_streams
        self.lease_grace = lease_grace
        self.stall_after = stall_after
        self._leases: set[Lease] = set()

    @property
    def active(self) -> int:
        return len(self._leases)

    def _reclaim(self, now: float) -> None:
        for lease in list(self._leases):
            if lease.started:
                stale = now - lease.last_touch > self.stall_after
            else:
                stale = now - lease.created > self.lease_grace
            if stale:
                lease.release()

    def acquire(self, now: float) -> Lease | None:
        self._reclaim(now)
        if len(self._leases) >= self.max_streams:
            return None
        lease = Lease(self, now)
        self._leases.add(lease)
        return lease


class EventFeed(Protocol):
    """What the hub offers (store ``StateView``): events after a cursor, and the newest events."""

    async def events_after(self, after_id: int, limit: int = 100, *, source: str | None = None,
                           service_id: str | None = None) -> list[Event]: ...

    def recent(self, limit: int = 50, source: str | None = None) -> list[Event]: ...


def _mine(event: Event, service_id: str) -> bool:
    return event.service_id is None if service_id == "unassigned" else event.service_id == service_id


def _closed(reason: str) -> str:
    return frame_named("closed", json.dumps({"reason": reason}))


async def stream_frames(feed: EventFeed, service_id: str, *, after: int | None, tail: int = 0,
                        min_severity: int, config: StreamConfig, clock: Callable[[], float],
                        sleep: Callable[[float], Awaitable[None]],
                        service_exists: Callable[[str], bool],
                        lease: Lease | None = None) -> AsyncIterator[str]:
    """Yield SSE frames for one service until the age limit, a deleted service or a dead feed.

    ``min_severity`` keeps events with ``severity <= min_severity`` (0 is the most severe). The slot
    lease is started at the first step and released in ``finally``, so a cancel frees it at once.
    """
    started = clock()
    try:
        if lease is not None:
            lease.start(started)
        # Cursor (and backlog) from one snapshot taken before the first frame is sent, so an event that
        # arrives while the client is still connecting is delivered (no gap) and never twice.
        backlog: list[Event] = []
        if after is not None:
            cursor = after
        else:
            try:
                window = feed.recent(RECENT_WINDOW if tail > 0 else 1)
            except Exception:
                log.warning("dashboard stream: feed.recent failed for %s", service_id)
                yield frame_comment("open")
                yield _closed("feed_error")
                return
            mine = [e for e in window if _mine(e, service_id)]
            cursor = mine[0].id if mine else (window[0].id if window else 0)
            if tail > 0:
                backlog = [e for e in mine if e.severity <= min_severity][:tail]
                backlog.reverse()
        yield frame_comment("open")
        for e in backlog:
            yield frame_event(e.id, event_payload(e))
        last_frame = clock()
        failures = 0
        last_warn: float | None = None
        while True:
            now = clock()
            if lease is not None:
                lease.touch(now)
            if now - started >= config.max_seconds:
                yield _closed("max_age")
                return
            if not service_exists(service_id):
                yield _closed("service_deleted")
                return
            queue: deque[Event] = deque(maxlen=config.queue_max)
            dropped = 0
            failed = False
            for _ in range(config.max_pages_per_cycle):
                try:
                    page = await feed.events_after(cursor, config.page, service_id=service_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    failed = True
                    break
                failures = 0
                for e in page:
                    cursor = max(cursor, e.id)
                    if e.severity <= min_severity:
                        if len(queue) == queue.maxlen:
                            dropped += 1
                        queue.append(e)
                if len(page) < config.page:
                    break
                await sleep(0)
            yielded = False
            if dropped:
                yield frame_named("dropped", json.dumps({"count": dropped}))
                yielded = True
            for e in queue:
                yield frame_event(e.id, event_payload(e))
                yielded = True
                if lease is not None:
                    lease.touch(clock())
            if yielded:
                last_frame = clock()
            if failed:
                failures += 1
                t = clock()
                if last_warn is None or t - last_warn >= 60.0:
                    last_warn = t
                    log.warning("dashboard stream: events_after failed (%d in a row)", failures)
                if failures >= config.max_feed_failures:
                    yield _closed("feed_error")
                    return
                await sleep(min(5.0, 0.5 * failures))
                continue
            if not yielded and clock() - last_frame >= config.heartbeat:
                yield frame_comment("hb")
                last_frame = clock()
            await sleep(config.poll_interval)
    finally:
        if lease is not None:
            lease.release()
