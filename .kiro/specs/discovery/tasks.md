# Implementation Plan: discovery (V1)

Requirements: [requirements.md](requirements.md). Design: [design.md](design.md). Property numbers below are the numbered Correctness Properties in the design. Interface names come from the approved `store`, `services` and `connectors` designs and are used as they are.

Conventions for every task: write the tests first and watch them fail, then implement; run the new tests and `python -m pytest -q`; no task edits an existing (baseline) test. The one V1 test file this module edits is `tests/unit/test_dependency_direction.py` (rows only), created by `store` and extended by `services`. Tasks marked `*` (Kiro's optional marker, `- [ ]*`) are optional property tests that may be skipped for a faster first pass; every requirement is also covered by a non-optional example test. Optional: 2.5 (properties 3, 4), 4.4 (property 10), 5.3 (properties 11, 12). Required: 1.3 (1), 2.4 (2), 3.2 (5), 4.3 (6, 7, 8, 9), 6.2 (13). Success Criterion 7 names properties 1, 2, 7 and 8; all are required. Stack: Python 3.11+, stdlib, FastAPI and pydantic only in `discovery/routes.py`, pytest, Hypothesis (already in the `dev` extra). New files under `src/chimericmesh/discovery/` unless a path is given. Each sub-task touches at most five files. Build order: `store`, `services` and `connectors` are merged first (tasks 1 to 7 need only `store` and `services`; task 8.2 needs `connectors`).

Review findings that became work are cited as `(M1)`, `(M3)`, `(L5)`, `(L8)` on the line they belong to. Findings with no work are listed in the design under "Review resolutions".

- [x] 1. Skeleton, text helpers and the static rules
  - [x] 1.1 Create the package skeleton, models and text helpers
    - Write `tests/unit/test_discovery_text.py` first: `clean_text` removes `[\x00-\x1f\x7f-\x9f]` and lone surrogates, strips, cuts to the limit, accepts non-`str` input; `normalize_process_name` lower-cases, drops directories (`/usr/sbin/nginx`, `C:\Windows\System32\W3WP.EXE`), removes one trailing `.exe`, cuts to 64, returns `""` for empty or hostile input; `path_key` for `windows` is case-insensitive and separator-insensitive, for `linux` it is case-sensitive; every constant of the design has its stated value.
    - Create `discovery/__init__.py` (re-exports only), `discovery/errors.py` (`DiscoveryError`, `CatalogError`, `CatalogUnavailable`, `ScanBusy`, `SuggestionNotFound`, `AlreadyAccepted`), `discovery/models.py` (frozen dataclasses and constants from the design), `discovery/text.py`.
    - _Requirements: 4.1, 4.7_
  - [x] 1.2 Add the dependency rows and the static safety scans (M1) (M3) (L5)
    - Edit `tests/unit/test_dependency_direction.py` first (rows only): every `discovery/*.py` file is in group `discovery`; `catalog/` contains no `.py` file; `discovery` may import `core`, `store` and `services` (stdlib-only submodules, never `services.deps` or `services.routes`) and nothing else; `subprocess` only in `runner.py`; `fastapi` and `pydantic` only in `routes.py`; `os` only in `probe.py` and `runner.py`. The "file has no group" check must fail for a new `discovery/*.py` file that is not in the table.
    - Write `tests/unit/test_discovery_static.py`: AST scan of `src/chimericmesh/discovery/`: no import of `socket`, `ssl`, `http`, `urllib`, `httpx`, `ftplib`, `smtplib`, `telnetlib`, `xmlrpc`; no call named `open` (builtin, `io.open`, `Path.open`, `.read_text`, `.read_bytes`) outside `probe.py` (the `/proc/<int>/comm` read) and `catalog.py` (the packaged resource); no `shell=True`, `os.system`, `os.popen`, `os.exec*`, `create_subprocess_shell`; no attribute call `open_connection`, `create_connection`, `getaddrinfo`; `engine.py`, `match.py`, `collect.py`, `probe.py`, `runner.py` import nothing from `chimericmesh.store` or `chimericmesh.services` (M3: the thread-side code shares no hub, registry or store object); no string in `src/chimericmesh/discovery/` or in `tests/**/test_discovery_*` matches `p\d+-[A-Za-z0-9]+` (L5); ids in the sources start with `sc` or `sg`.
    - The tests pass on the files from 1.1 and on any later file added under `discovery/`.
    - _Requirements: 1.5, 2.1f, 2.2, 2.3, Success Criterion 5_
  - [x] 1.3 Write the property test for name normalization
    - Create `tests/pbt/test_discovery_text_properties.py` (arbitrary text including NUL, control characters, lone surrogates, separators, `.EXE`, 10 000-character strings, each OS name).
    - **Property 1: Name normalization**
    - **Validates: Requirements 4.1, 4.7**

- [x] 2. Catalog and path expansion
  - [x] 2.1 Implement environment and wildcard expansion
    - Write `tests/unit/test_discovery_expand.py` first: `%SystemDrive%\inetpub\logs\LogFiles\W3SVC*\*.log` on `windows` with a fake lister expands to the matching directories and files (case-insensitive match); `$HOME/logs/*.log` on `linux`; a placeholder not in `ALLOWED_ENV` (`%PATH%`, `$SECRET`) yields nothing; a missing variable yields nothing; a value with a control character, a relative value, or a placeholder not at the start yields nothing; the expanded value is a literal base: `HOME=/tmp/*` and a value containing `%ProgramData%` are not expanded again, and the lister is never called on a directory that is not under the literal base; at most 200 names are listed per pattern even with a 10 000 entry directory (fake lister records its `limit`); names with a separator or a control character are skipped; results are sorted and deterministic; `ntpath` and `posixpath` are chosen by `os_name`, not by the host (run both OS names on any host).
    - Create `discovery/expand.py` (`ALLOWED_ENV`, `expand_leading_env`, `expand_pattern`).
    - _Requirements: 2.2, 3.2, 3.6_
  - [x] 2.2 Implement the catalog validator and loader
    - Write `tests/unit/test_discovery_catalog.py` first: a minimal valid document loads; each rule of `validate_catalog` in the design has one failing case that names the field (unknown key, bad `app_id`, duplicate `app_id`, `.exe` in a process name, port 0 and 65536, an OS key missing, all OS lists empty, relative path, `..`, `**`, `[`, three wildcard components, a placeholder in the middle, a placeholder not in `ALLOWED_ENV`, a last component ending in `*`, a format not in `known_formats`, a bad `kind`, a non-core `app_id` without `justification`, a `mosquitto` entry without one and the same entry with one); `load_catalog(text=invalid)` returns `CatalogLoad(None, reason)` and logs one error and never raises; `load_catalog(text="not json")` and `text=""` do the same; `Catalog.process_index("linux")` is keyed by the first 15 characters of each name, `windows` and `macos` by the whole name; `fingerprint` is 16 hex characters, stable under rule order, different for different OS or pattern sets.
    - Create `discovery/catalog.py` (`PathRule`, `CatalogEntry`, `Catalog`, `CatalogLoad`, `validate_catalog`, `load_catalog`, `fingerprint`).
    - _Requirements: 3.1, 3.2, 3.4, 3.5_
  - [x] 2.3 Ship the catalog data and package it
    - Write `tests/unit/test_discovery_catalog_data.py` first: the packaged file loads through `importlib.resources.files("chimericmesh")` and validates with `frozenset(FORMAT_NAMES)` (from `chimericmesh.parsers.formats`); at least 14 entries and exactly the 15 core ids of the design; every format of every rule is in `FORMAT_NAMES` (Success Criterion 5); no entry mentions Mosquitto or MQTT; every `kind` and placeholder is allowed; `pyproject.toml` (parsed with `tomllib`) lists `catalog/*.json` under `[tool.setuptools.package-data]` next to `web/*.html`; the file path matches that pattern with `fnmatch`; a test builds a wheel with `python -m pip wheel . --no-deps --no-build-isolation -w <tmp>` and asserts `chimericmesh/catalog/legacy_apps.json` is inside (skipped when `CHIMERICMESH_SKIP_BUILD=1` or when `pip` or `setuptools` cannot be imported); a loaded catalog with the contingency edit (`w3c` and `xferlog` replaced by `plain` in the text) still validates.
    - Create `src/chimericmesh/catalog/legacy_apps.json` from the design table (no `__init__.py`), edit `pyproject.toml` (`package-data`).
    - _Requirements: 3.1, 3.3, 3.4, Success Criterion 5_
  - [x] 2.4 Write the property test for placeholder expansion
    - Create `tests/pbt/test_discovery_expand_properties.py` (arbitrary pattern text, arbitrary environment mappings with a recording mapping spy).
    - **Property 2: Placeholder expansion allow-list**
    - **Validates: Requirements 3.2, 3.6**
  - [x]* 2.5 Write property tests for wildcard expansion and the validator
    - Create `tests/pbt/test_discovery_catalog_properties.py` (generated directory trees; JSON-like values; single-fault mutations of the shipped catalog).
    - **Property 3: Wildcard expansion is bounded and sound**
    - **Property 4: Catalog validator is total**
    - **Validates: Requirements 2.2, 3.1, 3.2, 3.4, 3.5**

- [x] 3. OS tools, parsers and the runner
  - [x] 3.1 Implement the output parsers
    - Write `tests/unit/test_discovery_parse.py` first, with fixture output of each tool: `ss -ltnup` lines with and without `users:(("nginx",pid=812,fd=6),...)`, IPv6 `[::]:80`, UDP; `netstat -ltnup` with `812/nginx` and `-`; `tasklist /fo csv /nh` with quoted names containing spaces and commas; `netstat -ano` TCP listening (foreign `0.0.0.0:0` and `[::]:0`), TCP established (skipped), UDP, a localized state word, IPv6; `ps -axo pid=,comm=` with a path containing spaces; `lsof` with `\x20` in the command and `*:80 (LISTEN)`, `127.0.0.1:5353`; empty input, header-only input and a truncated last line give empty or partial results and no exception.
    - Create `discovery/parse.py` (the six parsers; names pass through `normalize_process_name`; every result capped at 20000).
    - _Requirements: 2.1, 4.7_
  - [x] 3.2 Write the property test for parsers
    - Create `tests/pbt/test_discovery_parse_properties.py` (arbitrary text, arbitrary bytes decoded with `replace`, very long lines).
    - **Property 5: Parser totality**
    - **Validates: Requirements 2.4, 4.7**
  - [x] 3.3 Implement the command runner
    - Write `tests/unit/test_discovery_runner.py` first, using real child processes started through a private `run_argv` entry (the public `run(tool)` resolves system tools): a child that sleeps is killed at the timeout and the result is `timeout` within 1 second of it; a child that prints 5 MB returns at most `max_bytes` with `truncated=True` and the child is gone; an argument containing `;`, `&&` and `$(x)` reaches the child literally (no shell); a nonexistent executable gives `missing`; a non-executable file gives `denied` or `error` without raising; setting `cancel` kills the child within 0.5 second and the result is `cancelled`; stdin is closed; the child's environment contains only `SystemRoot`, `LC_ALL`, `PATH`; `resolve_tool` never returns a path under the current directory or from the caller's `PATH` (plant `netstat.exe`, `ss` and `tasklist` in a temp working directory and put it first on `PATH`; assert they are not chosen), returns `None` for an unknown tool name, and uses `<SystemRoot>\System32` on `windows` (pure test with a fake `exists`). (L8) Sub-criteria 2.1f, 2.1g, 2.1h are named in the test ids.
    - Create `discovery/runner.py` (`CommandRunner`, `SubprocessRunner`, `resolve_tool`; reader thread, 50 ms wait slices, kill on timeout, cap or cancel).
    - _Requirements: 2.1, 2.4, 2.5_
  - [x] 3.4 Implement the probes and `collect`
    - Write `tests/unit/test_discovery_collect.py` and `tests/unit/test_discovery_probe.py` first. (L8) `tool_plan("linux")`, `("windows")`, `("macos")` equal the R2.1 lists exactly as `ToolSpec` data (2.1a, 2.1d, 2.1e); `netstat -ltnup` runs only when `ss` is missing, errors or times out (2.1b); Linux names come from `ProcFs.comm` (2.1c); with a fake runner a missing tool is recorded and the others still run, the snapshot is `limited` and usable (2.5a); a timed-out tool is treated the same (2.5b); all tools failing gives `usable=False` and the names in `failed_tools` (2.5c); denied output sets `limited` and no retry or elevation call exists (a fake runner counts calls: exactly one per tool); a non-zero exit with output is parsed and `limited`; a Linux `ss` line without a process and `is_admin=False` sets `limited`, with `is_admin=True` it does not; `macos` with `is_admin=False` is `limited`; the 30-second deadline: a fake clock past the deadline marks the remaining tools failed with `timeout` and keeps the earlier data; the own pid is removed from procs and listeners; the raw process list is not an attribute of anything returned except `Snapshot` (R2.7: the returned `Snapshot` is dropped by the engine, tested in 4.2). Probe: `OsFsProbe.stat` returns `(False, None)` for a missing path and `(True, size)` for a file; `readable` matches `os.access`; `listdir` returns at most `limit` names and never calls `open` (patched to fail); `LinuxProcFs` is skipped on non-Linux hosts, and with a fake `/proc` directory it reads only `/proc/<int>/comm` and skips non-numeric entries and `PermissionError`.
    - Create `discovery/probe.py` (`FsProbe`, `ProcFs`, `OsFsProbe`, `LinuxProcFs`), `discovery/collect.py` (`tool_plan`, `collect_snapshot`).
    - _Requirements: 2.1, 2.2, 2.4, 2.5, 2.6, 2.7_

- [x] 4. Matching and the scan engine
  - [x] 4.1 Implement matching and suggestions
    - Write `tests/unit/test_discovery_match.py` first with a small test catalog (not the shipped one) and fake `FsProbe` (`stat` returns `(exists, size, mtime)`, `listdir` returns `(names, truncated)`). (L8) 4.3a to 4.3d: a high-confidence suggestion has every listed field and `evidence` holds process name, lowest pid, `process_count` and ports and no command line; a low-confidence suggestion has `process: None`; candidates have `path`, `format`, `kind`, `exists`, `readable`, `size_bytes`. Process match (R4.1): `NGINX.EXE`, `/usr/sbin/nginx`, and a Linux comm truncated to 15 characters match; no match gives nothing. Port-only (R4.2): port 21 with no process gives low suggestions for the entries listing 21; port 80 with no process gives none (not in `PORT_ONLY_PORTS`); with a matched `vsftpd` on 21 the other FTP entries are not offered (claimed-port rule); a listener owned by the hub's pid is ignored. Dedup (R4.5): three `nginx` processes give one suggestion with `process_count` 3 and the lowest pid. Candidates (R4.4): existing first, missing kept with `exists: false`, a wildcard with no match gives none, two rules with the same path keep the first, more than 25 are cut. Covered (R4.6, one rule: drop when at least one candidate is covered and every candidate with `exists: true` is covered): all existing candidates covered drops it; one uncovered existing candidate keeps it with `watched_by` on the others; no existing candidate and one non-existing candidate covered drops it; a covered non-existing plus an uncovered non-existing candidate (none existing) drops it; case-insensitive on `windows` (finding 8). Wildcard staleness (finding 1): a fake directory with 300 daily files whose newest by `mtime` sorts last gives that file as the first candidate of its rule and keeps it under the 25 cap; a directory the lister reports as truncated, and a cut by the 25 cap, each set `limited` with reason `candidate list truncated`. Time limit (finding 5): `build_suggestions` takes `deadline`, `cancel` and `clock`; a fake clock past the deadline, or a set `cancel`, stops it between rules and between candidates and returns what was built with `limited` and reason `scan time limit` (a slow `FsProbe` advancing the fake clock triggers it). Ids: two calls sharing one `next_id` counter never repeat an id (finding 11). Dismissed: a matching `(app_id, fingerprint)` drops it, a different fingerprint does not. Cap (R4.8): 60 matching entries give 50 suggestions, high first, ids `sg1..sg50` assigned only to kept ones. Control characters: a process name and a directory entry name with NUL and ESC never appear in any output; a listing name with a control character is skipped.
    - Create `discovery/match.py` (`build_suggestions`, `MatchResult` and helpers).
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8_
  - [x] 4.2 Implement the engine and prove the scan guarantees (M3)
    - Write `tests/unit/test_discovery_engine.py` first. Success Criterion 1: a fake runner that reports `nginx` and `vsftpd` on Linux, a catalog entry per app whose log paths sit under `$HOME` with `env={"HOME": str(tmp_path)}` and real files created there, and the real `OsFsProbe`: the scan returns two high-confidence suggestions with the right formats, `open` (patched to raise for any path under `tmp_path`, passing `/proc` and the catalog) was never called, and `os.stat`, `os.access`, `os.scandir` calls only name candidate paths or their parents (R2.2). Success Criterion 2: `socket.socket` patched to raise (the loop is created first; the engine is also called directly) and the scan completes. R2.4: a denied tool completes with `limited` and no second attempt. R2.6: with a fake clock the scan stops at 30 seconds in the tool phase, and a slow `FsProbe` that advances the same fake clock stops the match phase too: the outcome is `done` with `limited` and reason `scan time limit` (finding 5). R2.7: after `run_scan_sync` returns, a recursive attribute walk of the `ScanOutcome` finds no process name that is not a matched app, and `json.dumps(outcome)` contains none of the unmatched canary names. (M3) `run_scan_sync` is called with objects that have no reference to a `ServiceRegistry`, `Store` or `Hub` (its signature takes none), and the static test of 1.2 still passes.
    - Create `discovery/engine.py` (`run_scan_sync`).
    - _Requirements: 2.2, 2.3, 2.4, 2.6, 2.7, Success Criteria 1, 2_
  - [x] 4.3 Write property tests for matching
    - Create `tests/pbt/test_discovery_match_properties.py` (random snapshots with repeated, shuffled and hostile names over the shipped catalog; random candidate and covered sets).
    - **Property 6: Match soundness**
    - **Property 7: Deduplication and caps**
    - **Property 8: Control characters never escape**
    - **Property 9: Covered rule**
    - **Validates: Requirements 2.7, 4.1, 4.2, 4.5, 4.6, 4.7, 4.8**
  - [x]* 4.4 Write the property test for candidate order
    - Create `tests/pbt/test_discovery_order_properties.py` (fake file systems with random existing subsets and random mtimes).
    - **Property 10: Existing paths first, newest first within a wildcard**
    - **Validates: Requirements 4.4**

- [x] 5. Scan manager and dismissals
  - [x] 5.1 Implement the dismissal table and its operations
    - Write `tests/unit/test_discovery_dismissals.py` first, on a `:memory:` store and on a temporary file opened with `discovery_registry()`: the migration `discovery:0300-dismissals` runs once and `user_version` is at least 300; `dismiss` is idempotent and writes one audit row `discovery_dismissed` in the same transaction (a failing audit append rolls the row back); a repeat dismiss of the same pair writes no second audit row and trims nothing (finding 10); the table never holds more than 500 rows (the oldest goes first); `load_fingerprints` returns a `frozenset` of pairs; `clear` deletes all rows, audits `discovery_dismissals_cleared` with the count and returns it; rows survive a restart; `register_migrations` rejects nothing and `MODULE_BLOCKS["discovery"]` contains 300; every SQL statement in `dismissals.py` is a string literal (the store's SQL scan covers `store/` only, so this test asserts it for this module).
    - Create `discovery/schema.py` (`register_migrations`, `discovery_registry`), `discovery/dismissals.py`.
    - _Requirements: 5.4, 5.7_
  - [x] 5.2 Implement the scan manager
    - Write `tests/unit/test_discovery_manager.py` first (fake clock, a fake engine function, `asyncio.run` inside plain tests, a real `ServiceRegistry` on a `:memory:` store or a stub with `log_sources()`): constructing the manager starts nothing and calls neither the engine nor the runner (R1.1); `start` returns a `running` scan with id `sc1` and the engine runs once; a second `start` while running raises `ScanBusy(scan_id)` (R1.3); two `asyncio.gather` starts give one scan and one `ScanBusy` (the scan is registered before the first await); a `load_fingerprints` that raises `RuntimeError`, and a `start` cancelled during the dismissal read, each leave the scan `failed` with reason `internal error`, one audit row and no `running` scan, so a later `start` succeeds (finding 4); an unsupported platform (`os_name=None`) gives a `failed` scan with reason `unsupported platform` and the engine is never called (finding 9); scan ids and suggestion ids are monotonic across scans, never repeat while the process lives, and `find` is unambiguous with three kept scans (finding 11); after the engine finishes, `get` shows `done`, `started_at`, `finished_at`, `limited`, `os` and the suggestions (R1.4); an engine exception gives `failed` with reason `internal error` and one `log.exception`; an engine that does not return within 35 seconds (fake) gives `failed`, reason `scan timed out`, and sets `cancel`; the engine runs on the manager's own `ThreadPoolExecutor(max_workers=1, thread_name_prefix="discovery-scan")` through `run_in_executor` (the test asserts the worker thread name) (finding 6); with `load.catalog is None`, `start` raises `CatalogUnavailable` and the engine is never called (R3.5); one audit row `discovery_scan` per scan with counts only (a canary process name in the outcome never appears in any audit column, R1.6); a failing audit append is logged and the scan is still `done`; a store failure while reading dismissals still runs the scan with none; the `covered` input is built from `registry.log_sources()` with `path_key`; scans older than 3600 seconds after `finished_at` are dropped and `find` returns `None` for their suggestions (R5.6); at most 5 scans are kept and a running scan is never dropped; `dismiss(suggestion_id)` writes the dismissal and audit row and a repeat is harmless and writes no second audit row; dismiss of an accepted or being-accepted suggestion raises `AlreadyAccepted`; an unknown id raises `SuggestionNotFound`; `aclose` sets `cancel`, cancels the task, calls `executor.shutdown(wait=False, cancel_futures=True)` and returns within 0.2 second even with a blocked engine. The fake blocked engine waits on the `cancel` event with a 10 second safety timeout, never on a bare `Event().wait()`, and the test ends with `asyncio.run` returning without hanging (finding 6).
    - Create `discovery/manager.py` (`ScanManager`, `DiscoveryService`; register-first `start`, monotonic `sc` and `sg` counters, dedicated executor). `os_name_from_platform` is added to `discovery/text.py` in 1.1.
    - _Requirements: 1.1, 1.3, 1.4, 1.6, 3.5, 5.4, 5.6_
  - [x]* 5.3 Write property tests for the manager and dismissals
    - Create `tests/pbt/test_discovery_manager_properties.py` (random operation sequences with a fake clock; temporary file store with restarts).
    - **Property 11: Scan state machine**
    - **Property 12: Dismissals round trip**
    - **Validates: Requirements 1.3, 5.4, 5.6**

- [ ] 6. Accept
  - [ ] 6.1 Implement validation, the sink port and the acceptor
    - Write `tests/unit/test_discovery_accept.py` first, with a real `ServiceRegistry` on a `:memory:` store opened with `discovery_registry()` and a stub manager holding one suggestion. (L8) 5.2a: accepting creates the service with its log sources through the registry (audit rows `service_created` and `log_source_added` exist with actor `discovery`); 5.2b: every log source has origin `discovery` and the service has `protocols == ("file",)`; 5.2c: the body fields `id`, `name`, `log_paths` with formats and optional `code_folder` are honoured, `app_names` defaults to the matched process name and an explicit list overrides it. Atomicity (R5.5): a bad format, a relative path, a directory path, a device path, a duplicate path after normalization, 21 paths, zero paths, a bad slug, `unassigned`, or an unknown `known_formats` name each give a 422-class error with the field name and leave the registry, the tombstones and the audit log without any new `service_*` row. `code_folder` is passed unchanged to the registry, which normalizes and validates it (finding 3, O12, O16): the home directory, a filesystem root, the data directory, a relative path and a file each give 422 `code_folder` and nothing exists; a valid folder is stored normalized. Caps (finding 2): with 190 log sources already registered, accepting 20 paths gives 422 `log_paths` with no new service, no tombstone and no `service_*` audit row; with 100 services, accept gives 422 `id`; the same with a `ServiceSink` fake that records zero calls. Concurrency (finding 10): two `asyncio.gather` accepts of one suggestion with different service ids give one 201 and one `AlreadyAccepted`, and exactly one service exists; after a failed accept the marker is cleared and a corrected accept succeeds; accepting after a dismiss in the same scan is allowed. A service id that exists gives `Conflict` (409) and nothing new. A missing or expired suggestion raises `SuggestionNotFound`; a second accept of the same suggestion raises `AlreadyAccepted`. Scanning state: nothing is created and nothing is tailed until `accept` is called (R5.1: the registry is untouched after a scan). After accept the suggestion shows `state: accepted` with the service id. One audit row `discovery_accepted` with `app_id`, number of paths and the `service_id` reference (R5.7). `RegistrySink` (O2, accepted 2026-11-18): it makes exactly one `create_service_with_sources` call with actor `discovery` and `origin="discovery"`; when an in-transaction subscriber raises on the second log source, no service, no log source and no tombstone exist, the original error is re-raised, and a retry with the same id succeeds. A `ServiceSink` fake that records calls is used for the non-registry cases.
    - Also write first (O16, user decision 2026-11-18, option A: `code_folder` is only ever user-typed):
      - [ ] Suggestion and scan payloads never include `code_folder`: scan a fake tree containing source files and assert no suggestion or scan field proposes a folder.
        - _Requirements: 4.3, 1.4_
      - [ ] Accept with a user-typed valid folder creates the service with that folder (normalized by the registry).
        - _Requirements: 5.2_
      - [ ] Accept with a too-broad folder (the home directory, a filesystem root, the data directory) is refused with the services error and creates nothing (all or nothing).
        - _Requirements: 5.2, 5.5_
      - [ ] Accept without the admin token is refused (401) and creates nothing.
        - _Requirements: 5.1, 5.2_
    - Create `discovery/accept.py` (`AcceptBody`, `ServiceSink`, `RegistrySink`, `Acceptor(manager, sink, registry, store, known_formats)`). Needs `services` task 3.2a (`create_service_with_sources`) merged.
    - _Requirements: 5.1, 5.2, 5.3, 5.5, 5.7_
  - [ ] 6.2 Write the property test for accept atomicity
    - Create `tests/pbt/test_discovery_accept_properties.py` (random path lists with one invalid entry at a random position; a real registry on `:memory:`; hostile names).
    - **Property 13: Accept is all or nothing**
    - **Validates: Requirements 5.2, 5.5**

- [ ] 7. REST routes
  - [ ] 7.1 Implement the router and the route and tool tests
    - Write `tests/unit/test_discovery_routes.py` first, with `httpx.ASGITransport` on a small FastAPI app that mounts `build_router(service, require_admin)`, built with a real `AccessControl(enforce=True, tokens=TokenStore.open(None, in_memory=True), ...)` and a fake runner that counts calls. Without a token: every discovery route returns 401, the fake runner and the manager are never called, and one `auth_failed` audit row per request exists (Success Criterion 3, R1.2). With the token: `POST /api/discovery/scan` returns 202 with `scan_id`; a second start while running returns 409 with the running id (R1.3); with a disabled catalog it returns 503 with the plain sentence and no path or parser text; `GET /api/discovery/scans/{id}` returns `state`, `started_at`, `finished_at`, `limited`, `os` and suggestions (R1.4) and 404 for an unknown or expired id; `POST .../accept` returns 201 with `service` containing its log sources (5.2a), 422 `{"detail", "field"}` for an invalid path with nothing created (R5.5), 404 for an unknown suggestion (R5.6), 409 for an accepted one and for an existing service id; `POST .../dismiss` returns 200, repeats are harmless and the app is absent from the next scan (R5.4); `GET` and `DELETE /api/discovery/dismissals` list and clear (O4); a hostile body (extra key, wrong type, 1 MB) gives 422 or 413. No MCP tool (finding 14): the test reuses the tool-listing helper that `tests/unit/test_kiro_assets.py` already uses (read it first; do not call an unverified `MCPServer` method), asserts no tool name starts with `discovery_`, `scan_` or `suggest`, and asserts the text of `mcp_server.py` contains no `/api/discovery` (R1.5). It does not use a loose substring check, so a later `agent-loop` tool with `scan` inside its name does not break it. Dismiss of an accepted suggestion returns 409.
    - Create `discovery/routes.py` (`build_router`, request models with `extra="forbid"`, error mapping: `ValidationFailed` to 422, `Conflict` to 409, `SubscriberFailed` to 500, `ScanBusy` to 409, `CatalogUnavailable` to 503, `SuggestionNotFound` to 404, `AlreadyAccepted` to 409).
    - _Requirements: 1.2, 1.3, 1.4, 1.5, 3.1, 5.2, 5.4, 5.5, 5.6_

- [ ] 8. Wire into `create_app`, acceptance and final checkpoint
  - [ ] 8.1 `create_app` wiring and the shutdown step
    - Write `tests/unit/test_discovery_wiring.py` first: `build_registry()` includes the discovery migration (`discovery:0300-dismissals` applied on a new store, `user_version` at least 300); `create_app()` sets `app.state.discovery`; with an injected bare hub and the private `:memory:` store (M2, both modes) and with a store-backed hub the router answers `POST /api/discovery/scan` with 401 when enforcement is on and with 202 when `enforce_access=False`; building the app, entering and leaving the lifespan, and calling every other existing route once leaves a spy runner's call count at zero (R1.1); the shipped catalog loads with `frozenset(FORMAT_NAMES)` inside `create_app` (every format known to the registry, Success Criterion 5) and `DEFAULT_FORMATS == frozenset(FORMAT_NAMES)` so the two lists cannot drift (finding 7; this task needs `connectors` merged); `os_name_from_platform(sys.platform)` is called once in `create_app`, and a `None` result still lets the app start and gives a `failed` scan; `Acceptor` receives the same `protected` tuple as the registry (finding 3); the discovery executor thread is not a daemon-joined default-executor thread, so the lifespan test ends without waiting for a blocked fake engine (finding 6); a catalog that cannot load (monkeypatch `load_catalog`) leaves the app running, the rest of the routes working and the scan route at 503 (R3.5); the lifespan shutdown stops a running scan within 0.2 second and the remaining steps keep at least 3 seconds for `hub.aclose` (the shared deadline test of `services` task 7.4 is re-run unchanged); the dependency test and the static test pass for the whole tree.
    - Edit `src/chimericmesh/api.py` (`build_registry`, build `DiscoveryService` from `app.state.store`, the registry, `hub.now`, `os_name_from_platform(sys.platform)`, `os.geteuid` or `ctypes` admin check done in `api.py` (wiring), `os.getpid`, `os.environ` copy, `frozenset(FORMAT_NAMES)`; include the router with `require_admin` and the REST actor helper; one shutdown step `await discovery.aclose(min(0.2, remaining))` before the connector steps; set `app.state.discovery`).
    - _Requirements: 1.1, 3.1, 3.5, Success Criterion 5_
  - [ ] 8.2 Integration tests for the success criteria
    - Write `tests/unit/test_discovery_integration.py` (`create_app` and `httpx.ASGITransport` on a temporary database, lifespan entered with `asyncio.run`, a fake runner and a temp directory under `$HOME`): Success Criterion 1 end to end (two suggestions, `open` not called); Success Criterion 2 (`socket.socket` patched after the loop exists); Success Criterion 3 (no token, 401, runner never called); Success Criterion 4: scan, accept a suggestion whose path is a temp log file with format `plain`, append a line, and an event with the new `service_id` appears in `GET /api/events` within 2 seconds (uses the real `connectors` manager; skipped with a clear message if `connectors` is not merged); a second scan no longer suggests the accepted app (R4.6); a dismissed app stays hidden after an app restart on the same database and returns after `DELETE /api/discovery/dismissals` (R5.4); the audit log holds `discovery_scan`, `discovery_accepted`, `discovery_dismissed` rows and no process name or path (R1.6, R5.7).
    - _Requirements: 1.6, 4.6, 5.1, 5.2, 5.4, 5.7, Success Criteria 1, 2, 3, 4_
  - [ ] 8.3 README, manual checks and the demo note
    - Write `tests/unit/test_discovery_readme.py` first: the README has a Discovery section that says the scan is opt-in and needs the admin token, lists the OS tools by name, says it never reads log contents and makes no network connection, explains `limited`, says suggestions are kept for one hour, says discovery cannot be started by an agent (no MCP tool), points to the dashboard flow and to adding a path by hand, says accepted paths are the files that exist at accept time and "a rotating log needs its new file added by hand; patterns are not stored" (finding 1, O11), says IIS may not appear until `w3wp` has started after the first request (finding 12, O13), and names `catalog/legacy_apps.json` as the file to edit (and the contingency rule: when `w3c` or `xferlog` is dropped, change them to `plain`).
    - Edit `README.md` (Discovery section). Add a "Manual checks" list in the same section: (1) verify the catalog paths against vendor documentation or a sample machine for each of the 15 entries (R3.3); (2) Success Criterion 6: on the Windows development machine run a scan with a real IIS or Apache install if present, otherwise with the demo `legacy_app` running (it is not in the catalog and is added by hand to show the manual path; `examples/legacy_app/upstream.py` is not changed by this module); check the suggestion list and the `limited` flag are sensible; (3) one run as a non-admin user on Linux to see `limited`; (4) the live demo rehearsal with nginx or Apache; (5) on a Windows machine with IIS installed, scan once before and once after the first request to the site and check that `iis` appears only after `w3wp` runs (finding 12); (6) capture real output of `ss -ltnup`, `netstat -ltnup`, `lsof`, and `netstat -ano` on a non-English Windows and save them as fixtures for 3.1.
    - _Requirements: 3.3, Success Criterion 6_
  - [ ] 8.4 Final checkpoint
    - Run `python -m pytest -q`. The full existing suite (no baseline test edited) and all new tests must pass. Confirm by `git diff --stat tests/` that only new files were added under `tests/`, except rows added to `tests/unit/test_dependency_direction.py`.
    - Walk Success Criteria 1 to 7 one by one against the tests that cover them (1: 4.2 and 8.2; 2: 4.2 and 8.2; 3: 7.1 and 8.2; 4: 8.2; 5: 2.3, 8.1, 1.2; 6: manual, 8.3; 7: properties 1, 2, 7, 8) and note any gap in this file.
    - Confirm services N1 (`create_service_with_sources`, accepted 2026-11-18) is merged and used by `RegistrySink`, and that the other items named in "Needs from other modules" (services N2; agent-loop A1, A2) are either done in those modules or their fallbacks (wiring-supplied formats, broader allow-list wording) are in place.
    - _Requirements: all; Success Criterion 5_

## Requirement coverage

Every requirement of `requirements.md` appears in at least one non-optional task.

| Requirement | Tasks | Requirement | Tasks |
|---|---|---|---|
| 1.1 | 5.2, 8.1 | 4.1 | 1.1, 1.3, 4.1, 4.3 |
| 1.2 | 7.1, 8.2 | 4.2 | 4.1, 4.3 |
| 1.3 | 5.2, 7.1 | 4.3 | 4.1 |
| 1.4 | 5.2, 7.1 | 4.4 | 4.1, 4.4 |
| 1.5 | 1.2, 7.1 | 4.5 | 4.1, 4.3 |
| 1.6 | 5.2, 8.2 | 4.6 | 4.1, 4.3, 8.2 |
| 2.1 | 3.1, 3.3, 3.4 | 4.7 | 1.1, 1.3, 3.1, 3.2, 4.1, 4.3 |
| 2.2 | 1.2, 2.1, 3.4, 4.2 | 4.8 | 4.1, 4.3 |
| 2.3 | 1.2, 4.2, 8.2 | 5.1 | 6.1, 8.2 |
| 2.4 | 3.2, 3.3, 3.4, 4.2 | 5.2 | 6.1, 6.2, 7.1, 8.2 |
| 2.5 | 3.3, 3.4 | 5.3 | 6.1 |
| 2.6 | 3.4, 4.2 | 5.4 | 5.1, 5.2, 7.1, 8.2 |
| 2.7 | 3.4, 4.2, 4.3 | 5.5 | 6.1, 6.2, 7.1 |
| 3.1 | 2.2, 2.3, 7.1, 8.1 | 5.6 | 5.2, 7.1 |
| 3.2 | 2.1, 2.2, 2.4 | 5.7 | 5.1, 6.1, 8.2 |
| 3.3 | 2.3, 8.3 | | |
| 3.4 | 2.2, 2.3 | | |
| 3.5 | 2.2, 5.2, 8.1 | | |
| 3.6 | 2.1, 2.4 | | |

## Review finding references

| Finding | Where it became work |
|---|---|
| M1 | 1.2 (rows for every `discovery/*.py`, `catalog/` data-only rule) |
| M3 | 1.2 (engine imports), 4.2 (inputs and outputs immutable) |
| L5 | 1.2 (no `p<n>-<tag>` string, ids `sc`, `sg`) |
| L8 | 3.3, 3.4, 4.1, 6.1 (sub-criteria named in test ids) |
| Design review findings 1 to 15 (this round) | See the design, "Responses to the design review": 1 in 2.1, 3.4, 4.1, 4.4, 8.3; 2 and 3 in 6.1, 8.1; 4 and 6 in 5.2, 8.1; 5 in 4.1, 4.2; 7 in the header, 2.3, 8.1; 8 in 4.1; 9 in 1.1, 5.2, 8.1; 10 in 5.1, 5.2, 6.1, 7.1; 11 in 4.1, 5.2; 12 in 8.3; 14 in 7.1; 15 in 2.3. Finding 13 needs nothing beyond the existing hub-pid test in 4.1. |
| M2, M4, L1, L2, L3, L4, L6, L7, L9 | No work for this module; reasons in the design, "Review resolutions". M2 is exercised in 8.1, L2 in 7.1, L6 in 8.3. |

## Effort estimate

Counted against the measured `store` velocity (about 5 to 11 minutes per top-level task, about 2.5 minutes per sub-task including tests). This list has 8 top-level tasks and 26 sub-tasks: 23 required (one is the final checkpoint) and 3 optional property tasks (2.5, 4.4, 5.3).

| Task | Required sub-tasks | Agent minutes (required) | Optional |
|---|---|---|---|
| 1. Skeleton, text, static rules | 1.1 to 1.3 | 8 | none |
| 2. Catalog and expansion | 2.1 to 2.4 | 14 (the JSON data and its tests are the largest piece) | 2.5: 3 |
| 3. Tools, parsers, runner | 3.1 to 3.4 | 14 (real child-process tests and fixtures) | none |
| 4. Matching and engine | 4.1 to 4.3 | 13 | 4.4: 2 |
| 5. Manager and dismissals | 5.1, 5.2 | 8 | 5.3: 3 |
| 6. Accept | 6.1, 6.2 | 6 | none |
| 7. REST routes | 7.1 | 5 | none |
| 8. Wiring, acceptance, README, checkpoint | 8.1 to 8.4 | 12 | none |
| **Total** | **23** | **about 80 minutes** | **8 minutes** |

**Agent build time:** about 1.3 hours for the required sub-tasks, about 1.5 hours with the optional properties, and 2 to 2.5 hours if one in three sub-tasks needs a second pass (the likeliest causes are Windows path handling in the pure `ntpath` and `posixpath` code, child-process cleanup in the runner tests, and thread cleanup in the manager tests). The map's 1.25 days is therefore an upper bound on calendar time that is dominated by the human steps below.

**Human-in-the-loop work (separate from agent time):**

| Step | Time |
|---|---|
| Review of the design and these tasks; answers to open items O1 to O15 | 20 to 30 minutes |
| Decision on services N1 (atomic create; accepted 2026-11-18, services task 3.2a) and agent-loop A1 (narrow allow-list) | done for N1; 5 minutes for A1 |
| Catalog paths check against vendor docs or sample machines (manual, 15 entries) | 30 to 45 minutes |
| Manual scan on the Windows development machine, with and without a real Apache or IIS (Success Criterion 6) | 15 to 25 minutes |
| Non-admin Linux check of the `limited` flag (if a Linux machine or WSL is at hand) | 10 minutes |
| Demo rehearsal: install nginx or Apache, scan, accept, append an error line, watch the incident | 30 to 45 minutes |
| README and merge review | 15 to 20 minutes |
| **Total** | **about 2 to 3 hours** |

The browser check of the discovery screen belongs to the `dashboard` module's estimate.
