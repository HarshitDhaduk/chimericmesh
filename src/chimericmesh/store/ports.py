"""Protocols that let other modules use the store without importing the hub."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

Announce = Callable[[str], Awaitable[None]]


class AuditSink(Protocol):
    async def append(self, actor: str, action: str, subject: str, detail: str = "", *,
                     service_id: str | None = None, rule_id: str | None = None,
                     proposal_id: str | None = None) -> object: ...
