"""FastAPI application: REST API for agents/MCP plus the dashboard."""

from __future__ import annotations

import os
import random
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .events import Event
from .hub import Hub
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

    return app
