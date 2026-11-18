# Requirements: discovery (V1)

Capability map: [docs/v1-capability-map.md](../../../docs/v1-capability-map.md) (module `discovery`, depends on: `services` only; the valid format names come from the registry's injected `known_formats` (`services` R3.1), so `discovery` imports no `connectors` code; effort 1.25 days). Together with `chimericmesh/fsperms.py` (`store` R1.5), this module is the only place allowed to start a process (allow-list test in `agent-loop` R4.10).

## Baseline

[legacy-mesh](../legacy-mesh/requirements.md) R1 (ingestion). Nothing in the baseline scans the machine; this module is new.

## Introduction

A developer should not have to know where Apache, IIS, vsftpd or Postfix write their logs. Discovery looks at what is running on the local machine, matches it against a built-in catalog of well-known legacy apps, and suggests services with their usual log paths and formats. The user reviews and confirms each suggestion. Nothing is watched until they do, and the user can always add or edit paths by hand (decision D2).

## Objective

One click (opt-in) turns "what runs on this machine" into suggested services, each with candidate log paths per OS and the right format, without reading any log content, without admin rights and without network access.

Threat model: a scan reveals what software the machine runs. That is sensitive, so the scan only starts on an explicit privileged request, results stay on the machine, and no MCP tool can start a scan or read the results. The catalog is data shipped with the hub, not fetched. Process names and file names are untrusted text (a process can be called anything).

## Requirements

### 1. Opt-in scan
**User story:** As a developer, I want discovery to run only when I ask, so that the hub never inspects my machine on its own.

1. THE SYSTEM SHALL NOT run any scan at startup, on a timer, or as a side effect of another request. [Auto]
2. WHEN the user calls `POST /api/discovery/scan` with a valid admin token (B4), THE SYSTEM SHALL start one scan and return 202 with a scan id. IF the token is missing, THEN THE SYSTEM SHALL return 401 and run nothing. [Auto]
3. WHILE a scan is running, THE SYSTEM SHALL reject another start with 409. [Auto]
4. THE SYSTEM SHALL expose `GET /api/discovery/scans/{id}` with `state` (`running`, `done`, `failed`), `started_at`, `finished_at`, `limited` (true when some process details were not visible), `os`, and the suggestions. [Auto]
5. THE SYSTEM SHALL NOT expose scans or suggestions through any MCP tool. [Auto: tool list test]
6. THE SYSTEM SHALL write one audit row per scan (actor, scan id, counts only). [Auto]

### 2. What the scan may do
**User story:** As a developer, I want a guarantee about what the scan touches, so that I can trust it on my work machine.

1. THE SYSTEM SHALL gather processes and listening ports only with the standard OS tools, run with a fixed argument list (never through a shell), a 10 second timeout, and output capped at 2 MB: Linux `/proc/<pid>/comm` and `ss -ltnup` (fallback `netstat -ltnup`); Windows `tasklist /fo csv /nh` and `netstat -ano`; macOS `ps -axo pid=,comm=` and `lsof -nP -iTCP -sTCP:LISTEN -iUDP`. [Auto: command construction test with a fake runner]
2. THE SYSTEM SHALL NOT open, read, hash or list the contents of any log file during a scan. It MAY call `os.stat`, `os.path.exists` and `os.access` on candidate paths, and list a directory's entry names to expand a wildcard (at most 200 names per pattern). [Auto: test with `open` patched to fail on candidate paths]
3. THE SYSTEM SHALL NOT make any network connection during a scan and SHALL NOT send scan data off the machine. [Auto: test with `socket` patched to fail]
4. THE SYSTEM SHALL run with the user's normal rights. IF an OS tool or process detail is denied, THEN THE SYSTEM SHALL set `limited: true`, continue with what is visible, and SHALL NOT prompt for elevation or retry with other credentials. [Auto]
5. IF an OS tool is not installed or times out, THEN THE SYSTEM SHALL record which tool failed, continue with the remaining tools, and finish with `state: done` and `limited: true`. IF every tool fails, THEN THE SYSTEM SHALL finish with `state: failed` and a reason. [Auto]
6. THE SYSTEM SHALL cap a scan at 30 seconds in total. [Auto]
7. THE SYSTEM SHALL NOT keep the raw process list or command lines. It stores only the matched evidence listed in 4.3. [Auto]

### 3. Catalog of legacy apps
**User story:** As a developer, I want the hub to know the usual log locations of old servers, so that I do not look them up.

1. THE SYSTEM SHALL ship the catalog as a data file in the package (`src/chimericmesh/catalog/legacy_apps.json`) validated by a schema test, with at least 14 entries: Apache httpd, nginx, IIS, vsftpd, ProFTPD, FileZilla Server, Sendmail, Postfix, Exim, BIND (`named`), Samba, Squid, inetd/xinetd/telnetd, a syslog daemon (rsyslog, syslog-ng), and a net-snmp agent (`snmpd`, detection and log paths only, because trap intake is deferred). THE SYSTEM SHALL add `catalog/*.json` to the `[tool.setuptools.package-data]` entry in `pyproject.toml` (today only `web/*.html`), so a non-editable install ships the catalog. THE SYSTEM SHALL load the catalog with `importlib.resources`. IF it cannot be loaded, THEN the scan endpoint SHALL return 503 with a plain message instead of silently finding nothing. [Auto: build a wheel or use the installed layout, load the catalog, assert at least 14 entries]
2. THE SYSTEM SHALL define each entry as: `app_id`, `name`, `process_names` (lower-case, without `.exe`), `ports` (evidence: with a matching process they add confidence, without one they give only a `low` confidence suggestion, see 4.2), `protocol_label`, and per OS (`linux`, `windows`, `macos`) a list of `log_paths` each with a `path` (may contain `*` wildcards and `%ENV%` or `$ENV` placeholders from a fixed allow list such as `ProgramData`, `SystemDrive`, `HOME`), a `format` (one of the formats the `connectors` registry knows; the catalog is loaded with the registry's list passed in, so `discovery` imports no format code; when contingency item 2 of the map is applied, the catalog uses `plain` wherever it names `w3c` or `xferlog`) and a `kind` (`access`, `error`, `mail`, `ftp`, `other`). [Auto]
3. Illustrative defaults (final list in the design phase): Apache on Linux `/var/log/apache2/*.log`, `/var/log/httpd/*_log` with `ncsa` for access and `plain` for error logs; nginx `/var/log/nginx/*.log`; IIS `%SystemDrive%\inetpub\logs\LogFiles\W3SVC*\*.log` with `w3c` (`plain` if contingency item 2 is applied); vsftpd `/var/log/vsftpd.log` and `/var/log/xferlog` with `xferlog` (`plain` if contingency item 2 is applied); Postfix and Sendmail `/var/log/mail.log`, `/var/log/maillog` with `syslog-text`; Exim `/var/log/exim4/mainlog` with `syslog-text`; BIND `/var/log/named/*.log`; Samba `/var/log/samba/log.*`. [Manual: verify the paths on a sample of machines and docs]
4. THE SYSTEM SHALL NOT include Mosquitto or other protocols that are not outdated, unless a catalog entry carries a one-line `justification` field explaining why it is legacy. [Auto]
5. WHEN the catalog file is invalid at startup, THE SYSTEM SHALL log an error, disable discovery (`503` on scan start) and keep the rest of the hub running. [Auto]
6. THE SYSTEM SHALL expand environment placeholders only from the fixed allow list, never from process or file text. [Auto] [PBT]

### 4. Matching and suggestions
**User story:** As a developer, I want clear evidence for each suggestion, so that I can decide whether it is right.

1. THE SYSTEM SHALL match a process to a catalog entry when its normalized name (lower-case, `.exe` removed, directory removed) is in `process_names`. [Auto] [PBT]
2. THE SYSTEM SHALL treat a listening port as supporting evidence only: a catalog entry is suggested from ports alone only when its port list is a protocol-specific one (for example 21 for FTP, 25 for SMTP) and no process name matched, and it SHALL be marked `confidence: low`. [Auto]
3. THE SYSTEM SHALL return each suggestion as: `suggestion_id`, `app_id`, `name`, `confidence` (`high` when a process matched, `low` when only a port matched), `evidence` (matched process name, pid, listening ports; no command line), and `candidate_log_paths` each with `path`, `format`, `kind`, `exists`, `readable`, and `size_bytes` (from `os.stat`). [Auto]
4. THE SYSTEM SHALL list candidate paths that do not exist as `exists: false` and keep them (a log may be created on first use), but order existing ones first. [Auto]
5. THE SYSTEM SHALL suggest the same catalog entry at most once per scan, merging several matching processes into one suggestion. [Auto] [PBT]
6. THE SYSTEM SHALL NOT suggest an app that is already covered by a service with the same log path. [Auto]
7. THE SYSTEM SHALL strip control characters from every process name, path and port text before storing or returning it. [Auto] [PBT]
8. THE SYSTEM SHALL return no more than 50 suggestions per scan. [Auto]

### 5. Confirmation, edits and dismissal
**User story:** As a developer, I want to confirm, change or reject each suggestion, so that nothing is watched without my say.

1. THE SYSTEM SHALL NOT create a service or start tailing any file until the user accepts a suggestion. [Auto]
2. WHEN the user calls `POST /api/discovery/suggestions/{id}/accept` with the admin token and a body naming the service `id`, `name`, the selected `log_paths` (a subset of the candidates or user-edited paths), the `format` per path, and an optional `code_folder`, THE SYSTEM SHALL create the service and its log sources through the `services` rules (validation, caps, audit) with `origin: discovery`. [Auto]
3. THE SYSTEM SHALL let the user change any path or format at accept time and later through the normal `services` endpoints. Discovery does not need to run for a user to add a path. [Auto]
4. WHEN the user calls `POST /api/discovery/suggestions/{id}/dismiss` (admin token required, because it changes what discovery will offer), THE SYSTEM SHALL remember the dismissal (store table added by this module) and SHALL NOT suggest the same app and paths again unless the user clears dismissals. [Auto]
5. IF an accepted path fails the `services` log-source validation, THEN THE SYSTEM SHALL return 422 with the reason and create nothing from that request. [Auto]
6. WHEN a suggestion id is unknown or from an expired scan, THE SYSTEM SHALL return 404. Scan results are kept in memory for 1 hour. [Auto]
7. THE SYSTEM SHALL write an audit row per accepted or dismissed suggestion (app id, service id, number of paths). [Auto]

## Boundaries

- Always: scan only on explicit privileged request; use fixed argument lists; stat paths, never open them; strip control characters; mark partial results `limited`.
- Ask first: adding catalog apps that are not clearly legacy; reading process command lines or environment; adding any OS tool beyond the list in 2.1; persisting scan history.
- Never: read log contents during a scan; open a network connection; require or request admin rights for the basic scan; start tailing without confirmation; expose discovery to MCP tools; install, start, stop or modify any application found.

## Success Criteria

1. With a fake process runner that reports `nginx` and `vsftpd` and a temp directory standing in for `/var/log`, a scan returns two high-confidence suggestions with correct formats, and `open` was never called on any candidate path.
2. A scan with `socket.socket` patched to raise completes normally.
3. Without a token, the scan endpoint returns 401 and the runner is never called.
4. Accepting a suggestion creates a service and a log source, and a line appended to that file reaches the hub within 2 seconds (integration with `connectors`).
5. Catalog schema test passes; at least 14 entries; every `format` is in the registry's `known_formats`. The table-driven import test (capability map) passes: `discovery` imports only `core`, `services` and `store`.
6. Manual: on the Windows development machine run a scan with a real IIS or Apache install if present, or with the demo `legacy_app`, and confirm the suggestions list and `limited` flag are sensible.
7. Property tests (design phase) cover: name normalization, deduplication, placeholder expansion allow list, control-character stripping.

## Out of Scope (Deferred)

- Detecting apps by their config files or package databases (`dpkg -l`, registry): needs reading more of the machine; process and port evidence is enough for V1.
- Scanning other machines or subnets (port scanning): D1 and a security risk.
- Auto-accept of high-confidence suggestions: the user decides (D2).
- Discovering the code folder of an app: unreliable; the user sets it.
- Docker and container log paths: not outdated protocols; revisit later.
- Catalog updates from the internet: no network access in V1.
- Parsing app config files to learn custom log paths (for example `CustomLog` in Apache): reads config contents; the user can add the path by hand.
- macOS scan is in V1; it is item 3 on the map's contingency list if time is short.

## Open Questions

1. Resolved: only running processes and listening ports are suggested; installed but stopped Windows services are not (accepted 2026-11-18).
2. Resolved: `ss -ltnup` without root shows only the user's own processes, which is acceptable (`limited: true`) (accepted 2026-11-18).
3. Resolved: the demo needs Apache or nginx, IIS, and the demo `legacy_app`, which is not in the catalog and is added by hand to show the manual path (accepted 2026-11-18).

## Notes for design

Findings from the final review that design must resolve. No acceptance criterion changes for them.

- None of the open review findings (M1 to M4, L1 to L9) concerns this module directly. M1 (dependency-table groups) applies to the `discovery/*` and `catalog/*` files only through the map's file-to-group table.
