"""The service registry: a cache in front of three SQLite tables.

Callback contract (R10, design "Threads"). In-transaction subscribers (``on_service_changed``,
``on_code_folder_changed``, ``on_service_deleted``) run on the ``store-writer`` thread inside the
change's transaction. They may use only the ``tx`` they receive and the immutable ``ServiceChange``:
they may not await, call ``Store.transact*``, ``read*`` or ``AuditLog.append``, touch hub state, take
a lock, start a thread or do network or file I/O. The store may run the transaction closure again
after a retryable error, so every such callback must be re-runnable and have no effect outside ``tx``.
``after_commit`` callbacks run on the loop thread after the commit, may be coroutine functions, may
touch hub state, and have a 5-second limit each; they must be idempotent.

The registry starts no task and no thread.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import sqlite3
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from chimericmesh.events import Event
from chimericmesh.store import Store, SubscriberError, Tx
from chimericmesh.store.ports import StateView
from chimericmesh.store.tx import format_id, run_callbacks

from .errors import (
    BuiltinService,
    Conflict,
    LogSourceNotFound,
    ServiceNotFound,
    ServicesNotMigrated,
    SubscriberFailed,
    ValidationFailed,
)
from .models import (
    CLEAR,
    LogSource,
    LogSourceFields,
    LogSourcePatch,
    Origin,
    Service,
    ServiceChange,
    ServiceFields,
    ServicePatch,
    StatusProvider,
)
from .paths import inspect_log_path, normalize_code_folder, normalize_log_path
from .status import ProviderGuard, ServiceStatus, StatsTracker, build_status, build_statuses
from .tagging import EMPTY_INDEX, ServiceIndex, build_index, resolve_service
from .validate import (
    DEFAULT_FORMATS,
    FORMAT_NAME,
    LIMITS,
    RESERVED_IDS,
    validate_log_source_fields,
    validate_service_fields,
)

log = logging.getLogger("chimericmesh.services")

AFTER_COMMIT_TIMEOUT = 5.0

InTxCallback = Callable[[Tx, ServiceChange], None]
AfterCommitCallback = Callable[[ServiceChange], "Awaitable[None] | None"]

_SELECT_SERVICES = "SELECT * FROM services ORDER BY seq"
_SELECT_SOURCES = "SELECT * FROM log_sources ORDER BY seq"
_SELECT_ONE_SERVICE = "SELECT * FROM services WHERE id = ?"
_SELECT_ONE_SOURCES = "SELECT * FROM log_sources WHERE service_id = ? ORDER BY seq"
_SEED_STATS = "SELECT service_id, COUNT(*), MAX(id) FROM events GROUP BY service_id"  # covered by idx_events_service
_HAS_TABLE = "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'services'"


def _retrieve_exception(task: "asyncio.Future[Any]") -> None:
    """Mark the exception of a shielded task as retrieved so a cancelled caller logs nothing."""
    if not task.cancelled():
        task.exception()


def _json_list(text: str) -> tuple[str, ...]:
    try:
        value = json.loads(text)
    except ValueError:
        return ()
    return tuple(str(v) for v in value) if isinstance(value, list) else ()


def _source_from_row(row: sqlite3.Row) -> LogSource:
    return LogSource(
        id=row["id"], service_id=row["service_id"], path=row["path"], format=row["format"],
        enabled=bool(row["enabled"]), backfill_lines=int(row["backfill_lines"]), origin=row["origin"],
        host=row["host"], created_at=float(row["created_at"]), updated_at=float(row["updated_at"]),
    )


def _service_from_row(row: sqlite3.Row, sources: Sequence[LogSource]) -> Service:
    return Service(
        id=row["id"], name=row["name"], hosts=_json_list(row["hosts"]),
        app_names=_json_list(row["app_names"]), protocols=_json_list(row["protocols"]),
        log_sources=tuple(sources), code_folder=row["code_folder"],
        created_at=float(row["created_at"]), updated_at=float(row["updated_at"]), seq=int(row["seq"]),
    )


def _load_all(tx: Tx) -> list[Service]:
    by_service: dict[str, list[LogSource]] = {}
    for row in tx.all(_SELECT_SOURCES):
        by_service.setdefault(row["service_id"], []).append(_source_from_row(row))
    return [_service_from_row(r, by_service.get(r["id"], [])) for r in tx.all(_SELECT_SERVICES)]


def _seed_rows(tx: Tx) -> list[tuple[str | None, int, float | None]]:
    """Count and newest receive time per service. The newest row is found by id (index only) and its time read by primary key, so no table scan happens (R5.7)."""
    out: list[tuple[str | None, int, float | None]] = []
    for sid, total, newest in tx.all(_SEED_STATS):
        row = tx.one("SELECT ingested FROM events WHERE id = ?", (newest,))
        out.append((sid, int(total), None if row is None else float(row[0])))
    return out


def _load_one(tx: Tx, service_id: str) -> Service | None:
    row = tx.one(_SELECT_ONE_SERVICE, (service_id,))
    if row is None:
        return None
    return _service_from_row(row, [_source_from_row(r) for r in tx.all(_SELECT_ONE_SOURCES, (service_id,))])


@dataclass(frozen=True, slots=True)
class _Subscription:
    name: str
    on_service_changed: InTxCallback | None
    on_code_folder_changed: InTxCallback | None
    on_service_deleted: InTxCallback | None
    after_commit: AfterCommitCallback | None


@dataclass(frozen=True, slots=True)
class _Delta:
    upserts: tuple[Service, ...] = ()
    removed: tuple[str, ...] = ()
    changes: tuple[ServiceChange, ...] = ()
    result: Any = None


def _in_tx_callbacks(subs: Sequence[_Subscription], change: ServiceChange) -> list[tuple[str, Callable[..., None]]]:
    out: list[tuple[str, Callable[..., None]]] = []
    for sub in subs:
        if change.kind == "service_deleted":
            if sub.on_service_deleted is not None:
                out.append((sub.name, sub.on_service_deleted))
            continue
        if change.kind == "service_created":
            if sub.on_service_changed is not None:
                out.append((sub.name, sub.on_service_changed))
            continue
        if sub.on_service_changed is not None:
            out.append((sub.name, sub.on_service_changed))
        if change.kind == "service_updated" and change.code_folder_changed and sub.on_code_folder_changed is not None:
            out.append((sub.name, sub.on_code_folder_changed))
    return out


class ServiceRegistry:
    """Registry of services and log sources. Reads are in-memory; every mutation is one transaction."""

    def __init__(self, store: Store, state: StateView, *, services: Sequence[Service] = (),
                 protected: Sequence[str] = (), known_formats: Iterable[str] | None = None,
                 clock: Callable[[], float] | None = None, home: str | None = None,
                 stats_rows: Iterable[tuple[str | None, int, float | None]] = ()) -> None:
        self._store = store
        self._state = state
        self._protected = tuple(protected)
        self._home = home
        self._clock = clock or state.now
        self._known_formats: frozenset[str] = DEFAULT_FORMATS
        if known_formats is not None:
            self.set_known_formats(known_formats)
        self._services: dict[str, Service] = {}
        self._index: ServiceIndex = EMPTY_INDEX
        self._subs: list[_Subscription] = []
        self._status_provider: StatusProvider | None = None
        self._stats = StatsTracker()
        self._stats.seed(stats_rows)
        self._guard = ProviderGuard(self._clock)
        self._lock = asyncio.Lock()
        self._replace(services, ())

    # ---- construction --------------------------------------------------------------------------

    @classmethod
    def load(cls, store: Store, *, state: StateView, protected: Sequence[str] = (),
             known_formats: Iterable[str] | None = None, clock: Callable[[], float] | None = None,
             home: str | None = None) -> "ServiceRegistry":
        def read(tx: Tx) -> tuple[list[Service], list[tuple[str | None, int, float | None]]] | None:
            if tx.one(_HAS_TABLE) is None:
                return None
            return _load_all(tx), _seed_rows(tx)

        loaded = store.read_sync(read, allow_blocking=True)
        if loaded is None:
            raise ServicesNotMigrated("open the store with services.register_migrations")
        services, stats_rows = loaded
        return cls(store, state, services=services, protected=protected, known_formats=known_formats,
                   clock=clock, home=home, stats_rows=stats_rows)

    # ---- reads (loop thread, no I/O) -----------------------------------------------------------

    def list(self) -> list[Service]:
        return list(self._services.values())

    def get(self, service_id: str) -> Service | None:
        return self._services.get(service_id)

    def log_sources(self, *, enabled_only: bool = False) -> list[LogSource]:
        out = [s for svc in self._services.values() for s in svc.log_sources]
        out.sort(key=lambda s: int(s.id[2:]) if s.id[2:].isdigit() else 0)
        return [s for s in out if s.enabled] if enabled_only else out

    def source(self, log_source_id: str) -> LogSource | None:
        sid = self._index.by_source.get(log_source_id)
        if sid is None:
            return None
        return next((s for s in self._services[sid].log_sources if s.id == log_source_id), None)

    def tag(self, event: Event) -> Event:
        """Assign the service of the event (R4.2). Only ``service_id`` can change."""
        found = resolve_service(self._index, event.source, event.host, event.app, event.log_source_id)
        self._stats.observe(found, event.severity, float(self._clock()))
        if found == event.service_id:
            return event
        return dataclasses.replace(event, service_id=found)

    def statuses(self) -> list[ServiceStatus]:
        return build_statuses(list(self._services.values()), self._stats, self._open_counts(),
                              self._status_provider, self._guard, float(self._clock()))

    def status_of(self, service_id: str) -> ServiceStatus | None:
        return build_status(service_id, self._services, self._stats, self._open_counts(),
                            self._status_provider, self._guard, float(self._clock()))

    def _open_counts(self) -> Counter[str]:
        return Counter(i.service_id or "unassigned" for i in self._state.incidents(status="open"))

    # ---- injection -----------------------------------------------------------------------------

    def subscribe(self, on_service_changed: InTxCallback | None = None,
                  on_code_folder_changed: InTxCallback | None = None,
                  on_service_deleted: InTxCallback | None = None,
                  after_commit: AfterCommitCallback | None = None, *, name: str = "") -> None:
        callbacks = (on_service_changed, on_code_folder_changed, on_service_deleted, after_commit)
        label = name
        if not label:
            first = next((c for c in callbacks if c is not None), None)
            label = getattr(first, "__qualname__", None) or repr(first) if first is not None else "subscriber"
        self._subs.append(_Subscription(label, *callbacks))

    def set_known_formats(self, names: Iterable[str]) -> None:
        cleaned: set[str] = set()
        for name in names:
            low = str(name).lower()
            if FORMAT_NAME.fullmatch(low) is None:
                raise ValueError(f"invalid format name: {name!r}")
            cleaned.add(low)
        self._known_formats = frozenset(cleaned)

    def set_status_provider(self, fn: StatusProvider | None) -> None:
        self._status_provider = fn

    # ---- helpers on the loop thread ------------------------------------------------------------

    def _replace(self, upserts: Iterable[Service], removed: Iterable[str]) -> None:
        """Replace entries and rebuild the index. No await: readers never see a half-applied change."""
        services = dict(self._services)
        for sid in removed:
            services.pop(sid, None)
        for svc in upserts:
            services[svc.id] = svc
        ordered = dict(sorted(services.items(), key=lambda kv: kv[1].seq))
        index = build_index(list(ordered.values()))
        self._services, self._index = ordered, index

    def _normalize_folder(self, raw: str) -> str:
        return normalize_code_folder(raw, protected=self._protected, home=self._home)

    def _source_values(self, fields: LogSourceFields) -> LogSourceFields:
        raw = {"path": fields.path, "format": fields.format, "enabled": fields.enabled,
               "host": fields.host, "backfill_lines": fields.backfill_lines}
        checked = validate_log_source_fields(raw, known_formats=self._known_formats, for_create=True)
        assert isinstance(checked, LogSourceFields)
        path = normalize_log_path(checked.path)
        inspect_log_path(path)
        return dataclasses.replace(checked, path=path)

    def _service_values(self, fields: ServiceFields) -> ServiceFields:
        raw: dict[str, object] = {
            "id": fields.id, "name": fields.name, "hosts": list(fields.hosts),
            "app_names": list(fields.app_names), "protocols": list(fields.protocols),
            "code_folder": fields.code_folder,
        }
        checked = validate_service_fields(raw, for_create=True)
        assert isinstance(checked, ServiceFields)
        if checked.code_folder is not None:
            checked = dataclasses.replace(checked, code_folder=self._normalize_folder(checked.code_folder))
        return checked

    # ---- mutation machinery --------------------------------------------------------------------

    async def _mutate(self, plan: Callable[[Tx], _Delta]) -> _Delta:
        """Commit and apply as one shielded unit: a cancelled caller cannot split them (store T6)."""
        task = asyncio.ensure_future(self._commit_and_apply(plan))
        task.add_done_callback(_retrieve_exception)
        return await asyncio.shield(task)

    async def _commit_and_apply(self, plan: Callable[[Tx], _Delta]) -> _Delta:
        async with self._lock:
            try:
                delta = await self._store.transact(plan)
            except SubscriberError as exc:
                raise SubscriberFailed(exc.name, exc.cause) from exc
            except sqlite3.IntegrityError as exc:
                raise Conflict("conflicting change") from exc
            self._replace(delta.upserts, delta.removed)
            for sid in delta.removed:
                self._stats.drop(sid)
        for change in delta.changes:
            for sub in tuple(self._subs):
                if sub.after_commit is not None:
                    await self._run_after_commit(sub, change)
        return delta

    async def _run_after_commit(self, sub: _Subscription, change: ServiceChange) -> None:
        callback = sub.after_commit
        assert callback is not None
        try:
            async def call() -> None:
                result = callback(change)
                if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
                    await result

            await asyncio.wait_for(call(), AFTER_COMMIT_TIMEOUT)
        except asyncio.TimeoutError:
            log.error("after_commit subscriber %s exceeded %.1fs for %s", sub.name, AFTER_COMMIT_TIMEOUT, change.kind)
        except Exception:
            log.exception("after_commit subscriber %s failed for %s", sub.name, change.kind)

    def _audit(self, tx: Tx, actor: str, action: str, subject: str, detail: str, service_id: str) -> None:
        self._store.audit.append_tx(tx, actor, action, subject, detail, service_id=service_id)

    # ---- mutations -----------------------------------------------------------------------------

    async def create_service(self, fields: ServiceFields, *, actor: str) -> Service:
        service, _ = await self.create_service_with_sources(fields, (), actor=actor)
        return service

    async def create_service_with_sources(self, fields: ServiceFields, sources: Sequence[LogSourceFields], *,
                                          actor: str, origin: Origin = "user") -> tuple[Service, list[LogSource]]:
        """One transaction: the service, every source, one audit row each, subscribers once per change."""
        svc_fields = self._service_values(fields)
        src_fields = [self._source_values(s) for s in sources]
        if len(src_fields) > LIMITS.sources_per_service:
            raise ValidationFailed("log_sources", f"at most {LIMITS.sources_per_service} per service")
        now = float(self._clock())
        subs = tuple(self._subs)

        def plan(tx: Tx) -> _Delta:
            self._check_new_service(tx, svc_fields.id, len(src_fields))
            seq = tx.counter_next("service", floor=_max(tx, "services"))
            tx.execute(
                "INSERT INTO services (id, seq, name, hosts, app_names, protocols, code_folder, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (svc_fields.id, seq, svc_fields.name, json.dumps(svc_fields.hosts), json.dumps(svc_fields.app_names),
                 json.dumps(svc_fields.protocols), svc_fields.code_folder, now, now))
            created = Service(svc_fields.id, svc_fields.name, svc_fields.hosts, svc_fields.app_names,
                              svc_fields.protocols, (), svc_fields.code_folder, now, now, seq)
            changes: list[ServiceChange] = []
            first = ServiceChange("service_created", created.id, None, created, fields=_set_fields(svc_fields))
            self._audit(tx, actor, "service_created", created.id, _detail(first), created.id)
            run_callbacks(tx, _in_tx_callbacks(subs, first), first)
            changes.append(first)
            current = created
            made: list[LogSource] = []
            for fs in src_fields:
                src = self._insert_source(tx, current.id, fs, origin, now)
                made.append(src)
                nxt = dataclasses.replace(current, log_sources=current.log_sources + (src,))
                ch = ServiceChange("log_source_added", current.id, current, nxt, log_source_id=src.id,
                                   fields=("path", "format"))
                self._audit(tx, actor, "log_source_added", src.id, _detail(ch), current.id)
                run_callbacks(tx, _in_tx_callbacks(subs, ch), ch)
                changes.append(ch)
                current = nxt
            return _Delta((current,), (), tuple(changes), (current, made))

        delta = await self._mutate(plan)
        return delta.result

    def _check_new_service(self, tx: Tx, service_id: str, new_sources: int) -> None:
        if service_id in RESERVED_IDS:
            raise ValidationFailed("id", f"{service_id!r} is reserved")
        if tx.one("SELECT 1 FROM service_tombstones WHERE id = ?", (service_id,)) is not None:
            raise Conflict("id belonged to a deleted service; choose another id")
        if tx.one("SELECT 1 FROM services WHERE id = ?", (service_id,)) is not None:
            raise Conflict(f"service {service_id!r} already exists")
        if tx.one("SELECT COUNT(*) FROM services")[0] >= LIMITS.services:
            raise ValidationFailed("services", f"at most {LIMITS.services} services")
        if tx.one("SELECT COUNT(*) FROM log_sources")[0] + new_sources > LIMITS.sources_total:
            raise ValidationFailed("log_sources", f"at most {LIMITS.sources_total} log sources")

    def _insert_source(self, tx: Tx, service_id: str, fs: LogSourceFields, origin: Origin, now: float) -> LogSource:
        n = tx.counter_next("log_source", floor=_max(tx, "log_sources"))
        sid = format_id("ls", n)
        tx.execute(
            "INSERT INTO log_sources (id, seq, service_id, path, format, enabled, backfill_lines, origin, host,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (sid, n, service_id, fs.path, fs.format, int(fs.enabled), fs.backfill_lines, origin, fs.host, now, now))
        return LogSource(sid, service_id, fs.path, fs.format, fs.enabled, fs.backfill_lines, origin, fs.host, now, now)

    async def update_service(self, service_id: str, patch: ServicePatch, *, actor: str) -> Service:
        _refuse_builtin(service_id)
        raw: dict[str, object] = {}
        for name in ("name", "hosts", "app_names", "protocols"):
            value = getattr(patch, name)
            if value is not None:
                raw[name] = list(value) if isinstance(value, tuple) else value
        if patch.code_folder is CLEAR:
            raw["code_folder"] = None
        elif patch.code_folder is not None:
            raw["code_folder"] = patch.code_folder
        checked = validate_service_fields(raw, for_create=False)
        assert isinstance(checked, ServicePatch)
        folder = checked.code_folder
        if isinstance(folder, str):
            folder = self._normalize_folder(folder)
        now = float(self._clock())
        subs = tuple(self._subs)

        def plan(tx: Tx) -> _Delta:
            old = _load_one(tx, service_id)
            if old is None:
                raise ServiceNotFound(service_id)
            new_values = {
                "name": checked.name if checked.name is not None else old.name,
                "hosts": checked.hosts if checked.hosts is not None else old.hosts,
                "app_names": checked.app_names if checked.app_names is not None else old.app_names,
                "protocols": checked.protocols if checked.protocols is not None else old.protocols,
                "code_folder": (None if folder is CLEAR else folder) if folder is not None else old.code_folder,
            }
            changed = tuple(k for k, v in new_values.items() if getattr(old, k) != v)
            if not changed:
                return _Delta(result=old)
            tx.execute(
                "UPDATE services SET name=?, hosts=?, app_names=?, protocols=?, code_folder=?, updated_at=? WHERE id=?",
                (new_values["name"], json.dumps(new_values["hosts"]), json.dumps(new_values["app_names"]),
                 json.dumps(new_values["protocols"]), new_values["code_folder"], now, service_id))
            new = dataclasses.replace(old, updated_at=now, **new_values)
            change = ServiceChange("service_updated", service_id, old, new, fields=changed)
            self._audit(tx, actor, "service_updated", service_id, _detail(change), service_id)
            run_callbacks(tx, _in_tx_callbacks(subs, change), change)
            return _Delta((new,), (), (change,), new)

        delta = await self._mutate(plan)
        return delta.result

    async def delete_service(self, service_id: str, *, actor: str) -> None:
        _refuse_builtin(service_id)
        now = float(self._clock())
        subs = tuple(self._subs)

        def plan(tx: Tx) -> _Delta:
            old = _load_one(tx, service_id)
            if old is None:
                raise ServiceNotFound(service_id)
            tx.execute("DELETE FROM log_sources WHERE service_id = ?", (service_id,))
            tx.execute("DELETE FROM services WHERE id = ?", (service_id,))
            tx.execute("INSERT INTO service_tombstones (id, deleted_at) VALUES (?, ?)", (service_id, now))
            change = ServiceChange("service_deleted", service_id, old, None)
            self._audit(tx, actor, "service_deleted", service_id, "", service_id)
            run_callbacks(tx, _in_tx_callbacks(subs, change), change)
            return _Delta((), (service_id,), (change,))

        await self._mutate(plan)

    async def add_log_source(self, service_id: str, fields: LogSourceFields, *, actor: str,
                             origin: Origin = "user") -> LogSource:
        _refuse_builtin(service_id)
        fs = self._source_values(fields)
        now = float(self._clock())
        subs = tuple(self._subs)

        def plan(tx: Tx) -> _Delta:
            old = _load_one(tx, service_id)
            if old is None:
                raise ServiceNotFound(service_id)
            if len(old.log_sources) >= LIMITS.sources_per_service:
                raise ValidationFailed("log_sources", f"at most {LIMITS.sources_per_service} per service")
            if tx.one("SELECT COUNT(*) FROM log_sources")[0] >= LIMITS.sources_total:
                raise ValidationFailed("log_sources", f"at most {LIMITS.sources_total} log sources")
            src = self._insert_source(tx, service_id, fs, origin, now)
            new = dataclasses.replace(old, log_sources=old.log_sources + (src,))
            change = ServiceChange("log_source_added", service_id, old, new, log_source_id=src.id,
                                   fields=("path", "format"))
            self._audit(tx, actor, "log_source_added", src.id, _detail(change), service_id)
            run_callbacks(tx, _in_tx_callbacks(subs, change), change)
            return _Delta((new,), (), (change,), src)

        delta = await self._mutate(plan)
        return delta.result

    async def update_log_source(self, service_id: str, source_id: str, patch: LogSourcePatch, *,
                                actor: str) -> LogSource:
        _refuse_builtin(service_id)
        raw: dict[str, object] = {}
        if patch.format is not None:
            raw["format"] = patch.format
        if patch.enabled is not None:
            raw["enabled"] = patch.enabled
        if patch.host is CLEAR:
            raw["host"] = None
        elif patch.host is not None:
            raw["host"] = patch.host
        if patch.backfill_lines is not None:
            raw["backfill_lines"] = patch.backfill_lines
        checked = validate_log_source_fields(raw, known_formats=self._known_formats, for_create=False)
        assert isinstance(checked, LogSourcePatch)
        now = float(self._clock())
        subs = tuple(self._subs)

        def plan(tx: Tx) -> _Delta:
            old = _load_one(tx, service_id)
            if old is None:
                raise ServiceNotFound(service_id)
            current = next((s for s in old.log_sources if s.id == source_id), None)
            if current is None:
                raise LogSourceNotFound(source_id)
            values = {
                "format": checked.format if checked.format is not None else current.format,
                "enabled": checked.enabled if checked.enabled is not None else current.enabled,
                "host": (None if checked.host is CLEAR else checked.host) if checked.host is not None else current.host,
                "backfill_lines": checked.backfill_lines if checked.backfill_lines is not None else current.backfill_lines,
            }
            changed = tuple(k for k, v in values.items() if getattr(current, k) != v)
            if not changed:
                return _Delta(result=current)
            tx.execute("UPDATE log_sources SET format=?, enabled=?, host=?, backfill_lines=?, updated_at=? WHERE id=?",
                       (values["format"], int(values["enabled"]), values["host"], values["backfill_lines"], now, source_id))
            updated = dataclasses.replace(current, updated_at=now, **values)
            new = dataclasses.replace(old, log_sources=tuple(updated if s.id == source_id else s for s in old.log_sources))
            change = ServiceChange("log_source_updated", service_id, old, new, log_source_id=source_id, fields=changed)
            self._audit(tx, actor, "log_source_updated", source_id, _detail(change), service_id)
            run_callbacks(tx, _in_tx_callbacks(subs, change), change)
            return _Delta((new,), (), (change,), updated)

        delta = await self._mutate(plan)
        return delta.result

    async def remove_log_source(self, service_id: str, source_id: str, *, actor: str) -> None:
        _refuse_builtin(service_id)
        subs = tuple(self._subs)

        def plan(tx: Tx) -> _Delta:
            old = _load_one(tx, service_id)
            if old is None:
                raise ServiceNotFound(service_id)
            if not any(s.id == source_id for s in old.log_sources):
                raise LogSourceNotFound(source_id)
            tx.execute("DELETE FROM log_sources WHERE id = ?", (source_id,))
            new = dataclasses.replace(old, log_sources=tuple(s for s in old.log_sources if s.id != source_id))
            change = ServiceChange("log_source_removed", service_id, old, new, log_source_id=source_id,
                                   fields=("path", "format"))
            self._audit(tx, actor, "log_source_removed", source_id, _detail(change), service_id)
            run_callbacks(tx, _in_tx_callbacks(subs, change), change)
            return _Delta((new,), (), (change,))

        await self._mutate(plan)


def _refuse_builtin(service_id: str) -> None:
    if service_id in RESERVED_IDS:
        raise BuiltinService(f"{service_id!r} is built in and cannot be changed")


def _max(tx: Tx, table: str) -> int:
    sql = "SELECT COALESCE(MAX(seq), 0) FROM services" if table == "services" else "SELECT COALESCE(MAX(seq), 0) FROM log_sources"
    return int(tx.one(sql)[0])


def _set_fields(f: ServiceFields) -> tuple[str, ...]:
    out = ["name"]
    for name in ("hosts", "app_names", "protocols", "code_folder"):
        if getattr(f, name):
            out.append(name)
    return tuple(out)


def _detail(change: ServiceChange) -> str:
    """Field names only; a code folder change also names the old and new path (a scope change)."""
    text = "fields=" + ",".join(change.fields)
    if change.kind in ("service_updated",) and change.code_folder_changed:
        text += f"; code_folder={change.old_code_folder} -> {change.new_code_folder}"
    elif change.kind == "service_created" and change.new_code_folder:
        text += f"; code_folder={change.new_code_folder}"
    return text
