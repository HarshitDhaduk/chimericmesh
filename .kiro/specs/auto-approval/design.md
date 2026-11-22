# Design: auto-approval (V1)

Requirements: [requirements.md](requirements.md). Capability map: [docs/v1-capability-map.md](../../../docs/v1-capability-map.md) (module `auto-approval`, decisions D3, D4, D5, boundary refinements 2 and 5, B3, B7, B10, B14, B17, B18, B20, B22). Baseline: [legacy-mesh](../legacy-mesh/design.md). Fixed inputs: the approved designs of [store](../store/design.md), [services](../services/design.md) and [connectors](../connectors/design.md). They are used as written; additions they would need are listed under "Needs from store / services" and are flagged for the user.

## Overview

`auto-approval` lets the user say "fixes like this, in this service, may go ahead without asking me", and lets the hub (not the agent) enforce it. It adds three things: **standing rules** (a table, a pure matching function, hub-side limits), **structured proposals** (files, estimated lines, verification, completion report, one active proposal per incident, per-service rate limit, withdraw and expiry), and **the audit and announcement trail** that goes with every automatic decision. It also owns the token-free emergency stop: pause, revoke and withdraw.

Six decisions shape everything else:

1. **Evaluation is a synchronous, await-free step inside `Hub.create_proposal`, over an in-memory mirror.** The rules (at most 100 rows) and a usage ledger live in memory on the loop thread (`RuleBook`, `UsageLedger`). `ApprovalManager.prepare(ctx)` validates the request, checks limits, evaluates the rules in id order, and *reserves* the quota slot, all without an `await`. The loop cannot interleave another coroutine inside it (store T2), so two proposals created at the same time cannot both take the last slot (R4.4). Persistence follows: in the store-backed hub the proposal row, its `approved` transition, the usage row, `use_count` and the audit rows commit in **one store transaction**; in the bare hub the proposal lives in memory and the manager writes its own rows to the private `:memory:` store. Alternatives rejected: (a) evaluate in SQL inside the create transaction: it would run on the writer thread, cannot work for the bare hub (proposals are in memory), and would duplicate the pure matching function in SQL; (b) a `Hub._lock`: the store design forbids it (T2) and an await-free block is already atomic.
2. **The hub gets one seam, not rule logic.** `Hub.approver: ProposalApprover | None` (a protocol defined in `approvals/ports.py`). With `approver=None` (a bare `Hub()` that no `create_app` wired) `create_proposal`, `decide` and `complete` behave as today, so every existing test is unaffected. `approvals` imports no `hub`; `hub.py` imports `approvals` (group `wiring`).
3. **Reduce operations mirror first, widen operations database first.** Revoke, pause and per-rule pause change the mirror before any `await`, so the next evaluation already refuses (R6.1, no cache delay), then persist, then deny the rule's approved proposals. If the store is down the mirror change stays, the entry goes into an explicit `dirty` set, and the sweep persists it (fail safe: never "stuck more permissive"). Create, patch, enable and resume use the store as arbiter and update the mirror only after the commit, as a shielded unit (store T6). The two directions are ordered by the rules in "Rule state ordering" (version per rule, conditional widen UPDATEs, tighten-only reload merge), so a widen or a reload can never undo a reduce (review H2).
4. **One matching function, thirteen named conditions.** `match_rule(rule, ctx, usage) -> str | None` returns the code of the first failing condition or `None`. The codes are the sub-criteria 4.2.1 to 4.2.13 (L8). Everything else (ledger, limits, scope, validators, line formatting) is pure and total, so property tests do not need a hub.
5. **The hub declares nothing it cannot check.** It enforces what the proposal *declares* (files, lines, verification) and what the agent *reports* at completion (R6.5), and suspends the rule on a violation. It does not claim to stop writes (R5.4); Kiro's permissions do that (`agent-loop`).
6. **Cooldown from the observed reopen time.** B22 needs the time of the reopen. `Incident` has no `reopened_at` and the store design is fixed, so the manager records, once per (incident, opening), the first moment it *sees* that opening (in `prepare` and in the 60-second sweep) in the table `incident_openings`. The recorded time is never moved later, and it can be at most one sweep tick (60 s) later than the true reopen. There is exactly one cooldown formula (see "Usage ledger"): `reopen_at(ctx.opening) - completed_at < cooldown`. Open item O2; the exact alternative is one line in the store (Needs from store 3).

Technology stack (locked on approval): Python 3.11+, stdlib `sqlite3` through the `store` module only, FastAPI and pydantic in `approvals/routes.py` only, pytest and Hypothesis. No new dependency.

## Architecture

### Where `auto-approval` sits

```
core (events, diagnostics, parsers, __init__)
  ^
store (store/*, fsperms.py)
  ^
services (services/*)
  ^
connectors, discovery, auto-approval (approvals/*)      <- this spec
  ^
agent-loop (agent/*) -> dashboard (REST and SSE only)
  ^
wiring (hub.py, api.py, commands.py, listeners.py, mcp_server.py, __main__.py)
```

`approvals/*` may import `core`, `store` and `services` (map: dependency table). It imports no `hub`, `api`, `commands`, `listeners`, `mcp_server`, `connectors`, `discovery` or `agent`. It receives `announce` and `now` as injected callables (map: "a module that must tell the hub something gets an injected callable").

### File-to-group rows added by this module

| File | Group | Notes |
|---|---|---|
| `approvals/__init__.py` | `auto-approval` | Pure re-exports (names in "Public interfaces"). Imports no `fastapi`, `starlette` or `pydantic`. |
| `approvals/errors.py`, `models.py`, `ports.py`, `scope.py`, `validate.py`, `matching.py`, `ledger.py`, `limits.py`, `format.py`, `schema.py`, `rulebook.py`, `manager.py`, `irc.py` | `auto-approval` | stdlib, `core`, `store`, `services` only. |
| `approvals/routes.py` | `auto-approval` | The only file that may import `fastapi`, `starlette` or `pydantic` (same rule as `services/routes.py`). The per-file third-party allow-list of the dependency test (services task 1.3) gets this row. |

`hub.py`, `api.py`, `commands.py`, `mcp_server.py` stay `wiring`. `diagnostics.py` stays `core` and gains two public names (L4).

### Package layout

```
src/chimericmesh/approvals/
  __init__.py   re-exports
  errors.py     ApprovalsError and subclasses (see Error Handling)
  models.py     Rule, RuleFields, RulePatch, RuleUsage, MatchContext, RuleCheck, ApprovalPlan, RuleDefaults,
                CreateContext, Condition codes, CATEGORIES, constants
  ports.py      ProposalApprover (the hub seam), AnnounceFn, ClockFn
  scope.py      PROTECTED_PATTERNS, SOURCE_SUFFIXES, FileProblem, check_file, check_files, glob_to_regex
  validate.py   validate_rule_fields, validate_rule_patch, validate_new_proposal, validate_completion, clean_note
  matching.py   match_rule, first_match, RULE_CONDITIONS
  ledger.py     UsageLedger (hour window, per incident opening, completions, observed reopens)
  limits.py     MAX_PROPOSALS_PER_SERVICE_HOUR, ProposalLimits (active-proposal and hourly reservation)
  format.py     safe_text, auto_approved_line, cooldown_note, rule_line, audit_detail builders
  schema.py     register_migrations (versions 400 to 499, "approvals")
  rulebook.py   RuleBook (loop-thread mirror), SQL helpers that take a Tx
  manager.py    ApprovalManager (the one facade)
  irc.py        pure renderers for !rules and the replies of !revoke, !pause, !withdraw
  routes.py     build_router
```

### Injected interfaces (resolves M2)

`ApprovalManager.load(...)` receives, at construction, exactly these objects. None of them is `Hub`, `api` or `commands`.

| Parameter | Type | Source | Used for |
|---|---|---|---|
| `store` | `Store` | `app.state.store` (file-backed, or the private `:memory:` store for a bare hub) | Own tables, counters, `AuditLog`. |
| `state` | `StateView` (store ports) | the hub, in both modes | `get_incident`, `list_proposals`, `now`. Reads are synchronous. |
| `writer` | `ProposalWriter` (store ports) | the hub, in both modes | `hub_transition` (rule denial, withdraw, expiry). Never `decide`. |
| `registry` | `ServiceRegistry` (services) | `app.state.services` | `get(service_id)` for the live code folder, `subscribe`. |
| `limiter` | `ReduceAuditLimiter` (services) | `app.state.reduce_limiter` | Audit rows and announcement decision for pause, revoke, withdraw (R2.3). |
| `announce` | `Callable[[str], Awaitable[None]]` | `lambda t: hub.say(hub.irc_channel, t)` | One line per event. |
| `protected` | `Sequence[str]` | the same list `create_app` gives `services` (home, db parent, token parent) | Protected data directory in `scope.py`. |
| `settings` | `ApprovalSettings` | `from_env(environ)` | `autofix_cooldown` (86400), `proposal_ttl` (24 h), `rule_approval_ttl` (1800 s, constant, not env-settable), `sweep_interval` (60 s). Invalid environment values warn once and use the default. |

The same code runs against a plain `Hub()` (with the private store) and against the store-backed hub, because it sees only `StateView`, `ProposalWriter` and `Store`. Task 10.1 runs one parametrized test module over both modes (the criterion M2 asks for). `limits_enabled` is not a parameter: the hub puts `enforce_limits` into each `CreateContext`.

The data seam by mode:

| | Bare `Hub()` + `create_app(hub)` | `create_app()` / `python -m chimericmesh` |
|---|---|---|
| Proposals | hub memory, cap 200 | hub mirror of the file database |
| Rule tables (`approval_rules`, `rule_uses`, `approval_state`) | private `:memory:` store | the file store, same database as proposals |
| `approver.shares_store(hub.store)` | false | true |
| Auto-approval commit | proposal in memory, then `await approver.persist(plan, prop)` (own transaction) | one transaction: proposal, transition, `plan.write_in_tx(tx, prop)` |
| Audit rows | private store `AuditLog` | the store's `AuditLog` |

## Components and Interfaces

### Models (`approvals/models.py`)

```python
CATEGORIES = ("memory", "disk", "network", "security", "crash", "unknown")   # a test equals this set to what diagnostics can emit
MATCH_TYPES = ("signature", "category")
RULE_STATUSES = ("active", "suspended", "revoked", "expired")
VERIFICATION_RESULTS = ("passed", "failed", "not_run")
MAX_RULES_PER_SERVICE, MAX_RULES_TOTAL = 20, 100

@dataclass(frozen=True, slots=True)
class Rule:
    id: str; seq: int; service_id: str
    match_type: str; match_value: str
    most_urgent_severity: int            # 3 or 4
    expires_at: float
    max_per_hour: int; max_per_incident: int; max_files: int; max_lines: int
    status: str; status_reason: str      # "" when active
    created_by: str; created_at: float; updated_at: float
    source_proposal_id: str | None; note: str
    use_count: int; code_folder: str
    def to_dict(self, *, now: float, uses_last_hour: int) -> dict: ...   # adds uses_last_hour, remaining_this_hour

@dataclass(frozen=True, slots=True)
class RuleFields:                         # create input, already validated
    service_id: str; match_type: str; match_value: str
    most_urgent_severity: int = 3; expires_at: float | None = None      # None: now + 7 days
    max_per_hour: int = 3; max_per_incident: int = 1; max_files: int = 5; max_lines: int = 100
    note: str = ""
@dataclass(frozen=True, slots=True)
class RulePatch:                          # None = unchanged
    most_urgent_severity: int | None = None; expires_at: float | None = None
    max_per_hour: int | None = None; max_per_incident: int | None = None
    max_files: int | None = None; max_lines: int | None = None; note: str | None = None
```

`match_type`, `match_value`, `service_id`, `code_folder` and `created_by` are not patchable: changing what a rule covers is a new rule (a new, visible decision), which keeps "widen" down to six numeric fields and the expiry. `expires_at` may never exceed `created_at + 30 days` (R1.4).

```python
@dataclass(frozen=True, slots=True)
class CreateContext:                      # built by Hub.create_proposal on the loop thread
    incident_id: str; service_id: str | None; signature: str; category: str; severity: int; opening: int
    action: str; details: str; extra: Mapping[str, object]       # kind, files, estimated_lines, verification
    enforce_limits: bool

@dataclass(frozen=True, slots=True)
class RuleCheck:                          # one row of the audit trail for a proposal that was checked
    rule_id: str; failed: str | None      # a condition code, or None for the rule that matched

@dataclass(frozen=True, slots=True)
class ApprovalPlan:                       # immutable; crosses to the writer thread
    kind: Literal["pending", "auto", "cooldown"]
    extra: Mapping[str, object]           # normalized kind, files, estimated_lines, verification (only keys the caller sent, plus kind)
    rule_id: str | None                   # set when kind == "auto"
    checks: tuple[RuleCheck, ...]
    incident_id: str; service_id: str | None; opening: int
    files: tuple[str, ...]; remaining_hour: int | None; remaining_incident: int | None
    prior_proposal_id: str | None         # cooldown: the earlier auto-fixed proposal
    reserved_at: float
    opening_first_seen: float | None      # set when prepare was the first to see this opening of an incident that has a completed use (H1)
    token: int                            # reservation id inside the ledger and the limits
    def write_in_tx(self, tx: Tx, prop: Proposal) -> None   # store-backed commit, on the writer thread, tx only
```

`write_in_tx` runs on the `store-writer` thread (store T7 rules apply): it uses only its `tx` and this immutable plan, and it may be re-run by the store's retry. It inserts the `rule_uses` row (auto), runs `UPDATE approval_rules SET use_count = use_count + 1` (auto), inserts the `incident_openings` row with `INSERT OR IGNORE` when `opening_first_seen` is set, and appends the audit rows with `AuditLog.append_tx`: one `proposal_auto_approved` row (auto), or one `auto_approval_skipped` row per checked rule (pending and cooldown), or one `autofix_cooldown` row. **L10 (paused):** when the hub is paused, `prepare` skips rule evaluation, and writes one `auto_approval_skipped` row for the proposal (no `rule_id`, `condition=paused`) only if the service has at least one `active` rule; it never writes a row per rule.

### The hub seam (`approvals/ports.py`)

```python
class ProposalApprover(Protocol):
    def prepare(self, ctx: CreateContext) -> ApprovalPlan: ...
        # Synchronous, await-free, loop thread. Validates, enforces limits, evaluates rules, reserves quota.
        # Raises ProposalInvalid, ActiveProposalExists, ProposalRateLimited. Reserves nothing when it raises.
    def shares_store(self, store: Store | None) -> bool: ...
    async def persist(self, plan: ApprovalPlan, prop: Proposal) -> None: ...     # bare hub: own transaction, never raises
    def confirm(self, plan: ApprovalPlan, prop: Proposal) -> str | None: ...     # sync, after commit and mirror update.
        # Binds the reservation, then re-reads the rule in the mirror. Returns None normally. For kind == "auto" it returns a
        # decided_by text ("rule-revoked:<id>", "rule-suspended:<id>" or "rule-expired:<id>") when the rule is no longer
        # active, i.e. a reduce operation ran while the create transaction was in flight (review M1); the hub then calls
        # hub_transition(pid, "denied", decided_by=<text>) inside the same unit.
    def abort(self, plan: ApprovalPlan) -> None: ...                             # sync, releases the reservation
    async def before_complete(self, pid: str) -> None: ...                       # expiry on read (R6.4)
    def validate_completion(self, pid: str, extra: Mapping[str, object]) -> Mapping[str, object]: ...   # sync, raises ProposalInvalid
    async def after_complete(self, prop: Proposal, extra: Mapping[str, object]) -> None: ...   # R6.5, R6.6, completed_at; never raises
    def line_for(self, plan: ApprovalPlan, base_line: str, prop: Proposal) -> str: ...  # the announcement text (R7.2, R4.9)
```

How `Hub` uses it (wiring change, described under "Hub changes"): `create_proposal` builds the `CreateContext`, calls `prepare`, commits as one shielded unit (store T6), calls `confirm` (and, when it returns a denial text, `hub_transition(..., "denied", ...)`), and announces `line_for(...)`. `line_for` looks at the proposal's final status: for a proposal denied by the `confirm` re-check it returns `rule_line("denied", ...)` (`PROPOSAL p3 denied: rule r1 was revoked`), never the `AUTO-APPROVED` line. `abort` is called when the commit fails. `decide` is unchanged. `complete` calls `before_complete`, `validate_completion`, the existing commit, then `after_complete`.

### Scope (`approvals/scope.py`, pure, total)

```python
PROTECTED_PATTERNS = ("**/.git/**", "**/.kiro/**", "**/.github/**", "**/.env", "**/.env.*", "**/*.pem", "**/*.key",
                      "**/id_rsa*", "**/*.pfx")
SOURCE_SUFFIXES = CODE_SUFFIXES | frozenset({".php", ".pl", ".cs", ".cpp", ".cc", ".jsx", ".tsx"})   # CODE_SUFFIXES from core (L4)
MAX_PATH = 300

@dataclass(frozen=True, slots=True)
class FileProblem:
    file: str                  # the declared text, cut to 80 characters and cleaned
    code: Literal["too_long", "control_char", "outside_folder", "protected", "extension"]

def check_file(code_folder: str, file: str, *, protected_dirs: Sequence[str] = (),
               realpath=os.path.realpath, normcase=os.path.normcase) -> FileProblem | None
def check_files(code_folder: str, files: Sequence[str], *, protected_dirs=(), ...) -> list[FileProblem]
def glob_to_regex(pattern: str) -> re.Pattern[str]      # `**` = any number of path components, `*` = within one component
```

`check_file` applies, in this order: length over 300 gives `too_long`; NUL or any `[\x00-\x1f\x7f-\x9f]` gives `control_char`; `services.paths.path_in_code_folder(code_folder, file)` false gives `outside_folder` (this call resolves `realpath`, rejects `..`-style escapes, and rejects the Windows name tricks, services R2.3); then the path relative to the resolved folder (separators normalized to `/`, `normcase` applied) is matched against `PROTECTED_PATTERNS`, and a resolved absolute path that lies inside any `protected_dirs` entry gives `protected`; then the suffix (lowercased) must be in `SOURCE_SUFFIXES`, else `extension`. Config files (`.ini`, `.conf`, `.yaml`, `.json`, `.toml`) fail the suffix rule (D4). A relative declared path is joined to the folder first. `check_file` never raises.

**L6 (nested protected directories).** The three directory patterns start with `**/`, so `sub/.git/hooks/x.py` and `vendor/lib/.kiro/x.py` are protected as well as the root ones. R5.1 lists the root forms literally; the `**/` forms are a superset (`**/` matches zero or more components). **L7 (filesystem calls in `prepare`).** `check_files` calls `realpath` on up to 20 declared files. This is accepted as cheap on a local disk, and `prepare` calls it only when the service has at least one `active` rule (no rule, no scope check; a proposal for a service without rules never touches the filesystem).

**L4 (public lists in `core`).** `diagnostics.py` renames `_CODE_SUFFIXES` to `CODE_SUFFIXES` and `_SKIP_DIRS` to `SKIP_DIRS`, both `frozenset`, and keeps the old private names as aliases. `find_code_hints` is untouched. A golden test pins its behaviour: a `.php` file is still not found, a `.py` file still is, `node_modules` is still skipped. `agent-loop` R1.4 imports the same two names.

### Validation (`approvals/validate.py`, pure, total)

```python
def validate_rule_fields(raw: Mapping[str, object], *, now: float) -> RuleFields
def validate_rule_patch(raw: Mapping[str, object], *, now: float, created_at: float) -> RulePatch
def validate_new_proposal(raw: Mapping[str, object], *, code_folder: str | None) -> dict[str, object]   # normalized extra
def validate_completion(raw: Mapping[str, object]) -> dict[str, object]                               # normalized extra
def clean_note(value: object) -> str                  # control characters removed, up to 200 characters
```

Every failure raises `ProposalInvalid(field, reason)` or `RuleInvalid(field, reason)` (both carry `field`, REST 422 `{"detail", "field"}`).

| Input | Rule | On failure |
|---|---|---|
| `service_id` (rule) | string matching the services slug; the service exists and has a `code_folder` | 422 `service_id` (`unknown service`, `service has no code folder`) |
| `match_type` | `signature` or `category` | 422 |
| `match_value` | `signature`: 1 to 200 characters after cleaning, no control characters; `category`: one of `CATEGORIES` | 422 |
| `most_urgent_severity` | integer 3 or 4, default 3 (a `bool` is rejected) | 422 (5 to 7 and 0 to 2 included) |
| `expires_at` | finite epoch seconds greater than now and at most `created_at + 30 days`; default now + 7 days | 422 |
| `max_per_hour` | integer 1 to `MAX_PROPOSALS_PER_SERVICE_HOUR`, default 3 | 422 |
| `max_per_incident` | integer 1 to 3, default 1 | 422 |
| `max_files` / `max_lines` | integers 1 to 20 (default 5) / 1 to 500 (default 100) | 422 |
| `note` | up to 200 characters, control characters removed | cut, not an error |
| `created_by`, `status`, `id`, `use_count`, `code_folder` in the request | not accepted: `extra="forbid"` on the pydantic model gives 422 (R2.7) | 422 |
| proposal `files` | list of up to 20 strings, each up to 300 characters; with a service code folder each entry must not be absolute, contain `..` as a component, NUL or a control character; without a code folder only the length and control-character rules apply | 422 `files` |
| `estimated_lines` | integer 0 to 1000 (not `bool`) | 422 |
| `verification` | string up to 300 characters, control characters kept out (cleaned) | 422 over length |
| `kind` | `code_change` only | 422 |
| `files_changed` (completion) | list of up to 50 strings, each up to 300 characters | 422 |
| `lines_changed` | integer 0 to 1 000 000 | 422 |
| `verification_result` | `passed`, `failed` or `not_run` | 422 |
| `status`, `decided_by`, `approved_by_rule` on `POST /api/proposals` | ignored (never read) | none |

Absolute and `..` rejection at creation is for `files` only; the scope check of a rule match (`scope.check_files`) is the stricter second layer.

### Matching (`approvals/matching.py`, pure)

```python
class Condition(str, Enum):        # value = code, in evaluation order; the number is the sub-criterion
    PAUSED = "paused"              # 4.2.1  (a)
    INACTIVE = "inactive"          # 4.2.2  (b) status active and now < expires_at
    KIND = "kind"                  # 4.2.3  (c)
    SERVICE = "service"            # 4.2.4  (d) incident has a service and it equals the rule's
    MATCH = "match"                # 4.2.5  (e) signature equality, or category equality
    SEVERITY = "severity"          # 4.2.6  (f) severity >= max(3, most_urgent_severity)
    HOUR_QUOTA = "hour_quota"      # 4.2.7  (g) auto-approvals in the last 60 minutes < max_per_hour
    INCIDENT_QUOTA = "incident_quota"  # 4.2.8 (h) auto-approvals for this incident in this opening < max_per_incident
    FILES = "files"                # 4.2.9  (i) 1..max_files declared, every file passes scope
    LINES = "lines"                # 4.2.10 (j) estimated_lines declared and <= max_lines
    VERIFICATION = "verification"  # 4.2.11 (k) non-empty verification
    CODE_FOLDER = "code_folder"    # 4.2.12 (l) service's current resolved folder == rule.code_folder
    COOLDOWN = "cooldown"          # 4.2.13 (m) the 4.9 cooldown does not apply

@dataclass(frozen=True, slots=True)
class RuleUsage:    hour: int; incident_opening: int
@dataclass(frozen=True, slots=True)
class MatchContext:
    now: float; paused: bool; kind: str | None; service_id: str | None
    signature: str; category: str; severity: int
    files: tuple[str, ...] | None; estimated_lines: int | None; verification: str | None
    file_problem: str | None            # first FileProblem code for `files`, computed once per proposal by the manager
    live_code_folder: str | None        # registry.get(service_id).code_folder now
    cooldown_active: bool

def match_rule(rule: Rule, ctx: MatchContext, usage: RuleUsage) -> str | None
def first_match(rules: Sequence[Rule], ctx: MatchContext, usage_of: Callable[[str], RuleUsage]) -> tuple[Rule | None, tuple[RuleCheck, ...]]
```

`first_match` walks `rules` sorted by `seq` and stops at the first rule that returns `None`; `checks` lists every rule it examined with its first failing code. `match_rule` never raises and reads no clock, store or registry, so every condition is a one-line test. R4.7 (no service, no folder, severity 0 to 2 never match) follows from `SERVICE` (an incident with `service_id is None` fails), `CODE_FOLDER` (a rule can only exist with a folder; a live `None` fails) and `SEVERITY` (`max(3, x)`).

The numbering resolves **L8**: the requirement lists conditions (a) to (m), thirteen of them (the review note says twelve). The tests use 4.2.1 to 4.2.13 and `requirements.md` is not edited.

### Usage ledger and proposal limits (`approvals/ledger.py`, `approvals/limits.py`)

```python
MAX_PROPOSALS_PER_SERVICE_HOUR = 10                       # limits.py; the one constant (R1.4, R9.4)

class UsageLedger:                                        # loop thread only
    def usage(self, rule_id: str, incident_id: str, opening: int, now: float) -> RuleUsage
    def reserve(self, rule_id: str, incident_id: str, opening: int, now: float) -> int          # returns a token
    def release(self, token: int) -> None
    def bind(self, token: int, proposal_id: str) -> None
    def completed(self, proposal_id: str, now: float) -> None
    def note_opening(self, incident_id: str, opening: int, now: float) -> bool                  # first sighting wins (O2); True when new
    def reopen_at(self, incident_id: str, opening: int) -> float | None
    def cooldown_for(self, incident_id: str, opening: int, cooldown: float) -> str | None       # earlier proposal id; the one formula below
    def approved_at(self, proposal_id: str) -> float | None
    def seed(self, rows: Iterable[UsageRow], openings: Iterable[OpeningRow]) -> None; def prune(self, now: float) -> None

class ProposalLimits:                                     # loop thread only
    def reserve(self, incident_id: str, service_key: str, now: float, *, enforce: bool,
                active_of: Callable[[str], str | None]) -> int      # raises ActiveProposalExists, ProposalRateLimited
    def release(self, token: int) -> None
    def commit(self, token: int) -> None
    def seed(self, created_by_service: Mapping[str, Sequence[float]]) -> None   # at load, from StateView proposals (L8)
```

`usage.hour` counts reservations and confirmed uses for the rule in the last 3600 s (a deque of timestamps); `incident_opening` counts them for the pair (incident id, opening). Reservations count at once, so a slow commit cannot let a second proposal take the slot. **The one cooldown formula (R4.9, resolves H1).** `note_opening(incident, opening, now)` records the first time the manager saw that opening and never changes it (first write wins; `prepare` calls it before `cooldown_for`, so the first look is the reopen time when the sweep has not seen it earlier). Then

`cooldown_for(incident, opening, cooldown)` returns an earlier proposal id when *there exists a use U of this incident with `U.opening < opening` and `U.completed_at is not None` such that `reopen_at(incident, opening) - U.completed_at < cooldown`*, and `None` otherwise. When several uses qualify, the id returned is that of the use with the latest `completed_at`.

`reopen_at` is a property of the opening, not of the proposal's creation time, so the answer for a given opening does not change however late the proposal arrives. Consequences (these are the test cases of tasks 2.2, 2.4 and 5.1): completion at t0, reopen at t0 + 1 h, proposal at t0 + 30 h: cooldown (pending); reopen at t0 + 25 h, proposal right after: not cooldown (auto, Success Criterion 8b); reopen at exactly t0 + cooldown: not cooldown; reopen 1 h after and a *third* opening 100 h later: not cooldown for the third (its `reopen_at` is far from `completed_at`); no completed use, a user-approved proposal, or `opening == 1`: not cooldown. A completion reported after the reopen gives a negative difference, which is below the cooldown, so the proposal waits for the user (conservative). Persistence: the first-seen row goes into `incident_openings` through `plan.write_in_tx` when `prepare` is the first to see the opening, and through the sweep otherwise; after a restart the table is read back, so a restart cannot make the reopen look later.

`ProposalLimits.reserve` runs only when `enforce` is true (`Hub.enforce_limits`): (1) `active_of(incident_id)` returns the id of a `pending` or `approved` proposal of that incident (read from `StateView.list_proposals`), or an in-flight reservation for the same incident, and raises `ActiveProposalExists(that id)`; (2) the service key is `service_id` or the synthetic `unassigned`; more than `MAX_PROPOSALS_PER_SERVICE_HOUR` timestamps (including reservations) in the last 3600 s raises `ProposalRateLimited`. Both are decided in the same await-free block as rule evaluation, so concurrent creates for one incident give one proposal and one `ActiveProposalExists`.

### Persistence and mirror (`approvals/schema.py`, `approvals/rulebook.py`)

`RuleBook` holds `dict[str, Rule]` (all rules, at most 100), the `paused` flag, `paused_by` and `paused_at`, and the ordering state of "Rule state ordering": a `version` per rule and for the pause flag (mirror-only integers, bumped on every mirror change), a monotone `epoch`, a `dirty: set[str]` of rule ids and a `paused_dirty` flag. Reads are synchronous. `ApprovalManager.load` fills it, the ledger (`rule_uses` and `incident_openings` rows of the last 31 days) and the sweep state with one `store.read_sync(..., allow_blocking=True)`, the same pattern as `ServiceRegistry.load` (store T10 call site, flagged under "Needs from store"). `load` then seeds `ProposalLimits` with the `created` times of the proposals `StateView.list_proposals` returns for the last 3600 s, so a restart does not reset the hourly rate window of a store-backed hub (L8; a bare hub has no proposals after a restart, so there is nothing to seed).

### The facade (`approvals/manager.py`)

```python
class ApprovalManager:
    @classmethod
    def load(cls, store: Store, *, state: StateView, writer: ProposalWriter, registry: ServiceRegistry,
             limiter: ReduceAuditLimiter, announce: AnnounceFn, protected: Sequence[str] = (),
             settings: ApprovalSettings | None = None) -> "ApprovalManager": ...

    # ---- hub seam (ProposalApprover) ----
    # prepare, shares_store, persist, confirm, abort, before_complete, validate_completion, after_complete, line_for

    # ---- reads (loop thread, no I/O) ----
    def rules(self, *, service_id: str | None = None, status: str | None = None) -> list[Rule]       # ordered by seq
    def rule(self, rule_id: str) -> Rule | None
    def status(self) -> ApprovalStatus        # paused, paused_by, paused_at, counts by status, bounds, cooldown seconds
    def approval_of(self, prop: Proposal) -> dict | None     # the `approval` object of R7.5
    def proposal_view(self, prop: Proposal) -> dict          # prop.to_full_dict() plus "approval"
    def rule_defaults(self, proposal_id: str, *, now: float | None = None) -> RuleDefaults    # raises RuleNotApplicable (409) or RuleInvalid (422)

    # ---- widen operations (need the user; the caller has already passed require_admin) ----
    async def create_rule(self, fields: RuleFields, *, actor: str) -> Rule
    async def create_rule_from_proposal(self, proposal_id: str, overrides: Mapping[str, object], *, actor: str) -> Rule
    async def patch_rule(self, rule_id: str, patch: RulePatch, *, actor: str) -> Rule
    async def enable_rule(self, rule_id: str, *, actor: str) -> Rule
    async def resume(self, *, actor: str) -> ApprovalStatus

    # ---- reduce operations (token-free, always executed) ----
    async def revoke_rule(self, rule_id: str, *, actor: str, key: str) -> Rule
    async def pause_rule(self, rule_id: str, *, actor: str, key: str) -> Rule
    async def pause(self, *, actor: str, key: str) -> ApprovalStatus
    async def withdraw(self, proposal_id: str, *, actor: str, key: str) -> Proposal

    # ---- lifecycle ----
    async def expire_due(self) -> None        # R6.4, R6.8, R9.8 on read; cheap when nothing is due
    async def sweep_once(self) -> None        # what the 60 s task runs
    async def start(self) -> None; async def aclose(self) -> None     # the sweep task only
```

`actor` is the acting identity, built by the caller: `rest:<client address>` for REST (the services default), the IRC nick, `dashboard-terminal:<client address>` for the terminal. `key` is the limiter key from `services.limiter.reduce_key(kind, who)`. Reduce operations return the object they changed (idempotent: revoking a revoked rule returns it unchanged and writes no second audit row; pausing a paused hub is a no-op).

**Rule lifecycle (R6).**

| From | To | By | How |
|---|---|---|---|
| (new) | `active` | `create_rule`, `create_rule_from_proposal` (token) | one transaction: counter `rule`, row, audit `rule_created`; mirror after commit |
| `active` | `suspended` | `pause_rule` (token-free); `complete` check (R6.5); code folder or service change (R6.9) | audit `rule_paused` or `rule_suspended` |
| `suspended` | `active` | `enable_rule` (token) | refused 409 when the rule's `code_folder` differs from the service's current one, or the service is gone, or the rule is past `expires_at` (then it becomes `expired`) |
| `active`, `suspended` | `revoked` | `revoke_rule` (token-free) | final |
| `active`, `suspended` | `expired` | `expire_due` / sweep | final, audit `rule_expired`, announced |

**Reduce operations are never refused (R2.3, resolves M6).** `revoke_rule` and `pause_rule` return the rule with HTTP 200 and write nothing (no audit row, no announcement, no limiter call counted as a state change) when it is already in the target state or in a final one: revoke of a `revoked` or `expired` rule (an `expired` rule stays `expired`), pause of a `suspended`, `revoked` or `expired` rule. Only an unknown id is refused (404). `pause()` on a paused hub and `withdraw` of a proposal that is not `pending` behave the same way (200, the current object, nothing written; the IRC reply says `p4 is already done, nothing to withdraw`). The limiter is still called with the key, because the key's budget counts requests, not state changes.

A rule that is `suspended` for any reason (including `pause_rule`) can only be re-enabled with the token (R2.6). `pause()` sets only the global flag and leaves every status unchanged (R2.4); `resume()` needs the token and clears it. The pause flag is persisted in `approval_state` so a restart does not silently resume auto-approval.

**Rule state ordering (review H2).** Widen operations go to the database first and reduce operations go to the mirror first, so the two kinds can interleave. Five rules make the outcome always the stricter one:

1. **Versions.** `RuleBook` bumps a per-rule `version` (and `pause_version` for the flag) on every mirror change, and `epoch` on any change. A widen operation (`enable_rule`, `patch_rule`, `resume`) records the version at its read. Reduce operations do **not** take `_rules_lock` (they must not wait behind a slow store); widen operations take it only to serialize against each other.
2. **Conditional widen UPDATEs.** `enable_rule` runs `UPDATE ... SET status='active' WHERE id=? AND status='suspended'`, `patch_rule` runs `... WHERE id=? AND status IN ('active','suspended')`, and `resume` writes `paused='0'` only when the stored value is `'1'`. Each checks `rowcount`. A zero count (a reduce committed first) is `RuleConflict`.
3. **Mirror update only if unchanged.** After the commit, a widen operation replaces the mirror entry only when the entry's version still equals the version it read. If a reduce changed it meanwhile, the widen returns `RuleConflict("changed while enabling")`, leaves the mirror as the reduce set it, and adds the rule (or `paused_dirty`) to the dirty set, because the widen's commit may have overwritten a state the reduce already persisted. The sweep re-persists the mirror's stricter state.
4. **Reduce persistence is conditional too.** The persist for revoke is `SET status='revoked' WHERE id=? AND status IN ('active','suspended')`, for pause `SET status='suspended' WHERE id=? AND status='active'`, for the global pause an unconditional `paused='1'`. They are idempotent, so a retry from the dirty set is safe.
5. **Tighten-only reload merge.** Every reload (the `after_commit` callback, the sweep) is `RuleBook.merge(snapshot, started_epoch)`: it records `epoch` before its read starts; for a rule that is `dirty` or whose version changed after `started_epoch` the mirror entry is kept; otherwise the merged status is the stricter of mirror and snapshot (rank `active` < `suspended` < `revoked` = `expired`), new rows are added, and the other fields come from the snapshot. The paused flag merges as: kept if `paused_dirty` or `pause_version` changed after `started_epoch`; otherwise `mirror.paused or snapshot.paused`. A reload can therefore never turn a rule from `revoked`, `expired` or `suspended` into `active`, nor `paused` from true to false. The sweep persists the dirty set **before** its reload.

The only ways a rule becomes `active` again are `enable_rule` and `resume`, both token-gated, both applied to the mirror by their own unit.

**Denying approved proposals (R6.2, R6.3, R6.9).** After the rule's status is final in memory and persisted, `_deny_open_proposals(rule_id, decided_by)` lists `state.list_proposals("approved")` whose `approved_by_rule == rule_id` and calls `writer.hub_transition(pid, "denied", decided_by=f"rule-revoked:{id}" | f"rule-suspended:{id}" | f"rule-expired:{id}")`, one commit per proposal, each a store transition whose `approved -> denied` check is inside its own transaction (store design table). `InvalidTransition` (the agent completed it first) is ignored. The sweep re-runs `_deny_open_proposals` for every non-active rule that still has an `approved` proposal, which repairs a crash between the two steps. A proposal whose create transaction is still in flight when the rule is revoked is not listed yet; it is caught by the `confirm` re-check (see "The hub seam"): either `confirm` ran first, so the proposal is already in the hub mirror and this listing finds it, or the rule change ran first and `confirm` sees the non-active rule in the mirror and returns the denial text (review M1). This is the only place where this design is weaker than R6.9's wording "in one transaction": see O1.

**Subscriptions (R6.9).** `ApprovalManager.load` calls `registry.subscribe(on_code_folder_changed=..., on_service_deleted=..., after_commit=..., name="approvals")` itself, once, after the mirror is filled. There is no separate `attach` method (M4); `aclose` does not unsubscribe (the registry lives as long as the manager).

| Callback | Thread | Does |
|---|---|---|
| `on_code_folder_changed(tx, change)` | `store-writer`, inside the registry transaction (store T7) | `UPDATE approval_rules SET status='suspended', status_reason='code_folder_changed' WHERE service_id = ? AND status = 'active'`, and one `rule_suspended` audit row per rule through `AuditLog.append_tx`. Uses only `tx` and `change`. Re-runnable. A failure here rolls back the whole service change (the registry raises `SubscriberFailed("approvals")`, services R10.2d; the name is checked when the services code lands, L1). |
| `on_service_deleted(tx, change)` | same | the same for every rule of the service that is `active` or `suspended` (status stays `suspended`, reason `service_deleted`; the rule rows are never deleted, `use_count` and history stay) |
| `after_commit(change)` | loop thread, 5 s limit (services R10.2) | reloads that service's rules with one awaited read and applies them through the tighten-only `RuleBook.merge` (rule 5 of "Rule state ordering"), calls `_deny_open_proposals` for each newly suspended rule, announces one line per rule |

The condition `CODE_FOLDER` reads the live folder from the registry, so even before `after_commit` has run, a proposal evaluated after the registry applied the change cannot match (the registry applies its in-memory change without an `await` right after the commit). The sweep reloads the whole mirror every minute, which repairs a skipped `after_commit`.

**Sweep (`sweep_once`, R6.4, R6.8, R9.8).** One coroutine, every `sweep_interval` (60 s) from a task started in `start()`. It runs these steps in this order: (1) **persist the dirty set** (rules in `dirty`, `paused_dirty`) with the conditional reduce UPDATEs; (2) marks rules past `expires_at` as `expired` (mirror, store, audit, announce) and denies their approved proposals; (3) denies rule-approved proposals `approved` for more than 1800 s with `rule-expired:<id>`; (4) denies `pending` proposals older than `proposal_ttl` with `expired:ttl` (`hub_transition`); (5) repairs: non-active rules with `approved` proposals, `completed_at` missing for `done` proposals in `rule_uses`; (6) reloads the rules table and merges it with the tighten-only `RuleBook.merge` (only after step 1, so a dirty reduce can never be resurrected); (7) records first-seen openings (for every incident with a completed use whose `opening` is higher than the use's, `note_opening` and an `INSERT OR IGNORE` into `incident_openings`); (8) prunes `rule_uses` and `incident_openings` older than 31 days and the in-memory ledger. Every step is a function of the injected clock; tests call `sweep_once()` with a fake clock and no task runs. A step that raises does not stop the others.

**`expire_due()` contract (review M5).** `expire_due()` is steps 2 to 4 only, called before `GET /api/rules`, `GET /api/proposals*`, `POST .../complete` and `GET .../rule-defaults`. It is **bounded and never raises**: it returns at once when the sweep (or another `expire_due`) holds the lock (`lock.locked()`, never `await lock.acquire()`), otherwise runs the work as a task under `asyncio.wait_for(asyncio.shield(task), 1.0)`; on `TimeoutError` the task keeps running and the caller continues; any `StoreError` or other exception is logged once per minute and swallowed. Read routes therefore stay 200 and as fast as the mirror when the store is slow or down. Correctness does not depend on it:

- `approval_of(prop)` adds `expired: bool` to the `approval` object: true for a rule approval whose proposal is still `approved` and older than `rule_approval_ttl` (1800 s) by the hub clock, or whose rule is `expired`, `revoked` or `suspended`. This is computed from the mirror on every read, with no I/O. `agent-loop`'s `verify` and the dashboard treat `expired: true` as not usable (a public-interface addition, listed in "Public interfaces").
- `validate_completion` (synchronous) refuses a rule-approved proposal older than 1800 s with `ProposalExpired`, which `Hub.complete` turns into the `None` result (REST 409), also when the store is down.

The sweep and `expire_due` run on the loop thread; their store work is awaited transactions. They share one `asyncio.Lock`, taken by the sweep with `async with` and by `expire_due` only through the non-blocking check above. `prepare` and the reduce operations never take it.

### IRC and terminal commands (`approvals/irc.py`, `commands.py`)

`commands.py` (wiring) adds `!rules`, `!revoke <rule-id>`, `!pause`, `!withdraw <proposal-id>` and extends `HELP_LINES` with a third line `rules: !rules !revoke <rid> !pause !withdraw <pid> (any registered nick)`. `test_irc_commands.py::test_help_status_and_unknown` only asserts `!incidents` is in the help, so this adds a line without changing that test. They are handled when `hub.approvals` is set; on a bare hub they answer `unknown command` as today. They ask for no `OperatorVerdict` (services R9.3). `handle_command` gains one keyword, `reduce_key: str | None = None` (Needs from services 1); with `None` the key is `reduce_key("irc", nick)`. `!rules` lists at most 5 active rules (`MAX_LIST`): `r1 api-gw signature "<cut to 40>" expires <yyyy-mm-dd>`. There is no `!resume`, `!enable`, `!rule` or any other command that creates, edits or re-enables a rule (R8.2; a test lists the command names). `parse_command` is untouched (R8.3).

**L7 (reserved deciders).** The reserved list is the store's `is_reserved_decider`: `rule:`, `rule-`, `withdrawn:`, `expired:`, `expired-`, case-insensitive. A user named `ruleman` is not refused (R4.8 text asked for bare `rule` and `expired`; the store design already tightened it and its authorship never depends on `decided_by`). The REST `/decide` returns 422 for a reserved `by`; the IRC `_decide` and the terminal refuse a nick that is reserved (`sorry, that nick is reserved`). The README lists the prefixes. Open item O3 records the wording difference with R4.8.

### REST surface (`approvals/routes.py` and `api.py`)

All handlers are `async def` (store T2). Writes that widen use `Depends(require_admin)` (services). Token-free routes carry no dependency but still run through `HostGuard` and `BodyLimit`. Bodies of the **new rule routes** are pydantic models with `extra="forbid"` (an unknown key, or `created_by`, `status`, `id`, `use_count`, `code_folder`, is 422). The **existing** `ProposalRequest` and `CompleteRequest` keep the baseline pydantic default (unknown keys are ignored), so `status`, `decided_by` and `approved_by_rule` on `POST /api/proposals` are ignored and never read (L4); the two request models only gain optional fields. Error bodies are `{"detail": "<text>"}` plus `"field"` for 422.

| Route | Auth | Success | Failures |
|---|---|---|---|
| `GET /api/rules` (`service_id`, `status` filters) | none | `[rule dict]` ordered by id | 422 bad filter |
| `GET /api/rules/{id}` | none | rule dict | 404 |
| `GET /api/approvals/status` | none | `{paused, paused_by, paused_at, active_rules, suspended_rules, cooldown_seconds, proposal_ttl_hours, bounds: {...}}` (`bounds` = defaults and limits of R1.4, so the dashboard form does not hard-code them) | none |
| `POST /api/rules` | token | 201 rule | 401, 429, 422, 409 (20 per service or 100 in total) |
| `PATCH /api/rules/{id}` | token | 200 rule | 401, 429, 404, 409 (revoked or expired), 422 |
| `POST /api/rules/{id}/revoke` | none | 200 rule (already revoked or expired: 200, unchanged) | 404 |
| `POST /api/rules/{id}/pause` | none | 200 rule (already suspended, revoked or expired: 200, unchanged) | 404 |
| `POST /api/rules/{id}/enable` | token | 200 rule | 401, 429, 404, 409 (not suspended, folder changed, service gone, expired, changed while enabling) |
| `POST /api/rules/pause` | none | 200 `{paused: true}` (never refused) | none |
| `POST /api/rules/resume` | token | 200 `{paused: false}` | 401, 429, 409 (paused again while resuming) |
| `GET /api/proposals/{id}/rule-defaults` | none | 200 `{match_type, match_value, service_id, most_urgent_severity, expires_at, max_per_hour, max_per_incident, max_files, max_lines, note, source_proposal_id}` | 404, 409 (reason), 422 (no service or no folder) |
| `POST /api/proposals/{id}/rule` (body: optional overrides) | token | 201 rule | 401, 429, 404, 409, 422 |
| `POST /api/proposals/{id}/withdraw` | none | 200 proposal view (a proposal that is not `pending`: 200, unchanged) | 404 |
| `GET /api/audit` (`service_id`, `rule_id`, `proposal_id`, `limit` 1 to 500, `before`) | none | `[audit row]` newest first | 422 |
| `POST /api/proposals` (changed) | none | 201 proposal view; status `pending` or `approved` | 404 incident; 409 `ActiveProposalExists` `{detail, proposal_id}`; 429 `ProposalRateLimited`; 422 |
| `GET /api/proposals` (changed) | none | proposal views, newest `limit` (default 200) oldest first; new `service_id` filter (including `unassigned`) before the limit | 422 |
| `GET /api/proposals/{id}` (changed) | none | proposal view | 404 |
| `POST /api/proposals/{id}/decide` (changed) | token (services B4) | 200 | 409, 404, 422 reserved `by` (R4.8) |
| `POST /api/proposals/{id}/complete` (changed) | none | 200 proposal view | 404, 409 (not approved, including expired), 422 |

`proposal view` = `Proposal.to_full_dict()` (store) plus `approval` (R7.5): `null` for a proposal nobody approved, else `{by: "user" | "rule", rule_id, rule_status_now, approved_at, expired}`; `by` and `rule_id` come only from `approved_by_rule` (R4.6). `approved_at` for a rule approval comes from the ledger (the proposal's `decided_at` is overwritten when a rule denial happens) and is `null` when the `rule_uses` row has been pruned (L9); for a user approval it comes from `decided_at`. `expired` (new, review M5) is a boolean computed from the mirror and the clock: true for a rule approval that is older than 1800 s while the proposal is still `approved`, or whose rule is no longer `active`; always false for a user approval. `rule_status_now` is the mirror's status (`expired` is computed from `expires_at` on read).

A rule dict: `{id, service_id, match_type, match_value, most_urgent_severity, expires_at, max_per_hour, max_per_incident, max_files, max_lines, status, status_reason, created_by, created_at, updated_at, source_proposal_id, note, use_count, code_folder, uses_last_hour, remaining_this_hour}`.

An audit row, from the store: `{id, ts, actor, action, subject, detail, service_id, rule_id, proposal_id}`.

**Authorization failures (R2.1, R2.5, resolves M2).** `routes.py` wraps `require_admin`: when it raises **401** for a rule route, the route first appends a `rule_auth_failed` audit row (actor from the request, `rule_id` from the path when present, `detail` = the route name; never the token) and re-raises. On **429** (the address is blocked by services' wrong-token limiter) the route writes **nothing**, exactly as services does for a blocked address. Services counts only wrong-token attempts toward its block, so for one address the rows are limited to the block threshold per block window (10 per minute at the time of writing); a test sends 100 wrong-token requests from one address and asserts at most the threshold's worth of rows. The audit table is append-only and never trimmed (store), so this rate is the bound; it is the same rate as services' own `auth_failed` rows. Services' own row also exists; this one carries the rule id the requirement asks for.

**Existing routes changed in `api.py` (wiring).** `ProposalRequest` gains `files`, `estimated_lines`, `verification`, `kind`; `CompleteRequest` gains `files_changed`, `lines_changed`, `verification_result`; the create handler maps `ProposalInvalid` to 422, `ActiveProposalExists` to 409 and `ProposalRateLimited` to 429; list, get and create return `approvals.proposal_view(prop)`; `/decide` checks `is_reserved_decider(by)` and `DecideRequest.by` stays `max_length=30`. `test_proposals_endpoint_fields` is edited to the new key set (the one allowed test change of this module, R9.7). A bare hub without `app.state.approvals` (no `create_app`) keeps `to_dict()`.

### Public interfaces for `agent-loop` and `dashboard` (stated once)

`agent-loop` may import from `chimericmesh.approvals`: `scope.check_files`, `scope.FileProblem`, `scope.PROTECTED_PATTERNS`, `scope.SOURCE_SUFFIXES`, `errors.ProposalInvalid`, `errors.ActiveProposalExists`, `errors.ProposalRateLimited`, `ApprovalManager.approval_of` and `proposal_view` (through an object passed at construction, never a module global), `models.Rule` (read-only). It never imports `routes`, and it calls no rule-mutating method.

| Need of `agent-loop` | Provided as |
|---|---|
| `verify_proposal` (R3): is this proposal approved, by whom, and are these files inside scope | `manager.approval_of(prop)` for `approval` and `scope.check_files(service.code_folder, files, protected_dirs=...)` for the file list; the route itself (`POST /api/proposals/{id}/verify`) is `agent-loop`'s. `ok` for a rule-approved proposal also requires the rule `active`, the hub not paused and `approval.expired` false: `manager.rule(rule_id)`, `manager.status().paused`, `approval_of(prop)["expired"]`. |
| `wait_for_work` approved proposals | `state.list_proposals("approved")` (store `StateView`). A rule revocation turns them `denied`, so the next poll no longer lists them. |
| `complete_proposal` fields | `files_changed`, `lines_changed`, `verification_result`, passed through `Hub.complete(pid, note, extra=...)`; validated here. |
| `propose_remediation` fields | `files`, `estimated_lines`, `verification`, `kind` through `Hub.create_proposal(..., extra=...)`. The create response carries the final status. |
| Permissions snippet | `scope.PROTECTED_PATTERNS` (deny globs; now `**/.git/**`, `**/.kiro/**`, `**/.github/**` plus the unchanged others, L6) and `scope.SOURCE_SUFFIXES` (allow globs per extension). |

`dashboard` uses only the REST table above and the `approval` object. Fields it needs and where they come from: rules list (`GET /api/rules`), paused flag and form bounds (`GET /api/approvals/status`), defaults for the "Always approve fixes like this" form (`GET /api/proposals/{id}/rule-defaults`), audit rows (`GET /api/audit`), who approved a proposal (`approval`), per-service proposal list (`GET /api/proposals?service_id=`). Reduce actions (pause, revoke, withdraw) need no token; create, patch, enable and resume do.

### Hub changes (wiring; `hub.py`)

`Hub.__init__` gains `enforce_limits: bool = False` (map B3), and sets `self.approver: ProposalApprover | None = None` and `self.approvals: ApprovalManager | None = None` (the second one only so `commands.py` can reach it, as `hub.access` does). `create_app` sets both. Exceptions (`ProposalInvalid`, `ActiveProposalExists`, `ProposalRateLimited`) are imported from `approvals.errors`.

```python
async def create_proposal(self, incident_id, action, details="", *, extra=None):
    inc = self.get_incident(incident_id)
    if inc is None:
        return None
    ctx = CreateContext(...)                       # sync
    plan = self.approver.prepare(ctx) if self.approver else None      # may raise; reserves nothing then
    unit = asyncio.ensure_future(self._create_unit(inc, action, details, extra, plan))
    unit.add_done_callback(_retrieve_exception)
    return await asyncio.shield(unit)
```

`_create_unit` is the store T6 unit, so cancellation of the caller cannot split commit, mirror, ledger and announcement. Store-backed: one `store.transact` closure that calls `store.proposals.create_proposal(tx, ...)`, then, when `plan.kind == "auto"`, `transition_proposal(tx, pid, "approved", decided_by=f"rule:{plan.rule_id}", origin="hub", authority=HubAuthority(), approved_by_rule=plan.rule_id)`, then `plan.write_in_tx(tx, prop)`. After the commit, with no `await` in between: mirror the proposal, `denial = approver.confirm(plan, prop)`; if `denial` is not `None` (a revoke, suspension or expiry of the rule ran during the commit), `await self.hub_transition(prop.id, "denied", decided_by=denial)` (`InvalidTransition` ignored); then `await say(approver.line_for(plan, base_line, prop))`; on any exception before the commit finished, `approver.abort(plan)`. Bare hub: build the `Proposal` in memory (status `approved` with `approved_by_rule`, `decided_by`, `decision_origin="hub"` for `auto`), call `confirm`, then `await approver.persist(plan, prop)`. The `HubAuthority()` construction here and in `hub_transition` is the same file as the store design's static scan expects (`hub.py` only).

`decide` is unchanged. `complete(pid, note, *, extra=None)`: `await approver.before_complete(pid)`; `extra = approver.validate_completion(pid, extra)`; the existing commit; then `await approver.after_complete(prop, extra)` inside the same shielded unit (R6.5, R6.6, `completed_at`). A proposal that the sweep denied returns `None` from the status check, which REST maps to the existing 409.

This is the same edit region as the store tasks 11.3, 11.3a and 11.3b: merge order is store, services, connectors, then this module (Needs from store 1).

### Format and announcements (`approvals/format.py`, pure, total)

```python
def safe_text(value: object, limit: int) -> str       # removes [\x00-\x1f\x7f-\x9f], lone surrogates, collapses spaces, cuts
def auto_approved_line(pid: str, rid: str, sid: str, action: str) -> str
    # "AUTO-APPROVED p3 by rule r1 (service api-gw): <action>. Revoke with !revoke r1"
def cooldown_note(prior_pid: str, rule_id: str) -> str
def rule_line(verb: str, rid: str, sid: str, actor: str, extra: str = "") -> str
```

`auto_approved_line` cuts `action` so the whole line is at most 400 UTF-8 bytes and contains no CR, LF or NUL; it never contains "reply !approve" (B17). The cooldown case keeps the baseline `PROPOSAL ...` line and appends `cooldown_note` (R4.9). Lines for rule events (`RULE r1 created by <actor> for <service>`, `RULE r1 revoked by ...`, `RULE r1 suspended: <reason>`, `RULE r1 expired`, `AUTO-APPROVAL paused by ...`, `AUTO-APPROVAL resumed by ...`, `PROPOSAL p4 withdrawn by ...`) are built by `rule_line`, with `actor` and every log-derived value passed through `safe_text`. They go out through `announce`, which is `hub.say`, so `format_privmsg` strips CR/LF/NUL again at the socket (project rule). A reopen is **not** announced here (store R5.7 owns it; one line per reopen).

Reduce operations: the announcement is made only when `limiter.record(...)` returns `True`; the action is always performed (services R6.5).

## Data Models

### SQLite schema (migration `approvals:400`, name `approvals:0400-rules`)

Registered through `register_migrations(registry)` in `approvals/schema.py`; the module block is 400 to 499 (`MODULE_BLOCKS["approvals"]`). `create_app`'s `build_registry()` calls it. No foreign key into another module's tables (a deleted service must not delete rule history; the same reasoning as the connectors offsets).

```sql
CREATE TABLE approval_rules (
  id                   TEXT PRIMARY KEY,                   -- 'r' || seq
  seq                  INTEGER NOT NULL UNIQUE,
  service_id           TEXT    NOT NULL,
  match_type           TEXT    NOT NULL CHECK (match_type IN ('signature','category')),
  match_value          TEXT    NOT NULL CHECK (length(match_value) BETWEEN 1 AND 200),
  most_urgent_severity INTEGER NOT NULL CHECK (most_urgent_severity IN (3,4)),
  expires_at           REAL    NOT NULL,
  max_per_hour         INTEGER NOT NULL CHECK (max_per_hour BETWEEN 1 AND 10),
  max_per_incident     INTEGER NOT NULL CHECK (max_per_incident BETWEEN 1 AND 3),
  max_files            INTEGER NOT NULL CHECK (max_files BETWEEN 1 AND 20),
  max_lines            INTEGER NOT NULL CHECK (max_lines BETWEEN 1 AND 500),
  status               TEXT    NOT NULL CHECK (status IN ('active','suspended','revoked','expired')),
  status_reason        TEXT    NOT NULL DEFAULT '',
  created_by           TEXT    NOT NULL,
  created_at           REAL    NOT NULL,
  updated_at           REAL    NOT NULL,
  source_proposal_id   TEXT,
  note                 TEXT    NOT NULL DEFAULT '' CHECK (length(note) <= 200),
  use_count            INTEGER NOT NULL DEFAULT 0,
  code_folder          TEXT    NOT NULL
);
CREATE INDEX idx_rules_service ON approval_rules (service_id, status);

CREATE TABLE rule_uses (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  rule_id          TEXT    NOT NULL,
  proposal_id      TEXT    NOT NULL UNIQUE,
  incident_id      TEXT    NOT NULL,
  opening          INTEGER NOT NULL,
  service_id       TEXT,
  approved_at      REAL    NOT NULL,
  completed_at     REAL
);
CREATE INDEX idx_uses_rule     ON rule_uses (rule_id, approved_at);
CREATE INDEX idx_uses_incident ON rule_uses (incident_id, opening);

CREATE TABLE incident_openings (                          -- first time the manager saw this opening (O2, H1); first write wins
  incident_id    TEXT    NOT NULL,
  opening        INTEGER NOT NULL,
  first_seen_at  REAL    NOT NULL,
  PRIMARY KEY (incident_id, opening)
);

CREATE TABLE approval_state (
  key   TEXT PRIMARY KEY,                                 -- 'paused', 'paused_by', 'paused_at'
  value TEXT NOT NULL
);
```

Rule ids: `format_id("r", tx.counter_next("rule", floor=<SELECT COALESCE(MAX(seq),0) FROM approval_rules>))`, inside the create transaction (store R3.7). The `max_per_hour` upper bound in the CHECK is the literal 10 and a test asserts it equals `MAX_PROPOSALS_PER_SERVICE_HOUR`. All SQL is literal (store's SQL rule). `rule_uses` rows and `incident_openings` rows older than 31 days are deleted by the sweep (rules live at most 30 days). `approved_at` of a rule approval comes from `rule_uses`; once a row is pruned it is `null` in `approval_of` (L9: never a guess, and the dashboard shows no time).

### Audit actions written by this module

| `action` | When | `actor` | `subject` | Refs |
|---|---|---|---|---|
| `rule_created` | create (also from a proposal) | acting identity | rule id | service, rule, source proposal |
| `rule_updated` | patch | acting identity | rule id | detail: `field: before -> after` per changed field |
| `rule_enabled` | re-enable | acting identity | rule id | service, rule |
| `rule_revoked`, `rule_paused`, `rules_paused`, `proposal_withdrawn` | reduce operations (written by the limiter) | acting identity | rule or proposal id | |
| `rules_resumed` | resume | acting identity | `all` | |
| `rule_suspended` | R6.5, R6.9 | `hub` | rule id | detail: reason (`completion_out_of_scope`, `verification_failed`, `code_folder_changed`, `service_deleted`, `lines_over_limit`, `files_over_limit`) |
| `rule_expired` | sweep | `hub` | rule id | |
| `proposal_auto_approved` | match | `rule:<id>` | proposal id | service, rule, proposal; detail: `incident=<id> files=<a,b> remaining_hour=<n> remaining_incident=<n>` |
| `auto_approval_skipped` | a rule was checked and failed | `hub` | proposal id | rule; detail: `condition=<code>` (first failing only) |
| `autofix_cooldown` | R4.9 | `hub` | proposal id | rule; detail: `earlier=<pid> rule=<rid> reopened_at=<ts>` |
| `completion_unverified` | R6.6 | `hub` | proposal id | rule |
| `completion_checked` | R6.5 passed | `hub` | proposal id | rule |
| `rule_auth_failed` | 401 or 429 on a rule route | `client:<address>` | route name | rule id when in the path |

`detail` never carries log lines, signatures, the token or a webhook URL (R7.6): `match_value` is not copied into audit rows; only ids, counts, condition codes and declared file names. A canary test sends a signature, a token canary and an action text with a CRLF and asserts none of them appear in any audit row or announced line.

## Sequence diagrams

### Auto-approval at creation (store-backed hub)

```mermaid
sequenceDiagram
    participant A as Agent (REST)
    participant H as Hub (loop thread)
    participant M as ApprovalManager (loop thread)
    participant W as store-writer thread
    participant IRC as IRC / transcript

    A->>H: POST /api/proposals (files, lines, verification)
    H->>M: prepare(ctx)  [no await from here]
    M->>M: validate; limits.reserve; first_match(rules); ledger.reserve
    M-->>H: plan(kind=auto, rule r1, token)
    H->>W: transact(create_proposal, transition approved by rule, plan.write_in_tx)
    W->>W: BEGIN IMMEDIATE; insert p3; approved; rule_uses; use_count+1; audit rows; COMMIT
    W-->>H: committed
    H->>H: mirror p3 (approved); confirm(plan)  [no await between]
    H->>IRC: AUTO-APPROVED p3 by rule r1 (service api-gw): <action>. Revoke with !revoke r1
    H-->>A: 201 {status: approved, decided_by: rule:r1, approval: {by: rule, ...}}
```

### Revoke (token-free)

```mermaid
sequenceDiagram
    participant U as Caller (REST / IRC / terminal)
    participant M as ApprovalManager
    participant L as ReduceAuditLimiter
    participant W as store-writer thread
    participant H as Hub (writer: hub_transition)

    U->>M: revoke_rule(r1, actor, key)
    M->>M: mirror: r1 revoked  [no await before this; next prepare refuses]
    M->>L: record(key, ...) -> announce?
    M->>W: transact(UPDATE rule status revoked)
    W-->>M: committed (a failure leaves it dirty for the sweep)
    loop each approved proposal of r1
        M->>H: hub_transition(pN, denied, decided_by=rule-revoked:r1)
    end
    M-->>U: rule (revoked)
```

### Code folder change suspends rules

```mermaid
sequenceDiagram
    participant R as ServiceRegistry (services)
    participant W as store-writer thread
    participant M as ApprovalManager (loop thread)

    R->>W: transact(update service; run_callbacks)
    W->>W: on_code_folder_changed(tx, change): UPDATE rules suspended; audit rows
    W-->>R: committed (a raise here rolls back the service change)
    R->>R: apply in-memory change (no await): live folder != rule.code_folder, so 4.2.12 already fails
    R->>M: after_commit(change)
    M->>W: read rules of the service; refresh mirror
    M->>M: deny approved proposals (rule-suspended:r1); announce
```

## Threading and transaction model (resolves M3)

- **A1. Thread of the decision.** `prepare`, `confirm`, `abort`, `line_for`, `RuleBook`, `UsageLedger` and `ProposalLimits` run on the **loop thread only**. `prepare` contains no `await`, so evaluate-and-decide is atomic with respect to other coroutines (store T2). The map's "hub lock" is satisfied by that: no `Hub._lock` exists, and `prepare` is the "await-free in-memory `create_proposal`" that the store notes name.
- **A2. Store-backed hub.** The proposal insert, the `approved` transition, the `rule_uses` row, `use_count` and the audit rows are one `store.transact` (T3). The reservation made by `prepare` is what keeps two concurrent creates apart; the transaction makes the record durable atomically. A SQLite error aborts all of it, then `abort(plan)` releases the reservation.
- **A3. Callbacks may not take the hub lock.** There is none. Callbacks registered with `registry.subscribe` follow store T7: `on_*` run on the writer thread inside the registry transaction and use only `tx` and the immutable `ServiceChange`; no `await`, no `Store.transact*`, no hub state, no I/O. `plan.write_in_tx` follows the same rules. `after_commit` runs on the loop thread.
- **A4. Bare hub.** Proposals are in memory, rules in the private store. The decision is still the await-free `prepare`; `persist` is a separate awaited transaction and never raises (a failure logs once and leaves the in-memory result in force: the usage stays counted, so a store failure can only make the system stricter).
- **A5. Shielded units.** `create_rule`, `patch_rule`, `enable_rule`, `resume`, `create_rule_from_proposal` run as store T6 units: `asyncio.ensure_future(commit_then_mirror)` awaited through `asyncio.shield`; the mirror is updated in the same synchronous block as the commit's return; the announcement is the last step in the unit in a `try/except` that logs. A cancelled request still ends with a consistent mirror. Reduce operations update the mirror first (fail safe) and persist inside the same kind of unit, with conditional UPDATEs and the dirty set of "Rule state ordering".
- **A6. Serialization.** Widen operations (create, patch, enable, resume, create from a proposal) are serialized against each other by one `asyncio.Lock` (`_rules_lock`). Reduce operations (revoke, pause, pause of a rule) do **not** take it: they change the mirror before any `await`, and the ordering rules of "Rule state ordering" (versions, conditional UPDATEs, tighten-only merge, dirty set) decide every interleaving with a widen or a reload. Reads and `prepare` never take any lock. `prepare` sees either the old or the new mirror, never a half-updated rule (a rule is replaced as a whole frozen object).
- **A7. Idempotent reductions.** Revoke, pause and withdraw can arrive from REST, IRC and the terminal at once; each re-checks status in the mirror and the transition check of the store decides the proposals, so the second caller sees "already revoked".
- **A8. Time.** Every time read is `state.now()` (the hub clock). No `time.time()` in `approvals/`. A static test greps for it.

## Error Handling

| Operation | Failure | Recoverable? | Caller receives | Logged |
|---|---|---|---|---|
| `prepare`: invalid `files`, `estimated_lines`, `verification`, `kind` | bad request | yes | `ProposalInvalid(field, reason)`; REST 422 `{detail, field}`; the MCP wrapper shows the text; nothing reserved | none |
| `prepare`: incident has an active proposal (`enforce_limits`) | caller error | yes | `ActiveProposalExists(proposal_id)`; REST 409 `{detail, proposal_id}` | none |
| `prepare`: more than 10 proposals for the service in an hour | caller error | yes, later | `ProposalRateLimited`; REST 429 with `Retry-After` of seconds until the oldest slot frees | none |
| `prepare`: a bug in matching (exception) | internal | yes | caught inside `prepare`: the plan is `pending` with one audit row `auto_approval_skipped condition=error`; the proposal is created for the user. Never auto-approves on error | error, once per minute |
| Create transaction fails (`StoreError`) | store down | yes | `abort(plan)` releases the reservation; `StoreError` propagates (REST 503 by the store's handler, IRC text by `handle_command`); no memory change | error (store logs) |
| `persist` fails (bare hub) | private store | yes | none; in-memory result stays; logged | error once per minute |
| Rule create or patch: validation | bad request | yes | `RuleInvalid(field, reason)`; REST 422 | none |
| Rule create: limit reached (20 per service, 100 total) | caller error | yes | `RuleLimitReached`; REST 409 | none |
| Rule create: service missing, no code folder | caller error | yes | 422 `service_id` | none |
| Rule operation: unknown id | caller error | | `RuleNotFound`; REST 404; IRC plain text | none |
| Rule operation: illegal state (enable a revoked rule, patch an expired rule, enable after folder change) | caller error | | `RuleConflict(reason)`; REST 409 | none |
| `create_rule_from_proposal`: proposal approved by a rule, pending, denied, `done` without `passed` | caller error | | `RuleNotApplicable(reason)`; REST 409 with the reason | none |
| `create_rule_from_proposal`: incident has no service or the service no folder | caller error | | `RuleInvalid("service_id", ...)`; REST 422 naming which is missing | none |
| Widen operation finds the entry changed by a reduce (`enable_rule`, `patch_rule`, `resume`) | race | yes | `RuleConflict("changed while ...")`, REST 409; mirror keeps the reduce; entry added to the dirty set | info |
| Reduce operation on a rule already in the target or a final state | caller error | | 200 with the unchanged object, nothing written (M6) | none |
| `expire_due` store failure or timeout (over 1 s) | store down, slow | yes | nothing: swallowed, the read route continues; the sweep retries | warning, once per minute |
| `confirm` finds the rule no longer active | race with revoke | yes | the hub denies the proposal with `rule-revoked:<id>` (or `-suspended`, `-expired`); the creator receives the denied proposal | info |
| `complete` of a rule-approved proposal older than 1800 s | agent late | | `ProposalExpired` inside `Hub.complete`, returns `None`; REST 409 | none |
| Reduce operation: store write fails | store down | yes | the action stands in the mirror (revoked, paused); the call returns 200 with the object and `persisted: false` is logged; the sweep retries; REST does not return 503 for a reduce operation | error, then once per minute |
| `_deny_open_proposals`: `InvalidTransition` for one proposal | race with the agent | ignored | none | debug |
| `_deny_open_proposals`: store failure | store down | yes | left for the sweep | warning |
| `withdraw`: proposal not `pending` | caller error | | `RuleConflict`; REST 409; IRC plain text | none |
| `after_complete`: store failure while recording `completed_at` | store down | yes | none; the sweep repairs `completed_at` | warning |
| `after_complete`: check fails (file outside scope, over limits, `failed`) | agent misbehaved | rule suspended | none to the agent (the completion stays recorded); audit `rule_suspended`; one announcement | warning |
| Subscriber callback raises inside the registry transaction | bug | not retried | the service change rolls back, services R10.2 returns `SubscriberFailed("approvals")` | error |
| `after_commit` callback raises or exceeds 5 s | bug, slow store | yes | logged by services; the sweep reloads the mirror within 60 s | error |
| Sweep step raises | bug | yes | the other steps still run; next tick retries | error, once per minute per step |
| Environment: `CHIMERICMESH_AUTOFIX_COOLDOWN_SECONDS` outside 0 to 2 592 000, `CHIMERICMESH_PROPOSAL_TTL_HOURS` outside 1 to 720 | misconfiguration | yes | default (86400, 24); one warning | warning |
| Startup: `approval_rules` table missing | store opened without these migrations | fatal | `ApprovalsNotMigrated("open the store with approvals.register_migrations")` (same pattern as `ServicesNotMigrated`) | error |

**Auto-approval never happens on doubt.** Any unexpected condition during `prepare` falls back to `pending`. Reduce operations never fail closed to a more permissive state.

**Invariants and who enforces them.**

| Invariant | Layer | Why there |
|---|---|---|
| A proposal is auto-approved only by `prepare`, in the hub | `ApprovalManager.prepare` called from `Hub.create_proposal` | the agent, the MCP server and clients cannot reach `approved_by_rule` (store `hub_transition` is the only writer; a static test lists its call sites) |
| `approved_by_rule` is set only together with `decided_by = rule:<id>` and `decision_origin = hub` | store `transition_proposal` | the store owns the transition table |
| Quota never exceeded | ledger reservation in the await-free block | nothing else can interleave |
| A rule never covers sev 0 to 2, a service without a folder, or a non-`code_change` | `match_rule` (conditions 4.2.3, 4.2.4, 4.2.6, 4.2.12) and the CHECK constraints | defence in depth |
| Rules need the user to widen | `require_admin` on the route plus no MCP route | the token lives in services; the honest limit is in the map |
| Audit for every automatic decision | same transaction as the decision (store-backed) | cannot be lost without the decision |

## Security

- **No MCP path.** `mcp_server.py` calls no `/api/rules`, `/api/approvals`, `/api/audit`, `/rule`, `/rule-defaults` or `/withdraw` URL, and the tool list has no rule tool. A static test parses the endpoint literals in `mcp_server.py` against an allow-list (R2.2). `agent-loop` adds `verify` and its own routes to that allow-list in its own task.
- **Widening needs the token** (create, patch, enable, resume, rule from proposal). A request without it returns 401 and changes nothing. The wrong-token limiter and audit rows are services'.
- **Honest limit, repeated here on purpose.** The agent runs as the same OS user and can read the token file and call these routes from a shell. In hands-off mode (the user allowed a shell command), a prompt-injected agent could create a rule. The README (task 8.3) says so next to the rule documentation and points to Kiro's shell prompt as the real gate. Pause, revoke and withdraw are token-free, so the agent could call them too; that only reduces its own permissions.
- **Untrusted text.** Signatures, actions, details, verification and file names are log- or agent-derived. They go to IRC only through `safe_text` and `hub.say`; to audit only as ids, codes and capped file names; to the dashboard as JSON (the dashboard uses `textContent`). `match_value` is stored as given (up to 200 characters, control characters rejected) and compared by exact equality, never as a pattern, so a hostile signature cannot become a regex or SQL (SQL is parameterized).
- **File scope.** `scope.check_file` resolves symlinks with `realpath` at match time and `path_in_code_folder` rejects the Windows name tricks. Residual race (a link swapped between the check and the write) is the one services documents; Kiro's permissions are the real write gate.
- **Fail safe at the boundaries.** No service, no folder, severity 0 to 2, missing files, missing lines, missing verification, paused, cooldown: all give `pending`. Rule approval lasts 30 minutes (R6.4).
- **`GET /api/audit` is unauthenticated (L11).** It is consistent with services' open GET routes and runs behind `HostGuard` on localhost. Its rows can contain code-folder paths (services' `code_folder=<old> -> <new>`) and declared file names. No token, signature or log text is ever in a row (R7.6). Open item O9 asks the user to accept this.
- **No secrets in output.** The token is never passed into this module (only `require_admin` sees it). `rule_auth_failed` rows carry no header value.
- **Test-command risk in hands-off mode** is `agent-loop`'s README item; this module's README text covers the rule semantics, the reserved decider prefixes (L7) and the honest limit above.

## Correctness Properties

Each property is a universal statement for Hypothesis (`tests/pbt/`, `max_examples` 40 to 100, `deadline=None` where a store is opened). "Validates" names the requirement.

1. **Matching: no match when any condition fails.** *For any* rule, context and usage, `match_rule` returns `None` only when all thirteen conditions hold, and otherwise returns the code of the first failing one in evaluation order. **Validates: Requirements 4.1, 4.2, 4.3**
2. **Matching is monotone in the limits.** *For any* rule, context and usage for which `match_rule` is not `None`, lowering `max_per_hour`, `max_per_incident`, `max_files` or `max_lines`, or raising `most_urgent_severity` from 3 to 4, can only keep a match or remove it; and *for any* non-matching pair, a tighter rule still does not match. **Validates: Requirements 4.2 (Success Criterion 7: monotone)**
3. **First match wins.** *For any* list of rules, `first_match` returns the lowest-`seq` rule for which `match_rule` is `None`, and its `checks` contain exactly the rules examined up to and including it. **Validates: Requirements 4.1**
4. **Never covered.** *For any* context with severity 0 to 2, a `None` service, or a live folder of `None`, no rule matches. **Validates: Requirements 4.7, 1.3**
5. **Rule bounds.** *For any* accepted `RuleFields`, `most_urgent_severity` is 3 or 4, `max_per_hour` is between 1 and `MAX_PROPOSALS_PER_SERVICE_HOUR`, `max_per_incident` 1 to 3, `max_files` 1 to 20, `max_lines` 1 to 500, `expires_at` at most 30 days after creation; and *for any* rejected input the validator raises `RuleInvalid` and nothing else. **Validates: Requirements 1.3, 1.4**
6. **Default severity from the proposal.** *For any* incident severity `s`, the defaulted `most_urgent_severity` is `min(4, max(3, s))`, and the rule never covers a more urgent incident than the one the user saw. **Validates: Requirements 3.2**
7. **Hourly quota under concurrency.** *For any* number of proposals created concurrently (`asyncio.gather`) for one service against a rule with `max_per_hour = n`, at most `n` are `approved` and the rest `pending`, at any interleaving of the store's write delays. **Validates: Requirements 4.4, 4.2 (g)**
8. **Per-opening counting.** *For any* sequence of matching proposals, completions, resolves and reopens, the number of auto-approvals of one rule for one incident within one opening never exceeds `max_per_incident`, and a reopen gives a new allowance. **Validates: Requirements 4.2 (h)**
9. **Cooldown.** *For any* history of auto-approved completions (with `completed_at`) and first-seen opening times, and *for any* proposal creation time, the proposal is held for the user (`kind` `cooldown`) iff some use U of the incident with `U.opening < opening` and a `completed_at` has `reopen_at(opening) - U.completed_at < autofix_cooldown`; the answer depends on `reopen_at` and `completed_at` only, not on when the proposal arrives (a proposal 30 h after a reopen that came 1 h after the completion is still held; a reopen at exactly the cooldown, or later, is not held). Recording an opening twice never moves its first-seen time. **Validates: Requirements 4.9**
10. **Revocation is immediate and total.** *For any* rule and any later proposal, once `revoke_rule` has returned, no proposal matches it; every proposal it approved and that was still `approved` is `denied` with `decided_by` `rule-revoked:<id>`, **including a proposal whose create transaction was still in flight when the revoke started** (the `confirm` re-check), and no `done` or user-approved proposal changes. **Validates: Requirements 6.1, 6.2, 6.3**
11. **Denial transitions only for rule approvals.** *For any* sequence of operations, an `approved` to `denied` transition happens only for a proposal with `approved_by_rule` set and a `decided_by` of the three allowed prefixes. **Validates: Requirements 6.3**
12. **Reserved prefixes never accepted from callers.** *For any* `by` text, case and spacing varied, that starts (after the store's normalization) with `rule:`, `rule-`, `withdrawn:`, `expired:` or `expired-`, `POST /decide` answers 422 and `approved_by_rule` stays null; any other text is accepted up to 30 characters. **Validates: Requirements 4.8**
13. **Pause, revoke, withdraw are never refused.** *For any* number of calls per key per minute, every call takes effect, at most 10 individual audit rows and announcements exist, the excess is one coalesced row, and no call needs a token. *For any* rule status (including `revoked` and `expired`), `revoke_rule` and `pause_rule` return the rule without raising and without changing a final status; only an unknown id is refused. **Validates: Requirements 2.3**
14. **Pause blocks all auto-approval.** *For any* proposals created while paused, none is auto-approved and no rule status changed. **Validates: Requirements 2.4**
15. **File scope containment.** *For any* declared path, `check_file` returns `None` only if the path (resolved with the injected `realpath` and `normcase`) is a strict descendant of the code folder, is under none of the protected patterns or directories, has a source suffix, is at most 300 characters and has no control character; it never raises. **Validates: Requirements 5.1, 5.2, 5.3**
16. **Completion check.** *For any* completion report on a rule-approved proposal, the rule is suspended iff a changed file fails scope, more files than `max_files` or more lines than `max_lines` were reported, or the result is `failed`; a report without `files_changed` completes and leaves the rule active with a `completion_unverified` row. **Validates: Requirements 6.5, 6.6**
17. **Rule ids unique across restarts.** *For any* sequence of creates, revokes, direct row deletions and restarts, issued rule ids are distinct, match `^r[1-9][0-9]*$` and never reuse a number. **Validates: Requirements 1.1**
18. **Persisted rules round trip.** *For any* accepted rule, restart returns equal field values, status and `use_count`. **Validates: Requirements 1.8**
19. **Proposal creation limits.** *For any* sequence of creates with `enforce_limits` on, an incident never has two active proposals, a service never gets more than 10 new proposals in a rolling hour, and with `enforce_limits` off neither limit applies. **Validates: Requirements 9.3, 9.4**
20. **IRC line safety.** *For any* action, service id, rule id and actor text, `auto_approved_line` and `rule_line` contain no CR, LF or NUL, are at most 400 UTF-8 bytes, never contain "reply !approve" and survive `format_privmsg` unchanged in content. **Validates: Requirements 7.2, 7.3**
21. **`parse_command` stays total** with the four new commands in the alphabet. **Validates: Requirements 8.3**
22. **Audit hygiene.** *For any* signature, token canary and action text, no audit row or announced line written by this module contains the token or the signature. **Validates: Requirements 7.6**
23. **Approval derives from the column.** *For any* proposal and any `decided_by` text, `approval_of` reports `by: "rule"` iff `approved_by_rule` is set. **Validates: Requirements 4.6, 7.5**
24. **A reduce is never undone by a widen or a reload.** *For any* interleaving (random `sleep(0)` counts and store write delays) of `enable_rule`, `resume`, `patch_rule`, `after_commit` and sweep reloads with `revoke_rule`, `pause_rule` and `pause`, a rule that was revoked (or paused) and whose reduce call began before the widen or reload finished is never `active` in the mirror afterwards (or the hub is still paused), and `RuleBook.merge` never ranks a rule lower than the mirror entry it replaces unless the entry was unchanged and the snapshot is the same rule row. **Validates: Requirements 2.4, 2.6, 6.1**

Success Criterion 7 names: monotone matching (2), no match when a condition fails (1), hourly quota under concurrency (7), default severity (6), `max_per_hour` cap (5), reserved prefixes (12), per-opening counting (8), rule id uniqueness (17), file scope (15), IRC line safety (20). All are required tasks; optional (`*`) ones are 14, 18, 19, 21, 23. Properties 9, 10, 13 and 24 are required (the review fixes H1, H2, M1, M6 are tested through them).

## Testing Strategy

**Unit (pytest, example-based), one file per component under `tests/unit/`:** `test_approvals_scope.py`, `test_approvals_validate.py`, `test_approvals_matching.py` (one test per condition 4.2.1 to 4.2.13 plus the audit detail), `test_approvals_ledger.py` (the one cooldown formula, H1), `test_approvals_rules.py` (lifecycle, limits 20/100, patch, enable conflict), `test_approvals_reduce.py` (token-free pause, revoke, withdraw; limiter coalescing: eleven calls give ten rows plus one coalesced row, all eleven take effect), `test_approvals_create.py` (hub seam: auto-approve, skipped, cooldown, 409 and 429, store-backed and bare), `test_approvals_complete.py` (R6.5, R6.6, expiry after 30 minutes by fake clock), `test_approvals_subscribe.py` (code folder change, service delete, a raising subscriber rolls back), `test_approvals_routes.py` (every route in the table, with and without `enforce_access`), `test_approvals_irc.py` (the four commands, no create/edit/enable commands, `!help`), `test_approvals_static.py` (MCP allow-list R2.2, no `time.time` in `approvals/`, `hub_transition` call sites, `HubAuthority(` only in `hub.py`, dependency rows). The existing `test_find_code_hints` behaviour is pinned by a golden test (L4).

**Modes (M2).** `tests/unit/test_approvals_modes.py` is parametrized over `mode in ("bare", "store")` and runs Success Criteria 1 to 4 against a plain `Hub()` with `create_app(hub)` and against `create_app(store=...)` with a temporary database. The integration module `test_approvals_integration.py` (task 10.1) runs SC1 to SC9 through `httpx.ASGITransport` with a fake clock injected through `Hub(clock=...)`.

**Property tests** `tests/pbt/test_approvals_*_properties.py` for properties 1 to 24.

**Existing tests.** Every existing test passes unchanged except `test_proposals_endpoint_fields` (R9.7) and, owned by `agent-loop`, `test_kiro_assets.py::test_power_docs_name_only_real_tools_and_cover_all`. `test_irc_commands.py` creates 205 proposals on one hub: `enforce_limits` is off there, so it is unaffected (verified by task 5.2's test that a bare `Hub()` has `approver is None`). Fixtures: the store's `store`, `mem_store`, `FakeClock`; a `make_manager(mode)` helper in `tests/unit/approvals_helpers.py`.

**Manual.** IRC client check of `!rules`, `!revoke`, `!pause` and the `AUTO-APPROVED` line (R7.2, R8.1); README review of the reserved prefix list (L7) and the honest limit; the documented-text check of R5.4.

## Review resolutions

| Id | Decision | Where it becomes work |
|---|---|---|
| M2 | Named interfaces: `StateView`, `ProposalWriter`, `ServiceRegistry`, `ReduceAuditLimiter`, `announce`, `now` injected into `ApprovalManager.load`; the hub gets the one `ProposalApprover` seam. A parametrized test runs the module against a bare `Hub()` and the store-backed hub. | tasks 5.1, 5.2, 10.1 |
| M3 | Section "Threading and transaction model": decision on the loop thread in an await-free block; commit in one store transaction (store-backed) or the private store (bare); callbacks never take a hub lock (there is none); subscriber callbacks use only `tx`; the in-memory `create_proposal` is atomic by being await-free, not by a lock. The requirement text says "hub lock"; the store design replaced locks with await-free blocks (store T2, review L12). | tasks 4.5, 5.1, 5.2, 9.1 |
| L4 | `CODE_SUFFIXES` and `SKIP_DIRS` public in `diagnostics.py`; `SOURCE_SUFFIXES` extends them in `approvals/scope.py`; golden test keeps `find_code_hints` unchanged. | task 1.1, 1.3 |
| L7 | Reserved list is the store's `is_reserved_decider` (`rule:`, `rule-`, `withdrawn:`, `expired:`, `expired-`); documented in the README; refused on REST, IRC and terminal. | tasks 6.2, 7.1, 8.3 |
| L8 | R4.2 (a) to (m) split into 4.2.1 to 4.2.13 (thirteen conditions); one test each. | task 2.1 |

### Round 1 review responses

The review (`.agents/tasks/remaining-designs/review-auto-approval.md`) had 2 HIGH, 6 MEDIUM and 12 LOW findings. Every HIGH and MEDIUM finding is fixed. Each LOW finding is fixed, turned into a test-first task, or listed under the open items.

| Id | Response | Design place | Work |
|---|---|---|---|
| H1 | Fixed. One formula, `reopen_at(opening) - completed_at < cooldown`, with the first-seen opening time persisted in `incident_openings`. The observed-reopen branch and the creation-time fallback are gone. | Decision 6, "Usage ledger", schema, Property 9 | 2.2, 2.4, 3.1, 5.1, 10.1 |
| H2 | Fixed. "Rule state ordering": versions, conditional widen and reduce UPDATEs, mirror update only if unchanged, tighten-only merge, dirty set persisted before the reload, reduce operations do not take `_rules_lock`. New Property 24. | Decision 3, "Rule state ordering", A5, A6, Sweep, Property 24 | 3.1, 4.2, 4.4, 4.5, 9.1 |
| M1 | Fixed. `confirm` returns a denial text when the rule is no longer active, and the hub denies the new proposal in the same unit. Property 10 states it. | "The hub seam", "Hub changes", Property 10 | 4.2, 5.1, 5.2, 9.1 |
| M2 | Fixed. `rule_auth_failed` is written only on 401 and never on 429. The bound is stated as the services rate, with a 100-request test. | "Authorization failures" | 6.1 |
| M3 | Fixed. Manager-level tests moved from 3.1 and 3.2 to 4.1. Tasks 3.1 and 3.2 stay at the rulebook level. | none (task order) | 3.1, 3.2, 4.1 |
| M4 | Fixed. New task 4.6 tests `approval_of`, `proposal_view`, `status()`, `rules()` and `rule()`. `persist` and `shares_store` are tested in 5.2. `attach` is dropped and `load` subscribes. | "Subscriptions", "Public interfaces" | 4.5, 4.6, 5.2 |
| M5 | Fixed. `expire_due` is bounded to 1 s, never blocks on the lock and never raises. `approval.expired` and the synchronous `ProposalExpired` check keep correctness without the store. | "Sweep", "`expire_due()` contract" | 4.4, 4.6, 5.3, 6.2 |
| M6 | Fixed. Revoke, pause and withdraw return 200 with the unchanged object for a final or target state. Only an unknown id is refused (404). | Lifecycle, REST table, Property 13 | 4.2, 4.4, 6.1 |
| L1 | Fixed. `SubscriberFailed` is used throughout and checked when services lands. | Subscriptions table | 4.5 |
| L2 | Task. The first test of 7.1 pins `reduce_key` and its kinds ("Needs from services" 0). | Needs from services | 7.1 |
| L3 | Open item O3, extended with the `by` length cap. `requirements.md` is not edited here. | O3 | none |
| L4 | Fixed. The new rule routes forbid extra keys, and the two existing request models ignore them. | REST surface | 6.1, 6.2 |
| L5 | Fixed. The counts in `tasks.md` are recomputed. | Effort estimate | none |
| L6 | Fixed. `**/.git/**`, `**/.kiro/**`, `**/.github/**`. | Scope | 1.3 |
| L7 | Accepted and bounded. The scope check runs only when the service has an active rule. | Scope | 5.1 |
| L8 | Task. `ProposalLimits.seed` runs at load from `StateView`. | Usage ledger | 2.2, 4.1 |
| L9 | Fixed. `approved_at` is `null` after pruning, and a test covers it. | Data models | 4.6 |
| L10 | Fixed. A paused hub writes one row per proposal, and only when the service has an active rule. | `write_in_tx` | 5.1 |
| L11 | Security sentence and open item O9. | Security | none |
| L12 | Open item O10 (the map is outside this module). | O10 | none |
| D-CF | 2026-11-18, user decision, option A: discovery never guesses or pre-fills a code folder. A code folder the user types at accept time is accepted through the services registry with the same normalization and too-broad checks as the services page, and needs the admin token like every accept. This replaces the earlier rule that discovery never sets a code folder. | Needs from discovery | none (discovery task 6.1) |

## Needs from store, services, discovery (flagged for the user; no approved design is changed)

**Needs from store** (all small; none changes a table or a signature that exists). Resolved 2026-11-18: items 1, 2, 4 and 5 are coordination notes. Nothing is requested from the store build; this module implements them inside its own tasks on top of the finished store code. Item 3 is not needed (O2).
1. **Hub seam edit region.** `Hub.create_proposal`, `decide` and `complete` are rewritten by store tasks 11.3, 11.3a and 11.3b. This module adds `Hub.approver`, `Hub.approvals`, `Hub.enforce_limits`, the `CreateContext`/`prepare`/`confirm`/`abort`/`line_for` calls inside `_create_unit`, and `before_complete`/`validate_completion`/`after_complete` inside `complete`. Recommended: store merges first and this module edits on top of it (the build order already says so). If the store prefers, it can add the three attributes and empty hook calls, so this module's `hub.py` edit shrinks to filling them.
2. **`hub_transition` accepts `expired:ttl`.** The store design restricts `hub_transition` to `withdrawn:` and `expired:` prefixes for `pending -> denied`. R9.8 says `decided_by` `expired`. This design writes `expired:ttl` (inside the allowed prefix). No store change; recorded so the requirement wording is not read literally (O3).
3. **Not needed (O2 resolved 2026-11-18).** `Incident.reopened_at` is not added to the store. The manager observes a reopen within one sweep tick (up to 60 s late), which the user accepted.
4. **`T10` call site.** `ApprovalManager.load` calls `store.read_sync(..., allow_blocking=True)` from the synchronous `create_app`, like `ServiceRegistry.load`. Add it to the T10 sentence; no code change.
5. **`MODULE_BLOCKS["approvals"] = (400, 499)`** is already in the store design; this module registers versions inside it.

**Needs from services:**
0. **(L2)** `services.limiter.reduce_key(kind, who)` with the kinds `irc`, `terminal` and `rest`. The first test of task 7.1 imports it and pins these three kinds. If a kind is missing, 7.1 adds it to the services-owned file in the same way it adds the `handle_command` keyword (item 1).
1. `handle_command(hub, nick, channel, cmd, *, verdict=None, reduce_key=None)`: one more keyword so the dashboard terminal can pass `reduce_key("terminal", client)` (R6.5 names the terminal key `dashboard-terminal:<address>`). Default `None` means `reduce_key("irc", nick)`. The services task that changes this signature (7.2) should add the parameter; if it does not, this module's task 7.1 adds it.
2. `registry.get(service_id)` must return the current resolved `code_folder` synchronously (it does: reads are in-memory, no I/O).
3. `ReduceAuditLimiter.record` already returns `bool` and takes `rule_id` and `proposal_id`; no change.

**Needs from discovery (resolved 2026-11-18, user decision, option A):** discovery never guesses or pre-fills a code folder: scan results and suggestions carry none. A code folder defines the write scope of auto-approval, so it may come only from the user. A code folder the user types when accepting a suggestion is accepted through the services registry with the same normalization and too-broad checks as the services page, and needs the admin token like every accept. Without a typed folder, rules fail with 422 `service has no code folder` until the user sets it (`PATCH /api/services/{id}`), which is the intended order.

## Open items for the user

Every item below is resolved: the user accepted each recommended answer on 2026-11-18, and the design is built on those answers. None changes a requirement; `requirements.md` is not edited in this phase.

- **O1. R6.9 "in one transaction" for rule suspension and proposal denial.** The rule status change and its audit row are one transaction (inside the registry transaction for folder changes and deletes). The denial of the rule's approved proposals is a second step (`hub_transition`, one commit each) because the hub owns the proposal mirror and the subscriber callback runs on the writer thread (store T7). The window is closed by (a) the live-folder condition 4.2.12, (b) the mirror update before any `await` for reduce operations, and (c) a sweep that denies any `approved` proposal of a non-active rule within 60 seconds. Resolved (accepted 2026-11-18): two steps as described. The alternative (write the proposal rows inside the callback) breaks the hub mirror.
- **O2. Cooldown clock.** The reopen time is "first observed" by the manager (in `prepare` and the 60 s sweep), so it can be up to 60 s late. Resolved (accepted 2026-11-18): the up-to-60 s observation lag is accepted for V1. `Incident.reopened_at` is not added to the store (Needs from store 3 is marked not needed). The 25 h versus 1 h acceptance cases (SC8) are far from that error.
- **O3. Wording differences with the requirement text.** (a) Reserved prefixes are the store's tightened list (R4.8 says bare `rule` and `expired`; L7 allowed tightening). (b) Pending expiry writes `decided_by` `expired:ttl` (R9.8 says `expired`). (c) Conditions are numbered 4.2.1 to 4.2.13 (thirteen). (d) `by` is capped at 30 characters (store, `DecideRequest.by`), while R4.8 says 60. (e) R2.3 says "never refused", and this design adds that revoke and pause of a final rule answer 200 unchanged. Resolved (accepted 2026-11-18): the design wording stands; `requirements.md` R4.8, R9.8 and R2.3 are updated on the next requirements revision (L3).
- **O4. REST actor strings.** R2.5 says the actor is `dashboard` or the IRC nick. REST calls record `rest:<client address>` (the services default), because the hub cannot tell the dashboard page from `curl`. Resolved (accepted 2026-11-18): REST calls record `rest:<client address>`; the dashboard can display `rest:127.0.0.1` as "dashboard".
- **O5. Reduce operation with a failing store.** A revoke or pause during a store outage returns success and takes effect in memory; persistence retries every 60 s. After a crash in that window the rule is `active` again on restart. Resolved (accepted 2026-11-18): accepted as stated. Residual risk, kept documented: the outage window is the only exposure, a crash inside it brings the rule back as `active`, and the audit row is written when the store returns.
- **O6. Patchable fields.** `PATCH /api/rules/{id}` changes only the six numeric fields, the expiry and the note. Changing the signature, category, service or folder needs a new rule. Resolved (accepted 2026-11-18): only those fields are patchable (smaller widen surface; contingency item 4 of the map still drops `PATCH` entirely).
- **O7. Rule creation from a `done` proposal whose rule approval created it.** R3.1(a) refuses it (409). Same for an auto-approved proposal that is still `approved`. Resolved (accepted 2026-11-18): refused with 409, as stated.
- **O8. Withdraw of a proposal that is not `pending`.** Because R2.3 says withdraw is never refused, it returns 200 with the unchanged proposal (the IRC reply says nothing was withdrawn) instead of 409. Revoke of an `expired` or `revoked` rule and pause of a final rule behave the same way (M6). Resolved (accepted 2026-11-18): 200 with the unchanged object.
- **O9. `GET /api/audit` is open (L11).** Rows can show code-folder paths and declared file names. Resolved (accepted 2026-11-18): open for V1 (localhost, `HostGuard`, consistent with the other GET routes). Put it behind the token only if the dashboard moves off localhost.
- **O10. The capability map does not list the injected interfaces (L12).** This design names them in its own table. Resolved (accepted 2026-11-18): a one-line pointer to the per-module injected-interface tables was added to `docs/v1-capability-map.md` near the dependency groups.

## Effort estimate

Based on measured velocity: the store runs about 5 to 11 minutes per top-level task and about 2.5 minutes per sub-task, tests included. This module has 10 top-level tasks and 33 sub-tasks (including 2 checkpoints), of which 3 are optional property tasks (they cover properties 14, 18, 19, 21 and 23), so 30 sub-tasks are required.

| Part | Count | Agent build time |
|---|---|---|
| Top-level tasks (wiring, checkpoints, integration, review of failures) | 10 | 50 to 110 min |
| Sub-tasks, required at about 2.5 min | 30 | about 75 min |
| Property sub-tasks, optional, at about 3 min | 3 | about 10 min (skippable) |
| Fix-ups after the first full-suite run (hub seam merge with the store's `hub.py` changes) | | 15 to 30 min |
| **Total agent build time** | | **about 1.5 to 2.5 hours; plan 2 hours** (map estimate 1.75 days = about 14 working hours, which included design and review time the specs already spent) |

Human-in-the-loop work (separate, not agent time):

| Item | Time |
|---|---|
| Review and approve this design and `tasks.md`, answer O1 to O6 | 20 to 30 min |
| IRC client check: `!rules`, `!revoke`, `!pause`, `AUTO-APPROVED` line, no create commands (R8.1, R8.2) | 15 min |
| README review (reserved prefixes, rule semantics, honest limit) | 10 min |
| Demo rehearsal of Success Criteria 1 and 8 with real clocks: set `CHIMERICMESH_REOPEN_AFTER_SECONDS=10`, `CHIMERICMESH_AUTOFIX_COOLDOWN_SECONDS=60`, run the legacy-app defect, create the rule, watch the auto-approval and the cooldown refusal. The `upstream.py` defect must be restored first (L6) | 20 to 30 min |
| Browser check of the rule UI | counted in `dashboard` |
| Merge approval after the store, services and connectors are merged | 10 min |
| **Total human time** | **about 75 to 95 min** |

## Implementation deviations (recorded 2026-11-21)

- B1: a file outside the code folder, an absolute path or a path with '..' in a proposal for a service with a code folder is rejected with 422 (the design's input table) and never reaches matching; Success Criterion 2 is met by protected and non-source paths staying pending with condition 'files'.
- B2: 'kind' is stored as the caller sent it; no default of code_change.
- B3: Hub.hub_transition has an announce=False keyword used by the create unit when the confirm re-check denies a new proposal.
- Hub proposals use the hub clock for created/decided times (was time.time(); same default).

- B4: create_app with a store lacking the approvals migrations logs one warning and runs without rules (fail closed) instead of raising.
- B5: enforce_limits is True only for create_app() with neither hub nor store (the production path); create_app(store=...) and create_app(hub) leave it as the hub has it.
- B6: test_store_app::test_rest_validation expects the new key set (same reason as test_web_terminal's).
- B7: the IRC NICK rejection of reserved decider prefixes follows enforce_access; the !approve/!deny refusal does not.
- B8: reduce_key keeps rest and irc identical (services test pins it).

