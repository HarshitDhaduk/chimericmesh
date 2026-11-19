"""The ``require_admin`` FastAPI dependency (R6.3, R6.4, R6.7). One of two files that may import FastAPI."""
from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import HTTPException, Request

from .access import ADMIN_HEADER, AccessControl, AdminResult

MAX_TOKEN_HEADER = 256


def client_address(request: Request) -> str:
    client = request.client
    return client.host if client is not None and client.host else "unknown"


def route_template(request: Request) -> str:
    route = request.scope.get("route")
    return str(getattr(route, "path", None) or request.url.path)


def make_require_admin(access: AccessControl) -> Callable[[Request], Awaitable[None]]:
    """A plain async callable: a dependency for routes and directly awaitable from ``api.py``."""

    async def require_admin(request: Request) -> None:
        if not access.enforce:
            return
        presented = request.headers.get(ADMIN_HEADER)
        if presented is not None and len(presented) > MAX_TOKEN_HEADER:
            presented = ""                                   # oversized: counts as wrong
        outcome = await access.check_admin(
            presented, client=client_address(request),
            operation=f"{request.method} {route_template(request)}")
        if outcome.result is AdminResult.OK:
            return
        if outcome.result is AdminResult.RATE_LIMITED:
            raise HTTPException(429, "too many wrong tokens",
                                headers={"Retry-After": str(max(1, outcome.retry_after))})
        raise HTTPException(401, "admin token required")

    return require_admin
