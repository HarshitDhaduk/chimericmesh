"""One task per log source: poll the file, parse lines, submit events, keep counters."""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass

from ..parsers.formats import get_format
from ..parsers.logctx import FormatContext, LineFormat, make_file_event
from ..services.models import LogSource, Service
from .config import ConnectorConfig
from .limiter import DropNotice, TokenBucket
from .submit import Submit
from .tailread import PollResult, RawLine, TailState, decode_line, header_fields, poll_file

log = logging.getLogger("chimericmesh.connectors")

LAST_ERROR_MAX = 200
WARN_WINDOW = 10.0
NOT_REGULAR = "not a regular file"
_HEADER_REASONS = frozenset({"attached", "resumed", "appeared", "rotated", "truncated"})
_NEW_FILE_REASONS = frozenset({"rotated", "truncated"})


@dataclass(frozen=True, slots=True)
class SourceSpec:
    source_id: str
    service_id: str
    path: str
    format: str
    host: str | None
    app: str
    backfill_lines: int        # 0 to 1000


@dataclass(slots=True)
class SourceCounters:
    lines_read: int = 0
    events_emitted: int = 0
    lines_dropped: int = 0
    bytes_read: int = 0
    unparsed: int = 0
    skipped: int = 0
    last_error: str = ""
    status: str = "stopped"


def path_key(path: str) -> str:
    """Two spellings of one path compare equal (case on Windows, redundant separators)."""
    return os.path.normcase(os.path.normpath(path))


def build_spec(src: LogSource, service: Service | None, machine: str) -> SourceSpec:
    """The tailer settings of a registered source. ``machine`` is accepted for symmetry with the tailer."""
    app = ""
    if service is not None and service.app_names:
        app = service.app_names[0]
    if not app:
        app = os.path.splitext(os.path.basename(src.path))[0] or "file"
    backfill = max(0, min(1000, int(src.backfill_lines)))
    return SourceSpec(src.id, src.service_id, src.path, src.format, src.host, app, backfill)


class FileTailer:
    def __init__(self, spec: SourceSpec, state: TailState, counters: SourceCounters, submit: Submit,
                 cfg: ConnectorConfig, *, machine: str, clock: Callable[[], float] = time.monotonic,
                 on_state: Callable[[str, TailState], None], fmt: LineFormat | None = None) -> None:
        self.spec = spec
        self.state = state
        self.counters = counters
        self._submit = submit
        self._cfg = cfg
        self._clock = clock
        self._on_state = on_state
        self._fmt = fmt if fmt is not None else get_format(spec.format)
        self._ctx = FormatContext(machine=machine, service_id=spec.service_id, source_host=spec.host,
                                  app=spec.app)
        self._bucket = TokenBucket(cfg.rate, cfg.burst, clock())
        self._notice = DropNotice(cfg.drop_notice_interval)
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._busy = 0
        self._last_warn = float("-inf")

    # ---- lifecycle ---------------------------------------------------------------------------------

    def start(self) -> asyncio.Task:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run(), name=f"tailer-{self.spec.source_id}")
        return self._task

    @property
    def task(self) -> asyncio.Task | None:
        return self._task

    async def stop(self, graceful_timeout: float = 2.0) -> None:
        """Ask the loop to stop, wait for it, then cancel. No ``ingest`` happens after this returns."""
        self._stop.set()
        task = self._task
        if task is not None and not task.done():
            done, _ = await asyncio.wait({task}, timeout=graceful_timeout)
            if not done:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.counters.status = "stopped"

    # ---- the loop ----------------------------------------------------------------------------------

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                delay = await self._cycle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # a bug or a failing executor: never end the task
                self.counters.status = "error"
                self.counters.last_error = f"{type(exc).__name__}: {exc}"[:LAST_ERROR_MAX]
                self._warn("tailer %s failed: %s", self.spec.source_id, exc)
                delay = self._cfg.error_retry
            await self._sleep(delay)

    async def _sleep(self, delay: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), delay)
        except asyncio.TimeoutError:
            pass

    def _warn(self, msg: str, *args: object) -> None:
        now = self._clock()
        if now - self._last_warn >= WARN_WINDOW:
            self._last_warn = now
            log.warning(msg, *args)

    async def _cycle(self) -> float:
        cfg = self._cfg
        spec = self.spec
        result = await asyncio.to_thread(poll_file, spec.path, self.state, max_read=cfg.max_read,
                                         max_line=cfg.max_line, backfill_lines=spec.backfill_lines,
                                         head_size=cfg.head_size)
        if self._stop.is_set():
            return 0.0
        delay = self._apply_status(result)
        self.counters.bytes_read += result.bytes_read
        self.state = result.start
        await self._refresh_header(result)
        await self._process(result)
        if not self._stop.is_set():
            self.state = result.state
            self._on_state(spec.source_id, self.state)
        await self._send_notice()
        if result.more and result.status == "running":
            return cfg.catchup_interval
        return delay

    def _apply_status(self, result: PollResult) -> float:
        cfg = self._cfg
        c = self.counters
        error = result.error[:LAST_ERROR_MAX]
        if result.status == "busy":
            self._busy += 1
            if self._busy <= cfg.busy_polls:
                c.status, c.last_error = "running", error
                return cfg.poll_interval
            c.status, c.last_error = "unreadable", error
            return cfg.unreadable_retry
        self._busy = 0
        c.status, c.last_error = result.status, error
        if result.status == "running":
            return cfg.poll_interval
        if result.status == "missing":
            return cfg.missing_retry
        if result.status == "unreadable":
            return cfg.unreadable_retry
        return cfg.not_regular_retry if result.error == NOT_REGULAR else cfg.error_retry

    async def _refresh_header(self, result: PollResult) -> None:
        if self._fmt.name != "w3c" or result.status not in ("running",):
            return
        new_file = result.reason in _NEW_FILE_REASONS
        if new_file:
            self._ctx = dataclasses.replace(self._ctx, w3c_fields=None)
        if result.reason in _HEADER_REASONS or (self._ctx.w3c_fields is None and result.lines):
            header = await asyncio.to_thread(header_fields, self.spec.path)
            if header:
                self._ctx = self._fmt.update(header, self._ctx)

    async def _process(self, result: PollResult) -> None:
        c = self.counters
        sid = self.spec.source_id
        admitted = self._bucket.take(len(result.lines), self._clock())
        for i, raw in enumerate(result.lines):
            if self._stop.is_set():
                break                                   # the rest stays in the file
            if i >= admitted:
                c.lines_read += 1
                c.lines_dropped += 1
                self._notice.add(1)
            else:
                await self._one(raw)
            self.state = dataclasses.replace(self.state, offset=raw.end, discarding=raw.discarding_after)
            self._on_state(sid, self.state)

    async def _one(self, raw: RawLine) -> None:
        c = self.counters
        try:
            text = decode_line(raw)
            self._ctx = self._fmt.update(text, self._ctx)
            out = self._fmt.parse_line(text, self._ctx)
            if out.event is None:
                c.lines_read += 1
                c.skipped += 1
                return
            await self._submit(out.event, self.spec.source_id)
            c.lines_read += 1
            c.events_emitted += 1
            if not out.matched:
                c.unparsed += 1
        except Exception as exc:  # R1.3: one bad line never stops the tailer
            c.lines_read += 1
            c.lines_dropped += 1
            self._warn("source %s dropped a line: %s: %s", self.spec.source_id, type(exc).__name__, exc)

    async def _send_notice(self) -> None:
        n = self._notice.due(self._clock())
        if not n:
            return
        try:
            event = make_file_event(self._ctx, severity=4, message=f"connector dropped {n} lines (rate limit)",
                                    raw="")
            await self._submit(event, self.spec.source_id)
        except Exception as exc:
            self._warn("source %s drop notice failed: %s", self.spec.source_id, exc)
