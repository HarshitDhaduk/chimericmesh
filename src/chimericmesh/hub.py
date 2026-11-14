"""Central hub: stores events, detects incidents, fans out notifications."""

from __future__ import annotations

import os
import time
from collections import OrderedDict, deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from .diagnostics import Incident, IncidentTracker, find_code_hints
from .events import Event
from .notify import irc_summary, send_webhook, webhook_payload

IrcBroadcast = Callable[[str, str], Awaitable[None]]

OPEN_THRESHOLD = 3
UPDATE_MILESTONES = (10, 50, 100, 500)
RATE_WINDOW = 10  # seconds of per-second buckets kept for events_per_second

MAX_PROPOSALS = 200
MAX_ACTION = 200
MAX_DETAILS = 500
PROPOSAL_STATUSES = ('pending', 'approved', 'denied', 'done')

# Bundled sample app whose log statements the demo incidents match.
DEFAULT_CODE_ROOT = Path(__file__).resolve().parents[2] / "examples" / "legacy_app"


@dataclass
class Proposal:
    """A remediation an agent wants to perform, awaiting a human decision.

    SAFETY: ChimericMesh never executes ``action`` on any host. It only records the
    human decision and announces it on IRC. The agent (Kiro via MCP) does the real work
    under its own tool permissions, and only after the proposal is approved.
    """

    id: str
    incident_id: str
    action: str
    details: str = ""
    status: str = "pending"  # pending -> approved|denied ; approved -> done
    created: float = field(default_factory=time.time)
    decided_by: str = ""
    decided_at: float | None = None
    result_note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def parse_operators(value: str | None) -> set[str] | None:
    """Parse CHIMERICMESH_OPERATORS into lowercase nicks. None means any nick may decide."""
    nicks = {n.strip().lower() for n in (value or "").split(",") if n.strip()}
    return nicks or None


class Hub:
    def __init__(self, capacity: int = 2000, code_root: Path | None = None,
                 webhook_url: str | None = None, irc_channel: str = "#ops",
                 clock: Callable[[], float] = time.time) -> None:
        self.events: deque[Event] = deque(maxlen=capacity)  # bounded log view only
        self._clock = clock
        self.started_at = clock()
        self.total_events = 0  # cumulative, independent of the bounded buffer
        self.total_by_source: dict[str, int] = {}
        self._buckets: deque[list[int]] = deque(maxlen=RATE_WINDOW)  # [second, count]
        self.tracker = IncidentTracker()  # cumulative incident state, O(1) per event
        env_root = os.environ.get("CHIMERICMESH_CODE_ROOT")
        self.code_root = code_root or (Path(env_root) if env_root else DEFAULT_CODE_ROOT)
        self.webhook_url = webhook_url or os.environ.get("CHIMERICMESH_WEBHOOK_URL")
        self.irc_channel = irc_channel
        self.irc_broadcast: IrcBroadcast | None = None
        self.resolved: set[str] = set()
        self._announced: dict[str, int] = {}  # incident id -> count at last announcement
        self.webhook_log: deque[dict] = deque(maxlen=50)
        # Nicks allowed to !approve/!deny/!resolve; None = every registered nick (trusted nets only).
        self.operators: set[str] | None = parse_operators(os.environ.get('CHIMERICMESH_OPERATORS'))
        self._proposals: OrderedDict[str, Proposal] = OrderedDict()
        self._proposal_seq = 0

    async def ingest(self, event: Event) -> None:
        self.events.append(event)
        self._count(event)
        inc = self.tracker.add(event)  # O(1): updates only this event's cluster
        if inc is None or inc.id in self.resolved or not self.tracker.qualifies(inc):
            return
        if inc.id not in self._announced:
            # Open once the count is meaningful, or immediately for crit/alert/emerg.
            if inc.severity <= 2 or inc.count >= OPEN_THRESHOLD:
                self._announced[inc.id] = inc.count
                await self._announce(inc)
        else:
            # Follow-up only when the cluster crosses a new milestone.
            last = self._announced[inc.id]
            if any(last < m <= inc.count for m in UPDATE_MILESTONES):
                self._announced[inc.id] = inc.count
                await self._announce(inc, update=True)

    def _count(self, event: Event) -> None:
        self.total_events += 1
        self.total_by_source[event.source] = self.total_by_source.get(event.source, 0) + 1
        sec = int(self._clock())
        if self._buckets and self._buckets[-1][0] == sec:
            self._buckets[-1][1] += 1
        else:
            self._buckets.append([sec, 1])

    def rate(self) -> float:
        """Events/second over the last RATE_WINDOW seconds (0.0 when idle)."""
        now = int(self._clock())
        total = sum(n for sec, n in self._buckets if now - sec < RATE_WINDOW)
        return round(total / RATE_WINDOW, 1)

    def incidents(self) -> list[Incident]:
        found = self.tracker.incidents()
        for inc in found:
            inc.resolved = inc.id in self.resolved
        return found

    def get_incident(self, incident_id: str) -> Incident | None:
        inc = self.tracker.get(incident_id)
        if inc is not None:
            inc.resolved = inc.id in self.resolved
        return inc

    def code_hints(self, incident: Incident) -> list[dict]:
        return find_code_hints(incident, self.code_root)

    def recent(self, limit: int = 50, source: str | None = None) -> list[Event]:
        items = [e for e in self.events if source in (None, e.source)]
        return items[-limit:][::-1]

    async def resolve(self, incident_id: str, note: str = "") -> Incident | None:
        inc = self.get_incident(incident_id)
        if inc is None:
            return None
        self.resolved.add(incident_id)
        inc.resolved = True
        if self.irc_broadcast:
            await self.irc_broadcast(self.irc_channel, f"RESOLVED {inc.id} ({inc.category} on {inc.host}) {note}".strip())
        return inc

    # --- human-approved remediation (record + announce only; nothing is executed here) ---

    def is_operator(self, nick: str) -> bool:
        return self.operators is None or nick.lower() in self.operators

    async def create_proposal(self, incident_id: str, action: str, details: str = "") -> Proposal | None:
        inc = self.get_incident(incident_id)
        if inc is None:
            return None
        self._proposal_seq += 1
        prop = Proposal(id=f"p{self._proposal_seq}", incident_id=incident_id,
                        action=action[:MAX_ACTION], details=details[:MAX_DETAILS])
        self._proposals[prop.id] = prop
        while len(self._proposals) > MAX_PROPOSALS:
            self._proposals.popitem(last=False)  # oldest first
        await self.say(self.irc_channel,
                       f"PROPOSAL {prop.id} for {inc.id} ({inc.category} on {inc.host}): "
                       f"{prop.action} - reply !approve {prop.id} or !deny {prop.id}")
        return prop

    async def decide(self, pid: str, approve: bool, by: str) -> Proposal | None:
        prop = self._proposals.get(pid)
        if prop is None or prop.status != "pending":
            return None
        prop.status = "approved" if approve else "denied"
        prop.decided_by, prop.decided_at = by[:30], time.time()
        await self.say(self.irc_channel, f"{prop.id} {prop.status.upper()} by {prop.decided_by}")
        return prop

    async def complete(self, pid: str, note: str = "") -> Proposal | None:
        prop = self._proposals.get(pid)
        if prop is None or prop.status != "approved":
            return None
        prop.status, prop.result_note = "done", note[:300]
        await self.say(self.irc_channel, f"{prop.id} DONE: {prop.result_note}".strip())
        return prop

    def get_proposal(self, pid: str) -> Proposal | None:
        return self._proposals.get(pid)

    def list_proposals(self, status: str | None = None) -> list[Proposal]:
        return [p for p in self._proposals.values() if status in (None, p.status)]

    async def say(self, channel: str, text: str) -> None:
        if self.irc_broadcast:
            await self.irc_broadcast(channel, text)

    async def _announce(self, inc: Incident, update: bool = False) -> None:
        text = irc_summary(inc)
        await self.say(self.irc_channel, f"UPDATE {text}" if update else text)
        if self.webhook_url:
            payload = webhook_payload(inc, "incident.updated" if update else "incident.opened")
            ok = await send_webhook(self.webhook_url, payload)
            self.webhook_log.append({"incident": inc.id, "delivered": ok})

    def stats(self) -> dict:
        by_source: dict[str, int] = {}
        for e in self.events:
            by_source[e.source] = by_source.get(e.source, 0) + 1
        incs = self.incidents()
        return {
            "events": len(self.events),
            "by_source": by_source,
            "incidents": len(incs),
            "open_incidents": sum(1 for i in incs if not i.resolved),
            "total_events": self.total_events,
            "total_by_source": dict(self.total_by_source),
            "buffer_capacity": self.events.maxlen,
            "events_per_second": self.rate(),
            "uptime_seconds": int(self._clock() - self.started_at),
        }
