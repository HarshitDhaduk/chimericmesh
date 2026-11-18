# Implementation Plan: store (V1)

Requirements: [requirements.md](requirements.md). Design: [design.md](design.md). Property numbers below are the numbered Correctness Properties in the design.

Conventions for every task: write the tests first and watch them fail, then implement; run the new tests and `python -m pytest -q`; no task edits an existing test. Tasks marked `*` (Kiro's optional marker, `- [ ]*`) are optional property tests that may be skipped for a faster first pass; every requirement is also covered by a non-optional example test. Every property task **not** marked `*` is required. The optional ones are tasks 2.4 (properties 16, 17), 3.3 (2, 21), 7.5 (19) and 11.7 (15, 20). Required: 1, 3, 4 (5.3); 6, 7, 8 (5.4); 9, 10, 11 (7.4); 12, 13, 14 (8.4); 18 (4.2); 5 (11.6). Success Criterion 7 names 1, 3, 6, 10, 11, 12, 18 and the monotonicity of ids across restarts (property 5); all of them are required. Stack: Python 3.11+, stdlib `sqlite3` and `threading`, pytest, Hypothesis (already in the `dev` extra). New files under `src/chimericmesh/store/` unless a path is given. Each task touches at most five files.

Review round 3 (see "Review round 3 resolutions" in the design): behavioural findings are test-first sub-tasks with a letter suffix (2.2a, 9.4a, 11.2a, 11.2b, 11.3a, 11.3b) placed after the task they depend on, so no existing task number changed. Findings that are one-line changes to an existing task appear as a checklist line ending with the finding id, for example `- [ ] (L6) ...`. Within a sub-task, the failing test comes first and the implementation second.

- [x] 1. Store package skeleton and migration registry
  - [x] 1.1 Create the package skeleton and the migration registry
    - Write `tests/unit/test_store_registry.py` first: `register` accepts a version inside the module's block, rejects a version outside it, a duplicate version and a duplicate name (`MigrationError`); `migrations()` is sorted by version; `highest()` is 0 for an empty registry.
    - Create `store/__init__.py` (empty re-exports for now), `store/errors.py` (`StoreError`, `StoreOpenError`, `StoreCorruptError`, `SchemaTooNewError`, `MigrationError`, `StoreBusyError`, `StoreFullError`, `StoreWriteError`, `StoreClosedError`, `StoreReentryError`, `InvalidTransition`, `NotFound`), and `store/migrations.py` with `Migration`, `MODULE_BLOCKS` and `MigrationRegistry` (no runner yet).
    - Create `tests/conftest.py` with the autouse isolation fixture (sets `CHIMERICMESH_HOME` to a `tmp_path` directory, removes `CHIMERICMESH_DB`) and a `FakeClock` helper fixture.
    - _Requirements: 2.2_
  - [x] 1.2 Add `StoreConfig` and database path resolution
    - Write `tests/unit/test_store_config.py` first: default path is `<home>/chimericmesh.db` and absolute; `CHIMERICMESH_HOME` and `CHIMERICMESH_DB` overrides; `:memory:`; a relative `CHIMERICMESH_DB` is resolved once against the current directory; starting from two different working directories with no variables opens the same file; invalid `CHIMERICMESH_MAX_EVENTS`, `CHIMERICMESH_EVENT_TTL_DAYS` and `CHIMERICMESH_REOPEN_AFTER_SECONDS` warn and fall back (100000, 7, 300); range edges (0 and 86400 accepted for reopen, 86401 rejected).
    - Implement `store/config.py`: `StoreConfig.from_env(environ)`, `resolve_home`, `resolve_db_path`; pure functions, no file access.
    - _Requirements: 1.1, 1.2, 1.3, 5.11, 6.3_
  - [x] 1.3 Add the dependency-direction test with the file-to-group table
    - Write `tests/unit/test_dependency_direction.py`: the table from the design (root `__init__.py` and `parsers/*` are `core`; `store/*` and `fsperms.py` are `store`; `notify.py`, `replay.py`, `replay_irc.py`, `hub.py`, `api.py`, `commands.py`, `listeners.py`, `mcp_server.py`, `__main__.py` are `wiring`), the "may import" table, an AST import scan that counts `TYPE_CHECKING` imports, a failure for any file without a group, a check that `core` files import no third-party package (imports between `core` files are allowed, they are the same group), and a check that `httpx` is imported only by `wiring` files. Record the follow-ups for the next map revision as a comment in the test: the map's file-to-group table and the B10 / `auto-approval` R4.8 prefix list.
    - The test must already pass on the current tree plus the files from 1.1 and 1.2.
    - _Requirements: Success Criterion 8 (and the M1 resolution in the design)_

- [x] 2. File permissions, connection, migration runner and core schema
  - [x] 2.1 Implement `fsperms.py`
    - Write `tests/unit/test_fsperms.py` first: on POSIX, `secure_dir` gives mode 0700 and `secure_file` 0600; on Windows the command line is built without a shell and with the user name validated (test the builder with a fake runner, skip the real `icacls` call); a failing or missing `icacls` logs one warning and returns without raising; an unsupported platform logs one warning. Mark the real Windows ACL check as manual in a comment.
    - Create `src/chimericmesh/fsperms.py` (`secure_dir`, `secure_file`, `secure_sidecars`). It is the only file in this module that starts a process.
    - _Requirements: 1.5_
  - [x] 2.2 Implement the connection opener
    - Write `tests/unit/test_store_connection.py` first: a non-SQLite file raises `StoreCorruptError` naming the path and is byte-for-byte unchanged afterwards (not deleted, renamed or written); a zero-length file opens; a non-empty file shorter than 100 bytes raises `StoreCorruptError`; a database with `user_version` above the registry's highest raises `SchemaTooNewError`, and the fixture is a **WAL-mode** database (create it with `PRAGMA journal_mode=WAL`, then set `user_version` higher, then close) so the test fails if the guard opens any connection: the file bytes and the directory listing are identical before and after (no `-wal` or `-shm` file appears); a fake `sqlite3.sqlite_version_info` below 3.24 raises `StoreOpenError` naming the version; pragmas after open are `journal_mode=wal`, `busy_timeout=5000`, `synchronous=1`, `foreign_keys=1`; a new file is created with mode 0600 on POSIX; the parent directory is created when missing.
    - Implement `store/connection.py`: header guard that reads bytes only (`open(path, "rb")`: magic `SQLite format 3\0` in bytes 0..15, `user_version` as a big-endian integer at offset 60, no SQLite connection), pre-create with owner-only permissions, `open_connection` with `isolation_level=None`, the read-before-write ordering of pragmas from the design.
    - _Requirements: 1.5, 1.6, 2.3, 7.5_
  - [x] 2.2a Second open-time guard for a database whose WAL was not checkpointed (review M2)
    - Write `tests/unit/test_store_connection_wal_crash.py` first and watch it fail: a child process (`subprocess`) creates a WAL database, runs `BEGIN; PRAGMA user_version=<registry.highest()+1>; COMMIT` and ends with `os._exit(0)`, so no checkpoint runs; the test first asserts the fixture reproduces the problem (the `-wal` file is non-empty and the 4 bytes at offset 60 of the main file still hold the old value). Then `open_connection` must raise `SchemaTooNewError`, and afterwards the `migrations` table does not exist, there is no `clean_shutdown` counter row, and `PRAGMA user_version` still returns the high value (the opener wrote nothing). A second test with the graceful fixture of 2.2 patches `sqlite3.connect` to fail when called and still gets `SchemaTooNewError`, which pins "the byte guard runs first".
    - Edit `store/connection.py`: after the pragmas and before any write (before the `migrations` bootstrap and before `clean_shutdown = 0`) run `PRAGMA user_version`, close the connection and raise `SchemaTooNewError` when it is above `registry.highest()`.
    - [x] (O1) Test first, in the same file: the fixture holds 3 rows (events, incidents, proposals) committed before the crash; after the refused open the rows are all present and unchanged when read with the matching newer-version-tolerant raw query (a plain `sqlite3` connection in the test), and the test does not assert byte equality of the main file. No `immutable=1` or read-only URI anywhere in `store/connection.py` (static check on the source).`n    - _Requirements: 2.3_ (review M2; known limitation O1 in the design)
  - [x] 2.3 Implement the migration runner and the core schema
    - Write `tests/unit/test_store_migrations.py` first: a new database ends with tables `events`, `incidents`, `proposals`, `audit_log`, `counters`, `migrations`, the indexes and triggers from the design, and `user_version = 1`; opening again applies nothing; a registered extra migration (version 100) runs exactly once and `user_version` becomes 100; a migration added later with a lower version than `user_version` still runs once; a failing migration rolls back its DDL, its `migrations` row and its `user_version` change and the error names the migration; a callable migration that tries to `COMMIT` is rejected; a version higher than the highest known refuses to start (reuses 2.2); the `events` table has the `ingested` column and index, `incidents` rejects a `sample` over 4096 and a `host` over 255 characters.
    - [x] (M1) Test first: on a new database `apply_migrations` runs without "table migrations already exists", and the DDL of `store:0001-core-tables` (`schema.py`) contains no `CREATE TABLE migrations`; the bootstrap `CREATE TABLE IF NOT EXISTS migrations` in `apply_migrations` is the only definition.
    - [x] (L6) Test first: a callable migration that runs `COMMIT` raises `MigrationError` naming it (the runner checks `conn.in_transaction` after the callable); the message says DDL it already committed cannot be undone.
    - Add `apply_migrations` and the single `_set_user_version(conn, n)` helper (the only dynamic SQL string, `int(n)` inside) to `store/migrations.py`, create `store/schema.py` with the DDL of migration `store:0001-core-tables` and `register_core_migrations(registry)` plus `core_registry()`.
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5_
  - [x]* 2.4 Write property tests for migrations
    - Create `tests/pbt/test_store_migrations_properties.py`.
    - **Property 16: Migration idempotence**
    - **Property 17: Migration atomicity**
    - **Validates: Requirements 2.1, 2.2, 2.3, 2.4**

- [x] 3. Transactions, counters, writer thread and the `Store` facade
  - [x] 3.1 Implement `Tx`, counters and `run_callbacks`
    - Write `tests/unit/test_store_tx.py` first, against a raw connection: `counter_next` returns `max(stored, floor) + 1` and stores it (counter 5, floor 9 gives 10; counter 9, floor 5 gives 10; empty gives 1); `counter_raise` never lowers; counter names are validated and passed as parameters; `counter_add` adds a delta; `counter_name_part("Weird Source!")` gives a valid name part; `run_callbacks` runs callbacks in order with the same `tx`, and a raising callback raises `SubscriberError` naming it (the test wraps it in a transaction and asserts that every earlier callback's writes are rolled back).
    - Create `store/tx.py` (`Tx`, `format_id`, `counter_name_part`, `SubscriberError`, `run_callbacks`).
    - _Requirements: 3.5, 3.7_
  - [x] 3.2 Implement the writer thread and the `Store` facade
    - Write `tests/unit/test_store_facade.py` first: `transact` returns after commit (a second `Store` on the same file sees the row); an exception in `fn` rolls back and propagates; a retryable `sqlite3.OperationalError` injected twice is retried and then succeeds, injected four times raises `StoreBusyError` or `StoreFullError`; `store_ok` flips to false on exhaustion and back to true on the next commit; `transact_sync` from a running loop raises `RuntimeError` unless `allow_blocking=True`; a job submitted from the writer thread raises `StoreReentryError`; `close(timeout=5)` returns within 5 seconds, a second `close` is harmless, and calls after close raise `StoreClosedError`; `Store.open(":memory:")` creates no file and behaves the same; a raising callback inside `transact` rolls the whole transaction back (Property 21 example); the writer thread is a daemon, starts on the first job (no thread exists right after `open`), and creating and dropping 50 unclosed `:memory:` stores returns `threading.active_count()` to its starting value after `gc.collect()`; a subprocess that creates an unclosed store exits within 5 seconds; a result delivered to a closed loop does not crash the writer (run `asyncio.run` twice on one store), and a result delivered to a cancelled awaiter does not raise `InvalidStateError` (block the writer, cancel the awaiting task, release the writer, check the write is committed and nothing is logged as an error); `previous_shutdown_clean` is true for a new database and after `close`, false after `Store._abandon()` (the test hook that stops the writer without draining and leaves `clean_shutdown = 0`); `note_last_event_id(n)` raises the `event_id` counter and never lowers it; `StoreConfig` timing knobs (`batch_rows`, `batch_ms`, `queue_limit`, `trim_interval`, `flush_interval`, `flush_tick`, `persist_slots`) can be overridden by tests.
    - [x] (L1) Test first: exactly three injected retryable failures still succeed on the fourth call (`fn` is called four times); four failures raise.
    - [x] (L3) Test first: after dropping an unclosed store, `threading.active_count()` is back to its starting value right after `gc.collect()` (no sleep), because `_stop_writer` sets the flag, enqueues a sentinel and joins for up to 1 s.
    - [x] (L4) Test first: with the writer blocked inside a job, `close(timeout=0.2)` returns within 1 s, logs one error and does not close the connection from the calling thread; after the job is released the writer closes it.
    - [x] (L14) Test first: `close(clean=False)` leaves `clean_shutdown` at 0 and the next `previous_shutdown_clean` is false; plain `close()` still sets it to 1.
    - [x] (M4) Test first: `Store.open` leaves no `store-writer` thread; the first `read_sync` or `max_id_sync` starts it and runs on a thread named `store-writer`, not on the caller's thread (record `threading.current_thread().name` inside `fn`).
    - Create `store/writer.py` (job queue, daemon thread started lazily, `weakref.finalize` cleanup, retry with 50 ms, 200 ms, 800 ms pauses, health flags, `_deliver` helper that checks `future.cancelled()` and catches `InvalidStateError` and `RuntimeError`), `store/store.py` (`Store`, `StoreHealth`, `previous_shutdown_clean`, `note_last_event_id`, `_abandon`), extend `store/config.py` with the timing fields, and update `store/__init__.py` exports.
    - _Requirements: 1.3, 1.7, 7.1, 7.4, 7.5_
  - [x]* 3.3 Write the property test for the counter floor
    - Create `tests/pbt/test_store_counters_properties.py`.
    - **Property 2: Next id formula**
    - **Property 21: Transaction atomicity under callback failure** (same file, `transact` with random callback lists)
    - **Validates: Requirements 3.5, 2.4**

- [x] 4. Audit log
  - [x] 4.1 Implement `AuditLog`
    - Write `tests/unit/test_store_audit.py` first: ids increase; `read` is newest first, honours `limit` (default 100, clamped to 500) and `before`; caps (100 / 1000) and control-character stripping; a registered secret and `scheme://user:pass@host` credentials never appear in `detail` (canary test); direct SQL `UPDATE` and `DELETE` on `audit_log` fail with the trigger message; the public attributes of `AuditLog` contain only `append`, `append_tx` and `read`; trimming events leaves audit rows; `append_tx` rolls back with its transaction.
    - Create `store/audit.py`; add `Store.audit` and `Store.add_redaction` to `store/store.py`.
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6_
  - [x] 4.2 Write the property test for the audit log
    - Create `tests/pbt/test_store_audit_properties.py` (arbitrary text including control characters and surrogates).
    - **Property 18: Audit append-only**
    - **Validates: Requirements 8.1, 8.2, 8.3, 8.5**

- [ ] 5. Proposal model, state machine and persistence
  - [ ] 5.1 Implement the `Proposal` model and the pure transition rules
    - Write `tests/unit/test_store_transitions.py` first: every (from, to) pair of the four statuses, with and without `HubAuthority`; the three reserved `approved` to `denied` cases accepted only with authority and origin `hub`; the same pair with a caller-supplied `decided_by` starting with a reserved prefix and no authority is rejected; `ruleman` and `expired1` are not reserved, `rule-x`, `rule:x`, `withdrawn:x`, `expired:x`, `expired-x` are; `Proposal.to_dict()` has exactly the nine baseline keys and `to_full_dict()` adds the extended ones.
    - Create `store/models.py` (`Proposal`) and `store/proposals.py` with `HubAuthority`, `check_transition`, `is_reserved_decider` and the status constants.
    - _Requirements: 4.3, 4.5, 4.6, 4.7_
  - [ ] 5.2 Implement proposal create, transition and load
    - Write `tests/unit/test_store_proposals.py` first, on a temporary file: create `p1`, `p2`; restart and create `p3`; delete `p3` in SQL, restart, create `p4` (Success Criterion 1); lower the counter in SQL below the stored maximum and the next id is still above every stored id; two concurrent creates from `asyncio.gather` get distinct ids; transitions are checked against the current stored status inside the transaction (two concurrent `decide` calls give one winner and one `InvalidTransition`); the counter and the insert commit together (a failure after `counter_next` leaves the counter unchanged); field caps (action 200, details 500, `result_note` 300, `decided_by` 60); `decision_origin` is `user` without authority and `hub` with it; extended columns are nullable and a row without them loads; `incident_opening` and `service_id` are stored; `list_proposals` applies status and service filters before the limit.
    - Add `create_proposal`, `transition_proposal`, `get_proposal`, `list_proposals`, `load_all_proposals` to `store/proposals.py`.
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 4.1, 4.2, 4.5, 4.6, 4.7, 5.10_
  - [ ] 5.3 Write property tests for proposal ids
    - Create `tests/pbt/test_store_proposal_ids_properties.py` (random sequences of create, delete-row, trim, restart on a temporary file; concurrent creation with `asyncio.gather`).
    - **Property 1: Proposal ids are never reused**
    - **Property 3: Id format**
    - **Property 4: Concurrent creation**
    - **Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.6**
  - [ ] 5.4 Write property tests for transitions and persistence
    - Create `tests/pbt/test_store_proposal_properties.py`.
    - **Property 6: Transition table**
    - **Property 7: Origin is independent of `decided_by`**
    - **Property 8: Proposal round trip**
    - **Validates: Requirements 4.2, 4.3, 4.5, 4.7**
  - [ ] 5.5 Checkpoint: run `python -m pytest -q`; everything, including the 3 to 5 tests, must pass.

- [ ] 6. Core model changes (stdlib only, no existing test changes)
  - [ ] 6.1 Add event fields
    - Write `tests/unit/test_core_event_fields.py` first: `Event` has `id` (default 0, `compare=False`), `service_id` and `log_source_id` (default `None`); two events that differ only in `id` compare equal; `dataclasses.replace(event, id=7)` works on the frozen class; `to_dict()` includes the new keys; all existing event construction still works.
    - Edit `src/chimericmesh/events.py`.
    - [ ] (L7) Test first: `Event("syslog", "h", "a", 3, "m", "raw", 12.5)` (positional, as today) still works and `ts == 12.5`; the dataclass field order ends `ts, id, service_id, log_source_id`.
    - _Requirements: 6.1_
  - [ ] 6.2 Extend `Incident` and `IncidentTracker`
    - Write `tests/unit/test_tracker_lifecycle.py` first: `Incident` has `service_id`, `opening` (1), `resolved_at`, `opened`; `to_dict()` (overridden to drop `opened` from `asdict`) includes `service_id`, `opening`, and `resolved_at` (`null` while open) and not `opened`; `preview_id(event)` returns `None` for severity above 4 and for an empty signature, so an info-level event never reopens a resolved incident (test with a resolved incident past the window); `opened` is set the first time severity is at most 2 or count at least 3 and then `qualifies` and `get` return an incident with count 1 and severity 4; the incident id algorithm is unchanged (golden id); `reopen(inc, event)` resets `resolved`, `resolved_at`, `opening`, `count`, `opened`, `severity`, `first_seen`, `last_seen`, `sources` as in the design; `install(inc)` adds a cluster and `preview_id(event)` returns the id `add` would use without changing state; `on_evict` is called with the evicted incident; eviction keeps the baseline rule (never-qualified clusters first); `analyze()` results are unchanged; `service_id` rule: the first event sets it, a later non-null value fills a NULL once, a non-null value is never replaced by a different one, reopen keeps it.
    - [ ] (M6) Test first: `IncidentTracker.add` sets `opened` itself; feeding three severity-4 events with one signature returns an incident with `opened` false for the first two and true for the third; the hub never assigns `opened` (the static check lives in 11.2b).
    - Edit `src/chimericmesh/diagnostics.py`; keep `add(ev)` returning `Incident | None`.
    - _Requirements: 5.4, 5.5, 5.6, 5.7_

- [ ] 7. Event writer, retention and event queries
  - [ ] 7.1 Implement the bounded event queue and the batch writer
    - Write `tests/unit/test_store_events.py` first: `put` never blocks and stores nothing until the writer runs; rows are written in batches of at most 500 or after 250 ms (timing comes from the `StoreConfig` knobs `batch_rows`, `batch_ms`, `queue_limit`, tests override them); a stored row equals the event (id, ts, source, host, app, severity, message, service id, log source id, raw) plus `ingested` (the hub clock at `put`); `message` and `raw` are cut at 4096; a lone surrogate and NUL do not raise; the queue holds at most 20000 rows, drops the oldest, increments `dropped_events` and logs at most one warning per minute (fake clock); counter deltas `events_total` and `evsrc:<source>` and the `event_id` high-water mark are written in the same transaction as the batch; `close` drains pending rows; event rows are inserted with `INSERT OR IGNORE`, so a duplicate `id` in a batch is ignored, counted in `dropped_events` (batch size minus `rowcount`) and the batch still commits; `put` after `close` never raises and counts a drop.
    - Create `store/events.py` (`EventWriter.put`, batching loop hook into `store/writer.py`, `dropped_events`); wire `Store.events`.
    - _Requirements: 6.1, 6.1a, 6.2, 6.5, 7.1, 7.3_
  - [ ] 7.2 Add retention and write-failure handling for event batches
    - Extend `tests/unit/test_store_events.py` first: with `max_events=1000` the table never exceeds 1000 plus 500 plus one batch at observation points and is at most 1000 after each trim; rows whose `ingested` is older than `event_ttl_days` are removed (fake clock), including rows beyond the 5000 lowest ids, and an event with an old `ts` but fresh `ingested` (a replay) is kept; the TTL loop and the cap loop terminate by the stop rule in the design (chunks of 5000, stop on a short chunk or zero deletions); oldest means lowest id; trimming runs at least every 60 seconds and also when `approx_rows` passes `max_events + 500`; trims run in chunks of 5000 and a queued `transact` job is served between chunks; incidents, proposals and audit rows are untouched; an injected write failure retries 3 times, then the batch is dropped and counted, the deltas are put back, `store_ok` is false and the writer keeps running; the next successful batch makes `store_ok` true.
    - Edit `store/events.py` and `store/writer.py`.
    - _Requirements: 6.3, 7.4, 8.6_
  - [ ] 7.3 Implement event queries and the id seed
    - Extend `tests/unit/test_store_events.py` first: `after(after_id, limit)` returns ids above `after_id`, ascending, at most `limit`, with optional `source` and `service_id` filters; `newest(n)` returns the newest `n` ascending; `barrier()` returns only after earlier rows are committed or dropped; `max_id_sync()` is the larger of `max(events.id)` and the `event_id` counter, including after the table was emptied by retention, plus `crash_margin` (`2 * queue_limit + batch_rows`, L2) when `Store.previous_shutdown_clean` is false (open the same file after `Store._abandon()`, and the seed is above every id handed out); `after()` called on an empty table returns `[]`.
    - [ ] (L2) Test first: with `queue_limit=100`, `batch_rows=10`, ingest while a commit is blocked so more than `batch_rows` rows are dropped during it, `Store._abandon()`, reopen: the seed is above every id handed out; `crash_margin` equals `2 * queue_limit + batch_rows`.
    - [ ] (L5) Test first: with `batch_ms` set to 10 s and 3 rows queued, `await barrier()` returns within 1 s (it forces an immediate flush) and the rows are committed.
    - Edit `store/events.py`.
    - _Requirements: 6.1, 6.4, 6.6_
  - [ ] 7.4 Write property tests for event rows and retention
    - Create `tests/pbt/test_store_events_properties.py`.
    - **Property 9: Event row fidelity and caps**
    - **Property 10: Retention bounds**
    - **Property 11: Events after an id**
    - **Validates: Requirements 6.1, 6.2, 6.3, 6.6, 8.6**
  - [ ]* 7.5 Write the property test for write accounting
    - Create `tests/pbt/test_store_accounting_properties.py` (small queue limit, injected failures).
    - **Property 19: Write accounting**
    - **Validates: Requirements 7.3, 7.4**

- [ ] 8. Incident rows and lifecycle (reopen)
  - [ ] 8.1 Implement incident row mapping, upsert and load
    - Write `tests/unit/test_store_incidents.py` first: an `Incident` round-trips through `upsert_incident` and `load_incidents` with every field including `opened`, `opening`, `resolved_at` and `announced_count`; `hypothesis` and `remediation` come back through `classify(sample)`; rows of incidents evicted from memory stay in the table; a row with `resolved = 1` and no `resolved_at` is rejected by the CHECK; `IncidentRow.from_incident` is immutable (frozen, `sources` a tuple) and cuts `sample` to 4096 and `host` to 255; the upsert is a plain `INSERT ... ON CONFLICT(id) DO UPDATE SET <all columns>` with no `CASE` and always writes the snapshot's `announced_count` (NULL, 3, then 1 after a reopen are stored as given); `IncidentRow` carries `announced_count`.
    - Create `store/incidents.py` (`IncidentRow`, row mapping, `upsert_incident`, `load_incidents`, `get_incident_row`).
    - _Requirements: 5.1, 5.2, 5.5, 5.6_
  - [ ] 8.2 Implement `IncidentLifecycle` persistence
    - Write `tests/unit/test_store_lifecycle.py` first, with `store=None` and with a temporary store: `persist` writes the row and is a no-op without a store; `mark_dirty` plus `maybe_flush` writes at most once per 30 seconds of fake time; `flush_all(deadline)` writes every dirty incident and stops submitting at the deadline; `flush_dirty` writes every dirty incident whose last write is at least `flush_interval` old even when no event arrives (one incident, 5 events, fake clock +31 s, one call, the row has count 5); a failing store leaves the incident dirty and does not raise; persist happens before the injected `announce` is called; the closure given to `transact` uses the snapshot: mutate the incident after `persist` is submitted and the stored row equals the snapshot; with a store whose `transact` never returns, 5000 `persist` calls leave at most `persist_slots` (256) pending tasks, the rest return at once and stay dirty; with `announced_getter` returning 1 for the incident, skip a `persist` under a full semaphore, run `flush_dirty` later, reload the row, and its stored `announced_count` is 1 (the marker survives a skipped persist).
    - Add `IncidentLifecycle` (persistence part: semaphore, snapshots, `flush_due`, `flush_dirty`, `flush_all`) and the `Announce` and `AuditSink` protocols in `store/ports.py`.
    - _Requirements: 5.1_
  - [ ] 8.3 Implement the reopen rule
    - Extend `tests/unit/test_store_lifecycle.py` first, one test per sub-criterion 5.7a to 5.7i: a resolved incident and an event after `reopen_after` reopens; before it the count grows, nothing is announced and `opening` stays; exactly the `reopen_after` boundary reopens; `reopen_after=0` reopens at once; a reopened severity 3 incident with count 1 is returned by the tracker; severity is reset to the event's severity; the audit row `incident_reopened` (incident id, new opening, service id) is in the same transaction as the incident row and is rolled back with it; the announced text is `REOPENED <id>: <category> on <host>`, sent once, after the commit; the test "reopen, two more events, exactly one announcement for that opening" (M4); resolve again and reopen again gives opening 3 and a second `REOPENED`; works with `store=None` (audit through `audit_getter()` when a sink is attached, none otherwise; the getter is read at call time, so a sink attached after construction is used); `resolved_at` is reloaded after a restart so the window continues; the audit `detail` of `incident_reopened` contains neither the incident sample nor the signature (canary text in the event, R8.4); the announcement is not a webhook (no `incident.reopened` call).
    - Add `reopen_due` and `reopen` to `store/incidents.py`.
    - _Requirements: 5.7, 5.8, 5.9, 5.10_
  - [ ] 8.4 Write property tests for reopen
    - Create `tests/pbt/test_store_reopen_properties.py` (fake clock, random event, resolve and advance sequences, with `store=None` and with a store).
    - **Property 12: Reopen only after the window**
    - **Property 13: Reopen resets state**
    - **Property 14: One announcement per opening**
    - **Validates: Requirements 5.2, 5.6, 5.7, 5.8, 5.9**

- [ ] 9. In-memory hub behaviour (bare `Hub()`, no store yet)
  - [ ] 9.1 Event ids, tagged return value and incident state on the tracker
    - Write `tests/unit/test_hub_event_ids.py` first: `Hub.ingest` returns the event with an id starting at 1 and strictly increasing; the ring and `recent()` carry the ids; `GET /api/events` returns `id`; `hub.resolved` is a read-only set of resolved ids and keeps its truthiness behaviour; `resolve` records `resolved_at` once; an event for a resolved incident inside the window adds to the count and does not announce; after the window it reopens through `IncidentLifecycle` with one `REOPENED` line, using the injected `Hub(clock=...)`; `ingest` returns right after a reopen (no second line, even when the count later reaches 3); two concurrent `ingest` calls on one resolved incident give one `opening` increment and one `REOPENED`; `POST /api/ingest` returns the event dict with the assigned `id` (not 0); `/api/demo/storm` still returns exactly `{"injected": n, "at": now}` (B21, unchanged).
    - Edit `src/chimericmesh/hub.py` (`ingest`, `resolve`, `resolved`, lifecycle construction with `store=None` and `audit_getter`, `Hub.audit` property, `reopen_after` from `CHIMERICMESH_REOPEN_AFTER_SECONDS`), `src/chimericmesh/api.py` (use the tagged event returned by `ingest` in `/api/ingest` only; the storm route needs no edit); import `Proposal` from `store/models.py` and re-export it with `PROPOSAL_STATUSES`, `MAX_PROPOSALS`, `MAX_ACTION`, `MAX_DETAILS`.
    - _Requirements: 5.9, 5.11, 6.1_
  - [ ] 9.2 Per-run guided-workflow flags
    - Write `tests/unit/test_hub_workflow_per_run.py` first: `traffic`, `incidents`, `propose`, `decide` and `resolve` become done only after the action happens in this run (`traffic` from `_run_events`, `incidents` from `_run_opened` which counts openings and reopenings), for a bare hub the existing workflow tests give the same results; with persisted events, incident and proposal loaded from a store (added in 11.3, marked `@pytest.mark.xfail(strict=True)` until then, and task 11.3 removes the marker) all five stay not done.
    - Edit `src/chimericmesh/hub.py` (`_run_events`, `_run_opened`, `_run_proposals`, `_run_decided`, `_run_resolved`, `workflow`).
    - _Requirements: 4.8_
  - [ ] 9.3 `StateView`, filters and stats keys
    - Write `tests/unit/test_hub_stateview.py` first: `incidents(status=..., service_id=..., limit=...)` filters before the limit and `unassigned` selects incidents without a service; `list_proposals(status, service_id=..., limit=...)` returns the newest `limit` oldest-first and filters before the limit; `events_after(after_id)` serves from the ring in order, oldest first, with `source` and `service_id` filters applied before the limit, and `GET /api/events?after_id=` returns oldest first while the route without `after_id` still returns newest first; `GET /api/incidents` without `limit` returns every incident (no default limit), with `limit` 1 to 500 and **422** outside it (`/api/proposals` the same, `ge=1, le=500`, default 200; `/api/events` keeps its silent clamp to 1..500, so `limit=0` and `limit=9999` return 200 with 1 and up to 500 items; `after_id=-1` gives 422); `stats()` has `store_ok: true` and `dropped_events: 0`; the in-memory proposal counter is incremented in an await-free block (no `Hub._lock` exists) and the cap of 200 with `p1` restarting per process is unchanged.
    - Edit `src/chimericmesh/hub.py` and `src/chimericmesh/api.py` (filter parameters, `after_id`, `limit` validation); add the `StateView` protocol to `store/ports.py`.
    - _Requirements: 1.4, 3.1, 4.4, 4.4a, 6.6_
  - [ ] 9.4 In-memory hub proposal rules
    - Write `tests/unit/test_hub_inmemory_proposals.py` first, on a bare `Hub()`: every (from, to) status pair through `decide` and `complete` follows the transition table (`check_transition`), invalid ones return `None` and change nothing; `decision_origin` is `user` for decisions made through `Hub.decide` and `hub` only for the hub's own rule-logic method (`HubAuthority`); a REST or IRC `decided_by` that starts with a reserved prefix cannot produce `hub`; `decided_by` keeps 60 characters when written by the hub (REST `DecideRequest.by` stays at `max_length=30`); `Proposal.to_dict()` still returns the nine baseline keys; `incident_opening` and `service_id` are recorded at creation.
    - Edit `src/chimericmesh/hub.py` (`decide`, `complete`, `create_proposal` call the shared rules).
    - _Requirements: 1.4, 4.3, 4.5, 4.6, 4.7, 5.10_
  - [ ] 9.4a Proposal write seam on the bare hub (review M5)
    - Write `tests/unit/test_hub_proposal_writer.py` first, on a bare `Hub()`: `create_proposal(incident_id, action, extra={"kind": "edit", "files": ["a.py"], "estimated_lines": 5, "verification": "pytest -q"})` returns a `Proposal` carrying those attributes; `to_dict()` has exactly the nine baseline keys and `to_full_dict()` contains the extras; an unknown `extra` key raises `ValueError` and creates nothing (the in-memory counter does not advance); `hub_transition(pid, "approved", decided_by="rule:r1", approved_by_rule="r1")` stores `decision_origin == "hub"` and `approved_by_rule == "r1"`; `hub_transition` on an approved proposal to `denied` with `decided_by="rule-revoked:r1"` succeeds, with `decided_by="alice"` returns `None` and changes nothing; `Hub.decide(pid, True, "rule-x")` still stores `decision_origin == "user"`; `complete(pid, "n", extra={"files_changed": ["a.py"], "lines_changed": 3, "verification_result": "pass"})` stores them; `isinstance(hub, ProposalWriter)` and `isinstance(hub, ProposalClient)` hold (`runtime_checkable`).
    - Add `ProposalWriter` and `ProposalClient` to `store/ports.py`; edit `src/chimericmesh/hub.py` (`extra` arguments, `hub_transition`, the only `HubAuthority()` construction) and `store/models.py` if a field is missing.
    - _Requirements: 4.3, 4.6, 4.7, 5.10_ (review M5)
  - [ ] 9.5 Checkpoint: run `python -m pytest -q`; the whole existing suite must pass unchanged with the new tests.

- [ ] 10. Documentation and static guards
  - [ ] 10.1 Proposal-id format guard and README text
    - Write `tests/unit/test_proposal_id_format.py` first: scan `src/`, `tests/`, `powers/` and `.kiro/agents/` for a proposal id of the form `p<digits>-<tag>`, using exactly the pattern `\bp\d+-[a-z0-9]{3,}\b` (case-sensitive, tag of at least 3 characters, so a range such as `p1-p5` or `p1-3` does not match) with an `ALLOWED` tuple in the test for any legitimate future match, each entry with a comment; build the regex from string fragments at runtime and skip this test file by path so the guard does not match itself (L5); a second test checks the README states that proposal ids are unique per database and that deleting the database restarts them at `p1`, and that the startup log line (function `describe_startup(path)` in `store/config.py`) prints the path and that statement; the README also states that an explicit relative `CHIMERICMESH_DB` is resolved once against the current directory (the defaults never depend on it, R1.1) and the residual risk that after a crash following a long storage outage a restart may reuse event ids a client saw.
    - [ ] (L14) README text test first: the README states that one database file supports one running hub process (two processes serving one file are not supported) and that a start that fails before serving leaves the crash marker untouched.
    - [ ] (O1) README text test first: the README states that refusing a newer-schema database after a crash may still checkpoint its `-wal` into the file (the data is not changed). 
    - Edit `README.md` (a short "Data and proposal ids" section, the environment variables of this module, the reserved decider prefixes) and add `describe_startup` to `store/config.py`.
    - _Requirements: 3.8, 3.9_
  - [ ] 10.2 Static SQL and API-surface guards
    - Write `tests/unit/test_store_static.py`: an AST scan of `store/*.py` that fails when the first argument of `execute`, `executemany`, `one` or `all` is not a string literal or a module-level constant (rejects f-strings, `+`, `%`, `.format`), exempts exactly the function `_set_user_version` and asserts that no other function is exempt, and checks that optional filters use the `(:p IS NULL OR col = :p)` pattern (no string-built WHERE); a scan that no route in `api.py` is a plain `def` (T2); a scan that `store/` imports no module outside the `core` and `store` groups (`store/connection.py` imports `chimericmesh.fsperms`, which is in the `store` group; complements 1.3). The `HubAuthority(` scan (constructed only in `hub.py`) is written here. (L9) It scans `src/` only, because tests 5.1, 5.4 and others construct `HubAuthority` on purpose, and it exempts the class definition in `store/proposals.py`. 9.4a adds the first call (`hub_transition`), so the scan is meaningful from 9.4a on, and it is also a named checkpoint step of task 11.3 (run that one test again there).
    - No production edits expected; fix any violation found in earlier tasks.
    - _Requirements: 3.8, 4.3, Success Criterion 8_

- [ ] 11. Wire the store into the hub and `create_app`
  - [ ] 11.1 Store-backed hub: startup load, events and counters
    - Write `tests/unit/test_hub_store_events.py` first, on a temporary file: `Hub(store=store)` loads the newest 2000 events into the ring, seeds the event id from `max_id_sync`, restores `total_events` and `total_by_source`; ingest queues rows; ids continue after restart even after the table was emptied and, after a simulated crash (`Store._abandon()`, then reopen the file; `previous_shutdown_clean` is false), continue above every id shown to a client (the `crash_margin` of 7.3, L2); a graceful `aclose` whose deadline dropped queued rows still leaves the `event_id` counter at or above the last assigned id (`note_last_event_id`); the transcript is empty after a restart (R9.1: `Hub(store=...)` does not persist or reload it); an `ingest` after `aclose` updates memory, never raises and writes nothing; `events_after` on a store-backed hub: boundary `after_id == ring_min - 1` is served from the ring, a lower `after_id` is stitched from the database then the ring with no gap and no duplicate, even while 15 000 rows are still queued (the stitch awaits `barrier()` first), id gaps from dropped rows are allowed, and a hub with an empty ring (fresh store) answers from the database only; a source `"Weird Source!"` does not poison a batch; `hub.store` is set; a bare hub keeps `store=None`.
    - Edit `src/chimericmesh/hub.py` (constructor `store=`, `_load`, `ingest` event path, `events_after`, `start`, `aclose(deadline)`).
    - _Requirements: 6.1, 6.4, 6.5, 9.1_
  - [ ] 11.2 Store-backed hub: incidents
    - Write `tests/unit/test_hub_store_incidents.py` first: an incident opened with 50 events survives a restart with count 50, receives 5 more events and has count 55 and no new announcement (Success Criterion 2); resolved stays resolved; persistence happens at open, at milestones 10/50/100/500, on resolve, by the flush loop within `flush_interval` plus one tick even when the incident gets no further events (fake clock, `Hub.start()`), and on `aclose`; after a restart `_announced` is rebuilt from `announced_count` (open, flush with `Keep`, restart, one event, no announcement); announce is called after the commit; a store failure never makes `ingest` raise and the incident is retried; `Hub.resolve` never raises on a failing store; with a store whose `transact` never returns, 5000 events leave at most 256 pending persist tasks; an incident evicted from memory is rehydrated from its row on the next event (`MAX_CLUSTERS` patched small); the row of an evicted opened incident stays; restart with `MAX_CLUSTERS` patched to 3 and 6 stored incidents: the three newest are loaded, the other three ids are in `_stored_unloaded_ids`, and one event for an unloaded incident continues its stored count, `opening`, `resolved` flag and `announced_count` (no fresh cluster at count 1, no second announcement, and the stored row is not overwritten with a lower count); a skipped persist (semaphore full) followed by a flush and a restart does not announce the incident again (the marker is in the snapshot).
    - [ ] (L8) The tests patch `diagnostics.MAX_CLUSTERS` (not a name inside `hub`), and a test asserts the hub reads it at call time (`from . import diagnostics`; patch it to 3 after `import chimericmesh.hub` and the load still honours 3).
    - Edit `src/chimericmesh/hub.py` (lifecycle with the store, `announced_getter`, startup load of the newest `MAX_CLUSTERS` rows, `_stored_unloaded_ids`; the rehydrate step is done in 11.2a).
    - _Requirements: 5.1, 5.2, 5.3, 5.5, 7.4_
  - [ ] 11.2a Rehydrate on miss without a race (review M3)
    - Write `tests/unit/test_hub_store_rehydrate.py` first, with a stored incident that is in `_stored_unloaded_ids` (`diagnostics.MAX_CLUSTERS` patched small) and a store whose `read` can be held and released: (a) two concurrent `ingest` calls (`asyncio.gather`) for that id while the read is held: after release the incident has stored count + 2, `tracker.install` was called once (spy), the id is not in the set, no `KeyError`; (b) a resolved, unloaded incident past the reopen window with two concurrent events gives one `opening` increment and one `REOPENED` line; (c) a failing read (`read` raises `StoreError` once): the event is in the ring and the event queue, no cluster exists in the tracker, the id is still in the set, no announcement is made, and after `flush_dirty` the stored row is unchanged (count, `opening`, `announced_count` not lowered); the next event, with a working read, continues the stored count; (d) cancelling one of two waiting `ingest` calls lets the other finish with one install and leaves `_rehydrating` empty.
    - Edit `src/chimericmesh/hub.py` (`_rehydrating`, `_rehydrate`, step 0 of `ingest`, `discard` instead of `remove`).
    - _Requirements: 5.2, 5.3, 5.7_ (review M3, Property 15)
  - [ ] 11.2b First open is detected from `opened` and `_announced` (review M6)
    - Write `tests/unit/test_hub_store_first_open.py` first: a stream of severity-4 events for one signature crossing count 3 persists the row once with an awaited commit at the third event and announces exactly once; events 4 to 9 add no second announcement and no awaited persist from the open branch; after a restart `_announced` is rebuilt from `announced_count` and a further event does not announce; a static test asserts `hub.py` contains no assignment to `.opened` (`\.opened\s*=`) so only the tracker sets it.
    - Edit `src/chimericmesh/hub.py` (`first_open = inc.opened and inc.id not in _announced`; remove any assignment of `opened`).
    - _Requirements: 5.1, 5.2, 5.6_ (review M6, Property 14)
  - [ ] 11.3 Store-backed hub: proposals
    - Write `tests/unit/test_hub_store_proposals.py` first: create, decide and complete persist before returning; ids continue after restart (Success Criterion 1); all proposals are kept past 200 in store-backed mode; `list_proposals` returns the stored `decision_origin` and `incident_opening`; the mirror changes only after the commit and an `InvalidTransition` becomes `None`; hub-origin transitions only through a method that passes `HubAuthority`; a restart does not mark any of the five workflow flags done (remove the `xfail(strict=True)` marker added in 9.2; with `strict=True` an unexpected pass fails the run, so the marker cannot be forgotten); cancellation: for each of `create_proposal`, `decide` and `complete`, block the writer, cancel the awaiting task, release the writer, and assert the in-memory mirror equals the database row, `get_proposal` returns the new status, and a second `decide` returns `None` (InvalidTransition) only because the first one happened; no "exception never retrieved" or `InvalidStateError` is logged (the commit and mirror update are one `asyncio.shield`ed unit, T6); re-run the `HubAuthority(`-only-in-`hub.py` scan of 10.2 (`src/` only, class definition exempt, L9) now that the store path calls it; a failing store makes `create_proposal`, `decide` and `complete` raise `StoreError` before memory changes, and `commands.handle_command` returns `["store unavailable, try again"]` for the IRC and terminal paths (the connection stays open, the terminal route returns 200).
    - Edit `src/chimericmesh/hub.py` (store path of `create_proposal`, `decide`, `complete`, `hub_transition` for rule logic) and `src/chimericmesh/commands.py` (catch `StoreError`).
    - _Requirements: 3.3, 4.1, 4.2, 4.3, 4.4, 4.7, 4.8, 5.10_
  - [ ] 11.3a Proposal write seam on the store-backed hub (review M5)
    - Write `tests/unit/test_hub_store_proposal_writer.py` first, on a temporary file: `create_proposal(..., extra=...)` persists `kind`, `files` (JSON), `estimated_lines` and `verification`, and they come back after a restart through `to_full_dict()`; `hub_transition(pid, "approved", decided_by="rule:r1", approved_by_rule="r1")` commits the status, `decision_origin == "hub"` and `approved_by_rule` in one transaction and reloads after a restart; an approved proposal denied through `hub_transition(..., decided_by="rule-revoked:r1")` is stored as `denied` with origin `hub`, and with `decided_by="alice"` raises nothing but returns `None` and leaves the row unchanged; `complete(..., extra={"files_changed": [...], "lines_changed": 3, "verification_result": "pass"})` persists them; an unknown `extra` key raises `ValueError` before the writer thread is called (spy on `store.transact`); two concurrent `hub_transition` calls give one winner; (O2) `hub_transition` rejects (`InvalidTransition`, no change) every (from, to, decided_by) outside the restricted set (`approved`->`denied` with `rule-revoked:`/`rule-expired:`/`rule-suspended:`; `pending`->`denied` with `withdrawn:`/`expired:`; `pending`->`approved` with `approved_by_rule`), and always stores `decision_origin = 'hub'`; a static test asserts `hub_transition` is not referenced in `api.py`, `mcp_server.py` or `commands.py`.
    - Edit `src/chimericmesh/hub.py` (store path of `hub_transition` and the `extra` arguments; no new public name beyond the protocol).
    - _Requirements: 4.1, 4.3, 4.6, 4.7, 5.10_ (review M5)
  - [ ] 11.3b The announcement is inside the shielded unit (review M7)
    - Write `tests/unit/test_hub_store_announce_cancel.py` first: for each of `create_proposal`, `decide` and `complete`, block the writer, cancel the awaiting task, release the writer: the transcript contains the matching line (`PROPOSAL p1 ...`, `p1 APPROVED by <name>`, `p1 DONE: ...`) exactly once and the mirror equals the database; with `say` patched to raise, the commit stays, the mirror is updated, the call returns the proposal and one error is logged; with a failing store no announcement line is written and `StoreError` is raised.
    - Edit `src/chimericmesh/hub.py` (move the `say` call into the end of `_commit_and_mirror`, wrapped in `try/except Exception` that logs).
    - _Requirements: 4.1, 4.2_ (review M7)
  - [ ] 11.4 `create_app`, REST changes, entry point
    - Write `tests/unit/test_store_app.py` first, one test per row of the design's `create_app` contract table: signature `create_app(hub=None, start_listeners=True, *, store=None)`; `create_app()` with no hub opens `CHIMERICMESH_DB` (or the home default) and builds a store-backed hub; `create_app(hub)` with a bare hub attaches a private `:memory:` store, sets `hub.audit` to its `AuditLog` (the lifecycle sees it through `audit_getter`), writes no file and leaves events, incidents and proposals in the hub, and building the app through `ASGITransport` without lifespan leaves no thread behind (`threading.active_count()` after `gc.collect()`); `create_app(hub)` with a store-backed hub uses `hub.store`, opens no private store and leaves `hub.audit` alone; `create_app(store=s)` builds a hub on `s`; `hub` and `store` that disagree raise `ValueError`; shutdown order is listeners, then `hub.aclose()` with one shared 5-second deadline, and with a failing store (every write raises) the lifespan still finishes within 5 seconds; `CHIMERICMESH_DB=:memory:` writes no file; the lifespan shutdown calls `hub.aclose()` and `store.close()` within 5 seconds; `GET /api/proposals?limit=&service_id=` (default 200, oldest first in the window) and `GET /api/incidents?status=&service_id=&limit=` return 422 for `limit` outside 1 to 500, a bad `status` or an over-long `service_id`; `GET /api/events?after_id=&limit=` clamps `limit` silently to 1..500 (as the baseline) and returns 422 for `after_id` below 0 or non-integer values; `create_app(bare_hub, store=s)` uses `s` as `app.state.store`, leaves `hub.store` `None`, sets `hub.audit` to `s.audit` only when it was `None` (an existing sink is kept), and `create_app(store_backed_hub, store=other)` raises `ValueError`; incidents include `service_id` and `opening`; `/api/proposals` items still have the nine baseline keys; `/api/stats` has `store_ok` and `dropped_events`; a `StoreError` in a route gives `503 {"detail": "store unavailable"}`; a corrupt database file makes `create_app` raise `StoreCorruptError`; (L14) when startup fails after `Store.open` (patch the listener start to raise), the lifespan calls `store.close(clean=False)` and `clean_shutdown` stays 0 when it was 0 at open, while a normal shutdown sets it to 1.
    - Edit `src/chimericmesh/api.py` and `src/chimericmesh/__main__.py` (print the error and exit 2 on `StoreOpenError`; log `describe_startup`).
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.6, 1.7, 4.4, 4.4a, 6.6, 7.4_
  - [ ] 11.5 Integration and benchmark tests
    - Write `tests/unit/test_store_integration.py` (Success Criteria 1, 2, 4 and 6 through `create_app` and `httpx.ASGITransport` on a temporary database, plus starting the app from two working directories) and `tests/unit/test_store_benchmark.py` (R7.2: 5000 events at 1000 per second from 200 signatures, all persisted within 5 seconds of the last event or `persisted + dropped == 5000`, maximum loop stall under 100 ms, including the incident-open commits; skipped when `CHIMERICMESH_SKIP_BENCH=1`).
    - Register the `slow` marker in `pyproject.toml`.
    - _Requirements: 1.2, 3.3, 5.2, 5.7, 5.8, 6.3, 7.2, Success Criteria 1 to 4 and 6_
  - [ ] 11.6 Write the property test for event ids across restarts
    - Create `tests/pbt/test_store_event_ids_properties.py` (temporary file databases, fake clock, random sequences of ingest, trim-everything, graceful restart and `Store._abandon()` crash).
    - **Property 5: Event ids strictly increase across restarts**
    - **Validates: Requirements 6.1**
  - [ ]* 11.7 Write property tests across the wired hub (optional)
    - Create `tests/pbt/test_store_hub_properties.py` (temporary file databases, fake clock).
    - **Property 15: Incident count continuity**
    - **Property 20: Store-backed hub equals bare hub on incidents**
    - **Validates: Requirements 1.4, 5.2, 5.3, 5.9**
  - [ ] 11.8 Final checkpoint
    - Run `python -m pytest -q`. The full existing suite (no existing test edited by this module) and all new tests must pass. Confirm by `git diff --stat tests/` that only new files were added under `tests/`.
    - Walk the Success Criteria 1 to 8 one by one against the tests that cover them and note any gap in this file.
    - _Requirements: all; Success Criterion 5_

## Requirement coverage

Every requirement of `requirements.md` appears in at least one non-optional task.

| Requirement | Tasks | Requirement | Tasks |
|---|---|---|---|
| 1.1, 1.2 | 1.2, 11.4, 11.5 | 5.5 | 6.2, 8.1, 11.2 |
| 1.3 | 1.2, 3.2, 11.4 | 5.6 | 6.2, 8.1, 11.2b |
| 1.4 | 9.3, 11.4 | 5.7 | 6.2, 8.3, 11.2a, 11.5 |
| 1.5 | 2.1, 2.2 | 5.8 | 8.3, 11.5 |
| 1.6 | 2.2, 11.4 | 5.9 | 8.3, 9.1 |
| 1.7 | 3.2, 11.4 | 5.10 | 5.2, 8.3, 9.4, 9.4a, 11.3, 11.3a |
| 2.1, 2.2 | 1.1, 2.3 | 5.11 | 1.2, 9.1 |
| 2.3 | 2.2, 2.2a, 2.3 | 6.1 | 6.1, 7.1, 7.3, 9.1, 11.1 |
| 2.4, 2.5 | 2.3 | 6.2 | 7.1 |
| 3.1 | 3.1, 5.2, 9.3 | 6.3 | 1.2, 7.2, 11.5 |
| 3.2 | 5.2 | 6.4 | 7.3, 11.1 |
| 3.3 | 5.2, 11.3, 11.5 | 6.5 | 7.1, 11.1 |
| 3.4, 3.5, 3.6 | 5.2 (3.5 also 3.1) | 6.6 | 7.3, 9.3, 11.4 |
| 3.7 | 3.1, 5.2 | 7.1 | 3.2, 7.1 |
| 3.8 | 10.1, 10.2 | 7.2 | 11.5 |
| 3.9 | 10.1 | 7.3 | 7.1 |
| 4.1, 4.2 | 5.2, 11.3, 11.3a, 11.3b | 7.4 | 3.2, 7.2, 11.2, 11.4 |
| 4.3 | 5.1, 9.4, 9.4a, 10.2, 11.3, 11.3a | 7.5 | 2.2, 3.2 |
| 4.4, 4.4a | 9.3, 11.3, 11.4 | 8.1 to 8.5 | 4.1 |
| 4.5, 4.6 | 5.1, 5.2, 9.4, 9.4a | 8.6 | 4.1, 7.2 |
| 4.7 | 5.1, 5.2, 9.4, 9.4a, 11.3, 11.3a | 9.1 | 11.1 (transcript empty after restart; L11 moved this from 9.3, which has no transcript test) |
| 4.8 | 9.2, 11.3 | 5.1 to 5.4 | 6.2, 8.1, 8.2, 11.2, 11.2a, 11.2b |
