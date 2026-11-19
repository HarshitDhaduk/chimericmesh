"""Access control: Host allow-list, admin token check, wrong-token limiter and IRC policy (R6, R7, R9).

Pure pieces first, then ``AccessControl``. Nothing here imports the web stack: ``deps.py`` and
``asgi.py`` adapt it. The token is compared in constant time and never appears in ``repr``, logs or
audit rows.
"""
from __future__ import annotations

import hmac
import ipaddress
import logging
import math
import re
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from chimericmesh.store.ports import AuditSink

from .token import TokenStore

log = logging.getLogger("chimericmesh.services")

ADMIN_HEADER = "x-admin-token"
DEFAULT_ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
MAX_HEADER = 300
WRONG_TOKEN_LIMIT = 10
WRONG_TOKEN_WINDOW = 60.0
BLOCK_SECONDS = 60.0
MAX_TRACKED_ADDRESSES = 512

_HOST = re.compile(r"(\[[0-9A-Fa-f:.]{2,45}\]|[A-Za-z0-9][A-Za-z0-9.-]{0,252})(?::[0-9]{1,5})?")


# ---- Host check ----------------------------------------------------------------------------------

def _normalize_host(header: str | None) -> str | None:
    if not header or len(header) > MAX_HEADER:
        return None
    m = _HOST.fullmatch(header)
    if m is None:
        return None
    return m.group(1).casefold().strip("[]")


def host_allowed(header: str | None, allowed: frozenset[str]) -> bool:
    host = _normalize_host(header)
    return host is not None and host in allowed


def parse_allowed_hosts(value: str | None) -> frozenset[str]:
    if value is None:
        return DEFAULT_ALLOWED_HOSTS
    good: set[str] = set()
    bad = 0
    for entry in value.split(","):
        entry = entry.strip()
        if not entry:
            continue
        host = _normalize_host(entry)
        if host is None:
            bad += 1
        else:
            good.add(host)
    if bad:
        log.warning("ignored %d invalid entries in CHIMERICMESH_ALLOWED_HOSTS", bad)
    return frozenset(good) if good else DEFAULT_ALLOWED_HOSTS


def is_loopback(bind: str) -> bool:
    name = bind.strip().strip("[]").casefold()
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


# ---- IRC policy ----------------------------------------------------------------------------------

RESERVED_NICKS = frozenset({"dashboard", "chimericbot"})


def is_reserved_nick(nick: str) -> bool:
    return nick.strip().casefold() in RESERVED_NICKS


class OperatorVerdict(Enum):
    ALLOWED = "allowed"
    NOT_OPERATOR = "not_operator"
    OPERATORS_UNSET = "operators_unset"
    NEEDS_PASS = "needs_pass"
    BASELINE = "baseline"       # apply hub.is_operator(nick); never returned by operator_verdict


def operator_verdict(*, nick: str, operators: set[str] | None, pass_ok: bool) -> OperatorVerdict:
    if not operators:
        return OperatorVerdict.OPERATORS_UNSET
    if nick.lower() not in {o.lower() for o in operators}:
        return OperatorVerdict.NOT_OPERATOR
    if not pass_ok:
        return OperatorVerdict.NEEDS_PASS
    return OperatorVerdict.ALLOWED


# ---- Wrong-token limiter -------------------------------------------------------------------------

class AdminResult(Enum):
    OK = "ok"
    UNAUTHORIZED = "unauthorized"
    RATE_LIMITED = "rate_limited"


@dataclass(frozen=True, slots=True)
class AdminOutcome:
    result: AdminResult
    retry_after: int = 0


class _Entry:
    __slots__ = ("failures", "blocked_until")

    def __init__(self) -> None:
        self.failures: deque[float] = deque()
        self.blocked_until = 0.0


class WrongTokenLimiter:
    """At most 10 answered failures per address per minute, then a 60-second block (R6.7)."""

    def __init__(self, *, limit: int = WRONG_TOKEN_LIMIT, window: float = WRONG_TOKEN_WINDOW,
                 block: float = BLOCK_SECONDS, max_addresses: int = MAX_TRACKED_ADDRESSES) -> None:
        self._limit, self._window, self._block, self._max = limit, window, block, max_addresses
        self._entries: OrderedDict[str, _Entry] = OrderedDict()

    def fail(self, address: str, now: float) -> tuple[bool, int]:
        """Record a wrong token. Returns ``(blocked, retry_after)``; ``blocked`` False means answer 401."""
        entry = self._entries.get(address)
        if entry is None:
            entry = self._entries[address] = _Entry()
            self._evict(now)
        else:
            self._entries.move_to_end(address)
        if entry.blocked_until > now:
            return True, math.ceil(entry.blocked_until - now)
        while entry.failures and now - entry.failures[0] >= self._window:
            entry.failures.popleft()
        if len(entry.failures) >= self._limit:
            entry.failures.clear()
            entry.blocked_until = now + self._block
            return True, math.ceil(self._block)
        entry.failures.append(now)
        return False, 0

    def _evict(self, now: float) -> None:
        while len(self._entries) > self._max:
            victim = next((a for a, e in self._entries.items() if e.blocked_until <= now), None)
            if victim is None:
                victim = next(iter(self._entries))
            del self._entries[victim]


# ---- AccessControl -------------------------------------------------------------------------------

def _bytes(value: str) -> bytes:
    return value.encode("utf-8", "replace")


class AccessControl:
    def __init__(self, *, enforce: bool, tokens: TokenStore | None, allowed_hosts: frozenset[str],
                 audit: AuditSink | None, now: Callable[[], float] = time.time) -> None:
        self.enforce = enforce
        self.tokens = tokens
        self.allowed_hosts = allowed_hosts
        self._audit = audit
        self._now = now
        self._limiter = WrongTokenLimiter()

    @classmethod
    def disabled(cls) -> "AccessControl":
        return cls(enforce=False, tokens=None, allowed_hosts=DEFAULT_ALLOWED_HOSTS, audit=None)

    @property
    def token_path(self):
        return None if self.tokens is None else self.tokens.path

    def host_allowed(self, host_header: str | None) -> bool:
        return True if not self.enforce else host_allowed(host_header, self.allowed_hosts)

    def verify_token(self, presented: str | None) -> bool:
        """Constant-time comparison on UTF-8 bytes. Never raises."""
        if not self.enforce:
            return True
        if self.tokens is None or not isinstance(presented, str):
            return False
        try:
            return hmac.compare_digest(_bytes(presented), _bytes(self.tokens.current()))
        except Exception:
            return False

    def verify_pass(self, value: str | None) -> bool:
        return self.verify_token(value)

    async def check_admin(self, presented: str | None, *, client: str, operation: str) -> AdminOutcome:
        if not self.enforce or self.verify_token(presented):
            return AdminOutcome(AdminResult.OK)
        blocked, retry_after = self._limiter.fail(client, float(self._now()))
        if blocked:
            return AdminOutcome(AdminResult.RATE_LIMITED, retry_after)
        if self._audit is not None:
            try:
                await self._audit.append(f"client:{client}", "auth_failed", operation, "")
            except Exception:
                log.exception("could not write the auth_failed audit row")
        return AdminOutcome(AdminResult.UNAUTHORIZED)

    def operator_verdict(self, nick: str, *, operators: set[str] | None, pass_ok: bool) -> OperatorVerdict:
        return operator_verdict(nick=nick, operators=operators, pass_ok=pass_ok)

    def __repr__(self) -> str:
        return f"AccessControl(enforce={self.enforce!r}, token_path={str(self.token_path)!r})"
