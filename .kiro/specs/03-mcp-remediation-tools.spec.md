# Specification 03: Model Context Protocol (MCP) Remediation Server

## 1. Overview
The MCP Remediation Server exposes sandboxed diagnostic and remediation tools to Kiro and the Kiro Crew Remediation Specialist agent using the official Model Context Protocol (JSON-RPC 2.0).

## 2. Tool Definitions

### 2.1 `diagnose_process`
- **Description:** Inspects process status, memory consumption, and listening ports for legacy services.
- **Parameters:**
  - `service_name` (string, required): e.g. "syslogd", "ircd", "telemetry-collector".
- **Returns:** Process health dictionary (PID, status, CPU %, memory MB, uptime).

### 2.2 `restart_legacy_daemon`
- **Description:** Gracefully reloads or restarts a crashed legacy daemon.
- **Parameters:**
  - `service_name` (string, required): Target service name.
  - `graceful` (boolean, optional, default: true): If true, attempts SIGHUP before SIGTERM.
- **Returns:** Restart confirmation and new process status.

### 2.3 `rollback_configuration`
- **Description:** Restores a legacy configuration file from the timestamped backup stash if a patch creates anomalies.
- **Parameters:**
  - `config_path` (string, required): Absolute or relative path to the configuration file.
  - `backup_id` (string, optional): Specific backup ID to restore (defaults to latest).
- **Returns:** Rollback summary and verification status.

## 3. Sandboxing & Security Boundaries
- All commands execute in restricted subshells.
- Whitelist enforced: No arbitrary root shell execution.
- Path traversal prevention: File operations must remain within the designated project root.
