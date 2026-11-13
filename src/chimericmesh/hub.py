"""Central hub: stores events, detects incidents, fans out notifications."""

from __future__ import annotations

import os
from collections import deque
from pathlib import Path
from typing import Awaitable, Callable

from .diagnostics import Incident, analyze, find_code_hints
from .events import Event
from .notify import irc_summary, send_webhook, webhook_payload

IrcBroadcast = Callable[[str, str], Awaitable[None]]

OPEN_THRESHOLD = 3
UPDATE_MILESTONES = (10, 50, 100, 500)

# Bundled sample app whose log statements the demo incidents match.
DEFAULT_CODE_ROOT = Path(__file__).resolve().parents[2] / "examples" / "legacy_app"


class Hub:
    def __init__(self, capacity: int = 2000, code_root: Path | None = None,
                 webhook_url: str | None = None, irc_channel: str = "#ops") -> None:
        self.events: deque[Event] = deque(maxlen=capacity)
        env_root = os.environ.get("CHIMERICMESH_CODE_ROOT")
        self.code_root = code_root or (Path(env_root) if env_root else DEFAULT_CODE_ROOT)
        self.webhook_url = webhook_url or os.environ.get("CHIMERICMESH_WEBHOOK_URL")
        self.irc_channel = irc_channel
        self.irc_broadcast: IrcBroadcast | None = None
        self.resolved: set[str] = set()
        self._announced: dict[str, int] = {}  # incident id -> count at last announcement
        self.webhook_log: deque[dict] = deque(maxlen=50)

    async def ingest(self, event: Event) -> None:
        self.events.append(event)
        if event.severity > 4:
            return
        for inc in self.incidents():
            if inc.resolved:
                continue
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

    def incidents(self) -> list[Incident]:
        found = analyze(self.events)
        for inc in found:
            inc.resolved = inc.id in self.resolved
        return found

    def get_incident(self, incident_id: str) -> Incident | None:
        return next((i for i in self.incidents() if i.id == incident_id), None)

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
        }
