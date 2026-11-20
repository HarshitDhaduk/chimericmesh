"""The registry of file-line formats."""
from __future__ import annotations

from .logctx import LineFormat, identity_update
from .ncsa import parse_ncsa
from .plain import parse_plain
from .syslogtext import parse_syslog_text
from .w3c import parse_w3c, update_w3c
from .xferlog import parse_xferlog

FORMATS: dict[str, LineFormat] = {
    "syslog-text": LineFormat("syslog-text", identity_update, parse_syslog_text),
    "ncsa": LineFormat("ncsa", identity_update, parse_ncsa),
    "w3c": LineFormat("w3c", update_w3c, parse_w3c),
    "xferlog": LineFormat("xferlog", identity_update, parse_xferlog),
    "plain": LineFormat("plain", identity_update, parse_plain),
}
FORMAT_NAMES: tuple[str, ...] = tuple(FORMATS)


def get_format(name: str) -> LineFormat:
    """The format called ``name``; ``KeyError`` for an unknown name."""
    return FORMATS[name]
