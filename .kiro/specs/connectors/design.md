# Design: connectors (V1)

Requirements: [requirements.md](requirements.md). Capability map: [docs/v1-capability-map.md](../../../docs/v1-capability-map.md) (module `connectors`, B7, B15, B19, B21). Store design (interfaces used here): [../store/design.md](../store/design.md). Services design (the seams this module plugs into): [../services/design.md](../services/design.md). Baseline: [legacy-mesh](../legacy-mesh/design.md).

## Overview

`connectors` adds the intake paths that the hub does not have today: Syslog over TCP, tailing of log files with rotation handling, and the format parsers for common legacy servers (syslog-style text, NCSA, W3C extended, xferlog, plain). It sits in the fourth dependency group. It imports `core`, `store` and `services`, never `hub`, `api`, `commands`, `listeners`, `mcp_server`, `discovery`, `auto-approval`, `agent-loop` or `dashboard`. The hub reaches it only through three things the wiring layer passes in: the `ingest` callable, the registry, and the store.

Seven decisions shape everything else:

1. **The format parsers are `core`, the file and socket work is `connectors`.** R1.5 wants the parsers in `src/chimericmesh/parsers/`, which is group `core` (stdlib only). Each format is a pure pair `(update, parse)` over an immutable `FormatContext` that the tailer owns. Everything that touches the disk, a socket, the store or the registry lives in `connectors/*`. This keeps Success Criterion 4 (10 000 arbitrary strings per parser) cheap and independent of asyncio.
2. **Services tags; connectors only stamps the file source.** `services` installs `Hub.tagger` (services design, "Seams for `connectors`"), so `hub.ingest` tags every event, from every path. `submit(event, log_source_id=None)` therefore does one thing: set `log_source_id` and await the injected `ingest`. It returns the event that `ingest` returns (L1). The three existing listeners, `/api/ingest` and `/api/demo/storm` are not touched (R5, B21). This is the reading of R1.1 that the services design already proposed (its O6); see O1 here.
3. **A tailer never keeps a file open.** Each poll is one synchronous function, `poll_file(path, state, ...)`, run in the default thread executor: `os.stat` (and return at once when nothing changed), then open, read, close. The result is a new immutable `TailState` plus the complete lines. There is no handle between polls, so on Windows a rename is never blocked while a source is idle (a stat-only poll opens nothing). A rename that lands while a poll is reading fails with WinError 32 for that window (measured below, review 8), and the rotator's retry succeeds. The cost is the one R3.3 accepts: lines written after the last poll and before a rename are lost.
4. **File identity is `(device, inode)` plus a fingerprint of the first bytes.** An inode can be reused after a delete, and a copy-truncate followed by quick writes can regrow the file beyond the stored offset before the next poll. Size alone misses the second case and the inode alone misses the first. The fingerprint is a SHA-256 of at most the first 256 bytes, stored as a hash (so log text is never copied into the database).
5. **The offset advances only past a complete line, and only after that line was submitted.** A partial line therefore needs no buffer: it stays in the file and is read again on the next poll (R3.4, R6.1). Offsets are persisted every 5 seconds and on shutdown, so a crash can repeat at most 5 seconds of lines; a graceful stop repeats none. The store's event queue writes rows in 250 ms batches and can be behind the offset flush, so a hard crash can also lose a few hundred milliseconds of events that were submitted but not yet written (O4, review 19).
6. **One reconcile function, three triggers.** The desired tailers are computed from `registry.log_sources(enabled_only=True)`. `after_commit`, a 30-second timer and `start()` all call the same idempotent `reconcile()`. A skipped or failed `after_commit` is repaired by the timer without any special case (R1.6). The in-transaction subscriber only deletes `tail_offsets` rows, so a rolled-back registry change leaves them alone.
7. **Everything is bounded.** One read is at most 1 MiB, one line at most 8192 bytes, one TCP frame at most 8192 bytes, 64 TCP connections, 2000 lines per second per source, 200 sources (the services cap). Nothing read from a log is evaluated, imported or sent to a socket other than the hub's own announcements.

Technology stack (locked on approval): Python 3.11+, stdlib only (`asyncio`, `os`, `stat`, `re`, `hashlib`, `socket.gethostname`, `dataclasses`, `sqlite3` through the store). No new dependency, no `watchdog`, no `ctypes`. pytest and Hypothesis for tests. No FastAPI or pydantic import anywhere in this module (it adds no route).

Evidence measured on the development machine for this design (Windows 11, NTFS, Python 3.13, 2026-11-18; this is the "NTFS rotation identity" test item of the requirements, repeated automatically by task 4.4):

| Experiment | Result |
|---|---|
| `os.stat().st_ino` of a file, after `os.rename(a, a.1)` | unchanged (the renamed file keeps its id) |
| a new file created at the old name after the rename | a different `st_ino` |
| delete, then create at the same name | a different `st_ino` (no reuse seen on NTFS; ext4 does reuse, hence the fingerprint) |
| copy-truncate (copy, then `truncate(0)`) | same `st_ino`, size 0 |
| `os.rename` of a file that a Python `open()` still holds | `PermissionError`, WinError 32 |
| `os.rename` after the file is closed | succeeds |
| `os.rename` while a poll is inside its read (handle open) | fails with WinError 32 for the length of the read; the stat-only fast path never opens the file and never causes it (review 8; task 4.4 measures the failure rate) |
| `os.stat` of a path with an embedded NUL | raises `ValueError`, not `OSError` (review 9) |
| `os.fstat(fd).st_ino` versus `os.stat(path).st_ino` | equal, and `st_dev` equal |
| size of the values | `st_ino` about 4e15 and `st_dev` about 1.3e17 on this volume; Windows file ids can use all 64 bits, which does not fit a signed SQLite integer, so both are stored as text |
| `os.O_NONBLOCK` | absent on Windows (needed only on POSIX, for FIFOs) |

## Architecture

### Where `connectors` sits

```
core (events, diagnostics, parsers/*)            <- the format parsers are added here
  ^
store (store/*, fsperms.py)
  ^
services (services/*)
  ^
connectors (connectors/*)                         <- this spec
  ^
discovery, auto-approval -> agent-loop -> dashboard
  ^
wiring (hub.py, api.py, commands.py, listeners.py, mcp_server.py, __main__.py ...)
```

### File-to-group rows added by this module (resolves M1 for `connectors`)

| File | Group | Notes |
|---|---|---|
| `parsers/logctx.py`, `plain.py`, `syslogtext.py`, `ncsa.py`, `w3c.py`, `xferlog.py`, `formats.py` | `core` | Already covered by the `parsers/*` row. Stdlib and `core` imports only (`..events`, `.syslog`). The test parses each file with `ast` and flags an `ast.Call` whose `func` is a bare name in {`eval`, `exec`, `compile`, `__import__`} or `builtins.<name>`, and any import of `subprocess`, `os`, `socket`, `importlib`, `pickle`; `re.compile(...)` is an attribute call and is allowed (a fixture proves it, review 14). |
| `connectors/__init__.py` | `connectors` | Pure re-exports. Imports no `asyncio` server code at import time beyond what the other files do. |
| `connectors/config.py`, `submit.py`, `schema.py`, `limiter.py`, `framing.py`, `tailread.py`, `offsets.py`, `tailer.py`, `manager.py`, `syslog_tcp.py` | `connectors` | Imports `core`, `store`, `services` and the stdlib only. May import `chimericmesh.services` (its `__init__` loads no web framework) but not `services.deps` or `services.routes`. Only `manager.py` imports `socket` (for `gethostname`). Only `tailread.py` and `tailer.py` import `os`. No file imports `httpx`, `urllib`, `http`, `smtplib`, `ftplib` or `subprocess`. |

`events.py` (group `core`, edited by the `store` module for `id`, `service_id`, `log_source_id`) gets one more line from this module: `"file"` added to `Source`. `listeners.py` and `hub.py` are not edited. `api.py` is edited once (task 8.1).

### Package layout

```
src/chimericmesh/parsers/
  logctx.py       FormatContext, ParseOutcome, LineFormat, clean_line, redact, file_host, syslog_text_host,
                  http_severity, truncate
  plain.py        parse_plain
  syslogtext.py   parse_syslog_text
  ncsa.py         parse_ncsa
  w3c.py          update_w3c, parse_w3c
  xferlog.py      parse_xferlog
  formats.py      FORMATS, FORMAT_NAMES, get_format
src/chimericmesh/connectors/
  __init__.py     pure re-exports
  config.py       ConnectorConfig, TcpConfig, resolve_tcp_port
  submit.py       Ingest, Submit, make_submit
  schema.py       register_migrations, connectors_registry
  limiter.py      TokenBucket, DropNotice
  framing.py      FrameDecoder, FrameError, detect_mode
  syslog_tcp.py   ListenerStats, TcpSyslogServer, start_syslog_tcp
  tailread.py     FileIdent, TailState, RawLine, PollResult, poll_file, decode_line
  offsets.py      OffsetRow, OffsetStore, resume_state
  tailer.py       SourceSpec, SourceCounters, FileTailer, build_spec
  manager.py      ConnectorManager
```

### Seams used (all defined by the services design; none is added here)

| Seam | Used as |
|---|---|
| Tagging | Nothing to call. `hub.tagger = registry.tag` is installed by `create_app` (services task 7.1 and 7.4). File events carry `log_source_id`; `tag` assigns the source's service first (services R4.2b). |
| Subscribe | `registry.subscribe(on_service_changed=drop_offsets, on_service_deleted=drop_offsets, after_commit=manager.on_change, name="connectors")`. |
| Format names | `registry.set_known_formats(FORMAT_NAMES)` in the manager constructor. |
| Runtime status | `registry.set_status_provider(manager.runtime)` in the manager constructor. |
| Reading configuration | `registry.log_sources(enabled_only=True)`, `registry.log_sources()`, `registry.get(service_id)`. |
| Migrations | Block 200 to 299: `connectors:0200-tail-offsets`. No foreign key into `services` tables (services design, recommended). |
| Read interface for hub state | None. `connectors` reads no incident, proposal or event. |
| Ingest | One injected callable, `ingest: Callable[[Event], Awaitable[Event | None]]`, the bound method `hub.ingest`. |

### Threads and transactions (applies store T1 to T10; M3 as it concerns this module)

| Code | Runs on | Rules |
|---|---|---|
| Listeners, tailer tasks, `reconcile`, `submit`, counters, `runtime()` | the loop thread | Touch no SQLite. Await only `ingest`, `asyncio.to_thread`, sleeps and `store.transact` / `store.read`. |
| `poll_file` | a default-executor thread (`asyncio.to_thread`) | Pure function of a path and an immutable `TailState`. It touches no hub, registry, store or logger state, so a late result of a cancelled poll can be dropped safely. |
| `drop_offsets` (the in-transaction subscriber) | the `store-writer` thread, inside the registry's transaction (T7) | Uses only the `tx` and the immutable `ServiceChange`: one table-existence check and one `executemany` of a literal `DELETE`. It does not await, call `Store.transact*`, `read*` or `AuditLog.append`, and touches no manager state. |
| `OffsetStore.flush` and `.prune` | awaited `store.transact` (T3) | The row tuples are built on the loop thread from frozen `TailState` snapshots before the call; the closure touches nothing else, and tolerates the up-to-four re-runs of T8 because it is an idempotent upsert. |
| `after_commit` (`manager.on_change`) | the loop thread, 5-second limit from services | Calls `reconcile()`, which stops removed tailers concurrently (graceful wait at most 2 seconds, then cancel) and starts new ones. |

There is no lock between coroutines other than one `asyncio.Lock` that serializes `reconcile`. No hub lock exists (T2).

## Components and Interfaces

### Format context and shared helpers (`parsers/logctx.py`, `core`)

```python
@dataclass(frozen=True, slots=True)
class FormatContext:
    machine: str = "localhost"            # socket.gethostname(), cut to 100 characters
    service_id: str | None = None         # the log source's service; None or "unassigned" = no suffix
    source_host: str | None = None        # LogSource.host
    app: str = "file"                     # first app_names entry, else the file's base name without extension
    w3c_fields: tuple[str, ...] | None = None   # column names from the last "#Fields:" line

@dataclass(frozen=True, slots=True)
class ParseOutcome:
    event: Event | None                   # None = skip this line (comment, blank)
    matched: bool                         # False = the line did not fit the declared format (R4.8)

UpdateFn = Callable[[str, FormatContext], FormatContext]
ParseFn = Callable[[str, FormatContext], ParseOutcome]

@dataclass(frozen=True, slots=True)
class LineFormat:
    name: str
    update: UpdateFn                      # state carried across lines; identity for every format but w3c
    parse_line: ParseFn                   # richer form, used by the tailer
    def parse(self, line: str, ctx: FormatContext) -> Event | None     # the requirement's (line, context) -> Event | None
```

R1.5 and R4.1 are met by one convention: the tailer calls `ctx = fmt.update(line, ctx)` and then `fmt.parse_line(line, ctx)` for every line. Both are pure functions of their arguments, so `parse(line, ctx)` is a pure function of `(line, context)` and the `#Fields:` state is explicit and owned by the tailer.

```python
MAX_MESSAGE = 4096
MAX_RAW = 1024
def clean_line(text: str) -> str                    # removes [\x00-\x08\x0b-\x1f\x7f], keeps tab; no CR/LF in the result
def truncate(text: str, limit: int = MAX_MESSAGE) -> str
def redact(text: str) -> str                        # credentials-looking fragments -> "<redacted>"
def http_severity(status: int) -> int               # 5xx 3; 408 and 429 4; other 4xx 5; everything else 6
def file_host(ctx: FormatContext) -> str            # rule 4.10
def syslog_text_host(line_host: str, ctx: FormatContext) -> str    # rule 4.12
def make_file_event(ctx: FormatContext, *, severity: int, message: str, raw: str, host: str | None = None,
                    app: str | None = None) -> Event             # source="file", clamps severity to 0..7, applies redact/truncate
```

**Host and app (R4.10, R4.12).** `file_host(ctx)` is `ctx.source_host` when it is set; otherwise `ctx.machine`, plus `"/" + ctx.service_id` when the service id is a real id (not `None`, not `unassigned`). `syslog_text_host(line_host, ctx)` is `line_host[:150]` plus the same suffix when the line carried a host, otherwise `file_host(ctx)`. The longest result is `max(100, 150 + 1 + 40) = 191` characters, inside the 255-character `incidents.host` limit of the store. The remote client address of an access-log line never reaches `host`; it stays in `raw`. Because `signature(message)` collapses numbers and addresses and the host is the same for every client, three client IPs requesting one failing path make one incident (R4.11).

**Redaction (R4.7).** One compiled pattern with a bounded negated character class per branch (no nested quantifiers, so no catastrophic backtracking):

```python
_SECRET = re.compile(
    r"(?i)\b(authorization)\s*:\s*(?:[A-Za-z]+\s+)?[^\s\"']{1,512}"
    r"|\b(password|passwd|pwd|pass|secret|token|access[_-]?token|api[_-]?key|apikey|sessionid)"
    r"\s*[=:]\s*(?:\"[^\"]{0,512}\"|'[^']{0,512}'|[^\s&;,\"']{1,512})")
```

A match is replaced by `<name>=<redacted>` (`Authorization: <redacted>` for the header form). `redact` is applied to `message` and `raw` of every file event, not only NCSA and W3C (a design addition: Authorization headers and `token=` pairs appear in application logs too, and it costs one function call; the requirement's NCSA and W3C text is a subset). The NCSA and W3C paths also drop the query string (R4.3).

**How `raw` is built for NCSA and W3C (review 6).** `redact` removes `name=value` secrets, not arbitrary query text such as `?id=7&email=a@b.c`, so `raw` is constructed, not just redacted. NCSA: the request target inside the quoted request is cut at its first `?` or `#`, and the Referer URL (Combined format) is cut the same way; the rest of the line is kept (client address, timestamp, status, size and user agent included); then `redact`, then truncation to 1024 characters. W3C: the `cs-uri-query` column is replaced by `-` before the columns are joined with spaces; then `redact` and truncation. In both formats the client address stays in `raw`. The `?` in a Referer is stripped, not only redacted. For `plain` and `syslog-text` lines `raw` is the cleaned line after `redact` (no query construction, because there is no format to know where a query is).

### The five formats (pure, `core`)

All parsers take a line already cut to 8192 bytes by the tailer and decoded (`decode_line`), and return `ParseOutcome`. A line that is empty after `strip()` returns `ParseOutcome(None, True)`.

| Format | Rule | Severity | Message |
|---|---|---|---|
| `plain` | the whole line | `infer_severity(line, 6)` (events.py) | the line, cleaned, truncated; `host` by 4.10, `app` is `ctx.app` |
| `syslog-text` | `syslogtext.py` tries three patterns in this order: its own compiled ISO 8601 pattern (`2026-11-18T10:00:01.123+00:00 host app[12]: text`, the default of current rsyslog), then `_RFC3164` and `_RFC5424` imported from `parsers/syslog.py` (module-level compiled patterns of group `core`; that file is not edited, review 7). `matched` is true when one of the three matched. Host, app and message are that pattern's groups (a host of `-` counts as no host), so a real app named `syslog` and the "no match" fallback of `parse_syslog` are never confused. When no pattern matches the result is the `plain` outcome with `matched=False`. After a match of an RFC pattern, `parse_syslog(line, default_host="")` is called only to take its severity rules (the ISO pattern uses `infer_severity(message, 6)`). `source` becomes `file`. | the severity `parse_syslog` returns (from text, because no PRI); then `min(sev, 4)` when the text contains `status=deferred`, `min(sev, 3)` when it contains `status=bounced` or `status=rejected` (never less severe than before) | the text after `app[pid]:`; host by 4.12; `app` from the line (see O3), else `ctx.app` |
| `ncsa` | Common and Combined: `^(\S+) (\S+) (\S+) \[([^\]]{1,64})\] "([^"]{0,2048})" (\d{3}) (\S+)(?: "([^"]{0,2048})" "([^"]{0,2048})")?` | `http_severity(status)` | `<METHOD> <path> -> <status>` with everything from the first `?` or `#` removed; a request line that is not `METHOD target [proto]` becomes `"<first 100 characters, cleaned>" -> <status>` |
| `w3c` | `update_w3c` sets `ctx.w3c_fields` from a `#Fields:` line. Other `#` lines parse to `ParseOutcome(None, True)`. A data line is split on spaces (IIS uses `-` for empty and `+` for a space inside a value) and mapped by name. A data line before any `#Fields:`, or with a different number of columns, is handled by `plain` and reported `matched=False`. | `http_severity(sc-status)`, and `min(sev, 4)` when `sc-win32-status` is a non-zero number | `<cs-method> <cs-uri-stem> -> <sc-status>`; `cs-uri-query` is never used |
| `xferlog` | a right-anchored regex over the 14 standard fields; the file name may contain spaces | `c` is 6, `i` is 4 | `<download\|upload\|delete> <filename> (<complete\|incomplete>)` for direction `o`, `i`, `d` |

Unparsed lines (R4.8): the format returns the `plain` result with `matched=False`; the severity is `infer_severity(line, default=6)` ("6 or the inferred severity" in the requirement reads as: 6 unless the text says more). The tailer counts `unparsed`.

**Event time (review 16).** `ts` is left at the `Event` default, the time the hub ingested the line, for every file format in V1. The file's own timestamps are not parsed (the NCSA `[...]` field, the W3C date and time columns, the syslog-text stamp): they have no year in syslog-text and mixed zones elsewhere, and a backfilled or caught-up line would otherwise carry an old time into incident first-seen values. Parsing them later does not change any interface (O12).

**W3C header (review 2).** `update_w3c` only sees a `#Fields:` line that the tailer feeds it, and an attach, a resume or a restart starts after the header. The tailer therefore calls `header_fields(path)` (see "Reading files") and applies `update_w3c` to its result for the `w3c` format after the first poll of every start (reasons `attached`, `resumed`, `appeared`) and after `rotated` and `truncated` (where `w3c_fields` is first reset to `None`), before any data line of that poll is parsed. A manager restart after a format or host edit rebuilds the context and so re-reads the header. R4.4's fallback to `plain` is then limited to what the requirement allows: a data line with no `#Fields:` line in the first 4096 bytes of the file, or a column count that does not match.

`FORMATS` maps the five names to `LineFormat` objects; `FORMAT_NAMES` is the tuple the manager gives to `registry.set_known_formats`. The set equals the services default (`syslog-text`, `ncsa`, `w3c`, `xferlog`, `plain`), so a registry that was never given the names still accepts the same formats.

### `submit` (`connectors/submit.py`)

```python
Ingest = Callable[[Event], Awaitable[Event | None]]
Submit = Callable[[Event, str | None], Awaitable[Event]]       # (event, log_source_id=None)

def make_submit(ingest: Ingest) -> Submit
```

`submit(event, log_source_id=None)` does `event = dataclasses.replace(event, log_source_id=log_source_id)` when the argument is not `None`, awaits `ingest(event)`, and returns the result when it is an `Event` and the event it sent otherwise (an injected test `ingest` may return nothing; the store-backed `Hub.ingest` always returns the tagged event, so production returns the tagged event with its `id`, L1). It never mutates its input and never catches an exception from `ingest`; the callers do (R1.3). The function is built once by `create_app` and shared by the tailers and the TCP server.

Sub-criteria of R1.1 used by the tests (resolves M2 and L1 inside the design; `requirements.md` is not edited):

| Id | Condition |
|---|---|
| 1.1a | `submit` sets `log_source_id` when given and leaves the event otherwise unchanged; the input event is not mutated. |
| 1.1b | `submit` awaits the injected `ingest` exactly once and returns the tagged event it returns. |
| 1.1c | The service is assigned by the hub's tagger, not by `submit`; with `hub.tagger = registry.tag`, a file event gets its source's service (services R4.2b) and a TCP syslog event is matched by `app` or `host`. |
| 1.1d | `connectors` does not import `hub` (dependency test). |
| 1.1e | The same tailer, TCP listener and parser tests pass against a plain `Hub()` (private `:memory:` store) and against a store-backed hub (task 8.2). |

**How R1.1 and R5.1 are met (review 3, O1).** `submit` is used by the two intakes this module owns, the file tailer and TCP syslog. The UDP, Telnet and IRC listeners, `/api/ingest` and `/api/demo/storm` keep calling `hub.ingest` and receive their service from the same hub tagger, so every event is tagged by the same rule even though it does not pass through the function named `submit`. This is a departure from the literal text of R1.1 and R5.1 and waits for the user's answer to O1. If the user declines, task 8.6 (conditional) passes `submit` into the three listeners and the two REST routes.

### Syslog over TCP (`connectors/framing.py`, `connectors/syslog_tcp.py`)

```python
Mode = Literal["octet", "lf"]

@dataclass(frozen=True, slots=True)
class FrameError:
    reason: Literal["bad_length", "too_long", "zero_length"]

def detect_mode(buf: bytes) -> Mode | None           # None = need more bytes

class FrameDecoder:
    def __init__(self, max_frame: int = 8192) -> None
    mode: Mode | None
    failed: bool
    def feed(self, data: bytes) -> list[bytes | FrameError]     # complete frames, in order; after a FrameError returns []
    def finish(self) -> list[bytes]                              # LF mode: the unterminated last line at EOF
```

**Mode detection (R2.4).** `detect_mode` looks at the leading digits of the buffer. One to four digits followed by a space is `octet`. Zero digits, five or more digits, or one to four digits followed by anything but a space or a digit (`2026-11-18T...`, `5-abc`) is `lf`. A buffer that is all digits and shorter than five bytes returns `None` and waits. EOF in that state is decided as `lf`. The mode is fixed for the connection after the first decision.

**What a "100 KB frame" test means (review 1, O10).** A real 100 KB octet frame starts with `102400 `, which is six digits, so on a fresh connection R2.4 selects LF framing: the first line is emitted cut at 8192 bytes, the rest of it is discarded, the connection stays open and neither `dropped` nor `framing_errors` increases. Success Criterion 3 ("the connection closes and `lines_dropped` is 1") therefore holds for a connection that is already in octet mode. The tests use Option A: send a valid `5 hello` frame first (the decoder is then in octet mode), then the 100 KB frame, which gives a `FrameError` (`bad_length`, six digits) and closes only that connection; and a connection whose very first bytes are `9999 ` (four digits and a space: octet mode) gives a `FrameError` (`too_long`, 9999 above 8192). A separate test pins that a first frame `102400 ...` on a fresh connection is handled as LF and does not close it. Option B (accept five to seven digits as octet) would contradict R2.4's text and is not taken.

**Octet mode.** Skips any CR and LF bytes between frames (senders that add a trailing newline are tolerated; this is a leniency, not in RFC 6587). The header is the leading digits, one space, and the length. A header that does not parse, has more than four digits, announces zero, or announces more than 8192 returns `FrameError` and the connection closes (R2.5). The payload is exactly `length` bytes; bytes after it start the next header. A frame is decoded as UTF-8 with replacement characters (R2.8) and CR and LF inside it become one space, so a multi-line message is one event.

**LF mode.** Splits on `\n`, drops one trailing `\r`, ignores empty lines. A line that reaches 8192 bytes without a newline is emitted truncated and the rest of that line is discarded up to the next newline. This does not close the connection and does not count as a dropped line (R2.5 names frames).

```python
@dataclass(slots=True)
class ListenerStats:
    accepted: int = 0; refused: int = 0; active: int = 0
    frames: int = 0; dropped: int = 0; framing_errors: int = 0; idle_closed: int = 0

class TcpSyslogServer:
    def __init__(self, submit: Submit, cfg: TcpConfig | None = None) -> None
    stats: ListenerStats
    port: int | None                                  # the bound port, None when not listening
    async def start(self, host: str, port: int) -> bool       # False when the port is in use (one warning)
    async def aclose(self, timeout: float = 1.0) -> None

async def start_syslog_tcp(submit: Submit, host: str, port: int | None,
                           cfg: TcpConfig | None = None) -> TcpSyslogServer | None
    # None when port is None (disabled by CHIMERICMESH_SYSLOG_TCP_PORT=0) or the bind failed
```

Connection handling, one coroutine per connection: if `stats.active` is 64 the connection is closed at once and `refused` increases (R2.6). Otherwise `active` and `accepted` increase and the loop reads up to 65536 bytes with `asyncio.wait_for(reader.read(65536), 60)`; a timeout closes the connection and counts `idle_closed`. Each frame is decoded, parsed with `parse_syslog(text, default_host=peer_ip)` (the peer IP is the host when the message carries none, as UDP does, R2.7), and passed to `submit(event, None)`. A failure of one frame (an exception in parsing or in `ingest`) is logged at warning level (at most one line per 10 seconds per server), counts `dropped`, and the loop continues (R1.3). A `FrameError` counts `framing_errors` and `dropped` once and closes only that connection. The server never writes a byte back. `aclose` closes the listening socket and every open writer, then waits for the server with a timeout, because `Server.wait_closed()` waits for connections on Python 3.12.1 and later.

`resolve_tcp_port(environ)` reads `CHIMERICMESH_SYSLOG_TCP_PORT`: unset gives 5514, `0` gives `None`, any other integer from 1 to 65535 is used, and anything else logs one warning and uses 5514. The bind address is the existing `CHIMERICMESH_BIND` (default `127.0.0.1`, B15). The counters of `ListenerStats` are exposed on `app.state.syslog_tcp.stats` and logged at info level once a minute when they changed; they are not added to `/api/stats` (O7).

### Reading files (`connectors/tailread.py`)

```python
@dataclass(frozen=True, slots=True)
class FileIdent:
    dev: int
    ino: int                         # 0 when the filesystem has no inode numbers
    head_len: int                    # 0 to 256
    head_sha: str                    # hex SHA-256 of the first head_len bytes

@dataclass(frozen=True, slots=True)
class TailState:
    ident: FileIdent | None = None   # None = never seen the file
    offset: int = 0                  # bytes consumed, always at a line start
    discarding: bool = False         # skip bytes up to the next newline (after an overlong line)
    mtime_ns: int = 0                # for the unchanged fast path; not persisted
    attach: Literal["end", "begin"] = "end"    # used only while ident is None

@dataclass(frozen=True, slots=True)
class RawLine:
    data: bytes                      # at most max_line bytes, no line terminator
    end: int                         # file offset just after this line (and its terminator)
    truncated: bool
    discarding_after: bool = False   # True for an overlong unfinished line emitted cut: the rest of that line must be skipped (review 18)

@dataclass(frozen=True, slots=True)
class PollResult:
    status: Literal["running", "busy", "missing", "unreadable", "error"]   # busy: a transient sharing or lock violation, see step 1
    error: str                       # "" when running; the OS reason otherwise (cut to 200 by the tailer)
    start: TailState                 # adopt before emitting lines (new identity, offset of the first byte read)
    state: TailState                 # adopt after all lines were emitted
    lines: tuple[RawLine, ...]
    reason: Literal["", "attached", "resumed", "rotated", "truncated", "appeared"]
    more: bool                       # the read hit max_read, poll again soon
    bytes_read: int

def poll_file(path: str, state: TailState, *, max_read: int = 1 << 20, max_line: int = 8192,
              backfill_lines: int = 0, head_size: int = 256) -> PollResult
def decode_line(raw: RawLine) -> str     # utf-8 with replacement, truncated suffix, controls removed (R3.10)
def header_fields(path: str, *, size: int = 4096) -> str | None   # the last "#Fields:" line inside the first 4096 bytes, else None; never raises (review 2)
```

`poll_file` is the whole of R3.1 to R3.3, R3.6 to R3.9, R3.12. It never raises: every `OSError`, every `ValueError` (an embedded NUL makes `os.stat` raise `ValueError`) and every `UnicodeError` becomes a status. Steps, in order:

1. `os.stat(path)`. `FileNotFoundError` or `NotADirectoryError` gives status `missing`, with `attach="begin"` when the file was never seen (R3.6: a file that appears later is read from its start). A `PermissionError` whose `winerror` is 32 or 33 (sharing or lock violation: a writer or a rotator holds the file for a moment), and `EBUSY` or `ETXTBSY` on POSIX, give status `busy`: no lines, state unchanged, retried at the normal poll interval. The tailer reports `running` for up to `busy_polls` (5) consecutive `busy` polls and `unreadable` with the OS text after that, so a brief lock does not cause a 30-second blind period or a flapping status (review 9). Any other `PermissionError` or `OSError` gives `unreadable` with `strerror` (R3.7). A `ValueError` or `UnicodeError` (NUL in the path, a path that cannot be encoded) gives `error` with `invalid path`. The same mapping applies to errors raised by the open and the read in steps 3 to 6. A result whose mode is not a regular file gives `error` with `not a regular file` (R3.9). The `state` is returned unchanged except for `attach`.
2. **Unchanged fast path.** When the state has an identity and the stat shows the same device and inode (or inode 0), `st_size == state.offset`, the same `st_mtime_ns`, and `discarding` is false, return `running` with no lines and no open. An idle file costs one `stat` per poll, and a rotator that renames at that moment is never blocked. A rename that lands while a poll is reading (steps 3 to 6) fails with WinError 32 for that window, normally about a millisecond and at most the time to read 1 MiB, and the rotator's retry succeeds; this is the same order as the 500 ms bound that R3.3 documents (review 8). A `more=True` poll is followed by a pause of `catchup_interval` (100 ms), so a busy file is free for at least 100 ms between two reads.
3. Open with `os.open(path, O_RDONLY | O_BINARY | O_NONBLOCK | O_CLOEXEC)` using only the flags that exist on the platform (`O_NONBLOCK` keeps a FIFO from blocking the thread on POSIX), then `os.fstat(fd)` and verify a regular file again (a path can change between the stat and the open). Wrap with `os.fdopen(fd, "rb", buffering=0)` inside a `with` block, so the handle is closed before the function returns.
4. Read up to 256 bytes at offset 0: the head. Compute the new identity `(dev, ino)` from `fstat`.
5. **Decide the start.**
   - `state.ident is None` and `attach == "begin"`: start at 0, reason `appeared`.
   - `state.ident is None` and `attach == "end"` (first attach, R3.2): start at the end of the last complete line within the final 8192 bytes of the file; if there is no newline in that window and the file is larger than the window, start at the end and set `discarding` (the file ends inside an overlong line); if the file is shorter than the window and has no newline, start at 0 (the one partial line is emitted when it is completed). With `backfill_lines = N > 0`, also return the last N complete lines of the final `max_read` bytes before that start, oldest first. Reason `attached`.
   - the stored identity is the same file (same device and inode, or either inode is 0; and the first `head_len` bytes of the current file hash to `head_sha`; and the file has at least `head_len` bytes) and `size >= offset`: resume at `offset`. Reason `resumed` for the first poll after a restart, otherwise empty.
   - the identity matches and `size < offset` (copy-truncate, R3.3c): start at 0, reason `truncated`.
   - the identity or the fingerprint differs (rename-and-recreate, delete-and-create, or copy-truncate followed by quick growth past the old offset): start at 0, reason `rotated`, `discarding` cleared.
6. Seek to the start and read up to `max_read` (1 MiB). If `discarding`, drop bytes up to and including the first newline; if the read has no newline, consume it all and stay discarding.
7. `parts = data.split(b"\n")`. All parts but the last are complete lines, each with `end` set to the offset after its newline; one trailing `\r` is removed; a line over `max_line` bytes is cut and flagged `truncated`. The last part is the unfinished line: it is **not consumed** (R3.4: it stays in the file), unless it already has `max_line` bytes or more, in which case it is emitted truncated, consumed, and `discarding` is set so the rest of that line is skipped when it arrives. That `RawLine` has `discarding_after=True`, and the tailer's `at(raw)` helper sets `state.discarding = raw.discarding_after`, so a stop right after that line and a restart from the stored state still skip the rest of it instead of emitting its tail as an event (review 18).
8. **Head upgrade.** When the identity matched and the stored `head_len` is below 256 and the file now has more head bytes (the first poll after the file was empty or short), the new `head_len` and `head_sha` are computed over the current head (the old prefix was just verified, so this is safe).
9. `start` is the state with the new identity and the offset of the first byte read; `state` has the offset after the last emitted line, the current `mtime_ns`, and `discarding`. `more` is true when exactly `max_read` bytes were read.

`decode_line` decodes UTF-8 with replacement, removes NUL and the other control characters except tab (R3.10), and appends ` [truncated]` for a truncated line.

Because `poll_file` never reads beyond the file the path points to at that moment, it follows exactly the paths that were registered (R "never follow a path from event text"), and it never opens anything that is not a regular file.

R3.3 sub-criteria used by the tests (L8, resolves the finding for this requirement; `requirements.md` is not edited):

| Id | Condition |
|---|---|
| 3.3a | A poll opens the file, seeks to the stored offset, reads and closes it; no handle exists between polls (a rename between two polls succeeds on every platform). |
| 3.3b | WHEN the file identity changes (rename and recreate, or delete and create), the new file is read from its start. |
| 3.3c | WHEN the size is smaller than the stored offset (copy-truncate), the file is read from its start. |
| 3.3d | No line is emitted twice while the tailer runs. |
| 3.3e | No line that was complete and already read before the rotation is lost. |
| 3.3f | Lines appended after the last poll and before a rename or truncation can be lost: the writes of one poll interval (500 ms), plus any backlog that was still unread because the last poll stopped at the 1 MiB limit (`more=True`) when the rotation happened; this is documented in the README (review 20). |
| 3.3g | (design addition) WHEN the file was copy-truncated and regrew beyond the old offset before the next poll, the fingerprint differs and the file is read from its start. |
| 3.3h | (design addition, review 8) A rename between two polls never fails; a rename that lands inside a poll's read may fail once with WinError 32 on Windows and succeeds on retry; an idle file's stat-only poll never causes a failure. |

Implementation notes (2026-11-19, connectors group 2): (C2-2) the race test of 3.3h measured that one poll of a changed file can hold its handle for hundreds of milliseconds under on-access antivirus scanning or load, not only "about a millisecond"; every failure still fell inside a poll and cleared on retry once the poll returned, so the claim of 3.3h holds and only the test harness changed (see tasks.md 4.4). (C2-1) when `max_read` is configured below `max_line` (tests only), a read without a newline continues to `max_line` bytes in the same poll, so long lines are always cut at `max_line`; the defaults are unaffected.

### Rate limit and drop notice (`connectors/limiter.py`)

```python
class TokenBucket:
    def __init__(self, rate: float = 2000.0, burst: float = 2000.0, now: float = 0.0) -> None
    def take(self, n: int, now: float) -> int          # admits min(n, available) and returns it

class DropNotice:
    def __init__(self, interval: float = 10.0) -> None
    def add(self, n: int) -> None
    def due(self, now: float) -> int                   # 0, or the count to report; resets the counter and the timer
```

The tailer asks the bucket once per poll for the number of lines it may emit. The first `k` lines go out, the remaining lines of that poll are dropped and counted in `lines_dropped`. `DropNotice.due` returns a count at most once per 10 seconds; the tailer then submits one event with severity 4 and the text `connector dropped N lines (rate limit)` for the same `log_source_id`, host and app as the source's other events. That event bypasses the bucket. The signature of that text collapses `N`, so repeated notices cluster into one incident per service (R3.11).

### Offsets (`connectors/offsets.py`, `connectors/schema.py`)

```python
@dataclass(frozen=True, slots=True)
class OffsetRow:
    log_source_id: str
    dev: int; ino: int; head_len: int; head_sha: str
    offset: int; discarding: bool; updated_at: float

class OffsetStore:
    def __init__(self, store: Store, now: Callable[[], float] = time.time) -> None
    async def available(self) -> bool                       # the tail_offsets table exists
    async def load(self) -> dict[str, OffsetRow]
    async def flush(self, rows: Sequence[OffsetRow]) -> None        # one transaction, idempotent upsert
    async def prune(self, keep: frozenset[str]) -> int             # delete rows whose id is not in keep
    @staticmethod
    def drop_in_tx(tx: Tx, ids: Sequence[str]) -> None             # used by the in-transaction subscriber

def resume_state(row: OffsetRow | None) -> TailState             # row -> TailState(ident, offset, discarding) or the fresh state
def row_from_state(source_id: str, state: TailState, now: float) -> OffsetRow | None   # None when ident is None
```

Resuming is not a separate code path: `resume_state` builds an ordinary `TailState` from the row, and the first `poll_file` applies the identity rule of step 5 (same file and `size >= offset` resumes, anything else starts at the beginning of the new file, R3.5).

`prune` runs at startup and in the 30-second reconcile; it repairs a row that a flush wrote after the registry removed its source (the flush and the removal are in different transactions). It reads `SELECT log_source_id FROM tail_offsets`, computes the orphans in Python, and deletes them with one `executemany` of a literal statement (the store's SQL-literal rule).

### The tailer (`connectors/tailer.py`)

```python
@dataclass(frozen=True, slots=True)
class SourceSpec:
    source_id: str; service_id: str; path: str; format: str
    host: str | None; app: str; backfill_lines: int         # LogSource.backfill_lines (services O11), 0 to 1000

@dataclass(slots=True)
class SourceCounters:
    lines_read: int = 0; events_emitted: int = 0; lines_dropped: int = 0
    bytes_read: int = 0; unparsed: int = 0; skipped: int = 0
    last_error: str = ""; status: str = "stopped"

def build_spec(src: LogSource, service: Service | None, machine: str) -> SourceSpec

class FileTailer:
    def __init__(self, spec: SourceSpec, state: TailState, counters: SourceCounters, submit: Submit,
                 cfg: ConnectorConfig, *, machine: str, clock: Callable[[], float] = time.monotonic,
                 on_state: Callable[[str, TailState], None]) -> None
    spec: SourceSpec
    def start(self) -> asyncio.Task
    async def stop(self, graceful_timeout: float = 2.0) -> None     # sets the stop event, waits, then cancels
```

One task per source. The loop:

```
while not stop:
    result = await asyncio.to_thread(poll_file, spec.path, state, max_read=..., backfill_lines=spec.backfill_lines)
    status: "busy" polls are counted; the first busy_polls (5) in a row report "running", the sixth reports "unreadable" with the OS text;
            every other result sets status and last_error directly (cut to 200 characters)
    adopt result.start; bytes_read += result.bytes_read
    if fmt is w3c and (result.reason in {"attached", "resumed", "appeared", "rotated", "truncated"} or ctx.w3c_fields is None):
        if result.reason in {"rotated", "truncated"}: ctx = ctx with w3c_fields None
        header = await asyncio.to_thread(header_fields, spec.path)         # never raises
        if header: ctx = fmt.update(header, ctx)
    admitted = bucket.take(len(result.lines), clock())
    for i, raw in enumerate(result.lines):
        if stop.is_set(): break                       # the rest stays in the file; state is at the last emitted line
        lines_read += 1
        if i >= admitted: lines_dropped += 1; notice.add(1); state = at(raw); continue
        try:
            text = decode_line(raw); ctx = fmt.update(text, ctx); out = fmt.parse_line(text, ctx)
            if out.event is None: skipped += 1
            else:
                await submit(out.event, spec.source_id)
                events_emitted += 1
                if not out.matched: unparsed += 1        # counted only after a successful submit (review 10)
        except Exception: log warning (one per 10 s per source); lines_dropped += 1      # R1.3
        state = at(raw); on_state(spec.source_id, state)  # at(raw): offset = raw.end, discarding = raw.discarding_after
    if not stop.is_set(): adopt result.state (also for the discarding flag and mtime); on_state(...)
    if (n := notice.due(clock())): submit the summary event            # touches no counter
    delay = more -> catchup_interval | busy, running -> poll_interval | missing -> missing_retry | unreadable -> unreadable_retry
            | error -> not_regular_retry for "not a regular file", error_retry for every other error
    await sleep(delay) interruptible by the stop event
```

An unexpected exception outside the per-line guard (a bug in `poll_file`, a failing executor) sets status `error` with the exception text, waits `error_retry` (5 seconds) and continues. A task never dies by itself. The `FormatContext` is created at start from the spec (`machine`, `service_id`, `host`, `app`); its `w3c_fields` is set from `header_fields` as the loop above shows, and reset to `None` on `rotated` and `truncated`. `on_state` records the newest state in the manager, which marks the source dirty for the offset flush.

**Counter semantics (review 10).** `lines_read` counts every complete line taken from a poll. `events_emitted` counts lines whose event was submitted successfully. `lines_dropped` counts lines dropped by the rate limit and lines whose parse or submit raised. `skipped` counts lines the parser returned no event for (blank lines, comments). `unparsed` counts matched=False lines after their `submit` returned, so `unparsed <= events_emitted`. `bytes_read` is the number of bytes read from disk by `poll_file` (partial lines read again and the head reads included), so it can exceed the file size. The drop-notice summary event is submitted without touching any counter. Invariant (property 21): `lines_read == events_emitted + lines_dropped + skipped`.

### The manager (`connectors/manager.py`)

```python
class ConnectorManager:
    def __init__(self, registry: ServiceRegistry, store: Store, submit: Submit, *,
                 machine: str | None = None, config: ConnectorConfig | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None
        # registers set_known_formats, set_status_provider and the subscription; starts nothing
    async def start(self) -> None                   # loads offsets, reconciles, starts the flush and reconcile tasks
    async def aclose(self, timeout: float = 1.0) -> None    # stops tasks, final flush, within the timeout
    async def reconcile(self) -> None               # idempotent; serialized by one lock
    async def on_change(self, change: ServiceChange) -> None    # the after_commit callback: calls reconcile
    def runtime(self, log_source_id: str) -> SourceRuntime | None   # the status provider
    def drop_offsets(self, tx: Tx, change: ServiceChange) -> None  # the in-transaction subscriber
```

`reconcile` computes `desired = {s.id: build_spec(s, registry.get(s.service_id), machine) for s in registry.log_sources(enabled_only=True)}` and compares it with the running tailers. A source not desired is stopped (and, when it is no longer in `registry.log_sources()` at all, its counters and state are forgotten); a spec that differs is restarted with the same state and counters (a format or host edit does not lose the offset); a task that finished is restarted; a new spec starts with `resume_state(offsets.get(id))` or the fresh state. All stops of one reconcile run concurrently with a 2-second graceful budget.

**Disabling and re-enabling (review 17).** When a source is disabled its tailer stops, its `TailState` is replaced by the fresh state (`attach="end"`) and its counters stay (`runtime()` keeps reporting `stopped`); its offsets row is removed by the next flush. Re-enabling is therefore a new attach at the end of the file (R3.2 covers only the first attach, and an old backlog would otherwise arrive as a burst under the rate limit): lines written while the source was disabled are not emitted. The alternative, a paused offset, is O13.

**One path, one tailer (services trim T2).** `services` no longer rejects two sources with the same path. `reconcile` computes `normcase(normpath(path))` per desired source and starts only the first in registry order; the later one gets status `error` with `path already tailed by <id>` and one warning, so one file never produces duplicate events. Before `start()` and after `aclose()` the manager starts nothing, so a registry change made by a test app without a lifespan has no effect.

`runtime(log_source_id)` returns a `SourceRuntime` built from the counters of a known source: its live status while its tailer runs, `stopped` for a registered source that is disabled, `None` for an unknown id (services shows `unknown`). It is a dictionary lookup, called synchronously once per source per `GET /api/services` (R1.2, R3.13: a `missing` or `unreadable` source stays visible).

`drop_offsets(tx, change)` is the callback for both `on_service_changed` and `on_service_deleted`. For `log_source_removed` it deletes the row of `change.log_source_id`; for `service_deleted` it deletes the rows of `change.old.log_sources`; any other kind returns at once. It first checks `sqlite_master` for the table, because services' own tests build apps on a store opened without this module's migration (see "Review resolutions", M1 note); a store without the table skips the delete.

Housekeeping, two tasks started by `start()`:

| Task | Period | Work |
|---|---|---|
| offsets flush | 5 s (`flush_interval`) | `rows = [row_from_state(...) for dirty ids]`; `await offsets.flush(rows)`; on `StoreError` log one warning per minute and keep the ids dirty |
| reconcile | 30 s (`reconcile_interval`) | `await reconcile()`, then `await offsets.prune(frozenset(all registered source ids))` |

`aclose` sets the stop flag, stops every tailer (graceful, 1 second), cancels the two housekeeping tasks, and awaits one final `offsets.flush` of every dirty state, all inside `timeout`. It is called by the lifespan after the listeners and the TCP server close and before the services steps (see Wiring), so no event is ingested after the hub starts closing.

### Configuration (`connectors/config.py`)

```python
@dataclass(frozen=True, slots=True)
class ConnectorConfig:
    poll_interval: float = 0.5; catchup_interval: float = 0.1
    missing_retry: float = 5.0; unreadable_retry: float = 30.0
    error_retry: float = 5.0          # an unexpected exception in the loop or the executor
    not_regular_retry: float = 30.0   # status error "not a regular file" (directory, device, FIFO, socket)
    busy_polls: int = 5               # consecutive sharing/lock violations before the status becomes unreadable
    flush_interval: float = 5.0; reconcile_interval: float = 30.0
    max_read: int = 1 << 20; max_line: int = 8192; head_size: int = 256
    rate: float = 2000.0; burst: float = 2000.0; drop_notice_interval: float = 10.0
    stop_grace: float = 2.0

@dataclass(frozen=True, slots=True)
class TcpConfig:
    max_connections: int = 64; idle_timeout: float = 60.0; max_frame: int = 8192; read_size: int = 65536
```

Only one environment variable is new, `CHIMERICMESH_SYSLOG_TCP_PORT`. The timings are constructor arguments so tests can run in milliseconds; they are not environment settings (the requirement fixes them).

### Wiring (`api.py`, task 8.1)

Added to `create_app` after the services steps (services design, "`create_app` changes"):

1. `build_registry()` adds `connectors.register_migrations(registry)` (block 200 to 299).
2. `submit = make_submit(hub.ingest)`; `manager = ConnectorManager(registry, app.state.store, submit)`. The constructor sets the known formats, the status provider and the subscription. `app.state.connectors = manager`.
3. Lifespan, after the existing listeners start: when `start_listeners`, `app.state.syslog_tcp = await start_syslog_tcp(submit, bind, resolve_tcp_port(os.environ))`; then `await manager.start()` (tailers are not sockets, so they start whether or not `start_listeners` is true; a registry with no sources starts nothing).
4. Shutdown order and time budget (review 11): the lifespan takes `deadline = monotonic() + 5` once, when shutdown begins, and hands every step the time that remains. Steps: close the three existing listeners; `syslog_tcp.aclose(min(0.4, remaining))`; `manager.aclose(min(0.6, remaining))` (the two connector steps share a 1-second slice); the services steps (the limiter flush, at most 0.5 second; the registry has nothing to close); `hub.aclose(deadline=remaining)`; `store.close()`. Even when every earlier step uses its whole slice, `hub.aclose` keeps at least 3 seconds for its final flush, so queued events are not abandoned. A failing store or a slow client cannot make the shutdown longer than 5 seconds because every wait has its own timeout and the remaining time only shrinks.

`listeners.py` keeps `start_syslog`, `start_telnet` and `IrcServer` unchanged (R5.1, R5.2: the 4 MB receive buffer and the per-datagram line splitting stay in the same code). The reuse of `parse_syslog` for TCP means the two syslog transports parse identically.

## Data Models

### SQLite schema (migration `connectors:200`, name `connectors:0200-tail-offsets`)

Registered with `registry.register("connectors", 200, "connectors:0200-tail-offsets", (...))` by `register_migrations(registry)`; `connectors_registry()` returns `services_registry()` plus it.

```sql
CREATE TABLE tail_offsets (
  log_source_id TEXT PRIMARY KEY CHECK (length(log_source_id) BETWEEN 1 AND 30),
  dev           TEXT NOT NULL,                       -- decimal text: Windows ids can exceed a signed 64-bit integer
  ino           TEXT NOT NULL,
  head_len      INTEGER NOT NULL CHECK (head_len BETWEEN 0 AND 256),
  head_sha      TEXT NOT NULL CHECK (length(head_sha) = 64),
  byte_offset   INTEGER NOT NULL CHECK (byte_offset >= 0),
  discarding    INTEGER NOT NULL DEFAULT 0 CHECK (discarding IN (0, 1)),
  updated_at    REAL NOT NULL
);
```

No foreign key into `log_sources` on purpose (services design): a registry restored from a backup or a failed `after_commit` must not make a migration or a flush fail, and `prune` removes orphans. The table holds a hash of at most 256 bytes of the file head, never file text. The module writes no audit rows and no other table; events go through `ingest` into the store's `events` table with `source = "file"`, `log_source_id` and the tagged `service_id` (B7, B19).

### In-memory state

| Object | Lives in | Persisted? |
|---|---|---|
| `TailState` per running or disabled source | `ConnectorManager._states` | identity, offset and `discarding` through `tail_offsets`; `mtime_ns` and `attach` no |
| `SourceCounters` per source | `ConnectorManager._counters` | no (reset when the hub restarts; they are runtime counters, R1.2) |
| `TokenBucket`, `DropNotice` | the `FileTailer` | no |
| `ListenerStats` | `TcpSyslogServer` | no |

## Sequence diagrams

### File line to incident, with a rotation

```mermaid
sequenceDiagram
    participant A as App (appends, rotates)
    participant T as FileTailer (loop thread)
    participant P as poll_file (executor thread)
    participant S as submit
    participant H as Hub.ingest (tagger first)
    participant O as OffsetStore (store writer)

    T->>P: to_thread(poll_file(path, state))
    P->>P: stat; unchanged? return without opening
    P->>P: open, read head, decide start (resume / rotated / truncated), read <= 1 MiB, close
    P-->>T: PollResult(start, state, lines)
    T->>T: adopt start; take bucket tokens
    loop each complete line
        T->>T: decode_line, fmt.update, fmt.parse_line
        T->>S: submit(event, log_source_id)
        S->>H: ingest(event with log_source_id)
        H->>H: tagger assigns the source's service; id; ring; tracker; row
        H-->>S: tagged Event
        T->>T: state = offset after this line
    end
    A->>A: rename a.log -> a.log.1, create a.log
    T->>P: next poll
    P->>P: stat shows a different inode, open, fingerprint differs -> rotated, start 0
    P-->>T: reason "rotated", lines of the new file
    T->>O: every 5 s: flush dirty offsets (one transaction)
```

### Registry change to tailer, including a failing subscriber and a skipped callback

```mermaid
sequenceDiagram
    participant U as REST client
    participant R as ServiceRegistry (services)
    participant W as store-writer thread
    participant M as ConnectorManager (loop thread)
    participant F as FileTailer

    U->>R: add_log_source / edit / remove
    R->>W: transact(plan)
    W->>W: change rows, audit row
    W->>M: drop_offsets(tx, change) in the same transaction (removal only)
    alt a later subscriber raises
        W->>W: ROLLBACK (registry rows, audit row and the offsets delete)
        W-->>U: 500 naming the subscriber; no tailer changed
    else all return
        W->>W: COMMIT
        R->>R: apply delta, version += 1
        R->>M: after_commit(change)
        M->>M: reconcile(): desired specs vs running tailers
        M->>F: start / restart (same state) / stop (graceful)
    end
    Note over M: if after_commit never ran or raised, the 30 s timer calls reconcile() and converges to the same set
```

### TCP syslog connection

```mermaid
sequenceDiagram
    participant C as Sender (rsyslog)
    participant L as TcpSyslogServer
    participant D as FrameDecoder
    participant S as submit

    C->>L: connect
    alt 64 connections already open
        L-->>C: close; refused += 1
    else accepted
        C->>L: bytes (any chunking)
        L->>D: feed(bytes)
        D->>D: detect_mode from the first bytes: octet "<1-4 digits><space>" else LF
        D-->>L: frames in order, or FrameError (length not a number, > 8192, zero)
        alt FrameError
            L->>L: dropped += 1, framing_errors += 1, close this connection only
        else frames
            L->>L: decode utf-8 with replacement; parse_syslog(text, default_host=peer)
            L->>S: submit(event)
        end
        Note over L: idle 60 s -> close; no bytes are ever written back
    end
```

## Error Handling

| Operation | Failure | Recoverable? | What happens | Logged |
|---|---|---|---|---|
| Parser gets any text | bug (should be impossible, property 1) | the line only | the tailer's per-line guard counts `lines_dropped`, continues | warning, one per 10 s per source |
| `submit` raises (hub or store error) | hub bug | the line only | same as above | same |
| Line over 8192 bytes | oversized | Yes | emitted cut with ` [truncated]`; rest skipped to the newline | none |
| Bytes not UTF-8 | binary or other encoding | Yes | replacement characters; NUL and controls removed | none |
| Line does not match the declared format | wrong format chosen | Yes | `plain` rules, counted `unparsed` | none |
| File missing | not created yet, or rotated away | Yes | status `missing`, retry every 5 s; read from its start when it appears | one info line on each status change |
| File unreadable (`PermissionError` and other `OSError`) | permission | Yes | status `unreadable` with the OS text, retry every 30 s, never elevates | one warning on each change |
| Sharing or lock violation (WinError 32 or 33, `EBUSY`) | a writer or rotator holds the file | Yes | status `busy` internally, reported `running`; retry at the poll interval; after 5 in a row `unreadable` | none until the fifth |
| Path with a NUL or that cannot be encoded (`ValueError`, `UnicodeError`) | bad registry data | Yes | status `error`, `invalid path`, retry every `error_retry` | one warning on each change |
| Path is a directory, device, FIFO or socket (stat or fstat) | wrong path | Yes | status `error`, `not a regular file`, retry every 30 s; never opened | one warning on each change |
| Rotation (identity or fingerprint changes) | normal | Yes | read the new file from its start | info |
| Copy-truncate | normal | Yes | read from its start | info |
| Executor or `poll_file` raises | bug | Yes | status `error`, retry after 5 s, the task continues | error with the cause, one per minute |
| Rate limit exceeded | busy file | Yes | surplus lines dropped and counted; one summary event per 10 s | none (the summary event is the notice) |
| Offsets flush fails (`StoreError`) | store down | Yes | ids stay dirty, the next tick retries | warning, one per minute |
| Offsets table missing | store opened without this migration | Yes | offsets stay in memory only; tailing works | one warning at `start()` |
| `drop_offsets` raises | bug | Not retried | the registry change rolls back and the caller gets 500 naming `connectors` | error |
| `after_commit` raises or exceeds 5 s | bug | Yes | the change stays; the 30-second reconcile converges | logged by services |
| Reconcile cannot stop a tailer in 2 s | blocked task | Yes | the task is cancelled; a thread blocked in a read finishes in the background and its result is ignored | warning |
| TCP: frame length invalid, zero, or above 8192 | bad sender | That connection | closed, `dropped` and `framing_errors` +1, other connections continue | warning, one per 10 s |
| TCP: more than 64 connections | flood | Yes | new connection closed, `refused` +1 | info, one per minute |
| TCP: idle 60 s, or the peer resets | normal | Yes | connection closed | none |
| TCP: the port is in use at start | another process | Yes | one warning naming the port; the hub starts without the TCP listener (B15) | warning |
| `CHIMERICMESH_SYSLOG_TCP_PORT` not an integer or out of range | operator typo | Yes | default 5514, one warning | warning |
| Shutdown takes too long | slow disk or store | Yes | every wait has its own timeout; unflushed offsets are repeated lines at the next start | warning |

Input validation:

| Input | Rule | On failure |
|---|---|---|
| File bytes | untrusted; read as at most 1 MiB per poll, 8192 bytes per line | cut, replaced, counted |
| `LogSource.path`, `format`, `host`, `enabled` | validated by `services` at registration; this module trusts the registry for shape and re-checks the file type at every open | `error` status |
| TCP bytes | untrusted; frame at most 8192 bytes, header at most 4 digits | close the connection |
| `CHIMERICMESH_SYSLOG_TCP_PORT` | integer 0 to 65535 | warning, default |
| `CHIMERICMESH_BIND` | unchanged (services warns when it is not loopback) | none |
| `tail_offsets` row | `dev`, `ino` parsed with `int()`, `head_sha` 64 hex characters; a row that does not parse is ignored with one warning and the source starts as new | warning |

Invariant ownership: file type and identity rules live in `tailread.py` (one function, called by the tailer only). Line framing and truncation live in `tailread.py` (files) and `framing.py` (TCP). Severity and message shape live in `parsers/*`. Host and app naming live in `logctx.py`, so a change of the incident-key rule is one edit. Service assignment lives in `services` (the tagger). Which tailers run is decided only by `reconcile`. Offsets are written only by `OffsetStore`.

## Security

- **Untrusted input.** File contents and network bytes are data. Parsers are pure and total; nothing read from a log is passed to `eval`, `exec`, `compile`, `__import__`, a shell, or a path operation (R6.3). A static test scans `parsers/*` and `connectors/*` for those names and for the `subprocess`, `importlib`, `pickle`, `marshal` imports.
- **Only registered paths.** The tailer opens `spec.path` and nothing else. The path comes from the registry, never from event text; `discovery` proposals go through the same registry and need the user to confirm. Symlinks at the registered path are followed on purpose (`/var/log/nginx/access.log` is often one); the stat and fstat checks guarantee a regular file.
- **Special files cannot hang the hub.** A FIFO would block `open()` on POSIX; the code stats first and opens with `O_NONBLOCK`. Devices and sockets are refused. A directory is refused. The poll runs in a worker thread, so a slow disk cannot stall the loop; a hung read blocks one executor thread, not the hub.
- **No exfiltration.** File text goes to `ingest` (the hub's ring, the local SQLite file, and the hub's own IRC announcement and webhook of an incident) and nowhere else. The TCP server never writes to its clients. The import allow-list per file (above) keeps HTTP, mail and process libraries out of the module. The IRC announcement is the baseline injection-safe formatter (R3.2); this module writes to no IRC socket.
- **Secrets in logs.** `redact` replaces `password=`, `token=`, `Authorization:` and similar values in `message` and `raw` before the event leaves the parser; NCSA and W3C messages and their `raw` never contain the request query string (the `raw` construction above). A parser property tests that the secret values from generated lines never appear in the result. This reduces what reaches the database, the ring, announcements and the MCP tools; it is a heuristic, not a guarantee, and the README says so.
- **Resource bounds.** 1 MiB per poll, 8192 per line and per frame, 64 connections, 60-second idle, 2000 lines per second per source, 200 sources, 100 ms catch-up delay (so a busy file is read at most about 10 MiB per second per source). Per-connection TCP rate limiting is not implemented (UDP has none either); the store's bounded event queue is the backstop.
- **Bind address.** The TCP listener uses `CHIMERICMESH_BIND` (default `127.0.0.1`), never `0.0.0.0` by default.
- **No process.** This module starts no process (map, refinement 4).
- **Fingerprint storage.** The database stores a SHA-256 of at most 256 bytes of the file head and numeric ids, not text.

## Correctness Properties

Each is a universal statement for Hypothesis. A property sub-task in `tasks.md` is required unless its number carries `*`. Required: 1, 2, 3, 6, 7, 9, 10, 11, 12, 13, 14, 15 (their criteria carry the `[PBT]` tag, and Success Criterion 4 names 1).

1. **Parsers are total.** *For any* text (control characters, NUL, lone surrogates, very long, multi-line) and any `FormatContext`, `update` then `parse_line` of each of the five formats never raises; when an event is returned its severity is within 0 to 7, its `message` is at most 4096 characters, its `raw` at most 1024, its `source` is `file`, and its `host` and `app` are non-empty. **Validates: Requirements 1.5, 4.1, 4.9, 6.3**
2. **NCSA status mapping and message.** *For any* generated Common or Combined line with status `s`, severity is `http_severity(s)` (5xx 3; 408 and 429 4; other 4xx 5; the rest 6), and the message is `<METHOD> <path> -> <s>` with no `?`, `#` or query text from the request. **Validates: Requirements 4.3**
3. **W3C column mapping.** *For any* generated `#Fields:` list and data row, the fields are read by name and not by position, severity follows `sc-status` and is at most 4 when `sc-win32-status` is non-zero, `#` lines and blanks return `None`, and a data line before any `#Fields:` or with the wrong column count is parsed by the `plain` rules with `matched=False`. **Validates: Requirements 4.4**
4. **xferlog fields.** *For any* generated 14-field record (file names with spaces included), completion `c` gives severity 6, `i` gives 4, and direction and file name appear in the message. **Validates: Requirements 4.5**
5. **Syslog-text hints.** *For any* line without a PRI, `status=deferred` gives severity at most 4, `status=bounced` or `status=rejected` at most 3, and no hint ever makes the severity less severe than `parse_syslog` returned. **Validates: Requirements 4.2**
6. **Redaction.** *For any* text containing `password=V`, `token=V`, `Authorization: Scheme V` with `V` a generated secret of at least 4 characters, neither `V` nor the query string (for NCSA also the Referer's query string) appears in the message or `raw` of an NCSA, W3C or plain event, `redact` is idempotent, and the output never exceeds the limits of property 1. **Validates: Requirements 4.7**
7. **Host and app naming.** *For any* machine name, service id (or none or `unassigned`), source host, line host, client IP and format, the event host equals `source_host` when set, otherwise `<machine>/<service-id>` (just `<machine>` without a real service); for `syslog-text` with a line host it is `<line host>/<service-id>`; two NCSA lines that differ only in client address have equal hosts and equal signatures; the host is at most 191 characters; `app` follows rule 4.10. **Validates: Requirements 4.10, 4.11, 4.12**
8. **Unparsed fallback.** *For any* line that does not match the declared format, the result has `matched=False`, equals the `plain` result for that line, and severity is `infer_severity(line, 6)`; for any line generated from the format's own grammar `matched` is true. **Validates: Requirements 4.8**
9. **Framing is chunking-invariant.** *For any* list of valid messages encoded with octet counting (or LF) and any partition of the byte stream into chunks (including one byte at a time), `FrameDecoder.feed` yields the same frames in the same order, and `finish` completes an unterminated LF line. **Validates: Requirements 2.2, 2.3**
10. **Framing detection.** *For any* first bytes, the mode is `octet` if and only if they start with 1 to 4 digits and a space; any other start, including an ISO timestamp (`2026-11-18T...`) and a first frame whose length has five or more digits (`102400 ...`), selects LF and the connection is not closed. **Validates: Requirements 2.4**
11. **Invalid frames close only that connection.** *For any* stream with an invalid or oversized header (not a number, more than 4 digits, zero, above 8192), the decoder yields the frames before it, one `FrameError`, and nothing after it; the server counts exactly one drop and keeps serving other connections. **Validates: Requirements 2.5**
12. **Decoding is total and clean.** *For any* bytes, decoding a TCP frame or a file line never raises, yields a valid `str`, and file lines contain no NUL and no control character except tab. **Validates: Requirements 2.8, 3.10**
13. **Tail without rotation is exactly-once and ordered.** *For any* sequence of appends (whole lines, writes split at arbitrary byte positions, CRLF endings) interleaved with polls, the concatenation of emitted lines equals the complete lines appended so far, once each and in order; a line without its newline is never emitted; two polls with no change emit nothing. **Validates: Requirements 3.1, 3.3d, 3.4**
14. **Rotations lose only what was unread.** *For any* interleaving of appends, polls, rename-and-recreate, copy-truncate and delete-and-recreate (with unique line ids), the emitted lines are exactly the appended lines minus those that were unread at the last poll of their file generation when a rotation of that generation happened (appended after that poll, or left behind because it stopped at the 1 MiB limit); none is duplicated; order is preserved within each file generation; copy-truncate followed by growth past the old offset before the next poll is still detected. **Validates: Requirements 3.3a, 3.3b, 3.3c, 3.3d, 3.3e, 3.3f, 3.3g**
15. **Overlong lines are bounded.** *For any* append of bytes with no newline in chunks, no `RawLine.data` exceeds 8192 bytes, one overlong line produces at most one truncated line, the remainder up to the next newline is skipped, and the next complete line is emitted normally; no poll reads more than 1 MiB. **Validates: Requirements 3.4, 3.12, 6.1**
16. **Rate limit conservation.** *For any* arrival sequence of `(time, lines)`, admitted lines never exceed `burst + rate * elapsed`, admitted plus dropped equals arrived, and `DropNotice.due` returns a count at most once per interval and the counts sum to the drops. **Validates: Requirements 3.11**
17. **Offsets round trip and resume.** *For any* `TailState` with an identity, `row_from_state` then `resume_state` returns the same identity, offset and `discarding`, through the real store; a tailer stopped gracefully and a new tailer started from the stored row together emit the same lines, once each, as one tailer that never stopped; a row whose identity no longer matches starts at the beginning of the new file. **Validates: Requirements 3.5**
18. **Reconcile converges.** *For any* sequence of registry operations (add, edit format or host, enable, disable, remove, delete service) in which `after_commit` is skipped for a random subset, one `reconcile()` leaves exactly one running tailer per enabled registered source with the registry's current spec, none for a disabled or removed one, and the second call changes nothing. **Validates: Requirements 1.6, 1.4**
19. **Subscriber atomicity for offsets.** *For any* removal of a log source or deletion of a service followed by a later in-transaction subscriber that raises, the `tail_offsets` rows are unchanged and no tailer changed; when all subscribers return, the rows of the removed sources are gone. **Validates: Requirements 1.6, 3.5**
20. **Submit contract.** *For any* event and optional `log_source_id`, `submit` calls `ingest` once, the event it passes equals the input except for `log_source_id`, the input is unchanged, and the returned value is what `ingest` returned (or the sent event when `ingest` returned `None`). **Validates: Requirements 1.1**
21. **Counter consistency.** *For any* file content, parse outcomes and rate limit, after all lines of a run are processed `lines_read == events_emitted + lines_dropped + skipped`, `unparsed <= events_emitted` (a line is counted `unparsed` only after its `submit` returned), `bytes_read` equals the sum of the `bytes_read` of the polls (bytes read from disk, so it can exceed the file size), the drop-notice summary event changes no counter, and `last_error` is at most 200 characters. **Validates: Requirements 1.2, 1.3**

## Testing Strategy

**Unit tests (pytest, example-based), `tests/unit/`.** `test_logfmt_context.py` (R1.5, R4.7, R4.9 to R4.12 helpers), `test_logfmt_syslogtext.py` (R4.2, R4.6, R4.8), `test_logfmt_ncsa.py` (R4.3, R4.11), `test_logfmt_w3c.py` (R4.4), `test_logfmt_xferlog.py` (R4.5), `test_logfmt_registry.py` (R4.1, R6.3 static scan), `test_connectors_config.py` (R2.1 port rules), `test_connectors_submit.py` (R1.1, 1.1a to 1.1e), `test_tcp_framing.py` (R2.2 to R2.5, R2.8), `test_tcp_server.py` (R2.5 to R2.7, Success Criterion 3), `test_tcp_lifecycle.py` (R2.1, B15), `test_tailread_basic.py` (R3.1, R3.4, R3.10, R3.12), `test_tailread_rotation.py` (R3.3 with 3.3a to 3.3g, R3.6 to R3.9), `test_tailread_attach.py` (R3.2, backfill), `test_tailread_windows.py` (NTFS identity, R3.8), `test_connectors_limiter.py` (R3.11), `test_connectors_schema.py`, `test_connectors_offsets.py` (R3.5), `test_tailer.py` (R1.2, R1.3, R3.1, R3.6, R3.7, R3.11, R3.13), `test_connectors_manager.py` (R1.4, R1.6), `test_connectors_subscribe.py` (R1.6), `test_connectors_housekeeping.py` (R3.5 timing, shutdown), `test_connectors_app.py` (`create_app` contract), `test_connectors_both_hubs.py` (1.1e, M2), `test_connectors_integration.py` (Success Criteria 1 to 5, including R5.2), `test_readme_connectors.py`.

**Fixtures.** The store and services fixtures (`store`, `mem_store`, `FakeClock`, `svc_store`, `svc_mem_store`, `make_registry`, the autouse home isolation) are reused. Task 7.1 appends `conn_config` (millisecond timings), `conn_registry` (a store opened with `connectors_registry()`) and `make_manager` to `tests/conftest.py`; no existing fixture changes. File tests use `tmp_path`. Async tests use `asyncio.run` inside plain test functions (steering).

**Both hubs (M2).** Tailer, manager, TCP and integration tests that ingest through a hub run twice: against `Hub()` with the private `:memory:` store, and against `Hub(store=<file store>)`. A test that lists the tests of both parametrizations lives in `test_connectors_both_hubs.py` (1.1e).

**Windows and rotation tests.** `test_tailread_windows.py` repeats the table of measurements in the Overview with real files (skipped off Windows, except the rename-while-closed check, which runs everywhere): inode kept on rename, new inode on recreate, same inode and a smaller size on copy-truncate, rename of the file between two polls succeeds, and `poll_file` leaves no open handle (a rename right after it returns succeeds). A rename-race test runs a thread that renames the file away and back in a loop while the main thread polls it continuously: an idle file gives zero failures, a file that is being appended to may give WinError 32 failures that are counted and printed and every one succeeds on a retry within 3 attempts (review 8). One manual check (task 8.5) runs an IIS-style rotator against a W3C file.

**Property tests (Hypothesis, `tests/pbt/`).** Properties 1 to 21 in `test_logfmt_properties.py`, `test_logfmt_mapping_properties.py`, `test_logfmt_more_properties.py`, `test_tcp_framing_properties.py`, `test_tail_properties.py`, `test_tail_rotation_properties.py`, `test_connectors_limiter_properties.py`, `test_connectors_offsets_properties.py`, `test_connectors_manager_properties.py`. The rotation and tail properties use real files under `tmp_path` and call `poll_file` directly (synchronous, no asyncio), with `deadline=None` and unique line ids so every line is identifiable. The parser properties use no filesystem.

**Existing tests that must keep passing unchanged.** Every file in `tests/unit` and `tests/pbt`, in particular `test_parsers.py` and `test_real_log_regressions.py` (`parse_syslog` is called, not changed), `test_irc_commands.py` and `test_web_terminal.py` (the listeners and `/api/terminal` are not touched), `test_stats_counters.py` (the `file` source adds a counter key only when a file event arrives), `test_diagnostics_api.py`, `test_legacy_app_wiring.py`, `test_replay_properties.py`, `test_guard_hook.py`, `test_kiro_assets.py`, plus the `store` and `services` tests merged before this module. This module edits no existing test. It edits one file created by the `store` module (`tests/unit/test_dependency_direction.py`, new rows only, task 2.1) and appends fixtures to `tests/conftest.py` (task 7.1).

**Benchmarks (tolerant, skipped with `CHIMERICMESH_SKIP_BENCH=1`).** `poll_file` of an unchanged file under 1 ms (one stat); 100 000 appended NCSA lines parsed and submitted to a no-op `ingest` in under 5 seconds; 200 idle sources polled for 3 seconds keep the loop's maximum delay under 100 ms.

**Manual.** Success Criterion 6 on Windows (IIS-style W3C and Apache-style files, another process appending and rotating); one real rsyslog or `logger --tcp` check of TCP syslog if available.

## Needs from store (the store design is not changed; flagged for the user)

1. **Dependency-test shape.** Same item as services (its item 3): a prefix table (`"connectors/": "connectors"`) and a per-file `THIRD_PARTY` mapping make this module add rows only. If the store keeps an exhaustive list, task 2.1 adds rows to it. No store code change.
2. **`Event.log_source_id` and `Hub.ingest` returning the tagged `Event`.** Both are in the store design (field order after `ts`, "Hub changes"). This module depends on them; nothing to add. If `ingest` still returns `None` when this module is built, `submit` returns the event it sent (stated in the `submit` contract), and only the L1 assertion of task 2.3 waits for the store merge.
3. **`Source` literal.** `events.py` gets `"file"` in `Source` (one line, B7). The store module also edits `events.py` (new fields), so the two edits touch different lines; if both branches are open at once, rebase the one-line edit.
4. **No new T10 call site.** Offsets are loaded with `await store.read`, so no synchronous store call is added and the T10 sentence needs no change.
5. **Recommended, optional:** a store helper `Tx.table_exists(name)` (one `sqlite_master` query). This module writes the query itself (`drop_offsets`, `OffsetStore.available`) to avoid depending on it.

## Needs from services (conflicts resolved in round 1; the services design was edited, not the store design)

- **N1. `backfill_lines` (resolved).** Connectors R3.2 needs a per-source `backfill_lines` (0 to 1000, default 0). The services design now stores and validates it on `LogSource`, `LogSourceFields` and `LogSourcePatch`, in its `log_sources` DDL and in the two write routes (services O11, about 0.1 day, in its estimate). `build_spec` reads `src.backfill_lines`; the placeholder `getattr` is gone. If the user answers services O11 (O2 here) with "defer", drop the field, `poll_file`'s backfill and tasks 4.3 (backfill part) and 7.1 (clamp).
- **N2. One path, one tailer.** Services dropped its duplicate-path rule (its trim T2). `reconcile` guards against it (see "The manager").
- **N3. Removed services seams.** `registry.version`, `Subscription.cancel`, `registry.start()` and `registry.aclose()` no longer exist. This module used none of them; the lifespan text and tasks 8.1 and 7.2 name the real shutdown steps.
- **N4. Services T5 premise.** Its trim option assumes `connectors` edits `listeners.py`. This design does not touch it (O1 is accepted, so task 8.6 is not needed), so the premise is false, not harmful.
- **N5. Services task 7.4.** Its shutdown-order test must be re-run after task 8.1 here, since `connectors` inserts two steps before the services steps.
- **N6. Partial-migration stores.** Services' own tests build apps on a store without the connectors migration. The manager tolerates a missing `tail_offsets` table, so those tests keep passing.

## Open items for the user

Every item below is resolved: the user accepted each recommended answer on 2026-11-18, and the design is built on those answers.

- **O1. Where `submit` is used.** Resolved (accepted 2026-11-18): `submit` is used by the connector-owned intakes (file tailer, TCP syslog); the existing listeners, `/api/ingest` and `/api/demo/storm` keep calling `hub.ingest` and are tagged by the same hook (B21 holds, `listeners.py` stays untouched). The conditional task 8.6 is not needed.
- **O2. `backfill_lines`.** Resolved (accepted 2026-11-18): `services` stores the field (N1); backfill is kept.
- **O3. `app` for `syslog-text`.** Resolved (accepted 2026-11-18): the line's `app` wins for `syslog-text` (R4.12 carves out only the host), and R4.10 applies when the line has no app.
- **O4. Offsets after a crash.** Resolved (accepted 2026-11-18): offsets are written at least every 5 seconds, so after a crash up to 5 seconds of lines are read again (duplicates), and a few hundred milliseconds of submitted but unwritten events can be lost (the store's event queue can be behind the offset flush). After a graceful stop there are neither. R3.3 holds while the tailer runs and across a graceful restart. The README says so.
- **O5. A partial line left at a rotation is lost.** Resolved (accepted 2026-11-18): accepted, same bound as R3.3. A line whose newline had not arrived when the file was renamed stays in the old file, which the tailer no longer reads.
- **O6. IIS writes a new file name per day.** Resolved (accepted 2026-11-18): documented in the README now; a date-token path belongs to the next spec; the Windows demo uses a rotator that renames a stable name.
- **O7. Where the TCP counters are shown.** Resolved (accepted 2026-11-18): keep `ListenerStats` on `app.state.syslog_tcp`, log a changed summary once a minute, and let `dashboard` surface it later.
- **O8. Leniencies beyond the requirement text.** Resolved (accepted 2026-11-18): keep all of them. Octet framing tolerates CR and LF between frames; a multi-line octet frame becomes one event with spaces; `syslog-text` also accepts the ISO 8601 form; `redact` runs on every file format; a new file with the same inode but a different head is treated as rotated. Task 8.5 lists them.
- **O9. Effort and scope.** Resolved (accepted 2026-11-18): the full scope stays and no trim is applied, including the contingency cuts (TCP syslog, W3C and xferlog). Measured velocity decided it: the store implementation finished 6 of 11 tasks in about 50 minutes, so the estimates in `tasks.md` are not a schedule risk.
- **O10. Success Criterion 3 and the first frame.** Resolved (accepted 2026-11-18), Option A: test the close with a connection that is already in octet mode and with a four-digit first header, and pin the fresh-connection behaviour (a first `102400 ...` frame is LF-framed under R2.4) with its own test. R2.4 text is unchanged.
- **O11. "A rename is never blocked".** Resolved (accepted 2026-11-18): reword Success Criterion 2 in the README to "never held between polls; a rotator that retries once succeeds" and keep the 500 ms loss bound.
- **O12. File time stamps.** Resolved (accepted 2026-11-18): `ts` is the ingest time for every file format in V1 (see "Event time").
- **O13. Re-enabling a source.** Resolved (accepted 2026-11-18): a new attach at the end of the file.

## Review resolutions

| Finding | Decision | Why |
|---|---|---|
| M2 (data seam for the in-memory `Hub`) | `connectors` takes one injected `ingest` callable and imports no `hub`. `submit` calls it; the same tailer, listener and parser tests run against a plain `Hub()` (private `:memory:` store) and a store-backed hub (criterion 1.1e, task 8.2). The registry, store and status provider are the only other inputs. | R1.1 said `submit` calls `hub.ingest`; the dependency rule forbids the import. |
| L1 (`Event` is frozen; `submit` returns the tagged event) | `submit` uses `dataclasses.replace` for `log_source_id` and returns what `ingest` returns, which is the tagged event with its `id` (store design). Criteria 1.1a and 1.1b test it. | A frozen event cannot be assigned; the store design already returns the tagged event. |
| L8 (compound criteria; R3.3 first) | R3.3 split into 3.3a to 3.3g (one condition each, plus one design addition, 3.3g); R1.1 into 1.1a to 1.1e. Tests are named by those ids. `requirements.md` renumbering is backlogged to the next revision. | This phase edits only `design.md` and `tasks.md`. |
| NTFS rotation identity (test item from Open Question 2) | Measured on the development machine and recorded in the Overview: the inode is kept on rename, new on recreate, same on copy-truncate with a smaller size; a file held open by Python cannot be renamed (WinError 32). Decision: path-based open per poll, identity `(dev, ino)` plus a head fingerprint (the fingerprint also covers inode reuse on ext4 and quick regrowth after copy-truncate). Task 4.4 repeats the measurements as an automated Windows test. | The requirement asked for a design-phase test; the numbers support the accepted decision. |
| B15 (TCP port in use) | `start()` returns `False` and logs one warning naming the port; the hub starts without the TCP listener. | R2.1. |
| B7, B19, B21 | `"file"` added to `Source`; events carry `log_source_id`; the existing ingest routes are untouched and tagged by the hook. | Additive; same behaviour for existing callers. |
| M3 (threading, as it concerns this module) | Section "Threads and transactions": `poll_file` on executor threads, the in-transaction subscriber on the writer thread with `tx` only, everything else on the loop thread. | Follows store T1, T3, T7. |
| M1 (dependency table) | Rows added for `connectors/*`; `parsers/*` files are already `core`; per-file import allow-list for `os`, `socket`, `subprocess`-like names. Task 2.1 edits the dependency test. | New files need a group or the test fails by design. |
| Services seam: tolerate a store without this module's migration | `drop_offsets` and `OffsetStore.available` check for the table; production `build_registry()` always includes it. | Services' own tests build apps on `services_registry()` only; the tailer must not make their registry writes fail. |
| Services O6 (tagging moved into the hub) | Accepted (O1 here). | Keeps `services` usable alone and covers every ingest path. |

### Round 1 review resolutions (review of `design.md` and `tasks.md`; `requirements.md`, the map and the store design unchanged)

| Finding | Decision | Where |
|---|---|---|
| 1 (HIGH) | Option A. The close test follows a valid octet frame or a four-digit first header; a fresh-connection `102400 ...` frame is pinned as LF. | "What a 100 KB frame test means", O10, tasks 3.1, 3.3, 8.3 |
| 2 (HIGH) | `header_fields(path)` feeds `update_w3c` after attach, resume, appearance, rotation and truncation; a manager restart rebuilds the context and re-reads it. | "W3C header", tailer loop, tasks 4.1, 7.1 |
| 3 | R1.1 and R5.1 are met through the hub tagger for the existing listeners; the departure is stated and O1 is accepted; task 8.6 is not needed. | `submit` section, O1, task 8.6 |
| 4 | Services now stores `backfill_lines`; this module reads it directly. | N1, O2, task 7.1 |
| 5 | Optional property tasks stay uncounted; additions are priced; the remaining overrun is a user decision. | O9, tasks "Effort estimate" |
| 6 | `raw` is built from the line with the query cut (NCSA request target and Referer; W3C `cs-uri-query` replaced by `-`), then redacted. | "How `raw` is built" |
| 7 | `syslogtext.py` imports `_RFC3164` and `_RFC5424` and adds the ISO pattern; `matched` and `app` come from the matched pattern. | formats table, task 1.2 |
| 8 | The failure window is stated; a rename-race test measures it; Success Criterion 2 is reworded in the README. | Overview, evidence table, 3.3h, O11, task 4.4 |
| 9 | `busy` status for WinError 32 and 33 and `EBUSY`; `ValueError` and `UnicodeError` become `error`. | step 1 of `poll_file`, tasks 4.1, 4.2 |
| 10 | Counter semantics defined; `unparsed` counts after a successful submit; the summary event touches no counter; Property 21 reworded. | "Counter semantics", Property 21 |
| 11 | One deadline at shutdown start; TCP 0.4 s and manager 0.6 s of a 1-second slice; `hub.aclose` keeps at least 3 s. | Wiring step 4, task 8.1 |
| 12 | `Authorization: <redacted>` is the header form everywhere. | task 1.1 |
| 13 | `not_regular_retry` (30 s) next to `error_retry` (5 s); `busy_polls`. | `ConnectorConfig`, tasks 2.2, 7.1 |
| 14 | The scan flags a bare-name call of `eval`, `exec`, `compile`, `__import__` or `builtins.<name>`; `re.compile` is allowed. | group table, task 1.6 |
| 15 | `test_listeners_unchanged.py` removed from the strategy; R5.2 is in task 8.3. | Testing strategy |
| 16 | `ts` is the ingest time for all file formats in V1. | "Event time", O12 |
| 17 | Re-enable is a new attach at the end of the file. | "Disabling and re-enabling", O13, task 7.2 |
| 18 | `RawLine.discarding_after` and `at(raw)` carry the flag. | tailread types, tasks 4.1, 7.1 |
| 19 | Crash wording corrected (repeats, and a few hundred ms of events can be lost). | Overview, O4, task 7.4 |
| 20 | The loss bound includes an unread backlog; Property 14 and its oracle follow. | 3.3f, Property 14, task 4.6 |
| 21 | `conn_registry` named in task 8.2. | task 8.2 |
