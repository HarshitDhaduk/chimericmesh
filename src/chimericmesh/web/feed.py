"""Stream configuration, slot gate and frame generator (no web framework).

Stdlib and ``core`` only. Clocks and sleep are injected, so the generator runs against a fake feed and
a manual clock in tests, and against the hub (``StateView.events_after`` and ``recent``) in the app.
"""
from __future__ import annotations

from dataclasses import dataclass

MAX_STREAMS = 20


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
