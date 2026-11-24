# How we used Kiro

ChimericMesh is a local hub that listens to old protocols (Syslog over UDP and TCP, Telnet, IRC, plain log files). It turns the traffic into incidents and exposes them to Kiro agents through MCP. Many legacy applications log somewhere nobody looks and cannot be changed to speak a modern API, so an AI agent cannot see them. With ChimericMesh the agent investigates an incident, proposes a code fix, waits for a person to approve it, makes the change, runs the tests and checks that the error rate dropped. The hub never executes a fix. It records the human decision, and the agent does the work under Kiro's own permissions.

Where a claim rests only on a local session and not on a file in the repo, the text says so.

## How the work went

**Phase 1: vibe-coded start (13 to 14 Nov).** The first working version was built conversationally. `a6fa7f6` rebuilt the intake, incident detection, MCP server and dashboard in one pass. Real public logs then broke it, which is covered under property tests.

**Phase 2: steering and the first hooks.** `.kiro/steering/project.md` and `testing-pbt.md` fixed the rules the agent kept needing to be told (log text is untrusted, IRC lines are stripped of CR/LF/NUL). Two hooks followed. The first one failed, see the hooks section.

**Phase 3: specs before code (18 Nov).** For V1 we wrote requirements, a capability map and seven module designs before any module code. The user approved them: requirements and map in `b302d95`, the store design in `4c6dcfe`, services and connectors in `ddf9d3b`, and the other four in `dfdefa5`. The effort plan assumed a 16-day budget and was checked against the designs in a review.

**Phase 4: test-first builds with reviews.** Each module was built from its `tasks.md`, one commit per task (`feat(store): task 5 ...`), with review rounds after the code. Examples: store (`6187405` to `dfba7df`), connectors (`e97de40` to `4597951`), dashboard (`7cc3d02` to `7b3aa6f`). Each branch was merged only after the user approved the module (for example `de26ca6`, `f90d06f`).

**Phase 5: live runs in Kiro IDE.** The custom agents ran against a real incident from `examples/legacy_app`. The results are in the custom agents section.

## Kiro IDE, CLI, Web, Crew

We used Kiro IDE (version 1.4.215 on the build machine, per a local check). The agents, hook files, steering, specs and MCP config in `.kiro/` are what the IDE loads.

We also ran the agents from **Kiro CLI V3** (installed with the official `install.ps1`, signed in with Google). On first start in the repo folder it asked to trust the workspace, then loaded the same `.kiro/agents/` profiles the IDE uses: both `mesh-responder` and `mesh-watcher` printed their `welcomeMessage`. We switched to `mesh-watcher` and asked it to call `wait_for_work` once. It made the MCP call to the running hub and reported `Status: timeout` with no tool error, in a 38-second turn.

How the two surfaces differed for us: nothing in the repo had to change. The steering, the agent profiles, the MCP server config and the permissions all came from the same files, which is the point of keeping them in `.kiro/`. The IDE was where we did the long build and review work and watched diffs; the CLI was a quick, scriptable way to drive the same agent against the same hub from a terminal. The CLI's trust prompt also showed where Kiro keeps workspace trust (`~/.kiro/workspace-roots/<hash>/permissions.yaml`), outside the repo, which matches our honest-limits note that a cloned repo cannot grant itself permissions.

We did not use Kiro Web or Kiro Crew. (An early scaffold commit had a `src/agents/crew.py` file; it was replaced the same day and was not Kiro Crew.)

## Custom agents

Two agents live in `.kiro/agents/`.

`mesh-responder.json` (added in `4721950`) runs the approval loop on demand. It allows only read-only MCP tools without a prompt. Proposing, completing, resolving, shell and file writes ask first.

`mesh-watcher.json` (`903752d`, tightened in `39f80bb`) keeps one session running. It loops on `wait_for_work`, proposes one fix per incident, and edits only when `get_proposal` and `verify_proposal` both agree the proposal is approved. Its prompt forbids resolving an incident when the tests fail or the rate did not fall, and forbids relying on an approval remembered from earlier.

Live evidence:

- `mesh-watcher`: the 25-second `wait_for_work` long poll was checked in Kiro IDE on 2026-11-23 and again in Kiro CLI. The exported IDE session (`docs/kiro-sessions/kiro-session-sess_d194e285-…zip`, agent mode `mesh-watcher`) shows two calls: one sent at 10:35:43 that returned `{"status": "timeout"}` at 10:36:11, and one sent at 10:37:49 that returned at 10:38:19. That is about 28 and 29 seconds, so the IDE held the call for the whole 25-second wait plus the MCP round trip. Commit `efb8b85` records the check. Which file the IDE reads `requestTimeout` from was not tested; the README names both places.
- `mesh-responder`: run live in Kiro IDE on 2026-11-18 against the `legacy_app` incident. It traced the timeouts to the retry loop in `upstream.py`, proposed a capped backoff, waited for `!approve`, made the edit and ran the example tests (15 passed). It then refused to complete the proposal because the event rate had not dropped: the running worker still had the old code. When the file was later reverted outside its control, it noticed and asked before re-applying.
- That run also showed a gap. The hub had restarted in between, and proposals were in memory then, so the id `p1` now pointed at a different, unapproved proposal. The agent said re-applying was "still what p1 approved", which was false. That single moment produced two design changes. Proposals are now persisted by the store module, so `p1, p2, ...` are never reused across restarts. And the `mesh-watcher` agent must re-read the proposal and confirm its status, action and incident immediately before any change (first tried in `bf35fef` on an unmerged experiment branch, then built into `mesh-watcher` in `903752d`; `mesh-responder` was left unchanged on purpose).
- The agent's own backoff fix also had a latent bug: `2**failures` raises `OverflowError` after 1,024 consecutive failures. The example tests missed it, so a long-outage test now pins it (`5aa62a1`).
- The full run is exported from Kiro IDE in `docs/kiro-sessions/kiro-session-sess_6fd258c6-…zip`: session "Investigate And Fix Network Incident", agent mode `mesh-responder`, 18 Nov 2026, 273 messages including its `chimericmesh` tool calls. The export was checked before commit: it contains no admin token or other credentials.

In practice the agents were useful because the hub, not the prompt, holds the gate. Tests in `tests/unit/test_kiro_assets.py` check the profiles statically (JSON, tool names against the MCP server). They do not show that Kiro loads them.

## Agent hooks

Hooks live in `.kiro/hooks/`. The first innerHTML guard was an agent-prompt hook. The commit that replaced it says it "fired on every file write and enforced nothing" (`1f34704`). It became a command hook that runs `guard_innerhtml.py` and exits 2 only when a write to `src/chimericmesh/web/` uses `innerHTML`, `outerHTML`, `insertAdjacentHTML` or `document.write`. It is silent for every other write. `tests/unit/test_guard_hook.py` covers it, and `959463f` widened the pattern for the dashboard module.

Two follow-up fixes came from reading the hook documentation: the matcher was anchored to exact tool names, because a plain name matches any tool ID that contains it (`906bafd`), and the test-on-save `timeout` was moved to hook level (`98e83d6`). `79c4116` added tests for both.

`test-on-save.json` first ran the whole suite (`python -m pytest -q -x`) on every `.py` save. Once the suite grew past 3,000 tests that took 20 to 40 minutes per save and runs stacked up, so it now runs a fixed smoke set of five test files in about 40 seconds (`200f6d3`). `.kiro/hooks-optional/mesh-verify-write.json` is an opt-in PreToolUse hook (`14c9bb8`). It blocks a write unless the hub's verify route accepts the target file for the active proposal, and it fails open on unparseable input. We did not install it by default, and we did not run it live in the IDE.

## How the build was orchestrated: Kiro workflows and sub-agents

The V1 modules were not typed in one long chat. In Kiro IDE we ran declarative workflows: a planner step designed the steps, a coder agent executed the module's `tasks.md` test-first, and a separate reviewer agent checked each third of the work against the requirements and design, with a fix-and-review loop that stopped on an `APPROVED` verdict. Specs got the same treatment with a design reviewer. When a step hit a real conflict (for example a static SQL guard that could not pass as written, or event ids repeating after two crashes) it stopped and asked, and the decision was written into the design with a date and a blocker id. When the workflow tool was unavailable, the same pattern ran as single sub-agent tasks, each scoped to a few tasks and committing as it went.

Each module was built in its own git worktree and branch, so the developer's running demo and uncommitted files were never touched, and each branch was merged only after a human review of tests, diffs and screenshots. A test-run guard refused binds to the ports of the developer's running hub, so the 3,000-test suite could run while the demo stayed up. Evidence in the repo: one commit per task on each module branch, the merge commits, the `Review resolutions` and `Implementation deviations` sections in each `design.md`, and the dated deviation notes in each `tasks.md`.

## Spec-driven development versus vibe coding

Both were used, in that order. The vibe-coded version worked for a demo and failed on real log files. The spec-driven modules cost writing time up front, and their build logs record decisions as blockers instead of drift.

Evidence: `.kiro/specs/` has eight folders, each with `requirements.md`, `design.md` and `tasks.md`. `legacy-mesh` is the baseline spec for what existed before V1. The other seven are V1 modules: `store`, `services`, `connectors`, `discovery`, `auto-approval`, `agent-loop`, `dashboard`. `docs/v1-capability-map.md` records the binding decisions. Each task ends with a commit, and the dashboard `tasks.md` ends with a success-criteria walk.

What it changed: when an implementation disagreed with a design, we recorded it as a blocker and decided, instead of drifting. Example: the store's event-id design had a hole (see property tests) and the fix was a documented design change.

Not claimed: we cannot show that Kiro's built-in task runner executed the `tasks.md` files. The checkboxes do not prove it.

## Steering docs

`.kiro/steering/project.md` (always included) carries the layout and rules: parsers never raise, MCP tools are thin wrappers, no `innerHTML` for log text, strip CR/LF/NUL before IRC writes. `testing-pbt.md` is included for `tests/**` and tells the agent which properties parsers and formatters need. Static tests (`tests/unit/test_dependency_direction.py` and the innerHTML scans) turn the same rules into checks.

## MCP

`src/chimericmesh/mcp_server.py` exposes the hub as MCP tools using the MCP SDK v2 (`mcp.server.mcpserver.MCPServer`). The baseline tools cover incidents, proposals and resolving (`list_incidents`, `propose_remediation`, `resolve_incident` and others). The agent-loop module added `list_services`, `service_events`, `explain_log_window`, `verify_proposal` and `wait_for_work`. The server is configured in `.kiro/settings/mcp.json` and inline in both agent profiles. No tool takes the admin token, and there is no tool that creates or widens an approval rule.

## Property-based testing

Hypothesis tests are in `tests/pbt/` (55 files) plus property tests inside unit test files. The suite stood at 3308 passed, 9 skipped at the dashboard final checkpoint (`.kiro/specs/dashboard/tasks.md`, task 10.3).

Bugs that tests found:

- Event ids repeated after two crashes in a row. Property 5 failed on the sequence crash, ingest, crash, ingest with ids `[111, 111]`. Fixed in `dfba7df` (B2 in the store notes).
- A reference cycle kept a store writer thread alive after an app was dropped. Existing thread-count tests failed once the approval manager was wired in. Fixed in `a95a024`.
- Discovery accepted a non-absolute expanded base path. Fixed in `95922aa`.
- A resume property in connectors failed on a stray carriage return. This was a flaw in the test generator, since CRLF is stripped by design, so the fix was to the test (`aa60817`), not the product.

Real public data (Loghub and Ubuntu IRC logs, downloaded at run time, never committed) exposed bugs that hand-written examples had not. `fabf215`: a missing `<PRI>` made every event info-level, near-identical attacker lines split into many incidents, one-off errors were noise, and re-analysing history per event dropped UDP packets. `75d7e9b` added two more: a single chat line about the OOM killer opened an incident, and the IRC server trusted a client-supplied sender prefix.

## Kiro powers

The power `chimericmesh-remediation` is in `powers/chimericmesh-remediation/`. It uses the Agent Plugins layout: `plugin.json`, `mcp.json` and skills (`getting-started`, `triage`, `remediation-loop`, `watch-mode`). It was added in `be0c1ff`. The README says how to import it from the Powers panel. We did not record a verified install of it in Kiro, and the asset tests check only the files. Treat it as packaged, not as proven.

## Where Kiro surprised us

1. **A hook that did nothing.** The agent-prompt hook ran on every write and enforced nothing. A 38-line command script with a test replaced it (`1f34704`).
2. **A matcher that matched too much.** A plain tool name matched any tool ID containing it, so we anchored the pattern (`906bafd`).
3. **A property test beat the design review.** The reviewed design still let event ids repeat after back-to-back crashes. The property test found it during task 11.6 (`dfba7df`).
4. **Real logs beat our fixtures.** Four detection and parsing bugs in one afternoon of replaying public logs (`fabf215`).
5. **The agent said no, and once it should have.** In the live run, `mesh-responder` refused to complete a fix whose event rate had not dropped, which we had written into its prompt but not expected to see hold. In the same run it trusted a proposal id that a hub restart had silently reused. That one failure is why proposals are persisted and why the watch agent now re-checks the proposal right before it edits.
6. **Speed.** The store module (11 tasks) took about 4.5 hours from 18:35 to 23:05 on 18 Nov according to the build log, against a review estimate of about 7 human days. The build log is kept outside the repo.

## What we would do differently

- **Estimates.** Our own estimates disagreed (1.5 days for the store in the effort plan, about 7 in a later review) and both were far from the measured 4.5 hours. We would plan around review and decision time, which was the real cost, not typing.
- **Review loops.** Four modules (connectors, discovery, auto-approval, agent-loop) went through three review rounds, and some late fixes were README wording (`1cf3ba2`). We would cap reviews at two and keep a third for security-relevant modules.
- **Long agent runs.** Several sub-agent runs stopped partway (turn limits and network errors). Because every task was committed as soon as it passed, each one resumed from the last commit with nothing lost, but the progress logs live in `.agents/`, which is git-ignored, so judges cannot read them. We would keep a short build log in the repo.
- **Live checks earlier.** The hook matcher, `requestTimeout` and permission-glob questions were settled late. We would test them in the IDE on day one of each module.

## Not used

- Kiro Web and Kiro Crew.
- Headless Kiro CLI runs (the CLI was used interactively only).
- The `mesh-verify-write` hook live in the IDE (built and tested, not run live).
- A verified install of the `chimericmesh-remediation` power.
- Kiro's task runner for `tasks.md` (not claimed).
