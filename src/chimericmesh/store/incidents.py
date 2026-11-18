"""Incident rows: snapshot, upsert, load. The lifecycle (persist, reopen) is added below."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from chimericmesh.diagnostics import Incident, IncidentTracker, classify

from .ports import Announce, AuditSink
from .tx import Tx

if TYPE_CHECKING:
    from .store import Store

log = logging.getLogger("chimericmesh.store")

MAX_SAMPLE = 4096
MAX_HOST = 255

_COLUMNS = (
    "SELECT id, host, service_id, category, signature, severity, count, first_seen, last_seen, sample, "
    "sources, resolved, resolved_at, opening, opened, announced_count FROM incidents"
)
_UPSERT = (
    "INSERT INTO incidents (id, host, service_id, category, signature, severity, count, first_seen, "
    "last_seen, sample, sources, resolved, resolved_at, opening, opened, announced_count) "
    "VALUES (:id, :host, :service_id, :category, :signature, :severity, :count, :first_seen, :last_seen, "
    ":sample, :sources, :resolved, :resolved_at, :opening, :opened, :announced_count) "
    "ON CONFLICT(id) DO UPDATE SET host = excluded.host, service_id = excluded.service_id, "
    "category = excluded.category, signature = excluded.signature, severity = excluded.severity, "
    "count = excluded.count, first_seen = excluded.first_seen, last_seen = excluded.last_seen, "
    "sample = excluded.sample, sources = excluded.sources, resolved = excluded.resolved, "
    "resolved_at = excluded.resolved_at, opening = excluded.opening, opened = excluded.opened, "
    "announced_count = excluded.announced_count"
)
_LOAD_NEWEST = _COLUMNS + " ORDER BY last_seen DESC, id LIMIT :limit"
_GET = _COLUMNS + " WHERE id = ?"
_IDS_BEYOND = "SELECT id FROM incidents ORDER BY last_seen DESC, id LIMIT -1 OFFSET ?"


def _text(value: str, limit: int) -> str:
    """Cut to ``limit`` and replace lone surrogates, which sqlite3 cannot bind."""
    return value.encode("utf-8", "replace").decode("utf-8")[:limit]


@dataclass(frozen=True, slots=True)
class IncidentRow:
    """Immutable snapshot of an incident, built on the loop thread before a write is submitted."""

    id: str
    host: str
    service_id: str | None
    category: str
    signature: str
    severity: int
    count: int
    first_seen: float
    last_seen: float
    sample: str
    sources: tuple[str, ...]
    resolved: bool
    resolved_at: float | None
    opening: int
    opened: bool
    announced_count: int | None

    @classmethod
    def from_incident(cls, inc: Incident, announced_count: int | None) -> "IncidentRow":
        return cls(
            id=inc.id, host=_text(inc.host, MAX_HOST), service_id=inc.service_id, category=inc.category,
            signature=_text(inc.signature, 200), severity=inc.severity, count=inc.count,
            first_seen=inc.first_seen, last_seen=inc.last_seen, sample=_text(inc.sample, MAX_SAMPLE),
            sources=tuple(inc.sources), resolved=bool(inc.resolved), resolved_at=inc.resolved_at,
            opening=inc.opening, opened=bool(inc.opened), announced_count=announced_count,
        )

    def params(self) -> dict[str, object]:
        return {
            "id": self.id, "host": self.host, "service_id": self.service_id, "category": self.category,
            "signature": self.signature, "severity": self.severity, "count": self.count,
            "first_seen": self.first_seen, "last_seen": self.last_seen, "sample": self.sample,
            "sources": json.dumps([_text(s, 64) for s in self.sources]), "resolved": int(self.resolved),
            "resolved_at": self.resolved_at, "opening": self.opening, "opened": int(self.opened),
            "announced_count": self.announced_count,
        }


def _decode(row: object) -> IncidentRow:
    r = dict(row)  # type: ignore[call-overload]
    try:
        sources = tuple(str(s) for s in json.loads(r["sources"]))
    except (TypeError, ValueError):
        sources = ()
    return IncidentRow(
        id=r["id"], host=r["host"], service_id=r["service_id"], category=r["category"],
        signature=r["signature"], severity=r["severity"], count=r["count"], first_seen=r["first_seen"],
        last_seen=r["last_seen"], sample=r["sample"], sources=sources, resolved=bool(r["resolved"]),
        resolved_at=r["resolved_at"], opening=r["opening"], opened=bool(r["opened"]),
        announced_count=r["announced_count"],
    )


def row_to_incident(row: IncidentRow) -> Incident:
    """Rebuild an ``Incident``. Hypothesis and remediation are recomputed from the stored sample."""
    _category, hypothesis, steps = classify(row.sample)
    return Incident(
        id=row.id, host=row.host, category=row.category, signature=row.signature, severity=row.severity,
        count=row.count, first_seen=row.first_seen, last_seen=row.last_seen, sources=list(row.sources),
        sample=row.sample, hypothesis=hypothesis, remediation=list(steps), resolved=row.resolved,
        service_id=row.service_id, opening=row.opening, resolved_at=row.resolved_at, opened=row.opened,
    )


def upsert_incident(tx: Tx, row: IncidentRow) -> None:
    tx.execute(_UPSERT, row.params())  # type: ignore[arg-type]


def load_incidents(tx: Tx, limit: int) -> list[IncidentRow]:
    """The ``limit`` most recently seen incidents, newest first."""
    return [_decode(r) for r in tx.all(_LOAD_NEWEST, {"limit": max(0, int(limit))})]  # type: ignore[arg-type]


def stored_ids_beyond(tx: Tx, offset: int) -> list[str]:
    """Ids of incidents older than the newest ``offset`` (stored but not loaded at start)."""
    return [r[0] for r in tx.all(_IDS_BEYOND, (max(0, int(offset)),))]


def get_incident_row(tx: Tx, incident_id: str) -> IncidentRow | None:
    row = tx.one(_GET, (incident_id,))
    return None if row is None else _decode(row)


# ---- lifecycle -----------------------------------------------------------------------------

class IncidentLifecycle:
    """Persists incidents behind the hub's in-memory state and owns the reopen rule.

    Works with ``store=None`` (a bare hub): persistence is then a no-op. The caller builds every
    write from an immutable ``IncidentRow`` on the loop thread, so the writer thread never reads a
    live ``Incident``.
    """

    def __init__(self, tracker: IncidentTracker, *, store: "Store | None", announce: Announce,
                 now: Callable[[], float], reopen_after: float,
                 audit_getter: Callable[[], AuditSink | None] = lambda: None,
                 announced_getter: Callable[[str], int | None] = lambda _id: None,
                 persist_slots: int = 256, flush_interval: float = 30.0) -> None:
        self.tracker = tracker
        self.store = store
        self._announce = announce
        self._now = now
        self.reopen_after = reopen_after
        self._audit_getter = audit_getter
        self._announced_getter = announced_getter
        self._slots = asyncio.Semaphore(persist_slots)
        self.flush_interval = flush_interval
        self._dirty: dict[str, Incident] = {}
        self._gen: dict[str, int] = {}
        self._written: dict[str, float] = {}
        self._last_warn = -1e9

    # -- bookkeeping --

    def mark_dirty(self, inc: Incident) -> None:
        if self.store is None:
            return  # nothing is written, so nothing is kept alive
        self._dirty[inc.id] = inc
        self._gen[inc.id] = self._gen.get(inc.id, 0) + 1

    def _snapshot(self, inc: Incident) -> IncidentRow:
        return IncidentRow.from_incident(inc, self._announced_getter(inc.id))

    def _warn(self, message: str, *args: object) -> None:
        """One warning per minute, so a stalled store cannot flood the log."""
        t = time.monotonic()
        if t - self._last_warn >= 60:
            self._last_warn = t
            log.warning(message, *args)

    def _store_ok(self) -> bool:
        return self.store is not None and self.store.health().store_ok

    # -- persistence --

    async def persist(self, inc: Incident) -> None:
        """Write ``inc`` now. Never raises. Under backpressure or a failing store the incident stays dirty."""
        if self.store is None:
            return
        if self._slots.locked() or not self._store_ok():
            self.mark_dirty(inc)
            self._warn("store stalled: incident %s stays dirty", inc.id)
            return
        gen = self._gen.get(inc.id, 0)
        row = self._snapshot(inc)
        async with self._slots:
            try:
                await self.store.transact(lambda tx: upsert_incident(tx, row))
            except asyncio.CancelledError:
                self.mark_dirty(inc)
                raise
            except Exception:
                log.exception("incident %s not persisted; it will be retried", inc.id)
                self.mark_dirty(inc)
                return
        self._written[inc.id] = self._now()
        if self._gen.get(inc.id, 0) == gen:
            self._dirty.pop(inc.id, None)

    async def maybe_flush(self, inc: Incident) -> None:
        """Mark dirty and write when the last write is at least ``flush_interval`` old."""
        self.mark_dirty(inc)
        if self._is_due(inc.id):
            await self.persist(inc)

    def _is_due(self, incident_id: str) -> bool:
        return self._now() - self._written.get(incident_id, -1e18) >= self.flush_interval

    def flush_due(self) -> list[IncidentRow]:
        """Snapshots of dirty incidents whose last write is at least ``flush_interval`` old."""
        return [self._snapshot(i) for i in self._dirty.values() if self._is_due(i.id)]

    async def flush_dirty(self) -> None:
        for inc in [i for i in list(self._dirty.values()) if self._is_due(i.id)]:
            await self.persist(inc)

    # -- reopen --

    def reopen_due(self, inc: Incident) -> bool:
        """True for a resolved incident whose reopen window has passed (hub clock, not event time)."""
        return (inc.resolved and inc.resolved_at is not None
                and self._now() - inc.resolved_at >= self.reopen_after)

    async def reopen(self, inc: Incident) -> None:
        """Persist and announce a new opening. ``inc`` was already reset by ``tracker.reopen``.

        The incident row and the ``incident_reopened`` audit row share one transaction when the audit
        sink is this store's log. The announcement goes out after the commit, once, and never waits
        on a failing store.
        """
        row = IncidentRow.from_incident(inc, 1)  # marker 1: one announcement per opening
        sink = self._audit_getter()
        detail = f"incident={inc.id} opening={inc.opening} service={inc.service_id or 'unassigned'}"
        refs = {"service_id": inc.service_id}
        in_tx = (self.store is not None and sink is not None
                 and sink is getattr(self.store, "audit", None))
        gen = self._gen.get(inc.id, 0)
        committed = False
        if self.store is not None and not self._slots.locked() and self._store_ok():
            async with self._slots:
                try:
                    def write(tx: Tx) -> None:
                        upsert_incident(tx, row)
                        if in_tx:
                            sink.append_tx(tx, "hub", "incident_reopened", inc.id, detail, **refs)  # type: ignore[union-attr]
                    await self.store.transact(write)
                    committed = True
                except asyncio.CancelledError:
                    self.mark_dirty(inc)
                    raise
                except Exception:
                    log.exception("reopen of %s not persisted; it will be retried", inc.id)
        elif self.store is not None:
            self._warn("store stalled: reopen of %s stays dirty", inc.id)
        if self.store is not None:
            if committed:
                self._written[inc.id] = self._now()
                if self._gen.get(inc.id, 0) == gen:
                    self._dirty.pop(inc.id, None)
            else:
                self.mark_dirty(inc)
        if sink is not None and not in_tx:
            try:
                await sink.append("hub", "incident_reopened", inc.id, detail, **refs)
            except Exception:
                log.exception("audit row for reopen of %s lost", inc.id)
        try:
            await self._announce(f"REOPENED {inc.id}: {inc.category} on {inc.host}")
        except Exception:
            log.exception("announcement for reopen of %s failed", inc.id)

    async def flush_all(self, deadline: float | None = None) -> None:
        """Shutdown: write every dirty incident; stop submitting at ``deadline`` (``time.monotonic``)."""
        for inc in list(self._dirty.values()):
            if deadline is not None and time.monotonic() >= deadline:
                log.error("shutdown deadline passed with %d dirty incident(s) unwritten", len(self._dirty))
                return
            await self.persist(inc)
