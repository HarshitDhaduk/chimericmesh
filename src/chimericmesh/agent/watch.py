"""The watch queue: what the agent should look at now, and the long poll that waits for it (R4.3, R4.4).
``evaluate_work`` is pure and total. Sub-criteria 4.3a.1 to 4.3a.6 and 4.3b follow the design (review L8).
"""
from __future__ import annotations
import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from chimericmesh.diagnostics import Incident
from chimericmesh.store.models import Proposal
from .errors import TooManyWaiters
from .limits import MAX_WAITERS, TICK, WAIT_MAX, WAIT_MIN, clean_mapping, clean_text
from .ports import AgentPorts
UNASSIGNED = "unassigned"
MAX_WORK = 5
SAMPLE_LIMIT = 200
@dataclass(frozen=True, slots=True)
class WorkResult:
    incidents: tuple[dict, ...]
    approved_proposals: tuple[dict, ...]
    @property
    def has_work(self) -> bool:
        return bool(self.incidents or self.approved_proposals)
def _service_ok(sid: str | None, wanted: str | None, registered: Callable[[str], bool]) -> bool:
    """4.3a.2 and 4.3a.6 (and the same filter for proposals in 4.3b)."""
    if sid is None:
        return wanted == UNASSIGNED
    if wanted is not None and wanted != UNASSIGNED and sid != wanted:
        return False
    if wanted == UNASSIGNED:
        return False
    try:
        return bool(registered(sid))
    except Exception:  # noqa: BLE001 - total by contract
        return False
def _incident_view(inc: Incident) -> dict:
    return {"id": clean_text(inc.id), "service_id": None if inc.service_id is None else clean_text(inc.service_id),
            "host": clean_text(inc.host), "category": clean_text(inc.category), "severity": inc.severity,
            "count": inc.count, "opening": inc.opening, "first_seen": inc.first_seen,
            "last_seen": inc.last_seen, "sample": clean_text(inc.sample, SAMPLE_LIMIT)}
def _proposal_view(prop: Proposal, approval_of: Callable[[Proposal], dict | None]) -> dict:
    try:
        approval = clean_mapping(approval_of(prop))
    except Exception:  # noqa: BLE001
        approval = None
    files = prop.files if isinstance(prop.files, list) else None
    return {"id": clean_text(prop.id), "incident_id": clean_text(prop.incident_id),
            "service_id": None if prop.service_id is None else clean_text(prop.service_id),
            "action": clean_text(prop.action),
            "files": None if files is None else [clean_text(f) for f in files[:200]],
            "estimated_lines": prop.estimated_lines,
            "verification": None if prop.verification is None else clean_text(prop.verification),
            "approval": approval}
def _proposal_order(prop: Proposal) -> tuple:
    return (prop.created, len(prop.id), prop.id)
def evaluate_work(incidents: Sequence[Incident], proposals: Sequence[Proposal], *, service_id: str | None,
                  exclude: frozenset[str], registered: Callable[[str], bool],
                  approval_of: Callable[[Proposal], dict | None], limit: int = MAX_WORK) -> WorkResult:
    """The incidents (4.3a) and approved proposals (4.3b) the agent should act on. At most ``limit`` of each."""
    blocked: set[str] = set()                 # 4.3a.3: an active proposal
    denied_openings: set[tuple[str, int | None]] = set()    # 4.3a.5: a user denial, per opening
    approved: list[Proposal] = []
    for prop in proposals:
        if prop.status in ("pending", "approved"):
            blocked.add(prop.incident_id)
        elif prop.status == "denied" and prop.decision_origin == "user":
            denied_openings.add((prop.incident_id, prop.incident_opening))
        if prop.status == "approved" and prop.id not in exclude and _service_ok(prop.service_id, service_id, registered):
            approved.append(prop)
    work: list[Incident] = []
    for inc in incidents:
        if inc.resolved or inc.id in exclude or inc.id in blocked:                      # 4.3a.1, 4.3a.4, 4.3a.3
            continue
        if (inc.id, inc.opening) in denied_openings:                                   # 4.3a.5
            continue
        if not _service_ok(inc.service_id, service_id, registered):                    # 4.3a.2, 4.3a.6
            continue
        work.append(inc)
    work.sort(key=lambda i: (i.severity, -i.count, i.id))
    approved.sort(key=_proposal_order)
    cap = max(0, limit)
    return WorkResult(tuple(_incident_view(i) for i in work[:cap]),
                      tuple(_proposal_view(p, approval_of) for p in approved[:cap]))


def _timeout() -> dict:
    return {"status": "timeout", "incidents": [], "approved_proposals": []}


class WatchQueue:
    """The long poll. Evaluate, return at once when there is work, otherwise wait on a per-waiter event for
    one tick and evaluate again. No thread, no blocking sleep: a held waiter is a coroutine (G4)."""

    def __init__(self, ports: AgentPorts, *, wait_min: float = WAIT_MIN, wait_max: float = WAIT_MAX,
                 tick: float = TICK, max_waiters: int = MAX_WAITERS) -> None:
        self._ports = ports
        self.wait_min, self.wait_max = wait_min, wait_max
        self._tick = tick
        self._max = max_waiters
        self._waiters: set[asyncio.Event] = set()
        self._closed = False
        self.waiting = 0

    def _registered(self, sid: str) -> bool:
        return self._ports.registry.get(sid) is not None

    def _approval_of(self, prop: Proposal) -> dict | None:
        reader = self._ports.approvals
        return None if reader is None else reader.approval_of(prop)

    def _evaluate(self, service_id: str | None, exclude: frozenset[str]) -> WorkResult:
        state = self._ports.state
        return evaluate_work(state.incidents(status="open"), state.list_proposals(None), service_id=service_id,
                             exclude=exclude, registered=self._registered, approval_of=self._approval_of)

    def kick(self) -> None:
        """Wake every waiter now (a future hub listener, and tests)."""
        for waiter in tuple(self._waiters):
            waiter.set()

    def close(self) -> None:
        """Shutdown: every held waiter returns ``timeout`` at once, and so does any later call."""
        self._closed = True
        self.kick()

    async def next(self, *, timeout: float, service_id: str | None, exclude: frozenset[str],
                   is_disconnected: Callable[[], Awaitable[bool]] | None = None) -> dict:
        """Return work as soon as there is some, or ``timeout`` after ``timeout`` seconds. Raises
        ``TooManyWaiters`` when every slot is held. The slot is freed in ``finally``."""
        if self._closed:
            return _timeout()
        if self.waiting >= self._max:
            raise TooManyWaiters(retry_after=5)
        self.waiting += 1
        waiter = asyncio.Event()
        self._waiters.add(waiter)
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            while True:
                waiter.clear()                                           # before reading: no lost kick
                result = self._evaluate(service_id, exclude)             # synchronous: a consistent snapshot
                if result.has_work:
                    return {"status": "work", "incidents": list(result.incidents),
                            "approved_proposals": list(result.approved_proposals)}
                left = deadline - loop.time()
                if left <= 0 or self._closed:
                    return _timeout()
                if is_disconnected is not None and await is_disconnected():       # once per tick (R1-M1)
                    return _timeout()
                try:
                    await asyncio.wait_for(waiter.wait(), min(self._tick, left))
                except asyncio.TimeoutError:
                    pass
        finally:
            self.waiting -= 1
            self._waiters.discard(waiter)
