"""Service events: a cursor over one service's events, oldest first (R1.2). Read-only; text is cleaned."""
from __future__ import annotations

from chimericmesh.events import Event
from chimericmesh.store.ports import StateView

from .limits import DEFAULT_EVENTS, MAX_EVENTS, cap_items, clamp, clean_text

SCAN_CAP = 1000          # events looked at per call, kept or not
PAGE = 100


def _view(ev: Event) -> dict:
    return {"id": ev.id, "ts": ev.ts, "source": clean_text(ev.source), "host": clean_text(ev.host),
            "app": clean_text(ev.app), "severity": ev.severity, "message": clean_text(ev.message)}


async def service_events(state: StateView, service_id: str, *, limit: int = DEFAULT_EVENTS, after_id: int = 0,
                         min_severity: int | None = None) -> dict:
    """Up to ``limit`` (1 to 100) events of ``service_id`` with an id above ``after_id``.

    ``min_severity`` keeps events with ``severity <= min_severity`` (a lower number is more severe).
    ``next_after_id`` is the id of the last event *scanned*, so a caller that filters still makes progress;
    it equals ``after_id`` when nothing was scanned, and the id of the last kept event when the 20 KB cap
    cut the list.
    """
    limit = clamp(limit, 1, MAX_EVENTS)
    after = max(after_id, 0)
    worst = None if min_severity is None else clamp(min_severity, 0, 7)
    kept: list[Event] = []
    cursor, scanned, done = after, 0, False
    while not done and len(kept) < limit and scanned < SCAN_CAP:
        page = await state.events_after(cursor, min(PAGE, SCAN_CAP - scanned), service_id=service_id)
        if not page:
            break
        for ev in page:
            cursor, scanned = ev.id, scanned + 1
            if ev.service_id == service_id and (worst is None or ev.severity <= worst):
                kept.append(ev)
            if len(kept) >= limit or scanned >= SCAN_CAP:
                done = True
                break
    items = [_view(ev) for ev in kept]
    out = cap_items({"service_id": service_id, "next_after_id": cursor, "truncated": False}, "events", items)
    if len(out["events"]) < len(items):
        out["next_after_id"] = out["events"][-1]["id"] if out["events"] else after
    return out
