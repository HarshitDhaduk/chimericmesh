# Dashboard usability audit and UI improvement plan

Status: DRAFT for approval. Nothing in this plan is implemented. Branch `feat/ux-usability`, base `e84d83e`.

Method: headless Edge at 1440x900 and 390x844 (plus 320x640 for overflow probes) against a seeded hub on alternate
ports. The tester played a first-time user with one goal: see what is wrong, get the agent's proposed fix approved,
confirm it worked. Every view was captured locked and unlocked (token typed into the Unlock field), plus a wrong-token
error, a busy overview, discovery after a scan, rules with the rule form, the terminal after commands, the approve
flow and "Always approve fixes like this". Scripted DOM probes measured overflow, tab order, control names and sizes.
Screenshots are in the main checkout under `.agents/tasks/ux-audit/shots/` (file names are quoted as evidence).
Probe scripts: `.agents/tasks/ux-audit/{run2,run3,probe}.py`. Criteria: the frontend-ui-engineering skill
(keyboard, focus, contrast, headings, aria-live, labels, empty/error/loading states, responsive).

Source files read: `.kiro/specs/dashboard/{requirements,design}.md`, `src/chimericmesh/web/index.html`
(2546 lines), `tests/unit/test_dashboard*.py` (255 dashboard tests pass on the base commit).

## 1. Summary

1. The page is solid on safety, focus visibility (3 px outline on every control), named controls (0 unnamed of 39),
   headings (one h1, h2 per panel) and text-labelled status pills. The problems are about flow, not polish.
2. The main journey (spot incident, approve fix, confirm) works but hides its key action: Approve sits at the bottom
   of Overview (about 76% down on desktop, about 80% down on mobile) and at the bottom of the service page
   under 18 log rows. A first-time user has no pointer to it.
3. Feedback is weak. Approve, Lay to rest and a rejected token report only in tiny header status text or a terminal
   line, which is off-screen after scrolling. "Did it work?" is never answered with a next step.
4. The lock model is invisible where it matters: locked Approve/Deny are dimmed with only a `title`, the bright
   Withdraw button next to them is the easiest wrong click, Save on the token form says "Unlocked" without checking.
5. Mobile has two real bugs: Overview scrolls sideways (439 px content in a 390 px viewport, 119 px over at 320) and the
   Rules and Audit tables are unreadable, with Revoke/Pause actions off-screen. The playful copy is good where it
   exists (empty states, "Lay to rest") and absent from errors, which are flat and do not say what to do.

## 2. Journeys today and friction points

### J1. First run, empty hub (shots `01-*`, `03-*`, `05-*`, `06-*`)
Open page: stats all zero, Getting started box with an orange "Do it", and a second orange "Summon a failure storm" in
the header. Two primary buttons do the same thing. The terminal is an empty black box. Services shows an empty card
beside the built-in Unassigned card at equal weight. Friction: UX-13, UX-10.

### J2. See what is wrong
Overview lists haunted incidents with Explain / Find in code / Propose a fix / Lay to rest. Incident cards show the
host and category but not the owning service and do not link to it (UX-09). The service page lists incidents with no
actions at all (UX-09).

### J3. Get the proposed fix approved (core goal)
1. A pending proposal exists. Nothing on the top of Overview says so (UX-01).
2. The user scrolls to Proposals (`#propPanel`) below four incident cards, or opens Services, Proxy worker, then scrolls
   past the live log (UX-02). From the keyboard that is 25+ tab stops (UX-20).
3. Locked: Approve/Deny are dim and disabled. The note explaining why (`#propNote`) exists only on Overview, not on the
   service page (UX-05). Withdraw is the one bright button.
4. Unlock: the form opens as a band that pushes the page down. A wrong token is accepted with "Unlocked" (UX-04).
5. Click Approve: the pill changes to "approved - waiting for agent". Feedback sits in the header status, which is
   scrolled away (UX-03).

### J4. Confirm it worked
The proposal turns "done" with "verification passed" in grey meta text. Nothing tells the user to lay the incident to
rest, and Lay to rest itself gives no on-page confirmation, the card just disappears (UX-17, UX-18).

### J5. Standing rules and audit
"Always approve fixes like this" opens a good pre-filled form inline (shot `82-*`). Rules page: vocabulary
(suspended / paused / Re-enable / Resume), raw actor strings and snake_case actions leak through (UX-15). On mobile the
tables break (UX-07).

### J6. Discovery
Locked: the scan button is dimmed with no reason (`05-discovery-initial-locked-d`). After a scan with no result:
"Scan finished: no apps found." and nothing else (UX-14).

## 3. Findings

Severity: blocker = a user cannot complete the goal; high = the goal is reachable but most users will miss or mistrust it;
medium = friction or inconsistency; low = polish. No blockers were found. "Req" lists the approved dashboard
requirement the change touches (R = `.kiro/specs/dashboard/requirements.md`).

| id | severity | where | problem | evidence | proposed change (exact elements) | req | how to verify |
|----|----------|-------|---------|----------|----------------------------------|-----|---------------|
| UX-01 | high | Overview, `#view-overview`, `#propPanel` | The pending proposal and its Approve button are below `#incPanel` (four incident cards). Nothing at the top says a decision is waiting. Approve is about 76% of the way down the page on desktop (full page 2892 px) and about 80% on mobile (5808 px). | `10-overview-busy-locked-d-full.png`, `14-overview-unlocked-m-full.png` | Add `<div id="decideStrip" role="status">` between `#stats` and `#guide`, filled by a new `renderDecideStrip()` called from `renderProposals()`. Text: "1 fix is waiting for you: <action>" plus a button "Review" that calls `scrollFocus($('propPanel'))`. Hidden when no proposal is `pending`. On the same refresh, when any proposal is pending, move `#propPanel` above `#incPanel` in `.col.left` (CSS `order` via a class on `.col.left`, no DOM move). | R1.5 (views keep working; screenshot comparison with `docs/screenshots/` must be refreshed), R6.3 | Unit: static test that `decideStrip` exists and is written with `textContent`. Browser: seed one pending proposal, strip visible above the fold at 390x844 and 1440x900, Review focuses the card. |
| UX-02 | high | Service page, `#view-service` slots | The service's proposals (`[data-slot="proposals"]`) come after the facts, permissions and 18-row live log. Approve is about 82% of the way down the page on desktop (2071 px) and about 78% on mobile (2666 px). | `31-service-unlocked-d.png`, `31-service-unlocked-m.png` | Reorder the three `data-slot` blocks: facts, then proposals, then incidents, then live log, then permissions. Pure DOM order edit in `index.html` (keeps ids). Give `#liveLog` `.log` a smaller `max-height` (260px) on this page so it cannot dominate. | R6.1 (no order required). Contradicts `design.md` "Service detail" paragraph, which lists header, permissions, live log, incidents, proposals. Update design.md in the same commit. | `tests/unit/test_dashboard_views.py` slot tests (check any that assert order). Screenshot: Approve visible without scrolling at 1440x900. |
| UX-03 | high | `decideProposal()`, `withdrawProposal()`, `#toast`, `#status` | After Approve the only feedback is `setStatus('Proposal p4 approved')` in the header (scrolled out of view) and the card's `msg` line, which `refreshProposalViews()` immediately destroys by re-rendering the card. The toast is never used for success. | `80-approve-feedback-d.png`, probe output `status: "Proposal p4 approved", msgs: []` | In `decideProposal`/`withdrawProposal` also call `toast(...)` with a next-step sentence (see section 5). Make `#toast` `position:fixed; bottom:16px; right:16px` (it is already a live region). Keep `setStatus` (R2.3 text unchanged). Keep the card message across the re-render by passing the text into `renderProposalCard` via a `STATE.flash[p.id]` map cleared after 8 s. | R2.3 (additive), R6.3 | Browser: click Approve at scroll position 1500 and see the toast in the viewport (shot). Unit: toast called in `decideProposal`. |
| UX-04 | high | Unlock form (`#tokenForm`, `wireUnlock()`), `handle401()` | Save accepts any token of valid shape and sets status "Unlocked" with no check, so a wrong token is believed until the first privileged click. The failure is then shown only as "token rejected" in the header, the form does not reopen, and the page returns to "Locked". | `12-unlocked-wrong-d.png`, `13-error-wrong-token-d.png`, audit row `auth_failed` in `51-rules-unlocked-d.png` | In `handle401(...)` when `out.clearToken` is true: also `toast('That token was not accepted. Paste the contents of the token file again.')` and call `openUnlock()` with `tokenNote` set to the same sentence. In `wireUnlock` submit handler change the status text to "Token saved. It is checked when you approve." (honest; no new route needed). | R2.3 (still sets "token rejected" in the status area), R2.2, R2.5 | Browser: wrong token then Approve opens the Unlock form with the message and focus in `#tokenInput`. Existing token tests in `test_dashboard_token.py` stay green. |
| UX-05 | high | Locked Approve/Deny/Withdraw, `lockButton()`, `updatePropNote()` | Locked Approve/Deny are `disabled` with only `title="unlock first"`: not shown on touch, not focusable, not read by many screen readers. `#propNote` explains it, but only on Overview. The service page has no note. Withdraw stays bright and enabled next to two dim buttons, so it is the easiest click and it is the destructive one. | `30-service-locked-d.png`, `10-overview-busy-locked-d-full.png`, probe `dis: Approve dis=true title=unlock first desc=` | (a) New `renderLockNote(container)` used by `renderServiceProposals()` and `renderProposals()`: a visible line "Unlock to approve or deny. Withdraw works without it." with an inline "Unlock" button that calls `openUnlock()`. (b) `lockButton()` sets `aria-describedby` to that note's id. (c) Style `[data-action="proposal-withdraw"]` as quiet (`.ghost.small`, dim border) so Approve is the primary when unlocked. Buttons stay `disabled` as R2.4 requires. | R2.4 (keeps disabled, adds visible reason), R6.3 | `test_dashboard_views.py::test_pending_cards_have_locked_approve_deny_and_a_token_free_withdraw` stays green; add a test that the note exists on both views. |
| UX-06 | high | Overview mobile, `#evPanel`, `form.send` | Page is wider than the viewport: scrollWidth 439 vs 390 (and 439 vs 320). The Séance log `form.send` (select + input + button in one non-wrapping flex row) forces `#evPanel` to 439 px. | probe `m #/ ... sw/cw 439 390 wide ['evPanel:439','events:424']`, `14-overview-unlocked-m-full.png` (captured 439 wide) | CSS only: `form.send{flex-wrap:wrap}`, `form.send input{flex:1 1 12ch;min-width:0}`, `.grid>.col, #evPanel{min-width:0}`. | R8.3 (360 px usability) | `python .agents/tasks/ux-audit/probe.py` style check: scrollWidth == clientWidth at 320, 360, 390 on all five routes. |
| UX-07 | high | Rules and Audit tables, `table.rules`, `#rulesBody`, `#auditBody`, `ruleRow()`, `auditRow()` | On mobile the table keeps its 824 px width inside `.table-wrap`, and `overflow-wrap:anywhere` breaks words per character ("signatu/re: upstrea/m"). The Actions column (Revoke, Pause, Re-enable, Edit) is off-screen to the right. Pause and Revoke are the safety actions and must be reachable. Audit Detail renders one character per line. | `51-rules-unlocked-m.png` | At `max-width:640px`: `table.rules thead{display:none}`, `tr` as a block card, `td::before{content:attr(data-label)}`. `ruleRow()`/`auditRow()` set `data-label` with `setAttribute` (static header text, not untrusted). Remove `overflow-wrap:anywhere` from `td` and use `overflow-wrap:break-word` plus `min-width:0`. Desktop unchanged. | R7.1, R7.2, R7.5, R8.3 (note: R8.3 only names the services list; this extends it) | Browser at 390 px: rule card shows Revoke and Pause without sideways scroll; desktop screenshot identical. `test_dashboard_views.py` rule/audit rows tests. |
| UX-08 | medium | Service page header, `#svcName`, `#svcDetail`, `renderServiceHeader()` | No breadcrumb or back link (only the "gone" state has `#svcBack`). State is plain text in a `dl` instead of the pill used on the list. The only way back is the nav button or the browser back button. | `31-service-unlocked-d.png` | Add `<a href="#/services" id="svcCrumb">All services</a>` above `#svcName` (plain hash link; no JS needed). In `renderServiceHeader()` add `el('span','pill '+STATE_CLASS[s.state],stateLabel('service',s.state))` next to the title. | R3.1 | Browser: click crumb returns to list and focus keyboard path works (`Enter`). |
| UX-09 | medium | Incident cards: `renderIncidents()` (Overview), `renderIncidentsFor()` (service page) | Service page incidents are read-only: no Explain, Propose, Lay to rest. Overview incident cards show host and category but not the service, and do not link to it. Service cards show "1 open incidents" but not "1 fix waiting". | `31-service-unlocked-d.png` vs `10-overview-busy-locked-d-full.png`, `21-services-unlocked-d.png` | (a) Reuse the Overview card actions in `renderIncidentsFor()` for open incidents (Lay to rest at minimum; it needs no token). (b) In `renderIncidents()` add a small `a.tag` "service: proxy" linking to `#/services/<id>` when `i.service_id` is set. (c) In `serviceCard()` add a pill "N fix waiting" computed from `STATE.proposals` where `status==='pending'` and `service_id===s.id`. Fix the grammar "1 open incidents" (singular/plural). | R3.1, R6.1 | Static test that no `innerHTML` was used (existing). Browser: link and pill render with hostile service name as text. |
| UX-10 | medium | Services list, `serviceCard()`, `renderServices()` | The meta line wraps mid-phrase: "last seen 1 h ago · 0 errors/min · 1 / open incidents". Unassigned shows beside real services with no explanation. The empty-state card and the Unassigned card have equal weight. | `21-services-unlocked-d.png`, `03-services-empty-locked-d.png` | Split the meta line into three spans with `white-space:nowrap` inside a flex-wrap row. Add to the Unassigned card: "Events no service claims yet. Add a service or run discovery to claim them." When only Unassigned exists, render the empty-state card first with the two buttons. | R3.2, R3.6 (text "No services yet. Run discovery or add one." unchanged; test counts it exactly once) | `test_dashboard_views.py` line 90 keeps passing; screenshot at 390 px. |
| UX-11 | medium | Unlock UX, `#lockText`, `#unlockBtn`, `#tokenForm`, `.prompt` | "Locked" is dim 12.8 px text. Nothing says what locking costs the user. The form opens as a full-width band that pushes the page down. On mobile the Cancel button is cut off at the right edge. The form never says how to read the token file. | `11-unlock-open-d.png`, `11-unlock-open-m.png` | (a) `#lockText` text: "Locked: view only" / "Unlocked". (b) `applyLock()` also sets the Unlock button `title` and visible text "Unlock to approve". (c) `.unlock .prompt{flex-wrap:wrap}` and `input{flex:1 1 14ch;min-width:0}`. (d) Add one line to `TOKEN_PROMPT_TEXT`: "Open that file, copy its single line, paste it here." Do not echo the token. | R2.1, R2.5 | Browser: form usable at 320 px, Cancel visible. `test_dashboard_token.py`. |
| UX-12 | medium | Guided workflow, `renderGuide()`, `doIt()` | The guide still shows the original six steps and counts them as "done" cumulatively: with p4 pending it shows "Approve or deny it" done and 4/6. Step 5's hint says "!approve p1 or !deny p1" even when p1 was denied long ago. The guide never mentions unlocking, Services or Discovery. The sixth step wraps alone onto a second row (5-column grid). | `14-overview-unlocked-d.png`, `10-overview-busy-locked-d.png` | Keep six steps and flags (R6.6). Only change copy and layout: (a) `.steps` grid to `repeat(auto-fit,minmax(180px,1fr))` so 6 fit as 3x2 or 6x1. (b) One line under the progress when locked: "Step 5 needs the admin token. Unlock first." (new `#guideLockHint`, hidden when unlocked). (c) Hint copy per section 5. The step titles and keys come from the hub's workflow response (`w.steps`), so title changes are a hub change: out of scope, hints only if the hub allows. | R1.5, R6.6 (must stay six steps) | `test_dashboard_views.py` workflow tests; screenshot comparison. |
| UX-13 | medium | First-run Overview, `#storm`, `#termPanel`, `#transcript` | Two orange primary buttons ("Do it" step 1 and "Summon a failure storm") do the same thing. The terminal starts as an empty black rectangle with no hint. Stats are six zeros. | `01-overview-empty-d.png`, `01-overview-empty-m.png` | After the first incident exists (`STATE.incidents.length>0`) demote `#storm` to `class="ghost"` and relabel "Another storm". Empty transcript: one dim line "ChimericBot is listening. Try !help." (client-side placeholder `.tline.sys`, not stored). | R1.5, R9 | Browser: empty hub shows hint; after storm the header button is ghost. |
| UX-14 | medium | Discovery, `renderDiscovery()` (line ~2351), `#discStatus`, `#discScanSlot` | Empty result is a dead end: "Scan finished: no apps found." Locked, "Discover local apps" is dimmed with no reason. | `42-discovery-results-d.png`, `05-discovery-initial-locked-d.png` | Empty result text per section 5 plus a "Add a service by hand" button (`here.hash='#/services'` then `renderServiceForm(null)`). When locked add a line under the explainer "Scanning needs the admin token. Unlock first." (new `#discLockHint`). | R4.1 (explainer unchanged), R4.5 (no scan on load, unchanged) | `test_dashboard_views.py` discovery tests; browser. |
| UX-15 | medium | Rules and Audit vocabulary, `ruleRow()`, `auditRow()`, `fmtStamp()` | Same state, three words: status "suspended" with "paused by", button "Re-enable", button "Resume", "Pause all". Actor shows raw `rest:127.0.0.1` / `client:127.0.0.1`. Actions are snake_case and wrap mid-word ("proposal_withdr/awn"). The `auth_failed` subject is the route template `POST /api/proposals/{pid}/decide`. Dates use the browser locale ("28/11/2026, 10:33:08 am") while the rest of the app is 24 h. The preview says `signature "?"` while the field is empty. | `51-rules-unlocked-d.png`, `52-rule-form-d.png` | Display helpers (all via `textContent`): `actorText(a)` maps `rest:*` to "API", `client:*` to "browser", `dashboard` stays; `actionText(a)` replaces `_` with a space; `subjectText` leaves ids as is and shows route templates as "a decision request". `fmtStamp` uses 24 h `toLocaleString([], {hour12:false})`. `td:nth-child(1), td:nth-child(3){white-space:nowrap}` on the audit table. Add a one-line legend under Approval rules: "Paused rules can be resumed. Revoked rules are gone." Replace the empty-signature preview with "Type the signature to match." | R7.1, R7.4, R7.5 | `test_dashboard_views.py` audit/rule row tests; unit for the three helpers (pure, add to `PURE` and `runSelfTest`). |
| UX-16 | low | Séance log, `renderEvents()`, `.row` | Time column is 62 px and the locale 12 h time "9:13:40 am" wraps onto two lines in every row, unlike the terminal (24 h) and the live log (24 h). | `10-overview-busy-locked-d-full.png`, `60-terminal-d.png` | Use `fmtTime(ts)` (already 24 h) in `renderEvents`; `.row .t{white-space:nowrap}`. | R1.5 | Visual. |
| UX-17 | medium | Action feedback: `#storm`, Lay to rest (`button[data-act="resolve"]`), Pause all, Copy | Lay to rest: the card vanishes, the only confirmation is a terminal line `RESOLVED ...` scrolled in a separate panel; no toast, empty status. Summon storm: no confirmation text. Withdraw has no confirmation or toast. | `83-lay-to-rest-d.png`, probe `after rest {"status":"","toast":""}` | Use the existing `toast()` for every state-changing click, with a next step (section 5). Add `aria-live` is already on `#toast`. | R6.5 | Browser probe: toast text non-empty after each action. |
| UX-18 | medium | Closing the loop: `renderProposalCard()`, incident cards | After approval the card says "approved - waiting for agent". After the agent reports, "done" appears with "verification passed" in grey meta text. Nothing links the proposal back to its incident and nothing tells the user to lay the incident to rest (step 6 of the journey). | `81-approve-after-d.png`, `83-lay-to-rest-d.png` | In `renderProposalCard()`: when `p.status==='done' && p.verification_result==='passed'` show a `.pill.done` text "done, tests passed" and a button "Lay incident to rest" that calls the same resolve call as the incident card for `p.incident_id`. For a `failed` result show an amber line "Verification failed. Deny a follow-up or ask the agent to retry." On the incident card add a line "fix #p4 approved" when a non-denied proposal exists. | R6.1, R6.2 | Static test; browser with seeded `done` proposal. |
| UX-19 | low | Navigation, `nav [data-nav]`, `showRoute()` | No counters or markers in the nav: the user cannot see from Overview that Services has an open incident or that Rules are paused. Only `#pausedBanner` covers the rules-paused case. | `10-overview-busy-locked-d.png` | Add a `<span class="count">` inside the Services button (open incidents on non-Unassigned services + pending proposals) and Rules button (paused rules) set via `textContent`, with `aria-label` extended ("Services, 1 waiting"). | R3.1, R7.6 | `test_dashboard_a11y.py` names check. |
| UX-20 | medium | Keyboard and screen reader flow | No skip link. Overview has 39 tab stops; Approve is after nav (4), Unlock, Summon, guide (2), then 16 incident buttons, so 25+ stops. The locked Approve is disabled and not focusable. The h1 contains the skull glyph that is read aloud. | probe TAB sequence `d`/`m`, `noName: []`, `under44 39/39` | `<a class="skip" href="#main">Skip to content</a>` plus `id="main"` on `<main>` (visible on focus). UX-01 strip puts a "Review" button early in the tab order. Wrap the glyph in `<span aria-hidden="true">`. Keep the 3 px focus outline (it is good). | R8.1 | Browser tab through; `test_dashboard_a11y.py`. |
| UX-21 | low | Touch targets | All 39 controls on Overview are under 44 px tall; ghost small buttons are about 26 px (WCAG 2.2 AA minimum of 24 px is met). | probe `under44 39/39`, `small 0` | At `max-width:480px` `button{min-height:40px}` and `.actions{gap:10px}`. | R8.3 | Probe. |
| UX-22 | low | Service form `renderServiceForm()` | Id comes first and must be invented. The Protocols checkboxes sit under their labels with no explanation. "Log files" heading has no help. | `22-service-form-d.png` | Order: Name, then Id auto-filled with a slug of the name until edited (`slugify` pure function); one-line help under Protocols: "Which feeds belong to this service. Leave empty to match by host or app name only." | R3.5 | Pure test for `slugify`; browser. |
| UX-23 | low | Error copy: `errorText()`, `unreachable()`, `openPropose()` | Errors are flat and do not say what to do: "Could not reach the mesh.", "Rejected by the mesh (status 500).", "token rejected". | code reading, `13-error-wrong-token-d.png` | Copy per section 5. `errorText` and the 401 strings are pinned by tests (`'token rejected'`, `'unlock first'` must stay as substrings): keep those exact strings in the status area and add the friendlier sentence to the toast. | R2.3, R6.5 | `test_dashboard_core.py` string checks. |
| UX-24 | low | Kiro permissions panel, `#permPanel` | The summary "Kiro permissions" does not say what it is for. The Copy button sits below a 16-line scrolled `pre`. | `32-service-perms-d.png` | Summary text "Kiro permissions (optional, for hands-off edits)". Move `#permCopy` above `#permText`. | R3.7 (text from the hub unchanged, hub never writes the file) | `test_dashboard_views.py` permission tests. |

### What works and should not be touched
- Focus outline on every control (3 px solid), all controls named, one h1 and ordered h2/h3.
- Status never relies on colour alone (pill text: pending, approved, done, denied, idle).
- `aria-current="page"` on the active tab, including the service page under Services (`showRoute()`).
- Hash routes (`#/`, `#/services`, `#/services/<id>`, `#/discovery`, `#/rules`) survive reload; unknown service shows "This service no longer exists." with a Back button (`70-service-gone-d.png`).
- Sticky right column now scrolls as one unit (the earlier fix, commit e84d83e).
- Playful empty states ("The crypt is quiet. Summon a failure storm or send a log line.") already name the next action.
- "Always approve fixes like this" inline form with plain-language preview sentence.

## 4. Recommended change set (priority order)

Each batch is one commit, built and tested in about 1 to 2 hours. All edits are in `src/chimericmesh/web/index.html`
plus tests in `tests/unit/test_dashboard_*.py`. Run `python -m pytest -q tests/unit -k dashboard` (about 80 s) after each.
Estimates are for one developer who knows the file.

| batch | scope | findings | effort | risk |
|-------|-------|----------|--------|------|
| B1 Mobile fixes | CSS only: wrap `form.send`, `.unlock .prompt`, `min-width:0` on grid columns, 40 px touch targets at <=480 px, stacked Rules/Audit rows with `data-label`. | UX-06, UX-07, UX-11(c), UX-21 | 1.5 h | Low. Rules markup gets `data-label` attributes; desktop must be pixel-compared. |
| B2 Approve path first | `#decideStrip`, proposals above incidents when pending, service page slot reorder, visible lock note with inline Unlock, quiet Withdraw. | UX-01, UX-02, UX-05, UX-20 (skip link and glyph) | 2 h | Medium. Touches `design.md` layout text and `docs/screenshots/`. |
| B3 Feedback and errors | toast on approve, deny, withdraw, resolve, storm; keep card message across re-render; 401 reopens Unlock with a message; honest Save text; friendlier error sentences. | UX-03, UX-04, UX-17, UX-23 | 1.5 h | Low. Pinned strings `token rejected` and `unlock first` stay. |
| B4 Close the loop | "done, tests passed" pill with "Lay incident to rest"; incident shows linked fix; service tag on incident cards; "fix waiting" pill on service cards; nav counters. | UX-18, UX-09, UX-19 | 2 h | Medium. Needs a resolve call from the proposal card; reuse the incident card handler. |
| B5 Service page and services list | crumb and state pill, actionable incidents on the service page, meta line wrapping, Unassigned hint, service form (name first, slug id, protocol help). | UX-08, UX-09(a), UX-10, UX-22 | 1.5 h | Low. |
| B6 Rules/Audit/Discovery language | actor/action/subject display helpers, 24 h stamps, vocabulary legend, discovery empty result and lock hint. | UX-14, UX-15, UX-16 | 1.5 h | Low. Helpers are pure: add to `PURE` and `runSelfTest`. |
| B7 First run and copy | demote storm button after first incident, terminal placeholder, guide grid and lock hint, permissions summary, spooky copy pass (section 5). | UX-12, UX-13, UX-24 | 1 h | Low. Keep six guide steps. |

### Implementation status

| batch | finding | status |
|-------|---------|--------|
| B1 | UX-06 | Done 2026-11-28. Also needed `.grid{align-items:stretch}` at <=960 px: at 320 px the Overview terminal panel was shrink-wrapped to 357 px. |
| B1 | UX-07 | Done 2026-11-28. Deviation: the `overflow-wrap:break-word` override lives only inside the <=640 px block; the desktop `td` rule keeps `anywhere` (pinned by a test, desktop unchanged). Labels come from `RULE_COLS`/`AUDIT_COLS` (equal to the th texts, tested). |
| B1 | UX-11(c) | Done 2026-11-28 |
| B1 | UX-21 | Done 2026-11-28 |
| B2 | UX-01 | Done 2026-11-28. `#decideStrip` and pure `decideText` (selftest cases). Pending proposals move up with class `has-pending` on `.col.left`; on phones (<=960 px) they come first (`order:0`, before the terminal). The strip text for several fixes is "N fixes are waiting for you. Newest: ...". |
| B2 | UX-02 | Done 2026-11-28. Order: facts, proposals, incidents, live log, permissions. `#liveBox` max-height 260 px. design.md updated; two pinned tests updated for the new order. |
| B2 | UX-05 | Done 2026-11-28. Deviation: note copy keeps the pinned substring "unlock first" ("Approve and Deny are locked: unlock first with the admin token. Withdraw works without it."). Note and Unlock button on both pages (`#propNote`, `#svcPropNote`); Approve/Deny get `aria-describedby`, set in `renderProposalCard` (not `lockButton`, which cannot know the page when the button is created). Withdraw is `quiet`, Approve is the filled button. Buttons stay `disabled` (R2.4). |
| B2 | UX-20 | Done 2026-11-28: skip link (`#skip`, handler focuses `<main id="main">` without touching the hash, so the router is not triggered). The skull glyph was already `aria-hidden`. |
| B3 | UX-03 | Done 2026-11-28. Toast (polite live region, never takes focus) on approve, deny, withdraw with the section 5 sentences; header status kept (R2.3). The card message survives the re-render through `STATE.flash` (8 s). `#toast` was already `position: fixed` (bottom centre), so the CSS is unchanged. |
| B3 | UX-04 | Done 2026-11-28. Deviation, approved by the user: Save now verifies the token before it says "Unlocked" (the plan's honest-text option is replaced). It sends the saved token on `GET /api/discovery/dismissals`, an existing token-protected read that changes nothing; "Checking the token..." shows meanwhile. A 401 clears the token, keeps the status text "token rejected", toasts "That token was not accepted. The ghost wants the exact line from the token file." and reopens the form with the same message. Other failures (403, 429, 5xx, no answer) also clear the saved token and say why. The wrong-token sentence follows the section 5 copy table, not the longer sentence in the finding row. |
| B3 | UX-17 | Done 2026-11-28. Toasts for Lay to rest, Summon storm, withdraw. Deviation: the resolve sentence is "Laid X to rest. If the errors return, it can reopen." because the hub reopens the same incident id after its window (`IncidentTracker.reopen`), it does not open a new one. The success toast is shown only when the refreshed incident list says it is resolved, otherwise "Could not confirm that X was laid to rest. The terminal shows what the hub said." The button still sends `!resolve <id> via dashboard` through the terminal route, as before. |
| B3 | UX-23 | Done 2026-11-28. `friendlyError(status)` for the network and propose errors and for failed decisions; `errorText` and the pinned strings `token rejected` and `unlock first` are unchanged. |
| B4 | UX-18 | Done 2026-11-28. Pure `closeLoop` and `fixLine` (selftest cases). A done proposal with `verification_result` passed shows the pill "done, tests passed" and, while its incident is not known to be resolved, a button "Lay incident to rest" that calls the shared `resolveIncident` (the same `!resolve <id> via dashboard` terminal call as the incident card, no token). A failed result shows the amber line "Tests failed. Deny it and ask the agent for another fix." Incident cards show "fix #p4 approved" for the newest pending, approved or done proposal. Denied and withdrawn ones are not shown. |
| B4 | UX-09 | Partly done 2026-11-28 (B4 scope: b and c). Overview incident cards get a "service: <id>" button (sets the hash; no link built from data). Service cards get a "N fix waiting" pill and the singular "1 open incident". Deviation: the tag shows the service id, because the service names are not loaded on the Overview. Part (a) was done in B5 (see below). |
| B5 | UX-08 | Done 2026-11-28. `#svcCrumb` ("All services", plain hash link) above the title and a state pill `#svcPill` beside it (set with `textContent`, hidden until the service loads). The State row in the facts list stays. |
| B5 | UX-09(a) | Done 2026-11-28. The Overview card actions moved into one shared `appendIncidentActions(d, i, noService)`; the service page uses it without the "service: x" button (Explain, Find in code, and for open incidents Propose a fix and Lay to rest). A refresh does not wipe a propose form being typed in. Approved decision (a): proposals sit above the live log (done in B2). No backend change. |
| B5 | UX-10 | Done 2026-11-28. The meta line is three `nowrap` spans in a flex-wrap row; the Unassigned card says "Events no service claims yet. Add a service or run discovery to claim them." The empty-state card was already first. |
| B5 | UX-22 | Done 2026-11-28. Name first, Id follows it through pure `slugify` until the user edits the Id (reserved `unassigned` gives an empty suggestion); help line under Protocols. Deviation: one `slugify` self-test case only, because the self-test is capped at 200 lines. Focus now starts in Name. |
| B4 | UX-19 | Done 2026-11-28. Counters on the Services tab (open incidents with a service plus pending proposals) and the Rules tab (suspended rules, or "paused" while auto-approval is paused), set with `textContent`, with the accessible name extended ("Services, 2 waiting"). New scheduler task `nav-counts` (5 s, every view) reads incidents, pending proposals and rules, all token-free GETs. |
| B4 | extra | 2026-11-28: the `decideText` self-test case expected "?" where `capText` returns the ellipsis, so `#selftest` reported FAIL since B2. Expectation corrected; the page self-test now passes (197 cases). |
Total about 11 hours. If time is short, ship B1, B2 and B3: together they cover all seven high findings (UX-01 to UX-07).

Suggested tests per batch: a static test for every new element id; a pure-function test for each new helper; the
existing `guard-innerhtml` hook and static `textContent` test must pass (all new text uses `textContent`); a headless
probe at 320, 390 and 1440 that scrollWidth equals clientWidth (reuse `.agents/tasks/ux-audit/probe.py`).

## 5. Copy: before and after

Rule for playful copy: at most one short sentence of flavour, then the next action in plain words. No joke replaces the
instruction.

| where | before | after |
|-------|--------|-------|
| Locked status (`#lockText`) | `Locked` | `Locked: view only` |
| Unlock button | `Unlock` | `Unlock to approve` |
| Approve note, Overview (`#propNote`) and service page | Overview only: `Approve and Deny need the admin token: unlock first. Withdraw works without it.` | Both pages: `Approve and Deny are locked. Unlock with the admin token, then decide. Withdraw works without it.` |
| Token prompt (`TOKEN_PROMPT_TEXT`) | `Protected actions (approve, deny, add or edit a service, scan, rules) need the admin token. Paste the contents of the token file.` | add: `Open that file, copy its single line, paste it here.` |
| Token Save status | `Unlocked` | `Token saved. It is checked when you approve.` |
| Wrong token (toast, plus status `token rejected` kept) | `token rejected` | `That token was not accepted. The ghost wants the exact line from the token file.` |
| Approve done (toast) | status only: `Proposal p4 approved` | `Approved p4. The agent will apply it; this card turns "done" when it reports back.` |
| Deny done | `Proposal p4 denied` | `Denied p4. The agent will not run it.` |
| Withdraw done | none | `Withdrew p4. Nothing will run.` |
| Fix done and verified (pill and button) | `done` and grey meta `verification passed` | `done, tests passed` and button `Lay incident to rest` |
| Fix done, verification failed | `verification failed` in meta | `Tests failed. Deny it and ask the agent for another fix.` |
| Lay to rest (toast) | none | `Laid 2f9159b0aa to rest. If the errors come back, a new incident will open.` (confirm the hub behaviour before shipping this sentence) |
| Decide strip | none | `A fix is waiting for you: Add bounded backoff to upstream.py. [Review]` |
| Storm button after first incident | `Summon a failure storm` | `Another storm` |
| Empty transcript | blank | `ChimericBot is listening. Try !help.` |
| Empty incidents | `The crypt is quiet. Summon a failure storm or send a log line.` | keep (already good) |
| Empty proposals | `No proposals yet. An agent proposes fixes through MCP; you can simulate one from an incident card.` | `No proposals yet. Ask your agent to look at an incident, or press "Propose a fix" on a card.` |
| Discovery, locked | dimmed button, no text | `Scanning needs the admin token. Unlock first.` |
| Discovery, nothing found | `Scan finished: no apps found.` | `No apps found. Nothing visible is listening here. Add your service by hand.` plus button `Add service` |
| Service not found | `This service no longer exists.` | `This service is gone (or never existed). Back to services.` |
| Unassigned card hint | none | `Events no service claims yet. Add a service or run discovery to claim them.` |
| Service meta | `last seen 1 h ago · 0 errors/min · 1 open incidents` | `seen 1 h ago · 0 errors/min · 1 open incident` (three non-breaking chunks) |
| Network error | `Could not reach the mesh` | `The mesh is not answering. Check that the hub is running, then try again.` |
| Propose rejected | `Rejected by the mesh (status 500).` | `The mesh refused that (status 500). Try again or check the hub log.` |
| Audit actor / action | `rest:127.0.0.1` / `proposal_withdrawn` | `API` / `proposal withdrawn` |
| Rules empty | `No rules yet. Approve a fix, then choose "Always approve fixes like this".` | keep (already names the next action) |
| Permissions summary | `Kiro permissions` | `Kiro permissions (optional, for hands-off edits)` |
| Rule form preview, empty signature | `... match signature "?" ...` | `Type the signature to match.` |

## 6. Conflicts with approved dashboard requirements (flagged)

No finding contradicts an acceptance criterion. Items that need an explicit decision:

1. UX-02 (service page order) contradicts the order written in `design.md` ("Service detail ... header, permissions, live log,
   incidents, proposals"). Requirements R5.6 and R6.1 do not fix an order. Needs a design.md edit with approval.
2. UX-01 and B2 change the Overview layout. R1.5 says the existing views keep working with a manual "screenshot
   comparison with `docs/screenshots/`". The screenshots must be retaken after B2.
3. UX-05 keeps buttons `disabled` while locked as R2.4 requires. It does not use `aria-disabled` (which would make them
   focusable) because that would change R2.4's behaviour. Adding a visible reason is an addition, not a conflict.
4. UX-04 shows "That token was not accepted" next to the required "token rejected" status (R2.3). Both appear.
5. UX-07 and UX-06 go beyond R8.3, which only requires 360 px usability of the services list, and the Out of Scope entry
   "Mobile layout beyond 360 px usability". They fix real breakage (sideways scroll, unreachable Revoke). Treat as a
   scope extension needing approval, not as a spec violation.
6. UX-18 adds a button on proposal cards that resolves an incident. R6.3 and R6.4 list the buttons on a proposal card.
   The addition is outside that list. It calls an existing token-free route, but approve it explicitly.
7. UX-12: R6.6 pins the guide to the same six steps and flags. Only hints, layout and a lock line are proposed. Step titles
   come from the hub, so they are not changed.
8. Never-list items are respected: no `innerHTML`, no token in a URL or DOM, no third-party resources, no scan or
   approval on load, no new build step or dependency.

## 7. Not recommended

- A light theme or theme switch: Out of Scope in the dashboard requirements, and the dark crypt look is the product.
- Moving approval into a modal dialog: needs focus trapping per the APG pattern for little gain over the decide strip.
- Auto-refreshing the page to the next pending proposal or auto-focusing Approve: breaks the "never start an approval
  automatically" rule and risks accidental approval.
- Validating the token on Save with a new route: needs a hub change for a small gain. The 401 reopen (UX-04) covers it.
- Jokes inside errors that hide the instruction, ghost mascots or animated sprites: they add weight and no clarity.
  Flavour stays to one short phrase beside the real next step.
- Charts or history graphs: Out of Scope.
- Replacing `window.confirm` in Pause all with a custom dialog: works, accessible by default, low value.
- Making the Overview a tabbed layout (incidents / proposals / terminal tabs): hides the terminal and conflicts with the
  sticky column fix.
- Hash sub-routes for proposals (`#/services/x/proposals`): `parseRoute` is pure, tested and pinned by design; the decide
  strip and focus management give the same shortcut without a new route.
