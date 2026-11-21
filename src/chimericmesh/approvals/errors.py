"""Errors of the auto-approval module. REST maps each one to a status; none carries log text."""
from __future__ import annotations


class ApprovalsError(Exception):
    """Base class."""


class ProposalInvalid(ApprovalsError):
    """A proposal or completion field is out of bounds (REST 422)."""

    def __init__(self, field: str, reason: str) -> None:
        self.field = field
        self.reason = reason
        super().__init__(f"{field}: {reason}")


class RuleInvalid(ApprovalsError):
    """A rule field is out of bounds (REST 422)."""

    def __init__(self, field: str, reason: str) -> None:
        self.field = field
        self.reason = reason
        super().__init__(f"{field}: {reason}")


class ActiveProposalExists(ApprovalsError):
    """The incident already has a pending or approved proposal (REST 409)."""

    def __init__(self, proposal_id: str) -> None:
        self.proposal_id = proposal_id
        super().__init__(f"incident already has active proposal {proposal_id}")


class ProposalRateLimited(ApprovalsError):
    """The service reached its proposals per hour (REST 429)."""

    def __init__(self, retry_after: int) -> None:
        self.retry_after = max(1, int(retry_after))
        super().__init__(f"too many proposals for this service, retry in {self.retry_after}s")


class RuleNotFound(ApprovalsError):
    """Unknown rule id (REST 404)."""


class RuleConflict(ApprovalsError):
    """The rule is not in a state that allows the operation (REST 409)."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class RuleNotApplicable(ApprovalsError):
    """A rule cannot be made from this proposal (REST 409)."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class RuleLimitReached(ApprovalsError):
    """20 rules for the service or 100 in total (REST 409)."""


class ProposalExpired(ApprovalsError):
    """A rule approval older than its validity cannot be completed (REST 409)."""


class ApprovalsNotMigrated(ApprovalsError):
    """The store was opened without the approvals migrations."""
