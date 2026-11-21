"""Usage ledger: the hour window, per-opening counts, completions and observed reopens (loop thread only).

The cooldown (R4.9, review H1) is one formula and nothing else:

    cooldown_for(incident, opening) returns the id of a use U of this incident with
    ``U.opening < opening`` and ``U.completed_at`` set, such that
    ``reopen_at(incident, opening) - U.completed_at < cooldown``.

``reopen_at`` is the first time the manager saw that opening and never changes, so the answer for a
given opening does not depend on when the question is asked.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .matching import RuleUsage

HOUR = 3600.0
RETENTION = 31 * 86400.0


@dataclass(frozen=True, slots=True)
class UsageRow:
    rule_id: str
    proposal_id: str
    incident_id: str
    opening: int
    service_id: str | None
    approved_at: float
    completed_at: float | None


@dataclass(frozen=True, slots=True)
class OpeningRow:
    incident_id: str
    opening: int
    first_seen_at: float


@dataclass(slots=True)
class _Use:
    token: int
    rule_id: str
    incident_id: str
    opening: int
    approved_at: float
    proposal_id: str | None = None
    completed_at: float | None = None


class UsageLedger:
    def __init__(self) -> None:
        self._uses: dict[int, _Use] = {}
        self._by_proposal: dict[str, int] = {}
        self._openings: dict[tuple[str, int], float] = {}
        self._next = 0

    def usage(self, rule_id: str, incident_id: str, opening: int, now: float) -> RuleUsage:
        hour = per_opening = 0
        for use in self._uses.values():
            if use.rule_id != rule_id:
                continue
            if now - use.approved_at < HOUR:
                hour += 1
            if use.incident_id == incident_id and use.opening == opening:
                per_opening += 1
        return RuleUsage(hour=hour, incident_opening=per_opening)

    def reserve(self, rule_id: str, incident_id: str, opening: int, now: float) -> int:
        self._next += 1
        self._uses[self._next] = _Use(self._next, rule_id, incident_id, opening, now)
        return self._next

    def release(self, token: int) -> None:
        use = self._uses.pop(token, None)
        if use is not None and use.proposal_id is not None:
            self._by_proposal.pop(use.proposal_id, None)

    def bind(self, token: int, proposal_id: str) -> None:
        use = self._uses.get(token)
        if use is not None:
            use.proposal_id = proposal_id
            self._by_proposal[proposal_id] = token

    def completed(self, proposal_id: str, now: float) -> None:
        token = self._by_proposal.get(proposal_id)
        if token is not None and token in self._uses:
            self._uses[token].completed_at = now

    def note_opening(self, incident_id: str, opening: int, now: float) -> bool:
        """Record the first sighting of an opening. True when it was new; the first time never moves."""
        key = (incident_id, opening)
        if key in self._openings:
            return False
        self._openings[key] = now
        return True

    def reopen_at(self, incident_id: str, opening: int) -> float | None:
        return self._openings.get((incident_id, opening))

    def cooldown_for(self, incident_id: str, opening: int, cooldown: float) -> str | None:
        seen = self.reopen_at(incident_id, opening)
        if seen is None:
            return None
        best: _Use | None = None
        for use in self._uses.values():
            if (use.incident_id != incident_id or use.opening >= opening or use.completed_at is None
                    or use.proposal_id is None):
                continue
            if seen - use.completed_at < cooldown and (best is None or use.completed_at > (best.completed_at or 0.0)):
                best = use
        return None if best is None else best.proposal_id

    def approved_at(self, proposal_id: str) -> float | None:
        token = self._by_proposal.get(proposal_id)
        use = self._uses.get(token) if token is not None else None
        return None if use is None else use.approved_at

    def uses_of(self, incident_id: str) -> list[_Use]:
        return [u for u in self._uses.values() if u.incident_id == incident_id]

    def completed_uses(self) -> list[tuple[str, int, str]]:
        """(incident id, opening, proposal id) of every completed use: the sweep watches these for reopens."""
        return [(u.incident_id, u.opening, u.proposal_id) for u in self._uses.values()
                if u.completed_at is not None and u.proposal_id is not None]

    def incomplete_proposals(self) -> list[str]:
        return [u.proposal_id for u in self._uses.values() if u.proposal_id and u.completed_at is None]

    def seed(self, rows: Iterable[UsageRow], openings: Iterable[OpeningRow]) -> None:
        for row in rows:
            self._next += 1
            use = _Use(self._next, row.rule_id, row.incident_id, row.opening, row.approved_at, row.proposal_id,
                       row.completed_at)
            self._uses[self._next] = use
            self._by_proposal[row.proposal_id] = self._next
        for op in openings:
            self._openings.setdefault((op.incident_id, op.opening), op.first_seen_at)

    def prune(self, now: float) -> None:
        for token, use in list(self._uses.items()):
            if now - use.approved_at >= RETENTION:
                del self._uses[token]
                if use.proposal_id is not None:
                    self._by_proposal.pop(use.proposal_id, None)
        for key, seen in list(self._openings.items()):
            if now - seen >= RETENTION:
                del self._openings[key]
