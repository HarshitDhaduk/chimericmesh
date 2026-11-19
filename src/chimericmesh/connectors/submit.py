"""``submit``: stamp the log source on an event and hand it to the injected ``ingest``."""
from __future__ import annotations

import dataclasses
from collections.abc import Awaitable, Callable

from ..events import Event

Ingest = Callable[[Event], Awaitable[Event | None]]
Submit = Callable[[Event, str | None], Awaitable[Event]]


def make_submit(ingest: Ingest) -> Submit:
    """Build ``submit(event, log_source_id=None)`` around the hub's ``ingest``.

    The service is assigned by the hub's tagger, never here. An exception from ``ingest`` propagates:
    the callers (tailers, TCP handler) count and log it.
    """

    async def submit(event: Event, log_source_id: str | None = None) -> Event:
        if log_source_id is not None:
            event = dataclasses.replace(event, log_source_id=log_source_id)
        result = await ingest(event)
        return result if isinstance(result, Event) else event

    return submit
