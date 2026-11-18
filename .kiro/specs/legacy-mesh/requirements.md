# Requirements: ChimericMesh legacy mesh

## Introduction
Operators of older infrastructure emit logs over Syslog, IRC and Telnet. They cannot reach modern AI agents or cloud webhooks. ChimericMesh ingests all three, detects incidents, and lets Kiro agents diagnose and respond through MCP.

## Requirements

### R1: Legacy ingestion
**User story:** As an operator, I want my existing Syslog, IRC and Telnet feeds accepted unchanged, so that I do not have to modify old systems.
1. WHEN a UDP datagram arrives in RFC 3164 or RFC 5424 format THEN the system SHALL create an event with host, app, severity and message.
2. WHEN a Telnet client sends a line THEN the system SHALL remove IAC negotiation and ANSI escapes before storing it.
3. WHEN an IRC client sends PRIVMSG or NOTICE THEN the system SHALL store it as an event with severity inferred from the text.
4. WHEN any input is malformed THEN the parsers SHALL NOT raise.

### R2: Incident detection
**User story:** As an on-call engineer, I want related errors grouped, so that I see one incident instead of a flood.
1. Events SHALL be clustered by host and a normalized message signature.
2. An incident SHALL be raised for severity <= 3, or for 3+ repeats at warning level.
3. Each incident SHALL include a category, hypothesis and remediation steps.
4. The system SHALL locate candidate source lines for an incident message in a configured code root.

### R3: Notification
1. WHEN a new incident opens THEN the system SHALL announce it once on IRC and, if configured, POST a webhook.
2. Outbound IRC lines SHALL contain no CR, LF or NUL and SHALL be at most 510 bytes.
3. Webhook URLs SHALL be limited to http and https.

### R4: Agent access via MCP
1. The MCP server SHALL expose tools: list_incidents, get_incident, find_code_for_incident, recent_events, announce_to_irc, resolve_incident.
2. WHEN an incident is resolved THEN the system SHALL announce the resolution on IRC.

### R5: Dashboard
1. The dashboard SHALL show stats, incidents and a live event log.
2. Log content SHALL be rendered as text, never as HTML.
### R6: IRC commands
**User story:** As an operator, I want to query and control the mesh from my IRC client, so that I need no new tooling.
1. WHEN a registered IRC client sends a message starting with `!` THEN the system SHALL treat it as a command and SHALL NOT ingest it as a log event.
2. The system SHALL support !help, !status, !incidents (max 5), !explain, !resolve, !proposals, !approve and !deny, and SHALL answer unknown commands with a hint to use !help.
3. WHEN CHIMERICMESH_OPERATORS is set THEN only those nicks (case-insensitive) SHALL be allowed !resolve, !approve and !deny; others SHALL receive a refusal. IF it is unset THEN any registered nick MAY (trusted networks only).
4. Replies SHALL pass through the injection-safe IRC formatter; commands from unregistered clients SHALL be ignored.
5. parse_command SHALL NOT raise on arbitrary text.
### R7: Human-approved remediation
1. An agent SHALL be able to create a proposal for a known incident; the system SHALL announce it on IRC with !approve/!deny instructions.
2. A proposal SHALL move only pending -> approved|denied and approved -> done; other transitions SHALL be rejected (REST 409).
3. Decisions SHALL be announced on IRC, and completion SHALL be announced with its note.
4. The system SHALL NOT execute any proposed action; it only records the decision.
5. Proposals SHALL be held in memory, bounded to 200, evicting the oldest first. Action max 200 chars, details max 500.
6. The MCP server SHALL expose propose_remediation, get_proposal, list_proposals, complete_proposal.

### R8: Web terminal and transcript
1. The hub SHALL keep a bounded transcript (300 messages, ids strictly increasing from 1) of every bot message, IRC user command and web terminal line; text SHALL have control characters stripped and be capped at 500 chars.
2. GET /api/chat?after&limit SHALL return the oldest messages after the cursor (limit clamped 1..200) and the newest overall id.
3. POST /api/terminal SHALL accept 1..200 chars, run `!` commands as the fixed identity `dashboard` through the same handler and operator check as IRC, and return the replies. Non-commands SHALL get a system hint and SHALL NOT be ingested.
4. State-changing requests whose Origin header differs from Host SHALL be rejected with 403; requests without Origin SHALL pass.

### R9: Guided workflow
1. GET /api/workflow SHALL report six ordered steps (traffic, incidents, ask, propose, decide, resolve) with done flags, the next step, completion, and whether `dashboard` is an operator.

### R10: Real-service demo
1. The repository SHALL ship examples/legacy_app, a runnable stdlib-only service that sends RFC 3164 syslog to the hub and emits a real, repeating network error against a local unresponsive upstream.
2. The shipped service SHALL contain an intentional defect (unbounded immediate retries). Behaviour tests in examples/legacy_app/tests SHALL fail on it and pass after a correct fix.
3. The main test suite SHALL pass whether or not the defect is fixed.
4. The agent SHALL verify an approved fix (tests and event rate) and put that evidence in the complete_proposal note.
