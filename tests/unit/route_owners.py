"""Owner table of every route of the app (agent-loop task 8.2, Success Criterion 6).
A route that is not listed here fails ``test_agent_route_table`` until its module adds a row. The dashboard
module adds its own rows in its own tasks.
"""
from __future__ import annotations
OWNERS: dict[tuple[str, str], str] = {}
def _own(owner: str, *routes: tuple[str, str]) -> None:
    for route in routes:
        assert route not in OWNERS, route
        OWNERS[route] = owner
_own("agent-loop",
     ("GET", "/api/services/{service_id}/events"), ("GET", "/api/services/{service_id}/digest"),
     ("GET", "/api/services/{service_id}/code-search"), ("POST", "/api/proposals/{pid}/verify"),
     ("GET", "/api/watch/next"), ("GET", "/api/services/{service_id}/permissions-snippet"))
_own("services",
     ("GET", "/api/services"), ("POST", "/api/services"), ("GET", "/api/services/{service_id}"),
     ("PATCH", "/api/services/{service_id}"), ("DELETE", "/api/services/{service_id}"),
     ("POST", "/api/services/{service_id}/log-sources"),
     ("PATCH", "/api/services/{service_id}/log-sources/{source_id}"),
     ("DELETE", "/api/services/{service_id}/log-sources/{source_id}"),
     ("GET", "/api/audit"))
_own("discovery",                                       # none of these has an MCP tool (discovery A2)
     ("GET", "/api/discovery/dismissals"), ("DELETE", "/api/discovery/dismissals"), ("POST", "/api/discovery/scan"),
     ("GET", "/api/discovery/scans/{scan_id}"),
     ("POST", "/api/discovery/suggestions/{suggestion_id}/accept"),
     ("POST", "/api/discovery/suggestions/{suggestion_id}/dismiss"))
_own("auto-approval",
     ("GET", "/api/proposals"), ("POST", "/api/proposals"), ("GET", "/api/proposals/{pid}"),
     ("POST", "/api/proposals/{pid}/complete"), ("POST", "/api/proposals/{pid}/decide"),
     ("POST", "/api/proposals/{pid}/rule"), ("GET", "/api/proposals/{pid}/rule-defaults"),
     ("POST", "/api/proposals/{pid}/withdraw"), ("GET", "/api/approvals/status"),
     ("GET", "/api/rules"), ("POST", "/api/rules"), ("POST", "/api/rules/pause"), ("POST", "/api/rules/resume"),
     ("GET", "/api/rules/{rule_id}"), ("PATCH", "/api/rules/{rule_id}"), ("POST", "/api/rules/{rule_id}/enable"),
     ("POST", "/api/rules/{rule_id}/pause"), ("POST", "/api/rules/{rule_id}/revoke"))
_own("baseline",
     ("GET", "/"), ("GET", "/api/chat"), ("POST", "/api/demo/storm"), ("GET", "/api/events"),
     ("GET", "/api/incidents"), ("GET", "/api/incidents/{incident_id}"), ("GET", "/api/incidents/{incident_id}/code"),
     ("POST", "/api/incidents/{incident_id}/resolve"), ("POST", "/api/ingest"), ("POST", "/api/irc/say"),
     ("GET", "/api/stats"), ("POST", "/api/terminal"), ("GET", "/api/workflow"))
_own("framework", ("GET", "/docs"), ("HEAD", "/docs"), ("GET", "/docs/oauth2-redirect"),
     ("HEAD", "/docs/oauth2-redirect"), ("GET", "/openapi.json"), ("HEAD", "/openapi.json"),
     ("GET", "/redoc"), ("HEAD", "/redoc"))
