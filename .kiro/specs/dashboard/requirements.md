# Requirements: dashboard (V1)

Capability map: [docs/v1-capability-map.md](../../../docs/v1-capability-map.md) (module `dashboard`, depends on: `store`, `services`, `connectors`, `discovery`, `auto-approval`, `agent-loop`; effort 2.0 days; no cuts are applied, contingency items 6 and 7 in the map would save 0.5 and 0.1). The dashboard owns the SSE stream route (section 5). It calls the other modules' REST routes and SSE routes and imports none of their code (the map's import table lets it import only core; the dependency on the other modules is runtime, through the routes that wiring registers).

## Baseline

[legacy-mesh](../legacy-mesh/requirements.md) R5 (dashboard, text only), R8 (terminal and transcript), R9 (guided workflow). All of R5, R8 and R9 keep working. The guided-workflow flags are defined per run (B16, `store` R4.8).

## Introduction

The dashboard today shows stats, incidents, a live log and a terminal for one mixed stream. V1 adds the views that make the product usable day to day: all services at a glance, a confirm screen for discovery suggestions, a live log per service, incidents and proposals per service, and rule management with the audit trail. It stays one static `index.html` with no build step. Log text and service names are untrusted and are shown with `textContent` only.

## Objective

One local page where a developer sees every connected service and its state, watches its logs live, reviews what the agent proposes, approves or sets standing rules, and sees who decided what.

Threat model: the page shows logs that may contain hostile text (script tags, escape sequences, very long lines). The page holds the admin token only in `sessionStorage` after the user pastes it, and never in the URL, in logs or in the DOM text. Any script that runs in the page origin can read `sessionStorage`, and V1 defers a Content-Security-Policy (see Out of Scope). The token is therefore only as safe as the no-`innerHTML` rule (R1.2) and the absence of third-party scripts (R1.1); this is the stated residual risk. Another site must not be able to drive the page (same-origin and Host checks, `services` R7).

## Requirements

### 1. Page structure and safety
**User story:** As a developer, I want the page to be safe to open while looking at hostile logs.

1. THE SYSTEM SHALL keep a single `src/chimericmesh/web/index.html` served at `/` with no build step and no third-party scripts, fonts or images loaded from other origins. [Auto: static check]
2. THE SYSTEM SHALL write every log-derived or user-entered string (messages, samples, service and host names, paths, rule notes, process names, proposal text) into the DOM with `textContent` or `setAttribute` of non-URL attributes, and SHALL NOT use `innerHTML`, `outerHTML`, `insertAdjacentHTML` or `document.write` with such strings. [Auto: extends the existing `guard-innerhtml` hook and a test that fails on those calls] [Manual: open the page against hostile log text, SC2]
3. THE SYSTEM SHALL send `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer` on `/`. [Auto]
4. THE SYSTEM SHALL cap rendered log rows at 500 per view and each message at 500 characters (CSS ellipsis plus a "show more" that reveals up to 4096 characters, still as text). [Manual: browser check]
5. THE SYSTEM SHALL keep the existing views (stats tiles, incident cards, live log, terminal, guided workflow with its six steps) reachable and working. [Auto: existing tests] [Manual: screenshot comparison with `docs/screenshots/`]

### 2. Admin token entry
**User story:** As a developer, I want to enter my admin token once, so that I can approve and configure without it being stored anywhere unsafe.

1. THE SYSTEM SHALL show a "Unlock" control that asks the user to paste the admin token (the path of `admin.token` is shown by the hub at startup and on the page). [Manual]
2. THE SYSTEM SHALL keep the token only in `sessionStorage` and send it as the `X-Admin-Token` header on privileged requests, never in a URL or query string. [Manual: browser check]
3. WHEN the hub answers 401 to a privileged request, THE SYSTEM SHALL clear the stored token and show "token rejected" in the status area. [Auto]
4. WHILE no token is stored, THE SYSTEM SHALL show configuration controls (add service, accept suggestion, create rule, approve in terminal) as disabled with the reason "unlock first", and SHALL still show all read-only data. Pause all, per-rule Pause, Revoke and Withdraw need no token (D3, `services` R6.5) and stay enabled while locked. Create, edit, re-enable and resume stay disabled while locked. [Auto]
5. THE SYSTEM SHALL NOT display the token after entry (password-type field, cleared after save). [Manual]

### 3. Services overview
**User story:** As a developer, I want one list of all my services with their status, so that I see what is wrong at a glance.

1. THE SYSTEM SHALL show a card per service from `GET /api/services` with name, `state` (`active`, `idle`, `never_seen`), last seen (relative time), `error_rate` per minute, open incident count, and each log source's status (`running`, `missing`, `unreadable`, `error`) and `lines_dropped`. [Manual: browser check]
2. THE SYSTEM SHALL show the pseudo service with the reserved id `unassigned` and the name "Unassigned" (`services` R5.5) as a card with the same fields, flagged built-in. [Auto]
3. THE SYSTEM SHALL refresh the list every 5 seconds while the tab is visible and pause while hidden. [Manual: browser check]
4. WHEN a service has open incidents, THE SYSTEM SHALL sort it above services without, then by error rate. [Manual: browser check]
5. THE SYSTEM SHALL provide a form to add and edit a service (id, name, hosts, app names, log source paths with a format selector, code folder) that calls the `services` endpoints with the token and shows the server's validation message verbatim as text. [Manual: browser check]
6. THE SYSTEM SHALL show an empty state with the text "No services yet. Run discovery or add one." and the two buttons. [Auto]
7. THE SYSTEM SHALL show, per service, a "Kiro permissions" panel with the text from `GET /api/services/{id}/permissions-snippet` (including the prompt mode and autonomous mode text of `agent-loop` R5.8) and a copy button, and the note that the hub never writes that file. [Auto] [Manual]

### 4. Discovery review and confirm
**User story:** As a developer, I want to review what discovery found and confirm what to watch.

1. THE SYSTEM SHALL provide a "Discover local apps" button that explains in one sentence what the scan does and does not do (lists processes and listening ports, never reads log contents, nothing leaves the machine) before it calls `POST /api/discovery/scan`. [Manual] [Auto: text present]
2. WHILE a scan runs, THE SYSTEM SHALL poll `GET /api/discovery/scans/{id}` every second and show progress. WHEN `limited` is true, THE SYSTEM SHALL show "some processes were not visible without admin rights". [Auto]
3. THE SYSTEM SHALL show each suggestion with name, confidence, evidence (process name, pid, ports) and a checklist of candidate log paths with format selectors and `exists` markers, all editable, plus an optional code folder field. [Manual: browser check]
4. WHEN the user accepts, THE SYSTEM SHALL send only the paths that are checked, with the format chosen, and show the created service. WHEN the user dismisses, THE SYSTEM SHALL call the dismiss endpoint and hide the suggestion. [Auto]
5. THE SYSTEM SHALL NOT start any scan on page load. [Auto]

### 5. Per-service live log stream (SSE)
**User story:** As a developer, I want to watch one service's logs as they arrive.

1. THE SYSTEM SHALL add `GET /api/services/{id}/stream` (also `unassigned`) that responds with `text/event-stream`, sends events as `id: <event id>` (the integer event id of map B19, `store` R6.1) and `data: <JSON event>` lines, sends a comment heartbeat every 15 seconds, and supports `Last-Event-ID` or `?after=` to resume: ids still in the in-memory ring are served from memory, older ones from the store (an in-memory `Hub()` has only the ring). WHEN the service id is unknown or the service was deleted, THE SYSTEM SHALL answer 404 (`services` R1.6). [Auto]
2. THE SYSTEM SHALL serialize each event as one JSON line with no raw newlines in the data field (JSON escapes). A message containing `\n` or `data:` SHALL NOT be able to forge another SSE field. [Auto] [PBT]
3. THE SYSTEM SHALL allow at most 20 concurrent streams and answer extra ones with 429. THE SYSTEM SHALL release a stream's resources when the client disconnects within 5 seconds. [Auto]
4. THE SYSTEM SHALL deliver an event within 1 second of ingestion under normal load and, under overload, drop the oldest queued events for that stream (queue of 1000) and send a `event: dropped` message with the count. [Auto]
5. THE SYSTEM SHALL support an optional `min_severity` filter (0 to 7). [Auto] [PBT]
6. THE SYSTEM SHALL show the stream in the service detail view with pause and resume and an auto-scroll toggle, and a severity filter and a text filter (client-side, applied to `textContent`). WHEN the browser connection drops, THE SYSTEM SHALL reconnect with backoff from 1 to 30 seconds and resume from the last id. [Manual: browser check, drop the connection]
7. THE SYSTEM SHALL keep serving `GET /api/events` unchanged for the existing global log view. [Auto]

### 6. Incidents and proposals per service
**User story:** As a developer, I want to see each service's incidents and what the agent proposed, and decide on them.

1. THE SYSTEM SHALL show, in the service detail view, the service's incidents (from `GET /api/incidents?service_id=<id>`, `store` R4.4a) and its proposals (from `GET /api/proposals?service_id=<id>`, `store` R4.4, filtered by the hub before the limit, never client-side) (id, action, status, `files`, `verification`, who approved, linked incident), newest first. [Manual: browser check]
2. THE SYSTEM SHALL show for each proposal how it was approved: "approved by <nick>" or "auto-approved by rule <id>", and the rule's current status. [Auto]
3. WHEN a proposal is `pending`, THE SYSTEM SHALL show Approve and Deny buttons that call `POST /api/proposals/{id}/decide` with the token (B4), and show the result. [Auto] [Manual]
4. WHEN a proposal was approved by a user (`approval.by` is `user`) and is `approved`, or is `done` with `verification_result` `passed`, THE SYSTEM SHALL show an "Always approve fixes like this" button that opens the rule form pre-filled from `GET /api/proposals/{id}/rule-defaults` (read-only, creates nothing, `auto-approval` R3.2a), lets the user change limits within the bounds, and calls `POST /api/proposals/{id}/rule` with the token only on confirm. [Auto] [Manual]
5. THE SYSTEM SHALL keep the terminal (`POST /api/terminal`) and the transcript working. WHEN a `!approve` or `!deny` line is sent without a token, THE SYSTEM SHALL show the 401 as "unlock first". [Auto]
6. THE SYSTEM SHALL keep the guided workflow (R9) counting the same six steps and flags. [Auto]

### 7. Approval rules and audit trail
**User story:** As a developer, I want to see, revoke and pause my standing rules and read what they did.

1. THE SYSTEM SHALL list rules (id, service, match type and value, severity floor, expiry countdown, limits, use count in the last hour and in total, status, source proposal) from the rule read endpoint. [Auto]
2. THE SYSTEM SHALL show a Revoke button and a Pause button per active rule (Revoke also per suspended rule) and a global Pause all button. None of them needs the token (D3, `services` R6.5), so they work while the page is locked. WHEN clicked, THE SYSTEM SHALL ask for one confirmation and then call the endpoint. [Auto] [Manual]
3. THE SYSTEM SHALL show Re-enable (suspended rules), Resume (while paused), Edit and Create controls only when unlocked. [Auto]
4. THE SYSTEM SHALL show a rule's suspension reason when its status is `suspended`. [Auto]
5. THE SYSTEM SHALL show the audit log (`GET /api/audit`) as a table with time, actor, action, subject and detail, filterable by service and rule, newest first, paged by 100 rows with a "load more" action. [Auto]
6. THE SYSTEM SHALL show a banner at the top while rules are paused, and a distinct badge on every proposal that was auto-approved. [Auto]
7. THE SYSTEM SHALL refresh rules and the audit view every 5 seconds while visible. [Auto]

### 8. Accessibility and layout
**User story:** As a developer, I want the page to be usable without a mouse and in a narrow window.

1. THE SYSTEM SHALL make every button and control reachable by keyboard with a visible focus outline and an accessible name (`aria-label` where the text is an icon). [Manual] [Auto: static check for missing names]
2. THE SYSTEM SHALL not rely on colour alone for status: each state has a text label. [Manual]
3. THE SYSTEM SHALL render usably at 360 px width (no horizontal scroll for the services list). [Manual]

## Boundaries

- Always: use `textContent`; keep one HTML file; show server validation errors as text; ask for confirmation before revoke; keep unlocked-only controls disabled while locked.
- Ask first: adding a JavaScript or CSS dependency or a build step; adding a Content-Security-Policy that needs inline-script nonces; storing the token anywhere except `sessionStorage`.
- Never: render untrusted text as HTML; put the token in a URL; load resources from other origins; start a scan or an approval automatically on page load; expose rule creation without the token.

## Success Criteria

1. Opening the page against a hub with three services (one idle, one erroring, one never seen) shows three cards with the right states, sorted with the erroring one first.
2. A log line `<img src=x onerror=alert(1)>` and a service named `<script>x</script>` appear as literal text; no element is created from them (static test of the page plus a manual check).
3. A message containing `\ndata: {"forged":1}\n\n` arrives as one SSE message with the text intact.
4. Without the token, approve, add service, scan, rule creation, re-enable and resume are disabled, while Pause all, per-rule Pause, revoke and withdraw stay enabled.
5. Manual end to end (the demo, including the page-logic checks that have no automated test): discover or add `legacy_app`, run the storm, watch the live stream, see the agent's proposal appear with files, approve it, click "Always approve fixes like this", see the next proposal auto-approved with the badge and the audit row, revoke the rule.
6. All existing dashboard-related tests and the `guard-innerhtml` hook still pass.
7. The table-driven import test (capability map, "Dependency direction") passes: `dashboard` may import any lower module, and no module imports `dashboard`.

## Out of Scope (Deferred)

- A Content-Security-Policy with script nonces: the single-file page uses inline script; adding nonces is more work than the budget allows. Headers in 1.3 are the V1 floor.
- Charts and historical graphs: the digest numbers are enough; the story is fixing bugs, not metrics.
- Dark/light themes and localization: not required.
- Editing the Kiro permissions file from the page: the hub never writes it (`agent-loop` R5.5).
- Mobile layout beyond 360 px usability: V1 targets a desktop browser.
- Multi-user sessions and per-user tokens: D1.
- Log search over the whole history: the stream and the digest cover V1; full-text search needs an index in the store.
- Exporting the audit log: reading the table is enough for the demo.

## Open Questions

1. Resolved: the admin token is requested once per browser session and is not kept in `localStorage` (accepted 2026-11-18).
2. Resolved: the demo video shows the services overview, discovery confirm, live stream, proposal approval, rule from proposal and audit trail (accepted 2026-11-18).

## Notes for design

Findings from the final review that design must resolve. No acceptance criterion changes for them.

- M2: the SSE route (R5) needs the event ring and the store, but the dashboard imports only `core`. Name the injected event-feed interface in the map, register the route through wiring, and add one criterion that runs the stream against a plain `Hub()` and against the store-backed hub.
- L6: demo precondition for Success Criterion 5: the working tree's `examples/legacy_app/upstream.py` no longer has the defect. Restore it first (for example `git checkout -- examples/legacy_app/upstream.py`); the spec set does not decide this.
