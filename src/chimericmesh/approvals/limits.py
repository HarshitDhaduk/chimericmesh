"""Proposal creation limits: one active proposal per incident and a per-service hourly rate (R9.3, R9.4)."""
from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping, Sequence

from .errors import ActiveProposalExists, ProposalRateLimited

MAX_PROPOSALS_PER_SERVICE_HOUR = 10  # the one constant (R1.4, R9.4)
WINDOW_SECONDS = 3600.0
UNASSIGNED_KEY = "unassigned"


class ProposalLimits:
    """Loop thread only. ``reserve`` runs in the same await-free block as rule evaluation."""

    def __init__(self) -> None:
        self._times: dict[str, deque[float]] = {}
        self._tokens: dict[int, tuple[str, str, float]] = {}   # token -> (service key, incident id, time)
        self._by_incident: dict[str, int] = {}                  # incident id -> token of an in-flight reservation
        self._next = 0

    def _prune(self, key: str, now: float) -> deque[float]:
        times = self._times.setdefault(key, deque())
        while times and now - times[0] >= WINDOW_SECONDS:
            times.popleft()
        return times

    def reserve(self, incident_id: str, service_key: str, now: float, *, enforce: bool,
                active_of: Callable[[str], str | None]) -> int:
        """Reserve one slot. With ``enforce`` false nothing is checked or reserved (token 0)."""
        if not enforce:
            return 0
        if incident_id in self._by_incident:
            holder = self._by_incident[incident_id]
            raise ActiveProposalExists(f"in-flight-{holder}")
        existing = active_of(incident_id)
        if existing is not None:
            raise ActiveProposalExists(existing)
        key = service_key or UNASSIGNED_KEY
        times = self._prune(key, now)
        if len(times) >= MAX_PROPOSALS_PER_SERVICE_HOUR:
            retry = int(WINDOW_SECONDS - (now - times[0])) + 1
            raise ProposalRateLimited(retry)
        self._next += 1
        token = self._next
        times.append(now)
        self._tokens[token] = (key, incident_id, now)
        self._by_incident[incident_id] = token
        return token

    def release(self, token: int) -> None:
        """Free the slot of a reservation that did not commit."""
        entry = self._tokens.pop(token, None)
        if entry is None:
            return
        key, incident_id, at = entry
        if self._by_incident.get(incident_id) == token:
            del self._by_incident[incident_id]
        times = self._times.get(key)
        if times is not None:
            try:
                times.remove(at)
            except ValueError:
                pass

    def commit(self, token: int) -> None:
        """Keep the slot (it counts for an hour) and stop treating the incident as in flight."""
        entry = self._tokens.pop(token, None)
        if entry is None:
            return
        if self._by_incident.get(entry[1]) == token:
            del self._by_incident[entry[1]]

    def count(self, service_key: str, now: float) -> int:
        return len(self._prune(service_key or UNASSIGNED_KEY, now))

    def seed(self, created_by_service: Mapping[str, Sequence[float]], now: float | None = None) -> None:
        """Load creation times of the last hour (restart). Older times are ignored when ``now`` is given."""
        for key, stamps in created_by_service.items():
            times = self._times.setdefault(key or UNASSIGNED_KEY, deque())
            fresh = sorted(t for t in stamps if now is None or now - t < WINDOW_SECONDS)
            for t in fresh:
                times.append(t)
            ordered = sorted(times)
            times.clear()
            times.extend(ordered)
