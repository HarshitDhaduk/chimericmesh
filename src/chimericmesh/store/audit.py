"""Append-only audit log. No update or delete operation exists here or in SQL (triggers)."""
from __future__ import annotations

import re
import weakref
from typing import TYPE_CHECKING, Any

from .tx import Tx

if TYPE_CHECKING:
    from .store import Store

MAX_SHORT = 100
MAX_DETAIL = 1000
DEFAULT_LIMIT = 100
MAX_LIMIT = 500

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_USERINFO = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*://)[^/\s@]+@")

_INSERT = (
    "INSERT INTO audit_log (ts, actor, action, subject, detail, service_id, rule_id, proposal_id) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)
_SELECT = (
    "SELECT id, ts, actor, action, subject, detail, service_id, rule_id, proposal_id FROM audit_log "
    "WHERE (:before IS NULL OR id < :before) AND (:svc IS NULL OR service_id = :svc) "
    "AND (:rule IS NULL OR rule_id = :rule) AND (:prop IS NULL OR proposal_id = :prop) "
    "ORDER BY id DESC LIMIT :limit"
)


class AuditLog:
    """Writes and reads audit rows. Text is sanitised before it reaches SQL."""

    def __init__(self, store: "Store") -> None:
        self._store = weakref.ref(store)
        self._secrets: list[str] = []

    def _add_secret(self, secret: str) -> None:
        if secret and secret not in self._secrets:
            self._secrets.append(secret)

    def _clean(self, text: object, limit: int) -> str:
        out = str(text).encode("utf-8", "replace").decode("utf-8")
        out = _CONTROL.sub("", out)
        for secret in sorted(self._secrets, key=len, reverse=True):
            out = out.replace(secret, "[redacted]")
        out = _USERINFO.sub(r"\1***@", out)
        return out[:limit]

    def _ref(self, value: str | None) -> str | None:
        return None if value is None else self._clean(value, MAX_SHORT)

    def append_tx(self, tx: Tx, actor: str, action: str, subject: str, detail: str = "", *,
                  service_id: str | None = None, rule_id: str | None = None,
                  proposal_id: str | None = None) -> int:
        store = self._store()
        ts = store.now() if store is not None else 0.0
        cur = tx.execute(_INSERT, (
            ts,
            self._clean(actor, MAX_SHORT), self._clean(action, MAX_SHORT),
            self._clean(subject, MAX_SHORT), self._clean(detail, MAX_DETAIL),
            self._ref(service_id), self._ref(rule_id), self._ref(proposal_id),
        ))
        return int(cur.lastrowid)

    async def append(self, actor: str, action: str, subject: str, detail: str = "", *,
                     service_id: str | None = None, rule_id: str | None = None,
                     proposal_id: str | None = None) -> int:
        store = self._store()
        if store is None:
            raise RuntimeError("audit log used after its store was dropped")
        return await store.transact(lambda tx: self.append_tx(
            tx, actor, action, subject, detail,
            service_id=service_id, rule_id=rule_id, proposal_id=proposal_id))

    async def read(self, limit: int = DEFAULT_LIMIT, before: int | None = None, *,
                   service_id: str | None = None, rule_id: str | None = None,
                   proposal_id: str | None = None) -> list[dict[str, Any]]:
        store = self._store()
        if store is None:
            raise RuntimeError("audit log used after its store was dropped")
        params = {
            "before": before, "svc": service_id, "rule": rule_id, "prop": proposal_id,
            "limit": max(1, min(int(limit), MAX_LIMIT)),
        }
        rows = await store.read(lambda tx: [dict(r) for r in tx.all(_SELECT, params)])  # type: ignore[arg-type]
        return rows
