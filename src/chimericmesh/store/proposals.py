"""Proposal state machine and persistence. The transition table is enforced here, inside the
transaction that reads the current status, so no code path can skip it."""
from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from typing import Any

from .errors import InvalidTransition, NotFound
from .models import Proposal
from .tx import Tx, format_id

MAX_PROPOSALS = 200  # in-memory hub only; a store-backed hub keeps every proposal
MAX_ACTION = 200
MAX_DETAILS = 500
MAX_NOTE = 300
MAX_DECIDED_BY = 60

PROPOSAL_STATUSES = ("pending", "approved", "denied", "done")

_PLAIN_TRANSITIONS = frozenset({("pending", "approved"), ("pending", "denied"), ("approved", "done")})
_RULE_DENIAL = re.compile(r"^rule-(revoked|expired|suspended):\S+$")
_RESERVED_PREFIXES = ("rule:", "rule-", "withdrawn:", "expired:", "expired-")


class HubAuthority:
    """Capability passed by hub code only. REST, IRC and MCP layers never construct it."""

    __slots__ = ()


def is_reserved_decider(by: str) -> bool:
    """True when ``by`` starts with a prefix only the hub's own rule logic may use."""
    return str(by).lstrip().lower().startswith(_RESERVED_PREFIXES)


def check_transition(old: str, new: str, *, origin: str, decided_by: str,
                     authority: HubAuthority | None) -> None:
    """Raise ``InvalidTransition`` unless ``old -> new`` is allowed for this caller."""
    if (old, new) in _PLAIN_TRANSITIONS:
        return
    if (old, new) == ("approved", "denied"):
        if authority is not None and origin == "hub" and _RULE_DENIAL.fullmatch(decided_by or ""):
            return
        raise InvalidTransition("approved -> denied is reserved for the hub's rule logic")
    raise InvalidTransition(f"{old} -> {new} is not allowed")


_HUB_PENDING_DENIAL = ("withdrawn:", "expired:")


def check_hub_transition(old: str, new: str, *, decided_by: str, approved_by_rule: str | None) -> None:
    """The only transitions the hub's own rule logic may make (O2); anything else raises."""
    if (old, new) == ("pending", "approved") and approved_by_rule:
        return
    if (old, new) == ("pending", "denied") and decided_by.startswith(_HUB_PENDING_DENIAL):
        return
    if (old, new) == ("approved", "denied") and _RULE_DENIAL.fullmatch(decided_by or ""):
        return
    raise InvalidTransition(f"hub transition {old} -> {new} with this decided_by is not allowed")


# ---- persistence ---------------------------------------------------------------------------

UNASSIGNED = "unassigned"

EXTRA_COLUMNS = ("kind", "files", "estimated_lines", "verification", "files_changed", "lines_changed",
                 "verification_result")
_JSON_COLUMNS = ("files", "files_changed")

_MAX_SEQ = "SELECT COALESCE(MAX(seq), 0) FROM proposals"
_INSERT = (
    "INSERT INTO proposals (id, seq, incident_id, action, details, status, created, decided_by, "
    "decided_at, result_note, decision_origin, service_id, incident_opening, approved_by_rule, kind, "
    "files, estimated_lines, verification, files_changed, lines_changed, verification_result) "
    "VALUES (:id, :seq, :incident_id, :action, :details, :status, :created, :decided_by, :decided_at, "
    ":result_note, :decision_origin, :service_id, :incident_opening, :approved_by_rule, :kind, :files, "
    ":estimated_lines, :verification, :files_changed, :lines_changed, :verification_result)"
)
_UPDATE = (
    "UPDATE proposals SET status = :status, decided_by = :decided_by, decided_at = :decided_at, "
    "result_note = :result_note, decision_origin = :decision_origin, approved_by_rule = :approved_by_rule, "
    "kind = :kind, files = :files, estimated_lines = :estimated_lines, verification = :verification, "
    "files_changed = :files_changed, lines_changed = :lines_changed, "
    "verification_result = :verification_result WHERE id = :id"
)
_COLUMNS = (
    "SELECT id, incident_id, action, details, status, created, decided_by, decided_at, result_note, "
    "decision_origin, service_id, incident_opening, approved_by_rule, kind, files, estimated_lines, "
    "verification, files_changed, lines_changed, verification_result FROM proposals"
)
_GET = _COLUMNS + " WHERE id = ?"
_LOAD_ALL = _COLUMNS + " ORDER BY seq"
_LIST = (
    _COLUMNS + " WHERE (:status IS NULL OR status = :status) AND (:svc IS NULL OR service_id = :svc) "
    "ORDER BY seq DESC LIMIT :limit"
)
_LIST_UNASSIGNED = (
    _COLUMNS + " WHERE (:status IS NULL OR status = :status) AND service_id IS NULL "
    "ORDER BY seq DESC LIMIT :limit"
)


def _text(value: object, limit: int) -> str:
    """Cut to ``limit`` and replace lone surrogates, which sqlite3 cannot bind."""
    return str(value).encode("utf-8", "replace").decode("utf-8")[:limit]


def _opt_text(value: object | None, limit: int) -> str | None:
    return None if value is None else _text(value, limit)


def _extras(extra: Mapping[str, object] | None) -> dict[str, Any]:
    """Validate and encode extended columns. An unknown key raises before anything changes."""
    out: dict[str, Any] = {name: None for name in EXTRA_COLUMNS}
    for key, value in (extra or {}).items():
        if key not in EXTRA_COLUMNS:
            raise ValueError(f"unknown proposal field: {key!r}")
        out[key] = value
    return out


def validate_extra(extra: Mapping[str, object] | None) -> dict[str, Any]:
    """Public form of the extras check: an unknown key raises ``ValueError``."""
    return _extras(extra)


def _encode(prop: Proposal) -> dict[str, Any]:
    row = prop.to_full_dict()
    for name in _JSON_COLUMNS:
        if row[name] is not None:
            row[name] = json.dumps([_text(f, 500) for f in row[name]])
    for name in ("kind", "verification", "verification_result"):
        row[name] = _opt_text(row[name], 500)
    return row


def _decode(row: sqlite3.Row) -> Proposal:
    data = dict(row)
    for name in _JSON_COLUMNS:
        if data[name] is not None:
            data[name] = json.loads(data[name])
    return Proposal(**data)


def create_proposal(tx: Tx, *, incident_id: str, action: str, details: str, service_id: str | None,
                    incident_opening: int | None, now: float,
                    extra: Mapping[str, object] | None = None) -> Proposal:
    """Insert a pending proposal. The id comes from the durable counter in this transaction."""
    fields = _extras(extra)
    highest = int(tx.one(_MAX_SEQ)[0])
    seq = tx.counter_next("proposal", floor=highest)
    prop = Proposal(
        id=format_id("p", seq), incident_id=_text(incident_id, 64), action=_text(action, MAX_ACTION),
        details=_text(details, MAX_DETAILS), created=float(now),
        service_id=_opt_text(service_id, 64), incident_opening=incident_opening, **fields,
    )
    tx.execute(_INSERT, {**_encode(prop), "seq": seq})
    return prop


def get_proposal(tx: Tx, pid: str) -> Proposal | None:
    row = tx.one(_GET, (pid,))
    return None if row is None else _decode(row)


def transition_proposal(tx: Tx, pid: str, new_status: str, *, decided_by: str, origin: str,
                        authority: HubAuthority | None, now: float,
                        approved_by_rule: str | None = None, result_note: str | None = None,
                        extra: Mapping[str, object] | None = None) -> Proposal:
    """Move a proposal to ``new_status`` inside the caller's transaction.

    The current status is read in this transaction, so two concurrent decisions cannot both win.
    ``origin`` is ``hub`` only when ``authority`` is given; the argument cannot raise it otherwise.
    """
    fields = _extras(extra)
    prop = get_proposal(tx, pid)
    if prop is None:
        raise NotFound(f"proposal {pid} not found")
    effective_origin = "hub" if authority is not None else "user"
    check_transition(prop.status, new_status, origin=effective_origin, decided_by=decided_by,
                     authority=authority)
    if approved_by_rule is not None and authority is None:
        raise InvalidTransition("approved_by_rule can only be set by the hub's rule logic")
    prop.status = new_status
    if new_status != "done":
        prop.decided_by = _text(decided_by, MAX_DECIDED_BY)
        prop.decided_at = float(now)
        prop.decision_origin = effective_origin
        if approved_by_rule is not None:
            prop.approved_by_rule = _text(approved_by_rule, 100)
    if result_note is not None:
        prop.result_note = _text(result_note, MAX_NOTE)
    for key, value in fields.items():
        if value is not None:
            setattr(prop, key, value)
    tx.execute(_UPDATE, _encode(prop))
    return prop


def list_proposals(tx: Tx, *, status: str | None = None, service_id: str | None = None,
                   limit: int | None = None) -> list[Proposal]:
    """Newest ``limit`` matching proposals, oldest first. Filters apply before the limit."""
    params = {"status": status, "svc": service_id, "limit": -1 if limit is None else max(0, int(limit))}
    if service_id == UNASSIGNED:
        rows = tx.all(_LIST_UNASSIGNED, params)  # type: ignore[arg-type]
    else:
        rows = tx.all(_LIST, params)  # type: ignore[arg-type]
    return [_decode(r) for r in reversed(rows)]


def load_all_proposals(tx: Tx) -> list[Proposal]:
    return [_decode(r) for r in tx.all(_LOAD_ALL)]