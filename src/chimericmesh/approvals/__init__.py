"""Standing approval rules: hub-side matching, limits, audit and the token-free emergency stop.

This file imports no web framework; ``approvals.routes`` is imported by ``api.py`` only.
"""
from __future__ import annotations

from .errors import (
    ActiveProposalExists,
    ApprovalsError,
    ApprovalsNotMigrated,
    ProposalExpired,
    ProposalInvalid,
    ProposalRateLimited,
    RuleConflict,
    RuleInvalid,
    RuleLimitReached,
    RuleNotApplicable,
    RuleNotFound,
)

__all__ = [
    "ActiveProposalExists", "ApprovalsError", "ApprovalsNotMigrated", "ProposalExpired", "ProposalInvalid",
    "ProposalRateLimited", "RuleConflict", "RuleInvalid", "RuleLimitReached", "RuleNotApplicable",
    "RuleNotFound",
]
