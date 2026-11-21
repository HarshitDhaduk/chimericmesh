"""Pure matching of a ``Snapshot`` against the catalog (R4). The only I/O is the injected ``FsProbe``."""
from __future__ import annotations

import threading
import time
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from .catalog import Catalog, CatalogEntry, fingerprint
from .expand import expand_pattern
from .models import (
    MAX_PATHS_PER_SUGGESTION,
    MAX_SUGGESTIONS,
    PORT_ONLY_PORTS,
    CandidatePath,
    Evidence,
    Snapshot,
    Suggestion,
)
from .probe import FsProbe
from .text import clean_text, path_key

MAX_EVIDENCE_PORTS = 20
REASON_TRUNCATED = "candidate list truncated"
REASON_TIME = "scan time limit"


@dataclass(frozen=True, slots=True)
class MatchResult:
    suggestions: tuple[Suggestion, ...]
    limited: bool
    reason: str                       # "" when not limited


@dataclass(slots=True)
class _Hit:
    entry: CatalogEntry
    confidence: str
    evidence: Evidence


def _key(name: str, os_name: str) -> str:
    return name[:15] if os_name == "linux" else name


def _process_hits(catalog: Catalog, snapshot: Snapshot, os_name: str) -> dict[str, list[tuple[int | None, str]]]:
    index = catalog.process_index(os_name)
    found: dict[str, set[tuple[int | None, str]]] = defaultdict(set)
    for proc in snapshot.procs:
        for entry in index.get(_key(proc.name, os_name), ()):
            found[entry.app_id].add((proc.pid, proc.name))
    return {k: sorted(v, key=lambda t: (t[0] is None, t[0] or 0, t[1])) for k, v in found.items()}


def _hits(catalog: Catalog, snapshot: Snapshot, os_name: str) -> list[_Hit]:
    matched = _process_hits(catalog, snapshot, os_name)
    matched_pids = {pid for procs in matched.values() for pid, _ in procs if pid is not None}
    claimed = {l.port for l in snapshot.listeners if l.pid is not None and l.pid in matched_pids}
    listener_ports = {l.port for l in snapshot.listeners}
    hits: list[_Hit] = []
    for entry in catalog.entries:
        if not entry.log_paths.get(os_name):
            continue                                          # nothing to offer on this OS (for example IIS on Linux)
        procs = matched.get(entry.app_id)
        if procs:
            pids = {pid for pid, _ in procs if pid is not None}
            ports = sorted({l.port for l in snapshot.listeners if l.pid is not None and l.pid in pids})
            pid = next((p for p, _ in procs if p is not None), None)
            names = sorted({n for _, n in procs})
            hits.append(_Hit(entry, "high", Evidence(clean_text(names[0], 64), pid, len(procs),
                                                     tuple(ports[:MAX_EVIDENCE_PORTS]))))
            continue
        eligible = (set(entry.ports) & PORT_ONLY_PORTS) & listener_ports
        eligible -= claimed
        if eligible:
            hits.append(_Hit(entry, "low", Evidence(None, None, 0, tuple(sorted(eligible)[:MAX_EVIDENCE_PORTS]))))
    hits.sort(key=lambda h: h.confidence != "high")           # stable: catalog order inside each class
    return hits


def _is_wild(pattern: str) -> bool:
    return "*" in pattern or "?" in pattern


class _Stop(Exception):
    pass


def _candidates(entry: CatalogEntry, os_name: str, env: Mapping[str, str], fs: FsProbe,
                covered: Mapping[str, str], stop: Callable[[], bool]) -> tuple[list[CandidatePath], bool, bool]:
    """Candidates in catalog order, existing first. Returns (candidates, truncated, hit_time_limit)."""
    ordered: list[CandidatePath] = []
    seen: set[str] = set()
    truncated = False
    timed_out = False
    try:
        for rule in entry.log_paths.get(os_name, ()):
            if stop():
                raise _Stop
            expanded = expand_pattern(rule.path, os_name, env, fs.listdir)
            truncated = truncated or expanded.truncated
            rows: list[tuple[float, str, CandidatePath]] = []
            for path in expanded.paths:
                if stop():
                    raise _Stop
                key = path_key(path, os_name)
                if key in seen:
                    continue
                seen.add(key)
                exists, size, mtime = fs.stat(path)
                readable = fs.readable(path) if exists else False
                cand = CandidatePath(path=clean_text(path, 1024), format=rule.format, kind=rule.kind,
                                     exists=exists, readable=readable, size_bytes=size,
                                     watched_by=covered.get(key))
                rows.append((mtime if mtime is not None else float("-inf"), path, cand))
            if _is_wild(rule.path):
                rows.sort(key=lambda r: (r[0], r[1]), reverse=True)
            ordered.extend(r[2] for r in rows)
    except _Stop:
        timed_out = True
    ordered.sort(key=lambda c: not c.exists)                  # stable: existing first, catalog order kept
    return ordered, truncated, timed_out


def _covered_drop(cands: list[CandidatePath]) -> bool:
    """Drop when at least one candidate is covered and every existing candidate is covered."""
    if not any(c.watched_by for c in cands):
        return False
    return all(c.watched_by for c in cands if c.exists)


def build_suggestions(catalog: Catalog, snapshot: Snapshot, os_name: str, *, env: Mapping[str, str],
                      fs: FsProbe, covered: Mapping[str, str], dismissed: frozenset[tuple[str, str]],
                      next_id: Callable[[], str], deadline: float, cancel: threading.Event,
                      clock: Callable[[], float] = time.monotonic,
                      limit: int = MAX_SUGGESTIONS) -> MatchResult:
    def stop() -> bool:
        return cancel.is_set() or clock() >= deadline

    reasons: list[str] = []
    out: list[Suggestion] = []
    for hit in _hits(catalog, snapshot, os_name):
        if len(out) >= limit:
            break
        if stop():
            reasons.append(REASON_TIME)
            break
        fp = fingerprint(hit.entry, os_name)
        if (hit.entry.app_id, fp) in dismissed:
            continue
        cands, truncated, timed_out = _candidates(hit.entry, os_name, env, fs, covered, stop)
        if timed_out:
            reasons.append(REASON_TIME)
            break
        if len(cands) > MAX_PATHS_PER_SUGGESTION:
            cands = cands[:MAX_PATHS_PER_SUGGESTION]
            truncated = True
        if truncated:
            reasons.append(REASON_TRUNCATED)
        if _covered_drop(cands):
            continue
        out.append(Suggestion(
            suggestion_id=next_id(), app_id=hit.entry.app_id, name=clean_text(hit.entry.name, 80),
            protocol_label=clean_text(hit.entry.protocol_label, 20), confidence=hit.confidence,  # type: ignore[arg-type]
            evidence=hit.evidence, candidates=tuple(cands), fingerprint=fp))
    unique = list(dict.fromkeys(reasons))
    if REASON_TIME in unique:
        unique.remove(REASON_TIME)
        unique.insert(0, REASON_TIME)
    return MatchResult(tuple(out), bool(unique), "; ".join(unique))
