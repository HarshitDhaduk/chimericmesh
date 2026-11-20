"""Per-source rate limit (token bucket) and the periodic drop summary. Pure; the caller passes the clock."""
from __future__ import annotations


class TokenBucket:
    def __init__(self, rate: float = 2000.0, burst: float = 2000.0, now: float = 0.0) -> None:
        self.rate = rate
        self.burst = burst
        self._tokens = burst
        self._last = now

    def take(self, n: int, now: float) -> int:
        """Admit ``min(n, available)`` lines and return that number."""
        if now > self._last:
            self._tokens = min(self.burst, self._tokens + (now - self._last) * self.rate)
            self._last = now
        admitted = max(0, min(n, int(self._tokens)))
        self._tokens -= admitted
        return admitted


class DropNotice:
    def __init__(self, interval: float = 10.0) -> None:
        self.interval = interval
        self._count = 0
        self._since: float | None = None   # the last report, or the first unreported drop

    def add(self, n: int) -> None:
        if n > 0:
            self._count += n

    def due(self, now: float) -> int:
        """The count to report (and reset), or 0 while less than ``interval`` passed since the last report."""
        if self._count == 0:
            return 0
        if self._since is None:
            self._since = now
        if now - self._since < self.interval:
            return 0
        count, self._count = self._count, 0
        self._since = now
        return count
