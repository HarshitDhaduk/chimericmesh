# ChimericMesh Architectural Steering & Coding Standards

This document steers Kiro when developing, refactoring, or extending the ChimericMesh codebase.

## 1. Architectural Principles
- **Separation of Concerns:**
  - `legacy_bridge/`: Raw TCP/UDP socket listeners, Syslog RFC 5424/3164 parsers, and IRC protocol handlers. Must be non-blocking (asyncio).
  - `agents/`: Kiro Crew multi-agent orchestration. No agent may execute direct arbitrary shell code; all interventions MUST pass through validated MCP tools.
  - `mcp_servers/`: Explicit tool declarations following Model Context Protocol specifications.
  - `web/`: Vanilla HTML/CSS/JS frontend built using `design-taste-frontend` rules. No bloated single-page framework overhead.

## 2. Python Standards
- Python 3.11+ required.
- Type hints on all function signatures (`typing`, `pydantic`).
- Pydantic models for all data exchange between legacy parsers and agents.
- Error handling: Never raise unhandled exceptions in the legacy stream ingestion loop; categorize anomalies into `CorruptedPayload` events for the Triage Agent.

## 3. Node.js & Frontend Standards
- Node 22+ compliant.
- Strict adherence to `design-taste-frontend` (Taste Skill 2.5):
  - One section per screen at 1440x900.
  - Equal left and right margins.
  - Dark mode palette with semantic accent highlights.
  - Zero empty visual states.
  - Headless browser consistency validation passing across 1440, 1024, 768, and 390 screen widths.
