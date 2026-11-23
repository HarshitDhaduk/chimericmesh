# ChimericMesh

Kiroween 2026, category: **Frankenstein**.

ChimericMesh stitches 1980s infrastructure protocols (Syslog, IRC, Telnet) onto modern AI agent workflows. Legacy systems keep talking the way they always have. Incidents are detected, exposed to Kiro agents through the Model Context Protocol (MCP), and announced back on IRC and via webhooks.

```
Syslog/UDP ─┐
Telnet/TCP ─┼─> parsers ─> Hub ─> incident detection ─┬─> IRC announce
IRC/TCP ────┘                                         └─> webhook
Kiro agent ── MCP (stdio) ──> REST API ──> Hub
```

## Quick start

```powershell
pip install -e ".[dev]"
python -m chimericmesh        # dashboard at http://127.0.0.1:8000
```

Ports (override with env vars): HTTP 8000, Syslog/UDP 5514, IRC 6667, Telnet 2323. Everything binds to 127.0.0.1 by default (`CHIMERICMESH_BIND`).

Try it:

- Run `.\scripts\demo.ps1` to send real Syslog, Telnet and IRC traffic (`-Only syslog|telnet|irc` to send one, `-Count 12` to trigger an `UPDATE` line).
- Click "Summon a failure storm" on the dashboard, or `POST /api/demo/storm`.
- Join IRC (`/server 127.0.0.1 6667`, `/join #ops`) and watch the bot announce incidents.
- Send syslog: `<27>Nov 13 11:10:08 db01 kernel: Out of memory: Kill process 4412`
- Open a Telnet session to port 2323 and type an `ERROR ...` line.

The dashboard's **Events seen** tile is a cumulative counter (`total_events` in `/api/stats`, with `total_by_source` and a 10 second `events_per_second`). The hub only keeps the latest 2000 events in memory for the log view (`events`, `buffer_capacity`), so the log header reads "latest N of TOTAL" once the buffer has wrapped.

## Data and proposal ids

The hub keeps events (bounded), incidents, proposals, an append-only audit log and counters in one local SQLite database, `~/.chimericmesh/chimericmesh.db` by default. The startup log prints its path. Proposal ids are unique per database; deleting the database restarts them at `p1`. Ids look like `p1`, `p2`, `p3` and nothing else.

| Variable | Meaning | Default |
|---|---|---|
| `CHIMERICMESH_DB` | Database file, or `:memory:` for no file | `<home>/chimericmesh.db` |
| `CHIMERICMESH_HOME` | Directory for the database | `~/.chimericmesh` |
| `CHIMERICMESH_MAX_EVENTS` | Most event rows kept (1 to 10,000,000) | 100000 |
| `CHIMERICMESH_EVENT_TTL_DAYS` | Event rows older than this are removed (1 to 3650) | 7 |
| `CHIMERICMESH_REOPEN_AFTER_SECONDS` | A resolved incident reopens when a matching event arrives after this many seconds (0 to 86400) | 300 |

A bad value logs one warning and uses the default. An explicit relative `CHIMERICMESH_DB` is resolved once against the current directory at startup; the defaults never depend on the current directory, so starting the hub from two folders opens the same file.

Things to know:

- One database file supports one running hub process. Two processes serving one file are not supported. A start that fails before serving leaves the crash marker untouched.
- Event ids keep rising across restarts. After a crash a restart skips ahead by a safety margin so clients holding an `after_id` cursor stay valid. After a crash that follows a long storage outage, a restart may reuse event ids a client saw; this is the one known gap.
- A database written by a newer build is refused at startup and is not modified. Refusing a newer-schema database after a crash may still checkpoint its `-wal` file into the database file; the data is not changed.
- The audit log can only be appended to. Log text, secrets and URL credentials never reach it.
- On Windows the owner-only permission step uses `icacls` and is verified by hand; on other systems the directory is `0700` and the file `0600`.
- The decider name of a proposal is display text. These prefixes are reserved for the hub's own rule logic and never make a decision count as the hub's: `rule:`, `rule-`, `withdrawn:`, `expired:`, `expired-`.

## Replaying real logs

Besides the hand-written demo traffic, you can stream real public logs into the mesh over UDP syslog:

```powershell
$env:PYTHONPATH = "src"
python -m chimericmesh.replay --dataset openssh --rate 200   # real SSH brute-force traffic
python -m chimericmesh.replay --dataset linux                # real /var/log/messages
python -m chimericmesh.replay --file my.log --limit 500      # your own file
```

Samples are downloaded on first use into `data/` (git-ignored, never committed). Lines are sent exactly as they appear in the file, with no `<PRI>` header, so the parser infers severity from the text.

**Data credit:** [Loghub](https://github.com/logpai/loghub) by the logpai group. The datasets are free for research and academic use; please cite Zhu et al., "Loghub: A Large Collection of System Log Datasets for AI-driven Log Analytics", ISSRE 2023. Replayed data is real but is not a live production system.

What replaying real data taught us (each now has a regression test): real logs often have no `<PRI>` header, one attacker produces hundreds of near-identical lines that differ only in user and address, one-off errors are noise, and re-analyzing history on every event drops UDP packets. Incidents are now tracked incrementally and the syslog socket has a 4 MB receive buffer.

### Replaying real IRC chat

```powershell
$env:PYTHONPATH = "src"
python -m chimericmesh.replay_irc --dataset ubuntu-server --rate 100
python -m chimericmesh.replay_irc --file chat.txt --channel "#ops" --limit 200   # "[HH:MM] <nick> text" format
```

This replays a real Ubuntu support channel through the mesh's IRC server, with one connection per speaker. The logs come from [irclogs.ubuntu.com](https://irclogs.ubuntu.com/); the Common Pile dataset card describes them as released into the public domain. Nicknames belong to real people, so they are replaced with pseudonyms by default (`--no-mask` disables this). The file is downloaded on first use into `data/` and never committed.

Things the real chat taught us: a chat line is evidence, not proof, so IRC text is capped at error severity and a single message about the OOM killer can no longer open an incident, and the IRC server now ignores any sender prefix a client supplies and uses the registered nickname. Clients must register (`NICK`) before their messages are ingested.

## Log files and TCP syslog

Servers that only write log files (Apache, nginx, IIS, vsftpd, Postfix) are read in place; nothing wraps or launches them. Register a file on a service with `POST /api/services/{id}/log-sources` and a body like `{"path": "C:\\inetpub\\logs\\u_ex.log", "format": "w3c"}`. The formats are `syslog-text`, `ncsa` (Apache and nginx access logs), `w3c` (IIS), `xferlog` (FTP) and `plain`. `GET /api/services/{id}` shows each source's status and counters.

- The hub opens the file for a moment on each poll and never holds it open, so rotation by rename works on Windows too. A rename that lands inside a read can fail with WinError 32; the rotating program should retry after a short wait. Measured on a Windows 11 machine while the file was being appended to, 1.5 to 3 percent of renames needed one retry; on an idle file none did.
- Lines written in the last half second before a rename or truncation can be lost, and so can a line without its newline at that moment.
- Read positions are saved every 5 seconds. After a crash the hub can repeat up to 5 seconds of lines, and can lose a few hundred milliseconds of events that the store had not yet written. A graceful stop saves the final position.
- A source that is disabled and enabled again starts at the end of the file. A new source starts at the end too, unless `backfill_lines` asks for recent lines.
- Event times are the arrival time at the hub, not the time written in the line.
- Limits: 2000 lines per second per source (extra lines are counted as dropped and summarised in one warning event), 8192 bytes per line, 1 MiB per poll.
- The path is fixed. Date-named files such as IIS `u_exYYMMDD.log` need a new path each day.
- Secrets in query strings and in `password=`, `token=` and `Authorization:` values are replaced by `<redacted>`. This is a best effort, not a guarantee.

Syslog over TCP (octet-counted or newline-framed) listens on `CHIMERICMESH_SYSLOG_TCP_PORT`, which defaults to 5514 (the UDP port number, on its own socket); `0` disables it. A busy port logs one warning and the hub starts anyway.

## Discovery

Discovery is an opt-in scan of the local machine that suggests services for well-known legacy servers (Apache, nginx, IIS, vsftpd, ProFTPD, FileZilla Server, Sendmail, Postfix, Exim, BIND, Samba, Squid, inetd and telnetd, syslog daemons, snmpd). It never runs on its own: no scan at startup, on a timer or as a side effect. Nothing is watched until you accept a suggestion.

Every route needs the admin token (`X-Admin-Token`), including reading the results, because a scan lists the software installed on the machine:

| Route | Does |
|---|---|
| `POST /api/discovery/scan` | Start one scan. `202 {"scan_id", "state": "running"}`; `409` while a scan runs; `503` when the catalog could not be loaded. |
| `GET /api/discovery/scans/{id}` | `state` (`running`, `done`, `failed`), `started_at`, `finished_at`, `limited`, `os`, `reason` and the suggestions. Results are kept in memory for one hour (at most 5 scans). |
| `POST /api/discovery/suggestions/{id}/accept` | Body `{"id", "name", "log_paths": [{"path", "format"}], "code_folder"?, "app_names"?}`. Creates the service and its log sources in one step (`origin: discovery`), or `422` with a `field` and nothing created. |
| `POST /api/discovery/suggestions/{id}/dismiss` | Do not offer this app with these paths again. |
| `GET /api/discovery/dismissals` | List dismissals. |
| `DELETE /api/discovery/dismissals` | Clear all dismissals; the apps are offered again on the next scan. |

```powershell
$h = @{ "X-Admin-Token" = (Get-Content "$env:USERPROFILE\.chimericmesh\admin.token") }
$scan = Invoke-RestMethod -Method Post -Headers $h http://127.0.0.1:8000/api/discovery/scan
Invoke-RestMethod -Headers $h "http://127.0.0.1:8000/api/discovery/scans/$($scan.scan_id)"
```

What a scan does: it runs the operating system's own tools with a fixed argument list, never through a shell, from their system directory (never the current folder or `PATH`), each with a 10 second timeout and 2 MB of output, 30 seconds in total. On Windows `tasklist /fo csv /nh` and `netstat -ano`; on Linux the names in `/proc/<pid>/comm` and `ss -ltnup` (or `netstat -ltnup` when `ss` fails); on macOS `ps -axo pid=,comm=` and `lsof -nP -iTCP -sTCP:LISTEN -iUDP`. It matches process names (and, for protocol-specific ports such as 21, 23 and 25, listening ports) against `src/chimericmesh/catalog/legacy_apps.json`, then looks at each candidate log path with `stat` only. It never reads log contents, never reads command lines, makes no network connection and keeps no process list: a suggestion holds only the matched process name, the lowest pid, the number of matching processes and the listening ports. It runs with your normal rights and never asks for elevation. When some details were not visible (a tool failed or timed out, output was cut, another user's processes are hidden, a candidate list was cut at 25 paths), the result says `limited: true` and gives a `reason`.

Discovery cannot be started by an agent: there is no MCP tool for it, and the results are not exposed to MCP. The dashboard's discovery screen (when built) uses the same routes; you can always add a path by hand with `POST /api/services/{id}/log-sources`.

Limits to know:

- Accept stores the files that exist at accept time, not the pattern. A rotating log needs its new file added by hand; patterns are not stored. This matters for daily files such as IIS `u_exYYMMDD.log` and FileZilla Server logs.
- IIS may not appear: its worker `w3wp` starts only after the first request to a site, and port 80 alone is not evidence of IIS. Make one request to the site, then scan again, or add the IIS log path by hand.
- Discovery never guesses a code folder. Suggestions contain no `code_folder`; you may type one at accept time, and the services rules refuse a home directory, a drive root or the hub's data directory.
- The demo `examples/legacy_app` is not in the catalog on purpose. Add it by hand as a service to show the manual path.
- To add or change an app, edit `catalog/legacy_apps.json` (it is validated at startup; an invalid file disables discovery and nothing else). If the `w3c` or `xferlog` format is ever dropped, change those entries to `plain` in the same change.

Manual checks (not automated):

1. Verify the catalog paths against vendor documentation or a sample machine for each of the 15 entries.
2. On the Windows development machine, scan with a real IIS or Apache if present, otherwise with the demo `legacy_app` running (it is not suggested and is added by hand; `examples/legacy_app/upstream.py` is not changed by discovery). Check that the suggestions and the `limited` flag are sensible.
3. Run once as a non-admin user on Linux and check `limited: true`.
4. Rehearse the demo with nginx or Apache: scan, accept, append an error line, watch the incident.
5. On a Windows machine with IIS, scan once before and once after the first request to the site: `iis` should appear only after `w3wp` runs.
6. Capture real output of `ss -ltnup`, `netstat -ltnup`, `lsof` and `netstat -ano` on a non-English Windows and add them as parser fixtures.

## MCP for Kiro

Run `python -m chimericmesh.mcp_server` (stdio) with `PYTHONPATH=src` and `CHIMERICMESH_URL` pointing at the hub. Add it to `.kiro/settings/mcp.json`:

```json
{
  "mcpServers": {
    "chimericmesh": {
      "command": "python",
      "args": ["-m", "chimericmesh.mcp_server"],
      "env": { "PYTHONPATH": "src", "CHIMERICMESH_URL": "http://127.0.0.1:8000" }
    }
  }
}
```

Tools: `list_incidents`, `get_incident`, `find_code_for_incident`, `recent_events`, `announce_to_irc`, `resolve_incident`.

Optional: set `CHIMERICMESH_WEBHOOK_URL` for webhooks and `CHIMERICMESH_CODE_ROOT` for code lookups.

## How incidents behave

- **Hosts:** Syslog carries a host field. For IRC and Telnet, a leading `hostname:` in the message (for example `web02: upstream timed out` or `ALERT sw-core-1: Failed password`) sets the host. Otherwise the IRC nick or the Telnet peer address is used.
- **Opening:** an incident is announced on IRC (and by webhook) once it has 3 events, or immediately at crit severity or worse.
- **Updates:** follow-up `UPDATE` lines go out when the count passes 10, 50, 100 and 500, so channels do not get flooded.
- **Code lookup:** searches `examples/legacy_app/` by default, a small sample app whose log lines match the demo storm. Point `CHIMERICMESH_CODE_ROOT` at your own source tree to use it for real.

## Screenshots

Captured from the running app in headless Edge.

![Empty dashboard with the guided workflow at 0/6](docs/screenshots/dashboard-empty.png)
![Incident cards after a failure storm](docs/screenshots/dashboard-incidents.png)
![ChimericBot terminal with an approved proposal](docs/screenshots/dashboard-terminal.png)

## Tests

```powershell
python -m pytest -q
```

Includes Hypothesis property tests for parser totality, IRC injection safety, and chunking.

## Kiro usage

- Specs: `.kiro/specs/legacy-mesh/` (requirements, design with correctness properties, tasks)
- Steering: `.kiro/steering/`
- Hooks: `.kiro/hooks/` (tests on save, innerHTML guard)
- MCP: `src/chimericmesh/mcp_server.py`
- Custom agent: `.kiro/agents/mesh-responder.json`
- Power: `powers/chimericmesh-remediation/`

## Kiro agent and power

**Custom agent `mesh-responder`** (`.kiro/agents/mesh-responder.json`) runs the human-approved remediation loop. It defines the `chimericmesh` MCP server inline, loads `.kiro/steering/project.md`, and auto-allows only the read-only MCP tools (`list_incidents`, `get_incident`, `find_code_for_incident`, `recent_events`, `list_proposals`, `get_proposal`). `announce_to_irc`, `propose_remediation`, `complete_proposal`, `resolve_incident`, shell and file writes ask for permission. Select it with the agent picker in Kiro IDE, or `/agent` in Kiro CLI, after trusting the workspace. Start the hub first (`python -m chimericmesh`).

**Power `chimericmesh-remediation`** (`powers/chimericmesh-remediation/`) uses the Agent Plugins layout: `plugin.json`, `mcp.json`, and `skills/` (`getting-started`, `remediation-loop`, `triage`). To install it, open the Powers panel, choose Add Custom Power, then Import power from a folder, and select that directory.

`tests/unit/test_kiro_assets.py` checks both files statically (JSON validity, tool names against the MCP server, consistency with `.kiro/settings/mcp.json`). It does not show that Kiro loads them.
- PBT: `tests/pbt/`

## Agent watch mode

The custom agent **`mesh-watcher`** (`.kiro/agents/mesh-watcher.json`) keeps one Kiro session running as a watcher. It loops on `wait_for_work`, investigates an incident with the read-only tools, sends one `propose_remediation` per incident with `files` (absolute paths inside the service's code folder), `estimated_lines` and `verification`, and edits code only after the hub reports the proposal as approved and `verify_proposal` answers `ok`. Ids are `p1`, `p2` and so on. The power skill `watch-mode` carries the same loop. The existing `mesh-responder` agent, `.kiro/settings/mcp.json` and the power's own `mcp.json` are unchanged.

Setup: trust the workspace in Kiro (an untrusted workspace does not load the profile or its MCP server), start the hub (`python -m chimericmesh`), register a service with a code folder, and select `mesh-watcher`. Tools added for the loop: `list_services`, `get_service`, `service_events`, `explain_log_window`, `find_code_for_service`, `verify_proposal` and `wait_for_work`. They only read; none takes the admin token.

**Workspace root.** The hub finds the workspace root by walking up from its working directory to the nearest folder that has `.kiro/`; set `CHIMERICMESH_WORKSPACE_ROOT` (an existing absolute directory) to override it. The permissions snippet uses it to write workspace-relative paths. `files` in `propose_remediation` and `verify_proposal` are absolute paths; a relative path fails verify.

### Prompt mode (default) and autonomous mode

**Prompt mode (default).** No snippet is pasted. The mesh tools run without a Kiro prompt, but every file edit and every shell command asks you. The approval gate is the hub: you approve a proposal with `!approve <id>` (or the dashboard terminal) and the agent edits only after `get_proposal` and `verify_proposal` agree.

**Autonomous (hands-off) mode.** Paste the snippet from `GET /api/services/<id>/permissions-snippet` (optional `?test_command=...`) into a Kiro `permissions.yaml`. It allows `fs_write` inside the service's code folder, denies writes to protected names (`.env`, keys, `.git`, `.kiro`, `.github`) and has one commented line for a single test command. Nothing writes the file for you; the hub only returns text. Prefer the workspace-scoped file (`~/.kiro/workspace-roots/<hash>/permissions.yaml`, the hash is looked up on your machine). The user-scoped `~/.kiro/settings/permissions.yaml` applies to every session and agent of this user, not only `mesh-watcher`, so use it only on purpose. Hands-off write allows use workspace-relative globs only. Permission rules are static: if the service's code folder changes, the old allow no longer matches and Kiro prompts again until you paste a new snippet.

The snippet is a standing allow. It also applies while a proposal is still `pending`, so in this mode the gate rests on the agent following its prompt, on `verify_proposal` and on the audit trail, not on a Kiro prompt. That the agent cannot approve its own proposal or widen the rules is a design goal with the limits stated below, not a guarantee.

**Hands-off test-command risk (accepted for hands-off mode only).** The one allowed test command in autonomous mode runs code the agent has just written, with your privileges. That code could read the admin token file or call the hub. You accepted this risk for hands-off mode; prompt mode stays the default and has no such command. Pause, revoke and withdraw need no token and can only reduce the agent's permissions; create, widen, re-enable and resume need the token.

### Waiting and timeouts

`wait_for_work` holds the request for 25 seconds at most (the hard ceiling; `CHIMERICMESH_WATCH_MAX_WAIT` can only lower it) and then returns `status: "timeout"`. The MCP server's own client timeout is 35 seconds, and the profile sets `requestTimeout` to 120000 ms (the Kiro default).

Live check, 2026-11-23, Kiro IDE with `mesh-watcher`: one call with the default returned `timeout` after the full wait, with no tool error. No fallback was needed. Which file the IDE reads `requestTimeout` from was **not** checked. It can be set in the profile (`mcpServers.chimericmesh.requestTimeout`) or in `.kiro/settings/mcp.json`; that file is unchanged by this module, so add the value there yourself if the call ever fails with an IDE timeout. As a fallback, set `CHIMERICMESH_WATCH_MAX_WAIT` (for example `10`) in the environment of the hub process, the terminal that runs `python -m chimericmesh`, and restart the hub. Only the hub reads it; an entry in the profile's `env` changes nothing.

### Optional enforcement hook

`.kiro/hooks-optional/` holds `mesh-verify-write.json` and `mesh_verify_write.py`. To opt in, copy both files into `.kiro/hooks/`; to remove it, delete both copies. While it is installed, every write needs an active proposal: the agent writes the proposal id (for example `p4`) to `.chimericmesh-active-proposal` in the workspace root, and the hook asks the hub's verify route about the target file and blocks (exit 2) when the answer is not `ok`, when the state file is missing or bad, or when the hub cannot be reached. The hook sends no token, talks to loopback only, and exits 0 (no block) when its input cannot be parsed. It covers `fs_write`, `fs_append` and `str_replace` only; it does not see shell commands. The state file is written by the agent, so an injected agent can name another approved proposal; verify then still limits the write to that proposal's declared files and folder. The snippet has a commented allow for that one state file. The hook files are not installed by default, and `.kiro/hooks/` is unchanged.

### Limits

- The agent runs as the same OS user as the hub. With a shell command allowed it could read the token file and call the REST API, including the token-free pause, revoke and withdraw routes. Kiro's own prompt on shell commands is the real gate for that.
- The `~/.chimericmesh/**` and token deny globs in the profile are **check live** items: confirm in the IDE that they match. If they do not, what remains is the name-based denies and Kiro's read prompt; the hook covers writes only.
- The `PermissionRequest` hook trigger is not part of V1.
- `verify_proposal` refuses `.env`-type and other protected names inside the code folder for a user approval without declared files, but it cannot see edits made through a shell command.
- In an in-memory (bare) hub the proposals list keeps the latest 200, so a user-denied proposal that falls off it makes its incident show up as work again. With the store, every proposal is kept.
- Code search and digests read only the service's code folder and its log text, and treat all log text as untrusted.
- **Demo precondition.** Restore the defect in `examples/legacy_app/upstream.py` yourself before the demo, for example with `git checkout -- examples/legacy_app/upstream.py`. Nothing in this module runs that command. The demo test `examples/legacy_app/tests/test_resilience.py::test_long_outage_keeps_running_with_bounded_positive_delays` fails by design until the upstream client has a bounded backoff; the root `python -m pytest -q` does not collect it.
## Standing approval rules

A standing rule lets the hub approve a fix without asking each time. The hub decides, never the agent: the agent has no tool to create, edit, widen or resume a rule, and "approved by a rule" is read from the `approved_by_rule` column of the proposal, never from the decider text.

**What a rule covers.** One service (it must have a code folder), plus either one incident signature or one category (`memory`, `disk`, `network`, `security`, `crash`, `unknown`), and a most-urgent severity (3 or 4; severity 0 to 2 is never covered). A proposal is approved by the rule only when every condition holds: the hub is not paused, the rule is active and not expired, the proposal says `kind` `code_change`, the incident belongs to the rule's service and matches, the hourly and per-incident limits have room, it declares 1 to `max_files` files that all lie inside the service's code folder with a source-file extension (no `.git`, `.kiro`, `.github`, `.env`, keys; no `.json`, `.yaml`, `.ini`, `.conf`, `.toml`), declares `estimated_lines` up to `max_lines`, names a `verification`, and the service's code folder is still the one the rule was made for.

**Limits (hub-side).** At most 10 proposals per service per hour (all proposals, not only rule approvals), `max_per_hour` 1 to 10 (default 3), `max_per_incident` 1 to 3 (default 1) per opening of an incident, `max_files` 1 to 20 (default 5), `max_lines` 1 to 500 (default 100), expiry at most 30 days (default 7). One rule approval is valid for 30 minutes; after that it is denied by the hub (`rule-expired:<id>`). A pending proposal older than `CHIMERICMESH_PROPOSAL_TTL_HOURS` (default 24) is denied (`expired:ttl`). 20 rules per service and 100 in total.

**Cooldown.** If an incident reopens within `CHIMERICMESH_AUTOFIX_COOLDOWN_SECONDS` (default 86400, 0 to 2592000) of an auto-fixed proposal being completed, the next proposal for it needs your approval. The hub announces it with the normal `PROPOSAL ... reply !approve` line plus a note and writes an `autofix_cooldown` audit row. The reopen time is the moment the hub first saw the new opening, which may be up to 60 seconds late.

**Creating a rule.** Approve a proposal as usual, let the fix pass its verification, then `GET /api/proposals/<id>/rule-defaults` shows the suggested rule and `POST /api/proposals/<id>/rule` (admin token, optional overrides in the body) creates it. `POST /api/rules` creates one directly. Create, widen (`PATCH /api/rules/<id>`), re-enable (`POST /api/rules/<id>/enable`) and resume (`POST /api/rules/resume`) need the token. Only a proposal approved by a person and completed with `verification_result` `passed` can seed a rule.

**Emergency stop, no token.** Pause, revoke and withdraw need no token, are never refused, are rate limited in the audit trail (ten rows a minute per caller, then one coalesced row) and are announced: `!rules`, `!revoke <rule id>`, `!pause` and `!withdraw <proposal id>` on IRC or the dashboard terminal for any registered nick, and `POST /api/rules/<id>/revoke`, `POST /api/rules/<id>/pause`, `POST /api/rules/pause`, `POST /api/proposals/<id>/withdraw` over REST. Revoking or suspending a rule denies its approved proposals at once (`rule-revoked:<id>`, `rule-suspended:<id>`). Changing or removing a service's code folder, or deleting the service, suspends its rules the same way. `!pause` stops all auto-approval without changing any rule; only `POST /api/rules/resume` (token) ends it. Every automatic decision is announced as `AUTO-APPROVED p3 by rule r1 (service api-gw): <action>. Revoke with !revoke r1` (nobody has to reply) and written to the audit log (`GET /api/audit`).

**Completion check.** When a rule-approved fix is completed with `files_changed`, `lines_changed` or `verification_result`, the hub compares them with the rule. A file outside the code folder or on the protected list, too many files, too many lines or a failed verification completes the proposal but suspends the rule (`rule_suspended` row, one announcement). A completion without details is recorded as unverified and leaves the rule active.

**Reserved decider names.** The prefixes `rule:`, `rule-`, `withdrawn:`, `expired:` and `expired-` (any case) are used only by the hub's own rule logic. They are refused as the `by` of `POST /api/proposals/<id>/decide` and as IRC nicks.

**Honest limits.** The hub checks the files a proposal and a completion declare; the hub does not stop file writes outside the code folder, because it never executes anything. The agent runs as the same OS user as the hub and could read the token file, so Kiro's shell permission prompt is the real gate for that. In short: pause, revoke and withdraw need no token; create, widen, re-enable and resume need the token.

## Security notes

Intended for localhost. Changes that widen what can happen (approving, denying, editing services) need the admin token; see "Access control". Log text is rendered as text only, and outbound IRC is stripped of control characters.

## Access control

`python -m chimericmesh` turns access enforcement on. A test or embedding that passes its own hub to `create_app` gets it off unless it asks for `enforce_access=True`.

- **Admin token.** A random token lives in `~/.chimericmesh/admin.token` (owner-only permissions). Set `CHIMERICMESH_TOKEN_FILE` to use another path. The startup log prints the file's path, never the token. `python -m chimericmesh token --rotate` writes a new token and prints only the path. A running hub keeps its old token, and the new one takes effect at the next hub start. With `CHIMERICMESH_DB=:memory:` the token exists only in memory and no file is written.
- **What needs the token.** Creating, editing or deleting services and log sources (`/api/services...`), `POST /api/proposals/{id}/decide`, and `!approve` / `!deny` in the dashboard terminal. Clients send it in the `X-Admin-Token` header. Reads, ingest, creating or completing proposals, `!resolve` and the demo routes need no token.
- **Reducing operations are open.** Pause, revoke and withdraw need no token and are open to any local caller, including the agent from a shell. The cost is that a local process can stop auto-approvals or withdraw proposals: an annoyance, not escalation. Resume and every other rule change need the user.
- **Honest limit.** The token keeps out web pages and local processes that cannot read your files. It does not stop the agent, which runs as you and can read the token file. Kiro's shell prompt is the real gate for what the agent does.
- **Host check.** Requests to `/api/*` must carry a `Host` of `127.0.0.1`, `localhost` or `[::1]` (any port), which blocks DNS rebinding. `CHIMERICMESH_ALLOWED_HOSTS` (comma-separated) replaces that list. `CHIMERICMESH_BIND` sets the listen address; a non-loopback value logs a warning, because the dashboard is then reachable from other machines.
- **IRC operators.** `CHIMERICMESH_OPERATORS` lists the nicks that may use `!approve`, `!deny` and `!resolve`. With enforcement on, an IRC client must also set the server password to the admin token (`PASS`, for example `/server 127.0.0.1 6667 <token>`). The bundled `scripts/irc_client.py` does this with `--password <token>` or `--token-file` (reads the admin token file; honours `CHIMERICMESH_HOME` and `CHIMERICMESH_TOKEN_FILE`) and never prints the secret. If `CHIMERICMESH_OPERATORS` is unset, operator commands are refused on IRC. The nicks `dashboard` and `ChimericBot` are reserved.
- **Services and incidents.** Two services behind one sender host that log the same error signature share an incident; it belongs to the service of its first tagged event.

## License

MIT

## Talking to the mesh over IRC

IRC is a two-way operator interface. Registered clients can send commands (messages starting with `!`) in a channel the bot is in. Commands are never ingested as log events. Replies are posted to the channel, prefixed `[mesh]`.

| Command | Who | What it does |
| --- | --- | --- |
| `!help` | anyone | List commands |
| `!status` | anyone | Events seen, open incidents, pending proposals |
| `!incidents` | anyone | Up to 5 open incidents: `id category host xCOUNT severity` |
| `!explain <id>` | anyone | Hypothesis and up to 4 remediation steps |
| `!proposals` | anyone | Pending remediation proposals |
| `!resolve <id> [note]` | operator | Resolve an incident |
| `!approve <pid>` / `!deny <pid>` | operator | Decide a proposal |

Operators are set with `CHIMERICMESH_OPERATORS` (comma-separated nicks, case-insensitive) and, with enforcement on (the default for `python -m chimericmesh`), must also send the admin token as the server password; if it is unset or empty, operator commands are refused. Only with enforcement off (an injected hub, as in tests) does an unset list mean any registered nick counts as an operator. See "Access control".

### Human-approved remediation loop

```
agent (MCP)                 ChimericMesh / IRC #ops              human
-----------                 ----------------------               -----
list_incidents, get_incident, find_code_for_incident
propose_remediation  ---->  "PROPOSAL p1 for <id> ... "  ---->  reads it
get_proposal (poll)  <----  status: pending                      !approve p1  (or !deny p1)
get_proposal         <----  status: approved   <-----------------
(agent does the work itself)
complete_proposal    ---->  "p1 DONE: <note>"
resolve_incident     ---->  "RESOLVED <id> ..."
```

REST: `GET/POST /api/proposals`, `GET /api/proposals/{pid}`, `POST /api/proposals/{pid}/decide`, `POST /api/proposals/{pid}/complete`. MCP tools: `propose_remediation`, `get_proposal`, `list_proposals`, `complete_proposal`.

Safety: ChimericMesh never executes a proposed action on any host. It only records the human decision and announces it. The agent performs the real work under its own tool permissions, and must act only when the proposal is `approved`, never while it is `pending` or `denied`. Proposals are kept in the local database (see "Data and proposal ids"); an injected in-memory hub keeps the last 200.

## A real incident, a real fix

`examples/legacy_app` is a runnable service with a real bug (see its README). To fix it through the approval loop:

1. Run the hub: `python -m chimericmesh`.
2. Run the service: `python examples/legacy_app/run.py`. It sends syslog to the hub.
3. Open the dashboard and wait for the `network` incident about the upstream timeout.
4. In Kiro, pick the `mesh-responder` agent and ask it to investigate and propose a fix.
5. Approve with `!approve p1` in the dashboard terminal: it needs the admin token, and the page asks for it once (paste the contents of `~/.chimericmesh/admin.token`). On IRC, set the server password to the admin token and use a nick listed in `CHIMERICMESH_OPERATORS`.
6. The agent edits the code, runs `python -m pytest examples/legacy_app/tests -q`, restarts nothing on its own, and reports the event rate. Restart `run.py` to load the fix and watch the rate fall.
7. Reset the buggy state: `git checkout -- examples/legacy_app` and `git clean -fd examples/legacy_app`.

## The dashboard terminal and guided workflow

The dashboard has a ChimericBot terminal: a transcript of everything the bot says plus an input line that accepts the same `!` commands as IRC (`!help`, `!status`, `!incidents`, `!explain <id>`, `!proposals`, `!approve`, `!deny`, `!resolve`). Dashboard buttons just send these commands, so every action appears in the transcript and one authorization check applies everywhere. Replies are also broadcast to IRC.

Web commands run as the fixed nick `dashboard`; the browser cannot choose a nick. With enforcement on, `!approve` and `!deny` in the terminal need the admin token instead of an operator nick (the page asks for it once and keeps it for the tab), so `CHIMERICMESH_OPERATORS` only governs IRC and `!resolve`.

The guided workflow (`GET /api/workflow`) has six steps:
1. Send some traffic (any event ingested)
2. Spot an incident
3. Ask ChimericBot (run any command)
4. Propose a fix
5. Approve or deny it
6. Lay the incident to rest (resolve)

Endpoints: `GET /api/chat?after=&limit=`, `POST /api/terminal {"text": "!status"}`, `GET /api/workflow`.

Origin check: POST/PUT/PATCH/DELETE requests carrying an `Origin` header that differs from `Host` get 403, which blocks other websites from driving a local instance. Clients that send no Origin (curl, scripts, the MCP server) are unaffected.

Security note: IRC nicks are unauthenticated, so `CHIMERICMESH_OPERATORS` is only a guard on trusted networks, not real authentication.
