"""The connector manager: one tailer per enabled log source, kept in step with the registry."""
from __future__ import annotations

import asyncio
import logging
import socket
import time
from collections.abc import Callable
from dataclasses import replace

from ..services.models import ServiceChange, SourceRuntime
from ..services.registry import ServiceRegistry
from ..parsers.formats import FORMAT_NAMES
from ..store import Store, Tx
from .config import ConnectorConfig
from .offsets import OffsetRow, OffsetStore, resume_state, row_from_state
from .submit import Submit
from .tailer import LAST_ERROR_MAX, FileTailer, SourceCounters, SourceSpec, build_spec, path_key
from .tailread import TailState

log = logging.getLogger("chimericmesh.connectors")


class ConnectorManager:
    def __init__(self, registry: ServiceRegistry, store: Store, submit: Submit, *,
                 machine: str | None = None, config: ConnectorConfig | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.registry = registry
        self._submit = submit
        self._cfg = config or ConnectorConfig()
        self._clock = clock
        self._machine = machine or socket.gethostname() or "localhost"
        self._offsets = OffsetStore(store)
        self._rows: dict[str, OffsetRow] = {}
        self._tailers: dict[str, FileTailer] = {}
        self._states: dict[str, TailState] = {}
        self._counters: dict[str, SourceCounters] = {}
        self._lock = asyncio.Lock()
        self._started = False
        self._dirty: set[str] = set()          # ids whose position must be written
        self._gone: set[str] = set()           # ids whose row must be deleted (disabled sources)
        self._flush_task: asyncio.Task | None = None
        self._reconcile_task: asyncio.Task | None = None
        self._last_flush_warn = float("-inf")
        registry.set_known_formats(FORMAT_NAMES)
        registry.set_status_provider(self.runtime)
        registry.subscribe(on_service_changed=self.drop_offsets, on_service_deleted=self.drop_offsets,
                           after_commit=self.on_change, name="connectors")

    # ---- reads -------------------------------------------------------------------------------------

    @property
    def tailers(self) -> dict[str, FileTailer]:
        """The running tailers by log source id (a copy)."""
        return dict(self._tailers)

    def runtime(self, log_source_id: str) -> SourceRuntime | None:
        """Status provider: a dictionary lookup, no I/O."""
        c = self._counters.get(log_source_id)
        if c is None:
            return None
        return SourceRuntime(status=c.status, lines_read=c.lines_read, events_emitted=c.events_emitted,  # type: ignore[arg-type]
                             lines_dropped=c.lines_dropped, bytes_read=c.bytes_read, unparsed=c.unparsed,
                             last_error=c.last_error[:LAST_ERROR_MAX])

    # ---- lifecycle ---------------------------------------------------------------------------------

    async def start(self) -> None:
        if not await self._offsets.available():
            self._offsets.warn_missing()
        else:
            try:
                self._rows = await self._offsets.load()
            except Exception as exc:
                log.warning("could not load tail offsets (%s); sources start fresh", exc)
        self._started = True
        await self.reconcile()
        if self._flush_task is None:
            self._flush_task = asyncio.get_running_loop().create_task(self._flush_loop(), name="connectors-flush")
        if self._reconcile_task is None:
            self._reconcile_task = asyncio.get_running_loop().create_task(self._reconcile_loop(),
                                                                           name="connectors-reconcile")

    async def aclose(self, timeout: float = 1.0) -> None:
        """Stop the loops and every tailer, then write the final offsets, all inside ``timeout``."""
        self._started = False
        deadline = time.monotonic() + timeout
        loops = [t for t in (self._flush_task, self._reconcile_task) if t is not None]
        self._flush_task = self._reconcile_task = None
        for t in loops:
            t.cancel()
        if loops:
            await asyncio.gather(*loops, return_exceptions=True)
        grace = min(1.0, max(0.05, timeout * 0.5))
        try:
            await asyncio.wait_for(self._stop_all(grace), max(0.05, deadline - time.monotonic()))
        except asyncio.TimeoutError:
            log.warning("connectors did not stop in time; cancelling the remaining tailers")
            for t in self._tailers.values():
                if t.task is not None:
                    t.task.cancel()
            self._tailers.clear()
        try:
            await asyncio.wait_for(self._flush(), max(0.05, deadline - time.monotonic()))
        except Exception as exc:  # includes the timeout: positions are repeated, not lost, after a restart
            log.warning("final tail offsets were not written: %s", exc or type(exc).__name__)
        for c in self._counters.values():
            c.status = "stopped"

    async def _stop_all(self, grace: float) -> None:
        async with self._lock:
            await self._stop_many(list(self._tailers), grace=grace)
    async def on_change(self, change: ServiceChange) -> None:
        await self.reconcile()

    def drop_offsets(self, tx: Tx, change: ServiceChange) -> None:
        """In-transaction subscriber (store-writer thread): delete the offset rows of removed sources."""
        if change.kind == "log_source_removed" and change.log_source_id:
            ids = [change.log_source_id]
        elif change.kind == "service_deleted" and change.old is not None:
            ids = [s.id for s in change.old.log_sources]
        else:
            return
        OffsetStore.drop_in_tx(tx, ids)

    # ---- reconcile ---------------------------------------------------------------------------------

    def _desired(self) -> tuple[dict[str, SourceSpec], dict[str, str]]:
        desired: dict[str, SourceSpec] = {}
        owners: dict[str, str] = {}
        blocked: dict[str, str] = {}
        for src in self.registry.log_sources(enabled_only=True):
            key = path_key(src.path)
            if key in owners:
                blocked[src.id] = owners[key]
                continue
            owners[key] = src.id
            desired[src.id] = build_spec(src, self.registry.get(src.service_id), self._machine)
        return desired, blocked

    async def reconcile(self) -> None:
        """Make the running tailers match the registry. Idempotent; serialized by one lock."""
        async with self._lock:
            if not self._started:
                return
            desired, blocked = self._desired()
            registered = {s.id for s in self.registry.log_sources()}
            stop = [sid for sid, t in self._tailers.items()
                    if sid not in desired or t.spec != desired[sid]]
            await self._stop_many(stop, grace=self._cfg.stop_grace)
            for sid in stop:
                if sid not in desired and sid in registered:
                    self._states[sid] = TailState()          # a disabled source re-attaches at the end
                    self._dirty.discard(sid)
                    self._gone.add(sid)
            for sid in registered - desired.keys() - blocked.keys():
                if sid in self._rows:
                    self._rows.pop(sid)
                    self._gone.add(sid)
            for sid in list(self._counters):
                if sid not in registered:
                    self._forget(sid)
            for sid in registered:
                self._counters.setdefault(sid, SourceCounters())
            for sid, owner in blocked.items():
                c = self._counters[sid]
                if c.last_error != f"path already tailed by {owner}":
                    log.warning("log source %s is not tailed: path already tailed by %s", sid, owner)
                c.status, c.last_error = "error", f"path already tailed by {owner}"
            for sid, spec in desired.items():
                t = self._tailers.get(sid)
                if t is not None and t.task is not None and not t.task.done():
                    continue
                self._start_tailer(spec)

    def _forget(self, sid: str) -> None:
        self._counters.pop(sid, None)
        self._states.pop(sid, None)
        self._rows.pop(sid, None)
        self._dirty.discard(sid)
        self._gone.discard(sid)

    def _start_tailer(self, spec: SourceSpec) -> None:
        sid = spec.source_id
        state = self._states.get(sid)
        if state is None:
            state = resume_state(self._rows.get(sid))
            self._states[sid] = state
        counters = self._counters.setdefault(sid, SourceCounters())
        counters.last_error = ""
        tailer = FileTailer(spec, state, counters, self._submit, self._cfg, machine=self._machine,
                            clock=self._clock, on_state=self._on_state)
        self._tailers[sid] = tailer
        tailer.start()

    def _on_state(self, sid: str, state: TailState) -> None:
        old = self._states.get(sid)
        self._states[sid] = state
        if old is None or (old.ident, old.offset, old.discarding) != (state.ident, state.offset, state.discarding):
            self._dirty.add(sid)
            self._gone.discard(sid)

    async def _stop_many(self, ids: list[str], *, grace: float) -> None:
        tailers = [self._tailers.pop(sid) for sid in ids if sid in self._tailers]
        if tailers:
            await asyncio.gather(*(t.stop(grace) for t in tailers), return_exceptions=True)

    # ---- housekeeping ------------------------------------------------------------------------------

    async def _flush(self) -> None:
        """Write the positions that changed since the last flush; on failure they stay dirty."""
        ids, gone = list(self._dirty), list(self._gone)
        if not ids and not gone:
            return
        self._dirty.difference_update(ids)
        self._gone.difference_update(gone)
        now = time.time()
        rows = [r for r in (row_from_state(i, self._states[i], now) for i in ids if i in self._states) if r]
        try:
            await self._offsets.delete(gone)
            await self._offsets.flush(rows)
        except BaseException:
            self._dirty.update(i for i in ids if i in self._counters)
            self._gone.update(gone)
            raise

    def _warn_flush(self, exc: BaseException) -> None:
        now = time.monotonic()
        if now - self._last_flush_warn >= 60.0:
            self._last_flush_warn = now
            log.warning("could not write tail offsets (%s); they are kept and retried", exc)

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self._cfg.flush_interval)
            try:
                await self._flush()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._warn_flush(exc)

    async def _reconcile_loop(self) -> None:
        while True:
            await asyncio.sleep(self._cfg.reconcile_interval)
            try:
                await self.reconcile()
                await self._offsets.prune(frozenset(s.id for s in self.registry.log_sources()))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._warn_flush(exc)