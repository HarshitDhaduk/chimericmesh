"""Helpers of the agent-loop tests: a ``FakeState`` that implements ``StateView``, builders for events,
incidents and proposals, and ``make_agent(mode)`` (completed in task 7.1)."""
from __future__ import annotations

from collections.abc import Callable, Iterable

from chimericmesh.diagnostics import Incident
from chimericmesh.events import Event
from chimericmesh.store.models import Proposal

T0 = 1_700_000_000.0


def make_event(id: int, service_id: str | None = "svc", *, ts: float | None = None, severity: int = 3,
               message: str | None = None, host: str = "web01", app: str = "app", source: str = "syslog") -> Event:
    return Event(source=source, host=host, app=app, severity=severity,
                 message=message if message is not None else f"event {id}",
                 ts=T0 + id if ts is None else ts, id=id, service_id=service_id)


def make_incident(id: str = "i1", *, service_id: str | None = "svc", severity: int = 3, count: int = 3,
                  opening: int = 1, resolved: bool = False, first_seen: float = T0, last_seen: float | None = None,
                  category: str = "disk", signature: str = "disk full", host: str = "web01",
                  sample: str = "disk full on /var") -> Incident:
    return Incident(id=id, host=host, category=category, signature=signature, severity=severity, count=count,
                    first_seen=first_seen, last_seen=first_seen if last_seen is None else last_seen,
                    sample=sample, resolved=resolved, service_id=service_id, opening=opening)


def make_proposal(id: str = "p1", incident_id: str = "i1", *, status: str = "pending", service_id: str | None = "svc",
                  action: str = "fix it", decision_origin: str | None = None, incident_opening: int | None = 1,
                  approved_by_rule: str | None = None, decided_by: str = "", files: list[str] | None = None,
                  **extra) -> Proposal:
    return Proposal(id=id, incident_id=incident_id, action=action, status=status, service_id=service_id,
                    decision_origin=decision_origin, incident_opening=incident_opening,
                    approved_by_rule=approved_by_rule, decided_by=decided_by, files=files, **extra)


class FakeState:
    """A list of events (ids ascending), incidents and proposals behind the ``StateView`` methods."""

    def __init__(self, events: Iterable[Event] = (), incidents: Iterable[Incident] = (),
                 proposals: Iterable[Proposal] = (), *, now: float = T0 + 10_000) -> None:
        self.events: list[Event] = sorted(events, key=lambda e: e.id)
        self._incidents = list(incidents)
        self._proposals = list(proposals)
        self._now = now
        self.after_calls: list[tuple[int, int, str | None]] = []

    def now(self) -> float:
        return self._now

    def get_incident(self, incident_id: str) -> Incident | None:
        return next((i for i in self._incidents if i.id == incident_id), None)

    def incidents(self, *, status=None, service_id=None, limit=None) -> list[Incident]:
        out = [i for i in self._incidents
               if (status is None or (status == "resolved") == i.resolved)
               and (service_id is None or i.service_id == service_id)]
        return out[:limit] if limit is not None else out

    def get_proposal(self, pid: str) -> Proposal | None:
        return next((p for p in self._proposals if p.id == pid), None)

    def list_proposals(self, status=None, *, service_id=None, limit=None) -> list[Proposal]:
        out = [p for p in self._proposals
               if (status is None or p.status == status) and (service_id is None or p.service_id == service_id)]
        return out[:limit] if limit is not None else out

    def recent(self, limit: int = 50, source: str | None = None) -> list[Event]:
        pool = [e for e in self.events if source is None or e.source == source]
        return pool[-limit:][::-1] if limit > 0 else []

    async def events_after(self, after_id: int, limit: int = 100, *, source: str | None = None,
                           service_id: str | None = None) -> list[Event]:
        self.after_calls.append((after_id, limit, service_id))
        out: list[Event] = []
        for ev in self.events:
            if ev.id <= after_id:
                continue
            if source is not None and ev.source != source:
                continue
            if service_id is not None and ev.service_id != service_id:
                continue
            out.append(ev)
            if len(out) >= limit:
                break
        return out


class RangeState(FakeState):
    """``n`` events of one service with ids 1..n and ``ts = base + id * step``, built on demand (no list)."""

    def __init__(self, n: int, *, service_id: str = "svc", base: float = T0, step: float = 1.0,
                 ts_of: Callable[[int], float] | None = None, now: float | None = None) -> None:
        super().__init__(now=base + n * step if now is None else now)
        self.n, self.service_id = n, service_id
        self._ts = ts_of or (lambda i: base + i * step)

    def recent(self, limit: int = 50, source: str | None = None) -> list[Event]:
        return [self._make(i) for i in range(self.n, max(self.n - limit, 0), -1)]

    def _make(self, i: int) -> Event:
        return make_event(i, self.service_id, ts=self._ts(i), message=f"m{i % 7}")

    async def events_after(self, after_id: int, limit: int = 100, *, source: str | None = None,
                           service_id: str | None = None) -> list[Event]:
        self.after_calls.append((after_id, limit, service_id))
        if service_id not in (None, self.service_id):
            return []
        start = max(after_id, 0) + 1
        return [self._make(i) for i in range(start, min(start + limit, self.n + 1))]


def make_agent(mode: str):
    """``mode`` is ``bare`` or ``store``. Completed in task 7.1."""
    raise NotImplementedError("make_agent is completed in agent-loop task 7.1")


__all__ = ["FakeState", "RangeState", "T0", "make_agent", "make_event", "make_incident", "make_proposal"]
