"""Reduce-operations limiter (R6.5): bounds audit rows and announcements, never the action itself."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from chimericmesh.store.ports import AuditSink

log = logging.getLogger("chimericmesh.services")

REDUCE_OPS_PER_MINUTE = 10
WINDOW_SECONDS = 60.0
MAX_KEYS = 256


def reduce_key(kind: str, who: str) -> str:
    """The caller key: the client address for REST, the nick for IRC, ``dashboard-terminal:<address>``."""
    if kind == "rest" or kind == "irc":
        return who
    if kind == "terminal":
        return f"dashboard-terminal:{who}"
    raise ValueError(f"unknown caller kind: {kind!r}")


@dataclass
class _Window:
    start: float
    own: int = 0
    suppressed: int = 0


class ReduceAuditLimiter:
    def __init__(self, audit: AuditSink | None, now: Callable[[], float] = time.time, *,
                 per_minute: int = REDUCE_OPS_PER_MINUTE, max_keys: int = MAX_KEYS,
                 window: float = WINDOW_SECONDS) -> None:
        self._audit = audit
        self._now = now
        self._per = per_minute
        self._max = max_keys
        self._window = window
        self._windows: OrderedDict[str, _Window] = OrderedDict()
        self._timers: set[asyncio.TimerHandle] = set()
        self._tasks: set[asyncio.Task] = set()

    async def record(self, key: str, *, actor: str, action: str, subject: str, detail: str = "",
                     service_id: str | None = None, rule_id: str | None = None,
                     proposal_id: str | None = None) -> bool:
        """True: this call gets its own row and the caller announces it. False: counted in the window row."""
        now = float(self._now())
        win = self._windows.get(key)
        if win is not None and now - win.start >= self._window:
            await self._flush_one(key, win)
            win = None
        if win is None:
            win = self._windows[key] = _Window(now)
            await self._evict()
        self._windows.move_to_end(key)
        if win.own < self._per:
            win.own += 1
            await self._write(actor, action, subject, detail, service_id, rule_id, proposal_id)
            return True
        win.suppressed += 1
        if win.suppressed == 1:
            self._schedule(max(0.0, win.start + self._window - now))
        return False

    async def flush_due(self) -> None:
        now = float(self._now())
        for key, win in list(self._windows.items()):
            if now - win.start >= self._window:
                await self._flush_one(key, win)

    async def aclose(self) -> None:
        for handle in list(self._timers):
            handle.cancel()
        self._timers.clear()
        for key, win in list(self._windows.items()):
            await self._flush_one(key, win)
        for task in list(self._tasks):
            await asyncio.gather(task, return_exceptions=True)

    # ---- internals -----------------------------------------------------------------------------

    async def _flush_one(self, key: str, win: _Window) -> None:
        self._windows.pop(key, None)
        if win.suppressed:
            await self._write("reduce-limiter", "reduce_ops_suppressed", key, f"count={win.suppressed}")

    async def _evict(self) -> None:
        while len(self._windows) > self._max:
            key, win = next(iter(self._windows.items()))
            await self._flush_one(key, win)

    async def _write(self, actor: str, action: str, subject: str, detail: str,
                     service_id: str | None = None, rule_id: str | None = None,
                     proposal_id: str | None = None) -> None:
        if self._audit is None:
            return
        try:
            await self._audit.append(actor, action, subject, detail, service_id=service_id,
                                     rule_id=rule_id, proposal_id=proposal_id)
        except Exception:
            log.exception("could not write the reduce-operations audit row (%s)", action)

    def _schedule(self, delay: float) -> None:
        loop = asyncio.get_running_loop()
        handle: asyncio.TimerHandle

        def fire() -> None:
            self._timers.discard(handle)
            task = loop.create_task(self.flush_due())
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

        handle = loop.call_later(delay, fire)
        self._timers.add(handle)
