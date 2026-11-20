"""Persisted read positions (``tail_offsets``): identity hash and numbers only, never file text."""
from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from ..store import Store, Tx
from .tailread import FileIdent, TailState

log = logging.getLogger("chimericmesh.connectors")

_HEX64 = re.compile(r"[0-9a-f]{64}")
_TABLE_EXISTS = "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tail_offsets'"
_SELECT = ("SELECT log_source_id, dev, ino, head_len, head_sha, byte_offset, discarding, updated_at "
           "FROM tail_offsets")
_UPSERT = (
    "INSERT INTO tail_offsets (log_source_id, dev, ino, head_len, head_sha, byte_offset, discarding, updated_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT (log_source_id) DO UPDATE SET dev = excluded.dev, ino = excluded.ino, "
    "head_len = excluded.head_len, head_sha = excluded.head_sha, byte_offset = excluded.byte_offset, "
    "discarding = excluded.discarding, updated_at = excluded.updated_at"
)
_DELETE = "DELETE FROM tail_offsets WHERE log_source_id = ?"
_IDS = "SELECT log_source_id FROM tail_offsets"


@dataclass(frozen=True, slots=True)
class OffsetRow:
    log_source_id: str
    dev: int
    ino: int
    head_len: int
    head_sha: str
    offset: int
    discarding: bool
    updated_at: float


def row_from_state(source_id: str, state: TailState, now: float) -> OffsetRow | None:
    ident = state.ident
    if ident is None:
        return None
    return OffsetRow(source_id, ident.dev, ident.ino, ident.head_len, ident.head_sha, state.offset,
                     state.discarding, now)


def resume_state(row: OffsetRow | None) -> TailState:
    if row is None:
        return TailState()
    return TailState(ident=FileIdent(row.dev, row.ino, row.head_len, row.head_sha), offset=row.offset,
                     discarding=row.discarding)


def _decimal(value: object) -> int | None:
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 20:
        return int(value)
    return None


def _parse_row(r: Sequence[object]) -> OffsetRow | None:
    sid, dev, ino, head_len, head_sha, offset, discarding, updated_at = r
    d, i = _decimal(dev), _decimal(ino)
    if (d is None or i is None or not isinstance(sid, str) or not isinstance(head_sha, str)
            or not _HEX64.fullmatch(head_sha) or not isinstance(head_len, int) or not 0 <= head_len <= 256
            or not isinstance(offset, int) or offset < 0):
        return None
    return OffsetRow(sid, d, i, head_len, head_sha, offset, bool(discarding), float(updated_at or 0.0))


def _has_table(tx: Tx) -> bool:
    return tx.one(_TABLE_EXISTS) is not None


class OffsetStore:
    def __init__(self, store: Store, now: Callable[[], float] = time.time) -> None:
        self._store = store
        self._now = now
        self._available: bool | None = None
        self._warned_missing = False

    async def available(self) -> bool:
        if self._available is None:
            self._available = bool(await self._store.read(_has_table))
        return self._available

    def _missing(self) -> None:
        if not self._warned_missing:
            self._warned_missing = True
            log.warning("the tail_offsets table is missing; file read positions are not persisted")

    async def load(self) -> dict[str, OffsetRow]:
        if not await self.available():
            return {}
        raw = await self._store.read(lambda tx: [tuple(r) for r in tx.all(_SELECT)])
        rows: dict[str, OffsetRow] = {}
        bad = 0
        for r in raw:
            parsed = _parse_row(r)
            if parsed is None:
                bad += 1
            else:
                rows[parsed.log_source_id] = parsed
        if bad:
            log.warning("skipped %d unreadable tail_offsets row(s); those sources start fresh", bad)
        return rows

    async def flush(self, rows: Sequence[OffsetRow]) -> None:
        """One transaction, an idempotent upsert. A ``StoreError`` propagates to the caller."""
        if not rows:
            return
        if not await self.available():
            self._missing()
            return
        params = [(r.log_source_id, str(r.dev), str(r.ino), r.head_len, r.head_sha, r.offset,
                   1 if r.discarding else 0, r.updated_at) for r in rows]
        await self._store.transact(lambda tx: tx.executemany(_UPSERT, params))

    async def delete(self, ids: Sequence[str]) -> None:
        """Delete the given rows (a disabled source). A `StoreError` propagates to the caller."""
        if not ids or not await self.available():
            return
        params = [(i,) for i in ids]
        await self._store.transact(lambda tx: tx.executemany(_DELETE, params))

    async def prune(self, keep: frozenset[str]) -> int:
        """Delete the rows whose id is not in ``keep``; return how many were deleted."""
        if not await self.available():
            return 0

        def work(tx: Tx) -> int:
            orphans = [(r[0],) for r in tx.all(_IDS) if r[0] not in keep]
            if orphans:
                tx.executemany(_DELETE, orphans)
            return len(orphans)

        return await self._store.transact(work)

    @staticmethod
    def drop_in_tx(tx: Tx, ids: Sequence[str]) -> None:
        """Delete the given rows inside the caller's transaction (in-transaction subscriber)."""
        if not ids or not _has_table(tx):
            return
        tx.executemany(_DELETE, [(i,) for i in ids])
