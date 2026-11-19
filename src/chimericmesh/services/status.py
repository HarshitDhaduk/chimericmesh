"""Service status: in-memory counters, ``state``, ``error_rate`` and the status snapshot (R5)."""
from __future__ import annotations

import bisect
import logging
from collections import Counter, deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from .models import LogSource, Service, SourceRuntime, StatusProvider

log = logging.getLogger("chimericmesh.services")

STATE_WINDOW = 300.0
ERROR_WINDOW = 300          # seconds, kept as 1-second buckets
ERROR_SEVERITY_MAX = 3
UNASSIGNED = "unassigned"
LAST_ERROR_MAX = 200
PROVIDER_LOG_INTERVAL = 60.0

State = Literal["active", "idle", "never_seen"]


def compute_state(last_seen: float | None, now: float) -> State:
    if last_seen is None:
        return "never_seen"
    return "active" if now - last_seen <= STATE_WINDOW else "idle"


def error_rate(errors_in_window: int) -> float:
    return round(errors_in_window / (ERROR_WINDOW / 60), 1)


@dataclass(frozen=True, slots=True)
class StatsSnapshot:
    events_total: int
    last_seen: float | None
    errors_in_window: int


class _Entry:
    __slots__ = ("total", "last_seen", "seconds", "counts")

    def __init__(self) -> None:
        self.total = 0
        self.last_seen: float | None = None
        self.seconds: list[int] = []      # sorted distinct seconds that had errors
        self.counts: list[int] = []


class StatsTracker:
    """Per-service counters. O(1) amortized per event, no I/O, no task, no thread."""

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}

    def _entry(self, service_id: str | None) -> _Entry:
        key = UNASSIGNED if service_id is None else service_id
        entry = self._entries.get(key)
        if entry is None:
            entry = self._entries[key] = _Entry()
        return entry

    def observe(self, service_id: str | None, severity: int, now: float) -> None:
        e = self._entry(service_id)
        e.total += 1
        if e.last_seen is None or now > e.last_seen:
            e.last_seen = now
        if severity > ERROR_SEVERITY_MAX:
            return
        sec = int(now)
        if e.seconds and e.seconds[-1] == sec:
            e.counts[-1] += 1
            return
        if not e.seconds or e.seconds[-1] < sec:
            e.seconds.append(sec)
            e.counts.append(1)
        else:
            i = bisect.bisect_left(e.seconds, sec)
            if i < len(e.seconds) and e.seconds[i] == sec:
                e.counts[i] += 1
            else:
                e.seconds.insert(i, sec)
                e.counts.insert(i, 1)
        if len(e.seconds) > ERROR_WINDOW:
            del e.seconds[0]
            del e.counts[0]

    def snapshot(self, service_id: str | None, now: float) -> StatsSnapshot:
        e = self._entries.get(UNASSIGNED if service_id is None else service_id)
        if e is None:
            return StatsSnapshot(0, None, 0)
        errors = sum(c for s, c in zip(e.seconds, e.counts) if now - s < ERROR_WINDOW)
        return StatsSnapshot(e.total, e.last_seen, errors)

    def seed(self, rows: Iterable[tuple[str | None, int, float | None]]) -> None:
        for service_id, total, last_seen in rows:
            e = self._entry(service_id)
            e.total += int(total)
            if last_seen is not None and (e.last_seen is None or last_seen > e.last_seen):
                e.last_seen = float(last_seen)

    def drop(self, service_id: str) -> None:
        self._entries.pop(service_id, None)


@dataclass(frozen=True, slots=True)
class LogSourceStatus:
    source: LogSource
    runtime: SourceRuntime | None      # None: no provider or an unusable answer ("unknown", no counters)

    def to_dict(self) -> dict:
        out = self.source.to_dict()
        rt = self.runtime
        if rt is None:
            out["status"] = "unknown"
            return out
        out.update(status=rt.status, lines_read=rt.lines_read, events_emitted=rt.events_emitted,
                   lines_dropped=rt.lines_dropped, bytes_read=rt.bytes_read, unparsed=rt.unparsed,
                   last_error=rt.last_error[:LAST_ERROR_MAX])
        return out


@dataclass(frozen=True, slots=True)
class ServiceStatus:
    id: str
    name: str
    hosts: tuple[str, ...]
    app_names: tuple[str, ...]
    protocols: tuple[str, ...]
    code_folder: str | None
    created_at: float | None
    updated_at: float | None
    builtin: bool
    last_seen: float | None
    state: State
    error_rate: float
    open_incidents: int
    events_total: int
    log_sources: tuple[LogSourceStatus, ...] = field(default=())

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "hosts": list(self.hosts), "app_names": list(self.app_names),
            "protocols": list(self.protocols), "code_folder": self.code_folder,
            "created_at": self.created_at, "updated_at": self.updated_at, "builtin": self.builtin,
            "last_seen": self.last_seen, "state": self.state, "error_rate": self.error_rate,
            "open_incidents": self.open_incidents, "events_total": self.events_total,
            "log_sources": [s.to_dict() for s in self.log_sources],
        }


class ProviderGuard:
    """Calls the status provider for one source; a bad answer gives ``None`` and one log line a minute."""

    def __init__(self, now: Callable[[], float]) -> None:
        self._now = now
        self._last_log = float("-inf")

    def call(self, provider: StatusProvider | None, source_id: str) -> SourceRuntime | None:
        if provider is None:
            return None
        try:
            answer = provider(source_id)
        except Exception as exc:
            self._warn("status provider raised for %s: %s", source_id, exc)
            return None
        if answer is None:
            return None
        if not isinstance(answer, SourceRuntime):
            self._warn("status provider returned %s for %s", type(answer).__name__, source_id)
            return None
        return answer

    def _warn(self, msg: str, *args: object) -> None:
        t = self._now()
        if t - self._last_log >= PROVIDER_LOG_INTERVAL:
            self._last_log = t
            log.warning(msg, *args)


def build_statuses(services: Sequence[Service], stats: StatsTracker, open_by_service: Counter[str],
                   provider: StatusProvider | None, guard: ProviderGuard, now: float) -> list[ServiceStatus]:
    out = [_one(svc, stats, open_by_service, provider, guard, now) for svc in services]
    out.append(_unassigned(stats, open_by_service, now))
    return out


def build_status(service_id: str, services: dict[str, Service], stats: StatsTracker,
                 open_by_service: Counter[str], provider: StatusProvider | None, guard: ProviderGuard,
                 now: float) -> ServiceStatus | None:
    if service_id == UNASSIGNED:
        return _unassigned(stats, open_by_service, now)
    svc = services.get(service_id)
    return None if svc is None else _one(svc, stats, open_by_service, provider, guard, now)


def _common(service_id: str, stats: StatsTracker, open_by_service: Counter[str], now: float) -> dict:
    snap = stats.snapshot(service_id, now)
    return {
        "last_seen": snap.last_seen, "state": compute_state(snap.last_seen, now),
        "error_rate": error_rate(snap.errors_in_window), "open_incidents": open_by_service.get(service_id, 0),
        "events_total": snap.events_total,
    }


def _one(svc: Service, stats: StatsTracker, open_by_service: Counter[str], provider: StatusProvider | None,
         guard: ProviderGuard, now: float) -> ServiceStatus:
    sources = tuple(LogSourceStatus(s, guard.call(provider, s.id)) for s in svc.log_sources)
    return ServiceStatus(
        id=svc.id, name=svc.name, hosts=svc.hosts, app_names=svc.app_names, protocols=svc.protocols,
        code_folder=svc.code_folder, created_at=svc.created_at, updated_at=svc.updated_at, builtin=False,
        log_sources=sources, **_common(svc.id, stats, open_by_service, now))


def _unassigned(stats: StatsTracker, open_by_service: Counter[str], now: float) -> ServiceStatus:
    return ServiceStatus(
        id=UNASSIGNED, name="Unassigned", hosts=(), app_names=(), protocols=(), code_folder=None,
        created_at=None, updated_at=None, builtin=True, log_sources=(),
        **_common(UNASSIGNED, stats, open_by_service, now))
