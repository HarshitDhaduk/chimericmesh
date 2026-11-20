"""``plain`` format: the whole line is the message."""
from __future__ import annotations

from ..events import infer_severity
from .logctx import FormatContext, ParseOutcome, clean_line, make_file_event


def plain_outcome(line: str, ctx: FormatContext, matched: bool) -> ParseOutcome:
    """The ``plain`` result for a non-blank line; ``matched`` says whether it fit the declared format."""
    text = clean_line(line)
    if not text.strip():
        return ParseOutcome(None, True)
    return ParseOutcome(make_file_event(ctx, severity=infer_severity(text, 6), message=text, raw=text), matched)


def parse_plain(line: str, ctx: FormatContext) -> ParseOutcome:
    return plain_outcome(line, ctx, True)
