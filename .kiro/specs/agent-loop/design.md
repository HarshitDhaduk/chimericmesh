# Design: agent-loop (V1)

Requirements: [requirements.md](requirements.md). Capability map: [docs/v1-capability-map.md](../../../docs/v1-capability-map.md) (module `agent-loop`, decisions D1 to D5, "Honest limits of the access model", B6, B7, B8, B18). Baseline: [legacy-mesh](../legacy-mesh/design.md) R4, R7, R10.4. Fixed inputs, used as written: the approved designs of [store](../store/design.md), [services](../services/design.md) and [connectors](../connectors/design.md), and the finished designs of [auto-approval](../auto-approval/design.md) and [discovery](../discovery/design.md). Additions this module would need from them are listed under "Needs from other modules" and are flagged for the user. No other design is changed.

## Overview

`agent-loop` lets one Kiro session run as a watcher. It adds four things: (1) six **read-only REST routes** with the hub code behind them (service events, a deterministic digest, code search in a service's code folder, `verify`, the long poll `GET /api/watch/next`, and the permissions snippet); (2) seven **thin MCP wrappers** (`list_services`, `get_service`, `service_events`, `explain_log_window`, `find_code_for_service`, `verify_proposal`, `wait_for_work`) plus additive parameters on `propose_remediation` and `complete_proposal`; (3) the **agent profile** `mesh-watcher` and the power skills; (4) **Kiro permission guidance** (the snippet, the documented modes, an optional write-verification hook). It adds no table, no setting that widens anything, no process and no thread except one short-lived worker thread for code search.

Six decisions shape the rest:

1. **One package, one facade, pure cores.** All hub-side code lives in `src/chimericmesh/agent/*` (group `agent-loop`). Every rule is a pure, total function (`evaluate_work`, `evaluate_verify`, `build_digest`, `search_code`, `build_snippet`, `clean_text`, `cap_items`), and `AgentService` is the only class that touches ports. Property tests need no hub. Alternative rejected: put the logic in `api.py` (wiring), which would make it untestable without the whole app and would break the import rule (M2).
2. **Three injected ports, no `hub` import (resolves M2).** `AgentService` receives `StateView` (store), `ServiceRegistry` (services) and an `ApprovalReader` (a three-method protocol that `ApprovalManager` already satisfies; R1-L2). It reads events, incidents and proposals only through `StateView`, so the same code runs against a plain `Hub()` and against the store-backed hub; task 12.1 runs one parametrized module over both modes.
3. **Long poll = evaluate, then wait on an event with a 0.5 s tick.** `WatchQueue.next` evaluates the work set synchronously (no `await`), returns at once when there is work, and otherwise waits on a per-waiter `asyncio.Event` for `min(0.5 s, time left)` and evaluates again. `kick()` sets the events (used by tests and available to a future hub listener); without any hub notification the latency is at most 0.5 s, which meets Success Criterion 4 (1.5 s). This avoids a fourth edit of the hub's contested region (open item O9). No thread, no blocking sleep, no ingestion stall.
4. **Verification is a pure function over a snapshot.** `AgentService.verify` copies the proposal, the approval view, the rule, the pause flag, the live code folder and the clock into a frozen `VerifyInputs`, then calls `evaluate_verify(inputs, files)`. The six checks of R3.1 are the sub-criteria 3.1a to 3.1f (L8). It never reads `decided_by`.
5. **The REST layer is where text becomes safe.** Every log-derived string in a response of a route this module owns is stripped of control characters and cut to 500 characters, and a response is cut to 20 KB, in `agent/limits.py` (R1.7). The MCP wrappers pass parameters and results through.
6. **Nothing here can widen anything.** No route takes the admin token, none writes, no tool touches rules, services, tokens or discovery. The profile has no allow rule for `fs_write` or `shell`. The optional hook is a second layer, never the primary gate.

Technology stack (locked on approval): Python 3.11+, stdlib (`asyncio`, `threading`, `os`, `json`, `re`, `urllib.request` in the hook script only), FastAPI and pydantic in `agent/routes.py` only, `httpx` and the MCP SDK v2 (`from mcp.server.mcpserver import MCPServer`) in `mcp_server.py` only, pytest and Hypothesis. No new dependency.

## Architecture

### Where `agent-loop` sits

```
core -> store -> services -> connectors, discovery, auto-approval -> agent-loop (agent/*) -> dashboard (REST/SSE only)
                                                                          ^
wiring (hub.py, api.py, commands.py, listeners.py, mcp_server.py, __main__.py) builds and injects
```

`agent/*` imports `core`, `store`, `services` and `approvals` (map dependency table). It imports no `hub`, `api`, `commands`, `listeners`, `mcp_server`, `connectors`, `discovery`, `services.deps` or `services.routes`. Exactly what it uses from `approvals`: `approvals.scope` (`PROTECTED_PATTERNS`, `SOURCE_SUFFIXES`, `check_file`, `glob_to_regex`) and `approvals.models.Rule` (type only). `ApprovalManager` itself is never imported; the facade sees it through the `ApprovalReader` protocol.

### File-to-group rows added by this module

| File | Group | Notes |
|---|---|---|
| `agent/__init__.py` | `agent-loop` | Pure re-exports. Imports no `fastapi`, `starlette`, `pydantic`. |
| `agent/errors.py`, `limits.py`, `ports.py`, `feed.py`, `digest.py`, `codesearch.py`, `verify.py`, `watch.py`, `snippet.py`, `service.py` | `agent-loop` | stdlib, `core`, `store`, `services` (stdlib-only submodules), `approvals` (`scope`, `models`). |
| `agent/routes.py` | `agent-loop` | The only file that may import `fastapi`, `starlette` or `pydantic`. Gets a row in the per-file third-party allow-list of the dependency test. |

`api.py` and `mcp_server.py` stay `wiring`. `diagnostics.py` stays `core`; `CODE_SUFFIXES` and `SKIP_DIRS` become public there through `auto-approval` task 1.1 (L4); this module only imports them.

### Package layout

```
src/chimericmesh/agent/
  __init__.py     re-exports
  errors.py       AgentError and subclasses (see Error Handling)
  limits.py       constants, clean_text, cap_text, cap_items
  ports.py        AgentPorts, ApprovalReader (protocols)
  feed.py         service_events (page through StateView.events_after)
  digest.py       build_digest (pure) and collect_window (async, reads StateView)
  codesearch.py   search_code (pure over a folder, injected clock), SearchResult
  verify.py       VerifyInputs, VerifyResult, normalize_file, evaluate_verify
  watch.py        WorkItems, evaluate_work (pure), WatchQueue (long poll)
  snippet.py      build_snippet (pure)
  service.py      AgentService (the facade)
  routes.py       build_router
.kiro/agents/mesh-watcher.json
powers/chimericmesh-remediation/skills/watch-mode/SKILL.md      (new; remediation-loop and getting-started edited)
.kiro/hooks-optional/mesh-verify-write.json, mesh_verify_write.py   (opt-in, copied to .kiro/hooks/ by the user)
```

### Injected interfaces (resolves M2, stated once)

```python
class ApprovalReader(Protocol):                       # ApprovalManager satisfies this unchanged
    def approval_of(self, prop: Proposal) -> dict | None: ...      # {by, rule_id, rule_status_now, approved_at}
    def rule(self, rule_id: str) -> Rule | None: ...               # status, expires_at, code_folder
    def status(self) -> ApprovalStatus: ...                        # .paused
    # nothing else: no create, patch, enable, resume, revoke, pause or withdraw method is reachable

@dataclass(frozen=True, slots=True)
class AgentPorts:
    state: StateView                       # store ports: now, get_incident, incidents, get_proposal, list_proposals, recent, events_after
    registry: ServiceRegistry              # services: get(service_id) -> Service | None (live code_folder, in-memory)
    approvals: ApprovalReader | None       # None only in unit tests; every rule approval then fails verify (fail safe)
    protected_dirs: Sequence[str]          # same list create_app gives services and auto-approval
    workspace_root: str | None             # CHIMERICMESH_WORKSPACE_ROOT, else nearest ancestor of os.getcwd() that contains `.kiro/`, else os.getcwd() (R1-M3); snippet only
```

`AgentService(ports)` has no other input (R1-L1: there is no `AgentLimits` type; the constants in `agent/limits.py` are the only limits, so tests that need small values pass them to the `WatchQueue` constructor, as 5.2 does). It never receives `Hub`, `app`, `Store` or a token. It uses `state.now()` as its only clock for window arithmetic, and the running loop's monotonic clock only for the long poll.

| | Bare `Hub()` + `create_app(hub)` | `create_app()` / `python -m chimericmesh` |
|---|---|---|
| Events, incidents, proposals read through | `Hub` (memory, ring 2000, proposals cap 200) | `Hub` (store mirror; older events from SQLite through `events_after`) |
| Approval reads | `ApprovalManager` on the private `:memory:` store | `ApprovalManager` on the file store |
| `AgentService` code | identical | identical |

## Components and Interfaces

### Limits and safe text (`agent/limits.py`, pure, total)

```python
MAX_FIELD = 500                 # characters per log-derived string field (R1.7)
MAX_RESPONSE_BYTES = 20_480     # per response (R1.7)
MAX_EVENTS, DEFAULT_EVENTS = 100, 25
MAX_MINUTES = 240
MAX_QUERY = 100; MAX_HITS = 10; MAX_FILES_SCANNED = 2000; MAX_FILE_BYTES = 512 * 1024; SEARCH_SECONDS = 5.0
WAIT_MIN, TICK, MAX_WAITERS = 1, 0.5, 4
WAIT_CEILING = 25               # hard ceiling; never raised by configuration
WAIT_MAX = min(WAIT_CEILING, int(env CHIMERICMESH_WATCH_MAX_WAIT or 25), at least WAIT_MIN)   # R1-M6: lowering only; bad values fall back to 25 with one warning
MAX_EXCLUDE, MAX_ID_LEN = 200, 40
RULE_APPROVAL_WINDOW = 1800     # equals ApprovalSettings.rule_approval_ttl; a test asserts equality (Needs from auto-approval 3)

def clean_text(value: object, limit: int = MAX_FIELD) -> str
    # str(value); removes [\x00-\x1f\x7f-\x9f] and lone surrogates; cuts to `limit` characters. Never raises.
def clamp(value: int, low: int, high: int) -> int
def cap_items(envelope: dict, key: str, items: list[dict], *, max_bytes: int = MAX_RESPONSE_BYTES) -> dict
    # puts the longest prefix of `items` under envelope[key] such that json.dumps(envelope, separators=(",", ":")).encode() <= max_bytes;
    # sets envelope["truncated"] = True when items were dropped. An empty list always fits (the envelope is small by construction).
```

`cap_items` serializes with `ensure_ascii=False` and measures UTF-8 bytes, so a response of multi-byte text still fits. It drops items from the tail (oldest-first lists keep the oldest, and `next_after_id` is then the id of the last kept event).

### Service events (`agent/feed.py`)

```python
async def service_events(state: StateView, service_id: str, *, limit: int, after_id: int,
                         min_severity: int | None) -> dict
```

The route has already clamped `limit` to 1 to 100, `after_id` to at least 0 and `min_severity` to 0 to 7. The function pages `state.events_after(cursor, 100, service_id=service_id)` (oldest first), keeps events whose `severity <= min_severity` (lower number = more severe, so `min_severity=3` means error or worse; open item O7), stops at `limit` kept events or after scanning 1000 events, and returns `{service_id, events: [{id, ts, source, host, app, severity, message}], next_after_id, truncated}`. `next_after_id` is the id of the last event **scanned** (not only of the last kept), so a caller that filters by severity still makes progress; when nothing was scanned it equals `after_id`. Strings pass through `clean_text`; the list passes through `cap_items`. The store barrier for old rows is inside `events_after` (store design), not here.

### Digest (`agent/digest.py`)

```python
@dataclass(frozen=True, slots=True)
class Window: start: float; end: float; minutes: int
async def collect_window(state: StateView, service_id: str, window: Window, *, scan_cap: int = 20_000) -> tuple[list[Event], bool]
def build_digest(service_id: str, window: Window, events: Sequence[Event], incidents: Sequence[Incident],
                 *, scanned: int, truncated: bool) -> dict            # pure, total, deterministic
```

`collect_window` finds the first id of the window with a binary search over ids (`events_after(mid, 1)` reads one event, compares `ts` to `window.start`; at most about 17 reads for 100 000 rows; the newest id comes from `state.recent(1)`), then pages `events_after(start_id, 100, service_id=...)` until the end of the stream, `scan_cap` events (then `truncated=True`), or 200 **consecutive** events with `ts > window.end` (then `truncated=True` too). A single event past the window end (clock skew, a UDP host, a tailed file in another time zone) is skipped and the scan continues (R1-M7); it is neither counted nor does it end the scan. Ids are assigned in ingest order and `ts` is the message time, so for live traffic the search is exact; for replayed logs with old timestamps the digest is empty or partial and says so through `scanned` (open item O1; an `events_since` read on the store would make it exact, see Needs from store 2). `window.end = state.now()`, `window.start = end - 60 * minutes`.

`build_digest` returns:

```
{service_id, minutes, window: {start, end}, total_events,
 severity_counts: {"0": n, ..., "7": n},                      # all eight keys always present
 top_signatures: [{signature, count, first_ts, last_ts, sample, most_severe}],   # at most 10
 incidents_opened: [{id, category, severity, count, opening, first_seen}],       # first_seen in window, at most 20, newest first
 slices: [{start, end, events, per_minute}] x 5,              # equal slices of the window; per_minute = events / (minutes / 5), 2 decimals
 scanned, truncated}
```

Signatures are `diagnostics.signature(message)`. Order: count descending, then `first_ts` ascending, then the signature text ascending, so the result does not depend on input order (Property 2). `sample` is the message of the earliest event of that signature, cut to 200 characters. An event belongs to slice `min(4, int((ts - start) / (minutes * 12)))` (slices are `minutes * 12` seconds long); events outside `[start, end]` are not counted. The digest contains no model output.

### Code search (`agent/codesearch.py`)

```python
@dataclass(frozen=True, slots=True)
class SearchResult: hits: tuple[Hit, ...]; truncated: bool          # Hit(file, line, text)
def search_code(folder: str, query: str, *, protected_dirs: Sequence[str] = (), clock=time.monotonic,
                cancelled: Callable[[], bool] = lambda: False, realpath=os.path.realpath, normcase=os.path.normcase) -> SearchResult
```

Runs in a worker thread (`asyncio.to_thread`) with immutable inputs; it touches no hub object (store T2, T3). Algorithm: `needle = query.casefold()`; walk `folder` with `os.walk(followlinks=False)`, directories and files sorted so the result is deterministic; prune names in `diagnostics.SKIP_DIRS`; skip a file when its relative path matches a `glob_to_regex(p)` of `PROTECTED_PATTERNS` (after `normcase` and `/` normalization), when its suffix is not in `approvals.scope.SOURCE_SUFFIXES` (code only, D4; open item O6), when its size is over 512 KB, or when `path_in_code_folder(folder, path)` is false (this drops symlinks that leave the folder). It stops (and sets `truncated`) at 10 hits, at 2000 files visited, after 5 seconds on the injected `clock`, or when `cancelled()` is true. A line matches when `needle in line.casefold()`; one hit per matching line, and a file may give several hits. Text is the stripped line passed through `clean_text(..., 200)`. The query is never a regular expression. Files are read as `utf-8` with `errors="ignore"`; an `OSError` on a file skips it.

*Implementation note (2026-11-22, task 3.1).* Containment uses one `realpath` per file and a local "strict descendant of the real folder" test instead of calling `path_in_code_folder` (which resolves both the file and the folder every time): on Windows `realpath` costs about 1 ms, three calls per file made 2000 files exceed the 5 s ceiling. The semantics are the same for entries that `os.walk` lists (no Windows name tricks can occur in a listed name). The resolved path of a file is also checked against the protected patterns and `SOURCE_SUFFIXES`, so a link such as `notes.py -> .env` inside the folder is skipped. `search_code` also takes the injectable `walk`, `getsize` and `read_text` used by the property test, and raises `InvalidQuery` itself for an empty, over-long or control-character query.

### Verify (`agent/verify.py`)

```python
@dataclass(frozen=True, slots=True)
class VerifyInputs:
    proposal_id: str; status: str
    approved_by_rule: str | None; declared_files: tuple[str, ...] | None
    approval: Mapping[str, object] | None            # from approvals.approval_of
    rule_status: str | None; rule_code_folder: str | None; rule_expires_at: float | None   # None when the rule is unknown
    paused: bool; approvals_available: bool
    code_folder: str | None; has_service: bool       # the live folder of the incident's service
    now: float; protected_dirs: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class VerifyResult: ok: bool; reasons: tuple[str, ...]; status: str; approval: Mapping | None; declared_files: tuple[str, ...]

def normalize_file(code_folder: str | None, raw: str, *, normcase=os.path.normcase) -> str | None
def evaluate_verify(inp: VerifyInputs, files: Sequence[str], *, realpath=os.path.realpath, normcase=os.path.normcase) -> VerifyResult
```

The six checks are the sub-criteria of R3.1 (L8). `ok` is true exactly when none of the first five fails; the note of 3.1f does not make it false.

| Id | Check (all evaluated; one reason per failed check, in this order) | Reason text (fixed; never contains log, file or proposal text) |
|---|---|---|
| 3.1a | `status == "approved"` | `proposal status is <status>, not approved` (status is one of four fixed words) |
| 3.1b | `approved_by_rule is None`, or the rule is `active` now: rule known, `rule_status == "active"`, `paused` false, `now < rule_expires_at`, `samefolder(rule_code_folder, code_folder)`, `approval` is a mapping with a numeric `approved_at` and `now - approval.approved_at <= 1800` | first failing part, one text: `rule <id> is <status>`, `rule <id> is unknown`, `auto-approval is paused`, `rule <id> has expired`, `rule <id> was bound to another code folder`, `rule approval is older than 30 minutes`, `rule approval time is unknown` (approval `None`, or `approved_at` missing or not a number; R1-L4) |
| 3.1c | `1 <= len(files) <= 20` and every entry is a `str` of at most 300 characters without control characters **and is an absolute path** (`os.path.isabs`, also true for a Windows drive path on any host via `ntpath.isabs`) | `files must list 1 to 20 paths`, `file <n> is not a valid path` or `file <n> must be an absolute path` (n is the 1-based index; R1-H1) |
| 3.1d | `declared_files` is not `None` (always true for a rule approval): every `normalize_file(f)` is in the normalized declared set, and `check_file(code_folder, f, protected_dirs=...)` is `None` (membership only when `code_folder` is `None`) | `file <n> was not declared in the proposal` or `file <n> is outside the allowed scope` |
| 3.1e | `declared_files` is `None` (user approval only): no comparison | none (never a reason) |
| 3.1f | `code_folder` is not `None`: every file passes `path_in_code_folder(code_folder, f)` | `file <n> is outside the code folder`; when `code_folder` is `None` the note `no code folder, path scope not checked` is added and `ok` is not affected |

**Path form of `files` (R1-H1).** `files` passed to `verify_proposal` and to the hook are **absolute paths**. A relative entry is never resolved against the code folder here: Kiro's write tools and the agent resolve relative paths against the workspace root, so a code-folder join would check a different file from the one edited (for example `src/chimericmesh/hub.py` with code folder `examples/legacy_app` would pass 3.1d and 3.1f while the edit hits the real `src/chimericmesh/hub.py`). The absolute-path rule lives in 3.1c, applies to every approval, user or rule, and is the cause of the reason `file <n> must be an absolute path`. The agent builds the paths from `get_service(service_id).code_folder` (prompt contract). `declared_files` stay as the proposal stored them (auto-approval resolves a relative declared entry against the code folder); `normalize_file` is applied to the declared entries only, and to each verified file after it is known to be absolute (`normcase(normpath(...))`). The property-5 oracle is written with its own absolute-path rule, so the joining rule no longer sits on both sides.

`samefolder(a, b)` (R1-L5) is `normcase(normpath(a)) == normcase(normpath(b))` after stripping a trailing separator, and is false when either side is `None`.

A rule approval with `approvals_available` false, or an unknown `approved_by_rule` rule, fails 3.1b (fail safe). `approved_by_rule` is the proposal column; the `decided_by` text is never an input of `VerifyInputs`, so an IRC nick `rule:r1` cannot pass as a rule approval. `normalize_file` joins a relative **declared** entry to the code folder (the auto-approval convention), applies `os.path.normpath` and `normcase`, and returns `None` for an empty or invalid string; a verified file is always absolute (3.1c), so an absolute declared path inside the folder and the same file declared relative give the same key as the verified absolute path. Reasons for a rule approval that is also user-checked do not leak the rule's signature.

### Watch queue (`agent/watch.py`)

```python
@dataclass(frozen=True, slots=True)
class WorkResult: incidents: tuple[dict, ...]; approved_proposals: tuple[dict, ...]
    # status is "work" when either tuple is non-empty

def evaluate_work(incidents: Sequence[Incident], proposals: Sequence[Proposal], *, service_id: str | None,
                  exclude: frozenset[str], registered: Callable[[str], bool],
                  approval_of: Callable[[Proposal], dict | None], limit: int = 5) -> WorkResult      # pure, total

class WatchQueue:
    def __init__(self, ports: AgentPorts, *, wait_min=WAIT_MIN, wait_max=WAIT_MAX, tick=TICK, max_waiters=MAX_WAITERS) -> None
    async def next(self, *, timeout: float, service_id: str | None, exclude: frozenset[str],
                   is_disconnected: Callable[[], Awaitable[bool]] | None = None) -> dict   # raises TooManyWaiters
    def kick(self) -> None
    def close(self) -> None                       # shutdown: wakes every waiter, which returns {status: "timeout"}
    waiting: int                                  # current held waiters
```

Sub-criteria of R4.3 (L8), one test each:

| Id | Incident is work when |
|---|---|
| 4.3a.1 | the incident is open (not resolved) |
| 4.3a.2 | its `service_id` is a registered service (`registered(sid)`), or `service_id == "unassigned"` was requested and the incident has none |
| 4.3a.3 | no proposal of this incident has status `pending` or `approved` |
| 4.3a.4 | the incident id is not in `exclude` |
| 4.3a.5 | no proposal of this incident with `incident_opening == incident.opening` has status `denied` and `decision_origin == "user"` |
| 4.3a.6 | when `service_id` is given and is not `unassigned`, the incident has that service |
| 4.3b | a proposal is work when `status == "approved"`, its id is not in `exclude`, and its `service_id` passes the same filter as 4.3a.2 and 4.3a.6 |

`decision_origin`, never the `decided_by` text, separates a user denial from a hub denial (store R4.7). A denial by the hub (rule revoked, suspended, expired, withdrawn) does not block. A user denial blocks only the opening it was made in, so a reopen (`opening` increments, store R5.7) returns the incident. Ordering: incidents by `(severity, -count, id)`, at most 5; approved proposals by creation order, at most 5. An incident fields set: `{id, service_id, host, category, severity, count, opening, first_seen, last_seen, sample}`; a proposal set: `{id, incident_id, service_id, action, files, estimated_lines, verification, approval}`. Every string passes `clean_text` (`sample` 200, others 500); no event body is included (R4.4).

`WatchQueue.next` clamps nothing (the route did). Algorithm:

```python
async def next(self, *, timeout, service_id, exclude):
    if self._closed: return TIMEOUT
    if self.waiting >= self._max: raise TooManyWaiters(retry_after=5)
    self.waiting += 1; waiter = asyncio.Event(); self._waiters.add(waiter)
    try:
        deadline = loop.time() + timeout
        while True:
            result = self._evaluate(service_id, exclude)          # sync, no await
            if result.has_work: return {"status": "work", ...}
            left = deadline - loop.time()
            if left <= 0 or self._closed: return TIMEOUT
            if is_disconnected is not None and await is_disconnected(): return TIMEOUT   # once per tick (R1-M1)
            waiter.clear()
            try: await asyncio.wait_for(waiter.wait(), min(self._tick, left))
            except asyncio.TimeoutError: pass
    finally:
        self.waiting -= 1; self._waiters.discard(waiter)
```

`_evaluate` reads `state.incidents(status="open")` and `state.list_proposals(None)` (store mirror, synchronous) and calls the pure `evaluate_work`. With 10 000 proposals this costs about a millisecond; a test pins 50 ms for 10 000 proposals and 5000 incidents. `TIMEOUT = {"status": "timeout", "incidents": [], "approved_proposals": []}`. **Client disconnect (R1-M1).** A Starlette/FastAPI plain `GET` handler is not cancelled when the client goes away; only streaming responses watch for it. The route therefore takes `request: Request` and passes `request.is_disconnected` as `is_disconnected`; `next` checks it once per tick (at most 0.5 s) and returns `timeout`, so a cancelled or timed-out `wait_for_work` frees its slot within one tick, not after up to 25 s, and a few restarts of the loop cannot produce the 429 that makes the prompt rule stop. A cancellation of the coroutine (direct task cancel, tests) also runs `finally`, so the counter cannot leak. The check costs one awaited call per tick per waiter.

### Permissions snippet (`agent/snippet.py`, pure)

```python
def build_snippet(service: Service, *, workspace_root: str | None, test_command: str = "python -m pytest -q") -> str
def validate_test_command(value: str) -> str          # raises InvalidQuery
```

Output is plain text (YAML with leading comments). Lines in order: (1) `# Mode: AUTONOMOUS (hands-off)` and the trade-off sentences of R5.7 and R5.8 (in-folder edits no longer prompt, including edits for proposals that are still pending; the gate then rests on the prompt text, `verify_proposal` and the audit trail, which a prompt-injected agent could ignore); `# Prompt mode (default): do not paste this file.`; (2) scope text of R5.4: the user-scoped file `~/.kiro/settings/permissions.yaml` applies to every session and agent of this OS user; the workspace-scoped file `~/.kiro/workspace-roots/<hash>/permissions.yaml` limits the rule to this workspace and is preferred (look up the hash); (3) F11 and the "check live" note; (4) `rules:` with an `fs_write` allow `<relative folder>/**`, an `fs_write` deny for `PROTECTED_PATTERNS`, and the commented examples; (5) the closing comment that everything else keeps asking.

The allow line exists only when the folder is a strict descendant of `workspace_root` (`path_in_code_folder(workspace_root, folder)`), its relative path (separators `/`) has no character of `*?[]{}` and no control character, and `workspace_root` is known. Otherwise the allow line is replaced by a comment: `# Code folder is outside the open workspace (or equals it). The fix still runs, Kiro prompts for each write.` Strings are emitted with `json.dumps(..., ensure_ascii=False)`, valid YAML flow scalars, so a folder name cannot inject YAML. The shell example is commented: `#  - capability: shell` / `#    match: ["<test_command>"]` / `#    effect: allow`. `validate_test_command` accepts `[A-Za-z0-9_.:/ -]{1,80}`, rejects `*`, quotes, `;&|<>$()` and backtick, a word in `{curl, wget, nc, ncat, powershell, pwsh, bash, sh, cmd}` and the text `-c`; `python -m pytest -q` is the default. **Schema caveat (R1-L8).** The `rules:` / `capability` / `match` / `effect` layout is modelled on the JSON `permissions.rules` shape seen in `mesh-responder.json`; the exact `permissions.yaml` schema and whether a `shell` `match` is a prefix or an exact match are **not verified**. The snippet says so in a comment (`# Schema checked against https://kiro.dev/docs/permissions/ on <date of manual check>; verify before pasting`), and the manual check list in 12.2 carries it. A relative deny glob (`.git/**`, `.kiro/**`, the `PROTECTED_PATTERNS`) in the **user-scoped** file may apply in every workspace of this OS user; the snippet text says this next to the scope text and recommends the workspace-scoped file for the deny lines. An optional commented block for the hook state file (`.chimericmesh-active-proposal`, exactly that file, workspace-relative) is included for users who install the hook. The snippet never contains `allow-all`, `trust-all-tools`, `*` as a shell match or a network-capable command (R5.5; Property 10).

### The facade (`agent/service.py`)

```python
class AgentService:
    def __init__(self, ports: AgentPorts) -> None
    watch: WatchQueue
    async def service_events(self, service_id: str, limit: int, after_id: int, min_severity: int | None) -> dict
    async def digest(self, service_id: str, minutes: int) -> dict
    async def code_search(self, service_id: str, query: str) -> dict
    def verify(self, proposal_id: str, files: Sequence[str]) -> dict
    def permissions_snippet(self, service_id: str, test_command: str | None) -> str
    def code_folder_for_incident(self, inc: Incident) -> str | None          # B6
    def aclose(self) -> None                                                  # watch.close()
    # private: _service(sid) raises UnknownService for unknown ids and for "unassigned" (R1.5)
```

`code_search` allows at most two concurrent searches. `asyncio.Semaphore` has no non-blocking acquire (R1-L3), so the facade keeps a plain `self._running` counter on the loop thread: if `self._running >= 2` it raises `SearchBusy` (REST 429); otherwise it increments the counter and starts the thread. The counter is decremented by the **worker thread's completion callback** (the `to_thread` future's `add_done_callback`, scheduled on the loop), not by the awaiting task's `finally`, so a cancelled awaiting task whose thread is still running keeps its slot until the thread returns and G2 ("at most two threads") holds, for at most 5 s. The search runs in `asyncio.to_thread` with a `threading.Event`-backed `cancelled` that the awaiting task sets in `finally`, and returns `{service_id, hits: [{file, line, text}], truncated}`. A service without a code folder raises `NoCodeFolder` (422 `service has no code folder`). `verify` builds `VerifyInputs` in one synchronous block (no `await`, so the snapshot is consistent), reads `approved_by_rule` from the proposal, `approval_of(prop)`, `approvals.rule(rid)`, `approvals.status().paused`, `registry.get(prop.service_id)`; a proposal whose `service_id` is `None` or whose service was deleted gives `has_service=False, code_folder=None`.

### REST routes (`agent/routes.py`, owner `agent-loop`)

```python
def build_router(agent: AgentService) -> APIRouter
```

All handlers are `async def` (store T2; a static test lists the routes). No route has a dependency on `require_admin`; none changes state. `HostGuard` and `BodyLimit` (services) already cover `/api/*`.

| Route | Input validation (REST) | Success | Failures |
|---|---|---|---|
| `GET /api/services/{id}/events` | `limit` int, clamped 1 to 100 (default 25); `after_id` int, clamped to at least 0 (default 0); `min_severity` int, clamped 0 to 7, optional | 200 `{service_id, events, next_after_id, truncated}` | 404 unknown service or `unassigned`; 422 non-integer parameter |
| `GET /api/services/{id}/digest` | `minutes` int, clamped 1 to 240 (default 15) | 200 digest | 404; 422 |
| `GET /api/services/{id}/code-search` | `query` required, 1 to 100 characters, no control character | 200 `{service_id, hits, truncated}` | 404; 422 (missing, length, control character, `service has no code folder`); 429 (two searches running) |
| `POST /api/proposals/{id}/verify` | body `{files: list[str]}`, `extra="forbid"`, at most 200 entries of at most 1100 characters (a size bound only; the 1 to 20 rule is check 3.1c) | 200 `{ok, reasons, status, approval, declared_files}` | 404 unknown proposal; 422 body bounds |
| `GET /api/watch/next` | `timeout_seconds` int, clamped 1 to `WAIT_MAX` (25, lowered by `CHIMERICMESH_WATCH_MAX_WAIT`; default 25); the handler takes `request: Request` for `is_disconnected`; `service_id` optional (`unassigned` allowed); `exclude_ids` repeated query parameter, at most 200 entries, each `[A-Za-z0-9_.:-]{1,40}` | 200 `{status: "work" or "timeout", incidents, approved_proposals}` | 404 unknown `service_id`; 422 bad `exclude_ids`; 429 more than 4 waiters (`Retry-After: 5`) |
| `GET /api/services/{id}/permissions-snippet` | `test_command` optional (see `validate_test_command`) | 200 `text/plain; charset=utf-8` | 404; 422 (no code folder, bad test command) |

Table for the route-table test (R1.1, owners): the six routes above are `agent-loop`; `GET /api/services` and `GET /api/services/{id}` are `services`; the six `/api/discovery/*` routes are `discovery` (none has a tool; discovery A2); `POST /api/proposals`, `GET /api/proposals`, `GET /api/proposals/{id}`, `POST /api/proposals/{id}/complete` are `auto-approval` (section 9); the existing routes are `baseline`. Dashboard routes are added to the same owner table by the dashboard module.

### MCP server (`mcp_server.py`, wiring; thin wrappers)

New tools, each one `_call` to one route (parameters unchanged, nothing clamped or filtered):

| Tool | Call |
|---|---|
| `list_services()` | `GET /api/services` |
| `get_service(service_id)` | `GET /api/services/{service_id}` |
| `service_events(service_id, limit=25, after_id=None, min_severity=None)` | `GET /api/services/{service_id}/events`, params only for non-`None` values |
| `explain_log_window(service_id, minutes=15)` | `GET /api/services/{service_id}/digest` |
| `find_code_for_service(service_id, query)` | `GET /api/services/{service_id}/code-search` |
| `verify_proposal(proposal_id, files)` | `POST /api/proposals/{proposal_id}/verify` |
| `wait_for_work(timeout_seconds=25, service_id=None, exclude_ids=None)` (R1-minor: no mutable default; `None` sends no parameter) | `GET /api/watch/next` with `timeout=35.0` for this call |

Changed tools: `propose_remediation(incident_id, action, details="", files=None, estimated_lines=None, verification=None, kind=None)` sends only the keys that are not `None`, so an old caller sends the same body as today; `complete_proposal(proposal_id, note="", files_changed=None, lines_changed=None, verification_result=None)` likewise. `_call` gets an optional `timeout` argument that it passes per request (`client.request(..., timeout=35.0)`), so the injected zero-argument `_client_factory` that existing tests monkeypatch keeps working, and the default stays 10 s (R4.5). On HTTP 400 and above `_call` returns `{"error": status, "detail": <hub text>}` as today and now also copies the other keys of a JSON error body (for example `proposal_id` of a 409 `ActiveProposalExists`, `field` of a 422); a non-JSON body gives its text. The `instructions` string keeps the words `propose_remediation` and `approved` (an existing test) and gains one sentence for watch mode. No tool for rules, services, tokens, discovery, audit, withdraw or pause exists (R1.8, R2.4); the exact set is 17 names (the ten baseline tools plus the seven above).

### Agent profile (`.kiro/agents/mesh-watcher.json`)

New file; `mesh-responder.json` is untouched (R5.3). Fields (all in the documented `AGENT_FIELDS` set): `name: "mesh-watcher"`, `description`, `prompt`, `mcpServers.chimericmesh` (same command, args and env as `.kiro/settings/mcp.json`, plus `"requestTimeout": 120000`), `tools: ["read", "write", "shell", "@chimericmesh/<each of the 17 tools>"]`, `includeMcpJson: false`, `includePowers: false`, `resources: ["file://.kiro/steering/project.md"]`, `welcomeMessage`. Permissions:

```json
"permissions": { "rules": [
  { "capability": "mcp", "effect": "allow", "match": [
      "chimericmesh/list_incidents", "chimericmesh/get_incident", "chimericmesh/find_code_for_incident",
      "chimericmesh/recent_events", "chimericmesh/list_proposals", "chimericmesh/get_proposal",
      "chimericmesh/list_services", "chimericmesh/get_service", "chimericmesh/service_events",
      "chimericmesh/explain_log_window", "chimericmesh/find_code_for_service",
      "chimericmesh/wait_for_work", "chimericmesh/propose_remediation", "chimericmesh/verify_proposal",
      "chimericmesh/complete_proposal", "chimericmesh/resolve_incident", "chimericmesh/announce_to_irc" ] },
  { "capability": "fs_write", "effect": "deny", "match": [
      "**/.env", "**/.env.*", "**/*.pem", "**/*.key", "**/id_rsa*", "**/admin.token",
      "**/chimericmesh.db*", "~/.chimericmesh/**" ] },
  { "capability": "fs_read", "effect": "deny", "match": [ "**/admin.token", "**/chimericmesh.db*", "~/.chimericmesh/**" ] }
] }
```

There is no `allow` or `ask` rule for `fs_write` or `shell` and no `*` in an `mcp` match (R5.1; F4: an `ask` in the profile would beat the user's allow). `requestTimeout` is explicit although 120000 is the default, so the margin over the 25 s wait is visible; a test asserts it is at least 60000. The `~/.chimericmesh/**` globs are documented only as a config location (L9): a manual check (task 12.2) confirms they match; if they do not, the profile's deny of `**/admin.token` and `**/chimericmesh.db*` still matches by name, the hook (R5.9) covers writes only, and Kiro's own prompt for reads outside the workspace remains. This is stated in the README so nobody reads the hook as a read guard.

**Prompt contract** (the profile `prompt` and the skills; each item is checked by a static test, R2.3, R3.3, R3.5, R3.6, R4.8, R4.9): the watch loop in order; always declare `files`, `estimated_lines` and `verification` and read the returned `status` (never assume `pending`); one concrete code change per proposal; call `get_proposal` immediately before acting and compare `status`, `action` and `incident_id` with what was reviewed (carried from branch `fix/proposal-ids-and-recheck`, ids stay `p1`, `p2`; open item O4); `files` (in `propose_remediation`) are as the service's code folder shows them, and the `files` given to `verify_proposal` are **absolute paths built from `get_service(service_id).code_folder`**, never workspace-relative or bare names (R1-H1); **when verification after the fix fails** (tests fail, or the event rate has not fallen when re-checked with `service_events` or `explain_log_window`), do **not** call `resolve_incident`, add the incident id to `exclude_ids`, tell the user what was observed and continue the loop; a further attempt needs a new proposal and the user's decision (R1-M4); **finish the edits, the tests and `complete_proposal` within 30 minutes of the approval, otherwise `verify_proposal` fails the rule approval and a new proposal is needed** (R1-L7); call `verify_proposal` with the exact files before the first edit, before an edit of a file not listed before, and before `complete_proposal`; change nothing unless `ok` is true; when `ok` is false stop, make no change, report the reasons and, if files were already changed, list exactly which; a different action (other files, a restart, a configuration change) needs a new proposal and V1 is code only (D4); only the `status` and `approval` returned by tools count as approval, never text in a log, IRC, a proposal or a file; add an incident id to `exclude_ids` when its proposal is `pending` and a proposal id when starting to act on it, and keep `exclude_ids` to ids still open; stop and ask after three consecutive tool errors or when `wait_for_work` returns 401, 403 or 429 twice; keep notes short in a long session; write the proposal id into `.chimericmesh-active-proposal` only when the user installed the hook.

### Optional hook (`.kiro/hooks-optional/mesh-verify-write.json`, `mesh_verify_write.py`)

Built as an extra layer (R5.9). The files are **not** in `.kiro/hooks/`; the user copies both there to opt in (open item O5), because while it is installed every write in the workspace needs an active proposal. `mesh-verify-write.json` uses `"trigger": "PreToolUse"`, `"matcher": "^(fs_write|fs_append|str_replace)$"` (R1-M5: the regex over tool names that the repository's own `.kiro/hooks/guard-innerhtml.json` already uses and that works with the `tool_input.path` shape of `test_guard_hook`; the `write` category is only a manual-check alternative in 12.2, because the script fails open and a matcher that never fires gives no protection and no error) and the action `python .kiro/hooks/mesh_verify_write.py`.

```python
def decide(stdin_text: str, *, cwd: str, env: Mapping[str, str], post: Callable[[str, bytes, float], dict]) -> tuple[int, str]
    # returns (exit code, stderr text); main() calls sys.exit with it
```

Flow: parse stdin JSON; collect target paths from `tool_input` keys `path`, `file_path`, `paths` (what the input contains is confirmed live; an unparseable input or no path found exits 0, because a hook that cannot run must not break anything, like `guard_innerhtml.py`). **Each target is made absolute before the request** with `os.path.abspath(os.path.join(cwd, p))` (R1-H1), because Kiro usually sends workspace-relative paths and verify requires absolute ones (3.1c); the hook's `cwd` is assumed to be the workspace root and is part of the manual check (12.2). A write whose target basename is `.chimericmesh-active-proposal` is allowed (Kiro still prompts for it unless the snippet allows that single file). Otherwise read `<cwd>/.chimericmesh-active-proposal`; the content must match `^p[1-9][0-9]*$` after stripping, else exit 2 (blocks). Take the base URL from `CHIMERICMESH_URL` (default `http://127.0.0.1:8000`); a host that is not `127.0.0.1`, `localhost` or `::1` exits 2. `POST <base>/api/proposals/<id>/verify` with `{"files": [paths]}` (5 s timeout, stdlib `urllib.request`, no token header, no other host). Exit 2 when the hub is unreachable, the reply is not JSON, `ok` is not `true`, or `reasons` contains the note `no code folder, path scope not checked` (the hook cannot check scope then). Exit 0 otherwise. The reason goes to stderr. Residual risk (R5.9): the state file is written by the agent, so an injected agent can name another approved proposal; verify then still limits the write to that proposal's declared files and folder.

### Wiring (`api.py`, `__main__.py`)

`create_app` after the `auto-approval` step: `agent = AgentService(AgentPorts(state=hub, registry=app.state.services, approvals=app.state.approvals, protected_dirs=protected, workspace_root=find_workspace_root(os.environ, os.getcwd())))`, where `find_workspace_root` (in `agent/snippet.py`, pure over injected `exists`) returns `CHIMERICMESH_WORKSPACE_ROOT` when set and valid, else the nearest ancestor of the working directory that contains a `.kiro` directory, else the working directory (R1-M3: the documented run command starts in `src`, whose parent holds `.kiro/` and the demo folder `examples/legacy_app`; the working directory alone would put the demo folder outside the root and emit no allow line). The snippet and the dashboard text name the root they used (`# Workspace root used: <path>`); `app.state.agent = agent`; `app.include_router(build_router(agent))`. The existing route `GET /api/incidents/{id}/code` becomes: `folder = agent.code_folder_for_incident(inc)`; with a folder use `find_code_hints(inc, Path(folder))`, otherwise `hub.code_hints(inc)` as today (B6). Lifespan shutdown: `agent.aclose()` is the first step, before the listeners close, so a held `wait_for_work` ends with `{status: "timeout"}` and the server does not wait for it (R4.6). The timing slice of the services shutdown plan is unchanged (`aclose` is synchronous and instant). **Uvicorn waits for in-flight requests before it runs lifespan shutdown** (R1-M2), so the lifespan step alone would run only after the held requests expire (up to 25 s). Two changes make R4.6 true: (1) `__main__.py` passes `timeout_graceful_shutdown=3` to `uvicorn.run`, so a held request is cancelled after 3 s at most; (2) `__main__.py` builds a `uvicorn.Server` subclass (`install_signal_handlers` wrapper) whose exit handler calls `app.state.agent.aclose()` before it sets `should_exit`, so held waiters return `timeout` at once and the server stops in well under 3 s. `__main__.py` stays wiring and gets a small edit (task 7.3); a test starts a real `uvicorn.Server` on an ephemeral port, holds a waiter, requests exit and asserts it stops in under 3 s.

## Data Models

No table, no migration, no persisted state. `MODULE_BLOCKS["agent"] = (500, 599)` stays unused. In-memory state: the waiter set and counter in `WatchQueue`, the search semaphore. Response shapes are given with each component above.

## Sequence diagrams

### Watch loop with a rule-approved proposal

```
agent                MCP wrapper            hub REST                 WatchQueue / AgentService        auto-approval
 | wait_for_work()-------->|  GET /api/watch/next (35 s client timeout)-->| waiting=1, evaluate: nothing
 |                         |                                              | tick 0.5 s ... incident opens
 |                         |<-- {status: work, incidents:[i1]} -----------|
 | get_service, get_incident, explain_log_window, find_code_for_service ... (reads)
 | propose_remediation(files, estimated_lines, verification)------------> hub.create_proposal ------> prepare(): matches rule r1
 |<-- {id: p4, status: approved, approval: {by: rule, rule_id: r1}} -----
 | get_proposal(p4): status, action, incident_id unchanged
 | verify_proposal(p4, [files])-------> POST /api/proposals/p4/verify --> evaluate_verify: 3.1a..f -> ok: true
 | edit files (Kiro: no prompt inside the code folder in autonomous mode, prompt otherwise)
 | verify_proposal again before complete; run the test command
 | complete_proposal(p4, files_changed, lines_changed, verification_result) -> after_complete checks (R6.5)
 | resolve_incident(i1); wait_for_work(exclude_ids=[i1, p4])
```

### Revoked while working

```
user: !revoke r1 -> mirror changes before any await; p4 is denied (rule-revoked:r1)
agent: verify_proposal(p4, [files]) -> 3.1a fails ("proposal status is denied, not approved"); 3.1b fails ("rule r1 is revoked")
agent: stops, makes no change, reports both reasons.
```

## Threading and transaction model (applies store T1 to T10; M3 as it concerns this module)

- **G1. Loop thread only.** `AgentService`, `WatchQueue`, `evaluate_work`, `evaluate_verify`, `build_digest` and every route handler run on the loop thread. The evaluations read `StateView` synchronously and contain no `await` between reading and deciding, so a snapshot is consistent (store T2).
- **G2. One worker thread.** `search_code` runs in `asyncio.to_thread`. Its inputs are strings and tuples; it takes no hub, registry or store object; it returns an immutable `SearchResult`. A `threading.Event` carries cancellation from the loop. At most two such threads exist at once (semaphore).
- **G3. No writes.** The module writes no row and takes no store transaction. The only audit-relevant effect is the hub's own, in `auto-approval`.
- **G4. Held requests.** A waiter holds a coroutine, not a thread or a connection. Ingestion, the SSE stream and other routes are served meanwhile (test: 4 held waiters plus 200 ingests complete in under 2 s).
- **G5. Shutdown.** `AgentService.aclose` runs first in the lifespan teardown; waiters return `timeout` at their next wake (at most 0.5 s, and `close()` sets every waiter's event so it is immediate).

## Error Handling

| Operation | Failure | Recoverable? | Caller receives | Logged |
|---|---|---|---|---|
| Any per-service route | unknown service id, or `unassigned` | yes | 404 `{detail: "service not found"}` (R1.5) | none |
| Integer parameter not an integer, `files` or `exclude_ids` wrong type | bad request | yes | 422 (FastAPI validation) with `detail` | none |
| `service_events`, `explain_log_window`, `wait_for_work` out-of-range numbers | caller error | yes | clamped silently in REST (R1.2, R1.3, R4.2); the response shows the effective `limit` and `minutes` | none |
| `code-search`: `query` missing, empty, over 100 characters, control character | bad request | yes | 422 `{detail, field: "query"}` | none |
| `code-search`: service has no code folder | caller error | yes | 422 `service has no code folder` (R1.4) | none |
| `code-search`: code folder missing on disk | environment | yes | 200 `{hits: [], truncated: false}` (a folder removed after it was registered; `os.walk` yields nothing) | debug |
| `code-search`: 5 s, 2000 files, 10 hits reached | limits | yes | 200 with `truncated: true` | none |
| `code-search`: unreadable file or directory | environment | yes | the entry is skipped | debug, count only |
| `code-search`: two searches already running | load | yes | 429 `search busy`, `Retry-After: 2` | none |
| `service_events`/digest: store read fails (`StoreError`) | store down | yes | the store's 503 handler (`store unavailable`); the MCP wrapper returns `{error: 503, detail}` | the store logs |
| `digest`: scan cap reached | volume | yes | 200 with `truncated: true` and `scanned` | none |
| `verify`: unknown proposal id | caller error | yes | 404 `proposal not found` | none |
| `verify`: any check fails | normal | yes | 200 `ok: false` with one reason per failed check; never an error status, so the agent always gets the reasons | none |
| `verify`: internal exception | bug | yes | 500; the agent treats a non-200 as not ok (prompt rule) and stops; the hook blocks | error, once per minute |
| `wait_for_work`: nothing in time | normal | yes | 200 `{status: "timeout", ...}` | none |
| `wait_for_work`: fifth concurrent waiter | load | yes | 429 `too many waiters`, `Retry-After: 5` | none |
| `wait_for_work`: hub shutting down | normal | yes | 200 `{status: "timeout"}` | none |
| `wait_for_work`: client disconnects | normal | yes | the waiter sees `request.is_disconnected()` at its next tick (at most 0.5 s) and returns, `finally` frees the slot (R1-M1; the handler is not cancelled by Starlette) | none |
| `verify`: `files` contains a relative path | caller error | yes | 200 `ok: false`, reason `file <n> must be an absolute path` (R1-H1) | none |
| Bare hub: proposals ring holds 200 (R1-L9) | volume | yes | a user-denied proposal that falls off the ring makes its incident work again; store mode keeps all proposals; the README says so | none |
| `permissions-snippet`: no code folder, bad `test_command` | caller error | yes | 422 with the reason | none |
| `permissions-snippet`: folder outside the workspace, equal to it, or with glob characters | normal | yes | 200 text without an `fs_write` allow line, with the explanation | none |
| MCP wrapper: hub unreachable (`httpx` error) | environment | yes | the exception reaches the MCP SDK, which reports a tool error; the prompt rule "three consecutive tool errors: stop and ask" applies | the SDK |
| MCP wrapper: 400 or above | hub error | | `{error: status, detail: <hub text>, ...extra keys}` | none |
| Hook: parse failure, no path | hook bug | yes | exit 0 (fail open; Kiro's prompt still applies) | stderr text |
| Hook: state file missing or bad, hub unreachable, `ok` false, note present, non-loopback URL | policy | yes | exit 2, reason on stderr (blocks the write) | stderr |
| Environment: `CHIMERICMESH_WORKSPACE_ROOT` not an existing directory | misconfiguration | yes | the snippet behaves as "workspace unknown": no allow line | one warning at start |

**Invariants and who enforces them.**

| Invariant | Layer | Why there |
|---|---|---|
| No tool or route of this module can create, widen, re-enable or resume a rule, write config, or carry the token | `ApprovalReader` has no mutator; `routes.py` has no write route and no `require_admin`; the exact-tool-list and route-table tests | a protocol the type system cannot widen, plus tests that fail on any addition |
| Only `status` and `approval` from the hub count as approval | `evaluate_verify` (column `approved_by_rule`, never `decided_by`) and the prompt text | the hub is the arbiter; the prompt is the second layer |
| Log text is capped and clean in every response | `limits.clean_text` and `cap_items` in the service layer, not in the MCP wrapper | every client benefits, not only Kiro |
| A held request never blocks the hub | `WatchQueue` (coroutine, tick, no thread) | G4 |
| The hub never starts a Kiro process or any process | static allow-list test (R4.10): only `fsperms.py` and `discovery/runner.py` may import `subprocess` or call `os.system`, `os.popen`, `os.spawn*`, `os.exec*` or `asyncio.create_subprocess_*` | one reviewable place for each (discovery A1) |

## Security

- **Untrusted text.** Event messages, incident samples, proposal action and verification text, file names and file contents are data. They are stripped and capped on the way out (`clean_text`), appear in `reasons` never, and are never interpolated into YAML, shell or HTML: the snippet uses `json.dumps`, code-search text is returned as JSON, the dashboard renders with `textContent` (project rule).
- **No token path.** No route or tool accepts, forwards or returns the admin token. The hook never reads it. The route-table test fails when any route behind a tool depends on `require_admin`.
- **Honest limit, repeated on purpose.** The agent runs as the same OS user as the hub. With a shell command allowed it can read the token file and call the REST API, including the token-free pause, revoke and withdraw routes (which only reduce its own permissions). The real gate is Kiro's prompt on shell commands and on writes outside the allowed scope. The deny rules and the hook are extra layers. The README says so (task 11.1).
- **Autonomous mode trade-off.** The pasted snippet is a standing allow for edits in the code folder, including for proposals that are still `pending` (R5.7). The one allowed test command runs code the agent just wrote with the user's rights, and that code could read the token file (R5.11(b)). The user accepted this for hands-off mode only (2026-11-18). Prompt mode is the default and the demo default. The README documents both modes and the risk, as the Notes for design require.
- **Path scope.** Code search and verify resolve paths with `realpath` through `path_in_code_folder` (services) and `check_file` (auto-approval); symlinks that leave the folder are skipped or rejected; the Windows name tricks are handled by the same function. The residual race (a link swapped between check and write) is the one services documents; Kiro's permissions are the write gate.
- **Denial of service.** 4 waiters, 2 searches, 20 KB responses, 5 s search, 200 `exclude_ids`, 64 KB bodies (services middleware). A waiter costs one evaluation per 0.5 s.
- **Local only.** Everything binds to the interfaces the hub already binds (`127.0.0.1` default, Host check from services). The hook talks to a loopback host only.
- **No process.** The hub, the MCP server and this package start no process (R4.10). The hook script is run by Kiro, not by the hub.

## Correctness Properties

Each property is a universal statement for Hypothesis (`tests/pbt/`, `max_examples` 40 to 100, `deadline=None` where a store or a thread is used). Properties 1 to 5, 7 and 10 are required; 6, 8, 9 and 11 are optional (`*`).

1. **`service_events` limits and order.** *For any* `limit` (any integer), `after_id`, `min_severity` and any sequence of events of several services, the clamped call returns at most 100 events, all of the requested service, ids strictly increasing and greater than `after_id`, severities at most `min_severity` when given, `next_after_id >= after_id`, and a second call from `next_after_id` returns none of the first call's events. **Validates: Requirements 1.2**
2. **Digest is deterministic and consistent.** *For any* list of events and incidents, `build_digest` gives the same result for any permutation of the input; `severity_counts` has all eight keys and sums to `total_events`; `slices` has five entries whose `events` sum to `total_events`; `top_signatures` has at most 10 entries ordered by count descending; every `sample` is at most 200 characters; `minutes` outside 1 to 240 is clamped by the route to those bounds. **Validates: Requirements 1.3**
3. **Code search is contained and bounded.** *For any* folder tree (injected `realpath` and `normcase`, a fake file reader and fake clock), any query of 1 to 100 characters: at most 10 hits, every hit's file is relative, inside the folder, outside `SKIP_DIRS`, not protected, with a source suffix; every `text` is at most 200 characters without control characters; the number of files visited is at most 2000; when the fake clock passes 5 s the search stops and `truncated` is true; a query made of regex metacharacters matches only the literal text. **Validates: Requirements 1.4**
4. **Safe text.** *For any* text (NUL, CR, LF, control characters, lone surrogates, very long) `clean_text` returns a string of at most the limit with no character in `[\x00-\x1f\x7f-\x9f]`; and *for any* list of items `cap_items` returns JSON of at most 20 480 bytes that parses, keeps a prefix of the items, and sets `truncated` iff items were dropped. **Validates: Requirements 1.7**
5. **Verify is exactly the conjunction.** *For any* `VerifyInputs` and `files`, `evaluate_verify` returns `ok` iff 3.1a, 3.1b, 3.1c, 3.1d and 3.1f all hold (3.1e imposes nothing), with an independently written oracle; `len(reasons)` equals the number of failed checks plus the optional note; no reason contains any substring of a file name, proposal text or `decided_by` canary; it never raises; a `decided_by` of `rule:r1` with `approved_by_rule` of `None` is never read as a rule approval. **Validates: Requirements 3.1, 3.2**
6. *(optional)* **Verify is monotone in the files.** *For any* inputs where `ok` is true for `files`, `ok` is true for every non-empty subset of `files`; and adding a file outside the declared list makes `ok` false. **Validates: Requirements 3.1 (c), (d)**
7. **Work set is exactly the specified set.** *For any* generated incidents, proposals (statuses, `decision_origin`, `incident_opening`, `service_id`), `exclude` set and `service_id` filter, `evaluate_work` returns incidents that all satisfy 4.3a.1 to 4.3a.6, at most 5, in `(severity, -count)` order, and contains every qualifying incident that ranks within the first 5 (oracle); approved proposals satisfy 4.3b, at most 5; an incident of the `unassigned` pseudo service appears only when `service_id == "unassigned"`; and a user denial in the current opening hides the incident while the same denial with `decision_origin == "hub"` does not. **Validates: Requirements 4.3, 4.4**
8. *(optional)* **Reopen returns a denied incident.** *For any* history where an incident was denied by a user in opening `k`, the incident is absent from the work set while `opening == k` and present again when it reopens with `opening > k` and no active proposal. **Validates: Requirements 4.3 (a)**
9. *(optional)* **Long poll bounds.** *For any* timeout in 1 to 25 (clamped by the route), `next` returns within `timeout + tick`; at most 4 waiters are held at once and the others raise `TooManyWaiters`; after any mix of returns, cancellations and `close()`, `waiting == 0`. **Validates: Requirements 4.2, 4.5, 4.6**
10. **Snippet safety.** *For any* service id, code folder and workspace root (including spaces, quotes, `#`, `*`, `[`, newlines removed by validation, Windows drive letters), `build_snippet` output has no control character other than newline, contains the mode name in its first two lines, never contains `allow-all`, `trust-all-tools`, a shell `match` of `*`, or any of `curl`, `wget`, `python -c`, `powershell`, ` nc `, contains an `fs_write` allow line iff the folder is a strict descendant of the workspace whose relative path has no glob character, and that line's pattern is relative (no drive letter, no leading `/` or `\`) and ends with `/**`. **Validates: Requirements 5.4, 5.5, 5.8**
11. *(optional)* **Hook decision.** *For any* stdin text, state file text, URL and fake hub reply, `decide` never raises, returns exit 0 only when the input is unparseable, the target is the state file, or the fake hub replied `ok: true` without the note for a valid `p<n>`; and never contacts a non-loopback host. **Validates: Requirements 5.9**

Success Criterion 4 names the `files`, `verify_proposal` and `service_events` limits under property tests: properties 1, 5 and 7 are required for that reason.

## Testing Strategy

**Unit (pytest, example-based), `tests/unit/`:** `test_agent_limits.py`, `test_agent_feed.py`, `test_agent_digest.py`, `test_agent_codesearch.py`, `test_agent_verify.py` (one test per sub-criterion 3.1a to 3.1f), `test_agent_watch_eval.py` (4.3a.1 to 4.3b), `test_agent_watch_queue.py` (return within 1.5 s of an incident opening, timeout, 4 + 1 waiters, `close`, cancel, ingestion not blocked), `test_agent_snippet.py`, `test_agent_service.py`, `test_agent_routes.py` (every route in the table through `httpx.ASGITransport` on a small app), `test_agent_wiring.py` (B6, shutdown, `create_app` includes the router), `test_mcp_agent_tools.py`, `test_agent_route_table.py` (R1.1, owner table, no admin dependency), `test_no_process_spawn.py` (R4.10), `test_watcher_prompt.py`, `test_agent_readme.py`, `test_mesh_verify_write_hook.py`, `test_agent_modes.py` (M2). Helpers in `tests/unit/agent_helpers.py`: `make_agent(mode)` for `mode in ("bare", "store")`, a `FakeState` implementing `StateView`, and builders for incidents and proposals.

**Property tests** `tests/pbt/test_agent_*_properties.py` for properties 1 to 11. Async tests use `asyncio.run` inside plain test functions.

**Existing tests.** Every existing test passes unchanged except `tests/unit/test_kiro_assets.py::test_power_docs_name_only_real_tools_and_cover_all` (R6.2; gets the allow-list of parameter and field names; this is the one intentional change owned here, the other is `auto-approval`'s `test_proposals_endpoint_fields`). Existing MCP tests monkeypatch a zero-argument `_client_factory`; the per-request timeout design keeps them valid. `test_mcp_registers_expected_tools` asserts a subset, so the new tools do not break it. The new tests extend `test_kiro_assets.py` by appending functions.

**Manual (human, live Kiro):** the optional 25 s call (F2); permissions in the IDE and the `~/.chimericmesh/**` globs (L9, F5); the hook's input fields and the `write` matcher (F8b); Success Criteria 1, 2 and 3.

## Review resolutions

| Id | Decision | Where it becomes work |
|---|---|---|
| M2 | `AgentPorts` names the three injected interfaces: `StateView`, `ServiceRegistry`, `ApprovalReader` (the last is a three-method read-only protocol). The module never imports `hub`. One parametrized test runs it against a plain `Hub()` with `create_app(hub)` and against the store-backed hub. The map's M2 row should list these (map is not edited here). | tasks 1.1, 7.1, 12.1 |
| L2 | The power-docs test also needs every MCP tool in the skills and in `getting-started` as a `` - `name`: `` line. `getting-started` lists all 17; the skills mention all; the allow-list of parameter and field names is added in the same test. R6.2 should say so on its next revision. | task 9.2 |
| L4 | `CODE_SUFFIXES` and `SKIP_DIRS` are made public in `core` by `auto-approval` task 1.1 (golden test of `find_code_hints` is theirs). This module imports `SKIP_DIRS` and `SOURCE_SUFFIXES` and pins both in its skeleton test; it does not edit `diagnostics.py`. | tasks 1.1, 3.1 |
| L6 | The demo precondition is a **user action**: the working tree's `examples/legacy_app/upstream.py` must have the defect again (for example `git checkout -- examples/legacy_app/upstream.py`). Neither this spec nor any task edits or restores that file; the README and the final checklist state it. | tasks 11.1, 11.2, 12.2 |
| L8 | R3.1 split into 3.1a to 3.1f, R4.3 into 4.3a.1 to 4.3a.6 and 4.3b (tables above); one test each, named by id. `requirements.md` is not edited. | tasks 4.1, 5.1 |
| README | The hands-off test-command risk (R5.11(b)), the two modes, the trusted-workspace need (F7), static rules (F6), the user-scoped file (F5) and the `PermissionRequest` hook not being part of V1 (F8) go into the README when this module is implemented. | task 11.1 |
| L9 | `~/.chimericmesh/**` deny globs are verified live; the README names what remains if they do not match (name-based denies, Kiro's read prompt; the hook covers writes only). | tasks 9.1, 11.1, 12.2 |

**Branch `fix/proposal-ids-and-recheck` (read with `git show` and `git diff` only; not checked out or merged).** Two changes are carried: (1) re-verify the proposal (status, action, incident) immediately before acting: into the `mesh-watcher` prompt, the `remediation-loop` skill and `watch-mode`, strengthened by `verify_proposal` (tasks 9.1, 9.2); (2) the long-outage overflow test `test_long_outage_keeps_running_with_bounded_positive_delays` in `examples/legacy_app/tests/test_resilience.py` (task 11.2). The `p<n>-<tag>` id format and everything built on it (`hub.py`, `commands.py`, `mcp_server.py` changes, `test_proposal_ids.py`, the "ids look like p1-k7f" prompt and skill sentences) is **rejected** (D5); ids stay `p1`, `p2`. The branch's edit of `mesh-responder.json` is not carried because R5.3 and B8 keep that profile unchanged (open item O4).

**Responses to the design review, round 1** (ids `R1-H1`, `R1-M1` and so on; they do not reuse the ids above). The last column gives the place; tasks carry `(R1-xx)` checklist lines.

| Finding | Response | Where |
|---|---|---|
| H1 | Fixed: `files` are absolute; relative entries fail 3.1c; hook absolutizes; prompt says so; new test cases | "Verify" (path form), prompt contract, hook flow; tasks 4.1, 9.1, 10.1 |
| M1 | Fixed: `is_disconnected` per tick; table row and G4 reworded; real disconnect test | `WatchQueue`, Error Handling; tasks 5.2, 7.2 |
| M2 | Fixed: `timeout_graceful_shutdown=3`, signal path calling `aclose`; real `uvicorn.Server` test | "Wiring", G5; task 7.3 |
| M3 | Fixed: workspace root found by walking up to `.kiro/`; root named in snippet | "Wiring", O8; tasks 6.1, 7.3 |
| M4 | Fixed in prompt contract and O10 (no resolve, add to `exclude_ids`, tell the user); optional exclusion is O14 | prompt contract; tasks 9.1, 9.2 |
| M5 | Fixed: regex matcher `^(fs_write|fs_append|str_replace)$` | "Optional hook"; task 10.2 |
| M6 | Fixed as far as possible: check moved to 9.3 (the first point where tool and profile exist), not optional; `CHIMERICMESH_WATCH_MAX_WAIT` fallback; README names the file the IDE reads | limits, O16; tasks 1.2, 7.2, 9.3, 11.1 |
| M7 | Fixed: skip single future events, stop after 200 consecutive, set `truncated` | "Digest"; task 2.2 |
| L1 | Fixed: parameter dropped | facade; task 7.1 |
| L2 | Fixed: "three-method" | Decision 2, review table; task 1.1 |
| L3 | Fixed: counter released by thread completion | facade; task 7.1 |
| L4 | Fixed: reason `rule approval time is unknown` | 3.1b; task 4.1 |
| L5 | Fixed: `samefolder` | 3.1b; task 4.1 |
| L6 | Needs a requirement change: open item O13; optional sub-task 4.4\*; README wording meanwhile | O13; tasks 4.4\*, 11.1 |
| L7 | Fixed by the prompt rule (option 1); no requirement change | prompt contract; task 9.1 |
| L8 | Caveat in snippet text and a manual check (O16); no live lookup possible here | snippet; tasks 6.1, 12.2 |
| L9 | Documented in Error Handling and README | Error Handling; task 11.1 |
| L10 | Fixed: tests record the result for the current tree and for the restored defect; baseline snapshot and hashes taken in task 1 | tasks 1.5, 9.1, 11.2, 12.2 |
| Minor | `exclude_ids=None`; hash comparison instead of `git show HEAD:` | MCP table; task 9.1 |

## Needs from other modules

Flagged for the user. Nothing here changes an approved design; every item has a fallback.

**Needs from store.** (1) None is required. (2) Optional, recommended later only if the digest is too slow: `StateView.events_since(since_ts, *, service_id, limit)` so the digest does not binary-search ids (fallback: the binary search and page loop in `collect_window`, capped at 20 000 events). (3) Optional: a change notification on the hub (for example `Hub.add_listener(callback)` called after an incident opens or a proposal changes) so `WatchQueue.kick()` wakes waiters at once. Fallback: the 0.5 s tick (open item O9).

**Needs from services.** (1) The callable returned by `make_require_admin` should have `__name__ == "require_admin"`; the route-table test (no route behind a tool depends on it) looks for that name and falls back to a `require_admin` substring of `__qualname__`. (2) Nothing else: `registry.get` already returns the live code folder synchronously.

**Needs from auto-approval.** (1) `ApprovalManager` already provides `approval_of`, `rule` and `status` with the fields used here. (2) Add this module's literals to the endpoint allow-list of `tests/unit/test_approvals_static.py` (their task 8.2 says `agent-loop` does it): `/api/services`, `/api/services/{id}`, `/api/services/{id}/events`, `/api/services/{id}/digest`, `/api/services/{id}/code-search`, `/api/proposals/{id}/verify`, `/api/watch/next`; task 8.2 here edits that allow-list. (3) The 1800 s window is `ApprovalSettings.rule_approval_ttl` (not env-settable); this module keeps `RULE_APPROVAL_WINDOW = 1800` and a test asserts equality with `ApprovalSettings.from_env({}).rule_approval_ttl`. A public constant would remove the duplicate (optional).

**Needs from discovery.** Accepted as recommended: A1 (the R4.10 allow-list names exactly `fsperms.py` and `discovery/runner.py`, task 1.3) and A2 (the route-table test lists the six discovery routes with owner `discovery`, none with a tool, none reachable through an MCP path; task 8.2). The broader wording is the fallback.

**Needs from dashboard (contract only; the dashboard design is being written separately).** The dashboard may call `GET /api/services/{id}/permissions-snippet` (`text/plain`, 404 unknown service, 422 no code folder or bad `test_command`) and show its text. It adds its own routes to the owner table `tests/unit/route_owners.py` (created here, task 8.2) in its own tasks. The dashboard does not call `wait_for_work`.

**Needs from the map (next revision, not edited here).** Row M2 should name `StateView`, `ServiceRegistry` and `ApprovalReader` for `agent-loop`; the `agent-loop` row's file list should include `agent/feed.py` and `agent/routes.py`.

## Open items for the user

Every item below is resolved: the user accepted each recommended answer on 2026-11-18, and the design is built on those answers. None changes a requirement; `requirements.md` is not edited in this phase.

| Id | Question | Resolution |
|---|---|---|
| O1 | The digest window and its binary search use the event's message time `ts`, not the time the hub received it. Replayed old logs then give an empty digest. | Resolved (accepted 2026-11-18): message time `ts` for V1; the response carries `scanned`. Live traffic and the demo storm use current timestamps. |
| O2 | R1.7 (cap and strip log text "in routes behind a tool") also reads as covering the existing routes `/api/events` and `/api/incidents`. Capping them changes existing output, and the map's B-list has no such change. | Resolved (accepted 2026-11-18): R1.7 applies to the routes this module owns. The existing routes stay as they are. A later change is offered if `recent_events` floods the agent's context. |
| O3 | R5.3 says the existing power skills do not change; R6.1 says `remediation-loop` and `getting-started` are updated, existing steps and rules stay. | Resolved (accepted 2026-11-18): R6.1 wins (it is specific and additive). `mesh-responder.json` and `mcp.json` stay byte-identical (file comparison test). |
| O4 | The branch edits `mesh-responder.json` for the re-verify rule. | Resolved (accepted 2026-11-18): the re-verify rule goes to the `mesh-watcher` prompt and the skills only. `mesh-responder.json` stays unchanged (R5.3, B8). `p1`, `p2` ids stay. |
| O5 | R5.9 names `.kiro/hooks/mesh-verify-write`, but "without the opt-in the hook is not installed". While installed, every write in the workspace needs an active proposal. | Resolved (accepted 2026-11-18): the two files ship in `.kiro/hooks-optional/`; the user copies them to `.kiro/hooks/` to opt in (removes them to opt out). |
| O6 | R1.4 does not restrict the file suffix of code search. | Resolved (accepted 2026-11-18): search only `SOURCE_SUFFIXES` (code, D4). It keeps configuration files and their secrets out of agent context. |
| O7 | R1.1 names `min_severity` without a direction. | Resolved (accepted 2026-11-18): events at least as severe, `severity <= min_severity` (syslog numbering, lower is worse). |
| O8 | The hub does not know the open Kiro workspace. | Resolved (accepted 2026-11-18): `CHIMERICMESH_WORKSPACE_ROOT`, default the nearest ancestor of the hub's working directory that contains `.kiro/` (else the working directory; R1-M3). A code folder equal to the workspace root, or with glob characters in its relative path, gets no allow line (fail safe). |
| O9 | Wake-up of a held request. | Resolved (accepted 2026-11-18): a 0.5 s tick plus an event; no hub change. The hub listener (Needs from store 3) is added only if latency matters. |
| O10 | An incident whose proposal is `done` but that is not resolved returns to the work list (R4.3(a) only excludes active proposals). | Resolved (accepted 2026-11-18): it returns to the work list. The prompt tells the agent to resolve right after `complete_proposal` **only when the fix is confirmed** (event rate fell); otherwise to add the incident to `exclude_ids`, tell the user and continue (R1-M4). The `exclude_ids` list is empty after a restart of the loop, so a `done` but unresolved incident then returns once: the agent must call `list_proposals` for the incident first (prompt rule) and not propose again without the user. Excluding such incidents in `evaluate_work` would change R4.3 and is O14. |
| O11 | The empty long-poll result includes empty `incidents` and `approved_proposals` lists next to `status: "timeout"`. | Resolved (accepted 2026-11-18): the empty lists are included (simpler for the agent). |
| O12 | The snippet includes a commented allow for the single file `.chimericmesh-active-proposal` for hook users. | Resolved (accepted 2026-11-18): the commented allow is included. Without it every hook-guarded edit would prompt for the state file. |
| O13 | (R1-L6) R3.1(e) says a user approval without declared files skips the file comparison, so `verify_proposal` returns `ok` for `.env` or `.git/x` inside the code folder, and the hook relies on verify. Closing it means a protected-name check (`PROTECTED_PATTERNS`, not the extension rule) for every file whatever the approval, which narrows R3.1(e) and needs a requirement revision. | Resolved (accepted 2026-11-18): as the design states it, yes, add it: reason `file <n> is a protected file`, in 3.1e. The R3.1(e) wording change waits for the next requirements revision. Residual risk, kept documented: until sub-task 4.4\* is built, the README says the hook does not protect these names and the profile `fs_write` denies do. Sub-task 4.4\* is written test-first and is now buildable. |
| O14 | (R1-M4) An incident whose latest proposal in this opening is `done` and that is not resolved could be excluded from the work set. This changes R4.3. | Resolved (accepted 2026-11-18): not now. The prompt rule plus `exclude_ids` is enough for V1; revisit if a rehearsal shows repeat proposals. |
| O15 | (R1-H1) The absolute-path rule is placed in 3.1c, which R3.1(c) words only as a count and length check. | Resolved (accepted 2026-11-18): read as an interpretation of "a valid path". No wording change to R3.1(c) now. |
| O16 | (R1-L8, R1-M6) Two Kiro facts I cannot check here: the `permissions.yaml` schema and shell `match` semantics, and whether the IDE honours a profile's `requestTimeout`. | Resolved (accepted 2026-11-18): the user checks both in the IDE at task 9.3 (manual, before the README is finalised); this stays a task. Fallback if the 25 s call fails: set `CHIMERICMESH_WATCH_MAX_WAIT` to a smaller value (for example 10) in the MCP server environment of the profile. |

## Effort estimate

Based on measured velocity: the store runs about 5 to 11 minutes per top-level task and about 2.5 minutes per sub-task, tests included. This module has 12 top-level tasks and 33 sub-tasks, of which 3 are optional property tasks. Task 10 (the optional hook) is not in the map's 1.5 day figure and can be dropped or deferred without touching anything else.

| Part | Count | Agent build time |
|---|---|---|
| Top-level tasks (wiring, checkpoints, test-suite runs) | 12 | 60 to 130 min |
| Required sub-tasks at about 2.5 min | 30 | about 75 min |
| Optional property sub-tasks at about 3 min | 3 | about 10 min (skippable) |
| Fix-ups after merging with the `hub.py`, `api.py` and `mcp_server.py` edits of earlier modules and the first full-suite run | | 20 to 40 min |
| **Total agent build time** | | **about 2.5 to 4 hours; plan 3 hours** (of which the hook is about 15 min) |

Human-in-the-loop work (separate from build time):

| Item | Time |
|---|---|
| Review and approve this design and `tasks.md`, answer O1 to O12 | 25 to 35 min |
| Restore the demo defect in `examples/legacy_app/upstream.py` (user action, L6) | 5 min |
| Live Kiro checks: optional 25 s call (F2) 10 min; permissions in the IDE and `~/.chimericmesh/**` globs (L9, F5) 20 min; hook input fields and the `write` matcher (F8b) 15 min | 45 min |
| Success Criteria 1, 2 and 3 live, one rehearsal each (token entered in the dashboard for `!approve`) | 55 min |
| README review (modes, test-command risk, honest limit) | 10 min |
| Browser check of the snippet text in the dashboard | counted in `dashboard` |
| Demo rehearsal of the full storm to auto-fix path with real clocks | 30 min |
| Merge approval after `auto-approval` is merged | 10 min |
| **Total human time** | **about 3 to 3.5 hours** |
