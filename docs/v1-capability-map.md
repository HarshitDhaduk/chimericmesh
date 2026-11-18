# Capability Map: ChimericMesh V1

Status: revision 6: approved by the user on 2026-11-18; design in progress. Date: 2026-11-18. Hackathon deadline: 2026-12-04 2:00 PM PT.

ChimericMesh V1 lets a developer connect applications that run on, or only offer, outdated protocols (Syslog, Telnet, IRC, plain log files of old servers) to one local hub. The hub shows every service in one dashboard and exposes them to Kiro agents over MCP. The agent investigates, explains, proposes and fixes bugs in the **application's code**. The user approves each fix, or creates standing rules that let the hub approve similar fixes without interrupting them.

Baseline: `.kiro/specs/legacy-mesh/` (R1 to R10) describes what exists on `main`. It stays unchanged. Each V1 module spec extends it and links the requirements it touches.

## Binding decisions

These come from the user and are cited as D1 to D5 in every module spec.

- **D1. Deployment.** Everything runs on the developer's machine. No hosted dashboard, no accounts, no multi-tenancy, no relay to the internet in V1. Bind to localhost (`127.0.0.1`) by default.
- **D2. Protocols.** Support most of the commonly still-used outdated protocols. No wrapper around local apps: no `chimericmesh run -- <cmd>`, no stdout capture. ChimericMesh connects to apps by detecting them and knowing the default log paths of known legacy apps per OS. The user can optionally add log paths per service, chosen per protocol or format.
- **D3. Autonomy.** The agent works autonomously (investigates, explains, proposes, fixes). The approval step belongs to the user. The user can create standing rules so that, when a similar bug happens again (or for low-severity tasks), the agent may fix without interrupting the user. **User decision (confirmed 2026-11-18):** **create, edit/widen, re-enable and resume** need the user: the admin token on the dashboard or REST, or an IRC operator who registered with `PASS <admin token>`. Resume is a re-enable, so it needs the token. **Pause (global and per rule), revoke and withdraw** need no token and no `PASS` on any channel (REST, dashboard terminal, IRC; on IRC any registered nick may use them, operator status is not required). They only reduce what the agent may do. They are rate-limited by one constant (10 per minute per caller key; the excess is coalesced into one audit row but still executed), audited and announced. There is no switch: token-free is the behaviour. The hub, not the agent, decides whether a proposal is auto-approved. The agent has no MCP tool to create, edit, widen or resume rules. Because pause, revoke and withdraw are token-free REST routes, the agent could call them from a shell; that can only reduce its own permissions. See "Honest limits of the access model".
- **D4. Fix scope.** "Fix" in V1 means changing the application's code only. Configuration changes, restarts and infrastructure actions are out of scope (next spec).
- **D5. Proposal ids.** Ids stay human-friendly and sequential: `p1`, `p2`, `p3`, ... They are never reused, including across hub restarts, which is solved with persistence and not with id suffixes. The unmerged branch `fix/proposal-ids-and-recheck` changed the format to `p<n>-<tag>`. That change is rejected and nothing builds on it. Limitation: deleting the database file restarts the sequence (see `store` R3.9).

## Honest limits of the access model

(Accepted by the user, 2026-11-18.) The agent runs as the same OS user as the hub. It can therefore read any file that user can read (including the admin token file) and call the local REST API. The admin token protects against **other local processes and web pages** that do not have the file or the browser session. It does **not** stop the agent. The V1 safeguards against the agent approving its own work are, in this order:

1. Kiro's own permission prompt on shell commands and on writes outside the approved scope. The `mesh-watcher` profile adds no allow rule for `shell` and none for `fs_write` (`agent-loop` R5). This is the real gate, and it works only while the user does not paste an allow rule for shell.
2. Deny rules for the token file and the database in the agent profile, and the home directory outside the workspace (`store` R1.1). A second layer, bypassable by a shell command the user has allowed.
3. The optional `Pre Tool Use` hook (`agent-loop` R5.9, built as an extra layer; Kiro's own write prompt stays the primary gate) that calls the read-only verify route and blocks a write when the proposal is not approved.
4. The audit log and the IRC announcement of every decision, so a misuse is visible afterwards.

Residual risk, stated plainly: in autonomous mode (the user pasted a path-scoped write allow and one test-command allow), a prompt-injected agent can edit files inside the code folder before a proposal is approved, and can run the allowed test command, which executes code it just wrote with the user's rights. Prompt mode (the default) avoids this at the cost of Kiro prompts. The user accepted the test-command risk for hands-off mode only (2026-11-18); the README must document it when `agent-loop` is implemented. Pause, revoke and withdraw are token-free REST routes (D3), so the agent can call them from a shell. That can only reduce its own permissions. No requirement in this set claims otherwise.

## Modules

| Module id | Responsibility | Depends on | Effort (days) |
|---|---|---|---|
| `store` | SQLite persistence (stdlib `sqlite3`): events with retention, incidents (with the reopen rule), proposals, audit log, durable counters, migration registry, and the one module that sets file permissions (`fsperms.py`). Durable sequential ids (`p1`, `p2`, ...). | none | 1.5 |
| `services` | Service registry (name, hosts, protocols, file log sources, code folder), event-to-service tagging, per-service status, the change-notification interface (`subscribe`), and the local access control (admin token, Host check, IRC operator hardening). Knows no higher module. | store | 1.75 |
| `connectors` | Intake: existing Syslog/UDP, Telnet, IRC, plus Syslog/TCP and log-file tailing with rotation, and the format parsers for legacy servers (syslog-style text, NCSA, W3C extended, xferlog). Every event is tagged with its service. Subscribes to service changes and supplies the per-source status provider. | services, store (offsets) | 2.25 |
| `discovery` | Opt-in local scan (OS tools only) that matches processes and ports against a built-in catalog of legacy apps and proposes log paths. The user confirms before anything is watched. Valid format names come from the registry's injected `known_formats`, so it imports no `connectors` code. | services | 1.25 |
| `auto-approval` | User-created standing approval rules, the hub-side matching and limits, the audit trail, revocation. Also owns the hub/REST side of structured proposals (files, estimated lines, verification, completion report, one active proposal per incident, withdraw/expiry). Subscribes to service changes. | store, services | 1.75 |
| `agent-loop` | The REST routes and hub code behind the new MCP tools (service events, digest, code search, verify, long poll, permissions snippet), the thin MCP wrappers, agent profile and power changes, Kiro permission guidance. | services, store, auto-approval | 1.5 |
| `dashboard` | Services overview, discovery review, per-service live log stream (SSE), incidents and proposals per service, rule management with audit trail, existing terminal and guided workflow. | all modules above | 2.0 |
| **Total (modules)** | | | **12.0** |

Spec files: `.kiro/specs/<module-id>/requirements.md`. Design and tasks are the next gated phase and are not written yet.

**Dependency direction (one-way, enforced by one table-driven test).** Arrows go from the higher group to the lower one only. One static test assigns every `.py` file under `src/chimericmesh/` to a group with the file table below, parses its imports, and compares them with the "may import" table. It fails on any import not listed, and on any file that has no group (so a new file must be added to the table). Each module spec lists it in its Success Criteria. Imports under `TYPE_CHECKING` count.

Groups and their files:

| Group | Files (under `src/chimericmesh/`) |
|---|---|
| `core` | `events.py`, `diagnostics.py`, `parsers/*`, `replay*` helpers that only read files |
| `store` | `store/*` (package: connection, migrations, counters, audit, events), and `fsperms.py` (the only file-permission code, stays at `chimericmesh/fsperms.py`) |
| `services` | `services/*` (registry, tagging, access control) |
| `connectors` | `connectors/*` (tailer, formats, syslog TCP), and the existing UDP, Telnet and IRC listener classes once moved or wrapped there. Until then `listeners.py` belongs to `wiring` |
| `discovery` | `discovery/*`, `catalog/*` |
| `auto-approval` | `approvals/*` (rules, matching, proposal validation) |
| `agent-loop` | `agent/*` (digest, code search, verify, watch queue, snippet) |
| `dashboard` | `web/*` (static page, SSE route module) |
| `wiring` | `hub.py`, `api.py`, `commands.py`, `listeners.py`, `mcp_server.py`, `__main__.py`, `app.py` if added |

| Group | May import |
|---|---|
| `core` | stdlib only |
| `store` | `core` |
| `services` | `core`, `store` |
| `connectors` | `core`, `store`, `services` |
| `discovery` | `core`, `store`, `services` |
| `auto-approval` | `core`, `store`, `services` |
| `agent-loop` | `core`, `store`, `services`, `auto-approval` |
| `dashboard` | `core` (types only); it reaches every module through REST and SSE routes registered by `wiring` |
| `wiring` | every group above |

The file layout (package names above) is a proposal for the design phase; the test only needs the file-to-group table to exist and be complete. No group other than `wiring` may import `hub`, `api`, `commands`, `listeners` or `mcp_server`. A module that must tell the hub something gets an injected callable (for example `store` receives an `announce(text)` callable for the `REOPENED` line; `auto-approval` receives the hub's `announce` and `now` callables). `core` classes that the store persists (`Event`, `Incident`) live in `core`, so `store` imports them from there. Where a lower module must tell a higher one that something changed, it exposes `subscribe(...)` (`services` R10) and the higher module registers a callback at startup. Where a lower module needs data only a higher one has (valid format names, per-source runtime status), the higher one injects it when the app is built.

### Boundary refinements against the brief (with reasons)

1. **Schema ownership.** `store` owns the mechanism (connection, migrations, id counters, bounded writes) and the core tables (events, incidents, proposals, audit log). Tables that belong to one module (`services`, `log_sources`, `approval_rules`, discovery dismissals, tail offsets) are created by that module through the store's migration registry. Reason: `store` has no dependencies, so it cannot know about higher modules.
2. **Access control lives in `services`.** The admin token and Host-header check are first needed by the first module that mutates configuration (service registration). `services` provides the `require_admin` check and applies it to its own endpoints. `auto-approval`, `discovery` and `dashboard` apply the same check to theirs. Reason: keeps the arrows one-way without a ninth module.
3. **`connectors` depends on `services` and on `store`.** Event tagging is a pure lookup in `services` that connectors call. Tail offsets are stored through the `store` migration registry directly. `connectors` calls `services`; `services` never calls `connectors` (it only exposes `subscribe` and accepts an injected status provider). Reason: avoids a cycle.
4. **Subprocess allow-list.** Only `chimericmesh/fsperms.py` (Windows `icacls`) and `discovery` may start a process. A static test (`agent-loop` R4.10) enforces this list. Reason: one reviewable place for each.
5. **Reopen rule in `store`.** The rule that a resolved incident can reopen touches incident persistence and the tracker, so it is specified in `store` R5 and used by `auto-approval` (B14, confirmed by the user, default 300 seconds).

## Build order

`store` -> `services` -> `connectors` -> `discovery` -> `auto-approval` -> `agent-loop` -> `dashboard`

`auto-approval` could be built in parallel with `connectors` and `discovery` (it needs only `store` and `services`). The linear order is kept so one person can finish and demo each module. If time is short, `auto-approval` moves ahead of `discovery`, because D3 (user-approved autonomy) is the core of the product and discovery is convenience.

## Effort against the 16 day budget

**Calendar.** The user is going with the full plan and works on weekends. The budget is 16 calendar days, 2026-11-18 to 2026-12-03 inclusive, and every day counts as a working day. The deadline is 2026-12-04 2:00 PM PT.

**What the estimates include.** Module estimates cover implementation and tests, assuming the existing parsers, listeners and tests are reused. `design.md` and `tasks.md` (about 0.1 day per module, 0.7 in total) and the live Kiro checks are listed separately.

| Item | Days |
|---|---|
| Seven modules, full requirements as written | 12.0 |
| `design.md` and `tasks.md` for seven modules | 0.7 |
| Live Kiro checks (one optional confirmation of the 25 s wait F2, permissions in the IDE F5/L9, `Pre Tool Use` hook input F8b, `agent-loop` SC1 to SC3) | 0.5 |
| **Total as specified** | **13.2** |
| Budget (16 calendar days, 2026-11-18 to 2026-12-03) | 16.0 |
| Left for video, write-up and buffer | about 2.8 |

No requirement is cut. The `store` (1.5) and `services` (1.75) estimates are the most likely to run over, because they carry the migration registry, the access control and the reopen rule. **Recommendation:** review progress after those two modules (expected done on 2026-11-20, day 3) and compare it with the 3.25 days planned for them. If they are more than about 0.5 day behind, plan to use the contingency list below. Keep the 2.8 day tail for the video and write-up as long as possible.

**Contingency list (only if behind schedule).** Drop in this order. "Decide by" is the calendar date on which the module is expected to start, derived from the build order and the estimates, starting on 2026-11-18 with `design.md` and `tasks.md` (0.7 day) first: `store` starts 11-18, `services` 11-19, `connectors` 11-21, `discovery` 11-24, `auto-approval` 11-25, `agent-loop` 11-27, `dashboard` 11-28. Every item below is **in V1 scope** unless it is dropped.

| # | Decide by | Drop | Days saved | Module |
|---|---|---|---|---|
| 1 | 2026-11-21 | **Syslog over TCP.** UDP syslog stays; rsyslog and syslog-ng can send UDP. | 0.5 | `connectors` |
| 2 | 2026-11-21 | W3C extended and xferlog parsers. IIS and FTP logs use the `plain` format, and the `discovery` catalog uses `plain` for them. The whole 0.4 is taken from the `connectors` estimate; `discovery` keeps its 1.25. | 0.4 | `connectors` |
| 3 | 2026-11-24 | macOS discovery scan. Linux and Windows remain. | 0.25 | `discovery` |
| 4 | 2026-11-25 | `category` match type and rule editing (`PATCH`). Rules match by signature only; to change a rule the user revokes and creates a new one. | 0.25 | `auto-approval` |
| 5 | 2026-11-27 | `explain_log_window`. The agent uses `service_events`. | 0.25 | `agent-loop` |
| 6 | 2026-11-28 | Rule-creation form, permissions panel and audit-log view in the dashboard. Keep "Always approve fixes like this" on a proposal, revoke, pause, and the audit rows via `GET /api/audit` (no UI table). | 0.5 | `dashboard` |
| 7 | 2026-11-28 | Stream text and severity filters in the dashboard (the stream itself stays). | 0.1 | `dashboard` |

Dropping all seven saves 2.25 days (13.2 - 2.25 = 10.95). The criteria that belong to each item stay in the module specs as ordinary requirements; dropping an item means removing them in a spec revision first. The next candidates, if more is needed: the `unassigned` per-service stream and the discovery UI edit controls (accept with defaults only).

Items already moved out of V1 (details in each module's Out of Scope):

| Item | Days saved | Why |
|---|---|---|
| SNMP v1/v2c traps | about 1.0 | BER decoding, privileged port 162, and no way to demo without extra tools. **First to restore if the build is ahead of schedule.** |
| Windows Event Log (`wevtutil` polling) | about 1.0 | XML parsing, bookmarks, and channel permissions. Windows IIS and app text logs are covered by file tailing. |
| RELP | about 0.5 | Needs a handshake and acknowledgements. rsyslog and syslog-ng can forward over UDP or TCP. |
| Hosted or relayed dashboard, accounts | not estimated | D1: everything stays on the developer's machine. |
| Configuration, restart and infrastructure fixes | not estimated | D4: next spec. |
| `Pre Tool Use` hook script (optional install, extra layer) | about 0.25, not in the total | A script plus a test. Decided in favour on 2026-11-18 (F8b resolved from the docs); built if there is slack. |

Do not drop: persistence of proposal ids (D5), the hub-side rule enforcement and audit log (D3), the reopen rule (B14, otherwise "the same bug again" cannot happen on one host), the admin token and the IRC operator hardening.

### Resolved decisions (2026-11-18)

- Weekends: yes. The budget is 16 calendar days (2026-11-18 to 2026-12-03).
- No cuts are applied. The cut table became the contingency list above.
- Honest limits of the access model accepted: the token does not stop the agent; Kiro's shell prompt is the real gate.
- D3: create, edit/widen, re-enable and resume need the user; pause, revoke and withdraw are token-free, rate-limited, audited and announced.
- Reopen after 300 seconds (`CHIMERICMESH_REOPEN_AFTER_SECONDS`) is confirmed (B14).
- The user approved the requirements (this map and the seven module specs) on 2026-11-18 and answered every open question. The seven answers:
  1. MCP call wait (`agent-loop` F2): each `wait_for_work` long-poll waits at most 25 s and returns an empty result, and the agent loops. The agent configuration reference gives `mcpServers` `requestTimeout` (default 120000 ms); `timeout` in `mcp.json` is only a connection timeout. The MCP server's HTTP client timeout is raised above the wait (35 s).
  2. The Kiro IDE honours profile `permissions`: yes (configuration reference for IDE 1.0 and CLI V3, the "agent" scope, and the user's live run that prompted before `propose_remediation`).
  3. Absolute Windows globs (`agent-loop` F11): hands-off write allows use workspace-relative globs only. A code folder outside the open workspace still gets its fix, but Kiro prompts for each write (fails safe).
  4. Pre Tool Use in the IDE (`agent-loop` F8b): yes, from the docs. The optional hook (R5.9) is built as an extra layer; Kiro's write prompt stays the primary gate.
  5. Dashboard terminal needs the admin token, pasted once per browser session, not kept in `localStorage` (`services` Open Question 1, `dashboard` Open Question 1).
  6. New B22 (`auto-approval` R4.9): a reopen within `CHIMERICMESH_AUTOFIX_COOLDOWN_SECONDS` (default 86400) after a completed auto-approved fix is not auto-approved and needs the user's approval; announced and audited.
  7. The test command in hands-off mode (`agent-loop` Open Question 5) is accepted for hands-off mode only; prompt mode stays the default; the risk is in "Honest limits" and goes into the README when `agent-loop` is implemented.
- Every other module Open Question was accepted with its stated assumption. The `connectors` NTFS rotation test stays a design-phase item.

### Carried into design

The final review (`.agents/tasks/v1-spec/review.md`, verdict CHANGES_REQUESTED) left these findings open. They are handled in the design phase, listed under "Notes for design" in each module. No acceptance criterion changes for them.

| Id | Topic | Modules |
|---|---|---|
| M1 | Dependency-table groups for `notify.py` and `__init__.py` | map, `store` |
| M2 | Data seam for the in-memory `Hub` | `auto-approval`, `agent-loop`, `services`, `connectors`, `dashboard` |
| M3 | Threading and transaction model | `store`, `services`, `auto-approval` |
| M4 | Double announcement on reopen | `store` |
| L1 | `Event` is frozen, so assigning `id` needs a mechanism (`replace` or `compare=False` field); `submit` returns the tagged event | `store` (R6.1), `connectors` (R1) |
| L2 | The power-docs test also needs every MCP tool in the skills and in `getting-started` as a list entry | `agent-loop` (R6.2) |
| L3 | Map table formatting: B-row order, detached B19 to B21 rows, the `dashboard` "depends on" cell | map |
| L4 | Public suffix and skip-dir lists in `core`; `find_code_hints` behaviour unchanged by test | `auto-approval` (R5.2), `agent-loop` (R1.4) |
| L5 | The proposal-id static scan covers `tests/`; exempt the guard test file | `store` (R3.8) |
| L6 | Demo precondition: restore the `upstream.py` defect before the demo criteria | `agent-loop` (SC1), `dashboard` (SC5) |
| L7 | Reserved-prefix rule is broader than needed (`ruleman`); document or tighten | `auto-approval` (R4.8), `store` (R4) |
| L8 | Compound criteria are not one-condition-per-criterion; split into sub-numbered criteria | `store` (R5.7), `auto-approval` (R4.2), `agent-loop` (R3.1), `services` (R4.2), `connectors` (R3.3) |
| L9 | Profile deny globs with `~/` are unverified; add a "check live" note and point to the hook layer | `agent-loop` (R5.2) |

**Scope of D4 enforcement.** The hub enforces the code-only scope (code folder, source-file extensions, declared files) only for auto-approval. For manually approved proposals, D4 rests on the agent profile and skill text, and the user's own review of the proposal.

## Glossary

- **Event**: one normalized log line (source, host, app, severity 0 to 7, message, timestamp) plus a `service_id` and a `log_source_id`. Defined in `src/chimericmesh/events.py`.
- **Service**: a named application or server the user wants watched. It has an id (kebab-case slug), zero or more hosts and app names, protocols, file log sources and at most one code folder.
- **Log source**: one log **file** of a service: an absolute path plus a format, tailed by the hub. Network listeners (Syslog/UDP, Syslog/TCP, Telnet, IRC) are global, not log sources: their events are matched to a service by host or app name (`services` R4). V1 has no other kind of log source.
- **Connector**: the hub component that turns a protocol or file format into events. Listeners and the file tailer are connectors.
- **Incident**: a cluster of similar events (same host and normalized signature) that reached the open threshold (3 repeats, or severity crit or worse). Identified by the existing 10-character id. In V1 an incident also has a `service_id`, and it can reopen after being resolved (B14). Each time it opens counts as one **opening** (`opening` = 1, 2, ...).
- **Signature**: the normalized message text used for clustering (`diagnostics.signature`). It is the "same bug" key for approval rules.
- **Proposal**: a request, made by an agent, to change an application's code to fix an incident. Ids are `p1`, `p2`, ... and are never reused. Statuses: `pending`, `approved`, `denied`, `done`.
- **Approval rule**: a standing, user-created permission that lets the hub approve proposals that match it (same service, same signature or category, urgency limit, limits) without asking again. Ids are `r1`, `r2`, ...
- **Watch mode**: the agent runs in an open Kiro session and repeatedly waits for new work with the long-polling tool `wait_for_work` (new incidents and approved proposals), investigates and proposes without the user prompting each time.
- **Code folder**: the absolute directory that holds a service's source code. It is the only place an auto-approved fix may change files.
- **Admin token**: a random secret created on first start and kept in a local file. Privileged operations (create or edit services, rules and log sources, decide proposals over REST, run discovery) require it. No MCP tool carries it. It protects against other local processes and web pages, not against the agent (see "Honest limits of the access model").
- **Privileged operation**: any change that widens what the hub watches or what the agent may do. Listed in `services` R6.

## Terminology for criteria tags

Each acceptance criterion ends with a tag:

- `[Auto]` checked by an automated example test (Python, `pytest`).
- `[PBT]` good candidate for a Hypothesis property test (Kiro correctness guidance: https://kiro.dev/docs/specs/correctness/). The properties themselves are written in the design phase. Used only for Python code, never for browser JavaScript.
- `[Manual]` explicit manual check, usually against a live Kiro session or a browser.

## Intentional behaviour changes (everything else keeps working)

| # | Change | Module | Baseline touched |
|---|---|---|---|
| B1 | `create_app` stores state in SQLite by default (`CHIMERICMESH_DB`, default `~/.chimericmesh/chimericmesh.db`, an absolute per-user path outside any workspace). **Store rules in one place (`store` R1.4):** (a) `Hub()` built without a store keeps events, incidents and proposals in memory, exactly as today. (b) `create_app(hub)` with an injected in-memory hub (every existing test) attaches a private `:memory:` store that holds **only** the registry, tail offsets, approval rules, audit rows and counters of the new modules; events, incidents and proposals stay in the hub's memory. (c) `create_app()` without a hub and `python -m chimericmesh` build a file-backed store and a store-backed hub. | store | R7.5 |
| B2 | The **store-backed** hub no longer evicts proposals at 200. `GET /api/proposals` and `list_proposals` return the newest 200 by default, still ordered oldest first within that window (as today), and accept `limit`. The **in-memory** `Hub()` keeps the 200-proposal cap (`MAX_PROPOSALS`) and its existing eviction test unchanged. | store | R7.5 |
| B3 | **Gated by `enforce_limits`** (`Hub(enforce_limits=False)` by default, so every existing test that creates many proposals on one in-memory `Hub()` keeps working; `create_app()` without a hub and `python -m chimericmesh` set it `True`). WHEN on: at most one active (`pending` or `approved`) proposal per incident, signalled by the typed exception `ActiveProposalExists` (REST 409); at most 10 new proposals per service per hour, signalled by `ProposalRateLimited` (REST 429), one constant `MAX_PROPOSALS_PER_SERVICE_HOUR = 10`. `create_proposal` still returns `None` only for an unknown incident (REST 404). New tests set `enforce_limits=True`. | auto-approval | R7.1 |
| B4 | `POST /api/proposals/{id}/decide` and `!approve` / `!deny` typed in the dashboard terminal need the admin token (only when `enforce_access` is on, B12). The web terminal is authorised by the token alone and does not depend on `CHIMERICMESH_OPERATORS`. | services | R6.3, R7.2, R8.3 |
| B5 | Requests whose `Host` header is not on the allow list (default `127.0.0.1`, `localhost`, `[::1]`) get REST 403, to block DNS rebinding (only when `enforce_access` is on). | services | R8.4 |
| B6 | `find_code_for_incident` searches the service's code folder when the incident has a service with one; otherwise `CHIMERICMESH_CODE_ROOT` as today. | agent-loop | R2.4 |
| B7 | Additive fields: `id` (strictly increasing integer, B19), `service_id` and `log_source_id` on events, `service_id`, `opening` and `resolved_at` on incidents (store, services); `decision_origin` (`user` or `hub`, B10) on proposals; `file` added to the allowed `Source` values (connectors); `files`, `estimated_lines`, `verification`, `kind`, `approval`, `incident_opening` on proposals and the completion fields `files_changed`, `lines_changed`, `verification_result` (columns in store, REST and hub models in auto-approval, MCP wrappers in agent-loop). The completion note keeps its 300-character cap and the existing `result_note` column. | store, services, connectors, auto-approval, agent-loop | R4, R7 |
| B8 | A new agent profile `mesh-watcher` is added. `mesh-responder` and the `chimericmesh-remediation` power keep their current behaviour. | agent-loop | R7 |
| B9 | Event rows are bounded in the database (default 100,000 rows or 7 days). The in-memory log view stays at 2000, which comes from `Hub(capacity=2000)`, not from a baseline requirement. | store | R5 (log view) |
| B10 | New transition `approved` -> `denied`, only with `decided_by` `rule-revoked:<id>`, `rule-expired:<id>` or `rule-suspended:<id>`, and only written by the hub itself. `POST /api/proposals/{id}/decide` rejects a caller-supplied `by` that starts with a reserved prefix (`rule`, `rule-`, `withdrawn:`, `expired`), and `decided_by` is capped at 60 characters (baseline 30) so `withdrawn:<nick>` fits. Whether a proposal was approved by a rule is read from the `approved_by_rule` column, and whether a denial came from a user or from the hub is read from the `decision_origin` column (`user` for `/decide`, IRC and terminal decisions; `hub` only for rule revocation, suspension, expiry, withdrawal and proposal expiry, written only by hub code). Neither is ever read from the `decided_by` text. | store, auto-approval | R7.2 |
| B11 | **Access control for the IRC operator commands (only when `enforce_access` is on).** The IRC listener rejects the nicks `dashboard` and `ChimericBot` (any case), at registration and on every later `NICK`. `!approve`, `!deny` and `!resolve` fail closed (refusal text) when `CHIMERICMESH_OPERATORS` is unset or empty. Today an unset list allows any nick. An operator nick also needs a connection that registered with `PASS <admin token>`. `!pause`, `!revoke` and `!withdraw` need neither an operator nick nor `PASS`: any registered nick may use them (D3, rate-limited, audited and announced). Note the intentional asymmetry: IRC `!resolve` needs `PASS`, while REST `POST /api/incidents/{id}/resolve` and the dashboard terminal `!resolve` stay token-free (baseline, `services` R6.10); resolving only closes an incident and can reopen (B14). With enforcement off (every existing test), the baseline behaviour is unchanged. | services | R6.3 |
| B12 | `create_app(hub, ...)` takes `enforce_access: bool | None = None`. With an injected hub (all existing tests) the default is `False`: no Host check, no admin token, no IRC PASS requirement, no fail-closed operator rule, no reserved nick rejection. `python -m chimericmesh` and `create_app()` without a hub use `True`. `create_app` stores the flag on the hub (`hub.enforce_access`, also a `Hub` constructor keyword, default `False`) so the IRC command layer can read it. New tests for B4, B5, B11 set it to `True`. | services | R8.4 |
| B13 | Default location of the database and `admin.token` is `~/.chimericmesh/` (`CHIMERICMESH_HOME`), not `data/` in the working directory. Reason: the token must not sit in the workspace the agent edits. | store, services | none (new) |
| B14 | **Reopen (confirmed by the user, 2026-11-18).** A resolved incident reopens when a new matching event (same host and signature) arrives at least `CHIMERICMESH_REOPEN_AFTER_SECONDS` (default 300) after `resolved_at`. Reopening clears the resolved flag, increments `opening`, restarts the count at 1, **sets a persisted `opened` flag so the tracker treats the incident as open regardless of its count or severity** (`IncidentTracker.qualifies` and `get` honour `opened`; without it a severity 3 or 4 incident with count 1 would not qualify), resets `severity` to that of the reopening event (the baseline keeps the minimum over all events, so an old crit event would otherwise make a later incident permanently unapprovable), clears the announced flag, writes an audit row and announces `REOPENED <id>` on IRC and in the dashboard. Events inside the window are counted but do not reopen. Applies to the in-memory hub too (no existing test depends on a resolved incident staying resolved). | store, auto-approval | R2, R3.1 |
| B15 | Syslog over TCP listens by default on `127.0.0.1:5514` (`CHIMERICMESH_SYSLOG_TCP_PORT`, `0` disables). If the port is in use the hub logs a warning and starts without the TCP listener. | connectors | R1.1 |
| B16 | The guided workflow flags (`propose`, `decide`, `resolve`, ...) are defined **per run**: they are computed from state created since the hub process started, so a restart with persisted proposals does not mark `propose` as done. | store, dashboard | R9 |
| B18 | New read-only REST additions: optional `service_id` (and for incidents `status`) filters on `GET /api/incidents` and `GET /api/proposals`, applied before the limit (store); `GET /api/proposals/{id}/rule-defaults` (auto-approval). | store, auto-approval | R4, R7 |
| B17 | New IRC commands `!rules`, `!revoke`, `!pause`, `!withdraw`. The proposal announcement line for an auto-approved proposal is `AUTO-APPROVED <pid> by rule <rid> ...` and does not say "reply !approve". | auto-approval | R6, R7 |

| B19 | **Event ids.** Every event gets an integer `id` assigned at ingest by `Hub.ingest` from one in-process counter, strictly increasing. For a store-backed hub the counter is seeded at startup from `max(events.id)` and the id is the row id, so it is stable across restarts. For an in-memory `Hub()` it starts at 1 per process. `Event.to_dict` and `GET /api/events` return it. The id is assigned before the batched write, so streaming (`dashboard` R5) reads from the in-memory ring first and from the database only for ids older than the ring. | store, connectors | R5 |
| B20 | Other additive changes: `opening`, `resolved_at` and `service_id` on incidents; `store_ok` and `dropped_events` in `GET /api/stats`; the `!help` output lists `!rules`, `!revoke`, `!pause`, `!withdraw`. | store, auto-approval | R2, R6 |
| B21 | `/api/ingest` and `/api/demo/storm` are unchanged in input and output. Their events go through `submit` like all others, so they are tagged by the same rules as network events (`services` R4); a demo-storm event with no matching service is `unassigned`. | connectors | R1 |
| B22 | **Auto-fix cooldown (user decision, 2026-11-18).** WHEN an incident reopens within `CHIMERICMESH_AUTOFIX_COOLDOWN_SECONDS` (default 86400, 24 h) after a proposal that a rule auto-approved was completed, the new proposal is not auto-approved by any rule and needs the user's approval, because the previous fix evidently did not work. The hub audits it (`autofix_cooldown`) and announces it. | auto-approval | R7.2 |

**Existing tests that change (the complete list).** Exactly two existing tests are edited in V1, both as an intended consequence of an additive change; every other existing test passes unchanged. (1) `tests/unit/test_kiro_assets.py::test_power_docs_name_only_real_tools_and_cover_all`: gets an allow-list of parameter and field names (`agent-loop` R6.2). (2) `tests/unit/test_web_terminal.py::test_proposals_endpoint_fields`: its expected key set for `GET /api/proposals` items grows by the B7 proposal fields (`auto-approval` R9.7). Chosen over hiding the new fields behind a second endpoint, because agents and the dashboard need them in the one place. The specs say "no existing test other than these two".

Unchanged on purpose: incident detection (R2) except B14, IRC command set (R6) except B17, transcript (R8.1, R8.2), guided workflow (R9) except B16, the `examples/legacy_app` demo and its tests (R10), replay tools, and the `CHIMERICMESH_*` environment variables that exist today.

## Kiro feasibility findings (summary; details in `agent-loop`)

Researched on 2026-11-18 in the Kiro docs.

- Nothing in Kiro starts an agent from an external event on this machine. Hook triggers are IDE or session events only (https://kiro.dev/docs/hooks/types/). Watch mode therefore needs a running Kiro session and a long-polling MCP tool. (Kiro Web scheduling and Crew bots are cloud features and are excluded by D1.)
- Kiro CLI headless mode (`--no-interactive`) needs an API key on a paid plan, treats every `ask` as `deny`, and is not installed here (https://kiro.dev/docs/cli/headless/). Deferred.
- Path-scoped write permissions exist (`fs_write` with `match` globs, `deny > ask > allow`). Workspace rules live outside the repository, and the agent cannot write permission files (https://kiro.dev/docs/permissions/).
- `Pre Tool Use` hooks exist for the IDE and the CLI and can block a tool call (https://kiro.dev/docs/hooks/types/). A hook script that calls the read-only verify route and blocks writes when the proposal is not approved is the optional extra enforcement layer (`agent-loop` F8b, R5.9).
- Kiro CLI 3.4.0+ also has a `PermissionRequest` hook (CLI only). Not used.
- The docs do not say whether non-spec markdown files inside `.kiro/specs/` are tolerated. Decision: keep this map in `docs/` so Kiro's spec list is not affected.

## Review gate

Per the spec-driven workflow, nothing beyond requirements is written until the user approves this map and the seven requirements files.
