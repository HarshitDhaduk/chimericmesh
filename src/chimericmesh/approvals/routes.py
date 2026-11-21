"""REST routes of auto-approval. One of two files of this module that may import the web stack.

Widening operations (create, patch, enable, resume, rule from a proposal) depend on ``require_admin``.
Reduce operations (revoke, pause, withdraw) take no token and are never refused (R2.3). Nothing here, and
nothing in ``mcp_server.py``, lets the agent create or widen a rule.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from chimericmesh.services.deps import client_address, route_template
from chimericmesh.services.limiter import reduce_key
from chimericmesh.store.errors import InvalidTransition, NotFound

from .errors import (
    ActiveProposalExists, ApprovalsError, ProposalExpired, ProposalInvalid, ProposalRateLimited, RuleConflict,
    RuleInvalid, RuleLimitReached, RuleNotApplicable, RuleNotFound,
)
from .manager import ApprovalManager
from .validate import validate_rule_fields, validate_rule_patch

SERVICE_ID_MAX = 64


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RuleBody(_Strict):
    service_id: str
    match_type: str
    match_value: str
    most_urgent_severity: Any = None
    expires_at: Any = None
    max_per_hour: Any = None
    max_per_incident: Any = None
    max_files: Any = None
    max_lines: Any = None
    note: Any = None


class RulePatchBody(_Strict):
    most_urgent_severity: Any = None
    expires_at: Any = None
    max_per_hour: Any = None
    max_per_incident: Any = None
    max_files: Any = None
    max_lines: Any = None
    note: Any = None


class RuleFromProposalBody(_Strict):
    match_type: str | None = None
    match_value: str | None = None
    most_urgent_severity: Any = None
    expires_at: Any = None
    max_per_hour: Any = None
    max_per_incident: Any = None
    max_files: Any = None
    max_lines: Any = None
    note: Any = None


def _given(body: BaseModel) -> dict[str, Any]:
    """Only the fields the caller sent (a null is treated as not sent)."""
    return {k: v for k, v in body.model_dump().items() if v is not None}


def actor_of(request: Request) -> str:
    return f"rest:{client_address(request)}"


def register_error_handlers(app: FastAPI) -> None:
    """Map the module's errors to the status codes of the design's REST table."""

    @app.exception_handler(ApprovalsError)
    async def approvals_error(request: Request, exc: ApprovalsError):
        if isinstance(exc, (RuleInvalid, ProposalInvalid)):
            return JSONResponse({"detail": exc.reason, "field": exc.field}, status_code=422)
        if isinstance(exc, ActiveProposalExists):
            return JSONResponse({"detail": str(exc), "proposal_id": exc.proposal_id}, status_code=409)
        if isinstance(exc, ProposalRateLimited):
            return JSONResponse({"detail": str(exc)}, status_code=429,
                                headers={"Retry-After": str(exc.retry_after)})
        if isinstance(exc, RuleNotFound):
            return JSONResponse({"detail": "rule not found"}, status_code=404)
        if isinstance(exc, (RuleConflict, RuleNotApplicable)):
            return JSONResponse({"detail": exc.reason}, status_code=409)
        if isinstance(exc, ProposalExpired):
            return JSONResponse({"detail": "the approval expired"}, status_code=409)
        if isinstance(exc, RuleLimitReached):
            return JSONResponse({"detail": str(exc) or "too many rules"}, status_code=409)
        return JSONResponse({"detail": "request refused"}, status_code=400)

    @app.exception_handler(NotFound)
    async def not_found(request: Request, exc: NotFound):
        return JSONResponse({"detail": "not found"}, status_code=404)


def build_router(manager: ApprovalManager, access: Any, require_admin: Callable[[Request], Awaitable[None]],
                 *, state: Any = None) -> APIRouter:
    """The rule routes. ``state`` (the hub) is only used to read proposals for the view."""
    router = APIRouter()

    async def admin(request: Request) -> None:
        """``require_admin`` plus a ``rule_auth_failed`` row on 401 (R2.1, R2.5). Nothing is written on 429."""
        try:
            await require_admin(request)
        except HTTPException as exc:
            if exc.status_code == 401:
                try:
                    await manager.audit_log.append(
                        f"client:{client_address(request)}", "rule_auth_failed", route_template(request), "",
                        rule_id=request.path_params.get("rule_id"))
                except Exception:  # noqa: BLE001 - the 401 matters more than its audit row
                    pass
            raise

    admin_dep = [Depends(admin)]

    def rule_or_404(rule_id: str):
        rule = manager.rule(rule_id)
        if rule is None:
            raise RuleNotFound(rule_id)
        return rule

    def view(rule) -> dict[str, Any]:
        return manager.rule_dict(rule)

    def proposal_view(prop) -> dict[str, Any]:
        return manager.proposal_view(prop)

    # ---- reads (no token) ------------------------------------------------------------------------------------
    @router.get("/api/rules")
    async def list_rules(service_id: str | None = Query(None, max_length=SERVICE_ID_MAX),
                         status: Literal["active", "suspended", "revoked", "expired"] | None = None):
        await manager.expire_due()
        return [view(r) for r in manager.rules(service_id=service_id, status=status)]

    @router.get("/api/rules/{rule_id}")
    async def get_rule(rule_id: str):
        await manager.expire_due()
        return view(rule_or_404(rule_id))

    @router.get("/api/approvals/status")
    async def approvals_status():
        return manager.status().to_dict()

    @router.get("/api/audit")
    async def audit(service_id: str | None = Query(None, max_length=SERVICE_ID_MAX),
                    rule_id: str | None = Query(None, max_length=100),
                    proposal_id: str | None = Query(None, max_length=100),
                    limit: int = Query(100, ge=1, le=500), before: int | None = Query(None, ge=0)):
        return await manager.audit_log.read(limit, before, service_id=service_id, rule_id=rule_id,
                                            proposal_id=proposal_id)

    @router.get("/api/proposals/{pid}/rule-defaults")
    async def rule_defaults(pid: str):
        await manager.expire_due()
        return manager.rule_defaults(pid).to_dict()

    # ---- widening operations (token) -------------------------------------------------------------------------
    @router.post("/api/rules", status_code=201, dependencies=admin_dep)
    async def create_rule(body: RuleBody, request: Request):
        fields = validate_rule_fields(_given(body), now=manager.now())
        return view(await manager.create_rule(fields, actor=actor_of(request)))

    @router.patch("/api/rules/{rule_id}", dependencies=admin_dep)
    async def patch_rule(rule_id: str, body: RulePatchBody, request: Request):
        rule = rule_or_404(rule_id)
        patch = validate_rule_patch(_given(body), now=manager.now(), created_at=rule.created_at)
        return view(await manager.patch_rule(rule_id, patch, actor=actor_of(request)))

    @router.post("/api/rules/{rule_id}/enable", dependencies=admin_dep)
    async def enable_rule(rule_id: str, request: Request):
        rule_or_404(rule_id)
        return view(await manager.enable_rule(rule_id, actor=actor_of(request)))

    @router.post("/api/rules/resume", dependencies=admin_dep)
    async def resume(request: Request):
        return (await manager.resume(actor=actor_of(request))).to_dict()

    @router.post("/api/proposals/{pid}/rule", status_code=201, dependencies=admin_dep)
    async def rule_from_proposal(pid: str, request: Request, body: RuleFromProposalBody | None = None):
        overrides = _given(body) if body is not None else {}
        return view(await manager.create_rule_from_proposal(pid, overrides, actor=actor_of(request)))

    # ---- reduce operations (no token, never refused) ---------------------------------------------------------
    @router.post("/api/rules/pause")
    async def pause_all(request: Request):
        who = client_address(request)
        return (await manager.pause(actor=actor_of(request), key=reduce_key("rest", who))).to_dict()

    @router.post("/api/rules/{rule_id}/revoke")
    async def revoke(rule_id: str, request: Request):
        rule_or_404(rule_id)
        who = client_address(request)
        return view(await manager.revoke_rule(rule_id, actor=actor_of(request), key=reduce_key("rest", who)))

    @router.post("/api/rules/{rule_id}/pause")
    async def pause_rule(rule_id: str, request: Request):
        rule_or_404(rule_id)
        who = client_address(request)
        return view(await manager.pause_rule(rule_id, actor=actor_of(request), key=reduce_key("rest", who)))

    @router.post("/api/proposals/{pid}/withdraw")
    async def withdraw(pid: str, request: Request):
        who = client_address(request)
        try:
            prop = await manager.withdraw(pid, actor=actor_of(request), key=reduce_key("rest", who))
        except InvalidTransition:
            raise HTTPException(409, "proposal cannot be withdrawn") from None
        return proposal_view(prop)

    return router
