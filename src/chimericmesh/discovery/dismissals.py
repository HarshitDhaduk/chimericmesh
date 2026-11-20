"""Dismissed suggestions: one small table (R5.4). Every SQL statement is a string literal with ``?`` parameters.
A dismissal and its audit row commit together; a repeat dismissal changes nothing and writes no audit row."""
from __future__ import annotations

from chimericmesh.store import Store, Tx

from .models import MAX_DISMISSALS

_INSERT = "INSERT OR IGNORE INTO discovery_dismissals (app_id, fingerprint, dismissed_at) VALUES (?, ?, ?)"
_TRIM = ("DELETE FROM discovery_dismissals WHERE rowid IN (SELECT rowid FROM discovery_dismissals "
         "ORDER BY dismissed_at DESC, rowid DESC LIMIT -1 OFFSET ?)")      # keep the newest MAX_DISMISSALS
_SELECT_PAIRS = "SELECT app_id, fingerprint FROM discovery_dismissals"
_SELECT_LIST = "SELECT app_id, dismissed_at FROM discovery_dismissals ORDER BY dismissed_at DESC, rowid DESC"
_COUNT = "SELECT COUNT(*) FROM discovery_dismissals"
_DELETE_ALL = "DELETE FROM discovery_dismissals"


async def dismiss(store: Store, app_id: str, fingerprint: str, *, actor: str, paths: int) -> bool:
    """``True`` when a new row was written, ``False`` for a repeat."""
    def work(tx: Tx) -> bool:
        cur = tx.execute(_INSERT, (app_id, fingerprint, float(store.now())))
        if cur.rowcount != 1:
            return False
        tx.execute(_TRIM, (MAX_DISMISSALS,))
        store.audit.append_tx(tx, actor, "discovery_dismissed", app_id, f"paths={int(paths)}")
        return True
    return await store.transact(work)


async def load_fingerprints(store: Store) -> frozenset[tuple[str, str]]:
    rows = await store.read(lambda tx: [(r["app_id"], r["fingerprint"]) for r in tx.all(_SELECT_PAIRS)])
    return frozenset(rows)


async def list_dismissals(store: Store) -> list[dict]:
    return await store.read(lambda tx: [{"app_id": r["app_id"], "dismissed_at": float(r["dismissed_at"])}
                                        for r in tx.all(_SELECT_LIST)])


async def clear(store: Store, *, actor: str) -> int:
    def work(tx: Tx) -> int:
        count = int(tx.one(_COUNT)[0])
        tx.execute(_DELETE_ALL)
        store.audit.append_tx(tx, actor, "discovery_dismissals_cleared", "all", f"count={count}")
        return count
    return await store.transact(work)
