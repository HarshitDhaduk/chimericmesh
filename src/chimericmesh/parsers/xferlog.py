"""``xferlog`` format: the standard FTP transfer log of wu-ftpd and vsftpd."""
from __future__ import annotations

import re

from .logctx import FormatContext, ParseOutcome, clean_line, make_file_event
from .plain import plain_outcome

# Right-anchored over the 14 standard fields; the file name (field 5) may contain spaces.
_XFER = re.compile(
    r"^\w{3} \w{3} [ \d]\d \d{2}:\d{2}:\d{2} \d{4} (\d+) (\S+) (\d+) (.+) ([ab]) ([CUT_]) ([oid]) ([agr]) "
    r"(\S+) (\S+) ([01]) (\S+) ([ci])$"
)
_DIRECTION = {"o": "download", "i": "upload", "d": "delete"}


def parse_xferlog(line: str, ctx: FormatContext) -> ParseOutcome:
    text = clean_line(line)
    if not text.strip():
        return ParseOutcome(None, True)
    m = _XFER.match(text.strip())
    if m is None:
        return plain_outcome(line, ctx, False)
    complete = m.group(13) == "c"
    event = make_file_event(
        ctx,
        severity=6 if complete else 4,
        message=f"{_DIRECTION[m.group(7)]} {m.group(4)} ({'complete' if complete else 'incomplete'})",
        raw=text,
    )
    return ParseOutcome(event, True)
