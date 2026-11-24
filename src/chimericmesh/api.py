"""FastAPI application: REST API for agents/MCP plus the dashboard."""

from __future__ import annotations

import asyncio
import logging
import os
import random
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator

from .agent.ports import AgentPorts
from .agent.routes import build_router as build_agent_router
from .agent.service import AgentService
from .agent.snippet import find_workspace_root
from .approvals.errors import ApprovalsNotMigrated
from .approvals.manager import ApprovalManager
from .approvals.models import ApprovalSettings
from .approvals.routes import build_router as build_approvals_router
from .approvals.routes import register_error_handlers as register_approvals_errors
from .approvals.schema import register_migrations as register_approvals_migrations
from .commands import handle_command, parse_command
from .connectors.config import resolve_tcp_port
from .connectors.manager import ConnectorManager
from .connectors.schema import connectors_registry
from .connectors.submit import make_submit
from .connectors.syslog_tcp import start_syslog_tcp
from .diagnostics import find_code_hints
from .discovery.accept import Acceptor, RegistrySink
from .discovery.catalog import load_catalog
from .discovery.engine import run_scan_sync
from .discovery.manager import DiscoveryService, ScanManager
from .discovery.probe import LinuxProcFs, OsFsProbe
from .discovery.routes import build_router as build_discovery_router
from .discovery.runner import SubprocessRunner
from .discovery.schema import register_migrations as register_discovery_migrations
from .discovery.text import os_name_from_platform
from .events import Event
from .hub import WEB_NICK, Hub
from .listeners import IrcServer, start_syslog, start_telnet
from .parsers import irc_to_event, parse_syslog, telnet_to_event
from .parsers.formats import FORMAT_NAMES
from .services.access import AccessControl, OperatorVerdict, is_loopback, parse_allowed_hosts
from .services.asgi import MAX_BODY_BYTES, BodyLimit, HostGuard
from .services.deps import make_require_admin
from .services.limiter import ReduceAuditLimiter
from .services.limiter import reduce_key as make_reduce_key
from .services.registry import ServiceRegistry
from .services.routes import build_router
from .services.token import TokenStore, resolve_token_path
from .services.validate import KNOWN_PROTOCOLS
from .store import MigrationRegistry, Store, StoreError
from .store.config import MEMORY, resolve_db_path, resolve_home
from .store.proposals import is_reserved_decider
from .web.feed import StreamGate
from .web.stream import build_stream_router

log = logging.getLogger("chimericmesh.api")

WEB_DIR = Path(__file__).parent / "web"


class IngestRequest(BaseModel):
    source: Literal["syslog", "irc", "telnet"]
    line: str = Field(min_length=1, max_length=8192)
    host: str = Field(default="api-client", max_length=128)


class SayRequest(BaseModel):
    channel: str = Field(default="#ops", pattern=r"^#[\w\-]{1,48}$")
    text: str = Field(min_length=1, max_length=2000)


class ResolveRequest(BaseModel):
    note: str = Field(default="", max_length=300)


class ProposalRequest(BaseModel):
    # Unknown keys (status, decided_by, approved_by_rule ...) are ignored and never read (L4).
    incident_id: str = Field(min_length=1, max_length=64)
    action: str = Field(min_length=1, max_length=200)
    details: str = Field(default="", max_length=500)
    # Optional structure; ``approvals.validate`` checks the values and names the bad field in a 422.
    files: Any = None
    estimated_lines: Any = None
    verification: Any = None
    kind: Any = None


class DecideRequest(BaseModel):
    approve: bool
    by: str = Field(default="api", min_length=1, max_length=30)


class CompleteRequest(BaseModel):
    note: str = Field(default="", max_length=300)
    files_changed: Any = None
    lines_changed: Any = None
    verification_result: Any = None


class TerminalRequest(BaseModel):
    text: str = Field(min_length=1, max_length=200)

    @field_validator("text")
    @classmethod
    def _strip_nonempty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("text must not be blank")
        return v


MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


SHUTDOWN_SECONDS = 5.0
LIMITER_FLUSH_SECONDS = 0.5


def build_registry() -> MigrationRegistry:
    """Every module's migrations. Later modules add their ``register_migrations`` call here."""
    registry = connectors_registry()  # core, services and connectors migrations
    register_discovery_migrations(registry)
    register_approvals_migrations(registry)
    return registry


def _build_access(enforce: bool, hub: Hub, store: Store) -> AccessControl:
    """The access object. Enforcing: token file under the home directory (memory only for ``:memory:``)."""
    if not enforce:
        return AccessControl.disabled()
    tokens = TokenStore.open(resolve_token_path(os.environ, Path(resolve_home())),
                             in_memory=store.path == MEMORY, redact=store.add_redaction)
    return AccessControl(enforce=True, tokens=tokens,
                         allowed_hosts=parse_allowed_hosts(os.environ.get("CHIMERICMESH_ALLOWED_HOSTS")),
                         audit=store.audit, now=hub.now)


def _protected_dirs(store: Store) -> tuple[str, ...]:
    """Folders a service's code folder may never contain or lie inside (R2.2)."""
    home = resolve_home()
    token_dir = str(resolve_token_path(os.environ, Path(home)).parent)
    db_dir = home if store.path == MEMORY else str(Path(store.path).parent)
    return tuple(dict.fromkeys((home, db_dir, token_dir)))


def _detach_approvals(hub: Hub, approvals: ApprovalManager | None) -> None:
    """Shutdown: a hub the caller keeps drops the closed manager (fails closed, and breaks the hub-manager cycle)."""
    if approvals is not None and hub.approver is approvals:
        hub.approver = None
    if approvals is not None and hub.approvals is approvals:
        hub.approvals = None


def _wire_store(hub: Hub | None, store: Store | None) -> tuple[Hub, Store, bool]:
    """Apply the create_app contract. Returns (hub, store, whether the lifespan closes the store)."""
    if hub is None:
        if store is not None:
            return Hub(store=store), store, False
        opened = Store.open(resolve_db_path(), registry=build_registry())
        return Hub(store=opened), opened, True
    if hub.store is not None:
        if store is not None and store is not hub.store:
            raise ValueError("create_app: the hub is backed by a different store")
        return hub, hub.store, False
    owned = store is None
    store = store or Store.open(MEMORY, registry=build_registry())  # tables, counters, audit of new modules
    if hub.audit is None:
        hub.audit = store.audit  # an audit sink already attached is never overwritten
    return hub, store, owned


def _is_admin() -> bool:
    """Whether the hub runs with administrator rights. Only used to explain a ``limited`` scan; never elevates."""
    try:
        if os.name == "nt":
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
        return os.geteuid() == 0
    except Exception:  # noqa: BLE001
        return False


def _build_discovery(registry: ServiceRegistry, store: Store, hub: Hub) -> DiscoveryService:
    """Build discovery. Nothing here starts a scan, a task or a thread (R1.1)."""
    known_formats = frozenset(FORMAT_NAMES)
    load = load_catalog(known_formats)
    os_name = os_name_from_platform(sys.platform)
    system_root = {k: v for k, v in os.environ.items() if k.upper() == "SYSTEMROOT"}
    manager = ScanManager(
        load=load, engine=run_scan_sync, runner=SubprocessRunner(os_name or "linux", system_root),
        procfs=LinuxProcFs(), fs=OsFsProbe(), store=store, registry=registry, known_formats=known_formats,
        now=hub.now, os_name=os_name, is_admin=_is_admin(), own_pid=os.getpid(), env=dict(os.environ))
    acceptor = Acceptor(manager, RegistrySink(registry), registry, store, known_formats)
    return DiscoveryService(manager, acceptor, load)


def create_app(hub: Hub | None = None, start_listeners: bool = True, *,
               store: Store | None = None, enforce_access: bool | None = None) -> FastAPI:
    # R7.5: an explicit value wins; None means off for an injected hub and on for a hub this call builds.
    effective = enforce_access if enforce_access is not None else hub is None
    hub_arg, store_arg = hub, store
    if hub is not None and enforce_access is None and hub.enforce_access:
        log.warning("create_app: the hub was built with enforce_access=True but create_app got "
                    "enforce_access=None, which means False for an injected hub; pass "
                    "enforce_access=True to keep enforcement on")
    hub, store, owns_store = _wire_store(hub, store)
    hub.enforce_access = effective
    bind = os.environ.get("CHIMERICMESH_BIND", "127.0.0.1")
    if not is_loopback(bind):
        log.warning("CHIMERICMESH_BIND=%s is not a loopback address: the dashboard and the REST API "
                    "are reachable from other machines", bind)
    registry = ServiceRegistry.load(store, state=hub, protected=_protected_dirs(store), clock=hub.now)
    hub.tagger = registry.tag
    access = _build_access(effective, hub, store)
    hub.access = access if effective else None
    reduce_limiter = ReduceAuditLimiter(store.audit, now=hub.now)
    require_admin = make_require_admin(access)
    irc = IrcServer(hub)
    submit = make_submit(hub.ingest)
    manager = ConnectorManager(registry, store, submit)
    discovery = _build_discovery(registry, store, hub)

    async def announce(text: str) -> None:
        await hub.say(hub.irc_channel, text)

    approvals: ApprovalManager | None
    try:
        approvals = ApprovalManager.load(
            store, state=hub, writer=hub, registry=registry, limiter=reduce_limiter, announce=announce,
            protected=_protected_dirs(store), settings=ApprovalSettings.from_env(os.environ), now=hub.now)
    except ApprovalsNotMigrated:
        # Fail closed: without the tables there are no rules, so nothing is ever auto-approved (blockers B4).
        log.warning("the store has no approvals migrations: standing approval rules are off")
        approvals = None
    hub.approver = approvals
    hub.approvals = approvals
    if approvals is not None and hub_arg is None and store_arg is None:
        hub.enforce_limits = True  # B3: the production path (create_app() and python -m chimericmesh)

    agent = AgentService(AgentPorts(
        state=hub, registry=registry, approvals=approvals, protected_dirs=_protected_dirs(store),
        workspace_root=find_workspace_root(os.environ, os.getcwd())))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        closers = []
        syslog_tcp = None
        try:
            if start_listeners:
                ports = {
                    "syslog": int(os.environ.get("CHIMERICMESH_SYSLOG_PORT", 5514)),
                    "irc": int(os.environ.get("CHIMERICMESH_IRC_PORT", 6667)),
                    "telnet": int(os.environ.get("CHIMERICMESH_TELNET_PORT", 2323)),
                }
                syslog_t = await start_syslog(hub, bind, ports["syslog"])
                closers.append(syslog_t.close)
                telnet_s = await start_telnet(hub, bind, ports["telnet"])
                closers.append(telnet_s.close)
                irc_s = await irc.start(bind, ports["irc"])
                closers.append(irc_s.close)
                # A busy TCP port is a warning, never a failed start (B15).
                syslog_tcp = await start_syslog_tcp(submit, bind, resolve_tcp_port(os.environ))
                app.state.syslog_tcp = syslog_tcp
            hub.start()
            if approvals is not None:
                await approvals.start()
            # Tailers are not sockets: they start whether or not the listeners do.
            await manager.start()
        except BaseException:
            agent.aclose()
            for close in closers:
                close()
            await discovery.manager.aclose(0.2)
            if approvals is not None:
                await approvals.aclose(0.2)
            _detach_approvals(hub, approvals)
            if syslog_tcp is not None:
                await syslog_tcp.aclose(0.4)
            await manager.aclose(0.6)
            app.state.syslog_tcp = None
            if owns_store:
                store.close(timeout=SHUTDOWN_SECONDS, clean=False)  # a start that never served stays unclean
            raise
        yield
        # Shutdown order: the listeners stop calling ingest, the TCP server and the manager stop (one second
        # between them), then the limiter flushes its audit rows, then the hub flushes, then the store closes,
        # all against one deadline taken when shutdown begins. The hub keeps at least 3 seconds.
        deadline = time.monotonic() + SHUTDOWN_SECONDS

        def remaining() -> float:
            return max(0.05, deadline - time.monotonic())

        agent.aclose()                                              # held wait_for_work calls end with 	imeout first (R4.6)
        for close in closers:
            close()
        await discovery.manager.aclose(min(0.2, remaining()))     # a running scan stops before the connector steps
        if approvals is not None:
            await approvals.aclose(min(1.0, remaining()))         # the sweep, then in-flight units
        if syslog_tcp is not None:
            await syslog_tcp.aclose(min(0.4, remaining()))
        app.state.syslog_tcp = None
        await manager.aclose(min(0.6, remaining()))
        try:
            await asyncio.wait_for(reduce_limiter.aclose(), min(LIMITER_FLUSH_SECONDS, remaining()))
        except Exception:  # includes the timeout: the hub flush matters more than a coalesced audit row
            log.error("the reduce-operations limiter did not flush before its slice ended")
        await hub.aclose(max(0.1, remaining()))
        _detach_approvals(hub, approvals)
        if owns_store:
            store.close(timeout=max(0.1, remaining()))
    app = FastAPI(title="ChimericMesh", version="0.1.0", lifespan=lifespan)
    app.state.hub = hub
    app.state.store = store
    app.state.services = registry
    app.state.access = access
    app.state.reduce_limiter = reduce_limiter
    app.state.connectors = manager
    app.state.discovery = discovery
    app.state.approvals = approvals
    app.state.agent = agent
    app.state.syslog_tcp = None

    @app.exception_handler(StoreError)
    async def store_unavailable(request: Request, exc: StoreError):
        log.error("store error in %s %s: %s", request.method, request.url.path, type(exc).__name__)
        return JSONResponse({"detail": "store unavailable"}, status_code=503)

    @app.middleware("http")
    async def same_origin_only(request: Request, call_next):
        # Browser CSRF defence for a localhost service: a page on another site can fire
        # POSTs at 127.0.0.1, but the browser labels them with its own Origin. Reject
        # state-changing requests whose Origin differs from Host. Non-browser clients
        # (curl, httpx, MCP server) send no Origin and pass.
        origin = request.headers.get("origin")
        if origin is not None and request.method in MUTATING:
            if urlsplit(origin).netloc != request.headers.get("host", ""):
                return JSONResponse({"detail": "cross-origin request blocked"}, status_code=403)
        return await call_next(request)

    # Added after the same-origin middleware; the last one added runs first: HostGuard, then BodyLimit.
    app.add_middleware(BodyLimit, max_bytes=MAX_BODY_BYTES)
    app.add_middleware(HostGuard, access=access)
    app.include_router(build_router(registry, access, require_admin))
    app.include_router(build_discovery_router(discovery, require_admin))
    if approvals is not None:
        app.include_router(build_approvals_router(approvals, access, require_admin))
        register_approvals_errors(app)
    app.include_router(build_agent_router(agent))
    stream_gate = StreamGate(lease_grace=10.0)  # the live-log slots (dashboard R5.3); lease_grace is set here only
    app.state.stream_gate = stream_gate
    app.include_router(build_stream_router(
        hub, service_exists=lambda sid: sid == "unassigned" or registry.get(sid) is not None,
        gate=stream_gate, clock=time.monotonic))

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(WEB_DIR / "index.html",
                            headers={"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"})

    @app.get("/api/ui-config")
    async def ui_config():
        # What the page needs to lock its controls and fill its forms. Read-only; never the token itself.
        enforcing = bool(access.enforce)
        token_path = access.token_path if enforcing else None
        return {"enforce_access": enforcing, "token_file": None if token_path is None else str(token_path),
                "formats": list(FORMAT_NAMES), "protocols": list(KNOWN_PROTOCOLS)}

    @app.get("/api/stats")
    async def stats():
        return hub.stats()

    @app.get("/api/events")
    async def events(limit: int = 50, source: str | None = None, after_id: int | None = Query(None, ge=0),
                     service_id: str | None = Query(None, max_length=64)):
        limit = max(1, min(limit, 500))
        if after_id is not None:  # a cursor read: oldest first
            found = await hub.events_after(after_id, limit, source=source, service_id=service_id)
            return [e.to_dict() for e in found]
        return [e.to_dict() for e in hub.recent(limit, source, service_id)]

    @app.post("/api/ingest", status_code=202)
    async def ingest(req: IngestRequest):
        event: Event | None
        if req.source == "syslog":
            event = parse_syslog(req.line, default_host=req.host)
        elif req.source == "irc":
            raw = req.line if req.line.startswith(":") else f":{req.host}!u@h {req.line}"
            event = irc_to_event(raw)
        else:
            event = telnet_to_event(req.line.encode(), host=req.host)
        if event is None:
            raise HTTPException(422, "line produced no event")
        tagged = await hub.ingest(event)
        return tagged.to_dict()

    @app.get("/api/incidents")
    async def incidents(status: Literal["open", "resolved"] | None = None,
                        service_id: str | None = Query(None, max_length=64),
                        limit: int | None = Query(None, ge=1, le=500)):
        return [i.to_dict() for i in hub.incidents(status=status, service_id=service_id, limit=limit)]

    @app.get("/api/incidents/{incident_id}")
    async def incident(incident_id: str):
        inc = hub.get_incident(incident_id)
        if inc is None:
            raise HTTPException(404, "incident not found")
        return inc.to_dict()

    @app.get("/api/incidents/{incident_id}/code")
    async def incident_code(incident_id: str):
        inc = hub.get_incident(incident_id)
        if inc is None:
            raise HTTPException(404, "incident not found")
        folder = agent.code_folder_for_incident(inc)              # B6: the incident's own service first
        if folder:
            return find_code_hints(inc, Path(folder))
        return hub.code_hints(inc)

    @app.post("/api/incidents/{incident_id}/resolve")
    async def resolve(incident_id: str, req: ResolveRequest):
        inc = await hub.resolve(incident_id, req.note)
        if inc is None:
            raise HTTPException(404, "incident not found")
        return inc.to_dict()

    # Proposals only record a human decision; nothing here executes the proposed action.
    def _proposal_or_404(pid: str):
        prop = hub.get_proposal(pid)
        if prop is None:
            raise HTTPException(404, "proposal not found")
        return prop

    def _view(prop):
        return prop.to_dict() if approvals is None else approvals.proposal_view(prop)

    @app.get("/api/proposals")
    async def proposals(status: Literal["pending", "approved", "denied", "done"] | None = None,
                        service_id: str | None = Query(None, max_length=64),
                        limit: int = Query(200, ge=1, le=500)):
        if approvals is not None:
            await approvals.expire_due()
        return [_view(p) for p in hub.list_proposals(status, service_id=service_id, limit=limit)]

    @app.get("/api/proposals/{pid}")
    async def proposal(pid: str):
        if approvals is not None:
            await approvals.expire_due()
        return _view(_proposal_or_404(pid))

    @app.post("/api/proposals", status_code=201)
    async def create_proposal(req: ProposalRequest):
        extra = {k: v for k, v in (("kind", req.kind), ("files", req.files), ("estimated_lines", req.estimated_lines),
                                   ("verification", req.verification)) if v is not None}
        prop = await hub.create_proposal(req.incident_id, req.action, req.details, extra=extra or None)
        if prop is None:
            raise HTTPException(404, "incident not found")
        return _view(prop)

    @app.post("/api/proposals/{pid}/decide", dependencies=[Depends(require_admin)])
    async def decide_proposal(pid: str, req: DecideRequest):
        _proposal_or_404(pid)
        if is_reserved_decider(req.by):  # R4.8: these prefixes belong to the hub's own rule logic
            return JSONResponse({"detail": "that decider name is reserved", "field": "by"}, status_code=422)
        prop = await hub.decide(pid, req.approve, req.by)
        if prop is None:
            raise HTTPException(409, "proposal is not pending")
        return _view(prop)

    @app.post("/api/proposals/{pid}/complete")
    async def complete_proposal(pid: str, req: CompleteRequest):
        _proposal_or_404(pid)
        extra = {k: v for k, v in (("files_changed", req.files_changed), ("lines_changed", req.lines_changed),
                                   ("verification_result", req.verification_result)) if v is not None}
        prop = await hub.complete(pid, req.note, extra=extra or None)
        if prop is None:
            raise HTTPException(409, "proposal is not approved")
        return _view(prop)

    @app.post("/api/irc/say", status_code=202)
    async def say(req: SayRequest):
        await hub.say(req.channel, req.text)
        return {"ok": True}

    @app.post("/api/demo/storm", status_code=202)
    async def storm():
        """Inject a realistic multi-protocol failure burst for demos."""
        now = time.time()
        lines = [
            ("syslog", "<27>Nov 13 11:10:08 db01 postgres[4121]: FATAL: could not write block 8812: No space left on device", "db01"),
            ("syslog", "<27>Nov 13 11:10:09 db01 postgres[4121]: FATAL: could not write block 8813: No space left on device", "db01"),
            ("syslog", "<27>Nov 13 11:10:11 db01 postgres[4121]: FATAL: could not write block 8814: No space left on device", "db01"),
            ("irc", "#ops :web02: upstream timed out connecting to 10.0.4.17:5432", "web02"),
            ("irc", "#ops :web02: upstream timed out connecting to 10.0.4.17:5432", "web02"),
            ("irc", "#ops :web02: upstream timed out connecting to 10.0.4.17:5432", "web02"),
            ("telnet", "ALERT sw-core-1: Failed password for admin from 203.0.113.9 port 4%d" % random.randint(10, 99), "sw-core-1"),
            ("telnet", "ALERT sw-core-1: Failed password for admin from 203.0.113.9 port 4%d" % random.randint(10, 99), "sw-core-1"),
            ("telnet", "ALERT sw-core-1: Failed password for admin from 203.0.113.9 port 4%d" % random.randint(10, 99), "sw-core-1"),
        ]
        for source, line, host in lines:
            if source == "syslog":
                ev = parse_syslog(line, default_host=host)
            elif source == "irc":
                ev = irc_to_event(f":{host}!u@h PRIVMSG {line}")
            else:
                ev = telnet_to_event(line.encode(), host=host)
            if ev:
                await hub.ingest(ev)
        return {"injected": len(lines), "at": now}

    @app.get("/api/chat")
    async def chat(after: int = 0, limit: int = 100):
        return hub.chat(after, limit)

    @app.post("/api/terminal")
    async def terminal(req: TerminalRequest, request: Request):
        # Identity is fixed server-side; the client can never choose a nick.
        text = req.text
        cmd = parse_command(text)
        verdict: OperatorVerdict | None = None
        if cmd is not None and cmd.name in {"approve", "deny"}:
            # Token first, transcript second: a refused request leaves no forged "dashboard> !approve" line.
            await require_admin(request)
            verdict = OperatorVerdict.ALLOWED if hub.enforce_access else None
        elif cmd is not None and cmd.name == "resolve":
            verdict = OperatorVerdict.BASELINE
        hub.record(WEB_NICK, text, "user")
        if cmd is None:
            reply = "commands start with ! - try !help"
            hub.record("ChimericBot", reply, "system")
            return {"ok": True, "handled": False, "replies": [reply]}
        who = request.client.host if request.client is not None and request.client.host else "unknown"
        replies = await handle_command(hub, WEB_NICK, hub.irc_channel, cmd, verdict=verdict,
                                       reduce_key=make_reduce_key("terminal", who),
                                       actor=f"dashboard-terminal:{who}")
        return {"ok": True, "handled": True, "replies": replies}

    @app.get("/api/workflow")
    async def workflow():
        return hub.workflow()

    return app
