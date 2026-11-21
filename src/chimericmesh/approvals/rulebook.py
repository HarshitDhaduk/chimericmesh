"""The rule mirror (loop thread) and the literal-SQL helpers that take a ``Tx`` (writer thread).

All SQL here is a module-level constant; values go in as parameters (the store's SQL rule).
"""
from __future__ import annotations

import dataclasses
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

from chimericmesh.store import Store, Tx
from chimericmesh.store.tx import format_id

from .errors import ApprovalsNotMigrated
from .models import Rule

# active < suspended < revoked = expired: a merge may only move up this ladder.
RANK = {"active": 0, "suspended": 1, "revoked": 2, "expired": 2}


@dataclass(frozen=True, slots=True)
class Snapshot:
    """What one reload read from the database."""
    rules: tuple[Rule, ...]
    paused: bool
    paused_by: str
    paused_at: float | None


class RuleBook:
    """Mirror of the rules table plus the ordering state of "Rule state ordering" (review H2)."""

    def __init__(self) -> None:
        self._rules: dict[str, Rule] = {}
        self._version: dict[str, int] = {}
        self._changed_at: dict[str, int] = {}
        self.epoch = 0
        self.dirty: set[str] = set()
        self.paused = False
        self.paused_by = ""
        self.paused_at: float | None = None
        self.pause_version = 0
        self._pause_changed_at = 0
        self.paused_dirty = False

    # ---- reads ----------------------------------------------------------------------------------
    def rules(self, *, service_id: str | None = None, status: str | None = None) -> list[Rule]:
        found = [r for r in self._rules.values()
                 if (service_id is None or r.service_id == service_id) and (status is None or r.status == status)]
        return sorted(found, key=lambda r: r.seq)

    def get(self, rule_id: str) -> Rule | None:
        return self._rules.get(rule_id)

    def version(self, rule_id: str) -> int:
        return self._version.get(rule_id, 0)

    def __len__(self) -> int:
        return len(self._rules)

    # ---- changes ---------------------------------------------------------------------------------
    def load(self, rules: Iterable[Rule], *, paused: bool = False, paused_by: str = "",
             paused_at: float | None = None) -> None:
        self._rules = {r.id: r for r in sorted(rules, key=lambda r: r.seq)}
        self._version = {rid: 1 for rid in self._rules}
        self._changed_at = {rid: 0 for rid in self._rules}
        self.paused, self.paused_by, self.paused_at = paused, paused_by, paused_at

    def replace(self, rule: Rule) -> None:
        """Swap a whole rule object. Bumps that rule's version and the epoch."""
        self.epoch += 1
        self._rules[rule.id] = rule
        self._version[rule.id] = self._version.get(rule.id, 0) + 1
        self._changed_at[rule.id] = self.epoch

    def set_paused(self, paused: bool, by: str, at: float | None) -> None:
        self.epoch += 1
        self.paused, self.paused_by, self.paused_at = paused, by, at
        self.pause_version += 1
        self._pause_changed_at = self.epoch

    def mark_dirty(self, rule_id: str) -> None:
        self.dirty.add(rule_id)

    def clear_dirty(self, rule_id: str) -> None:
        self.dirty.discard(rule_id)

    def mark_paused_dirty(self) -> None:
        self.paused_dirty = True

    def clear_paused_dirty(self) -> None:
        self.paused_dirty = False

    def merge(self, snapshot: Snapshot, started_epoch: int) -> list[str]:
        """Tighten-only merge of a reload (rule 5). Returns the ids whose mirror entry changed.

        ``started_epoch`` is the epoch recorded before the read began. A rule that is dirty, or changed in
        the mirror after that, is kept whole; otherwise the stricter status of mirror and snapshot wins and
        the other fields come from the snapshot. The pause flag can go false to true, never back.
        """
        changed: list[str] = []
        for snap_rule in snapshot.rules:
            mine = self._rules.get(snap_rule.id)
            if mine is None:
                self._rules[snap_rule.id] = snap_rule
                self._version[snap_rule.id] = 1
                self._changed_at[snap_rule.id] = 0
                changed.append(snap_rule.id)
                continue
            if snap_rule.id in self.dirty or self._changed_at.get(snap_rule.id, 0) > started_epoch:
                continue
            if RANK[snap_rule.status] > RANK[mine.status]:
                merged = snap_rule
            else:
                merged = dataclasses.replace(snap_rule, status=mine.status, status_reason=mine.status_reason)
            if merged != mine:
                self._rules[mine.id] = merged
                self._version[mine.id] = self._version.get(mine.id, 0) + 1
                self.epoch += 1
                self._changed_at[mine.id] = self.epoch
                changed.append(mine.id)
        if not (self.paused_dirty or self._pause_changed_at > started_epoch):
            if snapshot.paused and not self.paused:
                self.paused, self.paused_by, self.paused_at = True, snapshot.paused_by, snapshot.paused_at
                self.pause_version += 1
                self.epoch += 1
                self._pause_changed_at = self.epoch
        return changed


# ---- SQL helpers (writer or reader thread, ``tx`` only) --------------------------------------------------

_HAS_TABLE = "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'approval_rules'"
_MAX_SEQ = "SELECT COALESCE(MAX(seq), 0) FROM approval_rules"
_COUNT_SERVICE = ("SELECT COUNT(*) FROM approval_rules WHERE service_id = ? AND status IN ('active', 'suspended')")
_COUNT_TOTAL = "SELECT COUNT(*) FROM approval_rules WHERE status IN ('active', 'suspended')"
_SELECT_RULES = (
    "SELECT id, seq, service_id, match_type, match_value, most_urgent_severity, expires_at, max_per_hour, "
    "max_per_incident, max_files, max_lines, status, status_reason, created_by, created_at, updated_at, "
    "source_proposal_id, note, use_count, code_folder FROM approval_rules ORDER BY seq"
)
_SELECT_ONE_RULE = (
    "SELECT id, seq, service_id, match_type, match_value, most_urgent_severity, expires_at, max_per_hour, "
    "max_per_incident, max_files, max_lines, status, status_reason, created_by, created_at, updated_at, "
    "source_proposal_id, note, use_count, code_folder FROM approval_rules WHERE id = ?"
)
_INSERT_RULE = (
    "INSERT INTO approval_rules (id, seq, service_id, match_type, match_value, most_urgent_severity, expires_at, "
    "max_per_hour, max_per_incident, max_files, max_lines, status, status_reason, created_by, created_at, "
    "updated_at, source_proposal_id, note, use_count, code_folder) VALUES (:id, :seq, :service_id, :match_type, "
    ":match_value, :most_urgent_severity, :expires_at, :max_per_hour, :max_per_incident, :max_files, :max_lines, "
    ":status, :status_reason, :created_by, :created_at, :updated_at, :source_proposal_id, :note, :use_count, "
    ":code_folder)"
)
_REVOKE = ("UPDATE approval_rules SET status = 'revoked', status_reason = ?, updated_at = ? "
           "WHERE id = ? AND status IN ('active', 'suspended')")
_SUSPEND = ("UPDATE approval_rules SET status = 'suspended', status_reason = ?, updated_at = ? "
            "WHERE id = ? AND status = 'active'")
_EXPIRE = ("UPDATE approval_rules SET status = 'expired', status_reason = 'expired', updated_at = ? "
           "WHERE id = ? AND status IN ('active', 'suspended')")
_ENABLE = ("UPDATE approval_rules SET status = 'active', status_reason = '', updated_at = ? "
           "WHERE id = ? AND status = 'suspended'")
_PATCH = (
    "UPDATE approval_rules SET most_urgent_severity = ?, expires_at = ?, max_per_hour = ?, max_per_incident = ?, "
    "max_files = ?, max_lines = ?, note = ?, updated_at = ? WHERE id = ? AND status IN ('active', 'suspended')"
)
_SELECT_ACTIVE_OF_SERVICE = "SELECT id FROM approval_rules WHERE service_id = ? AND status = 'active' ORDER BY seq"
_SELECT_LIVE_OF_SERVICE = (
    "SELECT id, status FROM approval_rules WHERE service_id = ? AND status IN ('active', 'suspended') ORDER BY seq")
_SUSPEND_SERVICE = (
    "UPDATE approval_rules SET status = 'suspended', status_reason = ?, updated_at = ? "
    "WHERE service_id = ? AND status IN ('active', 'suspended')"
)
_BUMP_USE = "UPDATE approval_rules SET use_count = use_count + 1 WHERE id = ?"
_SELECT_STATE = "SELECT key, value FROM approval_state"
_SET_STATE = "INSERT INTO approval_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value"
_PAUSE_STATE = "UPDATE approval_state SET value = ? WHERE key = 'paused' AND value = ?"
_INSERT_USE = (
    "INSERT INTO rule_uses (rule_id, proposal_id, incident_id, opening, service_id, approved_at) "
    "VALUES (?, ?, ?, ?, ?, ?)"
)
_COMPLETE_USE = "UPDATE rule_uses SET completed_at = ? WHERE proposal_id = ? AND completed_at IS NULL"
_SELECT_USES = ("SELECT rule_id, proposal_id, incident_id, opening, service_id, approved_at, completed_at "
                "FROM rule_uses WHERE approved_at >= ? ORDER BY id")
_INSERT_OPENING = "INSERT OR IGNORE INTO incident_openings (incident_id, opening, first_seen_at) VALUES (?, ?, ?)"
_SELECT_OPENINGS = "SELECT incident_id, opening, first_seen_at FROM incident_openings WHERE first_seen_at >= ?"
_PRUNE_USES = "DELETE FROM rule_uses WHERE approved_at < ?"
_PRUNE_OPENINGS = "DELETE FROM incident_openings WHERE first_seen_at < ?"
_UNCOMPLETED_USES = ("SELECT u.proposal_id FROM rule_uses u JOIN proposals p ON p.id = u.proposal_id "
                     "WHERE u.completed_at IS NULL AND p.status = 'done'")


def ensure_migrated(store: Store) -> None:
    """Raise ``ApprovalsNotMigrated`` when the store was opened without the approvals migrations."""
    found = store.read_sync(lambda tx: tx.one(_HAS_TABLE), allow_blocking=True)
    if found is None:
        raise ApprovalsNotMigrated("open the store with approvals.register_migrations")


def row_to_rule(row: sqlite3.Row) -> Rule:
    return Rule(
        id=row["id"], seq=int(row["seq"]), service_id=row["service_id"], match_type=row["match_type"],
        match_value=row["match_value"], most_urgent_severity=int(row["most_urgent_severity"]),
        expires_at=float(row["expires_at"]), max_per_hour=int(row["max_per_hour"]),
        max_per_incident=int(row["max_per_incident"]), max_files=int(row["max_files"]),
        max_lines=int(row["max_lines"]), status=row["status"], status_reason=row["status_reason"],
        created_by=row["created_by"], created_at=float(row["created_at"]), updated_at=float(row["updated_at"]),
        source_proposal_id=row["source_proposal_id"], note=row["note"], use_count=int(row["use_count"]),
        code_folder=row["code_folder"],
    )


def max_rule_seq(tx: Tx) -> int:
    return int(tx.one(_MAX_SEQ)[0])


def issue_rule_id(tx: Tx) -> tuple[int, str]:
    """The next rule number and id, from the durable counter in this transaction (store R3.7)."""
    seq = tx.counter_next("rule", floor=max_rule_seq(tx))
    return seq, format_id("r", seq)


def count_live_rules(tx: Tx, service_id: str) -> tuple[int, int]:
    """(rules of the service, rules in total) that are active or suspended."""
    return int(tx.one(_COUNT_SERVICE, (service_id,))[0]), int(tx.one(_COUNT_TOTAL)[0])


def insert_rule(tx: Tx, rule: Rule) -> None:
    tx.execute(_INSERT_RULE, dataclasses.asdict(rule))


def load_rules(tx: Tx) -> list[Rule]:
    return [row_to_rule(r) for r in tx.all(_SELECT_RULES)]


def load_rule(tx: Tx, rule_id: str) -> Rule | None:
    row = tx.one(_SELECT_ONE_RULE, (rule_id,))
    return None if row is None else row_to_rule(row)


def load_state(tx: Tx) -> tuple[bool, str, float | None]:
    state = {r["key"]: r["value"] for r in tx.all(_SELECT_STATE)}
    try:
        at = float(state["paused_at"]) if "paused_at" in state else None
    except ValueError:
        at = None
    return state.get("paused") == "1", state.get("paused_by", ""), at


def revoke_row(tx: Tx, rule_id: str, reason: str, now: float) -> int:
    return tx.execute(_REVOKE, (reason, now, rule_id)).rowcount


def suspend_row(tx: Tx, rule_id: str, reason: str, now: float) -> int:
    return tx.execute(_SUSPEND, (reason, now, rule_id)).rowcount


def expire_row(tx: Tx, rule_id: str, now: float) -> int:
    return tx.execute(_EXPIRE, (now, rule_id)).rowcount


def enable_row(tx: Tx, rule_id: str, now: float) -> int:
    return tx.execute(_ENABLE, (now, rule_id)).rowcount


def patch_row(tx: Tx, rule: Rule, now: float) -> int:
    return tx.execute(_PATCH, (rule.most_urgent_severity, rule.expires_at, rule.max_per_hour, rule.max_per_incident,
                               rule.max_files, rule.max_lines, rule.note, now, rule.id)).rowcount


def active_rule_ids(tx: Tx, service_id: str) -> list[str]:
    return [r[0] for r in tx.all(_SELECT_ACTIVE_OF_SERVICE, (service_id,))]


def live_rule_ids(tx: Tx, service_id: str) -> list[tuple[str, str]]:
    return [(r[0], r[1]) for r in tx.all(_SELECT_LIVE_OF_SERVICE, (service_id,))]


def suspend_service_rules(tx: Tx, service_id: str, reason: str, now: float) -> int:
    return tx.execute(_SUSPEND_SERVICE, (reason, now, service_id)).rowcount


def bump_use_count(tx: Tx, rule_id: str) -> None:
    tx.execute(_BUMP_USE, (rule_id,))


def set_state(tx: Tx, key: str, value: str) -> None:
    tx.execute(_SET_STATE, (key, value))


def set_paused_row(tx: Tx, paused: bool, by: str, at: float) -> None:
    set_state(tx, "paused", "1" if paused else "0")
    set_state(tx, "paused_by", by[:100])
    set_state(tx, "paused_at", repr(float(at)))


def resume_row(tx: Tx) -> int:
    """Write ``paused = 0`` only when it is stored as ``1`` (conditional widen)."""
    return tx.execute(_PAUSE_STATE, ("0", "1")).rowcount


def insert_use(tx: Tx, *, rule_id: str, proposal_id: str, incident_id: str, opening: int, service_id: str | None,
               approved_at: float) -> None:
    tx.execute(_INSERT_USE, (rule_id, proposal_id, incident_id, opening, service_id, approved_at))


def complete_use(tx: Tx, proposal_id: str, now: float) -> int:
    return tx.execute(_COMPLETE_USE, (now, proposal_id)).rowcount


def load_uses(tx: Tx, since: float) -> list[sqlite3.Row]:
    return tx.all(_SELECT_USES, (since,))


def load_openings(tx: Tx, since: float) -> list[sqlite3.Row]:
    return tx.all(_SELECT_OPENINGS, (since,))


def insert_opening(tx: Tx, incident_id: str, opening: int, first_seen_at: float) -> None:
    tx.execute(_INSERT_OPENING, (incident_id, opening, first_seen_at))


def prune_rows(tx: Tx, cutoff: float) -> None:
    tx.execute(_PRUNE_USES, (cutoff,))
    tx.execute(_PRUNE_OPENINGS, (cutoff,))


def done_without_completion(tx: Tx) -> list[str]:
    return [r[0] for r in tx.all(_UNCOMPLETED_USES)]

