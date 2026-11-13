---
inclusion: always
---

# ChimericMesh project rules

ChimericMesh fuses legacy protocols (Syslog, IRC, Telnet) with AI agents through MCP.

## Layout
- `src/chimericmesh/parsers/` pure functions, one per protocol. They must never raise on arbitrary input.
- `src/chimericmesh/diagnostics.py` clustering, classification, code hints.
- `src/chimericmesh/hub.py` event store plus notification fan-out. One announcement per incident.
- `src/chimericmesh/listeners.py` asyncio sockets (UDP syslog, TCP telnet, mini IRC server).
- `src/chimericmesh/mcp_server.py` MCP tools. Thin wrappers over the REST API, no business logic.
- `src/chimericmesh/web/index.html` dashboard, no build step.

## Conventions
- Python 3.11+, type hints, small pure functions. Match the existing style.
- MCP SDK is v2: use `from mcp.server.mcpserver import MCPServer`, not `FastMCP`.
- Treat all log text as untrusted. Never put it in `innerHTML`; use `textContent`.
- Strip CR/LF/NUL from anything written to an IRC socket.
- Only `http`/`https` webhook URLs.

## Commands
- Run: `python -m chimericmesh` (from `src`, or after `pip install -e .`)
- Tests: `python -m pytest -q`
