"""MCP server: exposes the mesh to Kiro agents. Talks to the running hub over REST.

Run with: ``python -m chimericmesh.mcp_server`` (stdio transport).
Set CHIMERICMESH_URL if the hub is not at http://127.0.0.1:8000.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote

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
        "records the human decision. Use announce_to_irc for status messages. "
        "To watch for work, call wait_for_work in a loop: it holds up to about 25 seconds and "
        "returns incidents or approved proposals when there are any."
    ),
)

WAIT_READ_TIMEOUT = 35.0  # wait_for_work holds the request up to 25 s; every other tool keeps the factory default


def _seg(value: str) -> str:
    """One path segment: nothing in it can change the route."""
    return quote(str(value), safe="")


def _error(resp: httpx.Response) -> dict[str, Any]:
    try:
        body = resp.json()
    except ValueError:
        return {"error": resp.status_code, "detail": resp.text}
    if not isinstance(body, dict):
        return {"error": resp.status_code, "detail": body}
    return {**body, "error": resp.status_code, "detail": body.get("detail", resp.text)}


async def _call(method: str, path: str, timeout: float | None = None, **kwargs: Any) -> Any:
    if timeout is not None:
        kwargs["timeout"] = timeout
    async with _client_factory() as client:
        resp = await client.request(method, path, **kwargs)
        if resp.status_code >= 400:
            return _error(resp)
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
async def propose_remediation(incident_id: str, action: str, details: str = "",
                              files: list[str] | None = None, estimated_lines: int | None = None,
                              verification: str | None = None, kind: str | None = None) -> Any:
    """Ask the human operators to approve a remediation (announced on IRC). Does NOT run it.

    Only act after get_proposal reports status "approved". files, estimated_lines, verification
    and kind describe the planned change.
    """
    body: dict[str, Any] = {"incident_id": incident_id, "action": action, "details": details}
    for key, value in (("files", files), ("estimated_lines", estimated_lines),
                       ("verification", verification), ("kind", kind)):
        if value is not None:
            body[key] = value
    return await _call("POST", "/api/proposals", json=body)


@mcp.tool()
async def get_proposal(proposal_id: str) -> Any:
    """Get a proposal's status: pending, approved, denied or done. Act only when approved."""
    return await _call("GET", f"/api/proposals/{proposal_id}")


@mcp.tool()
async def list_proposals(status: str | None = None) -> Any:
    """List proposals, optionally filtered by status (pending, approved, denied, done)."""
    params = {"status": status} if status else None
    return await _call("GET", "/api/proposals", params=params)


@mcp.tool()
async def complete_proposal(proposal_id: str, note: str = "", files_changed: list[str] | None = None,
                            lines_changed: int | None = None, verification_result: str | None = None) -> Any:
    """Report that an approved proposal's work is finished (announced on IRC)."""
    body: dict[str, Any] = {"note": note}
    for key, value in (("files_changed", files_changed), ("lines_changed", lines_changed),
                       ("verification_result", verification_result)):
        if value is not None:
            body[key] = value
    return await _call("POST", f"/api/proposals/{proposal_id}/complete", json=body)


@mcp.tool()
async def list_services() -> Any:
    """List the registered services."""
    return await _call("GET", "/api/services")


@mcp.tool()
async def get_service(service_id: str) -> Any:
    """Get one service, including its code folder."""
    return await _call("GET", f"/api/services/{_seg(service_id)}")


@mcp.tool()
async def service_events(service_id: str, limit: int = 25, after_id: int | None = None,
                         min_severity: int | None = None) -> Any:
    """Events of one service, oldest first. Pass the last id back as after_id to page."""
    params: dict[str, Any] = {"limit": limit}
    if after_id is not None:
        params["after_id"] = after_id
    if min_severity is not None:
        params["min_severity"] = min_severity
    return await _call("GET", f"/api/services/{_seg(service_id)}/events", params=params)


@mcp.tool()
async def explain_log_window(service_id: str, minutes: int = 15) -> Any:
    """Digest of one service's recent log window: counts, top signatures, samples."""
    return await _call("GET", f"/api/services/{_seg(service_id)}/digest", params={"minutes": minutes})


@mcp.tool()
async def find_code_for_service(service_id: str, query: str) -> Any:
    """Search a service's code folder for a literal text."""
    return await _call("GET", f"/api/services/{_seg(service_id)}/code-search", params={"query": query})


@mcp.tool()
async def verify_proposal(proposal_id: str, files: list[str]) -> Any:
    """Check that the files may be changed under a proposal. Change nothing unless ok is true."""
    return await _call("POST", f"/api/proposals/{_seg(proposal_id)}/verify", json={"files": files})


@mcp.tool()
async def wait_for_work(timeout_seconds: int = 25, service_id: str | None = None,
                        exclude_ids: list[str] | None = None) -> Any:
    """Wait up to about 25 seconds for incidents or approved proposals; status is work or timeout."""
    params: dict[str, Any] = {"timeout_seconds": timeout_seconds}
    if service_id is not None:
        params["service_id"] = service_id
    if exclude_ids:
        params["exclude_ids"] = list(exclude_ids)
    return await _call("GET", "/api/watch/next", timeout=WAIT_READ_TIMEOUT, params=params)


if __name__ == "__main__":
    mcp.run()
