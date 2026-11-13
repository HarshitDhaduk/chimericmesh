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
