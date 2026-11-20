"""REST routes of discovery (R1, R5). The only file of this module that may import FastAPI or pydantic.

Every route needs the admin token (O1: scan results list installed software). Domain errors become the status
codes of the design's error table. ``StoreError`` is left to the app's exception handler (503)."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from chimericmesh.services.errors import (
    BuiltinService,
    Conflict,
    ServiceNotFound,
    ServicesError,
    SubscriberFailed,
    ValidationFailed,
)

from .accept import AcceptBody
from .errors import AlreadyAccepted, CatalogUnavailable, DiscoveryError, ScanBusy, SuggestionNotFound
from .manager import DiscoveryService

MAX_TEXT = 1100
MAX_PATHS = 20
DISABLED = "discovery is disabled: the catalog could not be loaded"
ScanId = Annotated[str, Path(max_length=64)]


def default_actor(request: Request) -> str:
    client = request.client
    return f"rest:{client.host if client is not None and client.host else 'unknown'}"


def error_response(exc: Exception) -> JSONResponse:
    if isinstance(exc, ValidationFailed):
        return JSONResponse({"detail": exc.reason, "field": exc.field}, status_code=422)
    if isinstance(exc, Conflict):
        return JSONResponse({"detail": str(exc) or "conflict"}, status_code=409)
    if isinstance(exc, ScanBusy):
        return JSONResponse({"detail": "a scan is already running", "scan_id": exc.scan_id}, status_code=409)
    if isinstance(exc, AlreadyAccepted):
        return JSONResponse({"detail": "the suggestion is already accepted"}, status_code=409)
    if isinstance(exc, CatalogUnavailable):
        return JSONResponse({"detail": DISABLED}, status_code=503)         # the reason stays in the log
    if isinstance(exc, (SuggestionNotFound, ServiceNotFound)):
        return JSONResponse({"detail": "not found"}, status_code=404)
    if isinstance(exc, BuiltinService):
        return JSONResponse({"detail": "the unassigned service cannot be changed"}, status_code=403)
    if isinstance(exc, SubscriberFailed):
        return JSONResponse({"detail": f"subscriber {exc.name} failed"}, status_code=500)
    return JSONResponse({"detail": "internal error"}, status_code=500)


class _DiscoveryRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Awaitable[Any]]:
        original = super().get_route_handler()

        async def handler(request: Request):
            try:
                return await original(request)
            except (ServicesError, DiscoveryError) as exc:
                return error_response(exc)
        return handler


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LogPathBody(_Body):
    path: str = Field(max_length=MAX_TEXT)
    format: str = Field(max_length=MAX_TEXT)


class AcceptRequest(_Body):
    id: str = Field(max_length=MAX_TEXT)
    name: str = Field(max_length=MAX_TEXT)
    log_paths: list[LogPathBody] = Field(max_length=MAX_PATHS)
    code_folder: str | None = Field(default=None, max_length=MAX_TEXT)
    app_names: list[str] | None = Field(default=None, max_length=MAX_PATHS)


def build_router(service: DiscoveryService, require_admin: Callable,
                 *, audit_actor: Callable[[Request], str] = default_actor) -> APIRouter:
    router = APIRouter(route_class=_DiscoveryRoute)
    admin = [Depends(require_admin)]

    @router.post("/api/discovery/scan", status_code=202, dependencies=admin)
    async def start_scan(request: Request):
        scan = await service.manager.start(actor=audit_actor(request))
        return {"scan_id": scan.id, "state": scan.state}

    @router.get("/api/discovery/scans/{scan_id}", dependencies=admin)
    async def get_scan(scan_id: ScanId):
        scan = service.manager.get(scan_id)
        if scan is None:
            raise SuggestionNotFound(scan_id)
        return scan.to_dict()

    @router.post("/api/discovery/suggestions/{suggestion_id}/accept", status_code=201, dependencies=admin)
    async def accept(suggestion_id: ScanId, body: AcceptRequest, request: Request):
        parsed = AcceptBody(
            id=body.id, name=body.name, log_paths=tuple((p.path, p.format) for p in body.log_paths),
            code_folder=body.code_folder,
            app_names=None if body.app_names is None else tuple(body.app_names))
        svc, sources = await service.acceptor.accept(suggestion_id, parsed, actor=audit_actor(request))
        out = svc.to_dict()
        out["log_sources"] = [s.to_dict() for s in sources]
        return {"suggestion_id": suggestion_id, "service": out}

    @router.post("/api/discovery/suggestions/{suggestion_id}/dismiss", dependencies=admin)
    async def dismiss(suggestion_id: ScanId, request: Request):
        await service.manager.dismiss(suggestion_id, actor=audit_actor(request))
        return {"suggestion_id": suggestion_id, "dismissed": True}

    @router.get("/api/discovery/dismissals", dependencies=admin)
    async def list_dismissals():
        return await service.manager.dismissals()

    @router.delete("/api/discovery/dismissals", dependencies=admin)
    async def clear_dismissals(request: Request):
        return {"cleared": await service.manager.clear_dismissals(actor=audit_actor(request))}

    return router
