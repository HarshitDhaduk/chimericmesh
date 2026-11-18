"""FastAPI application: REST API for agents/MCP plus the dashboard."""

from __future__ import annotations

import os
import random
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator

from .commands import handle_command, parse_command
from .events import Event
from .hub import WEB_NICK, Hub
from .listeners import IrcServer, start_syslog, start_telnet
from .parsers import irc_to_event, parse_syslog, telnet_to_event

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
    incident_id: str = Field(min_length=1, max_length=64)
    action: str = Field(min_length=1, max_length=200)
    details: str = Field(default="", max_length=500)


class DecideRequest(BaseModel):
    approve: bool
    by: str = Field(default="api", min_length=1, max_length=30)


class CompleteRequest(BaseModel):
    note: str = Field(default="", max_length=300)


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


def create_app(hub: Hub | None = None, start_listeners: bool = True) -> FastAPI:
    hub = hub or Hub()
    irc = IrcServer(hub)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        closers = []
        if start_listeners:
            bind = os.environ.get("CHIMERICMESH_BIND", "127.0.0.1")
            ports = {
                "syslog": int(os.environ.get("CHIMERICMESH_SYSLOG_PORT", 5514)),
                "irc": int(os.environ.get("CHIMERICMESH_IRC_PORT", 6667)),
                "telnet": int(os.environ.get("CHIMERICMESH_TELNET_PORT", 2323)),
            }
            syslog_t = await start_syslog(hub, bind, ports["syslog"])
            telnet_s = await start_telnet(hub, bind, ports["telnet"])
            irc_s = await irc.start(bind, ports["irc"])
            closers = [syslog_t.close, telnet_s.close, irc_s.close]
        yield
        for close in closers:
            close()

    app = FastAPI(title="ChimericMesh", version="0.1.0", lifespan=lifespan)
    app.state.hub = hub

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

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/api/stats")
    async def stats():
        return hub.stats()

    @app.get("/api/events")
    async def events(limit: int = 50, source: str | None = None):
        limit = max(1, min(limit, 500))
        return [e.to_dict() for e in hub.recent(limit, source)]

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
        await hub.ingest(event)
        return event.to_dict()

    @app.get("/api/incidents")
    async def incidents():
        return [i.to_dict() for i in hub.incidents()]

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

    @app.get("/api/proposals")
    async def proposals(status: Literal["pending", "approved", "denied", "done"] | None = None):
        return [p.to_dict() for p in hub.list_proposals(status)]

    @app.get("/api/proposals/{pid}")
    async def proposal(pid: str):
        return _proposal_or_404(pid).to_dict()

    @app.post("/api/proposals", status_code=201)
    async def create_proposal(req: ProposalRequest):
        prop = await hub.create_proposal(req.incident_id, req.action, req.details)
        if prop is None:
            raise HTTPException(404, "incident not found")
        return prop.to_dict()

    @app.post("/api/proposals/{pid}/decide")
    async def decide_proposal(pid: str, req: DecideRequest):
        _proposal_or_404(pid)
        prop = await hub.decide(pid, req.approve, req.by)
        if prop is None:
            raise HTTPException(409, "proposal is not pending")
        return prop.to_dict()

    @app.post("/api/proposals/{pid}/complete")
    async def complete_proposal(pid: str, req: CompleteRequest):
        _proposal_or_404(pid)
        prop = await hub.complete(pid, req.note)
        if prop is None:
            raise HTTPException(409, "proposal is not approved")
        return prop.to_dict()

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
    async def terminal(req: TerminalRequest):
        # Identity is fixed server-side; the client can never choose a nick.
        text = req.text
        hub.record(WEB_NICK, text, "user")
        cmd = parse_command(text)
        if cmd is None:
            reply = "commands start with ! - try !help"
            hub.record("ChimericBot", reply, "system")
            return {"ok": True, "handled": False, "replies": [reply]}
        replies = await handle_command(hub, WEB_NICK, hub.irc_channel, cmd)
        return {"ok": True, "handled": True, "replies": replies}

    @app.get("/api/workflow")
    async def workflow():
        return hub.workflow()

    return app
