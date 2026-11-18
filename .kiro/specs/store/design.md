# Design: store (V1)

Requirements: [requirements.md](requirements.md). Capability map: [docs/v1-capability-map.md](../../../docs/v1-capability-map.md) (module `store`, B1, B2, B7, B9, B10, B13, B14, B16, B19, B20). Baseline: [legacy-mesh](../legacy-mesh/design.md).

## Overview

`store` adds a local SQLite database (stdlib `sqlite3`, no new dependency) under the hub. It keeps events (bounded), incidents, proposals, an append-only audit log and durable counters, and it gives every later module one place to persist data: a migration registry, a transaction API, named counters and the audit log. It also owns the proposal state machine and the reopen rule, so no other code path can break them.

Four decisions shape everything else:

1. **One connection, one writer thread.** A single `sqlite3.Connection` lives on a dedicated thread (`store-writer`). After startup, every read and write goes through it and the asyncio loop never touches SQLite. `Store.open` itself (header guard, migrations, `clean_shutdown` and counters) runs on the opening thread before the writer exists. Every later call, including the `Hub.__init__` load (`read_sync` with `allow_blocking=True`) and `max_id_sync`, is submitted to the writer thread (started by the first of them) and blocks the caller (T1, T10; review round 3, M4). Event rows are queued and written in batches; everything else is an awaited transaction.
2. **The hub keeps its in-memory working set in both modes.** A bare `Hub()` is exactly today's hub. A store-backed hub mirrors the store in memory (incidents in the tracker, all proposals, the 2000-event ring) and writes through. Higher modules read state through one `StateView` interface that `Hub` implements in both modes.
3. **Ids are durable counters.** Proposal ids come from a counter row incremented in the same transaction as the insert, with a floor of the highest stored number (D5). Event ids come from an in-process counter seeded from a durable high-water mark.
4. **The store never imports the hub.** It receives callables (`announce`, `now`) and an `AuditSink`. The reopen rule is one class (`IncidentLifecycle`) that works with or without a database.

Technology stack (locked on approval): Python 3.11+, stdlib `sqlite3` and `threading`, FastAPI/pydantic as today, pytest and Hypothesis for tests. No SQLAlchemy, no `aiosqlite`.

## Architecture

### Where `store` sits

`store` is in the second dependency group and may import only `core` (map, "Dependency direction"). It reaches the hub only through injected callables.

```
core (events, diagnostics, parsers, __init__)
  ^
store (store/*, fsperms.py)            <- this spec
  ^
services -> connectors, discovery, auto-approval -> agent-loop -> dashboard (REST/SSE only)
  ^
wiring (hub.py, api.py, commands.py, listeners.py, mcp_server.py, notify.py, replay*.py, __main__.py)
```

### File-to-group table (resolves M1)

The map left four kinds of file unassigned. This table is the complete assignment for the files that exist today plus the store files. The dependency test (task 1.3) holds it and fails on any `.py` file under `src/chimericmesh/` that has no row.

| File | Group | Reason |
|---|---|---|
| `__init__.py` (package root) | `core` | Version string only. Static test: it imports nothing from `chimericmesh`. |
| `events.py`, `diagnostics.py` | `core` | Stdlib only. |
| `parsers/*` including `parsers/__init__.py` | `core` | Pure functions, stdlib only. |
| `store/*` including `store/__init__.py`, `fsperms.py` | `store` | A package `__init__.py` belongs to its package's group and imports only from that package. |
| `notify.py` | `wiring` | Imports `httpx` (third party), so it cannot be `core` (`core` allows no third-party import; imports between `core` files are allowed, since they are the same group). Only `hub.py` uses it. Lower modules that need an incident summary receive it through an injected callable. |
| `replay.py`, `replay_irc.py` | `wiring` | Command-line tools that open sockets and use `urllib`. The map's "`core` replay helpers that only read files" does not describe them. They import `parsers` (core) and each other, which `wiring` may do. |
| `hub.py`, `api.py`, `commands.py`, `listeners.py`, `mcp_server.py`, `__main__.py` | `wiring` | As in the map. |

Where `httpx` code lives: only in `wiring` (`notify.py`, `mcp_server.py`). The dependency test adds a second assertion: no file outside `wiring` imports a third-party package other than the ones its group is allowed (`core`, `store`: none). Future groups (`services/*`, `connectors/*` ...) are added to the table by their own specs.

### Package layout

```
src/chimericmesh/
  fsperms.py          owner-only permissions (the only store-group code that starts a process: icacls)
  store/
    __init__.py       re-exports: Store, StoreConfig, MigrationRegistry, Migration, Tx, errors, ports
    errors.py         StoreError and subclasses
    config.py         StoreConfig.from_env, resolve_db_path, resolve_home
    connection.py     open_connection: header check, user_version guard, pragmas, permissions
    migrations.py     Migration, MigrationRegistry, MODULE_BLOCKS, apply_migrations
    schema.py         DDL of migration store:1, register_core_migrations
    tx.py             Tx wrapper (execute/query/savepoint/counter_next/counter_add), run_callbacks, SubscriberError
    writer.py         StoreWriter thread: job queue, retries, health flags
    store.py          Store facade: open, close, transact, read, health, add_redaction
    audit.py          AuditLog: append/read, sanitising, redaction
    events.py         EventWriter: bounded queue, batching, retention, event queries
    incidents.py      incident row mapping, IncidentLifecycle (persist, reopen, announce)
    proposals.py      transition table, create/transition/load, HubAuthority, is_reserved_decider
    models.py         Proposal dataclass
    ports.py          StateView, AuditSink, Announce protocols
```

Core changes (stdlib only): `events.py` (`Event` gets `id`, `service_id`, `log_source_id`), `diagnostics.py` (`Incident` gets `service_id`, `opening`, `resolved_at`, `opened`; `IncidentTracker` honours `opened`, gets `reopen`, `install`, `preview_id`, `on_evict`).

Wiring changes: `hub.py` (store-backed mode, `StateView`, event ids, per-run flags), `api.py` (`create_app` store wiring, filters, error handler, stats keys), `__main__.py` (error exit).

### Injected callables

| Callable | Type | Given to | Used for |
|---|---|---|---|
| `announce` | `Callable[[str], Awaitable[None]]` | `IncidentLifecycle` | The `REOPENED <id>: <category> on <host>` line. The hub passes `lambda text: hub.say(hub.irc_channel, text)`. `say` records to the transcript (strips control characters) and `IrcServer` sends through `format_privmsg` (strips CR/LF/NUL), so the line is injection-safe like R3.2. |
| `now` | `Callable[[], float]` | `Store`, `IncidentLifecycle` | Timestamps and the reopen window. Default `time.time`. The hub passes its own `clock`. |
| `audit` | `AuditSink` (protocol: `async def append(actor, action, subject, detail="", *, service_id=None, rule_id=None, proposal_id=None) -> None`) | `IncidentLifecycle` | Reopen audit row. In a store-backed hub it is the store's `AuditLog`; with an injected bare hub it is the private `:memory:` store's `AuditLog` (R1.4c). With neither, no audit row is written. |

## Components and Interfaces

### Store facade (`store/store.py`)

```python
class Store:
    @classmethod
    def open(cls, path: str | os.PathLike[str], *, registry: MigrationRegistry,
             config: StoreConfig | None = None, now: Callable[[], float] = time.time) -> "Store": ...
    path: str                       # absolute path, or ":memory:"
    config: StoreConfig
    events: EventWriter
    audit: AuditLog
    def close(self, timeout: float = 5.0, *, clean: bool = True) -> None: ...   # drain, checkpoint, join; clean=False leaves clean_shutdown untouched (L14)
    async def transact(self, fn: Callable[[Tx], T]) -> T: ...        # awaited write, commits before returning
    async def read(self, fn: Callable[[Tx], T]) -> T: ...            # awaited read, no BEGIN
    def transact_sync(self, fn, *, allow_blocking: bool = False) -> T: ...
    def read_sync(self, fn, *, allow_blocking: bool = False) -> T: ...
    def health(self) -> StoreHealth: ...                             # store_ok, dropped_events, queue_depth, path
    def add_redaction(self, secret: str) -> None: ...                # audit detail never contains it
    previous_shutdown_clean: bool    # value of the clean_shutdown flag before this open wrote 0 (true for a new database)
    async def note_last_event_id(self, n: int) -> None: ...          # counter_raise("event_id", n); see Event id high-water mark
    def _abandon(self) -> None: ...                                  # test hook: simulate a crash, no drain, flag stays 0
```

**Writer-thread lifecycle (review 2, 20, 25).** The `store-writer` thread is `daemon=True` and is started lazily on the first submitted job or event row, not in `Store.open` (open itself runs the header check, migrations and counters on the caller's thread through the same connection before the thread exists, then hands the connection over: the connection is opened with `check_same_thread=False` and from then on touched by one thread at a time, the writer). `Store.__init__` registers `weakref.finalize(self, _stop_writer, state)` where `state` holds the queue, the stop flag and the thread but no reference to the `Store`, so an unreferenced store stops its thread and closes its connection. `_stop_writer` sets the stop flag, enqueues a wake-up sentinel and joins the thread for up to 1 second, so the cleanup is observable right after `gc.collect()` (L3). Consequences: an app built through `httpx.ASGITransport` (no lifespan, so `close` never runs) leaks nothing and cannot keep the interpreter alive, and `python -m pytest -q` exits normally. The writer delivers results through one helper, `_deliver(loop, future, result, exc)`, which calls `loop.call_soon_threadsafe(_set, future, result, exc)` where `_set` first checks `future.cancelled()` and catches `asyncio.InvalidStateError`; `_deliver` itself catches `RuntimeError` (loop already closed). A cancelled awaiter, a closed loop, or a test that runs `asyncio.run` twice on one store therefore never raises on the writer thread or in the loop. `Store.open` raises `StoreOpenError` when `sqlite3.sqlite_version_info < (3, 24)` (the upsert syntax needs it) and when `PRAGMA journal_mode=WAL` does not report `wal` for a file database.

`StoreConfig` (not env-settable except the five documented variables `CHIMERICMESH_DB`, `CHIMERICMESH_HOME`, `CHIMERICMESH_MAX_EVENTS`, `CHIMERICMESH_EVENT_TTL_DAYS`, `CHIMERICMESH_REOPEN_AFTER_SECONDS`) also carries the timing knobs tests override: `batch_rows=500`, `batch_ms=250`, `queue_limit=20000`, `trim_interval=60.0`, `flush_interval=30.0`, `flush_tick=5.0`, `persist_slots=256`, plus `max_events`, `event_ttl_days`, `reopen_after`.

`fn` receives a `Tx` and must touch the database only through it. `transact` may call `fn` up to four times (one try and three retries, T8, R7.4) when SQLite reports a retryable error (see Error Handling), so `fn` must have no side effects outside `tx`. The sync variants raise `RuntimeError` when called from a thread that runs an event loop, unless `allow_blocking=True` (only `Hub.__init__` and tests use it). `Store.open(":memory:")` makes a private in-memory database with the same behaviour and writes no file.

### Transaction handle (`store/tx.py`)

```python
class Tx:
    def execute(self, sql: str, params: Sequence[object] = ()) -> sqlite3.Cursor: ...
    def executemany(self, sql: str, rows: Iterable[Sequence[object]]) -> sqlite3.Cursor: ...
    def one(self, sql: str, params=()) -> sqlite3.Row | None: ...
    def all(self, sql: str, params=()) -> list[sqlite3.Row]: ...
    def counter_next(self, name: str, *, floor: int = 0) -> int: ...  # max(counter, floor) + 1, stored
    def counter_get(self, name: str) -> int: ...
    def counter_add(self, name: str, delta: int) -> int: ...         # counter += delta (events_total, evsrc:*), returns new value
    def counter_raise(self, name: str, value: int) -> None: ...      # counter = max(counter, value)
    def savepoint(self) -> ContextManager[None]: ...

class SubscriberError(StoreError):                 # .name: str, .cause: BaseException
    ...

def run_callbacks(tx: Tx, callbacks: Iterable[tuple[str, Callable[..., None]]], *args) -> None
```

**SQL rule (review 7).** `sql` must be a string literal or a module-level constant (a name bound once at import to a literal). The static test in task 10.2 rejects an f-string, concatenation, `%` or `.format` as the first argument of `execute`, `executemany`, `one` or `all` under `store/`. Two patterns cover everything the design needs: (a) optional filters use fixed SQL with `NULL`-tolerant parameters, for example `... WHERE (:svc IS NULL OR service_id = :svc) AND (:before IS NULL OR id < :before) ORDER BY id DESC LIMIT :limit`; the value `unassigned` selects a second module-level constant whose text says `service_id IS NULL`, chosen in Python by `if svc == UNASSIGNED`; `LIMIT` is always a parameter. (b) `PRAGMA user_version = <n>` cannot take a parameter, so it lives in exactly one helper, `_set_user_version(conn, n: int)` in `store/migrations.py`, which does `int(n)` and formats the string. Task 10.2 exempts that function by name and asserts that no other function is exempt. Identifiers that vary (counter names) are validated against `^[a-z][a-z0-9_.:-]{0,47}$` and passed as parameters, never interpolated. Callers that build a counter name from data (`evsrc:<source>`) sanitise it first with `counter_name_part(s) = re.sub(r"[^a-z0-9_.-]", "_", s.lower())[:30]`, so a source such as `"Weird Source!"` cannot make the validator raise and poison a batch.

**Callback failure semantics (review 1).** `run_callbacks` runs each named callback in order inside the caller's open transaction and passes it the same `tx`. If a callback raises, `run_callbacks` raises `SubscriberError(name, cause)`; the exception propagates out of the caller's `fn`, `transact` rolls the whole transaction back, and the awaiting caller receives the `SubscriberError`. This is what `services` R10.2 and R4.5 require: a failing subscriber rolls back the change and the error names the subscriber. There is no isolating variant in the store; if a module wants one it writes it itself. `after_commit` callbacks are not a store feature: the loop-thread caller runs them after `await store.transact(...)` returns, each in its own `try/except` that logs and continues.

### Migration registry (`store/migrations.py`)

```python
@dataclass(frozen=True)
class Migration:
    module: str                       # "store", "services", "connectors", ...
    version: int                      # globally unique, inside the module's block
    name: str                         # "store:0001-core-tables"
    apply: tuple[str, ...] | Callable[[sqlite3.Connection], None]

MODULE_BLOCKS = {"store": (1, 99), "services": (100, 199), "connectors": (200, 299),
                 "discovery": (300, 399), "approvals": (400, 499), "agent": (500, 599),
                 "dashboard": (600, 699)}

class MigrationRegistry:
    def register(self, module: str, version: int, name: str,
                 apply: tuple[str, ...] | Callable[[sqlite3.Connection], None]) -> None: ...
    def migrations(self) -> list[Migration]: ...      # sorted by version
    def highest(self) -> int: ...
```

How another module uses it: the module exposes `register_migrations(registry: MigrationRegistry) -> None` that calls `registry.register("services", 100, "services:0100-registry", ("CREATE TABLE services (...)", "CREATE INDEX ..."))`. The wiring layer (`create_app`) builds one registry, calls `register_core_migrations` and every module's `register_migrations`, and passes it to `Store.open`. There is no import-time global state, so a test can build a registry with only the modules it needs.

`register` raises `MigrationError` for a version outside the module's block, a duplicate version or a duplicate name. A tuple of statements is run one `execute` at a time (never `executescript`, which commits). A callable receives the connection inside the open transaction and must not commit. After a callable returns, the runner checks `conn.in_transaction`; if it is false the callable committed or rolled back, and the runner raises `MigrationError` naming the migration (DDL the callable already committed cannot be undone, which is why the check is a hard error and not a repair) (L6).

`apply_migrations(conn, registry)` runs at open, before the store accepts work (on the opening thread; the writer thread has not started yet, see the lifecycle paragraph). The `migrations` table (`version`, `module`, `name`, `applied_at`) records what ran. It is defined in exactly one place: the bootstrap step of `apply_migrations` runs `CREATE TABLE IF NOT EXISTS migrations (version INTEGER PRIMARY KEY, module TEXT NOT NULL, name TEXT NOT NULL UNIQUE, applied_at REAL NOT NULL)` before the first numbered migration, and it is the only statement outside a numbered migration. Migration `store:0001-core-tables` does not create it (M1). `PRAGMA user_version` holds the highest applied version. For each registered migration whose version is not in `migrations`, in ascending order: `BEGIN IMMEDIATE`; run the statements; insert the `migrations` row; set `user_version = max(user_version, version)`; `COMMIT`. On an exception: `ROLLBACK`, raise `MigrationError(name, cause)`; the DDL, the `migrations` row and `user_version` all roll back because SQLite DDL and the `user_version` pragma are transactional.

### Counters

Table `counters(name, value)`. `Tx.counter_next(name, floor)` does `value = max(stored, floor) + 1`, writes it and returns it, in the caller's transaction. A proposal id is `format_id("p", tx.counter_next("proposal", floor=max_proposal_seq(tx)))`. `max_proposal_seq` is `SELECT COALESCE(MAX(seq), 0) FROM proposals`. This is the D5 rule `max(counter, highest stored) + 1` (R3.5). `auto-approval` issues rule ids with `format_id("r", tx.counter_next("rule", floor=<max rule number>))`; `services` uses its own counter names the same way (R3.7). Other counters kept here: `event_id` (event id high-water mark, see Event writer), `events_total` and `evsrc:<source>` (R6.5).

For an in-memory `Hub()` there is no store: the hub increments `_proposal_seq` in an await-free block (atomic on the loop thread, T2) and the sequence restarts at `p1` on every process start (R1.4a, R3.9).

### Audit API (`store/audit.py`)

```python
class AuditLog:
    async def append(self, actor: str, action: str, subject: str, detail: str = "", *,
                     service_id: str | None = None, rule_id: str | None = None,
                     proposal_id: str | None = None) -> int: ...      # own transaction, returns row id
    def append_tx(self, tx: Tx, actor: str, action: str, subject: str, detail: str = "", **refs) -> int: ...
    async def read(self, limit: int = 100, before: int | None = None, *, service_id: str | None = None,
                   rule_id: str | None = None, proposal_id: str | None = None) -> list[dict]: ...
```

`append_tx` lets a caller put the audit row in the same transaction as the change it records (reopen, rule decisions). The class has no update or delete method, and the table has `BEFORE UPDATE` and `BEFORE DELETE` triggers that `RAISE(ABORT, 'audit_log is append-only')`, so a bug elsewhere in the code also cannot change a row. Sanitising: `actor`, `action`, `subject` and the three reference columns are cut to 100 characters, `detail` to 1000; characters matching `[\x00-\x1f\x7f-\x9f]` are removed; lone surrogates are replaced; every registered secret (`add_redaction`) is replaced by `[redacted]`; the userinfo part of any `scheme://user:pass@host` is replaced by `***`. `read` caps `limit` to 1..500 and returns newest first. The extra reference columns exist because the reopen row records a service id (R5.7) and `auto-approval` filters its audit view by service, rule and proposal.

### Event writer and queries (`store/events.py`)

```python
class EventWriter:
    def put(self, event: Event) -> None: ...                 # loop thread, never blocks, never raises; stamps `ingested = now()`
    async def barrier(self) -> None: ...                     # forces an immediate flush of everything queued so far (no waiting for the 250 ms timer, L5); returns when those rows are committed or dropped
    async def after(self, after_id: int, limit: int = 100, *, source: str | None = None,
                    service_id: str | None = None, before_id: int | None = None) -> list[Event]: ...
    async def newest(self, limit: int) -> list[Event]: ...   # startup refill of the ring
    def max_id_sync(self) -> int: ...                        # startup seed: max(max(events.id), counter event_id) + crash margin if the previous run was not clean
```

`put` appends a row tuple to a bounded `collections.deque` (maxlen 20000) under a `threading.Lock`, adds to the pending counter deltas (`events_total`, `evsrc:<counter_name_part(source)>`, applied with `Tx.counter_add`), and sets a `threading.Event` that wakes the writer. When the deque is full it drops the oldest row, increments `dropped_events` and logs one warning per minute (R7.3). Text fields are cut to 4096 characters (`message`, `raw`) and passed through `encode("utf-8", "replace").decode()` so a lone surrogate cannot raise `UnicodeEncodeError` on the writer thread.

### Proposal layer (`store/proposals.py`, `store/models.py`)

```python
PROPOSAL_STATUSES = ("pending", "approved", "denied", "done")

class HubAuthority:
    """Capability passed by hub code only. REST, IRC and MCP layers never construct it."""

def is_reserved_decider(by: str) -> bool: ...    # True for rule:, rule-, withdrawn:, expired:, expired- prefixes

def check_transition(old: str, new: str, *, origin: str, decided_by: str,
                     authority: HubAuthority | None) -> None     # raises InvalidTransition

def create_proposal(tx: Tx, *, incident_id: str, action: str, details: str, service_id: str | None,
                    incident_opening: int | None, now: float, extra: Mapping[str, object] | None = None) -> Proposal
def transition_proposal(tx: Tx, pid: str, new_status: str, *, decided_by: str, origin: str,
                        authority: HubAuthority | None, now: float,
                        approved_by_rule: str | None = None, result_note: str | None = None,
                        extra: Mapping[str, object] | None = None) -> Proposal   # raises NotFound, InvalidTransition
def get_proposal(tx, pid) -> Proposal | None
def list_proposals(tx, *, status=None, service_id=None, limit=None) -> list[Proposal]
def load_all_proposals(tx) -> list[Proposal]
```

The transition table, enforced inside `transition_proposal` in the same transaction that reads the current status (so two concurrent decisions cannot both win):

| From | To | Allowed when |
|---|---|---|
| `pending` | `approved`, `denied` | always; `origin` is `user` unless `authority` is given |
| `approved` | `done` | always |
| `approved` | `denied` | only with `authority`, `origin == "hub"` and `decided_by` matching `^rule-(revoked|expired|suspended):\S+$` |
| anything else, including every transition out of `denied` or `done` | | rejected: `InvalidTransition`, no change |

`origin` is `hub` only when `authority` is passed; without it `transition_proposal` forces `user`. `decided_by` is cut to 60 characters and is display text only: nothing reads authorship from it (R4.7). The in-memory hub calls the same `check_transition` in `Hub.decide` and `Hub.complete`, sets `decision_origin` (`user` unless the hub's own rule logic passes `HubAuthority`), and cuts `decided_by` to 60 characters, so R4.3, R4.5 and R4.7 hold in both modes (task 9.4 tests the bare hub). `DecideRequest.by` in `api.py` keeps `max_length=30`: REST and IRC names are at most 30 characters, and only hub-written values such as `rule-revoked:<id>` reach 60. `Proposal.to_dict()` still returns the nine baseline keys.

Resolution of L7: the reserved-prefix list is tightened to `rule:`, `rule-`, `withdrawn:`, `expired:` and `expired-` (case-insensitive). A user named `ruleman` or `expired1` is no longer refused. The map's wider wording (`rule`, `expired` as bare prefixes) is stricter than needed because authorship never depends on `decided_by`; `is_reserved_decider` is the one definition, and `auto-approval` applies it to the REST `by` field. The README documents the list.

`Proposal` (in `models.py`) keeps today's nine fields, so `Proposal.to_dict()` still returns exactly those nine keys and `test_proposals_endpoint_fields` stays unchanged in this module. The extended fields (`decision_origin`, `service_id`, `incident_opening`, `approved_by_rule`, `kind`, `files`, `estimated_lines`, `verification`, `files_changed`, `lines_changed`, `verification_result`) are dataclass attributes with default `None`, returned by `to_full_dict()`. `auto-approval` R9.7 switches the REST layer to `to_full_dict()` together with its own test change. `hub.py` re-exports `Proposal`, `PROPOSAL_STATUSES`, `MAX_PROPOSALS`, `MAX_ACTION`, `MAX_DETAILS` so existing imports keep working.

### Incident lifecycle (`store/incidents.py`)

```python
class IncidentLifecycle:
    def __init__(self, tracker: IncidentTracker, *, store: Store | None, announce: Announce,
                 now: Callable[[], float], reopen_after: float,
                 audit_getter: Callable[[], AuditSink | None] = lambda: None,
                 announced_getter: Callable[[str], int | None] = lambda _id: None,
                 persist_slots: int = 256, flush_interval: float = 30.0) -> None: ...
    def reopen_due(self, inc: Incident) -> bool                       # resolved and now - resolved_at >= reopen_after
    async def reopen(self, inc: Incident) -> None                     # inc was already reset by tracker.reopen (see sequencing rule)
    async def persist(self, inc: Incident) -> None                    # snapshot includes the hub's current announced marker
    def mark_dirty(self, inc: Incident) -> None
    def flush_due(self) -> list[IncidentRow]                          # dirty rows whose last write is >= flush_interval old
    async def flush_dirty(self) -> None                               # persists flush_due() rows; called by the hub's flush loop
    async def flush_all(self, deadline: float | None = None) -> None  # shutdown: every dirty row, stops at the deadline

@dataclass(frozen=True, slots=True)
class IncidentRow:                      # immutable snapshot; built on the loop thread
    id: str; host: str; service_id: str | None; category: str; signature: str; severity: int
    count: int; first_seen: float; last_seen: float; sample: str; sources: tuple[str, ...]
    resolved: bool; resolved_at: float | None; opening: int; opened: bool
    announced_count: int | None         # the hub's _announced marker at snapshot time; None = never announced
```

Rows map to `Incident` with `row_to_incident`; `hypothesis` and `remediation` are not stored but recomputed with `classify(sample)` at load (the sample is the first event's message, which is what classified it originally). `IncidentRow.from_incident` cuts `sample` to 4096 and `host` to 255 characters (review 21).

**Snapshots (review 3).** The loop thread builds an `IncidentRow` before it calls `store.transact`. The closure given to `transact` captures the row and plain values only; it never touches an `Incident`, `Proposal`, `Event` list or any other hub object, so the writer thread cannot read a half-updated incident and `json.dumps(sources)` cannot see a list change size. Mutating the incident after `persist` was submitted does not change what is written.

**`announced_count` (review 5, replaced in round 2 finding 4).** There is no `Keep` sentinel and no `CASE` in the upsert. `IncidentRow.from_incident(inc, announced_count)` is always built on the loop thread with `announced_count = announced_getter(inc.id)`, which reads the hub's in-memory `_announced` marker (set before the first `await`, see the ingest ordering rule). Every write of the row (open, milestone, resolve, reopen, a 30-second flush, the shutdown flush, a retried persist after a skipped one) therefore carries the current marker, and the upsert is a plain `INSERT ... ON CONFLICT(id) DO UPDATE SET <all row columns>`. A persist that was skipped under backpressure or a failing store loses nothing: the later flush stores the marker the announcement already used, so a restart cannot announce the incident again. NULL in the column means "never announced". At load the hub sets `_announced[id] = row.announced_count` for every loaded row whose value is not NULL, so the baseline branch `inc.id not in _announced` stays false after a restart.

**Flush timer (review 4).** `Hub.start()` (called from the app lifespan and by tests that need it) creates `asyncio.create_task(self._flush_loop())`. Every `flush_tick` seconds (5) the loop calls `lifecycle.flush_dirty()`, which persists every dirty incident whose last write is at least `flush_interval` (30) seconds old, whatever its event traffic. `Hub.aclose()` cancels the loop, then runs `flush_all`. A bare `Hub()` that was never started has no task, and its `persist` is a no-op, so nothing changes for existing tests. This makes the R5.1 bound true: a hard crash loses at most about 35 seconds of count growth (30 s interval plus one 5 s tick); the requirement text says 30 s, and the test allows the tick.

**Backpressure (review 13).** Only the first event of an opening (`inc.opened and inc.id not in _announced`, the first-open test of M6; `IncidentTracker.add` sets `opened` and the hub never sets it), milestone events (10, 50, 100, 500), `resolve` and reopen await a persist. `persist` first tries `self._slots` (an `asyncio.Semaphore(persist_slots)` taken with `acquire` only if `locked()` is false). When all 256 slots are busy or `store_ok` is false, `persist` does not wait: it marks the incident dirty, returns, and the caller continues to the announcement, as the Error Handling table already promises for failures. A stalled writer therefore leaves at most 256 awaiting tasks (plus the dirty set, bounded by the incident count), not 20 000.

The same class serves a bare hub: with `store=None`, `persist` does nothing, and `reopen` skips the transaction but still writes the audit row through `audit_getter()` if a sink is attached and announces. This is the single implementation of R5.7 for both modes (R5.9). `Hub.audit` is a property backed by `hub._audit`; the lifecycle reads it through `audit_getter`, so `create_app` can attach the private store's audit log after the hub was constructed (review 8).

**`service_id` rule (review 11).** `IncidentTracker.add` sets `Incident.service_id` from the first event of the cluster. If it is NULL and a later event carries a non-null `service_id`, the incident takes it once; a non-null value is never replaced by a different one. Reopen keeps it. The reopen audit row records the incident's current `service_id`, and a proposal copies `service_id` from the incident at creation, so `unassigned` returns exactly the proposals whose incident had no service at that time. Cases are tested in `test_tracker_lifecycle.py`.

### StateView and the in-memory seam (resolves M2, store side)

```python
class StateView(Protocol):
    def now(self) -> float: ...
    def get_incident(self, incident_id: str) -> Incident | None: ...
    def incidents(self, *, status: Literal["open", "resolved"] | None = None,
                  service_id: str | None = None, limit: int | None = None) -> list[Incident]: ...
    def get_proposal(self, pid: str) -> Proposal | None: ...
    def list_proposals(self, status: str | None = None, *, service_id: str | None = None,
                       limit: int | None = None) -> list[Proposal]: ...      # newest `limit`, oldest first within
    def recent(self, limit: int = 50, source: str | None = None) -> list[Event]: ...    # newest first
    async def events_after(self, after_id: int, limit: int = 100, *, source: str | None = None,
                           service_id: str | None = None) -> list[Event]: ...           # oldest first
```

`Hub` implements `StateView` in both modes. A higher module (`services`, `auto-approval`, `agent-loop`) receives two objects at construction: a `Store` (its own tables, counters, audit) and a `StateView` (incidents, proposals, events). It imports neither `hub` nor `api`. The `service_id` value `unassigned` selects rows whose `service_id` is NULL; `services` must therefore refuse `unassigned` as a service id (carried to the `services` design).

What each mode keeps:

| | Bare `Hub()` | `create_app(hub)` with that bare hub | `create_app()` / `python -m chimericmesh` |
|---|---|---|---|
| Events ring (2000) | memory | memory | memory, refilled from the store at start |
| Event rows | none | none | store, bounded |
| Incidents | tracker in memory, no rows | same | tracker plus rows; newest 5000 reloaded at start, older ids kept in `_stored_unloaded_ids` and rehydrated on the next event |
| Proposals | `OrderedDict`, cap 200, in-process counter | same | `OrderedDict` mirror of all rows, no cap, durable counter |
| Event id counter | in process, starts at 1 | same | seeded from the durable high-water mark |
| `hub.store` | `None` | `None` | the file-backed `Store` |
| App store (`app.state.store`) | none | private `:memory:` store: registry, offsets, rules, audit rows, counters of new modules | the same file-backed `Store` |
| Reopen audit sink | none | `:memory:` store's `AuditLog` | the store's `AuditLog` |

**Proposal write seam (M5).** `StateView` is read-only. Higher modules create, decide and complete proposals through a second protocol in `store/ports.py`, which `Hub` implements in both modes:

```python
class ProposalWriter(Protocol):
    async def create_proposal(self, incident_id: str, action: str, details: str = "", *,
                              extra: Mapping[str, object] | None = None) -> Proposal | None: ...
    async def decide(self, pid: str, approve: bool, by: str) -> Proposal | None: ...          # decision_origin = 'user'
    async def hub_transition(self, pid: str, new_status: str, *, decided_by: str,
                             approved_by_rule: str | None = None, result_note: str | None = None,
                             extra: Mapping[str, object] | None = None) -> Proposal | None: ...   # decision_origin = 'hub'
    async def complete(self, pid: str, note: str = "", *,
                       extra: Mapping[str, object] | None = None) -> Proposal | None: ...
```

`extra` keys are limited to the extended columns `kind`, `files`, `estimated_lines`, `verification`, `files_changed`, `lines_changed`, `verification_result`; an unknown key raises `ValueError` before anything changes. `approved_by_rule` is a named argument of `hub_transition` only, and `incident_opening` and `service_id` come from the incident. `hub_transition` is the only place that builds `HubAuthority()`, so the `HubAuthority(` scan (src only, L9) stays "only in `hub.py`". In a bare hub the extras are plain attributes of `Proposal` (default `None`), so `to_dict()` still returns the nine baseline keys and `to_full_dict()` the rest. Who gets what: `create_app` passes the hub as `ProposalWriter` to `auto-approval`; `agent-loop` receives only `create_proposal` and `complete` (a typed two-method view, `ProposalClient`); REST, IRC and MCP call `Hub.decide`, `create_proposal` and `complete` and never `hub_transition`. Each method follows T6, and each announcement is part of the shielded unit.

`create_app(hub)` never moves hub state into the private store. Existing tests pass an injected `Hub()` and so run in memory, with the 200-proposal cap, `p1` restarting each process, and no enforcement.

**`create_app` contract (review 8).** Signature: `create_app(hub: Hub | None = None, start_listeners: bool = True, *, store: Store | None = None) -> FastAPI`. The existing positional parameters keep their order, so every existing call works.

| Call | Store used (`app.state.store`) | `hub.store` | `hub.audit` | Closed by |
|---|---|---|---|---|
| `create_app()` | `Store.open(resolve_db_path())` | the same store | the store's `AuditLog` | lifespan shutdown (`hub.aclose()` then `store.close()`) |
| `create_app(hub)`, bare hub (`hub.store is None`) | private `Store.open(":memory:")` | stays `None` | set to the private store's `AuditLog` (`hub.audit = ...`) | `weakref.finalize` (no lifespan under `ASGITransport`); lifespan shutdown when present |
| `create_app(hub)`, store-backed hub | `hub.store` (no private store) | unchanged | unchanged (the hub already has the store's `AuditLog`) | the caller that built the hub; lifespan shutdown calls `hub.aclose()` only |
| `create_app(store=s)` (no hub) | `s` | a new store-backed hub on `s` | `s.audit` | the caller; lifespan calls `hub.aclose()` only |

Two more rows (review 15):

| Call | Store used (`app.state.store`) | `hub.store` | `hub.audit` | Closed by |
|---|---|---|---|---|
| `create_app(hub_bare, store=s)` | `s` (the caller's store, for the new modules' tables, counters and audit) | stays `None` (hub state stays in memory) | set to `s.audit` only when `hub.audit` is `None`; an audit sink already attached is never overwritten | the caller |
| `create_app(hub_store_backed, store=s)` with `hub.store is not s` | not allowed | | | raises `ValueError` |

The same "only when `None`" rule applies to the private `:memory:` store in the second row of the first table: `hub.audit` is set only when it is `None`. Other modules' tests use `create_app(bare_hub, store=mem_store)` to run with an injected store and no persistence of hub state. The fourth and third rows of the first table are what the integration tests use, because a fake clock is injected through `Hub(store=..., clock=...)`. `hub.audit` is assigned after construction through the `audit_getter` described under `IncidentLifecycle`.

**`events_after` (review 9).** `GET /api/events` without `after_id` returns newest first (baseline). With `after_id` it returns events with a greater id, oldest first, up to `limit`; `source` and `service_id` filters apply before the limit. Algorithm for a store-backed hub: let `ring_min` be the lowest id in the ring. If `after_id >= ring_min - 1`, answer from the ring. Otherwise `await store.events.barrier()` first (the queue can hold up to 20 000 uncommitted rows during a storm, so "2000 events old" does not imply committed; the barrier costs one queue wait on a rare path), then read ids `after_id + 1 .. ring_min - 1` from the database (rows lost to drops appear as id gaps), then append the ring events in order, stopping at `limit`. When the ring is empty (a fresh store), `ring_min` is undefined and the answer comes from the database only (after the barrier). A bare hub serves from the ring only. Both paths are tested at the boundary `after_id == ring_min - 1`.

Proposal reads stay synchronous in both modes because the store-backed hub keeps a full mirror in memory (about 1 KB per proposal; 10 000 proposals is about 10 MB, and the per-service rate limit makes that a long way off). The database is the arbiter for transitions; the mirror is updated only after the commit (T6).

### Hub changes (`hub.py`)

- `Hub.__init__` gains `store: Store | None = None`, `reopen_after: int | None = None` (None reads `CHIMERICMESH_REOPEN_AFTER_SECONDS`, warn and use 300 when invalid, R5.11), `audit: AuditSink | None = None`. A store-backed hub loads state synchronously in `__init__` through `store.read_sync(..., allow_blocking=True)`.
- `Hub.ingest` returns the tagged `Event` (resolves L1). It assigns `event = dataclasses.replace(event, id=self._next_event_id())` before anything else, so the ring, the stream and the stored row all see the same id. `Event.id` is declared `id: int = field(default=0, compare=False)`, so two events that differ only in id still compare equal and existing equality tests keep passing. Connectors' `submit` returns the value of `ingest`. `api.py` uses the returned value in `/api/ingest` only: `tagged = await hub.ingest(event); return tagged.to_dict()`, so the returned event dict carries its `id` (R6.1). `/api/demo/storm` discards the return value of `ingest` and returns `{"injected": n, "at": now}` today; it needs no edit and its output stays unchanged (B21). Stored and streamed events carry the id through `GET /api/events`.

**`Event` field order (L7).** `Event.id`, `Event.service_id` and `Event.log_source_id` are appended after `ts`, in that order, so positional construction `Event(source, host, app, severity, message, raw, ts)` keeps working.

**`Incident.to_dict()` (review 12).** `Incident.to_dict` is overridden: it returns `asdict(self)` minus `opened`, and includes `service_id`, `opening` and `resolved_at` (`null` while the incident is open). `opened` is internal state, so it never reaches REST, MCP or the dashboard.

**`preview_id` (review 10).** `IncidentTracker.preview_id(event) -> str | None` returns the id `add` would use, or `None` in exactly the cases where `add` returns `None` (severity above 4, or an empty signature). The reopen path calls it first, so an info-level event or an event with no signature never reopens a resolved incident.
- `Hub.resolved` becomes a read-only property returning `{i.id for i in tracker clusters if i.resolved}` (existing tests only test its truthiness). `Incident.resolved` and `resolved_at` are the source of truth. `Hub.resolve` sets `resolved_at` only when the incident was not already resolved, then persists.
- Guided workflow (B16, R4.8, review 10): `workflow()` uses per-run counters incremented in the hub, none of them read from persisted state: `_run_events` (in `_count`, replaces `total_events > 0` for `traffic`), `_run_opened` (incremented when an incident opens or reopens in this run, replaces `bool(incidents())` for `incidents`), `_run_proposals` (`propose`), `_run_decided` (`decide`) and `_run_resolved` (`resolve`). `ask` is already per process. For a bare hub the results are identical to today; after a restart with persisted data all five stay not done until the action happens in this run (tested in 9.2 and 11.3).
- Lifecycle of the hub: `Hub.start()` creates the flush task; `Hub.aclose(deadline: float = 5.0)` cancels it, runs `lifecycle.flush_all` and `store.close` against one shared deadline (see T9). `Hub.audit` is a property (see the lifecycle section).
- Failure semantics outside REST (review 12): `Hub.resolve` changes memory first, marks the incident dirty and never raises on a store failure (memory is the truth for incidents, T6). `Hub.create_proposal`, `decide` and `complete` raise `StoreError` before touching memory when the write fails, because the database is the arbiter for proposals. `commands.handle_command` catches `StoreError` and returns `["store unavailable, try again"]`, so the IRC connection and the terminal route stay up (the existing `test_terminal_never_500s` holds).
- Locking: no `Hub._lock` is added. In-memory state is touched on the loop thread only (T2), and the in-memory proposal counter is incremented inside an await-free block, which is atomic with respect to other coroutines.
- Startup load of incidents (round 2 finding 5): rows are never pruned, so the hub does not load them all. `Hub.__init__` runs, through `read_sync`, (a) `SELECT ... FROM incidents ORDER BY last_seen DESC LIMIT :max_clusters` (the newest `MAX_CLUSTERS`, 5000 rows) and installs each with `tracker.install`, setting `_announced[id]` from `announced_count`; and (b) `SELECT id FROM incidents ORDER BY last_seen DESC LIMIT -1 OFFSET :max_clusters`, whose ids become `_stored_unloaded_ids` (a `set[str]`; ids only, about 10 bytes of payload per row). `install` during the load must not trigger `on_evict`, because the load never exceeds `MAX_CLUSTERS`.
- Rehydrate on miss (rewritten in round 3, M3): `IncidentTracker(on_evict=...)` reports each evicted cluster; the hub adds its id to `_stored_unloaded_ids` when a row exists for it (a cluster that qualified and was therefore persisted; ids of never-persisted clusters are not added). The hub reads `diagnostics.MAX_CLUSTERS` at call time (`from . import diagnostics`, not `from .diagnostics import MAX_CLUSTERS`), so tests patch `diagnostics.MAX_CLUSTERS` (L8). Rehydration is **step 0 of `ingest`**, before the event changes any state, and is the only `await` before the persist step:
  1. `pid = tracker.preview_id(event)`. If `pid` is `None`, not in `_stored_unloaded_ids`, or already in the tracker, skip to the ordering rule (no await).
  2. Otherwise get or create the shared task `self._rehydrating[pid]` (`dict[str, asyncio.Task[bool]]`, `ensure_future(self._rehydrate(pid))`) and `await asyncio.shield(task)`. Concurrent events for the same id, and a cancelled caller, share one task and one read.
  3. `_rehydrate(pid)` does `row = await store.read(...)`, and then, with no `await` in between: `if pid not in tracker: tracker.install(row_to_incident(row))`; set `_announced[pid]` when `announced_count` is not NULL; `_stored_unloaded_ids.discard(pid)` (`discard`, never `remove`); returns `True`. A `StoreError` is logged, `store_ok` is false through the store health, the id stays in the set and the task returns `False`. A `finally` pops the id from `_rehydrating`.
  4. After the await, `ingest` re-checks the tracker before it applies any branch. On `True` the stored count, `opening`, `resolved` and `announced_count` continue instead of restarting at count 1. On `False` (read failed) no fresh cluster is created: the event goes to the ring, the counters and the event queue, but not to the tracker, so a partial cluster can never overwrite the stored row or be announced a second time, and the next event for that id retries the read. The incident count lags by those events only while the store read is failing. The set holds ids only, so it grows by about 100 bytes per stored incident, which is acceptable for a single-machine store (at 1 million incidents it is about 100 MB; incident rows are trimmed by no retention rule in V1, and the backlog item "incident retention" is recorded in the Responses section). This works after a restart as well as inside one process, because the set is built from the database at startup. 
- `stats()` adds `store_ok` and `dropped_events` (always `true` and `0` for a bare hub).

## Data Models

### SQLite schema (migration `store:1`, name `store:0001-core-tables`)

`PRAGMA user_version = 1` after this migration. The `migrations` table is **not** in this block: it is created only by the bootstrap step of `apply_migrations` (see the Migration registry section), so migration 1 cannot fail with "table migrations already exists" (M1). The migration creates the five tables below.

```sql
CREATE TABLE events (
  id            INTEGER PRIMARY KEY,                 -- assigned by the hub at ingest; stable across restarts
  ts            REAL    NOT NULL,
  source        TEXT    NOT NULL,
  host          TEXT    NOT NULL,
  app           TEXT    NOT NULL,
  severity      INTEGER NOT NULL CHECK (severity BETWEEN 0 AND 7),
  message       TEXT    NOT NULL,                    -- cut to 4096 characters
  raw           TEXT    NOT NULL DEFAULT '',         -- cut to 4096 characters
  service_id    TEXT,                                -- NULL = unassigned
  log_source_id TEXT,
  ingested      REAL    NOT NULL                     -- hub clock at put(); the TTL clock (not ts, which is the message time)
);
CREATE INDEX idx_events_ingested ON events (ingested);         -- TTL trimming
CREATE INDEX idx_events_service ON events (service_id, id);    -- per-service streams

CREATE TABLE incidents (
  id              TEXT PRIMARY KEY,                  -- first 10 hex characters of SHA-1(host|signature)
  host            TEXT    NOT NULL CHECK (length(host) <= 255),
  service_id      TEXT,
  category        TEXT    NOT NULL,
  signature       TEXT    NOT NULL,
  severity        INTEGER NOT NULL CHECK (severity BETWEEN 0 AND 7),
  count           INTEGER NOT NULL CHECK (count >= 0),
  first_seen      REAL    NOT NULL,
  last_seen       REAL    NOT NULL,
  sample          TEXT    NOT NULL CHECK (length(sample) <= 4096),
  sources         TEXT    NOT NULL DEFAULT '[]',     -- JSON array of source names
  resolved        INTEGER NOT NULL DEFAULT 0 CHECK (resolved IN (0, 1)),
  resolved_at     REAL,
  opening         INTEGER NOT NULL DEFAULT 1 CHECK (opening >= 1),
  opened          INTEGER NOT NULL DEFAULT 0 CHECK (opened IN (0, 1)),
  announced_count INTEGER,                           -- NULL = never announced; else count at last announcement
  CHECK (resolved = 0 OR resolved_at IS NOT NULL)
);
CREATE INDEX idx_incidents_service ON incidents (service_id, resolved);
CREATE INDEX idx_incidents_last    ON incidents (last_seen);

CREATE TABLE proposals (
  id                  TEXT PRIMARY KEY,              -- 'p' || seq
  seq                 INTEGER NOT NULL UNIQUE,
  incident_id         TEXT    NOT NULL,
  action              TEXT    NOT NULL,              -- <= 200
  details             TEXT    NOT NULL DEFAULT '',   -- <= 500
  status              TEXT    NOT NULL CHECK (status IN ('pending','approved','denied','done')),
  created             REAL    NOT NULL,
  decided_by          TEXT    NOT NULL DEFAULT '',   -- <= 60, display only
  decided_at          REAL,
  result_note         TEXT    NOT NULL DEFAULT '',   -- <= 300
  decision_origin     TEXT    CHECK (decision_origin IN ('user','hub')),
  service_id          TEXT,                          -- copied from the incident at creation
  incident_opening    INTEGER,
  approved_by_rule    TEXT,
  kind                TEXT,
  files               TEXT,                          -- JSON array
  estimated_lines     INTEGER,
  verification        TEXT,
  files_changed       TEXT,                          -- JSON array
  lines_changed       INTEGER,
  verification_result TEXT
);
CREATE INDEX idx_proposals_status   ON proposals (status);
CREATE INDEX idx_proposals_incident ON proposals (incident_id, status);
CREATE INDEX idx_proposals_service  ON proposals (service_id, created);

CREATE TABLE audit_log (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          REAL NOT NULL,
  actor       TEXT NOT NULL,
  action      TEXT NOT NULL,
  subject     TEXT NOT NULL,
  detail      TEXT NOT NULL DEFAULT '',
  service_id  TEXT,
  rule_id     TEXT,
  proposal_id TEXT
);
CREATE INDEX idx_audit_service  ON audit_log (service_id, id);
CREATE INDEX idx_audit_rule     ON audit_log (rule_id, id);
CREATE INDEX idx_audit_proposal ON audit_log (proposal_id, id);
CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit_log
  BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit_log
  BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;

CREATE TABLE counters (
  name  TEXT PRIMARY KEY,
  value INTEGER NOT NULL
);
```

The proposal columns for other modules (`kind` onward) are nullable so rows written by older versions load. `AUTOINCREMENT` on `audit_log` stops SQLite from reusing the id of a deleted last row. Counter names in use: `proposal`, `event_id`, `clean_shutdown`, `events_total`, `evsrc:<sanitised source>`; other modules add `rule` and their own.

Open-time guard (review 16, round 2 finding 1): the guard opens no SQLite connection, because a `mode=ro` connection to a WAL-format database creates `-wal` and `-shm` files (reproduced on Python 3.13 / SQLite 3.50.4). It reads the file header with plain `open(path, "rb")`: an empty file (0 bytes) is a new database; a non-empty file shorter than 100 bytes, or whose first 16 bytes are not `SQLite format 3\0`, raises `StoreCorruptError`; otherwise `user_version` is the 4-byte big-endian integer at offset 60, compared with `registry.highest()` (`SchemaTooNewError` when higher). Nothing is written, so the file bytes and the directory listing are unchanged after a graceful last close. This byte read is the first, no-write guard and a best effort only (M2): in WAL mode a committed `user_version` can still sit in the `-wal` file until a checkpoint (checked locally on SQLite 3.50.4: after `BEGIN; PRAGMA user_version=7; COMMIT` the file header read 0, `PRAGMA user_version` returned 7, the `-wal` file was 4152 bytes), and a crash skips the checkpoint that a graceful close runs. **Second guard (M2):** after the real connection is open and its pragmas are set, and before any write (before the `migrations` bootstrap, before `clean_shutdown = 0`), `open_connection` runs `PRAGMA user_version` and raises `SchemaTooNewError` when it is above `registry.highest()`, closing the connection first. WAL recovery at that point may touch `-wal` and `-shm`, and closing the last connection may checkpoint the WAL into the main file, so for a too-new database that was last closed by a crash the file is not byte-identical (open item O1 in the round 3 resolutions). Connection pragmas, set in this order: `busy_timeout=5000`; `journal_mode=WAL`; `synchronous=NORMAL`; `foreign_keys=ON`; `wal_autocheckpoint=1000`. The connection uses `isolation_level=None` so the code issues `BEGIN IMMEDIATE`, `COMMIT` and `SAVEPOINT` itself. `synchronous=NORMAL` in WAL mode survives a process crash and loses at most the last committed transactions on a power failure, which matches R5.1 (a crash may lose up to 30 s of counts).

### Retention (R6.3)

`StoreConfig`: `max_events` (`CHIMERICMESH_MAX_EVENTS`, integer 1 to 10 000 000, default 100 000), `event_ttl_days` (`CHIMERICMESH_EVENT_TTL_DAYS`, integer 1 to 3650, default 7). An invalid value logs one warning and uses the default. The writer keeps `approx_rows` in memory (seeded from `COUNT(*)` at open, increased by `cursor.rowcount` of each batch, decreased by each trim). It trims when (a) `trim_interval` (60) seconds have passed since the last trim, or (b) `approx_rows > max_events + 500` after a batch.

**Clock (review 6).** The TTL is measured on `ingested`, the hub clock at `put`, not on `ts`. `ts` is the message time (`parse_syslog` takes it from the log line), so replayed or delayed logs carry old `ts` values in any id order; trimming on `ts` would delete fresh replays at once and keep old rows. "Oldest" means lowest `id` (ids are assigned in ingest order).

**Statements and stop rule.** A trim runs two loops, each chunk in its own transaction so queued sync jobs run between chunks:

```sql
-- TTL: repeat while rowcount == 5000
DELETE FROM events WHERE id IN (SELECT id FROM events WHERE ingested < :cutoff ORDER BY id LIMIT 5000);
-- size cap: excess = approx_rows - max_events; repeat with min(5000, remaining) until remaining <= 0 or rowcount == 0
DELETE FROM events WHERE id IN (SELECT id FROM events ORDER BY id LIMIT :n);
```

`:cutoff` is `now - ttl_days * 86400`. The TTL loop stops when a chunk deletes fewer than 5000 rows. The cap loop recomputes `remaining = COUNT(*) - max_events` once at its start and stops when it reaches 0 or a chunk deletes nothing. Incidents, proposals and audit rows are never trimmed. After every trim the table holds at most `max_events` rows and none older than the TTL (by `ingested`); between trims it never holds more than `max_events + 500 + one batch`.

### Event id high-water mark

Mechanism (review 22, replaced in round 2 finding 6):

- `Store.open` reads the `clean_shutdown` counter before it writes anything and exposes it as `Store.previous_shutdown_clean: bool` (true for a new database or when the stored value is 1). Only then does it set `clean_shutdown = 0`. `Store.close` sets it to 1 as its last write.
- `EventWriter.max_id_sync()` returns `max(MAX(events.id), counter event_id) + (0 if previous_shutdown_clean else crash_margin)`, where `crash_margin = 2 * queue_limit + batch_rows` (40 500 with the defaults; L2). The `Hub` seeds its next-id counter from it. `max_id_sync` is a `read_sync` and runs on the writer thread (M4).
- Each batch transaction does `counter_raise("event_id", last_id_in_batch)`. Retention can empty the table (for example after a week offline) and ids still continue upward (R6.1).
- Graceful close: rows the shutdown deadline drops are never committed, so `events.id` and the counter can lag the ids already shown to clients. `Hub.aclose` therefore calls `Store.note_last_event_id(n)` with the last id it assigned, before `flush_all`; this runs `counter_raise("event_id", n)` as one small awaited transaction inside the shared deadline, and `Store.close` repeats the raise on the writer thread if the awaited call missed the deadline and the stored value is lower (the value is kept in `Store._last_event_id`, updated by `note_last_event_id` synchronously before it submits the write). A clean close therefore never leaves the counter below an id a client has seen.
- Crash (the process dies, flag stays 0): the high-water mark lags by the unwritten queue and batch, about `queue_limit + batch_rows` rows plus the rows that arrive and are dropped while a batch commits (L2). At storm rates above about 10 000 events per second a 50 ms commit drops more than 500 rows, so the margin is `2 * queue_limit + batch_rows` and the claim is "at typical rates"; beyond that the same residual risk as the next bullet applies. The margin keeps client cursors (`after_id`) valid, at the cost of a documented id gap.
- A stalled or failing writer is outside the margin's guarantee: ids keep rising in memory while rows queue, up to `queue_limit` rows, and beyond that the oldest rows are dropped, so the number of ids assigned but not durable can exceed the margin during a long outage followed by a crash. To keep the guarantee, `flush_dirty` (the 30-second timer) also calls `store.note_last_event_id(hub.last_event_id)` while the writer is failing, so the counter advances through the awaited-write path (which fails too when the disk is gone, so this is best effort and stated as a limit). The gap that remains is the case "disk unavailable for a long time, then the process crashes", where a restart may reuse ids that clients saw; the resulting duplicates are visible only to a client that holds a cursor across both events. This residual risk is accepted and documented in the README text of task 10.1.
- Test hook: `Store._abandon()` stops the writer thread without draining the queue and without setting `clean_shutdown`, and closes the connection. It simulates a crash for tests (named in tasks 3.2, 7.3 and 11.1). Nothing in production calls it.

Dropped rows leave gaps in the stored ids; the in-memory ring has none.

## Sequence diagrams

### Ingest, persist, incident open

```mermaid
sequenceDiagram
    participant L as Listener (loop thread)
    participant H as Hub (loop thread)
    participant Q as EventWriter queue
    participant W as store-writer thread
    participant IRC as IRC / transcript

    L->>H: ingest(event)
    H->>H: id = next_event_id(); ring.append; count; tracker.add (sync, no await)
    H->>Q: put(row) (non-blocking, may drop oldest)
    Q-->>W: wake
    H->>H: first open? inc.opened (set by tracker.add) and id not in _announced
    H->>W: transact(upsert incident, announced_count = count)
    W->>W: BEGIN IMMEDIATE; upsert; COMMIT
    W-->>H: result via call_soon_threadsafe
    H->>IRC: announce "[ERR] network on host x3 ..."
    H-->>L: tagged Event
    Note over W: independently: batch of up to 500 rows or 250 ms,<br/>one transaction (rows + counter deltas + event_id high-water mark)
    W->>W: executemany(events); counters; COMMIT
```

### Reopen

```mermaid
sequenceDiagram
    participant H as Hub (loop thread)
    participant T as IncidentTracker
    participant LC as IncidentLifecycle
    participant W as store-writer thread
    participant IRC as IRC / transcript

    Note over H: no await from here to the first await below
    H->>T: preview_id(event); get(id) -> inc (resolved, resolved_at = t0)
    H->>LC: reopen_due(inc)? now - t0 >= reopen_after
    alt window not passed
        H->>T: add(event) (count grows)
        H->>LC: mark_dirty(inc); return event (no announcement)
    else window passed
        H->>T: reopen(inc, event): resolved=false, opening+1, count=1, opened=true, severity=event.severity
        H->>H: _announced[id] = 1 (before any await)
        H->>LC: snapshot IncidentRow
        Note over H: ingest now awaits lifecycle.reopen and then returns the tagged event;<br/>it never falls through to the open or update branches for this event
        LC->>W: transact(upsert row, announced_count=1; audit_log 'incident_reopened')
        W->>W: BEGIN IMMEDIATE; upsert; audit insert; COMMIT
        W-->>LC: committed
        LC->>IRC: announce "REOPENED <id>: <category> on <host>" (once)
    end
```

If the commit fails, the in-memory incident stays reopened and dirty, the audit row is lost and logged, and the `REOPENED` line is still announced (announcement never waits on a failing store, as for first openings). With `store=None` the sequence is identical except for the `transact` step.

## Threading and transaction model

This section is the only statement of the model; other sections refer to it as T1 to T10.

- **T1. Threads.** Two kinds of thread touch the store. The event-loop thread runs `Hub`, listeners and every route (all routes are `async def`; a plain `def` route would run in a thread pool and break T2). One daemon `store-writer` thread per `Store` runs every job after open (lazy start, `weakref.finalize` cleanup, see the Store facade). The single `sqlite3.Connection` is created with `check_same_thread=False` because `Store.open` uses it on the opening thread before the writer starts; after that exactly one thread uses it at a time (the writer). Nothing else opens the database for writing. Reads go through the writer too. The one exception is `Store.open`, which runs on the opening thread before any job exists. `read_sync`, `transact_sync` and `max_id_sync` are ordinary jobs: they start the writer if needed, run on it, and block the caller on a `concurrent.futures.Future` (M4). The open-time header guard reads file bytes and opens no connection; the second guard (`PRAGMA user_version`) runs on the opening thread inside `open_connection`.
- **T2. Hub state and locking.** All in-memory `Hub` state (ring, tracker, proposals, counters, `_announced`) is read and written on the loop thread only. A critical section is a stretch of code with no `await`; the loop cannot run another coroutine inside it, so there is no lock for coroutines and none is added: `Hub` gets no `_lock`. The in-memory proposal counter, the transition check of a bare hub and the in-memory evaluate-and-decide of `auto-approval` are all await-free blocks. The writer thread never touches hub state (T3 snapshots). Plain `def` routes are forbidden because FastAPI would run them in a thread pool and break this rule (a static test lists the routes).
- **T3. Awaited writes.** These run as `await store.transact(fn)` and return only after `COMMIT`. The loop thread builds every value the closure needs, as an immutable snapshot (`IncidentRow`, a frozen `Proposal` copy, plain strings, numbers and tuples), before calling `transact`; `fn` never touches an `Incident`, `Proposal`, `Event` or other hub object. The awaited writes are: incident upsert (open, milestone 10/50/100/500, resolve, reopen, 30-second flush, shutdown flush), proposal create, decide and complete, audit rows, counters, and every write of other modules (registry, rules, tail offsets). Each is one transaction (`BEGIN IMMEDIATE` to `COMMIT`) on the writer thread.
- **T4. Batched writes.** Only event rows. `EventWriter.put` is non-blocking and returns before the row exists in SQLite. The writer collects a batch when 500 rows are pending or 250 ms have passed since the oldest pending row, and commits the batch (rows, counter deltas, high-water mark) in one transaction.
- **T5. Order.** Awaited jobs run in FIFO order and are served between event batches, so an awaited write waits for at most one batch (500 rows). There is no ordering guarantee between an event row and an incident row: a stored incident count can be ahead of the stored event rows by up to 250 ms. After a crash the incident count can be behind by up to 30 s (R5.1).
- **T6. Memory versus database.** Proposals: the database is the arbiter. The hub applies the change to its mirror only after the commit returns, and the transition is checked inside the transaction, so two concurrent decisions on one proposal give one winner and one `InvalidTransition`. **The commit and the mirror update are one uncancellable unit (round 2 finding 3).** `Hub.create_proposal`, `decide` and `complete` each define `async def _commit_and_mirror(...)` that does `await store.transact(...)` and then, with no `await` in between, updates `_proposals`; the public method runs `task = asyncio.ensure_future(self._commit_and_mirror(...))` and returns `await asyncio.shield(task)`. If the awaiting request is cancelled (client disconnect, MCP timeout, IRC drop, shutdown), the task still finishes, so a committed change is always mirrored, `get_proposal` never shows `pending` for an approved row, and a retried `!approve` gets `InvalidTransition` only when the decision really happened. **The announcement is part of the same unit (round 3, M7):** `_commit_and_mirror` ends with `try: await self.say(...) except Exception: log`, after the mirror update, so the `PROPOSAL pN ...`, `pN APPROVED by ...` and `pN DONE` lines go out exactly once per committed change even when the caller was cancelled, and a failing `say` cannot undo or hide the commit. The task's own exception is retrieved by a done-callback so it is not reported as "never retrieved". Shutdown awaits outstanding commit tasks (tracked in a set) inside the shared deadline. Incidents: memory is the truth for counts. The hub updates the tracker first (in the sync section), then writes behind. If a write fails, the incident stays in `_dirty` and the next flush retries.
- **T7. Subscriber callbacks.** `on_*` callbacks that a lower module registers (`services` R10) run on the writer thread, inside the open transaction, through `run_callbacks(tx, callbacks, ...)`. A callback may read and write through the `tx` it is given. It must not: `await` anything, call `Store.transact*`, `read*` or `AuditLog.append` (the writer thread would wait for itself; `Store` raises `StoreReentryError` when a job is submitted from the writer thread), touch hub state, start threads, or do network or file I/O. A callback that needs the hub records its wish in a row or a list and lets the loop-thread caller act after `transact` returns. **An exception in a callback aborts the transaction**: `run_callbacks` raises `SubscriberError(name, cause)`, `transact` rolls everything back, no `after_commit` runs, and the caller receives the error naming the subscriber (`services` R10.2, R4.5). `after_commit` callbacks are run by the loop-thread caller after `await store.transact(...)` returns, each in its own `try/except` that logs and continues; they are not part of the store.
- **T8. Retry.** A retryable SQLite error (busy, locked, full, I/O) re-runs the whole job up to 3 more times with 50 ms, 200 ms and 800 ms pauses on the writer thread. After that the awaiting caller receives a `StoreWriteError`, and an event batch is dropped and counted (see Error Handling).
- **T9. Shutdown (review 25).** The app lifespan, after `yield`, runs in this order: (1) close the listeners (they stop calling `ingest`); (2) `await hub.aclose()`, which cancels the flush task, awaits outstanding proposal commit tasks (T6), calls `store.note_last_event_id(last_assigned_id)`, then runs `lifecycle.flush_all(deadline)` and `store.close(timeout=deadline - now)` against one shared 5-second deadline (`deadline = monotonic() + 5`). `flush_all` stops submitting writes when the deadline passes and leaves the rest dirty (lost, logged once at error level); it never retries beyond the deadline. `Store.close` stops intake, lets the writer drain event rows until the deadline (remaining rows are dropped and counted), runs `PRAGMA wal_checkpoint(TRUNCATE)` if time is left, sets `clean_shutdown = 1`, closes the connection and joins the thread with the remaining time. If the deadline expires the thread is a daemon, so it cannot block exit; the connection is then left to the writer thread (it closes it when it unblocks, or the OS does at exit) and `close` only logs one error, because `sqlite3` connections must not be closed from another thread while a statement runs (L4). `close(clean=False)` skips the `clean_shutdown = 1` write; the app lifespan uses it when startup fails after `Store.open` (for example a port that cannot be bound), so a start that never served cannot mark a running first process clean (L14). The whole sequence therefore finishes within 5 seconds even when the store is failing (R1.7); the test covers a failing store.
- **T10. Blocking calls.** `transact_sync` and `read_sync` submit a job to the writer thread and block the caller until it finishes (they do not run on the calling thread; `Store.open` is the only code that does). They are allowed in `Hub.__init__` (startup load), in `Store.open`, and in tests; from a running event-loop thread they raise `RuntimeError` unless `allow_blocking=True`.

### Ingest ordering rule (applies T2, T3, T6)

`Hub.ingest` runs step 0, the only `await` before the persist step (M3): if the event's incident is stored but not loaded, `await asyncio.shield(self._rehydrating[id])` (see "Rehydrate on miss"). After step 0, and re-checking the tracker, there is no `await` until the persist step: assign id, append to the ring, update counters, `put` the event row, then exactly one of three branches, each decided and applied to memory before the first `await`: (reopen) `tracker.preview_id` finds a resolved incident whose window passed, so `tracker.reopen(inc, event)`, `_announced[id] = 1`, in-flight marker, then `await lifecycle.reopen(inc)` and `return event`, never falling through to the other branches; (open or milestone) `tracker.add` (which sets `inc.opened` itself, M6), then `first_open = inc.opened and inc.id not in _announced`, set `_announced[...]` and the marker, then `await lifecycle.persist(inc)` through the semaphore, then announce; (update) `tracker.add`, `mark_dirty`, no await. After the commit it announces. Because the tracker and `_announced` change before the first `await`, a second `ingest` for the same incident that runs during the await sees the new state and cannot open or reopen it again. Persist-before-announce means a crash between the two loses one announcement and never repeats it.

### Reopen announcement rule (resolves M4)

The map and R5.7 say "clear the announced flag" and also "announce `REOPENED` once". If the flag were cleared, the baseline branch (`inc.id not in _announced` and `count >= 3`) would announce the same opening again when the count reached 3. The rule here: before its first `await`, the hub sets `_announced[id] = 1` (the count after reopen); the reopen transaction stores `announced_count = 1`. This is the "announced marker is set to the current count" wording of M4; `requirements.md` R5.7 and map B14 still say "clear the announced flag", and the next requirements revision should use the M4 wording (backlogged, this phase edits only `design.md` and `tasks.md`). Because the marker is set before any `await`, two concurrent `ingest` calls on one resolved incident produce one `opening` increment and one `REOPENED` line (the second call sees an unresolved incident inside the window). No webhook is sent on reopen in V1: `REOPENED` goes to IRC and the transcript only; the `incident.opened` webhook (R3.3) fires for first openings as today. The `REOPENED` line is the announcement for that opening. Later events produce only the baseline milestone `UPDATE` lines (count crossing 10, 50, 100, 500), which are updates and not announcements. Because the marker is part of every `IncidentRow` snapshot (finding 4 of round 2), a skipped or failed persist cannot lose it: the next flush writes it. Result: exactly one opening-announcement per opening: one `[SEV] ...` line for opening 1 and one `REOPENED` line for each later opening. Because `REOPENED` is sent by `IncidentLifecycle` alone and `auto-approval` never announces a reopen, there is a single owner.

Sub-criteria of R5.7 used by the tests (resolves L8 inside the design; `requirements.md` is not edited in this phase):

| Id | Condition |
|---|---|
| 5.7a | The event matches a resolved incident and `now - resolved_at >= CHIMERICMESH_REOPEN_AFTER_SECONDS`. |
| 5.7b | The resolved flag and `resolved_at` are cleared. |
| 5.7c | `opening` increases by 1. |
| 5.7d | `count` becomes 1. |
| 5.7e | `opened` becomes true, so `get`, `create_proposal` and the watch list see a severity 3 or 4 incident with count 1. |
| 5.7f | `severity` becomes the severity of the reopening event. |
| 5.7g | The announced marker becomes 1 (one announcement per opening). |
| 5.7h | One audit row `incident_reopened` (incident id, new opening, service id) is written in the same transaction as the incident row. |
| 5.7i | One `REOPENED <id>: <category> on <host>` line is announced after the commit. |

On reopen the tracker also sets `first_seen = last_seen = event.ts` and `sources = [event.source]` for the new opening; `sample`, `category` and `signature` are unchanged. The reopen window uses the hub clock (`now`), not the event timestamp, so replayed old logs cannot reopen or block a reopen.

## Error Handling

| Operation | Failure | Recoverable? | Caller receives | Logged |
|---|---|---|---|---|
| Open: file is not SQLite (non-empty and shorter than 100 bytes, or the 16-byte header is not `SQLite format 3\0`; the guard reads bytes, not a connection) | corrupt or foreign file | Fatal | `StoreCorruptError(path)`; `create_app` propagates; `__main__` prints `store: <path> is not a valid SQLite database; it was left untouched` and exits with status 2 | error |
| Open: `user_version` higher than `registry.highest()` | database from a newer build | Fatal | `SchemaTooNewError(path, found, known)`; no table or header write, no WAL switch of a new file. Detected by the byte guard, or by the post-open `PRAGMA user_version` check when the value is still in a `-wal` file that was never checkpointed (M2) | error |
| Open: migration raises | bug or conflicting schema | Fatal | `MigrationError(name, cause)`; migration rolled back; `user_version` unchanged | error |
| Open: cannot create the home directory or file (permission, read-only disk) | environment | Fatal | `StoreOpenError(path, cause)` | error |
| Open: `fsperms` fails (no `icacls`, unsupported OS) | cosmetic | Recoverable | none; the store opens | one warning |
| Write: busy or locked beyond the 5 s busy timeout | another process holds the file | Recoverable (T8) | after 3 retries `StoreBusyError` | warning per retry, error once when exhausted |
| Write: disk full (`SQLITE_FULL`) or I/O error | environment | Recoverable (T8) | `StoreFullError` / `StoreWriteError` | as above |
| Write: `fn` raises a non-SQLite exception | caller bug | Not retried | the exception, transaction rolled back | none (caller decides) |
| Write: corruption during run (`SQLITE_CORRUPT`, `NOTADB`) | fatal for the store | Not retried | `StoreWriteError`; `store_ok` stays false | critical once |
| Event batch: retries exhausted | as above | Rows lost | none (fire-and-forget); rows added to `dropped_events`, counter deltas put back for the next batch | error once per exhaustion |
| Event queue over 20 000 rows | storm | Rows lost | none; `dropped_events` += 1 per dropped row | one warning per minute |
| `ingest`: incident write fails | store down | Recoverable | none; `ingest` never raises on a store error; incident stays in `_dirty`; announcement still goes out; audit row of that transaction is lost and logged | error |
| REST route: `StoreError` raised by a hub method | store down | Recoverable | `503 {"detail": "store unavailable"}` through one exception handler in `api.py`; no database error text is returned | error |
| IRC command or terminal calls a hub method that raises `StoreError` | store down | Recoverable | `handle_command` catches it and returns `["store unavailable, try again"]`; the IRC connection stays open, the terminal route returns 200 | error |
| `Hub.resolve`: store write fails | store down | Recoverable | none; memory is changed, the incident is dirty and retried by the flush loop | error |
| `Hub.create_proposal`/`decide`/`complete`: store write fails | store down | Recoverable | `StoreError` raised before memory changes (REST 503, IRC text above) | error |
| Subscriber callback raises inside `transact` | caller bug | Not retried | `SubscriberError(name, cause)`; whole transaction rolled back | error |
| Writer result delivered to a closed loop or a cancelled future | test, shutdown, client disconnect | Ignored | none (`RuntimeError`, `InvalidStateError` and `future.cancelled()` handled in `_deliver`; the commit stays and is mirrored, T6) | debug |
| Event batch row violates `events.id` uniqueness (`IntegrityError`) | second process on the file, or an id-seed bug | Not retried | event rows use `INSERT OR IGNORE`; ignored rows are counted into `dropped_events` (rows in the batch minus `rowcount`), the batch commits, one error is logged per minute | error |
| `EventWriter.put` after `Store.close` | late `ingest` task after shutdown | Rows lost | never raises; the row is counted in `dropped_events` | debug |
| `Hub.ingest` after `aclose` | queued task after `Server.close()` (open IRC/Telnet connections survive it) | Memory only | updates ring and tracker, skips persistence and announcements that need the store, never raises | debug |
| Persist semaphore exhausted or `store_ok` false | store stalled | Recoverable | `persist` returns at once, incident stays dirty, announcement proceeds | one warning per minute |
| `transition_proposal`: invalid transition | caller error | Not an error to the system | `InvalidTransition`; `Hub.decide`/`complete` turn it into `None`, REST into the existing 409 | none |
| `transition_proposal`: unknown id | caller error | | `NotFound`; REST 404 as today | none |
| Open: too-new database whose last close was a crash (known limitation O1) | newer build | Fatal | `SchemaTooNewError`; SQLite may merge the `-wal` into the main file when the refusing connection closes: files change, no row is lost or altered | error |
| `Store` used after `close` | caller bug | | `StoreClosedError` | none |
| Job submitted from the writer thread | callback misuse (T7) | | `StoreReentryError` | error |

`store_ok` is false from the moment a write exhausts its retries (or corruption is seen) until the next successful commit, and each flip is logged once. `/api/stats` returns `store_ok` and `dropped_events`. `dropped_events` counts both queue overflow and rows lost with a failed batch, so `persisted + dropped == ingested` always holds (Success Criterion 3). Incident, proposal and audit writes are never dropped silently: they raise to the caller or stay dirty.

Input validation:

| Input | Rule | On failure |
|---|---|---|
| `CHIMERICMESH_DB` | optional; `:memory:` or a path; a relative path is resolved against the current directory once at startup (an explicit user choice) and logged absolute | startup error if the parent cannot be created |
| `CHIMERICMESH_HOME` | optional; `~` expanded; default `~/.chimericmesh`; never derived from the working directory by default | as above |
| `CHIMERICMESH_MAX_EVENTS`, `CHIMERICMESH_EVENT_TTL_DAYS` | integers in the ranges above | warn once, use default |
| `CHIMERICMESH_REOPEN_AFTER_SECONDS` | integer 0 to 86400 | warn once, use 300 |
| `limit` on `/api/events` | 1 to 500, default 50 | **silently clamped** to 1..500, exactly as the baseline route does (`max(1, min(limit, 500))`); 422 only for a non-integer (FastAPI default) |
| `limit` on `/api/proposals` | 1 to 500, default 200 | **422** outside the range (`Query(ge=1, le=500)`) |
| `limit` on `/api/incidents` | optional, 1 to 500; **no limit when absent**, as in the baseline (review 24) | **422** outside the range (`Query(ge=1, le=500)`) |
| `status` on `/api/incidents` | `open` or `resolved` | 422 |
| `after_id` on `/api/events` | integer >= 0 | 422 (`Query(ge=0)`) |
| `service_id` filters | string up to 64 characters; `unassigned` means NULL | 422 over length |
| Text written to the store | capped per column; lone surrogates replaced; embedded NUL and control characters kept in `events` (log fidelity) but stripped in `audit_log` | never raises |

Invariant ownership: id uniqueness and the transition table are enforced in `store/proposals.py` inside the transaction (SQLite `UNIQUE` on `seq` and `CHECK` on `status` are the last line). Append-only audit is enforced by the missing API and by SQLite triggers. `opened`, the reopen rule and the announced marker are enforced in `IncidentLifecycle`, the one place that writes them. The 200-proposal cap belongs to the in-memory hub only. File permissions belong to `fsperms.py` only.

## Security

- **Permissions.** `fsperms.secure_dir(path)` and `fsperms.secure_file(path)` are the only permission code. On POSIX they `chmod` 0700 and 0600. On Windows they run `icacls <path> /inheritance:r /grant:r <user>:(OI)(CI)F` (directory) or `<user>:F` (file) with `subprocess.run([...], shell=False, timeout=10)`, where the user name comes from `getpass.getuser()` and must match `^[\w.\- $@\\]+$` or the step is skipped with a warning. The home directory is secured only when this process created it. The database file is created empty with mode 0600 (`os.open(..., O_CREAT|O_EXCL, 0o600)`), then secured, then opened; the `-wal` and `-shm` files SQLite creates inherit the mode on POSIX and are secured after the first write on Windows. Residual risk, stated in the requirements: the Windows ACL step is manual-only, and owner-only permissions do not stop an agent that runs as the same user.
- **Raw column.** `raw` is truncated to 4096 characters, as is `message`. Log text may contain secrets; it never goes into audit rows (the design passes only ids, counts and names) and never into log messages of the store (it logs paths, counts and SQLite error names only).
- **No secrets.** The store holds no token. `add_redaction(secret)` lets `services` register the admin token so that no audit `detail` can carry it even by mistake; URL userinfo is also scrubbed. A canary test covers both (R8.4). "No full log lines in `detail`" is enforced where this module writes audit rows: the only writer here is `incident_reopened`, whose `detail` is built from the incident id, the opening number and the service id only; a test sends an event whose message and signature carry canary text and asserts that the `detail` of the stored row contains neither the sample nor the signature. Other modules own the same test for their audit rows (carried to their designs).
- **SQL.** Parameters only. A static test rejects dynamic SQL strings under `store/` (task 10.2).
- **Untrusted text.** Log text is stored and returned as data. The `REOPENED` line is built from `category` (from a fixed rule table) and `host` (log-derived, so untrusted), and goes through `hub.say` and `format_privmsg`.
- **No network.** Nothing in `store` opens a socket.

## Correctness Properties

Each property is a universal statement for Hypothesis. "Restart" means a graceful close of the store followed by opening the same file again, unless the property says "crash". Success Criterion 7 names these properties: 1 and 3 (id uniqueness and monotonicity), 6 (transitions), 18 (audit caps), 11 (events after id), 10 (retention) and 12 (reopen). In `tasks.md`, a property sub-task is required unless its number carries `*` (Kiro's optional marker). Optional: properties 16 and 17 (task 2.4), 2 and 21 (task 3.3), 19 (task 7.5), 15 and 20 (task 11.7). Required: 1, 3, 4 (5.3); 6, 7, 8 (5.4); 9, 10, 11 (7.4); 12, 13, 14 (8.4); 18 (4.2); 5 (11.6). Property 5 (R6.1, Success Criterion 7 "monotonicity across restarts") is required.

1. **Proposal ids are never reused.** *For any* sequence of create, delete-row (direct SQL), trim and restart operations, every id the store issues is different from every id issued before in the same database. **Validates: Requirements 3.1, 3.3, 3.4**
2. **Next id formula.** *For any* stored counter value `c` and any set of stored proposal numbers with maximum `m` (including `c < m` and `c > m`), the next id is `p<max(c, m)+1>`. **Validates: Requirements 3.5**
3. **Id format.** *For any* issued proposal id, it matches `^p[1-9][0-9]*$`, and numbers issued in one run are consecutive. **Validates: Requirements 3.2**
4. **Concurrent creation.** *For any* number of proposals created concurrently from several tasks, all ids are distinct and the set equals `p(n0+1)..p(n0+k)`. **Validates: Requirements 3.6**
5. **Event ids strictly increase across restarts.** *For any* sequence of ingests, retention trims (including trimming the whole table) and graceful restarts, and also across a simulated crash (writer killed without `close`, which seeds the next run with the `crash_margin`), the ids assigned to successive events are strictly increasing, and `Event.to_dict()` and `GET /api/events` carry the same id as the stored row. **Validates: Requirements 6.1**
6. **Transition table.** *For any* pair (from, to) of statuses and any `origin`, `decided_by` and presence of `HubAuthority`, `check_transition` accepts exactly: pending to approved, pending to denied, approved to done, and approved to denied with authority, origin `hub` and a `rule-revoked:`, `rule-expired:` or `rule-suspended:` `decided_by`; nothing else, and a rejected call changes no row. **Validates: Requirements 4.3**
7. **Origin is independent of `decided_by`.** *For any* `decided_by` text (including `expired...`, `rule-...`, `withdrawn:...`), a decision made without `HubAuthority` is stored with `decision_origin = 'user'`, and with `HubAuthority` with `'hub'`; no operation reads authorship from `decided_by`. **Validates: Requirements 4.7**
8. **Proposal round trip.** *For any* proposal built from arbitrary text and optional fields, restarting returns the same id, status, `decided_by`, timestamps, action and details, and the stored lengths respect the caps (200, 500, 300, 60). **Validates: Requirements 4.2, 4.5**
9. **Event row fidelity and caps.** *For any* event, the stored row equals the event with `message` and `raw` truncated to 4096 characters and lone surrogates replaced, and the writer never raises. **Validates: Requirements 6.1, 6.2**
10. **Retention bounds.** *For any* sequence of inserts (with `ts` generated independently of insertion order, and `ingested` non-decreasing) and trims with `max_events = M` and TTL `d`, after each trim the table holds at most `M` rows, no row with `ingested` older than `now - d` days, every row removed by the size cap has a lower `id` than every row that remains (L10), and incident, proposal and audit counts are unchanged. The trim terminates (the stop rule of the Retention section). **Validates: Requirements 6.3, 8.6**
11. **Events after an id.** *For any* stored events and any `after_id` and `limit`, `after` returns events with id greater than `after_id`, in ascending order, at most `limit`, and equal to the first `limit` such events. **Validates: Requirements 6.6**
12. **Reopen only after the window.** *For any* `reopen_after` R and any gap g between `resolved_at` and a matching event, the incident reopens if and only if `g >= R`; below R the count and the event log grow, the incident stays resolved, and nothing is announced; the same holds in a bare `Hub()` and after a restart. **Validates: Requirements 5.7, 5.8, 5.9**
13. **Reopen resets state.** *For any* resolved incident that reopens, afterwards `opening` is old plus 1, `count` is 1, `opened` is true, `severity` equals the reopening event's severity, `resolved` is false, `resolved_at` is null, and `get_incident` returns it. **Validates: Requirements 5.6, 5.7**
14. **One announcement per opening.** *For any* sequence of events, resolves and clock advances, the number of opening-announcements (the first `[SEV]` line plus `REOPENED` lines) equals the highest `opening` reached by announced incidents, exactly one per opening, even when more events follow the reopen. **Validates: Requirements 5.7, 5.2**
15. **Incident count continuity.** *For any* event counts `a` before a restart and `b` after, an opened incident has count `a + b`, is not re-announced, and keeps its resolved flag, also when the `b` events are delivered concurrently while the incident is stored but not loaded (one install, no lost count) and when a failing read precedes a successful one (round 3, M3). **Validates: Requirements 5.2, 5.3**
16. **Migration idempotence.** *For any* registry, opening the same database any number of times applies each migration exactly once and leaves the schema, the `migrations` rows and `user_version` the same after the first open. **Validates: Requirements 2.1, 2.2**
17. **Migration atomicity.** *For any* registry whose last migration raises at a random statement, the open fails, the database has the schema and `user_version` of the migrations before it, and the error names the failing migration. **Validates: Requirements 2.3, 2.4**
18. **Audit append-only.** *For any* sequence of appends and reads, ids strictly increase, every stored field respects its cap and contains no control character or registered secret, `read` is newest first and honours `before` and `limit`, and the store API exposes no update or delete. **Validates: Requirements 8.1, 8.2, 8.3, 8.5**
19. **Write accounting.** *For any* ingest bursts with a small queue limit and injected write failures, `persisted_rows + dropped_events == ingested_events` after the writer is idle. **Validates: Requirements 7.3, 7.4**
20. **Store-backed hub equals bare hub on incidents.** *For any* sequence of events, resolves and clock advances applied to a bare `Hub()` and to a store-backed hub, the lists of incidents (id, count, opening, resolved) and the announcement lines are equal. **Validates: Requirements 1.4, 5.9**

21. **Transaction atomicity under callback failure.** *For any* list of callbacks that each write rows and any position at which one raises, `transact` leaves the database exactly as before it, and raises `SubscriberError` naming that callback. **Validates: Requirements 2.4 (rollback mechanism shared with migrations); supports `services` R10.2**

## Testing Strategy

**Unit tests (pytest, example-based).** One file per component under `tests/unit/`: config and path resolution (R1.1 to R1.3, R5.11, R6.3 settings), fsperms (R1.5, POSIX asserts mode; Windows ACL skipped, manual), open errors (R1.6, R2.3), migration runner (R2), counters (R3.5, R3.7), audit (R8), proposal transitions (R4.3, R4.5 to R4.7), event writer (R6, R7.3 to R7.5), incident rows and lifecycle (R5), hub changes (R4.4, R4.8, R5.9, R6.1, R6.6), REST changes, README text test (R3.9), static scans (R3.8, SQL literals, dependency table).

**Additional example tests required by review.** Writer lifecycle: create and drop 50 unclosed `:memory:` stores, `threading.active_count()` returns to its starting value after `gc.collect()`, and a subprocess that creates an unclosed store exits within 5 seconds; `create_app(hub)` under `ASGITransport` without lifespan leaves no thread behind. Snapshots: mutate an incident after `persist` is submitted, the stored row equals the snapshot. Flush loop: one incident, 5 events, fake clock advanced 31 s, one loop tick, the row has count 5. `announced_count`: open, flush with `Keep`, restart, one event, no announcement. Backpressure: a store whose `transact` never returns, 5000 events, at most 256 pending tasks. Concurrency: two concurrent `ingest` calls on a resolved incident give one `opening` increment and one `REOPENED`. Seam: store-backed hub passed to `create_app`, both `events_after` paths and the boundary `after_id == ring_min - 1`. Failure paths: injected failing store through `handle_command` (IRC and terminal), `Hub.resolve`, `Hub.decide`. Shutdown with a failing store finishes within 5 s. Workflow: all five flags stay false after a restart with persisted data. Counter names: a source `"Weird Source!"` does not poison a batch. Static: no `def` route, SQL literal rule with the one named exemption, `core` allows intra-`core` imports only.

**Round 2 example tests.** Header guard: a too-new database created in WAL mode (`PRAGMA journal_mode=WAL`, then `user_version` set higher, closed) is refused with `SchemaTooNewError` and the directory listing and file bytes are identical before and after (no `-wal` or `-shm` appears); a 50-byte file raises `StoreCorruptError`; an empty file opens. Cancellation: for each of `create_proposal`, `decide` and `complete`, block the writer, cancel the awaiting task, release the writer, and assert the mirror equals the database and `get_proposal` shows the new status; no `InvalidStateError` is logged. Announced marker: open an incident, fill the persist semaphore so the later persist is skipped, flush, restart, send one event, no second announcement. Restart with `MAX_CLUSTERS` patched to 3 and 6 stored incidents: an event for an unloaded incident continues its stored count, `opening` and `resolved` and does not announce again. Event-id margin: `Store.previous_shutdown_clean` is false after `_abandon()` and the next `max_id_sync()` is above every id handed out; a graceful `aclose` with rows dropped by the deadline still leaves `event_id` at or above the last assigned id. Limits: `/api/events?limit=0` and `limit=9999` are clamped (200), `/api/proposals?limit=0` and `/api/incidents?limit=501` give 422. Info-level event never reopens a resolved incident (`preview_id` is `None`). `Incident.to_dict()` has no `opened` key and has `resolved_at: null` while open. `IntegrityError` batch: a duplicate id in a batch is ignored and counted in `dropped_events`. `put` after `close` counts a drop and does not raise. A source `"Weird Source!"`: the restored `total_by_source` key is the sanitised form `weird_source_` (see the next paragraph).

**`total_by_source` keys after a restart.** The counter is named `evsrc:<counter_name_part(source)>`, which lowercases and truncates, so a source with capitals comes back under its sanitised key. Today's sources are `syslog`, `irc`, `telnet`, `api` and `file`, all already lowercase, so nothing changes for them; for any other source the restored key is the sanitised one (stated here and tested).

**Property tests (Hypothesis, `tests/pbt/`).** Properties 1 to 21 above, in files named `test_store_*_properties.py`. Settings follow the existing pbt files (`max_examples` 40 to 100, `deadline=None` for anything that opens a database). Properties that need restarts use one `tmp_path` database per example. Arbitrary text strategies include lone surrogates and NUL.

**Existing tests that must pass unchanged.** Every file in `tests/unit` and `tests/pbt` today. Specifically: `test_irc_commands.py` (cap of 200 with `p6` first, `p1` then `p2`, operator rules), `test_web_terminal.py::test_proposals_endpoint_fields` (nine keys, kept by `Proposal.to_dict`), `test_stats_counters.py`, `test_diagnostics_api.py` and `test_commands_properties.py` (`PROPOSAL_STATUSES`); `hub.resolved` truthiness is asserted in `test_web_terminal.py` and `Hub().operators` in `test_irc_commands.py`. Success Criterion 5 holds: this module changes no existing test. The two tests the map lists as changing in V1 belong to other modules: `test_kiro_assets.py::test_power_docs_name_only_real_tools_and_cover_all` (`agent-loop` R6.2) and `test_proposals_endpoint_fields` (`auto-approval` R9.7, which switches REST to `to_full_dict()`).

**Isolated databases.** `tests/conftest.py` adds an autouse fixture that sets `CHIMERICMESH_HOME` to a `tmp_path` directory and deletes `CHIMERICMESH_DB`, so no test can open `~/.chimericmesh`. A `store` fixture opens `Store.open(tmp_path / "t.db", registry=core_registry())` and closes it after the test (Windows cannot delete an open file); a `mem_store` fixture uses `:memory:`. Restart tests close the store and reopen the same path. A `FakeClock` object is injected as `now` and as `Hub(clock=...)` for reopen and retention.

**Integration tests.** `tests/unit/test_store_integration.py` drives `create_app` with `httpx.ASGITransport` and a temporary database through the five Success Criteria that need a restart (1, 2, 4, 6 and the working-directory check of R1.2 using `monkeypatch.chdir` twice).

**Performance check (R7.2, Success Criterion 3).** `tests/unit/test_store_benchmark.py`, marked `slow` (registered in `pyproject.toml`; runs in the default suite). It builds a store-backed hub on a file database, ingests 5000 events at 1000 events per second from 200 distinct signatures (so about 200 incident-open commits happen during the run), and measures event-loop stall with a probe task that calls `asyncio.sleep(0.005)` and records the largest gap beyond its schedule. Assertions: all rows persisted within 5 seconds after the last event (or `persisted + dropped_events == 5000`), and maximum stall under 100 ms. The thresholds are tolerant on purpose: 3 times the expected values would still pass on a slow CI machine; set `CHIMERICMESH_SKIP_BENCH=1` to skip on very slow hardware.

## Resolution of review findings

| Finding | Resolution |
|---|---|
| M1 | File-to-group table above (`notify.py` and `replay*.py` are `wiring`, root `__init__.py` is `core`, package `__init__.py` follows its package, `httpx` code stays in `wiring`). |
| M2 (store side) | `StateView` protocol, implemented by `Hub` in both modes; the mode table; private `:memory:` store for the new modules; `Hub.audit` sink. |
| M3 | Threading and transaction model T1 to T10, benchmark includes about 200 incident-open commits. |
| M4 | Reopen announcement rule: announced marker set to 1, `REOPENED` is the one opening-announcement; property 14; task 8.3 test "reopen, two more events, one announcement". |
| L1 | `Hub.ingest` uses `dataclasses.replace(event, id=...)`; `Event.id` is a `compare=False` field; `ingest` returns the tagged event and `submit` returns that value. |
| L5 | The proposal-id scan (task 10.1) builds its regex from string fragments at runtime and excludes its own file, so the guard file does not match itself. |
| L7 | Reserved prefixes tightened to `rule:`, `rule-`, `withdrawn:`, `expired:`, `expired-`; one definition `is_reserved_decider`; documented in the README. |
| L8 | R5.7 split into sub-criteria 5.7a to 5.7i in the design; `requirements.md` renumbering is backlogged to the next requirements revision because this phase edits only `design.md` and `tasks.md`. |
| L2, L3, L4, L6, L9 | Not store findings; left to their modules and the map. |

### Responses to the design review (`.agents/tasks/store-design/review.md`)

| # | Sev | Response |
|---|---|---|
| 1 | HIGH | Addressed. `run_isolated` removed; `run_callbacks` raises `SubscriberError`, the whole transaction rolls back; `after_commit` is the caller's job (T7, Tx section, Property 21). |
| 2 | HIGH | Addressed. Daemon, lazily started writer, `weakref.finalize`, leak tests (Store facade, T1). |
| 3 | MED | Addressed. `IncidentRow` snapshot, rule in T3. |
| 4 | MED | Addressed. Hub flush task with a 5 s tick; crash-loss bound stated as about 35 s. |
| 5 | MED | Addressed in round 1 with a `Keep` sentinel; **superseded in round 2 (finding 4)**: no sentinel, every snapshot carries the hub's marker. `_announced` rebuilt at load. |
| 6 | MED | Addressed. `ingested` column and index, two-statement trim with stop rule, Property 10. |
| 7 | MED | Addressed. Fixed-SQL filter pattern, `_set_user_version` as the one named exemption. |
| 8 | MED | Addressed. `create_app` contract table (four rows), `audit_getter`, `Hub.audit` property. |
| 9 | MED | Addressed. `/api/ingest` returns the tagged event (storm was wrongly listed and is corrected in round 2 finding 2); `events_after` stitching algorithm (barrier added in round 2). |
| 10 | MED | Addressed. `_run_events` and `_run_opened` added to the per-run flags. |
| 11 | MED | Addressed. `service_id` assignment rule. |
| 12 | MED | Addressed. Error Handling rows for IRC, terminal, `resolve`, `decide`. |
| 13 | MED | Addressed. Persist semaphore (256) and skip when stalled. |
| 14 | MED | Addressed. One sequencing rule (marker before first `await`, return after reopen), no webhook on reopen, wording follow-up for R5.7/B14 backlogged to the next requirements revision. |
| 15 | MED | Addressed. In-memory hub uses `check_transition`, origin and the 60-character cap; `DecideRequest.by` stays 30; task 9.4. |
| 16 | LOW | Addressed. Header guard before the real connection; replaced in round 2 finding 1 by a byte-level read (a read-only URI connection creates `-wal` and `-shm`). |
| 17 | LOW | Addressed. Task 1.3 reworded (intra-`core` imports allowed). Backlogged to the next map revision: update the map's file-to-group table (`notify.py`, `replay*.py`, `__init__.py`) and the B10 / `auto-approval` R4.8 prefix list to match `is_reserved_decider`; the reason it is not done here is that this phase edits only `design.md` and `tasks.md`. See "Carried to other modules" below. |
| 18 | LOW | Addressed. Test on the `incident_reopened` row; other modules own theirs. |
| 19 | LOW | Addressed. `Tx.counter_add`, `counter_name_part`. |
| 20 | LOW | Addressed. Timing knobs in `StoreConfig`. |
| 21 | LOW | Addressed. `sample` 4096 and `host` 255, with CHECK constraints. |
| 22 | LOW | Addressed. Property 5 states graceful restart and adds the crash margin; the properties named in Success Criterion 7 are required tasks. |
| 23 | LOW | Addressed. No `Hub._lock`; await-free blocks are atomic. |
| 24 | LOW | Addressed. No default limit on `/api/incidents`. |
| 25 | LOW | Addressed. Listeners close first, one shared deadline. |
| extra | | SQLite version check (3.24+) in `Store.open`; closed-loop `RuntimeError` caught by the writer. |

### Responses to the second design review (round 2, same review file)

| # | Sev | Response |
|---|---|---|
| 1 | MED | Addressed. The guard reads the file header bytes (magic, `user_version` at offset 60), opens no connection; WAL-mode fixture and directory-listing assertion in task 2.2. |
| 2 | MED | Addressed. Only `/api/ingest` returns the tagged event; `/api/demo/storm` unchanged (B21). |
| 3 | MED | Addressed. T6 commit-and-mirror unit with `ensure_future` plus `shield`; `_deliver` handles cancelled futures and `InvalidStateError`; cancellation tests in task 11.3. |
| 4 | MED | Addressed. `Keep` removed; `IncidentRow.announced_count` always snapshots the hub marker through `announced_getter`; plain upsert. |
| 5 | MED | Addressed. Startup loads the newest `MAX_CLUSTERS` rows and the ids of the rest into `_stored_unloaded_ids`; rehydrate on miss works across restarts; restart test with `MAX_CLUSTERS=3`. Backlog: incident-row retention (not in R6.3, which covers events only). |
| 6 | MED | Addressed. `Store.previous_shutdown_clean`, margin inside `max_id_sync`, `note_last_event_id` at close, `Store._abandon()` test hook; the stalled-writer-then-crash residual risk is stated. |
| 7 | MED | Addressed. One rule per route in the Input validation table: `/api/events` clamps, `/api/proposals`, `/api/incidents` and `after_id` return 422. |
| 8 | LOW | Addressed. Optional markers are per property (see Correctness Properties); property 5 is required. |
| 9 | LOW | Addressed. Reserved-prefix list carried below; task 10.2 check says "outside the `core` and `store` groups". |
| 10 | LOW | Addressed. `preview_id` returns `None` where `add` does. |
| 11 | LOW | Addressed. `barrier()` before the database read; empty ring answers from the database. |
| 12 | LOW | Addressed. `Incident.to_dict` override drops `opened`; `resolved_at` is `null` while open. |
| 13 | LOW | Addressed. `INSERT OR IGNORE` for event rows, ignored rows counted in `dropped_events`. |
| 14 | LOW | Addressed. `put` after close is a counted drop; `ingest` after `aclose` updates memory only. |
| 15 | LOW | Addressed. `create_app(bare_hub, store=s)` row; `hub.audit` set only when `None`. |
| 16 | LOW | Addressed. Overview wording, five variables, `evsrc` key note, test-file attribution, xfail removal and the 10.1/10.2 task text fixed in `tasks.md`. |

### Carried to other modules

- Map and `auto-approval` R4.8 (B10): the reserved decider prefixes are `rule:`, `rule-`, `withdrawn:`, `expired:`, `expired-` (case-insensitive), one definition in `is_reserved_decider`. The map's earlier list (`rule`, `rule-`, `withdrawn:`, `expired`) must be updated to match.
- Map: file-to-group table gets `__init__.py` (`core`), `notify.py` (`wiring`), `replay.py` and `replay_irc.py` (`wiring`).
- `requirements.md` R5.7 and map B14: replace "clear the announced flag" with "set the announced marker to the current count".
- `services`: refuse `unassigned` as a service id.
- `requirements.md` R3.1 (L12): replace "under a lock" with "in an await-free block" (the design uses no lock, T2).
- `requirements.md` R5.1 (L13): replace "every 30 seconds" with "within 30 seconds plus one 5-second tick" (the flush loop runs every `flush_tick`), or shorten `flush_interval` to 25 s so the sum is 30.

Assumptions to check at design review: (1) `unassigned` is not a legal service id (carried to `services`); (2) a relative `CHIMERICMESH_DB` resolves against the current directory, while the defaults never do; (3) in a store-backed hub all proposals stay in memory; (4) incident announcement is at-most-once across a crash between commit and announce.

## Review round 3 resolutions

Source: `.agents/tasks/store-design/review.md` (round 3, 0 HIGH, 7 MEDIUM, 14 LOW). No further review rounds were agreed; behavioural findings became test-first tasks in `tasks.md` (sub-tasks with a letter suffix, so existing task numbers did not change and no renumbering map is needed).

| Id | Decision | Why |
|---|---|---|
| M1 | `migrations` is created only by the `apply_migrations` bootstrap; migration `store:0001-core-tables` no longer contains it. | Two definitions made migration 1 fail on every new database. Fix is text only. Task 2.3. |
| M2 | Byte guard stays as the first, no-write check. A second guard runs `PRAGMA user_version` after open and before any write. | A crash leaves `user_version` in the `-wal`, so the byte read can be stale. Tasks 2.2, 2.2a. Open item O1. |
| M3 | Rehydrate is step 0 of `ingest`, one shared task per incident id, no fresh cluster on a failed read. | Concurrent events must not install twice or lose counts, and a partial cluster must never overwrite the stored row. Tasks 11.2a. |
| M4 | `Store.open` does its work on the opening thread. Every later call, `read_sync`, `transact_sync` and `max_id_sync` included, runs on the writer thread and blocks the caller. | One thread uses the connection after open (T1); this is the rule the rest of the design and test 3.2 already assume. Text only. |
| M5 | `ProposalWriter` protocol (`create_proposal`, `decide`, `hub_transition`, `complete`, with `extra`), implemented by `Hub`. `auto-approval` gets all four; `agent-loop` gets `create_proposal` and `complete`. | Higher modules need a legal write path with the B7 fields, and `HubAuthority()` stays in `hub.py`. Tasks 9.4a, 11.3a. |
| M6 | `IncidentTracker.add` sets `opened`; the hub detects a first open as `inc.opened and inc.id not in _announced`. | The hub only sees the state after `add`, and `_announced` is already the durable first-open marker. Tasks 6.2, 11.2b. |
| M7 | The announcement (`say`) is the last step inside `_commit_and_mirror`, in a `try/except` that logs. | A committed decision must be announced even when the caller is cancelled (a V1 safeguard in the map). Task 11.3b. |
| L1 | Facade text says four calls (one try, three retries). | Matches T8 and R7.4. Task 3.2 adds the exactly-three-failures test. |
| L2 | `crash_margin = 2 * queue_limit + batch_rows`; the claim is stated for typical rates. | Drops during a commit exceed `batch_rows` at storm rates. Tasks 7.3, 11.1. |
| L3 | `_stop_writer` sets the flag, enqueues a sentinel and joins for up to 1 s. | The cleanup test can then rely on `gc.collect()`. Task 3.2. |
| L4 | If the writer is stuck at the deadline, `close` only logs and the writer closes the connection when it unblocks. | A connection must not be closed from another thread mid-statement. Task 3.2. |
| L5 | `barrier()` forces an immediate flush. | The `events_after` stitch path should not wait 250 ms. Task 7.3. |
| L6 | The runner checks `conn.in_transaction` after a callable migration and raises `MigrationError`. | Detects a callable that committed. Task 2.3. |
| L7 | New `Event` fields are appended after `ts` in the order `id`, `service_id`, `log_source_id`. | Keeps positional construction working. Task 6.1. |
| L8 | The hub reads `diagnostics.MAX_CLUSTERS` at call time; tests patch that attribute. | A `from ... import` copy would ignore the patch. Task 11.2. |
| L9 | The `HubAuthority(` scan covers `src/` only and exempts the class definition. | Tests construct it on purpose. Tasks 10.2, 11.3. |
| L10 | Property 10 sentence rewritten. | The old sentence could not be asserted. Text only. |
| L11 | Coverage row R9.1 points to task 11.1. | The transcript test lives there. Table in `tasks.md`. |
| L12 | Added to "Carried to other modules": R3.1 "under a lock" becomes "in an await-free block". | The design uses no lock. Text only. |
| L13 | Added to "Carried to other modules": R5.1 bound is 30 s plus one tick. | The requirement is not changed here. Text only. |
| L14 | Single-process use is the documented assumption. `Store.close(clean=False)` is used when startup fails after `open`, so that start never sets `clean_shutdown = 1`. No lock file in V1. | Fixes the reported case (second start fails to bind) without new platform code. Two processes serving one file stay unsupported. Tasks 3.2, 10.1, 11.4. |

**Open items for the user**

- **O1 (from M2), resolved by the user: accepted as a known limitation.** If the hub refuses a database from a newer version after a crash, SQLite may merge the `-wal` file into the main file when the refusing connection closes. The data is kept; only the files change. R2.3 ("not modified") is not changed. No workaround reads a stale `user_version` (no `immutable=1`, no read-only URI). The limitation is also a row in Error Handling and a README line (task 10.1).
- **O2 (from M5), resolved by the user: accepted.** `auto-approval` gets `hub_transition`, restricted as follows: it accepts only the hub-decision transitions of B10 and R4.7 (`approved` to `denied` with `rule-revoked:`, `rule-expired:` or `rule-suspended:`; `pending` to `denied` with `withdrawn:` or `expired:`; `pending` to `approved` only with a non-null `approved_by_rule`), always records `decision_origin = 'hub'`, and rejects any other (from, to, decided_by) with `InvalidTransition`. No MCP tool, REST route, IRC command or terminal command calls it; a static test lists the call sites (task 11.3a).
- **O3 (new, for the user).** B10 names only the `approved` to `denied` case, but `auto-approval` also needs the `pending` to `approved` by rule (R4.7 `approved_by_rule`). I included it in the O2 restriction because the table in R4.3 already allows `pending` to `approved`. Recommended: accept. If you want `hub_transition` limited to B10 alone, rule approvals must go through another seam.