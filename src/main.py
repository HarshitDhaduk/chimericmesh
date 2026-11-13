"""ChimericMesh Main Server: Connects legacy stream ingestion, Kiro Crew, MCP tools, and WebSockets."""
import asyncio
import json
import random
from datetime import datetime, timezone
from typing import AsyncGenerator
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel

from src.legacy_bridge.parser import LegacyParser, TelemetryEvent, Severity
from src.agents.crew import KiroCrew, IncidentReport
from src.mcp_servers.server import MCPRemediationServer

app = FastAPI(title="ChimericMesh", version="1.0.0")
crew = KiroCrew()
mcp = MCPRemediationServer()

# In-memory incident and event log ring buffer
EVENT_HISTORY = []
MAX_HISTORY = 100

SAMPLE_LEGACY_STREAMS = [
    "<34>Oct 11 22:14:15 srv-alpha su[2304]: 'su root' failed for unauthorized_user on /dev/pts/8",
    ":guard_bot!bot@irc.corp PRIVMSG #ops-war-room :FATAL: Redis buffer memory exhausted, dropping replication links",
    "<11>Nov 13 10:45:00 gw-core kernel[0]: Out of memory: Kill process 891 (postgres) score 920 or sacrifice child",
    "<19>Nov 13 10:46:12 edge-node bgpd[801]: BGP neighbor 192.0.2.1 DOWN: Peer closed connection",
    ":metrics!service@cluster PRIVMSG #alerts :CRITICAL: Gateway timeout 504 on endpoint /v1/checkout",
    "<165>Nov 13 10:47:01 db-cluster-01 mysqld[1204]: Disk space warning: partition /var/lib/mysql exceeded 92%",
    "RAW-TCP: [EMERGENCY] Kernel panic - not syncing: Fatal exception in interrupt",
    "<13>Nov 13 10:48:30 auth-service sshd[4401]: Accepted publickey for admin from 10.0.4.12 port 52140 ssh2",
]

class SystemTelemetryStatus(BaseModel):
    total_events: int
    active_incidents: int
    mcp_actions_taken: int
    system_health: str

@app.get("/api/status")
async def get_system_status():
    critical_count = sum(1 for e in EVENT_HISTORY if e.get("severity") in ["critical", "emergency", "alert"])
    return {
        "total_events": len(EVENT_HISTORY),
        "critical_events": critical_count,
        "mcp_actions_taken": sum(1 for e in EVENT_HISTORY if e.get("mcp_action")),
        "health": "DEGRADED" if critical_count > 0 else "OPERATIONAL",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

@app.get("/api/events")
async def get_events():
    return EVENT_HISTORY[-30:]

@app.post("/api/simulate")
async def simulate_random_event():
    raw_line = random.choice(SAMPLE_LEGACY_STREAMS)
    event_id = f"evt_{random.randint(1000, 9999)}"
    event = LegacyParser.parse_line(raw_line, event_id=event_id)
    incident = crew.process(event)
    
    mcp_result = None
    if incident:
        if incident.recommended_mcp_tool == "restart_legacy_daemon":
            mcp_result = mcp.restart_legacy_daemon(incident.impacted_subsystem.split("/")[0])
        elif incident.recommended_mcp_tool == "diagnose_process":
            mcp_result = mcp.diagnose_process(incident.impacted_subsystem.split("/")[0])

    record = {
        "event_id": event.event_id,
        "timestamp": event.timestamp.isoformat(),
        "protocol": event.protocol.value,
        "severity": event.severity.value,
        "source_host": event.source_host,
        "subsystem": event.subsystem,
        "raw_message": event.raw_message,
        "clean_message": event.clean_message,
        "incident": incident.root_cause if incident else None,
        "mcp_tool": incident.recommended_mcp_tool if incident else None,
        "mcp_action": mcp_result,
    }

    EVENT_HISTORY.append(record)
    if len(EVENT_HISTORY) > MAX_HISTORY:
        EVENT_HISTORY.pop(0)

    return record

# Mount static web directory
app.mount("/static", StaticFiles(directory="src/web"), name="static")

@app.get("/")
async def serve_index():
    return FileResponse("src/web/index.html")
