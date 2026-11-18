"""Central hub: stores events, detects incidents, fans out notifications."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import re
import time
from collections import OrderedDict, deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Awaitable, Callable, Mapping

from typing import TYPE_CHECKING

from . import diagnostics
from .diagnostics import Incident, IncidentTracker, find_code_hints
from .events import Event
from .notify import irc_summary, send_webhook, webhook_payload
from .store.config import env_reopen_after
from .store.incidents import (
    IncidentLifecycle,
    get_incident_row,
    load_incidents,
    row_to_incident,
    stored_ids_beyond,
)
from .store.models import Proposal
from .store.proposals import create_proposal as store_create_proposal
from .store.proposals import get_proposal as store_get_proposal
from .store.proposals import load_all_proposals, transition_proposal
from .store.events import read_counters

if TYPE_CHECKING:
    from .store.store import Store

log = logging.getLogger("chimericmesh.hub")
from .store.ports import AuditSink
from .store.errors import InvalidTransition, NotFound
from .store.proposals import (
    MAX_ACTION,
    MAX_DECIDED_BY,
    MAX_DETAILS,
    MAX_PROPOSALS,
    PROPOSAL_STATUSES,
    UNASSIGNED,
    HubAuthority,
    check_hub_transition,
    check_transition,
    validate_extra,
)

IrcBroadcast = Callable[[str, str], Awaitable[None]]

OPEN_THRESHOLD = 3
UPDATE_MILESTONES = (10, 50, 100, 500)
RATE_WINDOW = 10  # seconds of per-second buckets kept for events_per_second

__all__ = ["Hub", "Proposal", "ChatMessage", "PROPOSAL_STATUSES", "MAX_PROPOSALS", "MAX_ACTION", "MAX_DETAILS"]

TRANSCRIPT_MAX = 300
MAX_CHAT_TEXT = 500
BOT_NICK = "ChimericBot"
WEB_NICK = "dashboard"
_CHAT_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f]")

WORKFLOW_STEPS = (
    ("traffic", "Send some traffic", "Summon a failure storm, or run scripts/demo.ps1 or the replay tools"),
    ("incidents", "Spot an incident", "Three similar errors open an incident"),
    ("ask", "Ask ChimericBot", "Type !incidents or !explain <id> in the terminal"),
    ("propose", "Propose a fix", "An agent proposes a remediation; you can simulate one from an incident card"),
    ("decide", "Approve or deny it", "Operators decide with !approve p1 or !deny p1"),
    ("resolve", "Lay the incident to rest", "Use !resolve <id> or the button on the card"),
)

# Bundled sample app whose log statements the demo incidents match.
DEFAULT_CODE_ROOT = Path(__file__).resolve().parents[2] / "examples" / "legacy_app"


@dataclass
class ChatMessage:
    """One transcript line. Text may originate from IRC users: treat as untrusted."""

    id: int
    ts: float
    sender: str
    kind: str  # 'bot' | 'user' | 'system'
    text: str

    def to_dict(self) -> dict:
        return asdict(self)


def parse_operators(value: str | None) -> set[str] | None:
    """Parse CHIMERICMESH_OPERATORS into lowercase nicks. None means any nick may decide."""
    nicks = {n.strip().lower() for n in (value or "").split(",") if n.strip()}
    return nicks or None


class Hub:
    def __init__(self, capacity: int = 2000, code_root: Path | None = None,
                 webhook_url: str | None = None, irc_channel: str = "#ops",
                 clock: Callable[[], float] = time.time, *, reopen_after: int | None = None,
                 audit: AuditSink | None = None, store: Store | None = None) -> None:
        self.events: deque[Event] = deque(maxlen=capacity)  # bounded log view only
        self._event_seq = 0  # last event id handed out; ids start at 1
        self.store = store
        self._audit = audit if audit is not None or store is None else store.audit
        self._closed = False
        self._stored_unloaded_ids: set[str] = set()  # stored incidents not in the tracker (ids only)
        self._rehydrating: dict[str, asyncio.Task[bool]] = {}
        self._commit_tasks: set[asyncio.Task] = set()
        self._flush_task: asyncio.Task | None = None
        self._clock = clock
        self.started_at = clock()
        self.total_events = 0  # cumulative, independent of the bounded buffer
        self.total_by_source: dict[str, int] = {}
        self._buckets: deque[list[int]] = deque(maxlen=RATE_WINDOW)  # [second, count]
        self.tracker = IncidentTracker(on_evict=self._on_evict)  # cumulative incident state, O(1) per event
        env_root = os.environ.get("CHIMERICMESH_CODE_ROOT")
        self.code_root = code_root or (Path(env_root) if env_root else DEFAULT_CODE_ROOT)
        self.webhook_url = webhook_url or os.environ.get("CHIMERICMESH_WEBHOOK_URL")
        self.irc_channel = irc_channel
        self.irc_broadcast: IrcBroadcast | None = None
        self._announced: dict[str, int] = {}  # incident id -> count at last announcement
        self.lifecycle = IncidentLifecycle(
            self.tracker, store=store, announce=lambda text: self.say(self.irc_channel, text), now=clock,
            reopen_after=env_reopen_after() if reopen_after is None else reopen_after,
            audit_getter=lambda: self._audit, announced_getter=self._announced.get,
            **({} if store is None else {"persist_slots": store.config.persist_slots,
                                         "flush_interval": store.config.flush_interval}),
        )
        self.webhook_log: deque[dict] = deque(maxlen=50)
        # Nicks allowed to !approve/!deny/!resolve; None = every registered nick (trusted nets only).
        self.operators: set[str] | None = parse_operators(os.environ.get('CHIMERICMESH_OPERATORS'))
        self._proposals: OrderedDict[str, Proposal] = OrderedDict()
        self._proposal_seq = 0
        self.transcript: deque[ChatMessage] = deque(maxlen=TRANSCRIPT_MAX)
        self._chat_seq = 0
        self.commands_run = 0  # parsed commands handled (IRC or web)
        # Guided-workflow flags count actions taken in this run only, never persisted state.
        self._run_events = 0
        self._run_opened = 0  # openings and reopenings
        self._run_proposals = 0
        self._run_decided = 0
        self._run_resolved = 0
        if store is not None:
            self._load(store, capacity)

    # --- store-backed startup, shutdown and background flushing ---

    def _load(self, store: Store, capacity: int) -> None:
        """Mirror the store in memory. Blocks once, at construction."""
        self._event_seq = store.events.max_id_sync()
        for ev in store.events.newest_sync(capacity):
            self.events.append(ev)
        total, by_source = store.read_sync(read_counters, allow_blocking=True)
        self.total_events, self.total_by_source = total, by_source
        limit = diagnostics.MAX_CLUSTERS  # read at call time so tests can patch it

        def load_incident_rows(tx):
            return load_incidents(tx, limit), stored_ids_beyond(tx, limit)

        rows, beyond = store.read_sync(load_incident_rows, allow_blocking=True)
        for row in rows:
            self.tracker.install(row_to_incident(row))
            if row.announced_count is not None:
                self._announced[row.id] = row.announced_count
        self._stored_unloaded_ids = set(beyond)
        for prop in store.read_sync(load_all_proposals, allow_blocking=True):
            self._proposals[prop.id] = prop

    def _on_evict(self, gone: Incident) -> None:
        """A cluster left memory. A qualified one has a row (or will, below) and can be rehydrated."""
        if self.store is None or self._closed or not self.tracker.qualifies(gone):
            return
        self._stored_unloaded_ids.add(gone.id)
        self.lifecycle.mark_dirty(gone)
        try:
            task = asyncio.get_running_loop().create_task(self.lifecycle.persist(gone))
        except RuntimeError:
            return  # no loop: it stays dirty
        self._commit_tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task) -> None:
        self._commit_tasks.discard(task)
        if not task.cancelled():
            task.exception()  # retrieved, so it is never reported as "never retrieved"

    def start(self) -> None:
        """Start the background flush loop (store-backed hubs only). Safe to call twice."""
        if self.store is not None and self._flush_task is None and not self._closed:
            self._flush_task = asyncio.get_running_loop().create_task(self._flush_loop())

    async def _flush_loop(self) -> None:
        tick = self.store.config.flush_tick  # type: ignore[union-attr]
        while True:
            await asyncio.sleep(tick)
            try:
                await self.lifecycle.flush_dirty()
                if not self.store.health().store_ok:  # type: ignore[union-attr]
                    await self.store.note_last_event_id(self._event_seq)  # type: ignore[union-attr]
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("flush loop iteration failed")

    async def aclose(self, deadline: float = 5.0) -> None:
        """Stop background work and write what is dirty, within ``deadline`` seconds.

        The store itself is closed by whoever opened it. After this call the hub keeps working in
        memory and writes nothing.
        """
        if self._closed:
            return
        end = time.monotonic() + deadline
        self._closed = True
        if self._flush_task is not None:
            self._flush_task.cancel()
            await asyncio.gather(self._flush_task, return_exceptions=True)
            self._flush_task = None
        store = self.store
        if store is not None:
            pending = [t for t in self._commit_tasks if not t.done()]
            if pending:
                await asyncio.wait(pending, timeout=max(0.0, end - time.monotonic()))
            try:
                await asyncio.wait_for(store.note_last_event_id(self._event_seq),
                                       timeout=max(0.05, end - time.monotonic()))
            except Exception:
                log.error("could not record the last event id before shutdown")
            try:
                await asyncio.wait_for(self.lifecycle.flush_all(end), timeout=max(0.05, end - time.monotonic()))
            except Exception:
                log.error("shutdown flush of incidents did not finish")
        self.lifecycle.store = None  # later persists are no-ops

    @property
    def last_event_id(self) -> int:
        return self._event_seq

    @property
    def audit(self) -> AuditSink | None:
        return self._audit

    @audit.setter
    def audit(self, sink: AuditSink | None) -> None:
        self._audit = sink

    @property
    def resolved(self) -> set[str]:
        """Ids of resolved incidents (a copy; ``Incident.resolved`` is the source of truth)."""
        return self.tracker.resolved_ids()

    async def _rehydrate_wait(self, pid: str) -> bool:
        task = self._rehydrating.get(pid)
        if task is None:
            task = asyncio.ensure_future(self._rehydrate(pid))
            self._rehydrating[pid] = task
        return await asyncio.shield(task)

    async def _rehydrate(self, pid: str) -> bool:
        """Load one stored incident into the tracker. Returns False when the read failed."""
        try:
            try:
                row = await self.store.read(lambda tx: get_incident_row(tx, pid))  # type: ignore[union-attr]
            except Exception:
                log.exception("could not rehydrate incident %s; it stays stored", pid)
                return False
            # No await from here to the end: install, marker and set update together.
            if row is not None and not self.tracker.known(pid):
                self.tracker.install(row_to_incident(row))
                if row.announced_count is not None:
                    self._announced[pid] = row.announced_count
            self._stored_unloaded_ids.discard(pid)
            return True
        finally:
            self._rehydrating.pop(pid, None)

    def _next_event_id(self) -> int:
        self._event_seq += 1
        return self._event_seq

    async def ingest(self, event: Event) -> Event:
        """Record one event and return it tagged with its id.

        Everything up to the first ``await`` runs without yielding, so a second ``ingest`` for the
        same incident that runs during an await sees the new state and cannot open or reopen it twice.
        """
        pid = self.tracker.preview_id(event)  # None when the tracker would ignore the event
        rehydrated = True
        if pid is not None and pid in self._stored_unloaded_ids and not self.tracker.known(pid):
            # Step 0, the only await before the persist step: bring the stored incident back first.
            rehydrated = await self._rehydrate_wait(pid)
        event = dataclasses.replace(event, id=self._next_event_id())
        self.events.append(event)
        self._count(event)
        if self.store is not None and not self._closed:
            self.store.events.put(event)
        if not rehydrated:
            return event  # read failed: no fresh cluster that could overwrite the stored row
        existing = self.tracker.get(pid) if pid is not None else None
        if existing is not None and existing.resolved:
            if self.lifecycle.reopen_due(existing):
                self.tracker.reopen(existing, event)
                self._announced[existing.id] = 1  # one announcement per opening, set before any await
                self._run_opened += 1
                await self.lifecycle.reopen(existing)
            else:
                self.tracker.add(event)  # inside the window: the count grows, nothing is announced
                self.lifecycle.mark_dirty(existing)
            return event
        inc = self.tracker.add(event)  # O(1): updates only this event's cluster
        if inc is None or not self.tracker.qualifies(inc):
            return event
        if inc.opened and inc.id not in self._announced:
            # First opening: the tracker set ``opened`` (crit or worse, or the count passed the threshold).
            self._announced[inc.id] = inc.count
            self._run_opened += 1
            await self.lifecycle.persist(inc)
            await self._announce(inc)
        elif inc.id in self._announced:
            # Follow-up only when the cluster crosses a new milestone.
            last = self._announced[inc.id]
            if any(last < m <= inc.count for m in UPDATE_MILESTONES):
                self._announced[inc.id] = inc.count
                await self.lifecycle.persist(inc)
                await self._announce(inc, update=True)
            else:
                self.lifecycle.mark_dirty(inc)
        return event

    def _count(self, event: Event) -> None:
        self.total_events += 1
        self._run_events += 1
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

    def now(self) -> float:
        return self._clock()

    def incidents(self, *, status: str | None = None, service_id: str | None = None,
                  limit: int | None = None) -> list[Incident]:
        """Incidents, worst first. Filters apply before ``limit``; ``unassigned`` selects no service."""
        found = self.tracker.incidents()
        if status is not None:
            found = [i for i in found if i.resolved == (status == "resolved")]
        if service_id is not None:
            found = [i for i in found if i.service_id == (None if service_id == UNASSIGNED else service_id)]
        return found if limit is None else found[:max(0, limit)]

    def get_incident(self, incident_id: str) -> Incident | None:
        return self.tracker.get(incident_id)

    def code_hints(self, incident: Incident) -> list[dict]:
        return find_code_hints(incident, self.code_root)

    def recent(self, limit: int = 50, source: str | None = None, service_id: str | None = None) -> list[Event]:
        items = [e for e in self.events if source in (None, e.source)
                 and (service_id is None or e.service_id == (None if service_id == UNASSIGNED else service_id))]
        return items[-limit:][::-1]

    async def events_after(self, after_id: int, limit: int = 100, *, source: str | None = None,
                           service_id: str | None = None) -> list[Event]:
        """Events with an id above ``after_id``, oldest first. Filters apply before ``limit``.

        A store-backed hub reads ids below the ring from the database first (after a barrier, so
        rows still queued are committed), then continues with the ring: no gap, no duplicate.
        """
        found: list[Event] = []
        if self.store is not None and not self._closed:
            ring_min = self.events[0].id if self.events else None
            if ring_min is None or after_id < ring_min - 1:
                await self.store.events.barrier()
                ring_min = self.events[0].id if self.events else None
                found = await self.store.events.after(after_id, limit, source=source, service_id=service_id,
                                                      before_id=ring_min)
                if len(found) >= limit or ring_min is None:
                    return found[:limit]
                after_id = max(after_id, ring_min - 1)
        want = None if service_id is None else (None if service_id == UNASSIGNED else service_id)
        for e in self.events:
            if len(found) >= limit:
                break
            if e.id <= after_id or source not in (None, e.source):
                continue
            if service_id is not None and e.service_id != want:
                continue
            found.append(e)
            if len(found) >= limit:
                break
        return found

    async def resolve(self, incident_id: str, note: str = "") -> Incident | None:
        inc = self.get_incident(incident_id)
        if inc is None:
            return None
        if not inc.resolved:
            inc.resolved, inc.resolved_at = True, self._clock()
        self._run_resolved += 1
        self.lifecycle.mark_dirty(inc)
        await self.lifecycle.persist(inc)
        await self.say(self.irc_channel, f"RESOLVED {inc.id} ({inc.category} on {inc.host}) {note}".strip())
        return inc

    # --- human-approved remediation (record + announce only; nothing is executed here) ---

    def is_operator(self, nick: str) -> bool:
        return self.operators is None or nick.lower() in self.operators

    async def create_proposal(self, incident_id: str, action: str, details: str = "", *,
                              extra: Mapping[str, object] | None = None) -> Proposal | None:
        fields = validate_extra(extra)  # an unknown key raises ValueError before anything changes
        inc = self.get_incident(incident_id)
        if inc is None:
            return None
        if self.store is not None:
            now, svc, opening = time.time(), inc.service_id, inc.opening
            text = f"for {inc.id} ({inc.category} on {inc.host}): "

            def write(tx):
                return store_create_proposal(tx, incident_id=incident_id, action=action, details=details,
                                             service_id=svc, incident_opening=opening, now=now, extra=extra)

            def line(p: Proposal) -> str:
                return f"PROPOSAL {p.id} {text}{p.action} - reply !approve {p.id} or !deny {p.id}"

            prop = await self._commit_and_mirror(write, line, "proposals")
            return prop
        self._proposal_seq += 1  # no await between here and the insert: atomic on the loop thread
        prop = Proposal(id=f"p{self._proposal_seq}", incident_id=incident_id,
                        action=action[:MAX_ACTION], details=details[:MAX_DETAILS],
                        service_id=inc.service_id, incident_opening=inc.opening, **fields)
        self._proposals[prop.id] = prop
        self._run_proposals += 1
        while len(self._proposals) > MAX_PROPOSALS:
            self._proposals.popitem(last=False)  # oldest first
        await self.say(self.irc_channel,
                       f"PROPOSAL {prop.id} for {inc.id} ({inc.category} on {inc.host}): "
                       f"{prop.action} - reply !approve {prop.id} or !deny {prop.id}")
        return prop

    async def _commit_and_mirror(self, write, line, run_flag: str | None) -> Proposal | None:
        """Commit, mirror and announce as one unit that a cancelled caller cannot interrupt (T6, M7).

        ``InvalidTransition`` and ``NotFound`` become ``None``; a ``StoreError`` is raised before memory
        changes and no line is announced.
        """
        async def unit() -> Proposal | None:
            try:
                prop = await self.store.transact(write)  # type: ignore[union-attr]
            except (InvalidTransition, NotFound):
                return None
            # No await between the commit result and the mirror update.
            self._proposals[prop.id] = prop
            if run_flag == "proposals":
                self._run_proposals += 1
            elif run_flag == "decided":
                self._run_decided += 1
            try:
                await self.say(self.irc_channel, line(prop))
            except Exception:
                log.exception("could not announce %s after commit", prop.id)
            return prop

        task = asyncio.ensure_future(unit())
        self._commit_tasks.add(task)
        task.add_done_callback(self._task_done)
        return await asyncio.shield(task)

    @staticmethod
    def _allowed(prop: Proposal, new: str, decided_by: str) -> bool:
        """The shared transition table, as a caller without ``HubAuthority`` sees it."""
        try:
            check_transition(prop.status, new, origin="user", decided_by=decided_by, authority=None)
        except InvalidTransition:
            return False
        return True

    async def decide(self, pid: str, approve: bool, by: str) -> Proposal | None:
        new = "approved" if approve else "denied"
        if self.store is not None:
            now = time.time()
            return await self._commit_and_mirror(
                lambda tx: transition_proposal(tx, pid, new, decided_by=by, origin="user", authority=None, now=now),
                lambda p: f"{p.id} {p.status.upper()} by {p.decided_by}", "decided")
        prop = self._proposals.get(pid)
        if prop is None or not self._allowed(prop, new, by):
            return None
        prop.status = new
        prop.decided_by, prop.decided_at = by[:MAX_DECIDED_BY], time.time()
        prop.decision_origin = "user"  # only hub_transition (HubAuthority) can write 'hub'
        self._run_decided += 1
        await self.say(self.irc_channel, f"{prop.id} {prop.status.upper()} by {prop.decided_by}")
        return prop

    async def complete(self, pid: str, note: str = "", *,
                       extra: Mapping[str, object] | None = None) -> Proposal | None:
        fields = validate_extra(extra)
        if self.store is not None:
            now, cut = time.time(), note[:300]
            return await self._commit_and_mirror(
                lambda tx: transition_proposal(tx, pid, "done", decided_by="", origin="user", authority=None,
                                               now=now, result_note=cut, extra=extra),
                lambda p: f"{p.id} DONE: {p.result_note}".strip(), None)
        prop = self._proposals.get(pid)
        if prop is None or not self._allowed(prop, "done", ""):
            return None
        prop.status, prop.result_note = "done", note[:300]
        for key, value in fields.items():
            if value is not None:
                setattr(prop, key, value)
        await self.say(self.irc_channel, f"{prop.id} DONE: {prop.result_note}".strip())
        return prop

    async def hub_transition(self, pid: str, new_status: str, *, decided_by: str,
                             approved_by_rule: str | None = None, result_note: str | None = None,
                             extra: Mapping[str, object] | None = None) -> Proposal | None:
        """A transition made by the hub's own rule logic; the only place that builds ``HubAuthority``.

        Only the restricted set of ``check_hub_transition`` is accepted. REST, IRC and MCP never call it.
        """
        fields = validate_extra(extra)
        authority = HubAuthority()
        if self.store is not None:
            now = time.time()

            def write(tx):
                current = store_get_proposal(tx, pid)
                if current is None:
                    raise NotFound(pid)
                check_hub_transition(current.status, new_status, decided_by=decided_by,
                                     approved_by_rule=approved_by_rule)
                return transition_proposal(tx, pid, new_status, decided_by=decided_by, origin="hub",
                                           authority=authority, now=now, approved_by_rule=approved_by_rule,
                                           result_note=result_note, extra=extra)

            return await self._commit_and_mirror(
                write, lambda p: f"{p.id} {p.status.upper()} by {p.decided_by}", "decided")
        prop = self._proposals.get(pid)
        if prop is None:
            return None
        try:
            check_hub_transition(prop.status, new_status, decided_by=decided_by, approved_by_rule=approved_by_rule)
            check_transition(prop.status, new_status, origin="hub", decided_by=decided_by, authority=authority)
        except InvalidTransition:
            return None
        prop.status = new_status
        prop.decided_by, prop.decided_at = decided_by[:MAX_DECIDED_BY], time.time()
        prop.decision_origin = "hub"
        if approved_by_rule is not None:
            prop.approved_by_rule = approved_by_rule[:100]
        if result_note is not None:
            prop.result_note = result_note[:300]
        for key, value in fields.items():
            if value is not None:
                setattr(prop, key, value)
        self._run_decided += 1
        await self.say(self.irc_channel, f"{prop.id} {prop.status.upper()} by {prop.decided_by}")
        return prop

    def get_proposal(self, pid: str) -> Proposal | None:
        return self._proposals.get(pid)

    def list_proposals(self, status: str | None = None, *, service_id: str | None = None,
                       limit: int | None = None) -> list[Proposal]:
        """The newest ``limit`` matching proposals, oldest first. Filters apply before ``limit``."""
        found = [p for p in self._proposals.values() if status in (None, p.status)]
        if service_id is not None:
            found = [p for p in found if p.service_id == (None if service_id == UNASSIGNED else service_id)]
        return found if limit is None else found[len(found) - min(len(found), max(0, limit)):]

    def record(self, sender: str, text: str, kind: str = "user") -> ChatMessage:
        """Append a sanitized line to the bounded transcript and return it."""
        self._chat_seq += 1
        msg = ChatMessage(self._chat_seq, self._clock(), _CHAT_CTRL.sub("", str(sender))[:30],
                          kind, _CHAT_CTRL.sub("", str(text))[:MAX_CHAT_TEXT])
        self.transcript.append(msg)
        return msg

    def chat(self, after: int = 0, limit: int = 100) -> dict:
        after, limit = max(0, after), max(1, min(limit, 200))
        msgs = [m.to_dict() for m in self.transcript if m.id > after][:limit]
        return {"messages": msgs, "last_id": self._chat_seq if self.transcript else 0}

    def workflow(self) -> dict:
        done = {
            "traffic": self._run_events > 0,
            "incidents": self._run_opened > 0,
            "ask": self.commands_run > 0,
            "propose": self._run_proposals > 0,
            "decide": self._run_decided > 0,
            "resolve": self._run_resolved > 0,
        }
        steps = [{"key": k, "title": t, "hint": h, "done": done[k]} for k, t, h in WORKFLOW_STEPS]
        nxt = next((s["key"] for s in steps if not s["done"]), None)
        return {"steps": steps, "next": nxt, "complete": nxt is None,
                "you": {"nick": WEB_NICK, "operator": self.is_operator(WEB_NICK)}}

    async def say(self, channel: str, text: str) -> None:
        self.record(BOT_NICK, text, "bot")
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
            "store_ok": True if self.store is None else self.store.health().store_ok,
            "dropped_events": 0 if self.store is None else self.store.health().dropped_events,
        }
