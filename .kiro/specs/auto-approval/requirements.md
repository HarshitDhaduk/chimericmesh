# Requirements: auto-approval (V1)

Capability map: [docs/v1-capability-map.md](../../../docs/v1-capability-map.md) (module `auto-approval`, depends on: `store`, `services`, plus shared `core` types; effort 1.75 days). Owns the hub and REST side of structured proposals (section 9), so it does not depend on `agent-loop`. It subscribes to service changes through `services` R10; `services` does not import it.

## Baseline

[legacy-mesh](../legacy-mesh/requirements.md) R6.2 and R6.3 (`!approve`, `!deny`, operators), R7 (human-approved remediation; the mesh never executes). This spec extends R7 with structured proposal fields and one transition (the table is in `store` R4.3, B10) and adds standing rules (B3, B7, B14, B17).

## Introduction

Today every fix waits for a human `!approve`. For a developer working alone that gets in the way when the same low-risk bug returns. Decision D3: the approval step belongs to the user, and the user can create standing rules so that the hub approves a similar proposal without interrupting them. The hub, not the agent, decides whether a proposal is auto-approved. The agent has no tool to create, edit or widen a rule. Every auto-approval is recorded in the audit log and announced.

## Objective

Let the user say "fixes like this one, in this service, may go ahead without asking me", bounded by severity, time, rate and the files touched, and make that permission visible, auditable and instantly revocable.

Threat model:
- The agent is useful but not trusted with its own permissions. It could be misled by prompt injection in log text. Rules are created, widened or re-enabled only with the admin token, which no MCP tool carries (`services` R6) and which is stored outside the workspace (`store` R1.1). The IRC route is closed by `services` R9 (reserved nicks, operators fail closed, `PASS` = token). **Honest limit:** the agent runs as the same OS user, so it can read the token file and call the REST API from a shell. The token stops other local processes and web pages. What stops the agent is that no MCP tool exposes rules and that Kiro prompts before shell commands and writes outside the allowed scope (`agent-loop` R5). In autonomous mode a prompt-injected agent that has an allowed shell command could create a rule. This residual risk is stated in the map and not claimed away.
- D3 (user decision, 2026-11-18): create, edit or widen, re-enable and resume need the user (admin token, or an IRC operator with `PASS`). Pause (global and per rule), revoke and withdraw need no token and no `PASS` on any channel, because they only reduce what the agent may do. They always take effect, are rate-limited by one constant (10 per minute per caller key; the excess is coalesced into one audit row but still executed), audited and announced (`services` R6.5). Because they are token-free REST routes, the agent could call them from a shell. That can only reduce its own permissions.
- The hub cannot see what the agent actually edits. It enforces what the proposal declares, and it checks the agent's completion report. Real prevention of writes outside the code folder is the job of Kiro's path-scoped permissions (see `agent-loop`). This module detects violations and suspends the rule.
- Log text, proposal text and file names are untrusted.

## Requirements

### 1. Rule model
**User story:** As a developer, I want a rule to say exactly what may be auto-approved, so that I can read it and know its limits.

1. THE SYSTEM SHALL store an approval rule with: `id` (`r1`, `r2`, ... from the store's durable counter, never reused), `service_id`, `match_type` (`signature` or `category`), `match_value`, `most_urgent_severity`, `expires_at`, `max_per_hour`, `max_per_incident`, `max_files`, `max_lines`, `status`, `created_by`, `created_at`, `source_proposal_id` (optional), `note` (up to 200 characters), `use_count`, and `code_folder` (the service's resolved code folder at creation; the rule is bound to it, see 6.9). [Auto] [PBT]
2. THE SYSTEM SHALL support exactly one `match_type` per rule. With `signature`, `match_value` is a normalized incident signature (exact string equality, up to 200 characters). With `category`, `match_value` is one of the categories `diagnostics` emits (`memory`, `disk`, `network`, `security`, `crash`, `unknown`). [Auto]
3. THE SYSTEM SHALL define `most_urgent_severity` (replaces the earlier name `min_severity`) as an integer, 3 or 4: the most urgent incident severity the rule may cover. A rule applies only to an incident whose severity number is greater than or equal to it. Severity 0 to 2 (emerg to crit) is never auto-approved, and only severities up to 4 open incidents in the tracker, so 5 to 7 are unreachable and are rejected with 422. The default is 3. [Auto] [PBT]
4. THE SYSTEM SHALL require `expires_at` to be at most 30 days after creation (default 7 days). THE SYSTEM SHALL bound `max_per_hour` to 1 to `MAX_PROPOSALS_PER_SERVICE_HOUR` (the upper bound, value 10; the default `max_per_hour` of a new rule is 3). That constant is the same one that caps new proposals per service per hour (R9.4), so the rule limit can never exceed what the service can submit. THE SYSTEM SHALL bound `max_per_incident` to 1 to 3 (default 1) and count it **per opening** of the incident (R4.2(h), `store` R5.7), `max_files` to 1 to 20 (default 5), and `max_lines` to 1 to 500 (default 100). [Auto] [PBT: for every accepted rule, `max_per_hour` is at most the proposal rate cap]
5. THE SYSTEM SHALL allow rules only for a service that exists and has a code folder, and for proposals of kind `code_change` (D4: configuration, restart and infrastructure actions are out of scope). [Auto]
6. THE SYSTEM SHALL allow at most 20 rules per service and 100 in total (counting active and suspended ones). [Auto]
7. THE SYSTEM SHALL use the statuses `active`, `suspended`, `revoked` and `expired`. `revoked` and `expired` are final. [Auto] [PBT]
8. THE SYSTEM SHALL persist rules in the store (table added through the migration registry) and keep them across restarts. [Auto]

### 2. Who can create, widen and re-enable rules, and who can pause, revoke and withdraw (D3)
**User story:** As a developer, I want only me to be able to create, widen or re-enable a rule, and a token-free emergency stop, so that the agent cannot grant itself autonomy and I can always halt it.

1. WHEN a rule is created, edited or widened, re-enabled after a suspension, or its limits are changed, or auto-approval is resumed, THE SYSTEM SHALL require a valid admin token (`services` R6) or, on IRC, an operator whose connection registered with `PASS <admin token>` (`services` R9). IF the token is missing or wrong, THEN THE SYSTEM SHALL return 401 and change nothing. [Auto]
2. THE SYSTEM SHALL NOT expose any MCP tool, and the MCP server SHALL NOT call any endpoint, that creates, edits, enables, resumes or lists approval rules (`GET /api/rules` is read-only for the dashboard and is not an MCP tool). [Auto: tool list and endpoint-allow-list test]
3. **Pause (global and per rule), revoke and withdraw are token-free (user decision).** WHEN a caller pauses auto-approval or one rule (`POST /api/rules/pause`, the dashboard, `!pause` on IRC), revokes a rule (`POST /api/rules/{id}/revoke`, `!revoke <id>`), or withdraws a proposal (R9.8), THE SYSTEM SHALL require no admin token and no `PASS`, on REST, in the dashboard terminal and on IRC (any registered nick, operator status not required), and SHALL ALWAYS execute it (idempotent, it only reduces what the agent may do). THE SYSTEM SHALL write an audit row and announce each call through the shared `reduce_audit_limiter` of `services` R6.5: above 10 per minute per caller key, calls still take effect and are summed into one coalesced audit row without further announcements. There is no configuration switch for this. [Auto] [PBT]
4. THE SYSTEM SHALL provide `POST /api/rules/pause` and `POST /api/rules/resume`. WHILE rules are paused, THE SYSTEM SHALL approve nothing automatically (proposals stay `pending` for the user). Pausing follows R2.3. Resuming is a re-enable and always needs the token (R2.1). A pause does not change rule statuses and does not deny already approved proposals. [Auto]
5. WHEN any rule operation succeeds or fails authorization, THE SYSTEM SHALL write an audit row with the actor (`dashboard` or the IRC nick), the rule id and the changed fields with before and after values. [Auto]
6. THE SYSTEM SHALL expose these REST endpoints: `GET /api/rules` and `GET /api/rules/{id}` (read-only, no token), `POST /api/rules` and `PATCH /api/rules/{id}` (token), `POST /api/rules/{id}/revoke` (no token), `POST /api/rules/{id}/enable` (token, suspended rules only), `POST /api/rules/pause` (no token, never refused), `POST /api/rules/{id}/pause` (no token, never refused; sets that rule `suspended`, and re-enabling it needs the token, R6.7) and `POST /api/rules/resume` (token). [Auto]
7. THE SYSTEM SHALL record `created_by` as the acting identity and SHALL reject any request that tries to set `created_by` itself. [Auto]

### 3. Creating a rule from an approved proposal
**User story:** As a developer, I want a button on a proposal I just approved that says "always approve fixes like this one", so that I do not fill in a form.

1. WHEN the user calls `POST /api/proposals/{id}/rule` with the admin token, THE SYSTEM SHALL create a rule from that proposal only if (a) its `approved_by_rule` column is null (it was approved by a user), and (b) its status is `approved`, or its status is `done` AND `verification_result` is `passed`. IF not (a rule approved it, it is `pending` or `denied`, or it is `done` with `verification_result` `failed`, `not_run` or null), THEN THE SYSTEM SHALL return 409 with the reason. [Auto]
2. THE SYSTEM SHALL fill the defaults from the proposal's incident: the incident's service, `match_type: signature` with the incident's signature, and `most_urgent_severity` equal to the larger of 3 and the incident's severity number, capped at 4 (so the rule never covers more urgent incidents than the one the user saw). [Auto] [PBT]
2a. THE SYSTEM SHALL provide `GET /api/proposals/{id}/rule-defaults` (read-only, no token) that returns the defaults of 3.2 and the reason (409 or 422 as in 3.1 and 3.5) without creating anything, so the dashboard can pre-fill the form. `POST /api/proposals/{id}/rule` applies the same checks and creates the rule. [Auto]
3. THE SYSTEM SHALL let the request override `match_type` (to `category`), limits and `expires_at` within the bounds in 1.4, and SHALL return the created rule with all effective values. [Auto]
4. THE SYSTEM SHALL set `source_proposal_id` and write an audit row linking the rule to the proposal. [Auto]
5. IF the incident has no service or the service has no code folder, THEN THE SYSTEM SHALL return 422 explaining which is missing. [Auto]

### 4. Matching model: when is a proposal auto-approved?
**User story:** As a developer, I want a precise definition of "similar bug", so that I am never surprised.

1. WHEN a proposal is created, THE SYSTEM SHALL evaluate the rules of the incident's service in ascending rule id order, and stop at the first rule for which every condition in 4.2 holds. [Auto] [PBT]
2. A rule matches only WHEN all of these are true: (a) auto-approval is not paused; (b) the rule is `active` and not past `expires_at`; (c) the proposal kind is `code_change`; (d) the incident's `service_id` equals the rule's; (e) the incident signature equals `match_value` (for `signature`) or the incident category equals it (for `category`); (f) the incident severity number is at least `max(3, most_urgent_severity)`; (g) the rule's auto-approvals in the last 60 minutes are fewer than `max_per_hour`; (h) the rule's auto-approvals for this incident **in its current opening** (`store` R5.7, the proposal's `incident_opening`) are fewer than `max_per_incident`, so a reopened incident (B14) gets a new allowance and a fix that did not hold is not retried more than `max_per_incident` times per opening; (i) the proposal declares `files` (1 to `max_files`, section 9) and every file passes 5.1; (j) the proposal declares `estimated_lines` that is at most `max_lines`; (k) the proposal declares a non-empty `verification`; (l) the service's current resolved code folder equals the rule's `code_folder`; and (m) the cooldown of 4.9 does not apply. [Auto] [PBT]
3. IF any condition fails, THEN THE SYSTEM SHALL leave the proposal `pending` for the user, exactly as today, and SHALL record in the audit log which rule was checked and which condition failed (first failing condition only). [Auto]
4. THE SYSTEM SHALL evaluate and record the decision atomically (one store transaction for a store-backed hub, one hub lock for an in-memory `Hub()` whose rules live in the private `:memory:` store), so two proposals created at the same time cannot both use the last slot of `max_per_hour`. [Auto] [PBT]
5. THE SYSTEM SHALL evaluate rules in the hub (`create_proposal`), never in the agent, the MCP server or a client. A client cannot send a status or `decided_by` when creating a proposal. [Auto: API test that such fields are ignored or rejected]
6. WHEN a rule matches, THE SYSTEM SHALL set the proposal to `approved` with `decided_by` `rule:<id>` and `approved_by_rule` `<id>`, increment `use_count`, and return the proposal in the create response with `status: approved`. THE SYSTEM SHALL derive "approved by a rule" everywhere (the `approval` object, `verify_proposal`, rule creation from a proposal, the dashboard) from the `approved_by_rule` column, never from the text of `decided_by`. [Auto]
7. THE SYSTEM SHALL treat an incident with no service, a service with no code folder, or severity 0 to 2 as never matching. [Auto] [PBT]
8. `POST /api/proposals/{id}/decide`, IRC and the terminal SHALL record `decision_origin: user` (`store` R4.7). THE SYSTEM SHALL write `decision_origin: hub` only from the hub's own rule and sweep logic. `POST /api/proposals/{id}/decide` SHALL reject (422) a `by` value that, case-insensitively, starts with `rule`, `rule-`, `withdrawn:` or `expired`, and SHALL cap `by` at 60 characters. Only the hub's own rule logic writes those prefixes. A test SHALL send `by: "rule:r1"` and `by: "rule-revoked:r1"` through `/decide` and assert 422 and that `approved_by_rule` stays null. [Auto] [PBT]

9. WHEN a proposal is created for an incident that reopened (`store` R5.7) within `CHIMERICMESH_AUTOFIX_COOLDOWN_SECONDS` (default 86400, 24 hours) after a proposal that a rule auto-approved (`approved_by_rule` set) for the same incident was completed (status `done`), THE SYSTEM SHALL NOT auto-approve the new proposal by any rule, because the previous fix evidently did not work. THE SYSTEM SHALL leave it `pending` for the user, write an audit row `autofix_cooldown` that names the earlier proposal, the rule and the reopen time, and announce one line (the normal `PROPOSAL` line, with the note that the earlier auto-fix did not hold) through the injection-safe formatter. The time is read from the injected clock, so tests use a fake clock. [Auto] [PBT: for every history of completions and reopen times, a proposal created inside the cooldown after an auto-approved completion for the same incident is never auto-approved, and one created at or after the cooldown is decided by the other conditions only]

### 5. Scope of the files
**User story:** As a developer, I want an auto-approved fix to be unable to touch anything outside my service's code, so that the blast radius is one folder.

1. THE SYSTEM SHALL pass a declared file only when `path_in_code_folder(service.code_folder, file)` (`services` R2.3) is true, treating relative paths as relative to the code folder, AND the path matches none of the protected patterns: `.git/**`, `.kiro/**`, `.github/**`, `**/.env`, `**/.env.*`, `**/*.pem`, `**/*.key`, `**/id_rsa*`, `**/*.pfx`, and the hub's own data directory. [Auto] [PBT]
2. THE SYSTEM SHALL pass only files with extensions that are source or test files (the suffix list of `diagnostics.find_code_hints`, extended with `.php`, `.pl`, `.cs`, `.cpp`, `.cc`, `.jsx`, `.tsx`). Config files (`.ini`, `.conf`, `.yaml`, `.json`, `.toml`) fail, in line with D4. [Auto]
3. THE SYSTEM SHALL cap each declared path at 300 characters and reject paths with NUL or control characters. [Auto] [PBT]
4. THE SYSTEM SHALL NOT claim that these checks stop the agent from writing elsewhere. The enforcement of writes belongs to Kiro's permissions (`agent-loop` R5). [Manual: review of the docs text]

### 6. Lifetime, revocation and completion checks
**User story:** As a developer, I want to stop auto-approvals instantly and want the system to notice when an auto-approved fix misbehaves.

1. WHEN a rule is revoked or expires, THE SYSTEM SHALL stop matching it on the next proposal evaluation, with no cache that delays it. [Auto] [PBT]
2. WHEN a rule is revoked, THE SYSTEM SHALL also change every proposal it approved and that is still `approved` (not `done`) to `denied` with `decided_by` `rule-revoked:<id>`, so an agent that re-checks before acting sees it. [Auto]
3. THE SYSTEM SHALL use the transition `approved` to `denied` only for revocation, suspension (6.5, 6.9) and 6.4, with `decided_by` `rule-revoked:<id>`, `rule-suspended:<id>` or `rule-expired:<id>`, as the transition table of `store` R4.3 allows, and only for proposals whose `approved_by_rule` is set. [Auto] [PBT]
4. WHEN a proposal approved by a rule has been `approved` for more than 30 minutes without completion, THE SYSTEM SHALL change it to `denied` with `decided_by` `rule-expired:<id>`, persisted in the store, the next time it is read, listed or completed, and at least once a minute by the same sweep as 6.8 (injectable clock). `denied` is final and `complete_proposal` for it returns 409. Manual approvals have no such limit. [Auto]
5. WHEN `complete_proposal` is called for a rule-approved proposal with `files_changed`, `lines_changed` and `verification_result`, THE SYSTEM SHALL check them against the rule. IF any changed file fails 5.1, or more files than `max_files` or more lines than `max_lines` were changed, or `verification_result` is `failed`, THEN THE SYSTEM SHALL set the rule `suspended`, write an audit row `rule_suspended` with the reason, and announce it. [Auto] [PBT]
6. WHEN `complete_proposal` is called for a rule-approved proposal without `files_changed`, THE SYSTEM SHALL still complete it, record `completion_unverified` in the audit log, and leave the rule active. [Auto]
7. WHEN a suspended rule is re-enabled with the admin token, THE SYSTEM SHALL set it `active` and write an audit row. A revoked or expired rule cannot be re-enabled. [Auto]
8. THE SYSTEM SHALL mark rules past `expires_at` as `expired` at least once a minute and on read. [Auto]
9. THE SYSTEM SHALL register with `registry.subscribe` (`services` R10) at startup so it learns of code-folder changes and service deletion inside the registry's own transaction. WHEN a rule is suspended (6.5), or when the service's `code_folder` changes or is removed (`on_code_folder_changed`), or when the service is deleted (`on_service_deleted`), THE SYSTEM SHALL set every affected rule `suspended` (and, for the rule-approved proposals still `approved`, `denied` with `decided_by` `rule-suspended:<id>`) in one transaction, write an audit row per rule, and announce it. Re-enabling needs the admin token (6.7) and fails with 409 when the rule's `code_folder` differs from the service's current one: the user must create a new rule. [Auto] [PBT]

### 7. Audit and announcements
**User story:** As a developer, I want to see every automatic decision and be told when one happens.

1. WHEN a proposal is auto-approved, THE SYSTEM SHALL write an audit row with the actor `rule:<id>`, the proposal id, incident id, service id, the declared files and the rule's remaining quota. [Auto]
2. WHEN a proposal is auto-approved, THE SYSTEM SHALL announce on IRC and in the transcript a single line, **instead of** the baseline `PROPOSAL ... reply !approve <pid> or !deny <pid>` line: `AUTO-APPROVED <pid> by rule <rid> (service <sid>): <action>. Revoke with !revoke <rid>`. It SHALL NOT tell the reader to approve (B17). The line passes the injection-safe formatter and respects the 510 byte limit (R3.2). [Auto] [PBT]
3. WHEN a rule is created, revoked, suspended, expired or paused, or a proposal is withdrawn, THE SYSTEM SHALL announce one line saying so, with the acting identity. The `REOPENED` announcement of an incident belongs to `store` R5.7 alone and is not repeated here (one line per reopen). [Auto]
4a. THE SYSTEM SHALL accept an optional `service_id` filter (including `unassigned`) on `GET /api/proposals`, applied before the limit (`store` R4.4), so a per-service view never loses older proposals. [Auto]
4. THE SYSTEM SHALL expose the audit log through `GET /api/audit` (read-only, filterable by `service_id`, `rule_id`, `proposal_id`, `limit`). The audit endpoint is not an MCP tool. [Auto]
5. THE SYSTEM SHALL include in `GET /api/proposals/{id}` an `approval` object: `by` (`user` or `rule`), `rule_id`, `rule_status_now`, `approved_at`, so a reader can see who approved it and whether the rule is still active. [Auto]
6. THE SYSTEM SHALL NOT write the admin token or log lines into audit or IRC text. [Auto: canary test]

### 8. IRC commands
**User story:** As a developer on IRC, I want to see and stop rules from my chat client.

1. THE SYSTEM SHALL add `!rules` (list active rules: id, service, match, expiry), `!revoke <rule-id>`, `!withdraw <proposal-id>` and `!pause` (any registered nick; no operator status and no `PASS`; always executed, rate-limited in audit volume only, `services` R6.5 and R9.3). Unknown ids get a plain reply. `!help` lists them. [Auto]
2. THE SYSTEM SHALL NOT add any IRC command that creates, edits or re-enables a rule (these need the user: the token, or an operator with `PASS`). [Auto]
3. THE SYSTEM SHALL keep `parse_command` total on arbitrary text (R6.5). [Auto] [PBT]

### 9. Structured proposals and creation rules (B3, B7)
**User story:** As a user, I want each proposal to say which files change and how it will be verified, so that I can approve with confidence. As the hub, I want these fields validated in one place before any rule looks at them.

1. THE SYSTEM SHALL extend `POST /api/proposals` and the hub `create_proposal` with optional `files` (up to 20 paths, 300 characters each), `estimated_lines` (0 to 1000), `verification` (up to 300 characters) and `kind` (only `code_change` in V1, the default). Callers that send only `incident_id`, `action`, `details` keep working. [Auto]
2. WHERE the proposal's incident has a service with a code folder, THE SYSTEM SHALL reject (422) `files` entries that are absolute, contain `..`, NUL or control characters. A proposal without `files` is accepted and can only be approved by a user. [Auto] [PBT]
3. WHILE `Hub.enforce_limits` is true (map B3; `create_app()` without a hub and `python -m chimericmesh` set it, a bare `Hub()` and every existing test do not), IF the incident already has an active proposal (`pending` or `approved`), THEN `Hub.create_proposal` SHALL raise `ActiveProposalExists` (carrying the active proposal id) and the REST route SHALL return 409 naming that proposal. WHILE it is false, the hub allows several active proposals per incident as today, so `test_irc_commands.py` and the commands property tests keep passing. `create_proposal` still returns `None` only for an unknown incident (REST 404). [Auto]
4. WHILE `Hub.enforce_limits` is true, THE SYSTEM SHALL allow at most `MAX_PROPOSALS_PER_SERVICE_HOUR` (one constant, value 10; also the upper bound of a rule's `max_per_hour`, R1.4) new proposals per service per hour. Incidents without a service count under the synthetic key `unassigned` with the same limit. IF exceeded, THEN `Hub.create_proposal` SHALL raise `ProposalRateLimited` and the REST route SHALL return 429. WHILE `enforce_limits` is false, no limit applies (existing test `test_irc_commands.py` creates 205 proposals on one hub). The new tests for 9.3 and 9.4 build the hub with `enforce_limits=True`. [Auto] [PBT]
5. WHEN a proposal is created, THE SYSTEM SHALL return it with its final status: `pending`, or `approved` with `decided_by: rule:<id>` when a rule matched (section 4). [Auto]
6. THE SYSTEM SHALL extend `POST /api/proposals/{id}/complete` with optional `files_changed` (list), `lines_changed` (integer), `verification_result` (`passed`, `failed` or `not_run`), and keep the existing `note` (300 characters, stored in `result_note`, no change to the baseline cap), and apply 6.5 and 6.6. [Auto]
7. THE SYSTEM SHALL return `kind`, `files`, `estimated_lines`, `verification`, `decision_origin`, completion fields and the `approval` object (7.5) from `GET /api/proposals`, `GET /api/proposals/{id}`. This adds keys to `Proposal.to_dict`, so the existing test `tests/unit/test_web_terminal.py::test_proposals_endpoint_fields`, which asserts the exact key set, is **intentionally edited** to the new key set (map, "Existing tests that change"); this is one of exactly two existing tests V1 changes. The proposal id format stays `p<n>` (D5). [Auto]
8. WHEN `POST /api/proposals/{id}/withdraw` is called (no token; on IRC `!withdraw <id>` by any registered nick; audited and announced), THE SYSTEM SHALL change a `pending` proposal to `denied` with `decided_by` `withdrawn:<actor>`, so the incident can get a new proposal. THE SYSTEM SHALL also change a `pending` proposal older than `CHIMERICMESH_PROPOSAL_TTL_HOURS` (default 24) to `denied` with `decided_by` `expired`, in the sweep of 6.8. [Auto]

## Boundaries

- Always: decide in the hub; write an audit row for every decision and failure; keep the matching a pure function of (rule, incident, proposal, clock, history) so it can be tested; require the token for anything that widens.
- Ask first: new `match_type` values (for example regex); rules that cover more than one service; raising the caps in 1.4; allowing non-code files.
- Never: let the agent or any MCP tool create, edit, list or re-enable rules; auto-approve severity crit or worse; auto-approve a proposal without declared files; auto-approve a non-`code_change` proposal; reuse a rule id; match by free-text similarity or model judgement.

## Success Criteria

1. Approve `p1` manually, complete it with `verification_result: passed`, then `POST /api/proposals/p1/rule` with the token: rule `r1` exists for the service and signature. Resolve the incident, wait past `CHIMERICMESH_REOPEN_AFTER_SECONDS` (fake clock), and send the same bug again on the same host: the incident reopens (`store` R5.7, opening 2) and the agent's new proposal, with files inside the code folder and `estimated_lines` 20, is `approved` with `decided_by: rule:r1` in the create response, an audit row and an IRC line. The same applies to the same signature on a different host of the same service. (`r1` was created from a manually approved proposal, so no cooldown applies here.)
2. The same proposal with one file outside the code folder, or `estimated_lines` 101, stays `pending`, and the audit row names the failed condition.
3. A fourth matching proposal inside one hour (`max_per_hour` 3) stays `pending`.
4. Revoking `r1` immediately turns its still-approved proposals into `denied` with `decided_by: rule-revoked:r1`; the next matching proposal stays `pending`. Changing the service's code folder suspends `r1` the same way.
5. `POST /api/rules` without the token returns 401; the MCP tool list contains no rule tool. `POST /api/rules/r1/revoke` and `POST /api/rules/pause` without the token succeed; `POST /api/rules/resume` and a rule re-enable without the token return 401. Eleven pause calls in one minute all take effect (ten audit rows plus one coalesced row). `POST /api/proposals/p2/rule` for a proposal `done` with `verification_result: failed` returns 409.
6. A rule-approved proposal completed with a changed file outside the code folder suspends the rule.
7. Property tests (design phase) cover: matching is monotone (tightening any limit never makes a non-matching proposal match), no match when any condition fails, hourly quota never exceeded under concurrent creation, `most_urgent_severity` default from proposal, `max_per_hour` never above the proposal rate cap, `decided_by` reserved prefixes never accepted from callers, per-opening counting, rule id uniqueness across restarts, file scope containment, IRC line safety.

8. An incident whose previous fix was auto-approved by a rule and completed reopens 1 hour later (fake clock, cooldown 86400): its new matching proposal stays `pending`, with an `autofix_cooldown` audit row and one announcement. The same reopen 25 hours after completion is auto-approved by the matching rule.
9. The table-driven import test (capability map, "Dependency direction") passes: `auto-approval` imports only `core`, `services` and `store` (it receives the hub's `announce` and clock as injected callables).

## Out of Scope (Deferred)

- Rules for configuration, restart or infrastructure actions: D4, next spec.
- Regex or fuzzy signature matching, and cross-service rules: widens the blast radius and is hard to explain to the user.
- Rule templates and import or export: not needed for the demo.
- Notifications by e-mail or push when a rule fires: IRC and the dashboard are enough for V1.
- Hard blocking of writes outside the code folder by the hub: the hub never executes or intercepts (R7.4); Kiro permissions enforce it.
- Rule approval windows (office hours only): adds scheduling; expiry covers the main need.
- Two-person approval: D1, single developer.
- Auto-revert of a bad auto-fix: needs git integration; deferred with infrastructure actions.

## Open Questions

1. Resolved: accepted: `max_per_incident` default 1 counts per opening (R4.2(h)). A bug that returns within `CHIMERICMESH_AUTOFIX_COOLDOWN_SECONDS` (default 86400) after an auto-approved fix completed needs the user's approval (R4.9, map B22), which stops fix loops that `max_per_hour` alone would allow (accepted 2026-11-18).
2. Resolved: the 30 minute validity of a rule approval (6.4) is kept (accepted 2026-11-18).

## Notes for design

Findings from the final review that design must resolve. No acceptance criterion changes for them.

- M2: `auto-approval` cannot import `hub`, yet R4 needs the incident's service, signature, severity and `opening`, and runs inside `create_proposal`. Name the injected interfaces (incident source, proposal port) in the map and receive them at construction; add one criterion that runs the module against a plain `Hub()` and against the store-backed hub.
- M3: R4.4 requires an atomic decision under one store transaction (store-backed) or the hub lock (in-memory). Design states which thread runs it, that callbacks may not take the hub lock, and that an in-memory `create_proposal` holds `hub._lock` around evaluate-and-decide (see `store` notes).
- L4: put the code-suffix and skip-directory lists in `core` as public names, and keep `find_code_hints` behaviour unchanged by test (R5.2).
- L7: the reserved-prefix rule in R4.8 also refuses a legitimate user named `ruleman`; document it in the README or tighten it to `rule:`, `rule-`, `withdrawn:` and `expired` followed by `:` or `-`.
- L8: split R4.2 (a) to (l) into sub-numbered criteria (4.2.1 to 4.2.12) so each maps to one test.
