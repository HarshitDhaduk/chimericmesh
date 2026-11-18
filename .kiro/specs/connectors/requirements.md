# Requirements: connectors (V1)

Capability map: [docs/v1-capability-map.md](../../../docs/v1-capability-map.md) (module `connectors`, depends on: `services` and `store` directly (for tail offsets); effort 2.25 days; no cuts are applied, contingency items 1 and 2 in the map would save 0.5 and 0.4 days). `connectors` calls `services`. `services` never imports `connectors`: `connectors` subscribes to service changes and supplies the status provider and the format list at startup (`services` R10).

## Baseline

[legacy-mesh](../legacy-mesh/requirements.md) R1 (legacy ingestion, parsers never raise), R6 (IRC commands), R10 (real-service demo). Existing listeners and parsers stay as they are.

## Introduction

The hub accepts Syslog over UDP, Telnet lines and IRC messages. Many outdated servers do none of these: Apache, nginx, IIS, vsftpd, Sendmail, Postfix and Exim write log files, and some syslog senders use TCP. This module adds the intake for those cases: Syslog over TCP, log-file tailing with rotation handling, and the format parsers for common legacy servers. It does not wrap or launch applications (decision D2). Every event leaves a connector tagged with its service through `services.resolve_service`.

## Objective

Any app that logs to a file in a well-known format, or sends syslog over UDP or TCP, can feed the hub without changes to the app. Intake never crashes the hub on bad input and never floods it.

Threat model: file contents and network bytes are untrusted. Parsers are pure and total. A log path chosen by the user is trusted to exist, but the hub must not hang on special files, must not follow a path the user did not register, and must not leak file contents anywhere except the local hub.

## Candidate evaluation (in or out of V1)

| Candidate | Decision | Reason |
|---|---|---|
| Syslog/UDP RFC 3164 and 5424 | Existing | R1.1. Only gains service tagging. |
| Telnet, IRC | Existing | R1.2, R1.3. Only gain service tagging. |
| Syslog/TCP (RFC 6587 octet counting and LF framing) | In | About 0.5 day. Common for rsyslog and syslog-ng forwarding, avoids UDP truncation, reuses `parse_syslog`. First item on the map's contingency list. |
| Log-file tailing with rotation | In | The only way to reach most legacy servers (D2). About 0.75 day. |
| Syslog-style text (Sendmail, Postfix, Exim, vsftpd and ProFTPD via syslog, BIND, Samba) | In | Existing `parse_syslog` fallback handles lines without PRI (README: replay of real logs). Adds mail-status hints only. |
| NCSA common and combined (Apache, nginx, Squid with httpd emulation) | In | Simple regex; very common. |
| W3C extended (IIS) | In | `#Fields:` directive parsing; IIS is the default web server on Windows (the developer's OS). Contingency list item 2. |
| FTP xferlog (vsftpd, wu-ftpd, ProFTPD) | In | Fixed field layout; small. Contingency list item 2. |
| `plain` (one line, severity inferred) | In | Fallback for formats not listed (for example Squid native, custom app logs). |
| SNMP v1/v2c traps | Deferred | BER decoding with stdlib is about 1 day; port 162 needs privilege (an unprivileged default of 5162 plus forwarding advice would be needed). First to restore if time allows. |
| RELP | Deferred | Needs a session handshake and acks; TCP syslog covers rsyslog and syslog-ng. |
| Windows Event Log | Deferred | `wevtutil qe /f:xml` polling is feasible with stdlib, but needs XML parsing, bookmarks and channel permissions (about 1 day). IIS and app logs are covered by file tailing. |

## Requirements

### 1. Common connector contract
**User story:** As a developer, I want every intake path to behave the same way, so that I can trust and test them uniformly.

1. THE SYSTEM SHALL pass every event through one function `submit(event, log_source_id=None)` that resolves the service (`services` R4), then calls `hub.ingest`. [Auto]
2. THE SYSTEM SHALL keep each file source's runtime status (`running`, `missing`, `unreadable`, `stopped`, `error`), counters (`lines_read`, `events_emitted`, `lines_dropped`, `bytes_read`, `unparsed`) and last error text (capped at 200 characters), and SHALL register a function that returns them with `registry.set_status_provider` at startup. `services` R5.1 puts them in `GET /api/services/{id}` under each log source; `services` does not import this module. [Auto]
3. WHEN a connector raises an exception while handling one line or one connection, THE SYSTEM SHALL log it at warning level, count it as `lines_dropped`, and keep serving other lines and connections. [Auto]
4. THE SYSTEM SHALL start and stop connectors with the hub's lifespan and SHALL release sockets and file handles on shutdown. [Auto]
5. THE SYSTEM SHALL keep the parsers pure functions in `src/chimericmesh/parsers/` that never raise on arbitrary text or bytes and return severity within 0 to 7. A parser that needs state across lines (`w3c` needs the `#Fields:` line) SHALL get it in an explicit `context` argument that the tailer owns, so the parser stays a pure function of `(line, context)`. [Auto] [PBT]
6. THE SYSTEM SHALL register with `registry.subscribe` (`services` R10) and, on a created, edited or deleted file source or service, start, restart or stop the matching tailer from the registry's `after_commit` callback (`services` R10.1), never inside the store transaction, so a rolled-back registry change never leaves a tailer running or stopped. THE SYSTEM SHALL also reconcile its running tailers with the registry at startup and every 30 seconds (cheap comparison of source ids, paths and `enabled`), so a failed `after_commit` call is repaired within 30 seconds. [Auto: a failing second `on_*` subscriber leaves no tailer changed; a skipped `after_commit` is repaired by reconcile] THE SYSTEM SHALL pass the list of its format names to the registry at startup (`known_formats`). [Auto]

### 2. Syslog over TCP
**User story:** As an operator with rsyslog or syslog-ng forwarding over TCP, I want the hub to accept it, so that long messages are not truncated like UDP.

1. THE SYSTEM SHALL listen for syslog on TCP on the bind address (default `127.0.0.1`) at `CHIMERICMESH_SYSLOG_TCP_PORT` (default 5514, the same number as UDP; they are separate sockets), unless that variable is set to `0`, which disables the TCP listener. IF the port is already in use, THEN THE SYSTEM SHALL log one warning naming the port and start without the TCP listener (B15). [Auto]
2. WHEN a connection sends frames of the form `<length> <message>` (RFC 6587 octet counting), THE SYSTEM SHALL parse each message with `parse_syslog` and ingest it. [Auto] [PBT]
3. WHEN a connection sends LF-terminated lines (non-transparent framing), THE SYSTEM SHALL parse each line the same way. [Auto] [PBT]
4. THE SYSTEM SHALL detect the framing per connection from the first bytes: octet counting when the connection starts with 1 to 4 digits followed by a space; otherwise LF framing. A first line that starts with a digit but has no space after at most 4 digits (for example an ISO timestamp) SHALL therefore be handled as LF framing and SHALL NOT close the connection. [Auto] [PBT]
5. IF a frame announces a length above 8192 bytes, or is not a valid number, THEN THE SYSTEM SHALL close that connection, count one `lines_dropped`, and keep other connections. [Auto] [PBT]
6. THE SYSTEM SHALL allow at most 64 concurrent connections and close any connection idle for 60 seconds. WHEN the limit is reached, THE SYSTEM SHALL refuse new connections and count them. [Auto]
7. THE SYSTEM SHALL use the peer IP as the default host when the message carries none, as UDP does. [Auto]
8. WHEN bytes are not valid UTF-8, THE SYSTEM SHALL decode with replacement characters. [Auto] [PBT]

### 3. File tailing
**User story:** As a developer, I want to point the hub at a log file of an old server, so that its errors appear without changing the app.

1. WHEN a file log source is enabled, THE SYSTEM SHALL poll the file every 500 ms (stdlib only, no new dependency) and emit one event per complete line. [Auto]
2. WHEN a source is attached for the first time, THE SYSTEM SHALL start at the end of the file and emit only new lines. WHERE `backfill_lines` is set (0 to 1000, default 0), THE SYSTEM SHALL first emit that many most recent lines. [Auto]
3. THE SYSTEM SHALL read by path: on each poll it opens the file, seeks to the stored offset, reads, and closes it, so that no handle stays open between polls (see 3.8). WHEN the file identity changes (rename-and-recreate rotation) or the size becomes smaller than the stored offset (copy-truncate), THE SYSTEM SHALL read the new file from its start. THE SYSTEM SHALL NOT emit a line twice and SHALL NOT lose a line that was complete and already read before the rotation. Lines appended to the old file after the last poll and before a rename can be lost; this is bounded by one poll interval (500 ms) and documented. [Auto] [PBT: appends, polls and rotations interleaved; every line read before a rotation appears exactly once, in order, and the lost lines are only those written after the last poll before a rotation]
4. WHEN a write ends without a newline, THE SYSTEM SHALL hold the partial text until the newline arrives, up to 8192 bytes, and then emit it truncated with a `[truncated]` suffix. [Auto] [PBT]
5. THE SYSTEM SHALL persist the read offset and file identity per log source in a table this module adds through the store's migration registry (`tail_offsets`), at least every 5 seconds and on shutdown. WHEN the hub restarts and the identity matches and the size is at least the offset, THE SYSTEM SHALL resume at the offset, otherwise start at the beginning of the new file. [Auto]
6. IF the file does not exist, THEN THE SYSTEM SHALL set status `missing`, retry every 5 seconds, and start reading from the beginning when it appears. [Auto]
7. IF the file cannot be read (permission denied), THEN THE SYSTEM SHALL set status `unreadable` with the OS reason, retry every 30 seconds, and SHALL NOT prompt for elevation. [Auto]
8. THE SYSTEM SHALL hold a file open only during one read (3.3), read-only, so that the owning app can keep writing and, on Windows, rename or delete the file for rotation (Python's `open()` on Windows does not allow rename of a file that stays open). [Auto: rename the file between polls on every platform and assert the rename succeeds] [Manual on Windows with an IIS-style rotator]
9. THE SYSTEM SHALL refuse to tail a path that is not a regular file when it is opened (directory, device, named pipe) and set status `error`. [Auto]
10. THE SYSTEM SHALL decode lines as UTF-8 with replacement characters, and strip NUL and other control characters except tab from the message. [Auto] [PBT]
11. THE SYSTEM SHALL limit each source to 2000 lines per second. WHEN exceeded, THE SYSTEM SHALL drop the surplus lines, count them in `lines_dropped`, and emit one summary event per 10 seconds with severity 4 and the text `connector dropped N lines (rate limit)` for that service. [Auto]
12. THE SYSTEM SHALL read at most 1 MiB per poll per source so one busy file cannot starve the others. [Auto]
13. WHILE a source is `missing` or `unreadable`, THE SYSTEM SHALL still show it in the service's status so the user can fix the path. [Auto]

### 4. Format parsers
**User story:** As a developer, I want each log format turned into a sensible severity and message, so that errors become incidents.

1. THE SYSTEM SHALL register formats `syslog-text`, `ncsa`, `plain`, `w3c` and `xferlog`, each a pure function `(line, context) -> Event | None` where `None` means "skip this line" (comments, blanks) and `context` is the tailer-owned state (R1.5). [Auto] [PBT]
2. WHEN the format is `syslog-text`, THE SYSTEM SHALL parse RFC 3164-like lines without a PRI header (for example `Nov 18 10:00:01 host postfix/smtp[123]: ...`), taking host, app and message from the line, and infer severity from the text. WHERE the text contains `status=deferred`, THE SYSTEM SHALL use severity 4, and `status=bounced` or `rejected`, severity 3. [Auto]
3. WHEN the format is `ncsa`, THE SYSTEM SHALL parse Common and Combined Log Format, and map the HTTP status: 5xx to severity 3, 408 and 429 to 4, other 4xx to 5, everything else to 6. The message SHALL be `<method> <path> -> <status>` with the query string removed. [Auto] [PBT]
4. WHEN the format is `w3c`, THE SYSTEM SHALL read the `#Fields:` directive to learn the column order, apply the same status mapping on `sc-status` (and treat a non-zero `sc-win32-status` as severity 4 at least), and skip other `#` directive lines. IF a data line arrives before any `#Fields:` line, THEN THE SYSTEM SHALL fall back to `plain` for that line. [Auto] [PBT]
5. WHEN the format is `xferlog`, THE SYSTEM SHALL parse the standard fields (current time, transfer time, remote host, bytes, filename, transfer type, direction, access mode, username, service, authentication method, user id, completion status), and map completion status `i` (incomplete) to severity 4 and `c` to 6. [Auto]
6. WHEN the format is `plain`, THE SYSTEM SHALL use the line as the message, `host` and `app` as in 4.10, and `infer_severity` from `events.py`. [Auto]
7. THE SYSTEM SHALL truncate messages to 4096 characters and SHALL strip the client query string and credentials-looking fragments (`password=...`, `token=...`, `Authorization: ...`) from NCSA and W3C messages, replacing the value with `<redacted>`. [Auto] [PBT]
8. IF a line does not match the declared format, THEN THE SYSTEM SHALL emit it using `plain` rules with severity 6 or the inferred severity, and SHALL count it in a `unparsed` counter. [Auto]
9. THE SYSTEM SHALL set the event's `source` to `file` for file connectors, `syslog` for both syslog transports, and keep `telnet` and `irc` as they are. THE SYSTEM SHALL add `file` to the allowed `Source` values and a `log_source_id` field to events (B7). [Auto]
10. **Host and app of file events.** THE SYSTEM SHALL set the event `host` of every file-sourced event to the log source's optional `host` field when present, otherwise `<machine>/<service-id>` where `<machine>` is `socket.gethostname()` (for a source whose service is `unassigned`, just `<machine>`). Reason: the incident id is built from host and signature, so two file-based services with the same failing text get different incidents and one service's rule or code folder never governs the other's incident (`services` R4.5). THE SYSTEM SHALL set `app` to the log source's service's first `app_names` entry, otherwise the file's base name without extension. The remote client address of an access-log line SHALL NOT become the `host`. It stays only in `raw` (truncated), so one buggy URL requested by many clients is one incident. [Auto] [PBT]
11. WHEN the same failing request path (HTTP 500) appears in an `ncsa` file from three different client IPs, three times each, THE SYSTEM SHALL create exactly one incident, with host `<machine>/<service-id>`. WHEN two services each tail a file with the same failing text, THE SYSTEM SHALL create two incidents, one per service. [Auto]
12. WHEN `syslog-text` lines carry a host name, THE SYSTEM SHALL set the event `host` to `<line host>/<service-id>` (for a source whose service is `unassigned`, just `<line host>`), so the line's host is kept readable and two services tailing mail logs on one machine never share an incident (`services` R4.5). WHEN the line has no host, THE SYSTEM SHALL use rule 4.10. WHEN `ncsa`, `w3c`, `xferlog` or `plain` formats are used, rule 4.10 alone applies. [Auto] [PBT]

### 5. Existing listeners gain tagging (R1.1 to R1.3 unchanged)
**User story:** As an operator already sending syslog, Telnet or IRC to the hub, I want nothing to change except that my events can be tagged with a service.

1. WHEN a UDP syslog datagram, a Telnet line or an IRC message is ingested, THE SYSTEM SHALL resolve its service through `submit` and otherwise behave exactly as before. [Auto]
2. THE SYSTEM SHALL keep the 4 MB UDP receive buffer and the per-datagram line splitting. [Auto]
3. THE SYSTEM SHALL keep `POST /api/ingest` and `POST /api/demo/storm` working unchanged in input and output. Their events go through `submit` and are tagged by the same rules as network events (`services` R4); a demo-storm event with no matching service is `unassigned` (map B21). [Auto]

### 6. Safety limits
**User story:** As a developer, I want a misbehaving log or client to be unable to take the hub down.

1. THE SYSTEM SHALL bound memory per connector: partial-line buffer 8192 bytes, TCP frame 8192 bytes, one poll 1 MiB. [Auto]
2. THE SYSTEM SHALL never write file contents or event text to any socket other than the hub's own IRC announcements (which pass through the injection-safe formatter, R3.2) and configured webhooks. [Auto: review plus canary test]
3. THE SYSTEM SHALL NOT execute, import or evaluate any text read from logs. [Auto: static check for `eval`, `exec`, `subprocess` in `parsers/` and the tailer]

## Boundaries

- Always: keep parsers pure and total; bound every buffer; persist offsets; tag events through `submit`; add a Hypothesis test for each new parser's totality.
- Ask first: a new runtime dependency (`watchdog`, `pysnmp`); opening ports other than the existing ones plus syslog TCP; adding Windows-only code paths that spawn processes.
- Never: wrap, launch or capture stdout of the user's application (D2); read files that are not registered log sources; follow a path from event text; bind to `0.0.0.0` by default.

## Success Criteria

1. Append a 5xx Apache combined line to a registered file: within 2 seconds an event with severity 3 appears for the service. Three such lines open an incident.
2. Append numbered lines, wait for the tailer to read them, rotate the file (rename and recreate, then copy-truncate in a second run), append more: every line arrives exactly once, in order, and the rename is never blocked by the tailer on Windows. When lines are written during the 500 ms before a rename, at most those lines may be missing and none is duplicated.
3. Send a 100 KB frame over TCP syslog: the connection closes, other connections are unaffected, and `lines_dropped` is 1.
3a. Two services tailing `syslog-text` files with the same failing line from one machine give two incidents (R4.12). Three client IPs requesting the same 500 path in an `ncsa` file give one incident (R4.11).
4. Feed each parser 10,000 arbitrary strings: no exception, severity always within 0 to 7 (Hypothesis).
5. Existing UDP, Telnet, IRC and `legacy_app` behaviour tests pass unchanged (the only two existing tests V1 changes are listed in the map and do not belong to this module). The table-driven import test (capability map, "Dependency direction") passes: `connectors` imports only `core`, `services` and `store`.
6. Manual: on the Windows development machine, tail an IIS-style W3C file and an Apache-style file while another process appends to and rotates them.

## Out of Scope (Deferred)

- SNMP v1/v2c traps: about 1 day of BER decoding and a privileged-port story; first item restored if there is slack.
- RELP: needs a handshake; TCP syslog covers the common senders.
- Windows Event Log: needs XML parsing and bookmarks; IIS and app file logs cover the demo.
- Syslog over TLS (RFC 5425): needs certificates and the `ssl` module configuration; the hub is local only (D1).
- Telnet or IRC clients that the hub dials out to: intake is passive.
- Auto-detecting the format from file contents: explicit format per source keeps behaviour predictable (discovery proposes a format).
- Multi-line log records (Java stack traces, Postfix queue messages spanning lines): each line is its own event in V1.
- Compressed or already-rotated archives (`.gz`): only the live file is tailed.
- Remote file shares and `tail -F` over SSH: D1, local only.

## Open Questions

1. Resolved: a 500 ms polling loop is acceptable, because the stdlib has no portable notification API (accepted 2026-11-18).
2. Resolved: path-based open per poll, with no persistent handle and no `ctypes`. The NTFS rotation identity (rename versus copy-truncate) is a design-phase test item, see Notes for design (accepted 2026-11-18).
3. Resolved: `backfill_lines` is off by default, so connecting an old, noisy log does not open dozens of incidents at once (accepted 2026-11-18).

## Notes for design

Findings from the final review that design must resolve. No acceptance criterion changes for them.

- M2: `connectors` cannot import `hub`; reword R1.1 in design so `submit` calls an injected `ingest` callable, and add one criterion that runs the connectors against a plain `Hub()` and against the store-backed hub.
- NTFS rotation identity (former Open Question 2, kept as a design-phase test item): test on Windows whether `os.stat().st_ino` is stable across IIS rotation, covering rename and copy-truncate (size shrinks). The decision stays path-based open per poll; the cost is that lines written just before a rename can be missed.
- L1: `Event` is frozen; `submit` returns the tagged event (it sets `service_id` and `log_source_id`, and ingest assigns `id`) (R1.1, map B19).
- L8: split compound criteria into sub-numbered criteria, starting with R3.3.
