# Specification 02: Kiro Crew Multi-Agent Orchestration

## 1. Overview
This specification defines the 3-agent autonomous **Kiro Crew** architecture responsible for interpreting parsed legacy telemetry events, diagnosing root causes, and formulating safe remediation plans.

## 2. Agent Roles & Personas

### 2.1 Agent 1: The Triage Sentinel
- **Goal:** Continuously ingest normalized `TelemetryEvent` objects, calculate threat/incident scores, and cluster related events into unified `IncidentCase` records.
- **Constraints:** High-speed, stateless pattern recognition. Discards routine informational heartbeats.

### 2.2 Agent 2: The Spec Auditor
- **Goal:** Cross-reference active `IncidentCase` records against the project's formal requirements and historical telemetry baselines.
- **Input:** `IncidentCase` + active specs from `.kiro/specs/`.
- **Output:** Root-cause hypothesis, severity rating, and suggested remediation policy.

### 2.3 Agent 3: The Remediation Specialist
- **Goal:** Transform approved remediation policies into concrete, sandboxed Model Context Protocol (MCP) tool invocations.
- **Tools:**
  - `mcp_run_diagnostic_command`: Non-destructive health check.
  - `mcp_restart_service`: Restarting crashed legacy daemons.
  - `mcp_patch_config`: Modifying configuration files with backup rollback.
- **Safety Rule:** High-impact destructive interventions require simulated dry-run validation before execution.

## 3. Inter-Agent Communication Protocol
Agents communicate via structured JSON messages following the standard Crew protocol:
`Triage Sentinel -> IncidentCase -> Spec Auditor -> AuditVerdict -> Remediation Specialist -> ActionPlan`.
