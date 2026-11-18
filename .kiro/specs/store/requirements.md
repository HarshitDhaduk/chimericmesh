# Requirements: store (V1)

Capability map: [docs/v1-capability-map.md](../../../docs/v1-capability-map.md) (module `store`, depends on: none, effort 1.5 days). Also owns the columns for the proposal fields that `auto-approval` and `agent-loop` use (B7).

## Baseline

[legacy-mesh](../legacy-mesh/requirements.md) R2 (incidents), R7 (proposals, in memory, max 200), R8 (transcript), R3 (announce once per incident), R9 (guided workflow). This spec extends R7.5, R2 and R3.1 (see B1, B2, B9, B10, B14 and B16 in the map). `store` depends on no other module (only the shared `core` types), and a static test fails when it imports one (map, "Dependency direction").

## Introduction

Today the hub keeps everything in memory. A restart loses incidents, proposals and counters, and proposal ids restart at `p1`, so `p1` can mean two different proposals. The `store` module adds a local SQLite database (Python stdlib `sqlite3`) that holds events (bounded), incidents, proposals, an append-only audit log and durable counters. It makes ids sequential and never reused (decision D5) without changing the id format. Nothing leaves the machine.

## Objective

Make hub state survive restarts, keep proposal ids `p1`, `p2`, ... unique forever, and give other modules one safe place to persist data, with bounded growth and without slowing event intake.

Threat model: the database file contains log text, which is untrusted and may contain secrets. It stays on the local disk, readable by the user only where the OS allows. SQL is built only with parameters. Log text is data and never executed or interpolated into SQL. The agent runs as the same OS user, so owner-only file permissions do not keep the database or the token file from it. They protect against other OS users, not against the agent. The home directory sits outside the workspace and the agent profile denies it (`agent-loop` R5.2), which is a second layer and not a guarantee (map, "Honest limits of the access model").

## Requirements

### 1. Database location and lifecycle
**User story:** As a developer, I want the hub to keep its data in one local file, so that I know where my data is and can delete it.

1. WHEN `create_app` starts without an explicit store, THE SYSTEM SHALL open the SQLite file at `CHIMERICMESH_DB`. WHEN the variable is unset, THE SYSTEM SHALL use `chimericmesh.db` inside the per-user home directory `CHIMERICMESH_HOME` (default `~/.chimericmesh/`, an absolute path), and create parent directories if needed. THE SYSTEM SHALL resolve the path to an absolute path at startup and SHALL NOT depend on the working directory. [Auto]
2. THE SYSTEM SHALL NOT default the database, or any file in the home directory, to a path under the current working directory. A test SHALL start the app from two different working directories and assert that both open the same file. [Auto]
3. WHEN `CHIMERICMESH_DB` is `:memory:`, THE SYSTEM SHALL use an in-memory database with identical behaviour. The admin token for such a run lives in process memory only (`services` R6.1); no file is written. [Auto]
4. **One definition of in-memory versus stored state (map B1).** (a) WHEN a `Hub` is constructed without a store argument, THE SYSTEM SHALL behave as in the baseline: events, incidents and proposals in memory, no file created, proposals evicted at `MAX_PROPOSALS` (200), the existing eviction test (`test_irc_commands.py`) unchanged, and proposal ids `p1`, `p2`, ... restarting at `p1` on every process start (documented next to R3.9). (b) WHEN `create_app(hub)` receives such a hub (every existing test), THE SYSTEM SHALL attach a private `:memory:` store used only for the service registry, tail offsets, approval rules, audit rows and counters of the new modules (`services` R1.7, `auto-approval` R1.8); events, incidents and proposals stay in the hub's memory. (c) WHEN `create_app()` is called without a hub, or `python -m chimericmesh` runs, THE SYSTEM SHALL create a file-backed store and a store-backed hub. The audit rows of reopen (R5.7) are written to whichever store the app has. [Auto]
5. WHEN the database file or the home directory is created on a POSIX system, THE SYSTEM SHALL set mode `0600` (files) and `0700` (directory). On Windows, THE SYSTEM SHALL remove inherited permissions and grant only the current user, using `icacls` when available. All of this lives in one module, `chimericmesh/fsperms.py`, which, together with `discovery`, is the only code allowed to start a process (the allow-list test is `agent-loop` R4.10). WHEN the OS does not support either, THE SYSTEM SHALL log one warning and continue. Residual risk: the Windows ACL step is not tested automatically. [Auto: POSIX] [Manual: Windows ACL]
6. IF the database file exists but is not a valid SQLite database, THEN THE SYSTEM SHALL refuse to start with an error naming the file, and SHALL NOT delete, overwrite or rename it. [Auto]
7. WHEN the hub shuts down, THE SYSTEM SHALL flush pending writes and close the connection within 5 seconds. [Auto]

### 2. Schema versioning and migrations
**User story:** As a developer, I want upgrades to keep my data, so that I never lose history after updating.

1. THE SYSTEM SHALL record the schema version with `PRAGMA user_version` and apply registered migrations in order inside one transaction each. [Auto]
2. WHEN a module registers a migration through the store's migration registry, THE SYSTEM SHALL apply it exactly once per database. [Auto]
3. IF the database `user_version` is higher than the highest known migration, THEN THE SYSTEM SHALL refuse to start and SHALL NOT modify the file. [Auto]
4. IF a migration raises, THEN THE SYSTEM SHALL roll that migration back, leave `user_version` unchanged and refuse to start with the migration name in the error. [Auto]
5. WHEN a database is opened that has never been migrated, THE SYSTEM SHALL create the core tables: `events`, `incidents`, `proposals`, `audit_log`, `counters`. [Auto]

### 3. Durable sequential proposal ids (D5)
**User story:** As a developer, I want proposal ids `p1`, `p2`, `p3` that are never reused, so that I can say "approve p7" and it always means the same proposal.

1. WHEN a proposal is created, THE SYSTEM SHALL assign the id `p<n>` where `n` is the next value of the durable counter `proposal`, incremented in the same transaction that inserts the proposal (for an in-memory `Hub()`, an in-process counter under a lock). [Auto] [PBT]
2. THE SYSTEM SHALL keep the id format exactly `p` followed by decimal digits, with no suffix or tag. [Auto]
3. WHEN the hub restarts, THE SYSTEM SHALL continue the sequence from the stored counter, so the next id is greater than every id issued before. [Auto] [PBT]
4. WHEN a proposal row is deleted or trimmed, THE SYSTEM SHALL NOT decrease the counter, so the id is never issued again. [Auto] [PBT]
5. THE SYSTEM SHALL issue the next id as `max(counter, highest stored proposal number) + 1`, so a counter that is lower than the stored maximum (for example in a restored database) or a stored maximum lower than the counter (after a manual delete) never yields a reused id. [Auto] [PBT]
6. WHEN two proposals are created concurrently, THE SYSTEM SHALL give them distinct ids. [Auto] [PBT]
7. THE SYSTEM SHALL expose the same counter mechanism by name so other modules can issue `r<n>` rule ids and service-independent ids without a second implementation. [Auto]
8. THE SYSTEM SHALL NOT adopt the `p<n>-<tag>` format from the unmerged branch `fix/proposal-ids-and-recheck`. A static test SHALL scan `src/`, `tests/`, `powers/` and `.kiro/agents/` for a proposal id of the form `p<digits>-<tag>` and fail when one is found. [Auto]
9. WHEN the user deletes the database file, THE SYSTEM SHALL start a new sequence at `p1`. This is a documented limitation of D5: ids are unique per database. The README and the startup log line that prints the database path SHALL say so. The same applies to `:memory:` databases and to an in-memory `Hub()` (R1.4): the sequence restarts at `p1` on every process start. [Auto: README text test]

### 4. Proposal persistence
**User story:** As a developer, I want proposals and their decisions to survive a restart, so that an approved proposal is still approved when the agent comes back.

1. WHEN a proposal is created, decided or completed, THE SYSTEM SHALL persist it before the REST or MCP call returns. [Auto]
2. WHEN the hub restarts, THE SYSTEM SHALL return every stored proposal with the same id, status, `decided_by`, timestamps, action and details. [Auto] [PBT]
3. THE SYSTEM SHALL enforce this complete transition table in the store layer, so no code path can skip it: `pending` to `approved`; `pending` to `denied`; `approved` to `done`; `approved` to `denied` only when the caller is the hub's own rule logic (an internal argument, never a REST field), `decision_origin` is `hub`, and `decided_by` is `rule-revoked:<id>`, `rule-expired:<id>` or `rule-suspended:<id>`. `denied` and `done` are final. Every other transition is rejected with an error and changes nothing. The table tests SHALL cover every pair of statuses, the three reserved `approved` to `denied` cases, and the same `approved` to `denied` pair with a caller-supplied `decided_by` that starts with a reserved prefix (rejected). [Auto] [PBT]
4. WHILE the hub is store-backed, THE SYSTEM SHALL keep all proposals (no eviction at 200). WHEN `GET /api/proposals` is called, THE SYSTEM SHALL return the newest 200 by default, ordered oldest first within that window as the baseline does, and SHALL accept `limit` between 1 and 500. The in-memory `Hub()` keeps the cap (R1.4). THE SYSTEM SHALL also accept an optional `service_id` filter on `GET /api/proposals` (value `unassigned` selects incidents with no service), applied **before** the limit, so a per-service view never loses older proposals. [Auto]
4a. THE SYSTEM SHALL accept the same optional `service_id` filter and an optional `status` (`open`, `resolved`) on `GET /api/incidents`, applied before its limit, and SHALL return `service_id` and `opening` on each incident. This module owns both filters; `dashboard` and `agent-loop` use them. [Auto]
5. THE SYSTEM SHALL keep the baseline limits: action up to 200 characters, details up to 500 characters, completion note (`result_note`) up to 300 characters. THE SYSTEM SHALL cap `decided_by` at 60 characters (baseline 30) so that `withdrawn:<nick>` and `rule-revoked:<id>` are not truncated. [Auto] [PBT]
6. THE SYSTEM SHALL store proposal fields added by other modules (`kind`, `files`, `estimated_lines`, `verification`, `approved_by_rule`, `incident_opening`, `files_changed`, `lines_changed`, `verification_result`) and `decision_origin` (R4.7) as nullable columns, with meaning and validation left to `auto-approval` (B7), so rows written by older versions still load. THE SYSTEM SHALL keep using the existing `result_note` column for the completion note and SHALL NOT add a second note column. [Auto]
7. THE SYSTEM SHALL define "approved by a rule" only by a non-null `approved_by_rule` column, which only the hub's rule logic can set. THE SYSTEM SHALL record on every decision a `decision_origin` column: `user` for decisions made through `/decide`, IRC or the terminal, and `hub` only for decisions written by the hub's own logic (rule revocation, suspension and expiry, withdrawal, proposal expiry); the origin is an internal argument and never a REST or IRC field. `decided_by` is display text and SHALL NOT be used to decide who approved or who denied. An IRC nick that starts with `expired` therefore stays `decision_origin: user`. [Auto] [PBT]
8. THE SYSTEM SHALL compute the guided-workflow flags (R9) from state created since the process started (per run), so a restart with persisted proposals does not mark `propose` or `decide` as done (B16). [Auto]

### 5. Incident persistence
**User story:** As a developer, I want incidents and their counts to survive a restart, so that the hub does not re-announce old incidents or forget resolved ones.

1. WHEN an incident opens, changes status, or reaches a count milestone (10, 50, 100, 500) or every 30 seconds while it keeps receiving events, THE SYSTEM SHALL persist (a hard crash can lose up to 30 seconds of count growth; only a graceful stop persists the exact count, which Success Criterion 2 assumes) its id, host, service id, category, signature, severity, count, first and last seen, sample, resolved flag and announced flag. [Auto]
2. WHEN the hub restarts, THE SYSTEM SHALL reload incidents into the tracker with their stored counts, SHALL keep resolved incidents resolved, and SHALL NOT re-announce an incident already announced (R3.1). [Auto]
3. WHEN a restarted hub receives a new event for a reloaded incident, THE SYSTEM SHALL add it to the stored count, not restart at 1. [Auto] [PBT]
4. THE SYSTEM SHALL keep the existing incident id algorithm (first 10 hex characters of SHA-1 of `host|signature`). [Auto]
5. WHEN more than 5000 incident clusters exist, THE SYSTEM SHALL keep the baseline eviction (`MAX_CLUSTERS`) in memory and SHALL keep the rows of incidents that qualified as open. [Auto]
6. THE SYSTEM SHALL also persist for each incident `resolved_at` (timestamp or null), `opening` (integer, 1 for the first time it opened) and `opened` (boolean). `IncidentTracker.qualifies` and `get` SHALL treat an incident as open when `opened` is true, in addition to the baseline test (severity at most 2, or count at least 3). THE SYSTEM SHALL set `opened` the first time the baseline test passes and keep it until the incident is evicted. [Auto]
7. **Reopen (B14, confirmed by the user, default 300 seconds).** WHEN an event arrives for a resolved incident (same host and signature, hence the same id) and at least `CHIMERICMESH_REOPEN_AFTER_SECONDS` (integer 0 to 86400, default 300) have passed since `resolved_at`, THE SYSTEM SHALL in one transaction: clear the resolved flag and `resolved_at`, increment `opening`, set the count to 1, set `opened` to true (so a reopened severity 3 or 4 incident with count 1 is still returned by the tracker, `get_incident`, `create_proposal` and the watch list), reset the incident `severity` to the severity of the reopening event (the baseline keeps the minimum over all events; keeping an old crit value would make the incident permanently unapprovable by rules), clear the announced flag, write an audit row (`incident_reopened`, incident id, new opening number, service id) and announce `REOPENED <id>: <category> on <host>` once on IRC and in the transcript (same injection-safe formatter as R3.2). This module is the only owner of that announcement: one reopen produces exactly one `REOPENED` line, and no other module announces it (project rule: one announcement per incident opening). [Auto] The incident is then open and unresolved, so the normal announcement and the watch-mode work list (`agent-loop` R4.3) see it again. [Auto] [PBT]
8. WHEN an event arrives for a resolved incident less than `CHIMERICMESH_REOPEN_AFTER_SECONDS` after `resolved_at`, THE SYSTEM SHALL add it to the stored count and to the event log, SHALL NOT reopen the incident, and SHALL NOT announce. [Auto] [PBT]
9. THE SYSTEM SHALL apply 5.7 and 5.8 also to an in-memory `Hub()` (same logic, no database), and an injectable clock SHALL make both testable without waiting. WHEN the hub restarts, `resolved_at` is reloaded, so the window continues across restarts. [Auto]
10. THE SYSTEM SHALL record on each proposal the `incident_opening` current when it was created. Limits that count "per incident" in `auto-approval` count per opening (`auto-approval` R4.2(h)). [Auto]
11. IF `CHIMERICMESH_REOPEN_AFTER_SECONDS` is not an integer in range, THEN THE SYSTEM SHALL log a warning and use 300. [Auto]

### 6. Bounded event storage (B9)
**User story:** As a developer, I want the log history to stay within a size I choose, so that the database never fills my disk.

1. THE SYSTEM SHALL assign every event an integer `id` at ingest (map B19): one in-process counter, strictly increasing, seeded at startup from `max(events.id)` of a store-backed hub and starting at 1 for an in-memory `Hub()`. The id is assigned before the batched write (R7.1), so the in-memory ring and the stream can use it at once, and it is the primary key of the stored row, stable across restarts. `Event.to_dict` and `GET /api/events` SHALL return it. [Auto] [PBT]
1a. THE SYSTEM SHALL store each ingested event with its id, timestamp, source, host, app, severity, message, service id, log source id and a truncated raw line. [Auto]
2. THE SYSTEM SHALL truncate `message` and `raw` to 4096 characters each when storing. [Auto]
3. THE SYSTEM SHALL keep at most `CHIMERICMESH_MAX_EVENTS` rows (default 100000) and no rows older than `CHIMERICMESH_EVENT_TTL_DAYS` days (default 7), removing the oldest first, in batches, at least once per minute. [Auto] [PBT]
4. WHEN the hub restarts, THE SYSTEM SHALL refill the in-memory log view (the hub's existing capacity of 2000 events, `Hub(capacity=2000)`) from the newest stored events. [Auto]
5. THE SYSTEM SHALL keep the cumulative counters of `/api/stats` (`total_events`, `total_by_source`) across restarts. [Auto]
6. WHEN a client asks for events after an id, THE SYSTEM SHALL return events with a greater id, oldest first, up to the limit (used by the dashboard stream and agent tools). [Auto] [PBT]

### 7. Write path does not hurt intake
**User story:** As a developer, I want a log storm to be recorded without dropped UDP datagrams, so that incident counts stay accurate.

1. THE SYSTEM SHALL perform database writes off the asyncio event loop (a single writer thread or executor) and SHALL group **event** inserts in batches of up to 500 rows or 250 ms; event writes are fire-and-forget. Writes of incidents, proposals, rules, the registry and audit rows are synchronous: the call returns only after the commit (R4.1), and callbacks run inside that open transaction (`services` R10). [Auto]
2. WHEN 5000 events are ingested at 1000 events per second, THE SYSTEM SHALL persist all of them within 5 seconds after the last event and SHALL NOT block the event loop for more than 100 ms at a time. [Auto: benchmark test, tolerant threshold]
3. IF the writer queue holds more than 20000 pending rows, THEN THE SYSTEM SHALL drop the oldest pending event rows, increment a `dropped_events` counter, log one warning per minute, and SHALL keep incidents, proposals and audit rows unaffected (those are never dropped). [Auto]
4. IF a database write fails (disk full, locked), THEN THE SYSTEM SHALL retry up to 3 times with backoff, then log at error level, keep the hub running, and surface `store_ok: false` in `/api/stats`. [Auto]
5. THE SYSTEM SHALL open SQLite in WAL mode with a busy timeout of 5 seconds. [Auto]

### 8. Audit log (append-only)
**User story:** As a developer, I want a tamper-resistant record of who approved what, so that I can answer "why did the agent change that file".

1. THE SYSTEM SHALL provide `audit.append(actor, action, subject, detail)` that inserts a row with an increasing id and a timestamp. [Auto]
2. THE SYSTEM SHALL NOT provide any update or delete operation for audit rows through the store API, REST or MCP. [Auto: API surface test]
3. THE SYSTEM SHALL cap `actor`, `action`, `subject` at 100 characters and `detail` at 1000 characters, and SHALL strip control characters. [Auto] [PBT]
4. THE SYSTEM SHALL NOT write the admin token, webhook URLs with credentials, or full log lines into `detail`. [Auto: test with canary strings]
5. WHEN the audit log is read, THE SYSTEM SHALL return rows newest first with `limit` (default 100, max 500) and an optional `before` id. [Auto]
6. THE SYSTEM SHALL keep audit rows when events are trimmed. Audit rows are only removed by the user deleting the database file. [Auto]

### 9. Transcript
**User story:** As a developer, I want the bounded transcript to behave as before.

1. THE SYSTEM SHALL keep the transcript (R8.1, R8.2) in memory with the baseline bounds. It SHALL NOT be persisted in V1. [Auto]

## Boundaries

- Always: use parameterized SQL; keep writes off the event loop; add a migration for every schema change; keep ids human-friendly and sequential; test restart behaviour with a temporary file database.
- Ask first: adding a dependency (SQLAlchemy, aiosqlite); changing the id format; storing anything new from log content outside `events`.
- Never: reuse a proposal id; delete or rewrite audit rows through any API; delete a user's database file automatically; send data off the machine (the baseline user-configured webhook, R3.3, is the only existing exception and is unchanged); build SQL by string formatting.

## Success Criteria

1. Create `p1` and `p2`, restart the hub on the same file, create another proposal: it is `p3`. Delete `p3` directly in SQL, restart, create another: it is `p4`.
2. Open an incident to 50 events, restart, send 5 more events: the incident count is 55 and no new IRC announcement is sent.
3. A 5000-event burst at 1000 events per second leaves 5000 rows (or 5000 minus the stated drops with `dropped_events` matching) and a maximum event-loop stall under 100 ms in the benchmark test.
4. With `CHIMERICMESH_MAX_EVENTS=1000`, the table never exceeds 1000 + one batch after the next trim, and incidents are untouched.
5. `python -m pytest -q` passes with no existing test changed by this module. V1 changes exactly two existing tests, listed in the map ("Existing tests that change"): the docs-vs-tools test (`agent-loop` R6.2) and `test_proposals_endpoint_fields` (`auto-approval` R9.7). Existing tests construct `Hub()` without a store, which keeps the 200-proposal cap (B2) and does not enforce the B3 limits, and call `create_app(hub, ...)` with an injected hub, so they run in memory without enforcement (B12) and with a private `:memory:` store (R1.4).
6. Resolve an incident, send a matching event 10 seconds later with `CHIMERICMESH_REOPEN_AFTER_SECONDS=300`: the incident stays resolved and its count grows. Send another after 301 seconds (fake clock): it reopens with count 1 and `opening` 2, one `REOPENED` line is announced and one audit row exists.
7. Property tests (design phase) cover: id uniqueness and monotonicity across restarts, status transition validity, audit field caps, event-after-id ordering, retention bounds, reopen only when the window has passed.
8. The table-driven import test (capability map, "Dependency direction") passes: `store` imports only `core` (events, incidents) and reaches the hub only through injected callables, for example `announce(text)` for the `REOPENED` line.

## Out of Scope (Deferred)

- Persisting the chat transcript: not needed for any V1 decision; restart clears it.
- Tamper-evident audit (hash chain, signing): the file is owned by the same user; revisit if the hub ever runs shared.
- Database encryption: the data is local and the user controls disk encryption; adds a dependency.
- Export and import of the database: not needed for the demo.
- Postgres or any server database: D1, everything local.
- Per-service retention settings: one global bound is enough for V1.

## Open Questions

1. Resolved: the `CHIMERICMESH_HOME` override is enough for people who want the data inside the repo (accepted 2026-11-18).
2. Resolved: drop the oldest pending event rows under overload and count them, because UDP syslog cannot be slowed down anyway (accepted 2026-11-18).
3. Resolved: store the event `raw` column, truncated, because the agent sometimes needs the original line (accepted 2026-11-18).

## Notes for design

Findings from the final review that design must resolve. No acceptance criterion changes for them.

- M1: assign `notify.py`, `replay.py`, `replay_irc.py` and every package `__init__.py` to a group in the map's dependency table; `store` owns the `store/*` package files and `fsperms.py`.
- M3: state the threading and transaction model: which thread runs a synchronous write and the subscriber callbacks, whether callbacks may take the hub lock, and add the incident-open commit to the R7.2 benchmark.
- M4: on reopen, set the announced marker to the current count so the baseline announce branch cannot fire a second line; add the test "reopen, two more events, exactly one announcement for that opening" (R5.7).
- L1: `Event` is frozen; choose how `Hub.ingest` assigns `id` (`dataclasses.replace` or a `compare=False` field) and make `submit` return the tagged event (R6.1).
- L5: exempt the guard test file from the `p<digits>-<tag>` static scan, or build the regex at runtime (R3.8).
- L7: the reserved-prefix check on `/decide` is broader than needed (a user named `ruleman` is refused); document it or tighten it (R4.3, B10).
- L8: split compound criteria into sub-numbered criteria, starting with R5.7.
