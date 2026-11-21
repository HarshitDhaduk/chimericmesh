"""The catalog of legacy apps: data file, one pure validator, one total loader (R3)."""
from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from .expand import ALLOWED_ENV
from .text import CONTROL

log = logging.getLogger("chimericmesh.discovery")

CORE_APP_IDS = frozenset({
    "apache", "nginx", "iis", "vsftpd", "proftpd", "filezilla-server", "sendmail", "postfix", "exim",
    "bind", "samba", "squid", "inetd", "syslog-daemon", "snmpd",
})
OS_KEYS = ("linux", "windows", "macos")
KINDS = frozenset({"access", "error", "mail", "ftp", "other"})
_APP_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
_LABEL = re.compile(r"[a-z0-9 -]{1,20}")
_APP_KEYS = {"app_id", "name", "process_names", "ports", "protocol_label", "justification", "log_paths"}
_RULE_KEYS = {"path", "format", "kind"}
_WIN_DRIVE = re.compile(r"[A-Za-z]:[\\/]")
_WIN_PLACEHOLDER = re.compile(r"%([^%\\/]{1,40})%[\\/]")
_POSIX_PLACEHOLDER = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)/")


@dataclass(frozen=True, slots=True)
class PathRule:
    path: str
    format: str
    kind: str


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    app_id: str
    name: str
    process_names: tuple[str, ...]
    ports: tuple[int, ...]
    protocol_label: str
    justification: str | None
    log_paths: Mapping[str, tuple[PathRule, ...]]


@dataclass(frozen=True, slots=True)
class Catalog:
    entries: tuple[CatalogEntry, ...]
    version: int
    _cache: dict = field(default_factory=dict, compare=False, repr=False)

    def process_index(self, os_name: str) -> Mapping[str, tuple[CatalogEntry, ...]]:
        """Normalized process name -> entries. Linux names are cut to 15 characters (``/proc/<pid>/comm``)."""
        cached = self._cache.get(os_name)
        if cached is not None:
            return cached
        cut = 15 if os_name == "linux" else None
        index: dict[str, list[CatalogEntry]] = {}
        for entry in self.entries:
            for name in entry.process_names:
                bucket = index.setdefault(name[:cut], [])
                if entry not in bucket:
                    bucket.append(entry)
        frozen = {k: tuple(v) for k, v in index.items()}
        self._cache[os_name] = frozen
        return frozen


@dataclass(frozen=True, slots=True)
class CatalogLoad:
    catalog: Catalog | None
    reason: str


def fingerprint(entry: CatalogEntry, os_name: str) -> str:
    patterns = sorted(rule.path for rule in entry.log_paths.get(os_name, ()))
    text = os_name + "\n" + "\n".join(patterns)
    return hashlib.sha1(text.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_path(path: object, os_name: str, where: str, errs: list[str]) -> None:
    if not isinstance(path, str) or not path:
        errs.append(f"{where}.path: must be non-empty text")
        return
    if len(path) > 512:
        errs.append(f"{where}.path: must be at most 512 characters")
        return
    if CONTROL.search(path):
        errs.append(f"{where}.path: must not contain control characters")
        return
    rest = path
    if os_name == "windows":
        placeholder = _WIN_PLACEHOLDER.match(path)
        if placeholder is not None:
            allowed = {a.casefold() for a in ALLOWED_ENV["windows"]}
            if placeholder.group(1).casefold() not in allowed:
                errs.append(f"{where}.path: placeholder is not in the allow list")
                return
            rest = path[placeholder.end() - 1:]
        elif _WIN_DRIVE.match(path):
            rest = path[2:]
        else:
            errs.append(f"{where}.path: must be absolute (a drive letter or an allowed placeholder first)")
            return
        if "%" in rest:
            errs.append(f"{where}.path: a placeholder is only allowed at the start")
            return
        parts = [c for c in re.split(r"[\\/]", rest) if c]
    else:
        placeholder = _POSIX_PLACEHOLDER.match(path)
        if placeholder is not None:
            if placeholder.group(1) not in ALLOWED_ENV[os_name]:
                errs.append(f"{where}.path: placeholder is not in the allow list")
                return
            rest = path[placeholder.end() - 1:]
        elif not path.startswith("/"):
            errs.append(f"{where}.path: must be absolute")
            return
        if "$" in rest:
            errs.append(f"{where}.path: a placeholder is only allowed at the start")
            return
        parts = [c for c in rest.split("/") if c]
    if ".." in parts:
        errs.append(f"{where}.path: must not contain '..'")
    if "**" in path:
        errs.append(f"{where}.path: must not contain '**'")
    if "[" in path:
        errs.append(f"{where}.path: must not contain '['")
    if sum(1 for c in parts if "*" in c or "?" in c) > 2:
        errs.append(f"{where}.path: at most two wildcard components")
    if parts and parts[-1].endswith("*"):
        errs.append(f"{where}.path: the last component must not end with '*'")
    if not parts:
        errs.append(f"{where}.path: must name a file")


def _check_rules(rules: object, os_name: str, where: str, known_formats: frozenset[str], errs: list[str]) -> bool:
    if not isinstance(rules, list):
        errs.append(f"{where}: must be a list")
        return False
    if len(rules) > 20:
        errs.append(f"{where}: at most 20 rules")
    for i, rule in enumerate(rules[:21]):
        w = f"{where}[{i}]"
        if not isinstance(rule, dict):
            errs.append(f"{w}: must be an object")
            continue
        for key in rule:
            if key not in _RULE_KEYS:
                errs.append(f"{w}: unknown key {str(key)[:40]!r}")
        for key in ("path", "format", "kind"):
            if key not in rule:
                errs.append(f"{w}.{key}: is required")
        if "path" in rule:
            _check_path(rule["path"], os_name, w, errs)
        if "format" in rule and (not isinstance(rule["format"], str) or rule["format"] not in known_formats):
            errs.append(f"{w}.format: must be one of the known formats")
        if "kind" in rule and (not isinstance(rule["kind"], str) or rule["kind"] not in KINDS):
            errs.append(f"{w}.kind: must be one of {', '.join(sorted(KINDS))}")
    return bool(rules)


def _check_app(app: object, i: int, known_formats: frozenset[str], seen: set[str], errs: list[str]) -> None:
    where = f"apps[{i}]"
    if not isinstance(app, dict):
        errs.append(f"{where}: must be an object")
        return
    for key in app:
        if key not in _APP_KEYS:
            errs.append(f"{where}: unknown key {str(key)[:40]!r}")
    app_id = app.get("app_id")
    if not isinstance(app_id, str) or _APP_ID.fullmatch(app_id) is None:
        errs.append(f"{where}.app_id: must match [a-z0-9][a-z0-9-]{{0,39}}")
        app_id = None
    elif app_id in seen:
        errs.append(f"{where}.app_id: duplicate {app_id!r}")
    else:
        seen.add(app_id)
    name = app.get("name")
    if not isinstance(name, str) or not 1 <= len(name) <= 80:
        errs.append(f"{where}.name: must be 1 to 80 characters")
    names = app.get("process_names")
    if not isinstance(names, list) or not 1 <= len(names) <= 10:
        errs.append(f"{where}.process_names: must be a list of 1 to 10 names")
    else:
        for n in names:
            if (not isinstance(n, str) or not 1 <= len(n) <= 64 or n != n.lower() or n.endswith(".exe")
                    or "/" in n or "\\" in n or CONTROL.search(n)):
                errs.append(f"{where}.process_names: lower-case, 1 to 64 characters, no '.exe', no separators")
                break
    ports = app.get("ports")
    if not isinstance(ports, list) or len(ports) > 20 or not all(_is_int(p) and 1 <= p <= 65535 for p in ports):
        errs.append(f"{where}.ports: must be a list of at most 20 integers from 1 to 65535")
    label = app.get("protocol_label")
    if not isinstance(label, str) or _LABEL.fullmatch(label) is None:
        errs.append(f"{where}.protocol_label: must match [a-z0-9 -]{{1,20}}")
    just = app.get("justification")
    if just is not None and (not isinstance(just, str) or not 1 <= len(just) <= 200):
        errs.append(f"{where}.justification: must be null or 1 to 200 characters")
    elif just is None and app_id is not None and app_id not in CORE_APP_IDS:
        errs.append(f"{where}.justification: is required for an app that is not in the core list")
    paths = app.get("log_paths")
    if not isinstance(paths, dict):
        errs.append(f"{where}.log_paths: must be an object")
        return
    for key in paths:
        if key not in OS_KEYS:
            errs.append(f"{where}.log_paths: unknown OS {str(key)[:40]!r}")
    any_rules = False
    for os_name in OS_KEYS:
        if os_name not in paths:
            errs.append(f"{where}.log_paths.{os_name}: is required")
            continue
        any_rules |= _check_rules(paths[os_name], os_name, f"{where}.log_paths.{os_name}", known_formats, errs)
    if not any_rules:
        errs.append(f"{where}.log_paths: at least one OS needs a rule")


def validate_catalog(data: object, known_formats: frozenset[str]) -> list[str]:
    """Errors, or ``[]`` when valid. Total: never raises."""
    errs: list[str] = []
    try:
        if not isinstance(data, dict):
            return ["catalog: must be an object"]
        for key in data:
            if key not in ("version", "apps"):
                errs.append(f"catalog: unknown key {str(key)[:40]!r}")
        if data.get("version") != 1 or isinstance(data.get("version"), bool):
            errs.append("catalog.version: must be 1")
        apps = data.get("apps")
        if not isinstance(apps, list) or not 1 <= len(apps) <= 200:
            errs.append("catalog.apps: must be a list of 1 to 200 apps")
            return errs
        seen: set[str] = set()
        for i, app in enumerate(apps):
            _check_app(app, i, known_formats, seen, errs)
    except RecursionError:
        errs.append("catalog: too deeply nested")
    except Exception:  # noqa: BLE001 - total by contract
        errs.append("catalog: invalid")
    return errs


def _build(data: dict) -> Catalog:
    entries = []
    for app in data["apps"]:
        paths = {os_name: tuple(PathRule(r["path"], r["format"], r["kind"]) for r in app["log_paths"][os_name])
                 for os_name in OS_KEYS}
        entries.append(CatalogEntry(
            app_id=app["app_id"], name=app["name"], process_names=tuple(app["process_names"]),
            ports=tuple(app["ports"]), protocol_label=app["protocol_label"],
            justification=app.get("justification"), log_paths=paths))
    return Catalog(tuple(entries), 1)


def _read_packaged() -> str:
    from importlib import resources
    return resources.files("chimericmesh").joinpath("catalog", "legacy_apps.json").read_text("utf-8")


def load_catalog(known_formats: frozenset[str], *, text: str | None = None) -> CatalogLoad:
    """Never raises. ``reason`` is ``""`` when loaded; an error is logged once otherwise (R3.5)."""
    try:
        raw = _read_packaged() if text is None else text
        data = json.loads(raw)
        errs = validate_catalog(data, known_formats)
        if errs:
            reason = f"{len(errs)} error(s); first: {errs[0]}"
            log.error("discovery is disabled: the catalog is invalid: %s", reason)
            return CatalogLoad(None, reason)
        return CatalogLoad(_build(data), "")
    except Exception as exc:  # noqa: BLE001 - missing file, bad JSON, bad encoding, recursion
        reason = f"cannot load the catalog: {type(exc).__name__}"
        log.error("discovery is disabled: %s", reason)
        return CatalogLoad(None, reason)
