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
- PBT: `tests/pbt/`

## Security notes

No authentication; intended for localhost or trusted networks. Log text is rendered as text only, and outbound IRC is stripped of control characters.

## License

MIT
