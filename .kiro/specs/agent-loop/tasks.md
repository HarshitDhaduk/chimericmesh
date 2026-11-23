# Implementation Plan: agent-loop (V1)

Requirements: [requirements.md](requirements.md). Design: [design.md](design.md). Property numbers below are the numbered Correctness Properties in the design; sub-criteria 3.1a to 3.1f (R3.1) and 4.3a.1 to 4.3b (R4.3) are defined in the design (L8).

Preconditions: the `store`, `services`, `connectors`, `discovery` and `auto-approval` modules are merged (their `Hub`, `create_app`, `StateView`, `ServiceRegistry`, `ApprovalManager`, `approvals.scope` and the public `diagnostics.CODE_SUFFIXES` and `SKIP_DIRS` exist as designed). This module edits the same regions of `api.py` and `mcp_server.py` as those modules, on top of their result. It does not edit `hub.py`. Nothing here is committed by the agent. **No task edits, restores or stages `examples/legacy_app/upstream.py`** (the user's uncommitted demo edit; L6 is a user action), and none edits any `requirements.md`, `docs/v1-capability-map.md` or the worktree `e:\Projects\Kiroween-2026-store`.

Conventions for every task: write the tests first and watch them fail, then implement; run the new tests and `python -m pytest -q`; no task edits an existing test except 9.2 (`test_power_docs_name_only_real_tools_and_cover_all`, the one change the requirements allow, R6.2). Tasks marked `*` (Kiro's optional marker, `- [ ]*`) are optional property tests that may be skipped for a faster first pass; every requirement is also covered by a non-optional example test. Every property task **not** marked `*` is required. The optional tasks are 4.3 (property 6), 5.4 (8, 9) and 10.3 (11). Required properties: 4 (1.4); 1, 2 (2.3); 3 (3.2); 5 (4.2); 7 (5.3); 10 (6.2). Success Criterion 4 names 1, 5 and 7; all are required. Stack: Python 3.11+, stdlib, FastAPI and pydantic in `agent/routes.py` only, `httpx` and the MCP SDK v2 in `mcp_server.py` only, pytest, Hypothesis (already in the `dev` extra). New files under `src/chimericmesh/agent/` unless a path is given. Each sub-task touches at most five files. Test helper `tests/unit/agent_helpers.py` is created in 2.1 and exposes `make_agent(mode)` for `mode in ("bare", "store")`, a `FakeState`, and builders for incidents and proposals.

Review ids (design, "Review resolutions"): M2 (named interfaces, both hub modes) is work in 1.1, 7.1, 12.1; L2 (every tool in the skills and in `getting-started`) in 9.2; L4 (public lists in `core`) in 1.1, 3.1; L6 (demo precondition, user action) in 11.1, 11.2, 12.2; L8 (split R3.1 and R4.3) in 4.1, 5.1; L9 (`~/` deny globs, check live) in 9.1, 11.1, 12.2; README (test-command risk) in 11.1. Each sub-task that carries one lists it as a checklist line ending with the id, for example `- [ ] (L8) ...`.

Design-review round 1 ids (`R1-H1`, `R1-M1` to `R1-M7`, `R1-L1` to `R1-L10`; design, "Responses to the design review, round 1"): H1 (absolute `files`) in 4.1, 9.1, 10.1; M1 (disconnect) in 5.2, 7.2; M2 (graceful shutdown) in 7.3; M3 (workspace root) in 6.1, 7.3; M4 (verification fails after the fix) in 9.1, 9.2; M5 (hook matcher) in 10.2; M6 (25 s wait, early check, env fallback) in 1.2, 7.2, 9.3, 11.1; M7 (digest scan) in 2.2; L1 in 7.1; L2 in 1.1; L3 in 7.1; L4, L5 in 4.1; L6 in 4.4\* and 11.1 (user decision O13); L7 in 9.1, 9.2; L8 in 6.1, 12.2 (manual); L9 in 11.1; L10 in 1.5, 9.1, 11.2, 12.2. Checklist lines read `- [ ] (R1-xx) ...`.

- [x] 1. Package skeleton, shared limits and static guards
  - [x] 1.1 Create the package skeleton, ports and dependency rows
    - Extend `tests/unit/test_dependency_direction.py` first (rows only, as the services task 1.3 allows): every `agent/*.py` is group `agent-loop`; it may import `core`, `store`, `services` (stdlib-only submodules, never `services.deps` or `services.routes`) and `approvals` (only `approvals.scope` and `approvals.models`, never `approvals.routes`, `approvals.manager`); only `agent/routes.py` may import `fastapi`, `starlette` or `pydantic`; `agent/__init__.py` imports none of them; no `agent/*` file imports `hub`, `api`, `commands`, `listeners`, `mcp_server`, `connectors` or `discovery`. Write `tests/unit/test_agent_skeleton.py`: `diagnostics.SKIP_DIRS` and `diagnostics.CODE_SUFFIXES` are public `frozenset`s; `approvals.scope.PROTECTED_PATTERNS`, `SOURCE_SUFFIXES`, `check_file`, `glob_to_regex` exist; `ApprovalReader` is a `Protocol` whose method names are exactly `approval_of`, `rule`, `status` (no mutator name such as `create_rule`, `revoke_rule`, `resume`, `enable_rule`, `patch_rule`, `pause` is a member); an `ApprovalManager` instance satisfies it (`isinstance` with `runtime_checkable`); `AgentPorts` is frozen.
    - Create `agent/__init__.py` (empty re-exports), `agent/errors.py` (`AgentError`, `UnknownService`, `UnknownProposal`, `NoCodeFolder`, `InvalidQuery`, `TooManyWaiters(retry_after)`, `SearchBusy`), `agent/ports.py` (`ApprovalReader`, `AgentPorts`).
    - [x] (M2) The three injected interfaces are named and `agent/*` imports no `hub` (the dependency rows above).
    - [x] (L4) The public-name assertions above (the rename itself is `auto-approval` task 1.1).
    - [x] (R1-L2) Prose and docstrings say "three-method protocol"; the test asserts exactly three member names.
    - _Requirements: 6.3, Success Criterion 7_
  - [x] 1.2 Implement limits and safe text
    - Write `tests/unit/test_agent_limits.py` first: constants equal the requirement numbers (500, 20 480, 100, 25, 240, 4, 35 is in `mcp_server` not here); `clean_text` removes NUL, CR, LF, `\x1b`, `\x7f`, `\x85`, lone surrogates, cuts to the limit, accepts `None` and `int`; `clamp`; `cap_items` keeps all items when small, drops from the tail when over 20 480 bytes, sets `truncated` only then, measures UTF-8 bytes (a list of items with multi-byte text still fits), and returns valid JSON; an empty list fits; `RULE_APPROVAL_WINDOW == ApprovalSettings.from_env({}).rule_approval_ttl` (Needs from auto-approval 3); `watch_max_wait(env)` gives 25 for an empty env, 10 for `"10"`, 25 for `"99"` (never raised), 1 for `"0"` or `"-4"`, and 25 with one logged warning for `"abc"` (R1-M6).
    - Create `agent/limits.py` (`WAIT_CEILING`, `watch_max_wait`, `WAIT_MAX`).
    - [x] (R1-M6) The lowering-only setting `CHIMERICMESH_WATCH_MAX_WAIT`.
    - _Requirements: 1.2, 1.3, 1.4, 1.7, 4.2, 4.5_
  - [x] 1.3 Add the subprocess allow-list static test
    - Write `tests/unit/test_no_process_spawn.py` first: an AST scan of `src/chimericmesh/`; the names `subprocess`, `os.system`, `os.popen`, `os.spawn*`, `os.exec*`, `asyncio.create_subprocess_exec`, `asyncio.create_subprocess_shell` appear only in `fsperms.py` and `discovery/runner.py` (discovery A1: the narrow rule); the hub, `mcp_server.py`, the tailer, the parsers and `agent/*` have none; the test lists the allowed files explicitly so a new file fails it; a second assertion states that no file imports a module named `kiro` or builds a `kiro-cli` command line (R4.10, F1, F3).
    - If the discovery module is not merged yet, the test uses the broader `discovery/*` entry and a comment names A1 as the fallback.
    - _Requirements: 4.10_
  - [x] 1.4 Write the property test for safe text
    - Create `tests/pbt/test_agent_limits_properties.py` (arbitrary text strategies include NUL, control characters, lone surrogates, very long strings, nested item lists).
    - **Property 4: Safe text**
    - **Validates: Requirements 1.7**
  - [x] 1.5 Record the starting state of the tree (R1-L10)
    - Write `tests/unit/test_tree_baseline_helpers.py` first for a small helper `tests/unit/tree_baseline.py` (`snapshot(paths) -> dict[str, str]` of path to SHA-256 of the working-tree bytes, `diff_snapshots(a, b) -> list[str]`; missing file gives the value `absent`).
    - Create the helper, then run it once (no edit of any watched file) and write the result to `.agents/tasks/agent-loop-baseline.json`: the hashes of `examples/legacy_app/upstream.py`, `.kiro/agents/mesh-responder.json`, `.kiro/settings/mcp.json`, `src/chimericmesh/hub.py`, `tests/unit/test_kiro_assets.py`, `tests/unit/test_dependency_direction.py`, `tests/unit/test_approvals_static.py`, plus the output of `git status --short` and `git diff --stat`. `upstream.py` is modified in the working tree today (the user's fix is on disk); the snapshot records that state, whatever it is when this task runs.
    - [x] (R1-L10) Tasks 9.1, 11.2 and 12.2 compare against this file, not against `HEAD`.
    - _Requirements: 5.3, Success Criterion 1 (demo precondition)_

- [x] 2. Service events and the digest
  - [x] 2.1 Implement `service_events`
    - Write `tests/unit/test_agent_feed.py` first, with `FakeState` (a list of events with ids and services, `events_after` oldest first with a `service_id` filter): returns oldest first after `after_id`; at most 100 whatever the caller passes (the clamp is applied by the route, the function also never returns more than `limit`); default 25; `min_severity=3` keeps severity 0 to 3 and `next_after_id` is the last **scanned** id, so a second call makes progress when nothing matched; nothing scanned gives `next_after_id == after_id`; 1000 scanned events stop the scan; text fields pass through `clean_text`; a response over 20 KB is cut with `truncated` and `next_after_id` equal to the last kept id; events of other services never appear.
    - Create `tests/unit/agent_helpers.py` (`FakeState`, builders, `make_agent(mode)` with a stub until 7.1) and `agent/feed.py`.
    - _Requirements: 1.2, 1.5, 1.7_
  - [x] 2.2 Implement the digest
    - Write `tests/unit/test_agent_digest.py` first: severity counts have all eight keys and sum to `total_events`; top signatures use `diagnostics.signature`, are at most 10, ordered by count descending then `first_ts` then text; `sample` at most 200 characters from the earliest event; five equal slices with `per_minute`; events outside the window are not counted; incidents opened in the window (first seen inside it) are listed newest first, at most 20; the same events in another order give an identical result; `collect_window` finds the window start by binary search over ids (assert the number of `events_after(mid, 1)` calls is at most 20 for 100 000 events), pages the service's events, and sets `truncated` at the 20 000 cap; an empty service gives zeros, not an error; one event in the middle with a `ts` far past the window end is skipped and the later in-window events are still counted, with `truncated` false; 200 consecutive events past the end stop the scan with `truncated` true; a future-dated event is never counted (R1-M7).
    - [x] (R1-M7) The two future-timestamp tests above.
    - Create `agent/digest.py` (`Window`, `collect_window`, `build_digest`).
    - _Requirements: 1.3, 1.5, 1.7_
  - [x] 2.3 Write property tests for events and digest
    - Create `tests/pbt/test_agent_feed_digest_properties.py` (random event lists over several services, random limits including negative and huge, permutations).
    - **Property 1: `service_events` limits and order**
    - **Property 2: Digest is deterministic and consistent**
    - **Validates: Requirements 1.2, 1.3**

- [x] 3. Code search
  - [x] 3.1 Implement `search_code`
    - Write `tests/unit/test_agent_codesearch.py` first, on a temporary folder: a case-insensitive literal match returns `file` (relative, `/` separators), `line`, `text` (stripped, at most 200 characters); a regex query such as `.*` or `a(b` matches only the literal text; at most 10 hits and `truncated`; `node_modules`, `.git`, `__pycache__` skipped (the names come from `diagnostics.SKIP_DIRS`, asserted by identity); `.env`, `a/b/.env.local`, `k.pem`, `.kiro/x.py` and a file in `.git` never searched (protected patterns); `.json` and `.ini` files not searched (open item O6) and `.py`, `.php`, `.cs` searched; a file over 512 KB skipped; 2000 files visited stops with `truncated`; a fake clock past 5 s stops with `truncated`; `cancelled()` true stops; a symlink to a file outside the folder is skipped (skip the test when symlinks need a privilege, and add a second test with injected `realpath`); unreadable file skipped; order is deterministic.
    - [x] (L4) The skip set is `diagnostics.SKIP_DIRS` and the suffix set is `approvals.scope.SOURCE_SUFFIXES`; `find_code_hints` behaviour is untouched (golden test lives in `auto-approval` 1.1).
    - Create `agent/codesearch.py`. Note (2026-11-22): containment is one `realpath` per file plus a local descendant test, not `path_in_code_folder` (speed on Windows); see the implementation note in design.md, "Code search".
    - _Requirements: 1.4_
  - [x] 3.2 Write the property test for code search
    - Create `tests/pbt/test_agent_codesearch_properties.py` (generated trees through injected file reader and `realpath`, fake clock).
    - **Property 3: Code search is contained and bounded**
    - **Validates: Requirements 1.4**

- [x] 4. Verify
  - [x] 4.1 Implement `evaluate_verify`
    - Write `tests/unit/test_agent_verify.py` first, one test per sub-criterion, each starting from inputs that give `ok` and changing one thing: 3.1a status `pending`, `denied`, `done`; 3.1b user approval with `approved_by_rule` `None` passes whatever `decided_by` says; a rule approval fails for rule `revoked`, `suspended`, `expired`, past `expires_at`, unknown rule, `paused` true, rule bound to another code folder, `approved_at` 1801 s old (and 1800 s passes), and `approvals_available` false; a proposal with `decided_by` text `rule:r1` but `approved_by_rule` `None` is a user approval; 3.1c 0 files, 21 files, a 301-character entry, a non-string entry, **a relative entry** (`src/x.py`, `x.py`, `./x.py`) gives `file <n> must be an absolute path` for a user approval and for a rule approval, and a workspace-relative `src/chimericmesh/hub.py` with code folder `<root>/examples/legacy_app` gives `ok: false` (R1-H1); 3.1b an `approval` of `None` or without a numeric `approved_at` for a rule approval gives `rule approval time is unknown`, and the same for a user approval is not a failure; the rule folder differing from the code folder only by case (on a Windows-style `normcase` injected) or a trailing separator passes, a different folder fails (R1-L4, R1-L5); 3.1d a file not declared, a declared file outside scope (protected, extension), the same file as absolute and relative gives one key; 3.1e no declared files means no comparison; 3.1f a file outside the code folder fails, no code folder gives `ok` true with the note `no code folder, path scope not checked`; one reason per failed check in the order a to f; reasons never contain the file name, the action or the rule's signature (canary strings); an input of mixed garbage never raises.
    - [x] (L8) Test names carry 3.1a to 3.1f, and the file docstring maps the requirement's (a) to (f) to them in order.
    - [x] (R1-H1) The absolute-path cases above; property 5's oracle has its own absolute-path rule (task 4.2).
    - [x] (R1-L4) The missing approval time case. (R1-L5) The `samefolder` cases.
    - Create `agent/verify.py` (`VerifyInputs`, `VerifyResult`, `normalize_file`, `samefolder`, `evaluate_verify`), using `approvals.scope.check_file` and `services.paths.path_in_code_folder`.
    - _Requirements: 3.1, 3.2_
  - [x] 4.2 Write the property test for verify
    - Create `tests/pbt/test_agent_verify_properties.py` (generated `VerifyInputs` and file lists, an independently written oracle with its own absolute-path rule, relative entries included in the generated files, canary strings in file names and proposal text).
    - **Property 5: Verify is exactly the conjunction**
    - **Validates: Requirements 3.1, 3.2**
  - [x]* 4.4 Protected-name check for user approvals (open item O13 accepted 2026-11-18 as the design states it; buildable now, the R3.1(e) wording change waits for the next requirements revision)
    - Write the tests first in `tests/unit/test_agent_verify.py`: a user approval without declared files and `.env`, `a/.env.local`, `k.pem`, `.git/x`, `.kiro/x.py` inside the code folder gives `ok: false` with `file <n> is a protected file`; an ordinary source file still gives `ok: true`; a user approval with declared files behaves as before (the declared-list and scope checks).
    - Built 2026-11-22 (chunk A). Edit `agent/verify.py` (3.1e runs the `PROTECTED_PATTERNS` part only, never the extension rule). Until this is built and accepted, 11.1 states in the README that the hook does not protect these names and the profile `fs_write` denies do.
    - [x] (R1-L6) Decision recorded as open item O13 (accepted 2026-11-18). The residual risk stays documented in the README line of 11.1 until this task is built.
    - _Requirements: 3.1 (e) (needs a requirement revision)_
  - [x]* 4.3 Write the optional monotonicity property
    - Add to `tests/pbt/test_agent_verify_properties.py`.
    - **Property 6: Verify is monotone in the files**
    - **Validates: Requirements 3.1 (c), (d)**

- [x] 5. Watch queue
  - [x] 5.1 Implement `evaluate_work`
    - Write `tests/unit/test_agent_watch_eval.py` first, one test per sub-criterion: 4.3a.1 resolved incident excluded; 4.3a.2 incident of a deleted service excluded, `unassigned` incident excluded unless `service_id="unassigned"`; 4.3a.3 `pending` and `approved` proposals exclude the incident, `done` and `denied` do not; 4.3a.4 `exclude` contains the incident id; 4.3a.5 a `denied` proposal with `decision_origin="user"` and `incident_opening == opening` hides it, the same with `decision_origin="hub"` does not, a user denial of an earlier opening does not, and a nick `expired-bob` in `decided_by` stays a user denial because only `decision_origin` is read; 4.3a.6 the service filter; 4.3b approved proposals not in `exclude`, at most 5, with their `approval` object, `unassigned` rules as for incidents, `done` and `pending` never listed; ordering `(severity, -count, id)`; at most 5 incidents; `sample` at most 200 characters, other strings at most 500, no control characters, no event bodies; 10 000 proposals and 5000 incidents evaluate in under 50 ms.
    - [x] (L8) Test names carry 4.3a.1 to 4.3a.6 and 4.3b.
    - Create `agent/watch.py` with `WorkResult` and `evaluate_work` (the queue class comes in 5.2).
    - _Requirements: 4.3, 4.4_
  - [x] 5.2 Implement `WatchQueue`
    - Write `tests/unit/test_agent_watch_queue.py` first (`asyncio.run` inside plain tests, small `tick`, `wait_min`, `wait_max` through the constructor): work present returns at once; an incident opened 0.2 s after the call is returned within `tick + 0.05 s` and, in the integration form, within 1.5 s (Success Criterion 4); no work returns `{status: "timeout", incidents: [], approved_proposals: []}` at the timeout; `kick()` wakes a waiter at once; five concurrent waiters give four held and one `TooManyWaiters(retry_after=5)`; after a return, a cancellation and a `close()` the `waiting` counter is 0; an injected `is_disconnected` that turns true frees the slot within `tick + 0.05 s` and returns `timeout`, so five sequential "abandoned" calls never reach the fifth-waiter 429 (R1-M1); `close()` ends every held waiter with `timeout` immediately and a later call returns `timeout`; 4 held waiters do not delay 200 `hub.ingest` calls (under 2 s); the module has no `threading` import and no `time.sleep`.
    - Complete `agent/watch.py` with `WatchQueue` (event plus 0.5 s tick, `finally` frees the slot).
    - _Requirements: 4.1, 4.2, 4.5, 4.6, 4.7_
  - [x] 5.3 Write the property test for the work set
    - Create `tests/pbt/test_agent_watch_properties.py` (generated incidents, proposals with statuses, `decision_origin`, `incident_opening`, services, exclude sets and filters; an independently written oracle).
    - **Property 7: Work set is exactly the specified set**
    - **Validates: Requirements 4.3, 4.4**
  - [x]* 5.4 Write the optional watch properties
    - Add to `tests/pbt/test_agent_watch_properties.py` (fake loop times for the bounds).
    - **Property 8: Reopen returns a denied incident**
    - **Property 9: Long poll bounds**
    - **Validates: Requirements 4.2, 4.3 (a), 4.5, 4.6**

- [x] 6. Permissions snippet
  - [x] 6.1 Implement `build_snippet`
    - Write `tests/unit/test_agent_snippet.py` first: a folder inside the workspace gives an `fs_write` allow with the relative glob `examples/legacy_app/**` (forward slashes, no drive letter, quoted with `json.dumps`) and an `fs_write` deny line for every `PROTECTED_PATTERNS` entry; a folder outside the workspace, a folder equal to the workspace, an unknown workspace and a folder with `*`, `?`, `[`, `{` in its relative path give no allow line and a comment saying the fix runs but Kiro prompts for each write; the first two lines name `Mode: AUTONOMOUS` and say prompt mode is the default; the text says plainly that the user-scoped `~/.kiro/settings/permissions.yaml` applies to every session and agent, and that the workspace-scoped file (hash to be looked up) is preferred; the trade-off sentence (in-folder edits no longer prompt, including for `pending` proposals; the gate rests on the prompt text, `verify_proposal` and the audit trail) is present; a commented shell example for `python -m pytest -q` and for a valid custom command; `validate_test_command` rejects `*`, quotes, `;`, `&`, `|`, `$`, backticks, `-c`, `curl`, `wget`, `nc`, `powershell`; the text never contains `allow-all`, `trust-all-tools`, a shell match of `*`; a commented allow for the single file `.chimericmesh-active-proposal` (O12); output has no control character other than newline; the text names the workspace root it used (`# Workspace root used: ...`); it carries the unverified-schema comment and the warning that relative deny globs in the user-scoped file may apply in every workspace (R1-L8); `find_workspace_root` (injected `exists`): a valid `CHIMERICMESH_WORKSPACE_ROOT` wins, an invalid one is ignored, cwd `<repo>/src` gives `<repo>` when `<repo>/.kiro` exists, no `.kiro` anywhere gives cwd, and with root `<repo>` the demo folder `<repo>/examples/legacy_app` produces the allow line `examples/legacy_app/**` (R1-M3).
    - [x] (R1-M3) The walk-up test with cwd `src`.
    - [x] (R1-L8) The caveat comment and its test.
    - Create `agent/snippet.py` (`build_snippet`, `validate_test_command`, `find_workspace_root`).
    - _Requirements: 5.4, 5.5, 5.6, 5.7, 5.8_
  - [x] 6.2 Write the property test for the snippet
    - Create `tests/pbt/test_agent_snippet_properties.py` (arbitrary folder and workspace strings including spaces, quotes, `#`, glob characters, Windows paths, long text).
    - **Property 10: Snippet safety**
    - **Validates: Requirements 5.4, 5.5, 5.8**

- [x] 7. Facade, REST routes and wiring
  - [x] 7.1 Implement `AgentService`
    - Write `tests/unit/test_agent_service.py` first, parametrized over `mode in ("bare", "store")` through `make_agent(mode)` (replace the stub in `agent_helpers.py`): `service_events`, `digest` and `code_search` for a registered service work, `unassigned` and unknown ids raise `UnknownService`; `code_search` on a service without a code folder raises `NoCodeFolder`; two running searches and a third gives `SearchBusy` (a gated fake search); the search thread is told to stop when the awaiting task is cancelled; `verify` builds its inputs for a user-approved proposal, for a rule-approved one (real `ApprovalManager`, rule revoked afterwards gives `ok` false with a revoked reason), for a proposal of an unassigned incident (note, `ok` true), and for an unknown id (`UnknownProposal`); `approvals=None` makes every rule approval fail; `code_folder_for_incident` returns the live folder or `None` (B6); `permissions_snippet` uses `workspace_root`; `aclose` closes the queue.
    - [ ] (M2) The same test module passes for both modes with no `hub` import in `agent/*`.
    - [ ] (R1-L1) `AgentService(ports)` takes no limits argument; a skeleton test asserts `AgentLimits` does not exist.
    - [ ] (R1-L3) Add to the test list above: with two gated searches running, cancel one awaiting task while its thread is still blocked: a third call still gives `SearchBusy` until the thread is released, then succeeds (the slot is freed by thread completion, not by the cancelled task).
    - Create `agent/service.py` (a plain `_running` counter released by the worker future's done callback).
    - _Requirements: 1.1, 1.4, 1.5, 3.1_
  - [x] 7.2 Implement the REST routes
    - Write `tests/unit/test_agent_routes.py` first (`httpx.ASGITransport` on a small FastAPI app that mounts `build_router` over a real `AgentService`): every route of the design table with its success shape; `limit` 0, -5 and 10 000 give 1, 1 and 100 (the response shows the effective value); `after_id` -3 gives 0; `min_severity` 99 gives 7; `minutes` 0 and 10 000 give 1 and 240; `timeout_seconds` 0 and 99 give 1 and `WAIT_MAX` (25, or the lowered value of `CHIMERICMESH_WATCH_MAX_WAIT`; use a constructor-injected short `wait_max`); a real client disconnect over `httpx.ASGITransport` (the request is abandoned while a waiter is held) frees the slot within one tick, checked through `agent.watch.waiting` (R1-M1); non-integers give 422; `query` empty, 101 characters and with a control character give 422, a missing code folder gives 422 `service has no code folder`; `unassigned` and unknown ids give 404 on every per-service route; `exclude_ids` with 201 entries or an entry of 41 characters or with a space gives 422; a fifth waiter gives 429 with `Retry-After: 5`; `POST /verify` with 201 files gives 422, with 21 files gives 200 `ok: false`; the snippet route returns `text/plain`; log text in every response is capped at 500 characters with no control character; a static check lists every route handler in `agent/routes.py` as `async def` and finds no `require_admin` and no non-`GET`/`POST` method.
    - Create `agent/routes.py` (`build_router`, pydantic models with `extra="forbid"`, error mapping: `UnknownService` and `UnknownProposal` 404, `NoCodeFolder` and `InvalidQuery` 422, `TooManyWaiters` and `SearchBusy` 429).
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.7, 3.1, 4.2, 4.5, 5.4_
  - [x] 7.3 Wire the module into the app
    - Write `tests/unit/test_agent_wiring.py` first: `create_app` (both a bare hub and a store-backed hub) mounts the six routes and sets `app.state.agent`; `GET /api/incidents/{id}/code` searches the incident service's code folder when it has one and falls back to `CHIMERICMESH_CODE_ROOT` when it has none or the incident has no service (B6) and returns the same list-of-dicts shape as today; the lifespan teardown closes the queue first, so a held `wait_for_work` returns `timeout` and the app stops without waiting for it (R4.6); **a real `uvicorn.Server` on an ephemeral port with the app from `create_app`, one held `GET /api/watch/next` (25 s), then an exit request through the server's signal path, stops in under 3 s and the held client receives `timeout` or a closed connection** (R1-M2; the lifespan-only test cannot catch the uvicorn wait); `__main__.py` passes `timeout_graceful_shutdown=3` (a test inspects the `uvicorn.Config` built by the new `build_server(app, host, port)` helper); with `CHIMERICMESH_WORKSPACE_ROOT` set the snippet uses it, with an invalid value one warning is logged and no allow line is emitted; with cwd `<repo>/src` and no variable the workspace root is `<repo>` (R1-M3); existing route outputs are unchanged (golden comparison of `/api/events`, `/api/incidents`, `/api/proposals` for a fixed hub).
    - Edit `src/chimericmesh/api.py` (build `AgentService`, include the router, change `incident_code`, lifespan step) and `src/chimericmesh/__main__.py` (`build_server`, graceful timeout, exit handler that calls `agent.aclose()` first). Wiring only; `hub.py` is not edited.
    - [ ] (R1-M2) The real-server stop test. (R1-M3) The workspace-root default.
    - _Requirements: 1.6, 4.6_
  - [x] 7.4 Checkpoint: run `python -m pytest -q`; the new tests and the whole existing suite pass.

- [x] 8. MCP tools
  - [x] 8.1 Add the wrappers and the long-call timeout
    - Write `tests/unit/test_mcp_agent_tools.py` first (ASGI-backed client through the existing `_client_factory` monkeypatch, plus an `httpx.MockTransport` client for request inspection): each of the seven new tools calls exactly its route with unchanged parameters and returns the body (`service_events`, `explain_log_window`, `find_code_for_service`, `verify_proposal`, `wait_for_work`, `list_services`, `get_service`); parameters that are `None` are not sent; no tool clamps (a `limit` of 9999 reaches the route as 9999); `wait_for_work` sends `timeout=35.0` per request (`request.extensions["timeout"]["read"] == 35.0`) and every other tool keeps the factory default; `exclude_ids` is sent as repeated query parameters; `propose_remediation("i", "a", "d")` sends exactly `{incident_id, action, details}` as before, and with `files`, `estimated_lines`, `verification`, `kind` sends them unchanged; `complete_proposal` likewise with `files_changed`, `lines_changed`, `verification_result`; a 409 `ActiveProposalExists` returns `{error: 409, detail, proposal_id}` and a 422 returns `field`; a non-JSON error body returns its text; the `instructions` string still contains `propose_remediation` and `approved` and mentions `wait_for_work`; the existing MCP tests are unchanged.
    - Edit `src/chimericmesh/mcp_server.py` (tools, `_call(timeout=...)`, instructions). Keep the HTTP client timeout at 10 s for the factory.
    - _Requirements: 1.1, 1.6, 2.1, 2.2, 4.1, 4.5_
  - [x] 8.2 Add the route-table and exact-tool-set tests
    - Create `tests/unit/route_owners.py` (the owner table: the six agent routes `agent-loop`, `GET /api/services` and `/api/services/{id}` `services`, the six `/api/discovery/*` routes `discovery` with no tool (discovery A2), the proposal routes `auto-approval`, the existing routes `baseline`; the dashboard module adds its rows) and write `tests/unit/test_agent_route_table.py` first: the exact tool set is the ten baseline tools plus `list_services`, `get_service`, `service_events`, `find_code_for_service`, `verify_proposal`, `wait_for_work`, `explain_log_window` (17 names); no tool name contains `rule`, `approval`, `service` mutators, `token`, `discovery`, `audit`, `withdraw`, `pause`, `resume` (the allowed names are listed); an AST scan of `mcp_server.py` maps each tool to exactly one `_call` route literal and the app's route list contains that method and path; every route of the app has an owner in the table (a new route fails the test until it is listed); no route behind a tool has a dependency named `require_admin`; no tool takes a parameter named `token`, `admin_token` or `authorization`.
    - Edit `tests/unit/test_approvals_static.py` (created by `auto-approval`): add this module's endpoint literals to its allow-list (design, Needs from auto-approval 2); its other assertions stay.
    - _Requirements: 1.1, 1.8, 2.4, Success Criterion 6_

- [ ] 9. Kiro assets
  - [x] 9.1 Add the `mesh-watcher` profile
    - **2026-11-23 decision (user, chunk B3; recorded in blockers.md and design.md):** the `allow` rule lists the 13 read-only tools only; `announce_to_irc`, `propose_remediation`, `complete_proposal` and `resolve_incident` are an `mcp` `ask` rule. This replaces "the only `allow` rules ... equal the design list" below; the tests assert the 13 and the 4.
    - Append to `tests/unit/test_kiro_assets.py` first (new functions only, no existing test edited) and create `tests/unit/test_watcher_prompt.py`: the profile is valid JSON with fields inside `AGENT_FIELDS`, name `mesh-watcher`; `tools` lists `read`, `write`, `shell` and `@chimericmesh/<tool>` for exactly the 17 tools; `mcpServers.chimericmesh` equals the workspace config plus `requestTimeout >= 60000`; the only `allow` rules are `mcp` rules with explicit names (no `*`) and they equal the design list; **no `allow` and no `ask` rule for `fs_write` or `shell`; every `fs_write` and `fs_read` rule is `deny`** (R5.1); the `fs_write` denies equal `**/.env`, `**/.env.*`, `**/*.pem`, `**/*.key`, `**/id_rsa*`, `**/admin.token`, `**/chimericmesh.db*`, `~/.chimericmesh/**` and the `fs_read` denies equal `**/admin.token`, `**/chimericmesh.db*`, `~/.chimericmesh/**` (R5.2); every tool named exists in the MCP server; resources exist; `mesh-responder.json` and `.kiro/settings/mcp.json` have the SHA-256 recorded in `.agents/tasks/agent-loop-baseline.json` by task 1.5 (R5.3; a `git show HEAD:` comparison would fail on any uncommitted change by another module); prompt-text tests (R2.3, R3.3, R3.5, R3.6, R4.8, R4.9): the watch loop order, `wait_for_work`, `exclude_ids`, `files`, `estimated_lines`, `verification`, `status`, `verify_proposal` before the first edit, before an edit of a file not listed before, and before `complete_proposal`, "change nothing unless `ok` is true", "stop, make no change, report the reasons, say exactly which files changed", a different action needs a new proposal and V1 is code only, only `status` and `approval` count, `get_proposal` immediately before acting with `status`, `action` and `incident_id`, three consecutive tool errors, 401 403 or 429 twice, "keep notes short", and **no** `p1-k7f`-style id text anywhere in the profile or the skills; plus the round-1 sentences: `files` for `verify_proposal` are absolute paths built from `get_service(service_id).code_folder` (R1-H1); when the fix is verified wrongly (tests fail or the rate has not fallen) do not call `resolve_incident`, add the incident id to `exclude_ids`, tell the user and continue the loop (R1-M4); finish within 30 minutes of the approval or propose again (R1-L7); after a restart of the loop call `list_proposals` for an incident before proposing again (O10).
    - [x] (R1-H1) (R1-M4) (R1-L7) The three prompt-text tests above (each also asserted for `remediation-loop` and `watch-mode` in 9.2).
    - Create `.kiro/agents/mesh-watcher.json` with the prompt contract of the design.
    - [x] (L9) The profile description or the README names the `~/.chimericmesh/**` globs as "check live" (asserted in 11.1).
    - _Requirements: 2.3, 3.3, 3.5, 3.6, 4.8, 4.9, 5.1, 5.2, 5.3_
  - [ ] 9.2 Add and update the power skills
    - Write the failing tests first by editing `tests/unit/test_kiro_assets.py::test_power_docs_name_only_real_tools_and_cover_all` (the one allowed change): keep both original assertions (every tool named in the skills; every tool listed as `` - `name`: `` in `getting-started`) and replace the closing loop with an explicit allow-list, `SKILL_WORDS`, of the parameter and field names the skills use (`exclude_ids`, `timeout_seconds`, `service_id`, `incident_id`, `proposal_id`, `after_id`, `min_severity`, `estimated_lines`, `files_changed`, `lines_changed`, `verification_result`, `next_after_id`, `approved_by`, `approved_proposals`, `total_events`, and every other snake_case word the three skills use, computed once and written out), kept next to `_tool_names`; a backticked word that is neither a tool nor in `SKILL_WORDS` still fails. Add a test that the skills mention all 17 tools and that `getting-started` has a `` - `name`: `` line for each (L2). Add static tests for the skill text: `watch-mode` exists with frontmatter name equal to its folder and a description; it and `remediation-loop` carry the re-verify rule (call `get_proposal` immediately before acting and compare status, action and incident), the `verify_proposal` rule, the declared-files rule, the stop rules, the D4 code-only rule and the untrusted-input rule, the absolute-path rule for `files` (R1-H1), the "do not resolve when the fix is not confirmed, add to `exclude_ids`, tell the user" rule (R1-M4) and the 30-minute rule (R1-L7); the existing steps and rules of `remediation-loop` and `getting-started` are still present (every original line is a substring of the new file, ignoring the lines the update replaced, listed in the test); `getting-started` explains services, structured proposals and auto-approval in one short section.
    - Create `powers/chimericmesh-remediation/skills/watch-mode/SKILL.md`; edit `skills/remediation-loop/SKILL.md` and `skills/getting-started/SKILL.md` (additive).
    - [ ] (L2) The catalogue lines for the seven new tools and the allow-list.
    - _Requirements: 3.3, 3.4, 3.5, 3.6, 4.8, 6.1, 6.2, 6.3_

  - [ ] 9.3 Live check of the 25 s wait (user action; gate before tasks 10 to 12; R1-M6)
    - Not optional. It is the earliest point where `wait_for_work` (8.1) and the profile (9.1) both exist. In the Kiro IDE select `mesh-watcher`, trust the workspace, start the hub, and have the agent call `wait_for_work` once with the default 25 s on a quiet service. Record in this file whether the call returned `timeout` after about 25 s, or an IDE timeout error, and which file the IDE read the `requestTimeout` from (the profile, or `.kiro/settings/mcp.json`).
    - Also record here (O16, accepted 2026-11-18; the user checks both Kiro facts here): the `permissions.yaml` schema and the shell `match` semantics from https://kiro.dev/docs/permissions/, whether the snippet text must change, and whether the IDE honours the profile's `requestTimeout`.
    - [ ] (O16) User check: the `permissions.yaml` schema and shell `match` semantics, result written under this task.
    - [ ] (O16) User check: whether the IDE honours the profile's `requestTimeout`, result written under this task.
    - If the call fails: set `CHIMERICMESH_WATCH_MAX_WAIT` (for example `10`) in the `env` of the profile's `mcpServers.chimericmesh` entry and in the hub's environment, and repeat. If the IDE reads only `.kiro/settings/mcp.json`, say so in the README (11.1); that file stays unchanged (R5.3) and the user adds `requestTimeout` there.
    - [ ] (R1-M6) Result written under this task.
    - _Requirements: 4.5, F2 (requirements Open Q1)_

- [ ] 10. Optional enforcement hook (extra layer; not in the 1.5 day estimate; build if there is slack)
  - [ ] 10.1 Implement the hook script
    - Write `tests/unit/test_mesh_verify_write_hook.py` first (load the script with `importlib` from its path, as `test_guard_hook.py` does; a fake hub through the injected `post`): a valid `p4` state file and a reply `ok: true` exits 0; `ok: false` exits 2 with the first reason on stderr; a missing, empty or malformed state file (`p0`, `p-1`, `../x`, 5 000 characters) exits 2; the hub unreachable (`post` raises) exits 2; a reply that is not JSON exits 2; a reply with the note `no code folder, path scope not checked` exits 2; `CHIMERICMESH_URL` pointing at `http://example.com` or `http://127.0.0.1.evil.com` exits 2 without calling `post`; a write whose target is `.chimericmesh-active-proposal` exits 0; a workspace-relative target `src/chimericmesh/hub.py` with `cwd` `<root>` is sent to `post` as the absolute `<root>/src/chimericmesh/hub.py` (so with a code folder `<root>/examples/legacy_app` the fake hub, using the real `evaluate_verify`, answers `ok: false`) (R1-H1); stdin that is not JSON or has no path exits 0; the request body is `{"files": [...]}` and no header carries a token; the script reads no token file (static check: no `admin.token`, no `open(` except the state file).
    - Create `.kiro/hooks-optional/mesh_verify_write.py` (`decide`, `main`; stdlib only; 5 s timeout).
    - _Requirements: 5.9_
  - [ ] 10.2 Add the hook definition and install notes
    - Write the tests first, in `tests/unit/test_mesh_verify_write_hook.py`: `.kiro/hooks-optional/mesh-verify-write.json` has version `v1`, trigger `PreToolUse`, matcher exactly `^(fs_write|fs_append|str_replace)$` and equal to the matcher of `.kiro/hooks/guard-innerhtml.json` (R1-M5), a `command` action running the script, and no `timeout` key inside `action` (the hook-timeout rule of the existing test); the two files are **not** present in `.kiro/hooks/` (opt-in, O5); the README section (11.1) names the copy step, the effect "every write needs an active proposal while installed" and the removal step.
    - Create `.kiro/hooks-optional/mesh-verify-write.json`.
    - _Requirements: 5.9_
  - [ ]* 10.3 Write the optional hook property test
    - Create `tests/pbt/test_agent_hook_properties.py`.
    - **Property 11: Hook decision**
    - **Validates: Requirements 5.9**

- [ ] 11. Documentation and the carried changes
  - [ ] 11.1 Write the README section
    - Write `tests/unit/test_agent_readme.py` first: the README has an "Agent watch mode" section that names `mesh-watcher`, `wait_for_work`, the two modes **prompt mode (default)** and **autonomous mode**, the snippet route, and states: the workspace must be trusted (F7); permission rules are static, so a changed code folder makes Kiro prompt again (F6); the user-scoped file applies to every session and agent of this user (F5) and the workspace-scoped file is preferred; hands-off write allows use workspace-relative globs only (F11); the `PermissionRequest` hook is not part of V1 (F8); the snippet is a standing allow that also applies while a proposal is `pending` (R5.7); "the agent cannot approve its own proposal or widen rules" is a design goal with stated limits, with the token-file and shell-command limit (R5.11(a)); **the one allowed test command in autonomous mode runs code the agent has just written with the user's privileges and that code could read the token file or call the hub; the user accepted this for hands-off mode only; prompt mode stays the default** (R5.11(b), README item); pause, revoke and withdraw need no token and can only reduce the agent's permissions; the `~/.chimericmesh/**` deny globs are "check live" and what remains if they do not match (L9); the hook is write-only; the demo precondition (L6): restore the defect in `examples/legacy_app/upstream.py` yourself before the demo, for example with `git checkout -- examples/legacy_app/upstream.py` (the doc says it is the user's command; no task runs it); the long-wait setting (25 s, 35 s client timeout, `requestTimeout` 120000); ids are `p1`, `p2`.
    - Edit `README.md` (one new section; no change to other sections).
    - [ ] (L6) The demo precondition paragraph.
    - [ ] (L9) The "check live" paragraph.
    - [ ] The README item of the Notes for design: the test-command risk paragraph above.
    - [ ] (R1-L9) One sentence: in bare mode the proposals list keeps 200, so a user-denied proposal that falls off it makes its incident work again; store mode keeps all (asserted by the test).
    - [ ] (R1-L6) One sentence, kept until task 4.4\* is built: the hook and `verify_proposal` do not protect `.env`-type names inside the code folder for a user approval without declared files; the profile `fs_write` denies do.
    - [ ] (R1-M6) The README says which file the IDE reads for `requestTimeout` (result of 9.3), and the `CHIMERICMESH_WATCH_MAX_WAIT` fallback.
    - [ ] (R1-M3) The README says the workspace root is found by walking up to `.kiro/` and can be set with `CHIMERICMESH_WORKSPACE_ROOT`.
    - [ ] (R1-H1) The README says `files` are absolute paths.
    - _Requirements: 5.5, 5.7, 5.10, 5.11, 4.5_
  - [ ] 11.2 Carry the long-outage overflow test from the branch
    - Read the change with `git show fix/proposal-ids-and-recheck:examples/legacy_app/tests/test_resilience.py` and `git diff HEAD...fix/proposal-ids-and-recheck -- examples/legacy_app/tests` only (no checkout, no merge, no cherry-pick). Append `test_long_outage_keeps_running_with_bounded_positive_delays` to `examples/legacy_app/tests/test_resilience.py` (add the blank line before it that the branch lacks).
    - Run `python -m pytest -q` inside `examples/legacy_app` once and record the result **for the tree as it is** (today `upstream.py` carries the user's uncommitted fix, so the new test may pass). Then record what it would do with the defect present, without editing `upstream.py` or running any git command that changes the tree: copy `git show HEAD:examples/legacy_app/upstream.py` to a temporary directory together with the tests and run the suite there. Write both results as a comment line in the task notes (R1-L10). The root `python -m pytest -q` does not collect the new test. Do not change `examples/legacy_app/upstream.py`; do not try to make the new test pass or fail.
    - [ ] (L6) State in the test file's docstring comment line that it is a demo test that fails by design until the upstream client has a bounded backoff.
    - _Requirements: Success Criterion 1 (demo precondition)_

- [ ] 12. Integration and final checkpoint
  - [ ] 12.1 Integration through the app, in both modes
    - Write `tests/unit/test_agent_modes.py` first (`httpx.ASGITransport`, fake clock through `Hub(clock=...)`, parametrized over `("bare", "store")`; the store mode uses a temporary database) covering Success Criteria 1, 3, 4 and 5 without Kiro: register a service with a code folder, send the demo storm, `wait_for_work` returns the incident within 1.5 s of it opening and `timeout` otherwise; `service_events`, `explain_log_window`, `find_code_for_service` give sane answers for the service; `propose_remediation` with `files`, `estimated_lines`, `verification` returns the final status; a user `!approve` makes `wait_for_work` list the approved proposal and `verify_proposal` return `ok` true for the declared file; with an active rule the proposal is `approved` by rule at creation and `verify_proposal` is `ok`; revoking the rule makes the next `verify_proposal` return `ok` false and the proposal leaves the work list (Success Criterion 3 in its automated form); a user denial hides the incident until it reopens (`CHIMERICMESH_REOPEN_AFTER_SECONDS` short, fake clock); `complete_proposal` with the completion fields then `resolve_incident` close it; `unassigned` events appear only through `recent_events` and `list_incidents`; the route-table test and the dependency test pass (Success Criteria 6 and 7).
    - [ ] (M2) The module runs against a plain `Hub()` with `create_app(hub)` and against the store-backed hub with the same assertions.
    - _Requirements: all automated ones; Success Criteria 3 to 7_
  - [ ] 12.2 Final checkpoint
    - Run `python -m pytest -q`. The full existing suite and all new tests must pass. Compare the working tree with `.agents/tasks/agent-loop-baseline.json` (task 1.5) using `tests/unit/tree_baseline.py`: `examples/legacy_app/upstream.py`, `.kiro/agents/mesh-responder.json`, `.kiro/settings/mcp.json` and `src/chimericmesh/hub.py` have unchanged hashes (the user's own pre-existing `upstream.py` change is part of the baseline, so it does not show up as this module's change; R1-L10). Confirm by `git diff --stat tests/` that the only existing test files changed are `tests/unit/test_kiro_assets.py` (one test edited, others appended) and `tests/unit/test_dependency_direction.py` (rows added), and that `tests/unit/test_approvals_static.py` only gained allow-list entries; confirm by `git diff --stat src/ .kiro/ powers/ examples/` against the snapshot of 1.5 that this module changed only the files it lists (the only edits in `src/` outside `agent/` are `api.py`, `mcp_server.py` and `__main__.py`).
    - Walk Success Criteria 1 to 7 against the tests that cover them and note any gap in this file.
    - Manual checks for the user (not agent work): (1) restore the defect in `examples/legacy_app/upstream.py` yourself (L6); (2) the 25 s `wait_for_work` call was already checked at task 9.3 (R1-M6); re-run it once if the profile changed since; (3) confirm in the IDE that `~/.chimericmesh/**` and the profile deny globs match (L9, F5) and that a write outside the pasted folder prompts; (4) if the hook is installed, confirm that the regex matcher fires for `fs_write`, `fs_append` and `str_replace`, the fields of its stdin and that its `cwd` is the workspace root (F8b, R1-M5, R1-H1); also confirm the `permissions.yaml` schema and shell `match` semantics recorded at 9.3 (R1-L8); (5) Success Criterion 1: storm, proposal within 60 s without a prompt from you, `!approve` (token in the dashboard), verified edit, tests, complete, resolve; (6) Success Criterion 2 with a rule and the snippet pasted: an in-folder edit does not prompt, an outside edit and an unlisted shell command do; (7) Success Criterion 3: revoke while working, the agent stops; (8) README review.
    - _Requirements: all; Success Criteria 1 to 7_

## Requirement coverage

Every requirement of `requirements.md` appears in at least one non-optional task.

| Requirement | Tasks | Requirement | Tasks |
|---|---|---|---|
| 1.1 | 7.1, 7.2, 8.1, 8.2 | 4.1 | 5.2, 8.1 |
| 1.2 | 1.2, 1.4, 2.1, 2.3, 7.2 | 4.2 | 1.2, 5.2, 7.2 |
| 1.3 | 1.2, 2.2, 2.3, 7.2 | 4.3 | 5.1, 5.3 |
| 1.4 | 3.1, 3.2, 7.1, 7.2 | 4.4 | 5.1, 5.3 |
| 1.5 | 2.1, 2.2, 7.1, 7.2, 12.1 | 4.5 | 1.2, 5.2, 7.2, 8.1, 11.1 |
| 1.6 | 7.3, 8.1 | 4.6 | 5.2, 7.3 |
| 1.7 | 1.2, 1.4, 2.1, 2.2, 7.2 | 4.7 | 5.2 |
| 1.8 | 8.2 | 4.8 | 9.1, 9.2 |
| 2.1, 2.2 | 8.1, 12.1 | 4.9 | 9.1, 9.2 |
| 2.3 | 9.1 | 4.10 | 1.3 |
| 2.4 | 8.2 | 5.1, 5.2 | 9.1 |
| 3.1 | 4.1, 4.2, 4.3, 7.1, 7.2 | 5.3 | 9.1, 9.2 |
| 3.2 | 4.1, 4.2 | 5.4, 5.5 | 6.1, 6.2, 7.2 |
| 3.3 | 9.1, 9.2 | 5.6 | 12.2 (manual) |
| 3.4 | 9.2, 12.2 (manual) | 5.7, 5.8 | 6.1, 6.2, 11.1 |
| 3.5, 3.6 | 9.1, 9.2 | 5.9 | 10.1, 10.2, 10.3 |
| 6.1 | 9.2 | 5.10, 5.11 | 11.1 |
| 6.2 | 9.2 | 6.3 | 1.1, 9.1 |

Success Criteria: 1 (11.1, 11.2, 12.2 manual), 2 (6.1, 12.2 manual), 3 (12.1, 12.2 manual), 4 (4.2, 5.2, 5.3, 2.3, 9.1, 12.1), 5 (7.4, 12.2), 6 (8.2, 12.1), 7 (1.1, 12.1).

## Effort estimate

Measured velocity: the store runs about 5 to 11 minutes per top-level task and about 2.5 minutes per sub-task including tests. This plan has 12 top-level tasks and 36 sub-tasks (3 optional property tasks, optional 4.4\*, and 9.3, a 10 to 15 minute user check added by design review round 1; task 10, the optional hook, is not in the map's 1.5 day figure).

| Part | Agent build time |
|---|---|
| 12 top-level tasks (wiring, checkpoints, test-suite runs) | 60 to 130 min |
| 30 required sub-tasks at about 2.5 min | about 75 min |
| 3 optional property tasks at about 3 min | about 10 min (skippable) |
| Fix-ups after merging with earlier modules' edits of `api.py` and `mcp_server.py` and the first full-suite run | 20 to 40 min |
| **Total** | **about 2.5 to 4 hours; plan 3 hours** (the hook is about 15 min of it) |

Human-in-the-loop work, separate from build time:

| Item | Time |
|---|---|
| Review and approve `design.md` and this file, answer open items O1 to O16 (done 2026-11-18: all recommended answers accepted) | 25 to 35 min |
| Restore the demo defect in `examples/legacy_app/upstream.py` (L6) | 5 min |
| Live Kiro checks: 25 s call (F2), IDE permissions and the `~/` globs (L9, F5), hook input and `write` matcher (F8b) | 45 min |
| Success Criteria 1, 2 and 3 live, one rehearsal each | 55 min |
| README review | 10 min |
| Full storm to auto-fix rehearsal with real clocks | 30 min |
| Merge approval after `auto-approval` is merged | 10 min |
| **Total human time** | **about 3 to 3.5 hours** |
