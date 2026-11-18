"""Incident detection: cluster similar events, classify, and suggest remediation."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .events import Event

_NORMALIZERS = [
    # Who/where values vary per attacker but not per problem (rhost=, user=, "from X").
    (re.compile(r"\b(rhost|user|logname|ruser)=\S*", re.I), r"\1=<v>"),
    (re.compile(r"\b(password|publickey|login)\s+for\s+(?:invalid\s+user\s+)?\S+", re.I), r"\1 for <v>"),
    (re.compile(r"\binvalid user\s+\S+", re.I), "invalid user <v>"),
    (re.compile(r"\bfrom\s+\S+", re.I), "from <v>"),
    (re.compile(r"\[\d{1,3}(?:\.\d{1,3}){3}\]"), "[<ip>]"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"), "<ip>"),
    (re.compile(r"\b0x[0-9a-fA-F]+\b"), "<hex>"),
    (re.compile(r"\b[0-9a-f]{8,}\b"), "<hex>"),
    (re.compile(r"\d+"), "<n>"),
]

# (category, pattern, hypothesis, remediation steps)
RULES: list[tuple[str, re.Pattern[str], str, list[str]]] = [
    ("memory", re.compile(r"out of memory|oom|cannot allocate|memory leak", re.I),
     "A process is exhausting RAM and the kernel is killing workloads.",
     ["Identify the top memory consumer (ps aux --sort=-rss | head)",
      "Add or raise memory limits and restart the offender",
      "Look for unbounded caches or leaks in the logged component"]),
    ("disk", re.compile(r"no space left|disk full|filesystem full|quota exceeded", re.I),
     "A filesystem is full, which will cascade into write failures.",
     ["Find large files (du -xh / | sort -h | tail)",
      "Rotate or compress logs and clear temp directories",
      "Add disk usage alerting before 90% full"]),
    ("network", re.compile(r"connection refused|timed out|timeout|unreachable|reset by peer", re.I),
     "A dependency is unreachable or overloaded; callers are timing out.",
     ["Check the target service is listening and healthy",
      "Verify firewall and routing between the hosts",
      "Add retries with backoff and a circuit breaker at the caller"]),
    ("security", re.compile(r"authentication fail(?:ure|ed)|failed password|invalid user|denied|brute|break-in", re.I),
     "Repeated authentication failures suggest credential stuffing or a misconfigured client.",
     ["Block or rate-limit the offending source address",
      "Confirm no legitimate client has stale credentials",
      "Enable lockout or key-only authentication"]),
    ("crash", re.compile(r"segfault|core dumped|panic|fatal|abort", re.I),
     "A process is crashing; the fault is likely a code defect or bad input.",
     ["Capture the core dump and stack trace",
      "Reproduce with the logged input under a debugger",
      "Roll back the most recent deploy if the crash began after it"]),
]
_GENERIC = ("unknown", "Recurring error pattern with no known signature.",
            ["Inspect the surrounding log lines for context",
             "Search the codebase for the logged message"])


def signature(message: str) -> str:
    """Collapse volatile tokens (ips, numbers, hex) so similar messages cluster."""
    out = message.lower()
    for pattern, repl in _NORMALIZERS:
        out = pattern.sub(repl, out)
    return re.sub(r"\s+", " ", out).strip()[:200]


def classify(message: str) -> tuple[str, str, list[str]]:
    for category, pattern, hypothesis, steps in RULES:
        if pattern.search(message):
            return category, hypothesis, steps
    return _GENERIC


@dataclass
class Incident:
    id: str
    host: str
    category: str
    signature: str
    severity: int
    count: int
    first_seen: float
    last_seen: float
    sources: list[str] = field(default_factory=list)
    sample: str = ""
    hypothesis: str = ""
    remediation: list[str] = field(default_factory=list)
    resolved: bool = False
    service_id: str | None = None
    opening: int = 1  # 1 for the first time it opened, +1 on each reopen
    resolved_at: float | None = None
    opened: bool = False  # internal: set once the baseline test passed; never reaches REST

    def to_dict(self) -> dict:
        data = asdict(self)
        del data["opened"]
        return data


def incident_id(host: str, sig: str) -> str:
    return hashlib.sha1(f"{host}|{sig}".encode()).hexdigest()[:10]


MAX_CLUSTERS = 5000


class IncidentTracker:
    """Incrementally clusters events by (host, signature) in O(1) per event.

    An incident is critical or worse (<= max_severity), or repeats at least
    ``min_count`` times at warning or worse. Counts are cumulative and independent
    of any event buffer, so a bounded log view never loses incident history.

    ``opened`` records that a cluster passed that test once; an opened incident stays an
    incident (for example after a reopen resets its count to 1). ``on_evict`` is called with
    each cluster dropped to bound memory.
    """

    def __init__(self, min_count: int = 3, max_severity: int = 2,
                 on_evict: Callable[[Incident], None] | None = None) -> None:
        self.min_count, self.max_severity = min_count, max_severity
        self.on_evict = on_evict
        self._clusters: dict[str, Incident] = {}

    def _baseline(self, inc: Incident) -> bool:
        return inc.severity <= self.max_severity or inc.count >= self.min_count

    def qualifies(self, inc: Incident) -> bool:
        return inc.opened or self._baseline(inc)

    def preview_id(self, ev: Event) -> str | None:
        """The id ``add`` would use for ``ev``, or None when ``add`` would ignore it."""
        if ev.severity > 4:
            return None
        sig = signature(ev.message)
        if not sig:
            return None
        return incident_id(ev.host, sig)

    def add(self, ev: Event) -> Incident | None:
        """Fold one event into its cluster. Returns the cluster, or None if ignored."""
        iid = self.preview_id(ev)
        if iid is None:
            return None
        inc = self._clusters.get(iid)
        if inc is None:
            category, hypothesis, steps = classify(ev.message)
            inc = self._clusters[iid] = Incident(
                id=iid, host=ev.host, category=category, signature=signature(ev.message),
                severity=ev.severity, count=0, first_seen=ev.ts, last_seen=ev.ts,
                sample=ev.message, hypothesis=hypothesis, remediation=list(steps),
                service_id=ev.service_id,
            )
            if len(self._clusters) > MAX_CLUSTERS:
                self._evict(keep=iid)
        elif inc.service_id is None and ev.service_id is not None:
            inc.service_id = ev.service_id
        inc.count += 1
        inc.severity = min(inc.severity, ev.severity)
        inc.first_seen = min(inc.first_seen, ev.ts)
        inc.last_seen = max(inc.last_seen, ev.ts)
        if ev.source not in inc.sources:
            inc.sources.append(ev.source)
        if not inc.opened and self._baseline(inc):
            inc.opened = True
        return inc

    def reopen(self, inc: Incident, ev: Event) -> Incident:
        """Start a new opening of a resolved incident from ``ev``."""
        inc.resolved, inc.resolved_at = False, None
        inc.opening += 1
        inc.count = 1
        inc.opened = True
        inc.severity = ev.severity
        inc.first_seen = inc.last_seen = ev.ts
        inc.sources = [ev.source]
        if inc.service_id is None and ev.service_id is not None:
            inc.service_id = ev.service_id
        return inc

    def install(self, inc: Incident) -> None:
        """Add a cluster built elsewhere (a stored row). Eviction applies as for ``add``."""
        self._clusters[inc.id] = inc
        if len(self._clusters) > MAX_CLUSTERS:
            self._evict(keep=inc.id)

    def _evict(self, keep: str) -> None:
        """Bound memory: drop the stalest cluster that is not yet an incident."""
        pool = [i for i in self._clusters.values() if i.id != keep and not self.qualifies(i)]
        pool = pool or [i for i in self._clusters.values() if i.id != keep]
        gone = self._clusters.pop(min(pool, key=lambda i: i.last_seen).id)
        if self.on_evict is not None:
            self.on_evict(gone)

    def resolved_ids(self) -> set[str]:
        return {i.id for i in self._clusters.values() if i.resolved}

    def get(self, incident_id: str) -> Incident | None:
        inc = self._clusters.get(incident_id)
        return inc if inc is not None and self.qualifies(inc) else None

    def incidents(self) -> list[Incident]:
        found = [i for i in self._clusters.values() if self.qualifies(i)]
        return sorted(found, key=lambda i: (i.severity, -i.count, -i.last_seen))


def analyze(events: Iterable[Event], min_count: int = 3, max_severity: int = 2) -> list[Incident]:
    """Group events by (host, signature). An incident is raised when a cluster is
    critical or worse (<= max_severity), or repeats at least ``min_count`` times at
    warning or worse. One-off errors are ignored: real logs are full of them.

    Batch form of IncidentTracker, handy for tests and offline analysis."""
    tracker = IncidentTracker(min_count, max_severity)
    for ev in events:
        tracker.add(ev)
    return tracker.incidents()


_CODE_SUFFIXES = {".py", ".js", ".ts", ".c", ".h", ".go", ".java", ".rb", ".sh", ".rs"}
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".hypothesis"}


def find_code_hints(incident: Incident, root: Path, limit: int = 5) -> list[dict]:
    """Locate source lines that could emit the incident's message (agentic diagnostics).

    Picks the longest literal fragment of the sample message (no digits) and greps for it.
    """
    fragments = [f.strip() for f in re.split(r"[\d:=\[\]()\"',]+", incident.sample) if len(f.strip()) >= 6]
    if not fragments or not root.is_dir():
        return []
    needle = max(fragments, key=len).lower()
    hits: list[dict] = []
    for path in root.rglob("*"):
        if len(hits) >= limit:
            break
        if path.suffix not in _CODE_SUFFIXES or any(p in _SKIP_DIRS for p in path.parts):
            continue
        try:
            if path.stat().st_size > 512_000:
                continue
            for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                if needle in line.lower():
                    hits.append({"file": str(path.relative_to(root)), "line": lineno, "text": line.strip()[:200]})
                    break
        except OSError:
            continue
    return hits
