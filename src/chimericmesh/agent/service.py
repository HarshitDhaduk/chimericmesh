"""The facade of the agent loop: the only class that touches the injected ports (design decision 1).
It reads events, incidents and proposals through ``StateView``, services through ``ServiceRegistry`` and
approvals through the three-method ``ApprovalReader``. It writes nothing and imports no ``hub``.
"""
from __future__ import annotations
import asyncio
import threading
from collections.abc import Sequence
from chimericmesh.diagnostics import Incident
from .codesearch import search_code, validate_query
from .digest import Window, build_digest, collect_window
from .errors import NoCodeFolder, SearchBusy, UnknownProposal, UnknownService
from .feed import service_events
from .limits import clean_mapping, clean_text
from .ports import AgentPorts
from .snippet import DEFAULT_TEST_COMMAND, build_snippet
from .verify import VerifyInputs, evaluate_verify
from .watch import UNASSIGNED, WatchQueue
MAX_SEARCHES = 2
class AgentService:
    def __init__(self, ports: AgentPorts) -> None:
        self._ports = ports
        self.watch = WatchQueue(ports)
        self._running = 0              # code-search worker threads; changed on the loop thread only
    # --- lookups -----------------------------------------------------------------------------------
    def _service(self, service_id: str):
        """The registered service, or ``UnknownService`` (also for the ``unassigned`` pseudo service, R1.5)."""
        if not isinstance(service_id, str) or service_id == UNASSIGNED:
            raise UnknownService(service_id)
        svc = self._ports.registry.get(service_id)
        if svc is None:
            raise UnknownService(service_id)
        return svc
    def knows_service(self, service_id: str) -> bool:
        """For the long poll filter: a registered id or ``unassigned``."""
        return service_id == UNASSIGNED or self._ports.registry.get(service_id) is not None
    # --- reads -------------------------------------------------------------------------------------
    async def service_events(self, service_id: str, limit: int, after_id: int, min_severity: int | None) -> dict:
        self._service(service_id)
        return await service_events(self._ports.state, service_id, limit=limit, after_id=after_id,
                                    min_severity=min_severity)
    async def digest(self, service_id: str, minutes: int) -> dict:
        self._service(service_id)
        state = self._ports.state
        window = Window.ending(state.now(), minutes)
        events, truncated = await collect_window(state, service_id, window)
        incidents = state.incidents(service_id=service_id)
        return build_digest(service_id, window, events, incidents, scanned=len(events), truncated=truncated)
    async def code_search(self, service_id: str, query: str) -> dict:
        svc = self._service(service_id)
        folder = svc.code_folder
        if not folder:
            raise NoCodeFolder("service has no code folder")
        validate_query(query)           # InvalidQuery before a slot is taken
        if self._running >= MAX_SEARCHES:
            raise SearchBusy(retry_after=2)
        stop = threading.Event()
        protected = tuple(self._ports.protected_dirs)
        loop = asyncio.get_running_loop()
        # The slot is released by the worker's completion, not by this task: a cancelled task whose thread is
        # still running keeps its slot (G2, R1-L3). ``search_code`` is looked up here so tests can replace it.
        worker = loop.create_task(asyncio.to_thread(search_code, folder, query, protected_dirs=protected,
                                                    cancelled=stop.is_set))
        self._running += 1
        worker.add_done_callback(self._release)
        try:
            result = await asyncio.shield(worker)
        finally:
            stop.set()
        return {"service_id": service_id,
                "hits": [{"file": clean_text(h.file), "line": h.line, "text": clean_text(h.text, 200)}
                         for h in result.hits],
                "truncated": result.truncated}
    def _release(self, task: asyncio.Future) -> None:
        self._running = max(0, self._running - 1)
        if not task.cancelled():
            task.exception()            # retrieved: a failed search of a cancelled caller is not logged twice
    # --- verify ------------------------------------------------------------------------------------
    def verify(self, proposal_id: str, files: Sequence[str]) -> dict:
        """One synchronous block, so the inputs are one consistent snapshot (G1)."""
        ports = self._ports
        prop = ports.state.get_proposal(proposal_id)
        if prop is None:
            raise UnknownProposal(proposal_id)
        reader = ports.approvals
        rule_id = prop.approved_by_rule
        rule = reader.rule(rule_id) if reader is not None and rule_id else None
        svc = ports.registry.get(prop.service_id) if prop.service_id else None
        inputs = VerifyInputs(
            proposal_id=prop.id, status=prop.status, approved_by_rule=rule_id,
            declared_files=tuple(prop.files) if isinstance(prop.files, list) else None,
            approval=reader.approval_of(prop) if reader is not None else None,
            rule_status=None if rule is None else rule.status,
            rule_code_folder=None if rule is None else rule.code_folder,
            rule_expires_at=None if rule is None else rule.expires_at,
            paused=bool(reader.status().paused) if reader is not None else False,
            approvals_available=reader is not None,
            code_folder=None if svc is None else (svc.code_folder or None), has_service=svc is not None,
            now=ports.state.now(), protected_dirs=tuple(ports.protected_dirs))
        result = evaluate_verify(inputs, files)
        return {"ok": result.ok, "reasons": list(result.reasons), "status": clean_text(result.status, 20),
                "approval": clean_mapping(result.approval),
                "declared_files": [clean_text(f) for f in result.declared_files]}
    # --- snippet and wiring helpers ------------------------------------------------------------------
    def permissions_snippet(self, service_id: str, test_command: str | None) -> str:
        svc = self._service(service_id)
        return build_snippet(svc, workspace_root=self._ports.workspace_root,
                             test_command=test_command or DEFAULT_TEST_COMMAND)
    def code_folder_for_incident(self, inc: Incident) -> str | None:
        """The live code folder of the incident's service, or ``None`` (B6)."""
        if inc.service_id is None:
            return None
        svc = self._ports.registry.get(inc.service_id)
        return None if svc is None else (svc.code_folder or None)
    def aclose(self) -> None:
        self.watch.close()
