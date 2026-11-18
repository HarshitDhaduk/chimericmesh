# Design: ChimericMesh legacy mesh

## Flow
```
Syslog/UDP ─┐
Telnet/TCP ─┼─> parsers ─> Event ─> Hub ─> analyze() ─> Incident ─┬─> IRC announce
IRC/TCP ────┘                                                     ├─> webhook
REST /api/ingest ─────────────────────────────────────────────────┘
Kiro agent ─ MCP (stdio) ─> mcp_server ─ REST ─> Hub
```

## Components
- **Event**: frozen dataclass (source, host, app, severity 0..7, message, raw, ts).
- **Parsers**: pure, total functions. Syslog falls back to an info event. Telnet strips IAC and ANSI. IRC output is built by `format_privmsg`.
- **Hub**: bounded deque of events, set of announced incident ids, resolved set. Re-runs `analyze` on each severe event.
- **Diagnostics**: signature normalization, regex rule table (memory, disk, network, security, crash), `find_code_hints` grep.
- **MCP server**: separate process using REST, so agents and the hub can run independently.
- **IrcServer**: minimal NICK/JOIN/PING/PRIVMSG/QUIT server; broadcasts bot messages to joined clients.

## Correctness properties (tested with Hypothesis)
- P1: parse_syslog, parse_irc_line, irc_to_event never raise.
- P2: syslog PRI decodes to severity = PRI mod 8.
- P3: format_privmsg output has no CR/LF/NUL and each line <= 510 bytes.
- P4: chunking preserves the original text.
- P5: strip_telnet output has no NUL, ESC or CR; printable ASCII passes through.
- P6: analyze never counts more events than it was given.

## Out of scope
Authentication on the REST API and persistence. Bind to localhost by default.

## Addendum: IRC commands and approval loop (R6, R7)
- **commands.py**: pure `parse_command` (max 200 chars considered, 5 args, lowercase name) and async `handle_command` that sends `[mesh]`-prefixed replies through `hub.say`, so `format_privmsg` sanitises them.
- **IrcServer._dispatch**: registered clients only; text starting with `!` runs the command and skips ingestion. The registered nick is the sender, so prefix spoofing cannot impersonate an operator.
- **Hub**: `operators` (from CHIMERICMESH_OPERATORS, None = open), `Proposal` dataclass, ordered dict bounded to 200. Methods create_proposal, decide, complete, get_proposal, list_proposals.
- **State machine**: pending -> approved|denied; approved -> done. Nothing executes the action; the agent acts under its own permissions after approval.
- **REST/MCP**: thin wrappers; 404 unknown incident/proposal, 409 invalid transition.
- P7: parse_command never raises; names are lowercase and whitespace-free.
- P8: command replies contain no CR/LF/NUL.
- P9: under any operation sequence, proposal statuses stay in {pending, approved, denied, done} and transitions occur only from the allowed source status.

## Web terminal and workflow (R8, R9)
- **Hub**: `ChatMessage`, `transcript` deque(300), `record()` sanitizes; `say()` records every bot message before forwarding to IRC. `commands_run` counts handled commands. `workflow()` derives step state from existing hub data.
- **API**: `/api/chat`, `/api/terminal` (identity `WEB_NICK = "dashboard"`, never client-supplied), `/api/workflow`, plus an HTTP middleware rejecting cross-origin mutating requests.
- **Single action path**: dashboard buttons send the same `!` commands, so one `is_operator` check covers IRC and web. Transcript text is returned only as JSON and rendered with textContent.
## Addendum: per-run proposal ids (R11)
- **Hub**: `run_tag` (injectable, default 3 random characters from `RUN_TAG_ALPHABET`) is part of every proposal id (`p<n>-<tag>`). The in-memory store is unchanged, so after a restart old ids simply miss the lookup and give 404 or "no proposal". `stats()` adds `run_id` and `started_at`.
- **Agent**: mesh-responder and the remediation-loop skill re-call get_proposal immediately before acting and compare status, action and incident_id; unknown ids mean the hub restarted.
- P10: for any valid tag, proposal ids are unique within a run, have the form `p<n>-<tag>`, and round-trip through parse_command; generated tags always match the alphabet and length.
