"""Errors of the agent loop. The REST layer maps each to a status code; messages carry no log text."""
from __future__ import annotations


class AgentError(Exception):
    """Base class."""


class UnknownService(AgentError):
    """Unknown service id, or the ``unassigned`` pseudo service (404)."""


class UnknownProposal(AgentError):
    """Unknown proposal id (404)."""


class NoCodeFolder(AgentError):
    """The service has no code folder (422)."""


class InvalidQuery(AgentError):
    """A query or test command that is empty, too long or unsafe (422)."""


class TooManyWaiters(AgentError):
    """All long-poll slots are taken (429 with ``Retry-After``)."""

    def __init__(self, retry_after: int = 5) -> None:
        super().__init__("too many waiters")
        self.retry_after = retry_after


class SearchBusy(AgentError):
    """Two code searches are already running (429)."""

    def __init__(self, retry_after: int = 2) -> None:
        super().__init__("search busy")
        self.retry_after = retry_after
