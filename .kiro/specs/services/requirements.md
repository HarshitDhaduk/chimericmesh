# Requirements: services (V1)

Capability map: [docs/v1-capability-map.md](../../../docs/v1-capability-map.md) (module `services`, depends on: `store` only (plus the shared `core` types), effort 1.75 days). Higher modules reach `services` through its public functions and its `subscribe` interface (section 10). `services` imports none of them. Store use with and without a file database follows `store` R1.4 and map B1: for an injected in-memory hub the registry still lives in the private `:memory:` store.

## Baseline

[legacy-mesh](../legacy-mesh/requirements.md) R1 (ingestion), R2 (incidents), R5 (dashboard), R6.3 (operators), R8.4 (same-origin). Extends R6.3 and R8.4 with B4, B5, B11, B12 and B13 (see the map).

## Introduction

The hub today knows hosts, not applications. A developer who runs a web server, an FTP daemon and an old in-house service sees one mixed stream. The `services` module adds a registry of services so every event, incident and proposal belongs to a service, and one place shows each service's status. It also owns the local access control that protects configuration: the admin token and the Host-header check. Reason it lives here: this is the first module that lets someone widen what the hub watches, and that must not be possible for the agent.

## Objective

A developer registers each application once (name, how to recognize its events, where its logs are, where its code is). From then on every event is tagged with the service, and a single list shows each service's last seen time, error rate and open incidents.

Threat model: everything on the machine runs as the same user, so "local" is not "trusted". Two parties matter. A web page in the user's browser, or another local process without the token file, must not be able to change the hub's configuration (DNS rebinding, cross-site requests, local scripts). The admin token covers that. The Kiro agent runs as the same OS user, so it **can** read the token file and call the REST API. The token therefore does not stop the agent. What stops it from widening its own access is that no MCP tool exposes a privileged operation, Kiro's permission prompt on shell commands and on writes outside the approved scope, and the deny rules in the agent profile (`agent-loop` R5). Residual risk: a Kiro session where the user has allowed shell commands without a prompt lets the agent call the REST API with the token. Log text and service names are untrusted input.

## Requirements

### 1. Service registry
**User story:** As a developer, I want to register my applications as services, so that I can see and fix each one separately.

1. THE SYSTEM SHALL store a service with: `id` (slug), `name`, `hosts` (list), `app_names` (list), `protocols` (list, descriptive labels), `log_sources` (list of file sources, section 3), `code_folder` (optional), `created_at`, `updated_at`. [Auto]
2. THE SYSTEM SHALL require `id` to match `^[a-z0-9][a-z0-9-]{0,39}$` and be unique and not equal the reserved id `unassigned` (the id of the built-in pseudo service, R5.5). IF it does not match, is reserved or already exists, THEN THE SYSTEM SHALL return REST 422 or 409 and change nothing. [Auto] [PBT]
3. THE SYSTEM SHALL cap: `name` 80 characters, each list at 20 entries, each host or app name at 100 characters, and the registry at 100 services. IF a limit is exceeded, THEN THE SYSTEM SHALL return 422 with the field name. [Auto]
4. THE SYSTEM SHALL accept `protocols` values only from `syslog-udp`, `syslog-tcp`, `telnet`, `irc`, `file`. [Auto]
5. WHEN a service is created, changed or deleted, THE SYSTEM SHALL write an audit row with the actor, the service id and the changed field names (not file contents). [Auto]
6. WHEN a service is deleted, THE SYSTEM SHALL keep its events, incidents and proposals with the deleted service id and notify subscribers with `on_service_deleted` (section 10). Subscribers that only touch the database (`auto-approval` suspends the rules) run inside the same transaction; subscribers with outside effects (`connectors` stops the tailers) run in `after_commit` callbacks (section 10). `GET /api/services/{id}` SHALL then return 404, and the events stay readable through `GET /api/events` and the incident and proposal endpoints. (The stream endpoint belongs to `dashboard`, which also answers 404 for a deleted service.) [Auto]
7. THE SYSTEM SHALL persist services in the store (table added through the migration registry) and reload them on restart. WHEN the app is built around an injected in-memory `Hub()` (every existing test), the registry lives in the private `:memory:` store of `store` R1.4, with identical behaviour and no file. [Auto]

### 2. Code folder
**User story:** As a developer, I want to say where a service's code is, so that the agent looks and writes only there.

1. WHEN a code folder is set, THE SYSTEM SHALL require an absolute path to an existing directory, normalize it (resolve `..` and symlinks), and store the resolved path. [Auto] [PBT]
2. IF the path is a filesystem root, the user's home directory itself, or the hub's own data directory, THEN THE SYSTEM SHALL reject it with 422 (too broad for an auto-approved write scope). [Auto]
3. THE SYSTEM SHALL provide a pure function `path_in_code_folder(code_folder, candidate)` that returns true only when the resolved candidate is inside the resolved folder, and false for `..` escapes, absolute paths elsewhere, symlinks that leave the folder, and case or separator tricks on Windows. [Auto] [PBT]
4. THE SYSTEM SHALL treat a service without a code folder as having no auto-approvable scope (used by `auto-approval`). [Auto]
5. WHEN the code folder of a service is changed or removed, THE SYSTEM SHALL call the `on_code_folder_changed` subscribers (section 10) in the same way as other service changes (section 10): database-only subscribers inside the transaction, outside effects after commit. `auto-approval` subscribes and suspends the service's rules (`auto-approval` R6.9). A rule is bound to the code folder it was created for. IF an in-transaction subscriber raises, THEN the whole change SHALL be rolled back and the caller gets 500 with the subscriber's name. [Auto]

### 3. Log sources
**User story:** As a developer, I want to attach log files to a service, optionally choosing the format, so that its logs reach the hub.

1. THE SYSTEM SHALL store a log source as: `id`, `path`, `format`, `enabled`, `origin` (`user` or `discovery`) and an optional `host` (up to 100 characters, used by `connectors` as the event host for file sources). The only kind of log source in V1 is a file. Network listeners are global and match services through `hosts` and `app_names` (section 4). `format` is validated against the set of names passed to the registry when the app is built (`known_formats`, injected by the application wiring from the `connectors` registry). `services` does not import `connectors`. WHEN no set is injected (unit tests), THE SYSTEM SHALL accept `syslog-text`, `ncsa`, `w3c`, `xferlog`, `plain`. IF the format is not in the set, THEN 422. [Auto]
2. WHEN a file log source is added, THE SYSTEM SHALL require an absolute path without NUL characters, and SHALL NOT read the file at registration time beyond checking that it exists and is a regular file or reporting `missing`. [Auto]
3. THE SYSTEM SHALL allow a log source whose file does not exist yet (status `missing`), because many apps create logs on first use. [Auto]
4. THE SYSTEM SHALL reject a path that is a directory, device, or named pipe with 422. [Auto]
5. THE SYSTEM SHALL cap log sources at 20 per service and 200 overall. [Auto]
6. THE SYSTEM SHALL NOT expose a service's file contents through any `services` endpoint. Events are exposed only after `connectors` has parsed and stored them. [Auto]

### 4. Event-to-service tagging
**User story:** As a developer, I want events from my apps to land on the right service automatically, so that I do not tag them by hand.

1. THE SYSTEM SHALL provide a pure function `resolve_service(registry, source, host, app, log_source_id)` that returns a service id or none. [Auto] [PBT]
2. Network listeners are global: their events carry no `log_source_id` and are matched by `app` or `host` below. WHEN an event carries a `log_source_id` (file sources only), THE SYSTEM SHALL assign that source's service. OTHERWISE, WHEN the event's `app` equals one of a service's `app_names` (case-insensitive), THE SYSTEM SHALL assign that service. OTHERWISE, WHEN the event's `host` equals one of a service's `hosts` (case-insensitive), THE SYSTEM SHALL assign that service. OTHERWISE none. [Auto] [PBT]
3. IF an event matches several services at the same level, THEN THE SYSTEM SHALL choose the service created first, deterministically. [Auto] [PBT]
4. WHEN an event has no service, THE SYSTEM SHALL store it with a null `service_id` and show it under the built-in pseudo service with the reserved id `unassigned` and the name "Unassigned" (R5.5). Other modules use this id. Existing demo traffic continues to work unchanged. [Auto]
5. THE SYSTEM SHALL tag an incident with the service of its first event. IF later events of the same incident map to another service, THEN THE SYSTEM SHALL keep the first and SHALL NOT split the incident. Because file events default their host to `<machine>/<service-id>` (`connectors` R4.10), two file-based services on one machine get different incidents even for the same signature. Remaining consequence, accepted for V1: two services whose events arrive over a network listener with the same `host` and the same signature still merge into one incident owned by the service of its first event (Open Question 4). [Auto]
6. WHEN a service is created or its `hosts` or `app_names` change, THE SYSTEM SHALL apply the new matching to events received afterwards. It SHALL NOT rewrite stored events. [Auto]
7. THE SYSTEM SHALL resolve a service in under 1 ms for a registry of 100 services (cached lookup tables rebuilt on registry change). [Auto: micro benchmark with tolerant threshold]

### 5. Service status
**User story:** As a developer, I want one list that shows which services are healthy, so that I see problems without reading logs.

1. WHEN `GET /api/services` is called, THE SYSTEM SHALL return each service with: `last_seen` (timestamp of its newest event or null), `state`, `error_rate`, `open_incidents`, `events_total`, and per log source `status` and counters. The per-source runtime fields (`status`, `lines_read`, `lines_dropped`, `last_error`) come from a **status provider** function that the application wiring injects (`registry.set_status_provider(fn)`, `connectors` supplies it). WHEN no provider is injected, THE SYSTEM SHALL return `status: "unknown"` and no counters. [Auto]
2. THE SYSTEM SHALL compute `state` as `active` when the newest event is at most 5 minutes old, `idle` when older, and `never_seen` when there is none. [Auto] [PBT]
3. THE SYSTEM SHALL compute `error_rate` as the number of events with severity 3 or lower in the last 5 minutes divided by 5 (events per minute, one decimal). [Auto] [PBT]
4. THE SYSTEM SHALL compute `open_incidents` as incidents of that service that qualify as open and are not resolved. [Auto]
5. THE SYSTEM SHALL include the pseudo service with id `unassigned` (reserved, R1.2) and name "Unassigned" in the list with the same fields, flagged `builtin: true`, answer `GET /api/services/unassigned` with it, and SHALL NOT allow editing or deleting it. [Auto]
6. `GET /api/services/{id}` SHALL return 404 for an unknown id. [Auto]
7. THE SYSTEM SHALL compute status from the store and in-memory counters without scanning the whole events table (target: under 50 ms for 100 services and 100,000 stored events). [Auto: benchmark]

### 6. Admin token and privileged operations (B4)
**User story:** As a developer, I want configuration changes to require something the agent does not have, so that the agent cannot widen its own permissions.

1. WHEN the hub starts with access enforcement on (B12) and no token exists, THE SYSTEM SHALL generate a random token of at least 32 bytes (URL-safe) and write it to the file named by `CHIMERICMESH_TOKEN_FILE`, or `admin.token` in `CHIMERICMESH_HOME` (default `~/.chimericmesh/`) when unset, with the same owner-only permissions as `store` R1.5, using the helper in `chimericmesh/fsperms.py` (this module starts no process itself). WHEN `CHIMERICMESH_DB` is `:memory:`, THE SYSTEM SHALL keep the token in process memory only and write no file (tests read it from the app object). THE SYSTEM SHALL NOT default the token file to a path under the current working directory, and SHALL log a warning when a configured token file is under the working directory. [Auto: two working directories, one token file]
2. THE SYSTEM SHALL print the token file path on startup and SHALL NOT print the token value in logs, IRC, webhooks, the audit log or any API response. [Auto: canary test]
3. THE SYSTEM SHALL provide one reusable check `require_admin` (a FastAPI dependency) and apply it to its own privileged operations: create, edit or delete a service; add, edit or remove a log source; set or change a code folder; `POST /api/proposals/{id}/decide`; and `!approve` or `!deny` sent through `POST /api/terminal`. The other modules apply the same check to theirs (`discovery`: scan, accept, dismiss; `auto-approval`: create, edit or widen, re-enable and resume rules, create a rule from a proposal). Pause, revoke and withdraw are not privileged operations (D3, R6.5). [Auto]
4. IF the header is missing or wrong, THEN THE SYSTEM SHALL return REST 401, change nothing, and write one audit row (`auth_failed`, operation name, no token material). [Auto]
5. THE SYSTEM SHALL NOT require the token header for read-only endpoints. **User decision (D3, confirmed 2026-11-18):** pause (global `POST /api/rules/pause`, `!pause`, and per rule), revoke (`POST /api/rules/{id}/revoke`, `!revoke`) and withdraw (`POST /api/proposals/{id}/withdraw`, `!withdraw`) only reduce what the agent may do, so THE SYSTEM SHALL accept them with no admin token and no `PASS` on every channel (REST, the dashboard terminal, IRC; on IRC any registered nick, operator status not required), and they SHALL always take effect, even when the caller is over the rate limit. Create, edit or widen, re-enable and resume of rules need the token (R6.3). THE SYSTEM SHALL provide a shared helper `reduce_audit_limiter` (constant `REDUCE_OPS_PER_MINUTE = 10` per caller key) that limits only the **volume of audit rows and announcements**, never the action: the first 10 accepted calls per key per minute each get their own audit row and announcement, further calls in that minute still execute and are summed into one coalesced audit row `reduce_ops_suppressed` with the count (no extra announcement). The caller key is the client address for REST, the nick for IRC, and `dashboard-terminal:<client address>` for commands typed in `POST /api/terminal`. [Auto] [PBT]
5a. THE SYSTEM SHALL state in the README that pause, revoke and withdraw are open to any local caller (including the agent, from a shell), that the cost is a local process being able to stop auto-approvals or withdraw proposals (annoyance, not escalation), and that resume and every other rule change need the user. [Auto: README text test]
6. THE SYSTEM SHALL NOT expose any MCP tool that sends or reads the admin token, and SHALL NOT include it in any MCP tool result. [Auto: tool list and canary test]
7. WHEN more than 10 requests with a wrong token arrive from one client address in one minute, THE SYSTEM SHALL answer 429 for 60 seconds to wrong-token requests from that address only. A request with the correct token SHALL always be processed, so another local process cannot lock the user out. [Auto]
8. THE SYSTEM SHALL provide `python -m chimericmesh token --rotate` that replaces the token file and prints only its path. [Auto]
9. WHILE access enforcement is on, THE SYSTEM SHALL authorise `!approve` and `!deny` sent through `POST /api/terminal` by a valid `X-Admin-Token` alone. The terminal acts as nick `dashboard` and SHALL NOT depend on `CHIMERICMESH_OPERATORS` (set or unset). The fail-closed operator rule of R9.2 applies to the IRC path only. WHILE enforcement is off (all existing tests), the baseline applies (`hub.operators` including `None` as today). [Auto: enforcement on, operators unset, terminal with the token approves; without the token it returns 401; an IRC connection in the same configuration is refused]
10. THE SYSTEM SHALL keep existing REST endpoints that mutate non-privileged state (`/api/ingest`, `/api/incidents/{id}/resolve`, `/api/proposals` create and complete, `/api/irc/say`, `/api/demo/storm`) working without the token. [Auto]

### 7. Host-header check and same-origin (B5)
**User story:** As a developer, I want web pages on other sites to be unable to read my logs through my browser.

1. THE SYSTEM SHALL keep R8.4: state-changing requests whose `Origin` differs from `Host` get 403, and requests without `Origin` pass the origin check. [Auto]
2. WHILE enforcement is on AND a request's `Host` header host part is not in the allow list (`CHIMERICMESH_ALLOWED_HOSTS`, default `127.0.0.1,localhost,[::1]`), THE SYSTEM SHALL answer 403 for every `/api/*` path. [Auto] [PBT]
3. THE SYSTEM SHALL ignore the port in the Host check and SHALL compare hosts case-insensitively. [Auto] [PBT]
4. WHEN the hub is started with `CHIMERICMESH_BIND` set to a non-loopback address, THE SYSTEM SHALL log a warning at startup that logs and approvals are reachable from the network. [Auto]
5. THE SYSTEM SHALL add the parameter `enforce_access: bool | None = None` to `create_app`. WHEN `create_app` receives an injected hub (as all existing tests do, with hosts such as `t`, `test` and `testserver`), `None` SHALL mean `False`: no Host check, no admin token, no IRC PASS requirement, no reserved-nick rejection and no fail-closed operator rule (B12). `python -m chimericmesh` and `create_app()` without a hub SHALL pass `True`. `create_app` SHALL store the effective value as `hub.enforce_access` (also a `Hub` constructor keyword, default `False`), and the IRC command layer SHALL read that attribute. New tests for B4, B5 and B11 SHALL create the app with `enforce_access=True`. [Auto]
6. THE SYSTEM SHALL default to binding `127.0.0.1` for HTTP, Syslog, Telnet and IRC, as today. [Auto]

### 8. Services over MCP and REST (read side)
**User story:** As an agent, I want to list services and read one, so that I can work per service.

1. THE SYSTEM SHALL expose `GET /api/services` and `GET /api/services/{id}` as described in section 5. The MCP tools that wrap them belong to `agent-loop`. [Auto]
2. THE SYSTEM SHALL return `code_folder` in service reads, because the agent needs the scope. It SHALL NOT return the admin token or any file contents. [Auto]
3. THE SYSTEM SHALL treat service names, host names and app names as untrusted text: control characters are stripped on input and the dashboard renders them with `textContent` only. [Auto] [PBT]

### 9. IRC operator hardening (B11)
**User story:** As a developer, I want a local process to be unable to approve fixes by pretending to be me or the dashboard, so that the approval step really belongs to the user (D3).

1. THE SYSTEM SHALL reject IRC registration with the nicks `dashboard` and `ChimericBot` (case-insensitive) using numeric 432 or 433, and SHALL apply the same check to every later `NICK` command of a registered connection (a registered client cannot switch to a reserved nick; the reply is the same numeric and the old nick is kept). The web terminal acts as `dashboard` internally and is protected by the admin token (R6.9). This section applies only WHILE access enforcement is on (B12). [Auto]
2. IF access enforcement is on AND `CHIMERICMESH_OPERATORS` is unset or empty, THEN THE SYSTEM SHALL answer `!approve`, `!deny` and `!resolve` from IRC with a refusal that names the variable, and SHALL change nothing. This replaces the baseline behaviour that allows any nick when the list is unset, but only with enforcement on. WHILE enforcement is off (every existing test, including those that set `hub.operators = None` and expect `!approve` to work), the baseline applies unchanged. [Auto]
3. WHILE access enforcement is on, THE SYSTEM SHALL accept `!approve`, `!deny` and `!resolve` from an IRC connection only when its nick is in `CHIMERICMESH_OPERATORS` AND the connection registered with `PASS <admin token>`. `!pause`, `!revoke` and `!withdraw` need neither an operator nick nor `PASS`: any registered nick may use them (R6.5). IRC `!resolve` needs the `PASS` while REST `POST /api/incidents/{id}/resolve` and the terminal `!resolve` stay token-free (R6.10, baseline); this asymmetry is intentional. Other nicks keep read-only commands. [Auto]
4. THE SYSTEM SHALL compare the `PASS` value in constant time, SHALL NOT echo it, and SHALL NOT include it in logs, the transcript or the audit log. [Auto: canary test]
5. WHILE enforcement is on, THE SYSTEM SHALL NOT let an IRC connection that registered as an operator nick without `PASS`, or as `dashboard`, approve a pending proposal. [Auto: two extra connections try and the proposal stays pending]
6. THE SYSTEM SHALL document in the README that an IRC client of the user must set the server password to the admin token to use operator commands. [Auto: README text test]

### 10. Change notification and injection (keeps the dependency arrows one-way)
**User story:** As the maintainer of a higher module, I want to react to service changes without the registry knowing me, so that `services` stays below `connectors`, `auto-approval` and `dashboard`.

1. THE SYSTEM SHALL provide `registry.subscribe(on_service_changed=None, on_code_folder_changed=None, on_service_deleted=None, after_commit=None)`. The three `on_*` arguments are optional callables that receive the open store transaction and the service (id, old and new values for the code folder). They SHALL do database work only. `after_commit` is an optional callable that receives the same event description (kind, service id, old and new values) and is called once, after the transaction committed. Starting or stopping tailers and any other outside effect belongs in `after_commit`. [Auto]
2. WHEN a service or its log sources are created, edited or deleted, THE SYSTEM SHALL call the matching `on_*` subscribers synchronously, in registration order, inside the same store transaction as the change (registry, rule and audit writes are synchronous and awaited; only event rows use the store's asynchronous writer, `store` R7.1). IF an `on_*` subscriber raises, THEN THE SYSTEM SHALL roll the transaction back, leave the registry unchanged, NOT call any `after_commit` callback, and return an error that names the subscriber. WHEN the transaction has committed, THE SYSTEM SHALL call each `after_commit` callback; IF one raises, THE SYSTEM SHALL log it, keep the committed change, and continue with the next callback (`connectors` reconciles its tailers from the registry at startup and on a periodic check, `connectors` R1.6). [Auto: a failing second `on_*` subscriber leaves no tailer started and no row changed] [PBT]
3. THE SYSTEM SHALL receive the set of valid formats (`known_formats`) and the per-source status provider (`set_status_provider`) from the application wiring, and SHALL NOT import `connectors` to get them. [Auto]
4. THE SYSTEM SHALL keep the module dependency arrows one-way, checked by one table-driven static import test (defined in the capability map, "Dependency direction") that fails when a `services` file imports anything but `core` and `store`. [Auto]
5. THE SYSTEM SHALL NOT call subscribers for changes that only touch `updated_at` or an unchanged list. [Auto]

## Boundaries

- Always: validate and cap every input; resolve and normalize paths before comparing them; write an audit row for each privileged change; keep tagging a pure function.
- Ask first: adding a second credential type (per-user logins); changing which operations are privileged; letting `hosts` take wildcards or CIDR ranges.
- Never: read log file contents in this module; let any MCP tool change a service, log source or code folder; log or return the admin token; accept a relative path or a path with `..` as a code folder; rewrite stored events when matching rules change.

## Success Criteria

1. Register a service `proxy-worker` with `app_names: ["proxy-worker"]` and a code folder. Send the demo syslog traffic: events and the incident carry `service_id: proxy-worker`, and `GET /api/services` shows `state: active`, a non-zero `error_rate` and `open_incidents: 1`.
2. `POST /api/services` without the token returns 401 and creates nothing. With the token it returns 201.
3. A request with `Host: evil.example` to `/api/stats` returns 403.
4. `path_in_code_folder` returns false for 1000 generated escape attempts (`..`, symlinks, absolute paths, mixed separators) and true for generated children.
5. Property tests (design phase) cover: slug validation, tagging determinism and precedence, `state` and `error_rate` formulas, path containment, Host allow-list.
6. All existing tests pass unchanged by this module (the two tests changed in V1 are listed in the map, "Existing tests that change", and belong to `agent-loop` R6.2 and `auto-approval` R9.7), because they inject a hub and therefore run with `enforce_access=False` (B12). The new tests for the Host check, the token and IRC hardening run with `enforce_access=True`.
7. A second IRC client that registers as `dashboard`, or as an operator nick without `PASS`, cannot approve a proposal.
8. With the working directory changed, the hub still finds the same token file under `~/.chimericmesh/`.
9. With enforcement on and `CHIMERICMESH_OPERATORS` unset, the dashboard terminal with a valid token approves a proposal, the same terminal without the token gets 401, and an IRC client is refused.
10. The dependency-direction test (R10.4, capability map) passes: `services` imports only `core` and `store`.
11. `POST /api/rules/pause`, `POST /api/rules/r1/revoke` and `POST /api/proposals/p1/withdraw` without the token succeed. Eleven pause calls in one minute from one address all take effect: ten have their own audit row and one coalesced `reduce_ops_suppressed` row records the eleventh. `POST /api/rules`, a rule edit, re-enable and resume without the token return 401.

## Out of Scope (Deferred)

- Multiple users, logins, roles: D1 (single developer, local).
- Wildcard or CIDR host matching: simple equality covers the demo; revisit with real fleets.
- Auto-creating services from unknown hosts: surprising for the user; discovery suggests, the user confirms.
- Service groups, tags, ownership: not needed to review and fix a bug.
- Token stored in an OS keychain: needs a dependency; a `0600` file is enough for V1.
- Per-service API keys for log shippers: ingestion stays unauthenticated on localhost, as in the baseline.

## Open Questions

1. Resolved: the dashboard terminal needs the admin token for `!approve`, pasted once per browser session (accepted 2026-11-18).
2. Resolved: an IRC client must be configured with the admin token as server password to use `!approve` (accepted 2026-11-18).
3. Resolved: an unassigned incident may receive proposals as today, but it can never be auto-approved (accepted 2026-11-18).
4. Resolved: two services behind one sender host that log the same signature merge into one incident owned by the first service; acceptable for V1, since the demo has one service per sending host (accepted 2026-11-18).

## Notes for design

Findings from the final review that design must resolve. No acceptance criterion changes for them.

- M2: `services` cannot import `hub`, but R5.1 and R5.7 need `last_seen`, `error_rate`, `events_total` and `open_incidents` from the event ring and incidents. Name the injected read interfaces in the map and receive them at construction; run the module against a plain `Hub()` and against the store-backed hub.
- M3: the subscriber callbacks of R10.2 run inside the store's write transaction; the design states which thread runs them and that they may not await or take the hub lock (see `store` notes).
- L8: split compound criteria into sub-numbered criteria, starting with the first sentences of R4.2.
