"""The scan manager: owns scans in memory, starts them on request only and never on its own (R1.1).

The engine runs on one dedicated executor thread with immutable inputs. This class runs on the loop thread:
it builds the inputs, stores the result and writes one audit row per scan (counts only)."""
from __future__ import annotations

import asyncio
import functools
import itertools
import logging
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from chimericmesh.services.registry import ServiceRegistry
from chimericmesh.store import Store, StoreError

from . import dismissals
from .catalog import CatalogLoad
from .errors import AlreadyAccepted, CatalogUnavailable, ScanBusy, SuggestionNotFound
from .models import MAX_KEPT_SCANS, SCAN_TOTAL_SECONDS, SCAN_TTL_SECONDS, Scan, ScanInputs, ScanOutcome, Suggestion
from .text import path_key

if TYPE_CHECKING:
    from .accept import Acceptor

log = logging.getLogger("chimericmesh.discovery")


class ScanManager:
    def __init__(self, *, load: CatalogLoad, engine: Callable[..., ScanOutcome], runner: Any, procfs: Any, fs: Any,
                 store: Store, registry: ServiceRegistry, known_formats: frozenset[str], now: Callable[[], float],
                 os_name: str | None, is_admin: bool, own_pid: int, env: Mapping[str, str],
                 scan_timeout: float = SCAN_TOTAL_SECONDS + 5.0) -> None:
        self._load, self._engine, self._runner, self._procfs, self._fs = load, engine, runner, procfs, fs
        self._store, self._registry, self._known_formats, self._now = store, registry, known_formats, now
        self._os_name, self._is_admin, self._own_pid = os_name, is_admin, own_pid
        self._env = dict(env)
        self._scan_timeout = scan_timeout
        self._scans: dict[str, Scan] = {}
        self._cancels: dict[str, threading.Event] = {}
        self._tasks: set[asyncio.Task] = set()
        self._scan_ids = itertools.count(1)
        self._suggestion_ids = itertools.count(1)
        self._id_lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="discovery-scan")
        self._closed = False

    # ---- ids ---------------------------------------------------------------------------------------

    def _next_suggestion_id(self) -> str:
        with self._id_lock:
            return f"sg{next(self._suggestion_ids)}"

    @property
    def os_name(self) -> str | None:
        return self._os_name

    # ---- queries -----------------------------------------------------------------------------------

    def _purge(self) -> None:
        now = self._now()
        for sid in [k for k, s in self._scans.items()
                    if s.state != "running" and s.finished_at is not None and now - s.finished_at > SCAN_TTL_SECONDS]:
            self._scans.pop(sid, None)
        finished = sorted((s for s in self._scans.values() if s.state != "running"),
                          key=lambda s: (s.finished_at if s.finished_at is not None else 0.0))
        while len(self._scans) > MAX_KEPT_SCANS and finished:
            self._scans.pop(finished.pop(0).id, None)

    def get(self, scan_id: str) -> Scan | None:
        self._purge()
        return self._scans.get(scan_id)

    def find(self, suggestion_id: str) -> tuple[Scan, Suggestion] | None:
        self._purge()
        for scan in self._scans.values():
            for sug in scan.suggestions:
                if sug.suggestion_id == suggestion_id:
                    return scan, sug
        return None

    # ---- start -------------------------------------------------------------------------------------

    async def start(self, *, actor: str) -> Scan:
        """Nothing else in the application calls this, so no scan begins on startup, on a timer or as a side
        effect (R1.1). The first block has no await, so two concurrent starts cannot both pass the busy check."""
        if self._load.catalog is None:
            raise CatalogUnavailable(self._load.reason)
        self._purge()
        for existing in self._scans.values():
            if existing.state == "running":
                raise ScanBusy(existing.id)
        scan = Scan(id=f"sc{next(self._scan_ids)}", state="running", started_at=float(self._now()),
                    finished_at=None, limited=False, os=self._os_name or "unknown", reason="")
        self._scans[scan.id] = scan
        cancel = threading.Event()
        self._cancels[scan.id] = cancel
        try:
            if self._os_name is None:
                self._fail(scan, "unsupported platform")
                await self._audit(scan, actor)
                return scan
            try:
                dismissed = await dismissals.load_fingerprints(self._store)
            except StoreError:
                log.warning("discovery: the dismissals could not be read; scanning without them")
                dismissed = frozenset()
            covered = {path_key(src.path, self._os_name): src.service_id for src in self._registry.log_sources()}
            inputs = ScanInputs(os_name=self._os_name, is_admin=self._is_admin, own_pid=self._own_pid,
                                env=dict(self._env), catalog=self._load.catalog, covered=covered,
                                dismissed=dismissed, known_formats=self._known_formats)
            task = asyncio.create_task(self._run(scan, inputs, actor, cancel))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        except BaseException:
            self._fail(scan, "internal error")
            try:
                await self._audit(scan, actor)
            except BaseException:  # noqa: BLE001 - best effort; the original error is re-raised
                pass
            raise
        return scan

    def _fail(self, scan: Scan, reason: str) -> None:
        scan.state = "failed"
        scan.reason = reason
        scan.finished_at = float(self._now())
        scan.limited = True
        self._cancels.pop(scan.id, None)

    async def _run(self, scan: Scan, inputs: ScanInputs, actor: str, cancel: threading.Event) -> None:
        loop = asyncio.get_running_loop()
        call = functools.partial(self._engine, inputs, self._runner, self._procfs, self._fs,
                                 self._next_suggestion_id, cancel)
        try:
            outcome: ScanOutcome = await asyncio.wait_for(loop.run_in_executor(self._executor, call),
                                                          self._scan_timeout)
        except asyncio.CancelledError:
            cancel.set()
            self._fail(scan, "cancelled")
            raise
        except TimeoutError:
            cancel.set()
            log.warning("discovery: a scan did not finish in time")
            self._fail(scan, "scan timed out")
        except Exception:  # noqa: BLE001
            log.exception("discovery: the scan engine failed")      # no process data is in scope here
            self._fail(scan, "internal error")
        else:
            scan.state = outcome.state
            scan.finished_at = float(self._now())
            scan.limited = outcome.limited
            scan.reason = outcome.reason
            scan.failed_tools = outcome.failed_tools
            scan.suggestions = outcome.suggestions
        finally:
            self._cancels.pop(scan.id, None)
        await self._audit(scan, actor)

    async def _audit(self, scan: Scan, actor: str) -> None:
        detail = (f"state={scan.state} suggestions={len(scan.suggestions)} limited={scan.limited} "
                  f"tools_failed={len(scan.failed_tools)}")
        try:
            await self._store.audit.append(actor, "discovery_scan", scan.id, detail)
        except Exception:  # noqa: BLE001 - a failing audit write never changes the scan
            log.error("discovery: the audit row for scan %s could not be written", scan.id)

    # ---- dismissals --------------------------------------------------------------------------------

    async def dismiss(self, suggestion_id: str, *, actor: str) -> None:
        found = self.find(suggestion_id)
        if found is None:
            raise SuggestionNotFound(suggestion_id)
        scan, sug = found
        if sug.suggestion_id in scan.accepted or sug.suggestion_id in scan.accepting:
            raise AlreadyAccepted(suggestion_id)
        await dismissals.dismiss(self._store, sug.app_id, sug.fingerprint, actor=actor, paths=len(sug.candidates))

    async def dismissals(self) -> list[dict]:
        return await dismissals.list_dismissals(self._store)

    async def clear_dismissals(self, *, actor: str) -> int:
        return await dismissals.clear(self._store, actor=actor)

    # ---- shutdown ----------------------------------------------------------------------------------

    async def aclose(self, timeout: float = 0.2) -> None:
        """Stop a running scan: set its cancel flag (the tools are killed), cancel the task, never raise."""
        self._closed = True
        for cancel in list(self._cancels.values()):
            cancel.set()
        loop = asyncio.get_running_loop()
        tasks = [t for t in self._tasks if not t.done() and t.get_loop() is loop]
        for task in tasks:
            task.cancel()
        try:
            self._executor.shutdown(wait=False, cancel_futures=True)
        except Exception:  # noqa: BLE001
            pass
        if tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout)
            except BaseException:  # noqa: BLE001 - never raises, even when cancelled
                pass


@dataclass(frozen=True, slots=True)
class DiscoveryService:
    manager: ScanManager
    acceptor: "Acceptor"
    load: CatalogLoad
