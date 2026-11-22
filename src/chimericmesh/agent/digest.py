"""The digest of one service over a time window (R1.3). Deterministic, no model output.

Window arithmetic uses the event ``ts`` (the message time) and the hub clock for the window end. Event ids are
assigned in ingest order, so for live traffic ``ts`` grows with the id and the window start is found by a
binary search over ids. For replayed logs with old timestamps the digest is empty or partial and says so
through ``scanned`` (open item O1).
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from chimericmesh import diagnostics
from chimericmesh.diagnostics import Incident
from chimericmesh.events import Event
from chimericmesh.store.ports import StateView

from .limits import MAX_MINUTES, cap_items, clamp, clean_text

SCAN_CAP = 20_000
PAGE = 100
FUTURE_RUN = 200           # this many consecutive events past the window end stop the scan (R1-M7)
TOP_SIGNATURES = 10
MAX_INCIDENTS = 20
SAMPLE_CHARS = 200
SLICES = 5


@dataclass(frozen=True, slots=True)
class Window:
    start: float
    end: float
    minutes: int

    @classmethod
    def ending(cls, end: float, minutes: int) -> "Window":
        minutes = clamp(minutes, 1, MAX_MINUTES)
        return cls(start=end - 60 * minutes, end=end, minutes=minutes)


def _first_not_before(state: StateView, service_id: str, start: float):
    async def probe(mid: int) -> bool:
        """True when the first event of the service after ``mid`` is not older than the window start."""
        found = await state.events_after(mid, 1, service_id=service_id)
        return not found or found[0].ts >= start
    return probe


async def collect_window(state: StateView, service_id: str, window: Window, *,
                         scan_cap: int = SCAN_CAP) -> tuple[list[Event], bool]:
    """Every event of the service read from the window start on (oldest first) and a ``truncated`` flag.

    The list can hold events outside the window (a skipped future-dated event, an older one); the digest
    counts only the ones inside it. ``scanned`` for the digest is ``len(events)``.
    """
    newest = await _newest_id(state)
    if newest <= 0:
        return [], False
    probe = _first_not_before(state, service_id, window.start)
    low, high = 0, newest              # smallest id after which every event is inside the window
    while low < high:
        mid = (low + high) // 2
        if await probe(mid):
            high = mid
        else:
            low = mid + 1
    events: list[Event] = []
    cursor, future_run = low, 0
    while len(events) < scan_cap:
        page = await state.events_after(cursor, min(PAGE, scan_cap - len(events)), service_id=service_id)
        if not page:
            return events, False
        for ev in page:
            events.append(ev)
            cursor = ev.id
            future_run = future_run + 1 if ev.ts > window.end else 0
            if future_run >= FUTURE_RUN:
                return events, True
    more = await state.events_after(cursor, 1, service_id=service_id)
    return events, bool(more)


async def _newest_id(state: StateView) -> int:
    latest = state.recent(1)
    return max((ev.id for ev in latest), default=0)


def _event_key(ev: Event) -> tuple:
    return (ev.ts, ev.id, ev.message, ev.severity, ev.host, ev.app, ev.source)


def build_digest(service_id: str, window: Window, events: Sequence[Event], incidents: Sequence[Incident],
                 *, scanned: int, truncated: bool) -> dict:
    """Pure and total. Events and incidents outside the window are ignored; the order of the input does not
    change the result. ``incidents`` are the ones the caller chose for this service."""
    minutes = max(window.minutes, 1)
    inside = sorted((ev for ev in events if window.start <= ev.ts <= window.end), key=_event_key)

    severity_counts = {str(n): 0 for n in range(8)}
    slice_len = minutes * 12
    slice_counts = [0] * SLICES
    groups: dict[str, dict] = {}
    for ev in inside:
        severity_counts[str(ev.severity)] += 1
        slice_counts[min(SLICES - 1, int((ev.ts - window.start) / slice_len))] += 1
        sig = diagnostics.signature(ev.message)
        group = groups.get(sig)
        if group is None:
            groups[sig] = {"signature": sig, "count": 1, "first_ts": ev.ts, "last_ts": ev.ts,
                           "sample": ev.message, "most_severe": ev.severity}
        else:                                   # ``inside`` is sorted by ts, so the first event stays earliest
            group["count"] += 1
            group["last_ts"] = max(group["last_ts"], ev.ts)
            group["most_severe"] = min(group["most_severe"], ev.severity)
    ranked = sorted(groups.values(), key=lambda g: (-g["count"], g["first_ts"], g["signature"]))
    top = [{**g, "signature": clean_text(g["signature"]), "sample": clean_text(g["sample"], SAMPLE_CHARS)}
           for g in ranked[:TOP_SIGNATURES]]

    opened = sorted((inc for inc in incidents if window.start <= inc.first_seen <= window.end),
                    key=lambda inc: (-inc.first_seen, inc.id))[:MAX_INCIDENTS]
    opened_view = [{"id": clean_text(inc.id), "category": clean_text(inc.category), "severity": inc.severity,
                    "count": inc.count, "opening": inc.opening, "first_seen": inc.first_seen} for inc in opened]

    step = (window.end - window.start) / SLICES if window.end > window.start else 0.0
    slices = [{"start": window.start + i * step, "end": window.end if i == SLICES - 1 else window.start + (i + 1) * step,
               "events": n, "per_minute": round(n / (minutes / SLICES), 2)} for i, n in enumerate(slice_counts)]
    envelope = {"service_id": service_id, "minutes": window.minutes,
                "window": {"start": window.start, "end": window.end}, "total_events": len(inside),
                "severity_counts": severity_counts, "top_signatures": top, "slices": slices,
                "scanned": scanned, "truncated": bool(truncated)}
    return cap_items(envelope, "incidents_opened", opened_view)
