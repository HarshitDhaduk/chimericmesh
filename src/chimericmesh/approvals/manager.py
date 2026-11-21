"""``ApprovalManager``: the one facade of auto-approval.

Threads: everything here runs on the loop thread except ``ApprovalPlan.write_in_tx`` and the registry
callbacks, which run on the ``store-writer`` thread and use only their ``tx``.

Authorization is the caller's business: the widen operations (create, patch, enable, resume) are called
after ``require_admin`` passed; the reduce operations (revoke, pause, withdraw) take no token and are
never refused (R2.3). There is no method that the agent can reach.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
import weakref
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from chimericmesh.services.models import ServiceChange
from chimericmesh.services.registry import ServiceRegistry
from chimericmesh.store import Store, StoreError, Tx
from chimericmesh.store.errors import InvalidTransition, NotFound
from chimericmesh.store.models import Proposal
from chimericmesh.store.ports import ProposalWriter, StateView

from . import rulebook as rb
from .errors import (
    ActiveProposalExists, ProposalExpired, ProposalInvalid, RuleConflict, RuleInvalid, RuleLimitReached,
    RuleNotApplicable, RuleNotFound,
)
from .format import auto_approved_line, cooldown_note, rule_line, safe_text
from .ledger import OpeningRow, UsageLedger, UsageRow
from .limits import ProposalLimits
from .matching import MatchContext, first_match
from .models import (
    BOUNDS, MAX_EXPIRY_SECONDS, MAX_RULES_PER_SERVICE, MAX_RULES_TOTAL, ApprovalPlan, ApprovalSettings,
    ApprovalStatus, CreateContext, Rule, RuleCheck, RuleDefaults, RuleFields, RulePatch,
)
from .rulebook import RuleBook, Snapshot
from .scope import check_files
from .validate import clean_note, validate_completion, validate_new_proposal

log = logging.getLogger("chimericmesh.approvals")

AnnounceFn = Callable[[str], Awaitable[None]]
RETENTION_SECONDS = 31 * 86400.0
HOUR = 3600.0
EXPIRE_DUE_TIMEOUT = 1.0
SWEEP_STEPS = ("_step_persist_dirty", "_step_expire", "_step_deny_stale", "_step_ttl", "_step_repair",
               "_step_reload", "_step_openings", "_step_prune")


def _retrieve(task: "asyncio.Future[Any]") -> None:
    """Mark the exception of a shielded unit as retrieved so a cancelled caller logs nothing."""
    if not task.cancelled():
        task.exception()


class ApprovalManager:
    def __init__(self, store: Store, *, state: StateView, writer: ProposalWriter, registry: ServiceRegistry,
                 limiter: Any, announce: AnnounceFn, protected: Sequence[str] = (),
                 settings: ApprovalSettings | None = None, now: Callable[[], float] | None = None,
                 expire_timeout: float = EXPIRE_DUE_TIMEOUT) -> None:
        # Weak, like the registry's: a strong reference would keep a private ``:memory:`` store (and its writer
        # thread) alive after the app that owns it is dropped.
        self._store_ref = weakref.ref(store)
        self._state = state
        self._writer = writer
        self._registry = registry
        self._limiter = limiter
        self._announce = announce
        self._protected = tuple(protected)
        self._settings = settings or ApprovalSettings()
        self._now: Callable[[], float] = now or state.now
        self._expire_timeout = expire_timeout
        self._book = RuleBook()
        self._ledger = UsageLedger()
        self._limits = ProposalLimits()
        self._rules_lock = asyncio.Lock()      # serializes widen operations against each other only
        self._sweep_lock = asyncio.Lock()
        self._sweep_task: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()
        self._warned_at = float("-inf")
        self._closed = False
        self._unsaved_openings: dict[tuple[str, int], float] = {}   # first sightings not yet in the database

    # ---- construction ----------------------------------------------------------------------------------
    @classmethod
    def load(cls, store: Store, *, state: StateView, writer: ProposalWriter, registry: ServiceRegistry,
             limiter: Any, announce: AnnounceFn, protected: Sequence[str] = (),
             settings: ApprovalSettings | None = None, now: Callable[[], float] | None = None,
             expire_timeout: float = EXPIRE_DUE_TIMEOUT) -> "ApprovalManager":
        rb.ensure_migrated(store)
        self = cls(store, state=state, writer=writer, registry=registry, limiter=limiter, announce=announce,
                   protected=protected, settings=settings, now=now, expire_timeout=expire_timeout)
        since = self._now() - RETENTION_SECONDS

        def read(tx: Tx):
            paused, by, at = rb.load_state(tx)
            return (rb.load_rules(tx), paused, by, at,
                    [UsageRow(r["rule_id"], r["proposal_id"], r["incident_id"], int(r["opening"]), r["service_id"],
                              float(r["approved_at"]),
                              None if r["completed_at"] is None else float(r["completed_at"]))
                     for r in rb.load_uses(tx, since)],
                    [OpeningRow(r["incident_id"], int(r["opening"]), float(r["first_seen_at"]))
                     for r in rb.load_openings(tx, since)])

        rules, paused, by, at, uses, openings = store.read_sync(read, allow_blocking=True)
        self._book.load(rules, paused=paused, paused_by=by, paused_at=at)
        self._ledger.seed(uses, openings)
        self._seed_limits()
        registry.subscribe(on_code_folder_changed=self._on_code_folder_changed,
                           on_service_deleted=self._on_service_deleted, after_commit=self._on_after_commit,
                           name="approvals")
        return self

    def _seed_limits(self) -> None:
        now = self._now()
        by_service: dict[str, list[float]] = {}
        for prop in self._state.list_proposals():
            if now - prop.created < HOUR:
                by_service.setdefault(prop.service_id or "unassigned", []).append(prop.created)
        self._limits.seed(by_service, now)

    @property
    def _store(self) -> Store:
        store = self._store_ref()
        if store is None:
            raise RuntimeError("the approval manager was used after its store was dropped")
        return store

    # ---- small helpers -----------------------------------------------------------------------------------
    def _warn_once_a_minute(self, text: str, *args: object) -> None:
        mono = time.monotonic()
        if mono - self._warned_at >= 60.0:
            self._warned_at = mono
            log.warning(text, *args)

    async def _say(self, text: str) -> None:
        try:
            await self._announce(text)
        except Exception:  # noqa: BLE001 - an announcement problem never undoes a decision
            log.exception("could not announce an approvals event")

    def _unit(self, coro: Awaitable[Any]) -> "asyncio.Future[Any]":
        """Run ``coro`` as a task that a cancelled caller cannot interrupt (store T6)."""
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(_retrieve)
        return asyncio.shield(task)

    def _audit(self, tx: Tx, actor: str, action: str, subject: str, detail: str = "", *,
               service_id: str | None = None, rule_id: str | None = None, proposal_id: str | None = None) -> int:
        return self._store.audit.append_tx(tx, actor, action, subject, detail, service_id=service_id,
                                           rule_id=rule_id, proposal_id=proposal_id)

    # ---- reads (loop thread, no I/O) ---------------------------------------------------------------------
    def rules(self, *, service_id: str | None = None, status: str | None = None) -> list[Rule]:
        return self._book.rules(service_id=service_id, status=status)

    def rule(self, rule_id: str) -> Rule | None:
        return self._book.get(rule_id)

    def rule_dict(self, rule: Rule) -> dict[str, Any]:
        now = self._now()
        uses = self._ledger.usage(rule.id, "", 0, now).hour
        return rule.to_dict(now=now, uses_last_hour=uses)

    @property
    def paused(self) -> bool:
        return self._book.paused

    def now(self) -> float:
        return self._now()

    @property
    def audit_log(self):
        """The audit log the manager writes to (read by ``GET /api/audit``)."""
        return self._store.audit

    def status(self) -> ApprovalStatus:
        return ApprovalStatus(
            paused=self._book.paused, paused_by=self._book.paused_by, paused_at=self._book.paused_at,
            active_rules=len(self._book.rules(status="active")),
            suspended_rules=len(self._book.rules(status="suspended")),
            cooldown_seconds=self._settings.autofix_cooldown,
            proposal_ttl_hours=self._settings.proposal_ttl / 3600.0, bounds=BOUNDS)

    def _expired_by_clock(self, rule: Rule) -> bool:
        return rule.status in ("active", "suspended") and self._now() >= rule.expires_at

    # ---- widen operations (the caller has passed require_admin) -----------------------------------------
    async def create_rule(self, fields: RuleFields, *, actor: str, source_proposal_id: str | None = None) -> Rule:
        async with self._rules_lock:
            return await self._unit(self._create(fields, actor, source_proposal_id))

    async def _create(self, fields: RuleFields, actor: str, source_proposal_id: str | None) -> Rule:
        service = self._registry.get(fields.service_id)
        if service is None:
            raise RuleInvalid("service_id", "unknown service")
        if not service.code_folder:
            raise RuleInvalid("service_id", "service has no code folder")
        folder = service.code_folder
        now = self._now()
        who = safe_text(actor, 100)
        expires = fields.expires_at if fields.expires_at is not None else now + 7 * 86400.0
        if expires > now + MAX_EXPIRY_SECONDS:
            raise RuleInvalid("expires_at", "may be at most 30 days after the rule was created")

        def write(tx: Tx) -> Rule:
            per_service, total = rb.count_live_rules(tx, fields.service_id)
            if per_service >= MAX_RULES_PER_SERVICE or total >= MAX_RULES_TOTAL:
                raise RuleLimitReached(f"at most {MAX_RULES_PER_SERVICE} rules per service and "
                                       f"{MAX_RULES_TOTAL} in total")
            seq, rid = rb.issue_rule_id(tx)
            rule = Rule(
                id=rid, seq=seq, service_id=fields.service_id, match_type=fields.match_type,
                match_value=fields.match_value, most_urgent_severity=fields.most_urgent_severity,
                expires_at=float(expires), max_per_hour=fields.max_per_hour, max_per_incident=fields.max_per_incident,
                max_files=fields.max_files, max_lines=fields.max_lines, status="active", status_reason="",
                created_by=who, created_at=now, updated_at=now, source_proposal_id=source_proposal_id,
                note=clean_note(fields.note), use_count=0, code_folder=folder)
            rb.insert_rule(tx, rule)
            detail = f"from proposal {source_proposal_id}" if source_proposal_id else "created directly"
            self._audit(tx, who, "rule_created", rid, detail, service_id=rule.service_id, rule_id=rid,
                        proposal_id=source_proposal_id)
            return rule

        rule = await self._store.transact(write)
        self._book.replace(rule)                       # no await between the commit result and the mirror
        await self._say(rule_line("created", rule.id, rule.service_id, who))
        return rule

    async def patch_rule(self, rule_id: str, patch: RulePatch, *, actor: str) -> Rule:
        async with self._rules_lock:
            return await self._unit(self._patch(rule_id, patch, actor))

    async def _patch(self, rule_id: str, patch: RulePatch, actor: str) -> Rule:
        rule = self._book.get(rule_id)
        if rule is None:
            raise RuleNotFound(rule_id)
        if rule.status in ("revoked", "expired") or self._expired_by_clock(rule):
            raise RuleConflict(f"rule {rule_id} is {rule.status if rule.status != 'active' else 'expired'}")
        changes = {k: v for k, v in dataclasses.asdict(patch).items() if v is not None}
        if "expires_at" in changes and not self._now() < changes["expires_at"] <= rule.created_at + MAX_EXPIRY_SECONDS:
            raise RuleInvalid("expires_at", "must be in the future and at most 30 days after the rule was created")
        if "note" in changes:
            changes["note"] = clean_note(changes["note"])
        for name, low, high in (("most_urgent_severity", 3, 4), ("max_per_hour", 1, BOUNDS["max_per_hour"]["max"]),
                                ("max_per_incident", 1, 3), ("max_files", 1, 20), ("max_lines", 1, 500)):
            if name in changes and not low <= changes[name] <= high:
                raise RuleInvalid(name, f"must be from {low} to {high}")
        changes = {k: v for k, v in changes.items() if getattr(rule, k) != v}
        if not changes:
            return rule
        version = self._book.version(rule_id)
        now = self._now()
        new = dataclasses.replace(rule, updated_at=now, **changes)
        who = safe_text(actor, 100)
        detail = "; ".join(f"{k}: {getattr(rule, k)} -> {v}" if k != "note" else "note changed"
                           for k, v in changes.items())

        def write(tx: Tx) -> None:
            if rb.patch_row(tx, new, now) == 0:
                raise RuleConflict(f"rule {rule_id} changed while updating")
            self._audit(tx, who, "rule_updated", rule_id, detail, service_id=rule.service_id, rule_id=rule_id)

        await self._store.transact(write)
        current = self._book.get(rule_id)
        if current is None or self._book.version(rule_id) != version:
            self._book.mark_dirty(rule_id)
            raise RuleConflict(f"rule {rule_id} changed while updating")
        self._book.replace(new)
        return new

    async def enable_rule(self, rule_id: str, *, actor: str) -> Rule:
        async with self._rules_lock:
            return await self._unit(self._enable(rule_id, actor))

    async def _enable(self, rule_id: str, actor: str) -> Rule:
        rule = self._book.get(rule_id)
        if rule is None:
            raise RuleNotFound(rule_id)
        if rule.status != "suspended":
            raise RuleConflict(f"rule {rule_id} is {rule.status}, only a suspended rule can be re-enabled")
        if self._expired_by_clock(rule):
            await self._expire_rules([rule])
            raise RuleConflict(f"rule {rule_id} has expired")
        service = self._registry.get(rule.service_id)
        if service is None:
            raise RuleConflict("the service of this rule is gone")
        if service.code_folder != rule.code_folder:
            raise RuleConflict("the code folder of the service changed; create a new rule")
        version = self._book.version(rule_id)
        now = self._now()
        who = safe_text(actor, 100)

        def write(tx: Tx) -> None:
            if rb.enable_row(tx, rule_id, now) == 0:
                raise RuleConflict(f"rule {rule_id} changed while enabling")
            self._audit(tx, who, "rule_enabled", rule_id, "", service_id=rule.service_id, rule_id=rule_id)

        await self._store.transact(write)
        if self._book.version(rule_id) != version:
            self._book.mark_dirty(rule_id)          # the database may now hold a looser state than the mirror
            raise RuleConflict(f"rule {rule_id} changed while enabling")
        new = dataclasses.replace(rule, status="active", status_reason="", updated_at=now)
        self._book.replace(new)
        await self._say(rule_line("enabled", rule_id, rule.service_id, who))
        return new

    async def resume(self, *, actor: str) -> ApprovalStatus:
        async with self._rules_lock:
            return await self._unit(self._resume(actor))

    async def _resume(self, actor: str) -> ApprovalStatus:
        if not self._book.paused:
            return self.status()
        version = self._book.pause_version
        who = safe_text(actor, 100)

        def write(tx: Tx) -> None:
            rb.resume_row(tx)
            self._audit(tx, who, "rules_resumed", "all")

        await self._store.transact(write)
        if self._book.pause_version != version:
            self._book.mark_paused_dirty()
            raise RuleConflict("auto-approval was paused again while resuming")
        self._book.set_paused(False, "", None)
        await self._say(rule_line("resumed", "", "", who))
        return self.status()

    # ---- reduce operations (token-free, always executed, mirror first) ------------------------------------
    async def _gate(self, key: str, **row: Any) -> bool:
        """The limiter decides whether this call gets its own audit row and announcement. Never raises."""
        try:
            return bool(await self._limiter.record(key, **row))
        except Exception:  # noqa: BLE001 - the action goes ahead; at worst it is announced
            log.exception("the reduce-operations limiter failed")
            return True

    async def revoke_rule(self, rule_id: str, *, actor: str, key: str) -> Rule:
        rule = self._book.get(rule_id)
        if rule is None:
            raise RuleNotFound(rule_id)
        if rule.status in ("revoked", "expired"):
            return rule                                  # already final: nothing to write or announce
        who = safe_text(actor, 100)
        now = self._now()
        revoked = dataclasses.replace(rule, status="revoked", status_reason=f"revoked by {who}"[:100], updated_at=now)
        self._book.replace(revoked)                      # before any await: the next prepare already refuses
        self._book.mark_dirty(rule_id)
        speak = await self._gate(key, actor=who, action="rule_revoked", subject=rule_id, detail="",
                                 service_id=rule.service_id, rule_id=rule_id)
        await self._persist_rule(rule_id, lambda tx: rb.revoke_row(tx, rule_id, revoked.status_reason, now))
        await self._deny_open_proposals(rule_id, f"rule-revoked:{rule_id}")
        if speak:
            await self._say(rule_line("revoked", rule_id, rule.service_id, who))
        return revoked

    async def pause_rule(self, rule_id: str, *, actor: str, key: str) -> Rule:
        rule = self._book.get(rule_id)
        if rule is None:
            raise RuleNotFound(rule_id)
        if rule.status != "active":
            return rule
        who = safe_text(actor, 100)
        now = self._now()
        paused = dataclasses.replace(rule, status="suspended", status_reason=f"paused by {who}"[:100], updated_at=now)
        self._book.replace(paused)
        self._book.mark_dirty(rule_id)
        speak = await self._gate(key, actor=who, action="rule_paused", subject=rule_id, detail="",
                                 service_id=rule.service_id, rule_id=rule_id)
        await self._persist_rule(rule_id, lambda tx: rb.suspend_row(tx, rule_id, paused.status_reason, now))
        await self._deny_open_proposals(rule_id, f"rule-suspended:{rule_id}")
        if speak:
            await self._say(rule_line("rule_paused", rule_id, rule.service_id, who))
        return paused

    async def pause(self, *, actor: str, key: str) -> ApprovalStatus:
        if self._book.paused:
            return self.status()
        who = safe_text(actor, 100)
        now = self._now()
        self._book.set_paused(True, who, now)
        self._book.mark_paused_dirty()
        speak = await self._gate(key, actor=who, action="rules_paused", subject="all", detail="")
        try:
            await self._store.transact(lambda tx: rb.set_paused_row(tx, True, who, now))
            if self._book.paused:
                self._book.clear_paused_dirty()
        except StoreError:
            self._warn_once_a_minute("could not persist the pause flag; the sweep will retry")
        if speak:
            await self._say(rule_line("paused", "", "", who))
        return self.status()

    async def _persist_rule(self, rule_id: str, fn: Callable[[Tx], int]) -> None:
        """Write a reduce to the database. A failure leaves the rule dirty for the sweep (design O5)."""
        try:
            await self._store.transact(fn)
        except StoreError:
            self._warn_once_a_minute("could not persist a rule change; the sweep will retry")
            return
        self._book.clear_dirty(rule_id)

    async def withdraw(self, proposal_id: str, *, actor: str, key: str) -> Proposal:
        prop = self._state.get_proposal(proposal_id)
        if prop is None:
            raise NotFound(f"proposal {proposal_id} not found")
        if prop.status != "pending":
            return prop
        who = safe_text(actor, 40)
        speak = await self._gate(key, actor=who, action="proposal_withdrawn", subject=proposal_id, detail="",
                                 service_id=prop.service_id, proposal_id=proposal_id)
        try:
            moved = await self._writer.hub_transition(proposal_id, "denied", decided_by=f"withdrawn:{who}"[:60])
        except InvalidTransition:
            moved = None
        if moved is None:
            return self._state.get_proposal(proposal_id) or prop
        if speak:
            await self._say(rule_line("withdrawn", proposal_id, prop.service_id or "", who))
        return moved

    async def _deny_open_proposals(self, rule_id: str, decided_by: str) -> None:
        """Deny every ``approved`` proposal of the rule. Each is its own commit; a failure is left to the sweep."""
        for prop in self._state.list_proposals("approved"):
            if prop.approved_by_rule != rule_id:
                continue
            try:
                await self._writer.hub_transition(prop.id, "denied", decided_by=decided_by)
            except (InvalidTransition, NotFound):
                continue
            except StoreError:
                self._warn_once_a_minute("could not deny proposals of rule %s; the sweep will retry", rule_id)
                return

    # ---- rules from a proposal (R3) ------------------------------------------------------------------------
    def rule_defaults(self, proposal_id: str, *, now: float | None = None) -> RuleDefaults:
        prop = self._state.get_proposal(proposal_id)
        if prop is None:
            raise NotFound(f"proposal {proposal_id} not found")
        if prop.approved_by_rule:
            raise RuleNotApplicable("this proposal was approved by a rule, not by a person")
        if prop.status == "pending":
            raise RuleNotApplicable("the proposal is still pending")
        if prop.status == "denied":
            raise RuleNotApplicable("the proposal was denied")
        if prop.status == "done" and prop.verification_result != "passed":
            raise RuleNotApplicable("the fix was not verified as passed")
        inc = self._state.get_incident(prop.incident_id)
        if inc is None:
            raise RuleInvalid("incident_id", "the incident of this proposal is no longer known")
        if not inc.service_id:
            raise RuleInvalid("service_id", "the incident is not tagged to a service")
        service = self._registry.get(inc.service_id)
        if service is None or not service.code_folder:
            raise RuleInvalid("service_id", "the service has no code folder")
        at = self._now() if now is None else now
        return RuleDefaults(
            service_id=inc.service_id, match_type="signature", match_value=inc.signature[:200],
            most_urgent_severity=min(4, max(3, int(inc.severity))), expires_at=at + 7 * 86400.0,
            max_per_hour=BOUNDS["max_per_hour"]["default"], max_per_incident=BOUNDS["max_per_incident"]["default"],
            max_files=BOUNDS["max_files"]["default"], max_lines=BOUNDS["max_lines"]["default"],
            note=f"from {proposal_id}", source_proposal_id=proposal_id)

    async def create_rule_from_proposal(self, proposal_id: str, overrides: Mapping[str, object], *,
                                        actor: str) -> Rule:
        from .validate import validate_rule_fields

        defaults = self.rule_defaults(proposal_id)
        raw: dict[str, object] = {k: v for k, v in defaults.to_dict().items() if k != "source_proposal_id"}
        for key, value in overrides.items():
            if key in ("service_id", "source_proposal_id"):
                raise RuleInvalid(key, "cannot be changed when a rule is made from a proposal")
            raw[key] = value
        fields = validate_rule_fields(raw, now=self._now())
        return await self.create_rule(fields, actor=actor, source_proposal_id=proposal_id)

    # ---- rule expiry and denial helpers -----------------------------------------------------------------------
    @staticmethod
    def _denial_for(rule: Rule) -> str:
        word = {"suspended": "suspended", "revoked": "revoked", "expired": "expired"}.get(rule.status, "suspended")
        return f"rule-{word}:{rule.id}"

    async def _expire_rules(self, rules: Sequence[Rule]) -> None:
        for rule in rules:
            current = self._book.get(rule.id)
            if current is None or current.status not in ("active", "suspended"):
                continue
            now = self._now()
            self._book.replace(dataclasses.replace(current, status="expired", status_reason="expired", updated_at=now))
            self._book.mark_dirty(rule.id)

            def write(tx: Tx, rid: str = rule.id, sid: str = rule.service_id) -> int:
                changed = rb.expire_row(tx, rid, now)
                if changed:
                    self._audit(tx, "hub", "rule_expired", rid, "", service_id=sid, rule_id=rid)
                return changed

            await self._persist_rule(rule.id, write)
            await self._deny_open_proposals(rule.id, f"rule-expired:{rule.id}")
            await self._say(rule_line("expired", rule.id, rule.service_id, "hub"))

    # ---- registry subscriptions (R6.9) ------------------------------------------------------------------------
    def _suspend_in_tx(self, tx: Tx, service_id: str, reason: str, *, only_active: bool) -> None:
        """Writer thread, inside the registry's transaction: uses only ``tx`` and the immutable arguments."""
        ids = rb.active_rule_ids(tx, service_id) if only_active else [rid for rid, _ in rb.live_rule_ids(tx, service_id)]
        if not ids:
            return
        rb.suspend_service_rules(tx, service_id, reason, self._store.now(), only_active=only_active)
        for rid in ids:
            self._audit(tx, "hub", "rule_suspended", rid, reason, service_id=service_id, rule_id=rid)

    def _on_code_folder_changed(self, tx: Tx, change: ServiceChange) -> None:
        self._suspend_in_tx(tx, change.service_id, "code_folder_changed", only_active=True)

    def _on_service_deleted(self, tx: Tx, change: ServiceChange) -> None:
        self._suspend_in_tx(tx, change.service_id, "service_deleted", only_active=False)

    async def _on_after_commit(self, change: ServiceChange) -> None:
        if change.kind == "service_deleted" or (change.kind == "service_updated" and change.code_folder_changed):
            await self._reload(announce=True)

    async def _reload(self, *, announce: bool = False) -> list[str]:
        """Re-read the rules and merge tighten-only (rule 5). Returns the ids that changed."""
        started = self._book.epoch

        def read(tx: Tx) -> Snapshot:
            paused, by, at = rb.load_state(tx)
            return Snapshot(tuple(rb.load_rules(tx)), paused, by, at)

        snapshot = await self._store.read(read)
        before = {r.id: r.status for r in self._book.rules()}
        changed = self._book.merge(snapshot, started)
        for rid in changed:
            rule = self._book.get(rid)
            if rule is None or rule.status == "active":
                continue
            if before.get(rid, "active") == "active":
                await self._deny_open_proposals(rid, self._denial_for(rule))
                if announce:
                    await self._say(rule_line("suspended", rid, rule.service_id, "hub", rule.status_reason))
        return changed

    # ---- sweep and expiry on read (R6.4, R6.8, R9.8) --------------------------------------------------------------
    async def start(self) -> None:
        if self._sweep_task is None and not self._closed:
            self._sweep_task = asyncio.get_running_loop().create_task(self._sweep_loop())

    async def aclose(self) -> None:
        self._closed = True
        task, self._sweep_task = self._sweep_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(self._settings.sweep_interval)
            try:
                await self.sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("approvals sweep failed")

    async def sweep_once(self) -> None:
        async with self._sweep_lock:
            for name in SWEEP_STEPS:
                try:
                    await getattr(self, name)()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - one failing step never stops the others
                    self._warn_once_a_minute("approvals sweep step %s failed", name)

    async def expire_due(self) -> None:
        """Steps 2 to 4 on read. Bounded, never raises, returns at once while the sweep runs (review M5)."""
        if self._sweep_lock.locked():
            return
        task = asyncio.ensure_future(self._expire_work())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(_retrieve)
        try:
            await asyncio.wait_for(asyncio.shield(task), self._expire_timeout)
        except asyncio.TimeoutError:
            return                                      # the work keeps running; the caller goes on
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - includes StoreError: reads stay 200
            self._warn_once_a_minute("expire_due failed; reads continue without it")

    async def _expire_work(self) -> None:
        async with self._sweep_lock:
            for name in ("_step_expire", "_step_deny_stale", "_step_ttl"):
                try:
                    await getattr(self, name)()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    self._warn_once_a_minute("approvals step %s failed", name)

    async def _step_persist_dirty(self) -> None:
        now = self._now()
        for rid in list(self._book.dirty):
            rule = self._book.get(rid)
            if rule is None or rule.status == "active":
                self._book.clear_dirty(rid)
                continue
            version = self._book.version(rid)

            def write(tx: Tx, rule: Rule = rule) -> int:
                if rule.status == "revoked":
                    return rb.revoke_row(tx, rule.id, rule.status_reason, now)
                if rule.status == "expired":
                    return rb.expire_row(tx, rule.id, now)
                return rb.suspend_row(tx, rule.id, rule.status_reason, now)

            await self._store.transact(write)
            if self._book.version(rid) == version:
                self._book.clear_dirty(rid)
        if self._book.paused_dirty:
            if self._book.paused:
                await self._store.transact(lambda tx: rb.set_paused_row(tx, True, self._book.paused_by,
                                                                        self._book.paused_at or now))
            self._book.clear_paused_dirty()

    async def _step_expire(self) -> None:
        due = [r for r in self._book.rules() if self._expired_by_clock(r)]
        if due:
            await self._expire_rules(due)

    def _approved_age_start(self, prop: Proposal) -> float | None:
        at = self._ledger.approved_at(prop.id)
        return at if at is not None else prop.decided_at

    async def _step_deny_stale(self) -> None:
        now = self._now()
        for prop in self._state.list_proposals("approved"):
            if not prop.approved_by_rule:
                continue
            started = self._approved_age_start(prop)
            if started is not None and now - started > self._settings.rule_approval_ttl:
                try:
                    await self._writer.hub_transition(prop.id, "denied", decided_by=f"rule-expired:{prop.approved_by_rule}")
                except (InvalidTransition, NotFound):
                    continue

    async def _step_ttl(self) -> None:
        now = self._now()
        for prop in self._state.list_proposals("pending"):
            if now - prop.created > self._settings.proposal_ttl:
                try:
                    await self._writer.hub_transition(prop.id, "denied", decided_by="expired:ttl")
                except (InvalidTransition, NotFound):
                    continue

    async def _step_repair(self) -> None:
        for rule in self._book.rules():
            if rule.status != "active":
                await self._deny_open_proposals(rule.id, self._denial_for(rule))
        now = self._now()
        for pid in self._ledger.incomplete_proposals():
            prop = self._state.get_proposal(pid)
            if prop is not None and prop.status == "done":
                await self._store.transact(lambda tx, pid=pid: rb.complete_use(tx, pid, now))
                self._ledger.completed(pid, now)

    async def _step_reload(self) -> None:
        await self._reload(announce=True)

    async def _step_openings(self) -> None:
        now = self._now()
        for (incident_id, opening), seen in list(self._unsaved_openings.items()):
            await self._store.transact(lambda tx, i=incident_id, o=opening, s=seen: rb.insert_opening(tx, i, o, s))
            self._unsaved_openings.pop((incident_id, opening), None)
        for incident_id, opening, _pid in self._ledger.completed_uses():
            inc = self._state.get_incident(incident_id)
            if inc is None or inc.opening <= opening:
                continue
            if self._ledger.note_opening(incident_id, inc.opening, now):
                await self._store.transact(lambda tx, i=incident_id, o=inc.opening: rb.insert_opening(tx, i, o, now))

    async def _step_prune(self) -> None:
        now = self._now()
        self._ledger.prune(now)
        await self._store.transact(lambda tx: rb.prune_rows(tx, now - RETENTION_SECONDS))

    # ---- views (R4.6, R7.5) --------------------------------------------------------------------------------------
    def approval_of(self, prop: Proposal) -> dict[str, Any] | None:
        """Who approved ``prop``. ``by`` and ``rule_id`` follow the ``approved_by_rule`` column only."""
        rule_id = prop.approved_by_rule
        if rule_id:
            rule = self._book.get(rule_id)
            now = self._now()
            status_now: str | None = None
            if rule is not None:
                status_now = "expired" if self._expired_by_clock(rule) else rule.status
            started = self._ledger.approved_at(prop.id)
            expired = False
            if prop.status == "approved":
                begun = started if started is not None else prop.decided_at
                too_old = begun is None or now - begun > self._settings.rule_approval_ttl
                expired = too_old or status_now != "active"
            return {"by": "rule", "rule_id": rule_id, "rule_status_now": status_now, "approved_at": started,
                    "expired": expired}
        if prop.status in ("approved", "done"):
            return {"by": "user", "rule_id": None, "rule_status_now": None, "approved_at": prop.decided_at,
                    "expired": False}
        return None

    def proposal_view(self, prop: Proposal) -> dict[str, Any]:
        view = prop.to_full_dict()
        view["approval"] = self.approval_of(prop)
        return view

    # ---- the hub seam (ProposalApprover) -----------------------------------------------------------------------
    def shares_store(self, store: Store | None) -> bool:
        return store is not None and store is self._store

    def _active_of(self, incident_id: str) -> str | None:
        for status in ("pending", "approved"):
            for prop in self._state.list_proposals(status):
                if prop.incident_id == incident_id:
                    return prop.id
        return None

    def prepare(self, ctx: CreateContext) -> ApprovalPlan:
        """Validate, enforce limits, evaluate the rules, reserve quota. Synchronous and await-free."""
        now = self._now()
        service = self._registry.get(ctx.service_id) if ctx.service_id else None
        live_folder = service.code_folder if service is not None else None
        extra = validate_new_proposal(ctx.extra, code_folder=live_folder)        # raises before anything is reserved
        limit_token = self._limits.reserve(ctx.incident_id, ctx.service_id or "unassigned", now,
                                           enforce=ctx.enforce_limits, active_of=self._active_of)
        try:
            return self._decide_plan(ctx, extra, now, live_folder, limit_token)
        except BaseException:
            self._limits.release(limit_token)
            raise

    def _decide_plan(self, ctx: CreateContext, extra: dict[str, object], now: float, live_folder: str | None,
                     limit_token: int) -> ApprovalPlan:
        files = tuple(str(f) for f in (extra.get("files") or ()))
        # The first sighting of a reopened opening is recorded whether or not any rule exists (O2, H1).
        first_seen: float | None = None
        prior: str | None = None
        if ctx.opening > 1 and any(u.opening < ctx.opening for u in self._ledger.uses_of(ctx.incident_id)):
            if self._ledger.note_opening(ctx.incident_id, ctx.opening, now):
                first_seen = now
                self._unsaved_openings[(ctx.incident_id, ctx.opening)] = now
            prior = self._ledger.cooldown_for(ctx.incident_id, ctx.opening, self._settings.autofix_cooldown)
        live = [r for r in self._book.rules(service_id=ctx.service_id) if r.status == "active"] if ctx.service_id else []
        kind: str = "pending"
        checks: tuple[RuleCheck, ...] = ()
        rule_id: str | None = None
        token = 0
        remaining_hour = remaining_incident = None
        paused_skip = False
        if self._book.paused:
            paused_skip = bool(live)
        elif live:
            try:
                problem: str | None = None
                if files and live_folder:
                    found = check_files(live_folder, files, protected_dirs=self._protected)
                    problem = found[0].code if found else None
                mctx = MatchContext(
                    now=now, paused=False, kind=extra.get("kind") if isinstance(extra.get("kind"), str) else None,
                    service_id=ctx.service_id, signature=ctx.signature, category=ctx.category, severity=ctx.severity,
                    files=files or None, estimated_lines=extra.get("estimated_lines"),            # type: ignore[arg-type]
                    verification=extra.get("verification"),                                        # type: ignore[arg-type]
                    file_problem=problem, live_code_folder=live_folder, cooldown_active=prior is not None)
                rule, checks = first_match(live, mctx,
                                           lambda rid: self._ledger.usage(rid, ctx.incident_id, ctx.opening, now))
                if rule is not None:
                    usage = self._ledger.usage(rule.id, ctx.incident_id, ctx.opening, now)
                    token = self._ledger.reserve(rule.id, ctx.incident_id, ctx.opening, now)
                    kind, rule_id = "auto", rule.id
                    remaining_hour = max(0, rule.max_per_hour - usage.hour - 1)
                    remaining_incident = max(0, rule.max_per_incident - usage.incident_opening - 1)
                elif prior is not None and any(c.failed == "cooldown" for c in checks):
                    kind = "cooldown"
                    rule_id = next(c.rule_id for c in checks if c.failed == "cooldown")
            except Exception:  # noqa: BLE001 - a failure in matching is a refusal, never an approval
                log.exception("rule matching failed; the proposal stays pending")
                kind, rule_id, token = "pending", None, 0
                checks = (RuleCheck(live[0].id, "error"),)
        return ApprovalPlan(
            kind=kind, extra=dict(extra), rule_id=rule_id, checks=checks, incident_id=ctx.incident_id,  # type: ignore[arg-type]
            service_id=ctx.service_id, opening=ctx.opening, files=files, remaining_hour=remaining_hour,
            remaining_incident=remaining_incident, prior_proposal_id=prior, reserved_at=now,
            opening_first_seen=first_seen, token=token, limit_token=limit_token, paused_skip=paused_skip,
            audit_append=self._store.audit.append_tx)

    def confirm(self, plan: ApprovalPlan, prop: Proposal) -> str | None:
        """After the commit and the mirror update: bind the reservation, re-read the rule (review M1)."""
        self._limits.commit(plan.limit_token)
        if plan.opening_first_seen is not None:
            self._unsaved_openings.pop((plan.incident_id, plan.opening), None)
        if plan.kind != "auto" or plan.rule_id is None:
            return None
        self._ledger.bind(plan.token, prop.id)
        rule = self._book.get(plan.rule_id)
        if rule is None:
            return f"rule-suspended:{plan.rule_id}"
        if self._expired_by_clock(rule):
            return f"rule-expired:{plan.rule_id}"
        if rule.status != "active":
            return self._denial_for(rule)
        return None

    def abort(self, plan: ApprovalPlan) -> None:
        self._limits.release(plan.limit_token)
        if plan.token:
            self._ledger.release(plan.token)

    async def persist(self, plan: ApprovalPlan, prop: Proposal) -> None:
        """Bare hub: the plan's rows in the manager's own transaction. Never raises."""
        try:
            await self._store.transact(lambda tx: plan.write_in_tx(tx, prop))
            self._unsaved_openings.pop((plan.incident_id, plan.opening), None)
        except Exception:  # noqa: BLE001
            self._warn_once_a_minute("could not persist the auto-approval record of %s; it stays in memory", prop.id)

    def line_for(self, plan: ApprovalPlan, base_line: str, prop: Proposal) -> str:
        if plan.kind == "auto" and plan.rule_id is not None:
            if prop.status == "denied":
                reason = {"rule-revoked": "was revoked", "rule-suspended": "is suspended",
                          "rule-expired": "has expired"}.get(prop.decided_by.split(":", 1)[0], "is no longer active")
                return rule_line("denied", prop.id, plan.rule_id, "hub", reason)
            return auto_approved_line(prop.id, plan.rule_id, plan.service_id or "", prop.action)
        if plan.kind == "cooldown" and plan.prior_proposal_id:
            return base_line + cooldown_note(plan.prior_proposal_id, plan.rule_id or "")
        return base_line

    async def before_complete(self, pid: str) -> None:
        await self.expire_due()

    def validate_completion(self, pid: str, extra: Mapping[str, object] | None) -> Mapping[str, object]:
        clean = validate_completion(extra)
        prop = self._state.get_proposal(pid)
        if prop is not None and prop.approved_by_rule and prop.status == "approved":
            started = self._approved_age_start(prop)
            if started is not None and self._now() - started > self._settings.rule_approval_ttl:
                raise ProposalExpired(f"the approval of {pid} expired; propose the fix again")
        return clean

    async def after_complete(self, prop: Proposal, extra: Mapping[str, object]) -> None:
        """R6.5 and R6.6: check what was changed against the rule. Never raises."""
        try:
            await self._after_complete(prop, extra)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("completion check of %s failed", prop.id)

    async def _after_complete(self, prop: Proposal, extra: Mapping[str, object]) -> None:
        rule_id = prop.approved_by_rule
        if not rule_id:
            return                                          # a user approval is not checked
        now = self._now()
        self._ledger.completed(prop.id, now)
        rule = self._book.get(rule_id)
        files = list(extra.get("files_changed") or prop.files_changed or [])
        lines = extra.get("lines_changed", prop.lines_changed)
        result = extra.get("verification_result", prop.verification_result)
        reason: str | None = None
        if rule is not None:
            if files and check_files(rule.code_folder, files, protected_dirs=self._protected):
                reason = "completion_out_of_scope"
            elif len(files) > rule.max_files:
                reason = "files_over_limit"
            elif isinstance(lines, int) and lines > rule.max_lines:
                reason = "lines_over_limit"
            elif result == "failed":
                reason = "verification_failed"
        unverified = not files and lines is None and result is None
        action = "completion_unverified" if unverified else "completion_checked"
        sid = prop.service_id

        def write(tx: Tx) -> None:
            rb.complete_use(tx, prop.id, now)
            self._audit(tx, "hub", action if reason is None else "completion_checked", prop.id,
                        "" if reason is None else f"violation={reason}", service_id=sid, rule_id=rule_id,
                        proposal_id=prop.id)
            if reason is not None:
                rb.suspend_row(tx, rule_id, reason, now)
                self._audit(tx, "hub", "rule_suspended", rule_id, reason, service_id=sid, rule_id=rule_id)

        if reason is not None and rule is not None and rule.status == "active":
            self._book.replace(dataclasses.replace(rule, status="suspended", status_reason=reason, updated_at=now))
            self._book.mark_dirty(rule_id)
        try:
            await self._store.transact(write)
            if reason is not None:
                self._book.clear_dirty(rule_id)
        except StoreError:
            self._warn_once_a_minute("could not record the completion check of %s", prop.id)
        if reason is not None and rule is not None:
            await self._deny_open_proposals(rule_id, f"rule-suspended:{rule_id}")
            await self._say(rule_line("suspended", rule_id, rule.service_id, "hub", reason))
