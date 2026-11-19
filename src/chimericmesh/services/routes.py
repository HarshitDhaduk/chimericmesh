"""REST routes for services and log sources (R1, R3, R5, R8). One of two files that may import FastAPI.

Reads need no token. Every write depends on ``require_admin``. Registry errors become the status
codes of the design's error table; ``StoreError`` is left to the app's exception handler (503).
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from .access import AccessControl
from .deps import client_address
from .errors import (
    BuiltinService,
    Conflict,
    LogSourceNotFound,
    ServiceNotFound,
    ServicesError,
    SubscriberFailed,
    ValidationFailed,
)
from .models import LogSourceFields, LogSourcePatch, ServiceFields, ServicePatch
from .registry import ServiceRegistry
from .validate import validate_log_source_fields, validate_service_fields

MAX_LIST = 50
MAX_TEXT = 1100


def default_actor(request: Request) -> str:
    return f"rest:{client_address(request)}"


def error_response(exc: ServicesError) -> JSONResponse:
    if isinstance(exc, ValidationFailed):
        return JSONResponse({"detail": exc.reason, "field": exc.field}, status_code=422)
    if isinstance(exc, Conflict):
        return JSONResponse({"detail": str(exc) or "conflict"}, status_code=409)
    if isinstance(exc, (ServiceNotFound, LogSourceNotFound)):
        return JSONResponse({"detail": "not found"}, status_code=404)
    if isinstance(exc, BuiltinService):
        return JSONResponse({"detail": "the unassigned service cannot be changed"}, status_code=403)
    if isinstance(exc, SubscriberFailed):
        return JSONResponse({"detail": f"subscriber {exc.name} failed"}, status_code=500)
    return JSONResponse({"detail": "internal error"}, status_code=500)


class _ServicesRoute(APIRoute):
    """Turns registry errors into responses, so the router needs no app-level handler."""

    def get_route_handler(self) -> Callable[[Request], Awaitable[Any]]:
        original = super().get_route_handler()

        async def handler(request: Request):
            try:
                return await original(request)
            except ServicesError as exc:
                return error_response(exc)

        return handler


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ServiceCreate(_Body):
    id: str = Field(max_length=MAX_TEXT)
    name: str = Field(max_length=MAX_TEXT)
    hosts: list[str] | None = Field(default=None, max_length=MAX_LIST)
    app_names: list[str] | None = Field(default=None, max_length=MAX_LIST)
    protocols: list[str] | None = Field(default=None, max_length=MAX_LIST)
    code_folder: str | None = Field(default=None, max_length=MAX_TEXT)


class ServiceUpdate(_Body):
    name: str | None = Field(default=None, max_length=MAX_TEXT)
    hosts: list[str] | None = Field(default=None, max_length=MAX_LIST)
    app_names: list[str] | None = Field(default=None, max_length=MAX_LIST)
    protocols: list[str] | None = Field(default=None, max_length=MAX_LIST)
    code_folder: str | None = Field(default=None, max_length=MAX_TEXT)


class LogSourceCreate(_Body):
    path: str = Field(max_length=MAX_TEXT)
    format: str = Field(max_length=MAX_TEXT)
    enabled: StrictBool | None = None
    host: str | None = Field(default=None, max_length=MAX_TEXT)
    backfill_lines: StrictInt | None = None


class LogSourceUpdate(_Body):
    format: str | None = Field(default=None, max_length=MAX_TEXT)
    enabled: StrictBool | None = None
    host: str | None = Field(default=None, max_length=MAX_TEXT)
    backfill_lines: StrictInt | None = None


def _given(body: BaseModel) -> dict[str, object]:
    """Only the keys the caller sent. JSON null stays ``None``: the validators decide what it means."""
    return body.model_dump(exclude_unset=True)


def build_router(registry: ServiceRegistry, access: AccessControl, require_admin: Callable,
                 *, audit_actor: Callable[[Request], str] = default_actor) -> APIRouter:
    router = APIRouter(route_class=_ServicesRoute)
    admin = [Depends(require_admin)]

    @router.get("/api/services")
    async def list_services():
        return [s.to_dict() for s in registry.statuses()]

    @router.get("/api/services/{service_id}")
    async def get_service(service_id: str):
        status = registry.status_of(service_id)
        if status is None:
            raise HTTPException(404, "service not found")
        return status.to_dict()

    @router.post("/api/services", status_code=201, dependencies=admin)
    async def create_service(body: ServiceCreate, request: Request):
        fields = validate_service_fields(_given(body), for_create=True)
        assert isinstance(fields, ServiceFields)
        return (await registry.create_service(fields, actor=audit_actor(request))).to_dict()

    @router.patch("/api/services/{service_id}", dependencies=admin)
    async def update_service(service_id: str, body: ServiceUpdate, request: Request):
        patch = validate_service_fields(_given(body), for_create=False)
        assert isinstance(patch, ServicePatch)
        return (await registry.update_service(service_id, patch, actor=audit_actor(request))).to_dict()

    @router.delete("/api/services/{service_id}", dependencies=admin)
    async def delete_service(service_id: str, request: Request):
        await registry.delete_service(service_id, actor=audit_actor(request))
        return {"deleted": service_id}

    @router.post("/api/services/{service_id}/log-sources", status_code=201, dependencies=admin)
    async def add_log_source(service_id: str, body: LogSourceCreate, request: Request):
        fields = validate_log_source_fields(_given(body), known_formats=registry.known_formats, for_create=True)
        assert isinstance(fields, LogSourceFields)
        source = await registry.add_log_source(service_id, fields, actor=audit_actor(request))
        return source.to_dict()

    @router.patch("/api/services/{service_id}/log-sources/{source_id}", dependencies=admin)
    async def update_log_source(service_id: str, source_id: str, body: LogSourceUpdate, request: Request):
        patch = validate_log_source_fields(_given(body), known_formats=registry.known_formats, for_create=False)
        assert isinstance(patch, LogSourcePatch)
        source = await registry.update_log_source(service_id, source_id, patch, actor=audit_actor(request))
        return source.to_dict()

    @router.delete("/api/services/{service_id}/log-sources/{source_id}", dependencies=admin)
    async def remove_log_source(service_id: str, source_id: str, request: Request):
        await registry.remove_log_source(service_id, source_id, actor=audit_actor(request))
        return {"deleted": source_id}

    return router
