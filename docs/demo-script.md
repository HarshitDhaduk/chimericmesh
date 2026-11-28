# Demo video script (target 2:45, hard limit under 3:00)

Voice-over budget: about 2.4 words per second. The draft below has 301 spoken words, about 2:05 of speech. Section 2 is deliberately light, because the agent steps and the falling rate need silent screen time (about 45 s). Each section lists its budget and its counted draft.

| Section | Time | Budget | Draft words |
|---|---|---|---|
| 1. Cold open, then problem | 0:00 to 0:20 | 48 | 40 |
| 2. Live loop | 0:20 to 2:05 | 250 | 162 |
| 3. Built with Kiro | 2:05 to 2:40 | 84 | 82 |
| 4. Close | 2:40 to 2:45 | 12 | 10 |

Everything on screen is recorded on the target machine (Windows 11, Kiro IDE, a browser, a terminal). The agent steps take real time, so cut the waiting with a hard cut or a visible "sped up 4x" label. Do not edit the result: the rate must really fall.

## 1. Cold open, then the problem (0:00 to 0:20, 44 words)

Judges' advice (Devpost, 26 Nov): show the project working in the first 10 to 15 seconds. So the video opens on the payoff, cut from the section 2 takes, and only then names the problem.

| Time | On screen | Voice-over | Command or click |
|---|---|---|---|
| 0:00 to 0:10 | Fast montage from the section 2 footage: a `network` incident card appears, the Kiro chat shows "propose a fix to upstream.py", a click on Approve, the Events / sec tile falling to zero. On-screen text: "A legacy app breaks. A Kiro agent fixes its code. You approve." | "This legacy app is failing. A Kiro agent finds the bug in its code, I approve the fix, and the errors stop." (21) | Edit only: four 2-second cuts from section 2. Nothing is recorded for this row. |
| 0:10 to 0:20 | Title card "ChimericMesh", then the dashboard Overview. | "Old apps still log over Syslog, Telnet and IRC, where no AI agent can see them. ChimericMesh connects them." (19) Then on-screen text only: "For developers keeping a legacy service alive." | Title card from a still frame. Browser at `http://127.0.0.1:8000/`, Overview. |

## 2. The live loop (0:20 to 2:05, 161 words)

| Time | On screen | Voice-over | Command or click |
|---|---|---|---|
| 0:20 to 0:28 | Terminal A: the hub is ALREADY running (do not film start-up or setup); scroll its log for a second. On-screen text: "Hub on localhost: Syslog, Telnet, IRC". | "The hub is running on my machine and listens for Syslog, Telnet and IRC." (14) | Start the hub before recording: `python -m chimericmesh` from the repo root, with `CHIMERICMESH_HOME` and `CHIMERICMESH_OPERATORS=operator` set (see the checklist). Register the `proxy` service beforehand (checklist item 3), but leave the page locked so the unlock happens on camera at 1:02. |
| 0:28 to 0:42 | Terminal B runs the legacy app. Browser: Events / sec tile climbs, then a `network` incident card appears: "upstream timed out connecting to 127.0.0.1:19099". | "Here is a real legacy proxy worker. It cannot reach its upstream, and it retries as fast as it can. Within seconds the hub opens an incident." (27) | Terminal B: `python examples/legacy_app/run.py`. Browser: Overview, then click the incident card. |
| 0:42 to 1:02 | Kiro IDE, agent `mesh-watcher` already running. It picks up the incident, reads it, searches the code. The chat shows it naming `upstream.py` and a `propose_remediation` call. | "In Kiro, the mesh-watcher agent is waiting on the hub. It picks up the incident, explains what it sees, and proposes a fix: add a bounded backoff to upstream.py. It declares the file, the size of the change and the test command." (42) | In Kiro IDE: select agent `mesh-watcher`, send "Start watching." once before recording. On screen: its `wait_for_work`, `get_incident`, `find_code_for_service`, `propose_remediation` calls. |
| 1:02 to 1:22 | Browser: Services view, the `proxy` card, the proposal card with Approve and Deny greyed out. Click Unlock, paste the token, Save. Approve turns active. Click Approve. | "The agent cannot change anything yet. The proposal is pending, and Approve is locked until I unlock the page with the admin token. I approve it." (26) | Browser: `#/services`, open the `proxy` card. Top bar: click `Unlock`, paste from the clipboard, `Save`, then `Approve` on proposal `p1`. Alternative below. |
| 1:22 to 1:45 | Kiro IDE: the agent re-checks `get_proposal` and `verify_proposal`, edits `examples/legacy_app/upstream.py`, asks to run the tests, and the tests pass. | "Only now does the agent act. It checks the approval again, edits upstream.py, and runs the example's tests. They pass." (20) | In Kiro IDE: click Allow on the shell prompt for `python -m pytest examples/legacy_app/tests -q`. |
| 1:45 to 2:05 | Terminal B: stop and restart the worker. Browser: Events / sec drops. Kiro: the agent reads the lower rate, calls `complete_proposal` and `resolve_incident`. Dashboard: the incident shows resolved. | "I restart the worker to load the fix. The event rate falls. The agent checks it, completes the proposal and resolves the incident. If the rate had not fallen, it would have refused." (33) | Terminal B: `Ctrl+C`, then `python examples/legacy_app/run.py`. Browser: Overview, watch `Events / sec`. |

Alternative approval on IRC (use if the dashboard is not on screen): in Terminal C, `"!approve p1" | python scripts/irc_client.py --token-file --nick operator`. It needs `CHIMERICMESH_OPERATORS=operator` in the hub's environment and prints the bot's reply (`--channel` defaults to `#ops`, `--port` to 6667). Check the proposal id first with the dashboard or `get_proposal`; it is `p1` only on a fresh database.

Optional auto-approval (10 s, only if the cut is under 2:45): after the fix, on the proposal card click `Always approve fixes like this`, confirm the pre-filled rule, then open `#/rules`. In a later take, a second proposal of the same kind shows the line `AUTO-APPROVED p2 by rule r1 ...` in the terminal transcript. Say: "A standing rule can approve a repeat of the same fix. Every automatic decision is announced and audited." (18 words, not counted above.) Skip it if it risks the time limit.

## 3. How it was built with Kiro (2:05 to 2:40, 82 words)

| Time | On screen | Voice-over | Command or click |
|---|---|---|---|
| 2:05 to 2:13 | Editor tree: `.kiro/specs/` with eight folders, one open to `requirements.md`, `design.md`, `tasks.md`. | "We wrote specs before code: seven module specs, each with requirements, design and tasks." (14) | Kiro IDE file tree: `.kiro/specs/store`. |
| 2:13 to 2:21 | `.kiro/steering/project.md`, then `.kiro/hooks/guard-innerhtml.json`. | "Steering holds the project rules. A hook blocks innerHTML in the dashboard. Our first hook was an agent prompt, enforced nothing, and we replaced it with a script." (28) | Open both files. |
| 2:21 to 2:25 | `.kiro/agents/mesh-watcher.json`, `powers/chimericmesh-remediation/`, `src/chimericmesh/mcp_server.py`. | "Two custom agents, a power and an MCP server connect Kiro to the hub." (14) | Open the three paths. |
| 2:25 to 2:29 | Terminal: Kiro CLI V3 with `mesh-watcher` selected, the reply `Status: timeout` after `wait_for_work`. | "The same agent runs from Kiro CLI, unchanged." (8) | Pre-recorded: `kiro-cli` in the repo, `/agent` to `mesh-watcher`, ask it to call `wait_for_work` once. |
| 2:29 to 2:40 | Terminal: pytest summary line, or `tests/pbt/` listing. | "Property tests found real bugs, such as event ids repeating after two crashes. We also replayed real public logs, and they broke four of our assumptions." (26) | Show a pasted summary: `3308 passed, 9 skipped` from `.kiro/specs/dashboard/tasks.md`, or run `python -m pytest tests/pbt -q` before recording and show that line. |

(Section 3 is 90 words with the CLI line; trim "such as event ids repeating after two crashes" if the cut runs long.)

## 4. Close (2:40 to 2:45, 10 words)

| Time | On screen | Voice-over | Command or click |
|---|---|---|---|
| 2:40 to 2:45 | Dashboard Overview, resolved incident. Text overlay: repo URL and "Kiroween 2026, Frankenstein". | "ChimericMesh: old protocols, new agents, a human in the loop." (10) | Static overlay. No music, or music you made yourself. |

## Before recording

1. Make sure the shipped bug is in place: `git status --short examples/legacy_app` prints nothing. If the agent fixed it in an earlier take, run `git checkout -- examples/legacy_app` and `git clean -fd examples/legacy_app`. Then `python -m pytest examples/legacy_app/tests -q` shows 5 failed (the resilience tests), which is what the agent has to fix.
2. Start from a fresh data folder: `$env:CHIMERICMESH_HOME = "$env:TEMP\cm-demo"` and `$env:CHIMERICMESH_OPERATORS = "operator"` in Terminal A, then `python -m chimericmesh`. A fresh database makes the first proposal `p1`.
3. Register the service once before recording (it is not part of the take, or show it as a cut): browser, `#/services`, `Add service`. Id `proxy`, Name `Proxy worker`, App names `proxy-worker`, Code folder the absolute path to `examples\legacy_app`. Unlock first. The legacy app logs as app `proxy-worker`.
4. Token ready: `Get-Content "$env:CHIMERICMESH_HOME\admin.token" | Set-Clipboard`. Never show the token file on screen. Check that the unlock field does not display the pasted value; if it does, pause the recording there.
5. Kiro IDE: workspace trusted, `mesh-watcher` selected, the MCP server `chimericmesh` shows as connected. Run the agent once before the take so the first `wait_for_work` is warm.
6. Windows to arrange, left to right: browser (dashboard), Kiro IDE, a small terminal for Terminal A (hub) and Terminal B (worker). Zoom the browser and IDE to 125 percent so text is readable at 1080p.
7. Clean screen: close other tabs and notifications, hide personal paths and the email in the title bar, and use a plain terminal theme.
8. No third-party logos or copyrighted music. Avoid showing an IRC client's branding; `scripts/irc_client.py` is plain text. Kiro's own UI is on screen, which is expected.
9. Upload to YouTube as Public (not Unlisted), duration under 3:00, English narration or captions, and test the link in a private window.
10. After the take, restore the bug again if you plan a second take.
11. Recording style (Devpost's video advice, 26 Nov):
    - Record each table row as its own short clip, so a bad clip can be redone alone.
    - Never type live: paste prompts and the token, or cut straight to the result.
    - Cut every wait. The 25-second `wait_for_work`, test runs and model thinking become hard cuts, or a labelled "4x" speed-up.
    - Use jump cuts to remove pauses.
    - Put one short on-screen caption per beat, for example "Approve is locked until a human unlocks it".
    - Keep at least one Kiro artifact on screen: section 3 shows a spec, a hook and the steering file.
    - Leave the team story for the written description.

## Fallback plan

| Step that can fail | What to do |
|---|---|
| Hub does not start (port in use) | Stop the process using 8000, 5514 or 6667, or move the hub with `CHIMERICMESH_HTTP_PORT`, `CHIMERICMESH_SYSLOG_PORT` and `CHIMERICMESH_IRC_PORT`. If you move the syslog port, run the worker with `--hub 127.0.0.1:<port>`. |
| No incident within 15 s | Check the worker prints the hub address (`127.0.0.1:5514`). Send the demo storm instead: the dashboard button "Summon a failure storm". The incident will not name `upstream.py`, so say so and keep the live loop only if the agent still finds the code hint. |
| The service is not registered, so the agent sees no work | Register `proxy` (checklist item 3) and restart the worker. |
| The agent does not pick up the incident | Say "Start watching." again, or call `list_incidents` by hand. Fallback: switch to `mesh-responder` and ask it to investigate the open incident (README, "A real incident, a real fix"). |
| `wait_for_work` times out in the IDE | Set `CHIMERICMESH_WATCH_MAX_WAIT=10` in Terminal A and restart the hub (README, "Waiting and timeouts"). |
| The agent proposes something else | Click Deny, say "Fix upstream.py with a bounded backoff", and let it propose again. Cut the denied proposal if the time is short. |
| Approve stays greyed out | The page is locked. Click Unlock and paste the token. If it says "token rejected", the hub was started with another `CHIMERICMESH_HOME`: re-copy the token. |
| IRC approval fails | Use the dashboard. For IRC, check `CHIMERICMESH_OPERATORS` includes the nick and that `--token-file` points at the hub's token. |
| The agent's `verify_proposal` fails | It will stop and report. Read the reason on screen, correct the code folder on the service, and propose again. |
| Tests fail after the edit | Do not resolve. The agent should say the rate did not fall. Reset the bug and retake. Keep the failure as a short beat only if it still fits the time. |
| The rate does not fall after restart | Check the worker running is the restarted one, and wait about 10 seconds for the tile to average. Otherwise retake. |
| Kiro IDE freezes or the video exceeds 3:00 | Record the loop in two takes and cut at the approval. Speed up agent thinking with a labelled 4x cut. Drop the optional auto-approval beat first, then shorten section 3. |
| A live run cannot be made at all | Do not fake it. Record the dashboard flow with the demo storm and say plainly that the fix step is shown from an earlier recorded run. |

## Word count check

Count the voice-over cells before recording a take. Section totals above were counted by script from the voice-over cells: 48, 161, 82 and 10, 301 in all. Read the script aloud once with a timer. If a section runs long, cut words, not the pause where the rate falls.
