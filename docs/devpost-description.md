# Devpost submission text (draft)

Paste each section into the matching Devpost field. Edit freely; every claim here is backed by the repo (see `docs/kiro-usage.md` for commit evidence).

## Tagline (one line)

Old protocols, new agents: a Kiro agent fixes your legacy app's code from its Syslog, Telnet and IRC logs, and you approve every change.

## The 30-second version (top of the description)

**What it does.** ChimericMesh is a local hub that listens to the old protocols legacy apps still speak (Syslog over UDP and TCP, Telnet, IRC, plain server log files), turns their errors into incidents, and hands them to Kiro agents through MCP. The agent finds the bug in the app's code and proposes a fix. Nothing changes until a human approves it, on the dashboard or by typing `!approve` in IRC.

**Who it is for.** Developers who keep a legacy service alive, often alone: an old proxy, mail server, FTP box or in-house daemon whose logs nobody watches and whose code nobody wants to touch.

**Why it matters.** AI coding agents cannot see these systems. ChimericMesh is the bridge, and it keeps a person in charge of every change.

## Inspiration

Plenty of real infrastructure still logs over Syslog or chats status into IRC. Those systems fail quietly. We wanted the most modern tool we had, an AI agent in Kiro, to be able to watch them, explain them and fix them, without giving it free rein over production code.

## What it does

- **Listens to legacy protocols** on your machine: Syslog (UDP and TCP, RFC 3164 and 5424), Telnet, IRC (with a small built-in IRC server), and log files in syslog, Apache/nginx, IIS (W3C) and FTP (xferlog) formats, following them through log rotation.
- **Discovers legacy apps** already running on your machine (opt-in scan) and suggests their usual log paths.
- **Groups errors into incidents** per service and explains the likely cause.
- **Lets a Kiro agent fix the code.** The `mesh-watcher` agent waits for new incidents, searches the service's code folder, proposes one concrete change naming the files and how it will be verified, and acts only after approval.
- **Keeps a human in the loop.** Approve on the dashboard (unlocked with an admin token) or with `!approve` on IRC. Approvals are re-checked right before any edit.
- **Standing rules for repeat bugs.** You can say "always approve fixes like this one" for a service, with limits (files, lines, per hour, expiry). A bug that comes back within 24 hours of an auto-fix needs you again. Pause, revoke and withdraw work instantly for anyone; creating or widening a rule needs you.
- **A dashboard** with a guided first-run walkthrough, the ChimericBot terminal, live log streams per service, incidents, proposals, rules and a full audit trail.

## How we built it (with Kiro)

Python and FastAPI with SQLite, a dependency-free HTML dashboard, and an MCP server built on the MCP Python SDK. All of it was built in Kiro:

- **Spec-driven development:** a baseline spec, then seven module specs (requirements in EARS form, design with correctness properties, tasks), each approved before code (`.kiro/specs/`).
- **Steering:** project rules every agent follows, such as "log text is untrusted" (`.kiro/steering/`).
- **Hooks:** a command hook that blocks unsafe HTML in the dashboard, a fast test-on-save hook, and an opt-in hook that blocks file writes not covered by an approved proposal (`.kiro/hooks/`, `.kiro/hooks-optional/`).
- **MCP:** 17 tools connecting Kiro agents to the hub (`src/chimericmesh/mcp_server.py`).
- **Custom agents:** `mesh-responder` and `mesh-watcher` (`.kiro/agents/`), run live in both **Kiro IDE and Kiro CLI**.
- **A Kiro power:** `chimericmesh-remediation` packages the MCP server with skills for triage and the remediation loop (`powers/`).
- **Property-based testing:** Hypothesis properties tied to the design's correctness properties caught real bugs that review missed.
- **Kiro workflows and sub-agents:** each module was built test-first by a coder agent and checked by an independent reviewer agent, in its own git branch, merged only after human review.

The full story, with commit evidence and exported Kiro sessions, is in `docs/kiro-usage.md` and `docs/kiro-sessions/`.

## Challenges we ran into

- **Real logs broke our assumptions.** Replaying public Loghub and Ubuntu IRC data exposed six bugs our hand-written examples never showed.
- **The agent trusted a stale approval.** After a hub restart reused a proposal id, the agent believed an old approval still applied. We made proposal ids permanent and made the agent re-read the proposal right before every edit.
- **Our first safety hook did nothing.** It was a prompt-style hook that fired on every write and enforced nothing; we replaced it with a deterministic script and tests.

## Accomplishments we're proud of

- The full loop works on a real buggy app: incident, diagnosis, proposal, human approval, code fix, tests, and the error rate actually falling.
- 3,308 automated tests, including property tests that found bugs a reviewed design still had (event ids repeating after two crashes in a row).
- The same agents, steering and MCP config work unchanged in Kiro IDE and Kiro CLI.

## What we learned

Writing specs first cost hours and saved days. The real limit on speed was review and decisions, not typing code. And an agent is only as safe as the gate it cannot move: in ChimericMesh, the hub holds the gate, not the prompt.

## What's next

SNMP traps and Windows Event Log intake, configuration and restart fixes (not just code), and a hosted relay for teams.

## Built With

kiro, kiro-cli, python, fastapi, uvicorn, sqlite, mcp, hypothesis, pytest, httpx, pydantic, javascript, html, css, syslog, irc, telnet

## Try it (testing instructions)

ChimericMesh runs locally. Requires Python 3.11+.

```
git clone https://github.com/HarshitDhaduk/chimericmesh
cd chimericmesh
python -m venv .venv
.venv\Scripts\activate        # macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
python -m chimericmesh
```

Open http://127.0.0.1:8000, click **Summon a failure storm**, and follow the guided steps. For the real bug-fixing loop, run `python examples/legacy_app/run.py` and open the repo in Kiro with the `mesh-watcher` agent; see the README section "A real incident, a real fix". The admin token is created on first start in `~/.chimericmesh/admin.token` (no account or login needed).
