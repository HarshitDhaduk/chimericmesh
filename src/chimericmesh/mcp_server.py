"""MCP server: exposes the mesh to Kiro agents. Talks to the running hub over REST.

Run with: ``python -m chimericmesh.mcp_server`` (stdio transport).
Set CHIMERICMESH_URL if the hub is not at http://127.0.0.1:8000.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer

BASE_URL = os.environ.get("CHIMERICMESH_URL", "http://127.0.0.1:8000")

# Tests inject an ASGI-backed client here; production builds one per call.
_client_factory = lambda: httpx.AsyncClient(base_url=BASE_URL, timeout=10.0)  # noqa: E731

mcp = MCPServer(
    "chimericmesh",
    instructions=(
        "Bridge to legacy infrastructure (Syslog, IRC, Telnet). Remediation loop: "
        "list_incidents -> investigate with get_incident and find_code_for_incident -> "
        "propose_remediation (a human decides on IRC with !approve/!deny) -> poll get_proposal "
        "until status is approved; never act while pending or denied -> do the work yourself "
        "-> complete_proposal -> resolve_incident. The mesh never executes actions; it only "
        "records the human decision. Proposal ids (like p1-k7f) are unique per hub run; an unknown id "
        "after a hub restart means re-investigate and propose again. Use announce_to_irc for status messages."
    ),
)


async def _call(method: str, path: str, **kwargs: Any) -> Any:
    async with _client_factory() as client:
        resp = await client.request(method, path, **kwargs)
        if resp.status_code >= 400:
            return {"error": resp.status_code, "detail": resp.json().get("detail", resp.text)}
        return resp.json()


@mcp.tool()
async def list_incidents() -> Any:
    """List detected incidents across all legacy sources, most severe first."""
    return await _call("GET", "/api/incidents")


@mcp.tool()
async def get_incident(incident_id: str) -> Any:
    """Get one incident with hypothesis and remediation steps."""
    return await _call("GET", f"/api/incidents/{incident_id}")


@mcp.tool()
async def find_code_for_incident(incident_id: str) -> Any:
    """Search the configured code root for the source line that emits an incident's message."""
    return await _call("GET", f"/api/incidents/{incident_id}/code")


@mcp.tool()
async def recent_events(limit: int = 25, source: str | None = None) -> Any:
    """Recent raw events. source may be syslog, irc, or telnet."""
    params: dict[str, Any] = {"limit": limit}
    if source:
        params["source"] = source
    return await _call("GET", "/api/events", params=params)


@mcp.tool()
async def announce_to_irc(text: str, channel: str = "#ops") -> Any:
    """Post a message to an IRC channel so legacy operators see agent findings."""
    return await _call("POST", "/api/irc/say", json={"channel": channel, "text": text})


@mcp.tool()
async def resolve_incident(incident_id: str, note: str = "") -> Any:
    """Mark an incident resolved and announce it on IRC."""
    return await _call("POST", f"/api/incidents/{incident_id}/resolve", json={"note": note})


@mcp.tool()
async def propose_remediation(incident_id: str, action: str, details: str = "") -> Any:
    """Ask the human operators to approve a remediation (announced on IRC). Does NOT run it.

    Only act after get_proposal reports status "approved". Returns the proposal id (p<n>-<tag>).
    """
    return await _call("POST", "/api/proposals",
                       json={"incident_id": incident_id, "action": action, "details": details})


@mcp.tool()
async def get_proposal(proposal_id: str) -> Any:
    """Get a proposal's status: pending, approved, denied or done. Act only when approved.
    Ids look like p1-k7f and are unique per hub run; an unknown id (error 404) means the
    proposal does not exist on this hub, for example after a hub restart. Call this again
    right before acting and confirm status, action and incident_id.
    """
    return await _call("GET", f"/api/proposals/{proposal_id}")


@mcp.tool()
async def list_proposals(status: str | None = None) -> Any:
    """List proposals, optionally filtered by status (pending, approved, denied, done)."""
    params = {"status": status} if status else None
    return await _call("GET", "/api/proposals", params=params)


@mcp.tool()
async def complete_proposal(proposal_id: str, note: str = "") -> Any:
    """Report that an approved proposal's work is finished (announced on IRC)."""
    return await _call("POST", f"/api/proposals/{proposal_id}/complete", json={"note": note})


if __name__ == "__main__":
    mcp.run()
