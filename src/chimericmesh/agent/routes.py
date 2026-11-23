"""REST routes of the agent loop (owner ``agent-loop``). Read-only: no route writes, none takes the admin token.
This is the only file of the package that may import the web stack. Every handler is ``async def`` (store T2).
Numbers are clamped here (R1.2, R1.3, R4.2); the text of every response is cleaned in the facade (R1.7).
"""
from __future__ import annotations
import re
from typing import Annotated
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from .errors import InvalidQuery, NoCodeFolder, SearchBusy, TooManyWaiters, UnknownProposal, UnknownService
from .limits import DEFAULT_EVENTS, MAX_EVENTS, MAX_EXCLUDE, MAX_ID_LEN, MAX_MINUTES, clamp
from .service import AgentService
MAX_CURSOR = 2**62                  # an id beyond this cannot exist; keeps the SQL bind value in range
_EXCLUDE_ID = re.compile(r"[A-Za-z0-9_.:-]{1,%d}" % MAX_ID_LEN)
class VerifyBody(BaseModel):
    """A size bound only; the 1 to 20 rule is check 3.1c of verify, which answers 200 with reasons."""
    model_config = ConfigDict(extra="forbid")
    files: list[Annotated[str, StringConstraints(max_length=1100)]] = Field(max_length=200)
def _not_found() -> HTTPException:
    return HTTPException(404, "service not found")
def _unprocessable(detail: str, field: str | None = None) -> JSONResponse:
    body = {"detail": detail} if field is None else {"detail": detail, "field": field}
    return JSONResponse(body, status_code=422)
def build_router(agent: AgentService) -> APIRouter:
    router = APIRouter()
    @router.get("/api/services/{service_id}/events")
    async def service_events(service_id: str, limit: int = DEFAULT_EVENTS, after_id: int = 0,
                             min_severity: int | None = None):
        try:
            return await agent.service_events(
                service_id, clamp(limit, 1, MAX_EVENTS), clamp(after_id, 0, MAX_CURSOR),
                None if min_severity is None else clamp(min_severity, 0, 7))
        except UnknownService:
            raise _not_found() from None
    @router.get("/api/services/{service_id}/digest")
    async def digest(service_id: str, minutes: int = 15):
        try:
            return await agent.digest(service_id, clamp(minutes, 1, MAX_MINUTES))
        except UnknownService:
            raise _not_found() from None
    @router.get("/api/services/{service_id}/code-search")
    async def code_search(service_id: str, query: str | None = None):
        if query is None:
            return _unprocessable("query is required", "query")
        try:
            return await agent.code_search(service_id, query)
        except UnknownService:
            raise _not_found() from None
        except NoCodeFolder:
            return _unprocessable("service has no code folder")
        except InvalidQuery as exc:
            return _unprocessable(str(exc), "query")
        except SearchBusy as exc:
            raise HTTPException(429, "search busy", headers={"Retry-After": str(exc.retry_after)}) from None
    @router.post("/api/proposals/{pid}/verify")
    async def verify(pid: str, body: VerifyBody):
        try:
            return agent.verify(pid, body.files)
        except UnknownProposal:
            raise HTTPException(404, "proposal not found") from None
    @router.get("/api/watch/next")
    async def watch_next(request: Request, timeout_seconds: int = 25,
                         service_id: str | None = Query(None, max_length=64),
                         exclude_ids: list[str] | None = Query(None)):
        exclude = exclude_ids or []
        if len(exclude) > MAX_EXCLUDE:
            return _unprocessable(f"at most {MAX_EXCLUDE} ids", "exclude_ids")
        if not all(_EXCLUDE_ID.fullmatch(item) for item in exclude):
            return _unprocessable("every id is 1 to %d characters of letters, digits and _ . : -" % MAX_ID_LEN,
                                  "exclude_ids")
        if service_id is not None and not agent.knows_service(service_id):
            raise _not_found()
        queue = agent.watch
        timeout = max(queue.wait_min, min(timeout_seconds, queue.wait_max))
        try:
            return await queue.next(timeout=timeout, service_id=service_id, exclude=frozenset(exclude),
                                    is_disconnected=request.is_disconnected)
        except TooManyWaiters as exc:
            raise HTTPException(429, "too many waiters", headers={"Retry-After": str(exc.retry_after)}) from None
    @router.get("/api/services/{service_id}/permissions-snippet")
    async def permissions_snippet(service_id: str, test_command: str | None = None):
        try:
            text = agent.permissions_snippet(service_id, test_command)
        except UnknownService:
            raise _not_found() from None
        except NoCodeFolder:
            return _unprocessable("service has no code folder")
        except InvalidQuery as exc:
            return _unprocessable(str(exc), "test_command")
        return PlainTextResponse(text, media_type="text/plain; charset=utf-8")
    return router
