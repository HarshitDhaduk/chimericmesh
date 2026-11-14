"""Replay real public IRC logs into a running mesh through its IRC server.

Data: Ubuntu IRC channel logs (https://irclogs.ubuntu.com/), described by the Common
Pile dataset card (huggingface.co/datasets/common-pile/ubuntu_irc) as released into
the public domain. Files are downloaded at run time into ``data/`` (git-ignored).

Each speaker gets their own IRC connection so the mesh sees a genuine sender. Nicknames
belong to real people, so they are replaced with pseudonyms by default (``--no-mask``
disables this).

Usage:
    python -m chimericmesh.replay_irc --dataset ubuntu-server --rate 30
    python -m chimericmesh.replay_irc --file chat.txt --channel #ops --limit 200
"""

from __future__ import annotations

import argparse
import re
import socket
import sys
import time
from pathlib import Path
from typing import Iterable

from .parsers import format_privmsg
from .replay import download

# Fixed allowlist: the downloader never fetches an arbitrary URL.
IRC_DATASETS = {
    "ubuntu-server": ("https://irclogs.ubuntu.com/2015/04/17/%23ubuntu-server.txt", "#ubuntu-server"),
}
CITATION = (
    "Data: Ubuntu IRC logs (irclogs.ubuntu.com), public domain per the Common Pile "
    "dataset card. Nicknames are masked."
)

_LINE = re.compile(r"^\[(\d{2}:\d{2})\] <([^>\s]+)> (.*)$")
_BAD_NICK = re.compile(r"[^A-Za-z0-9_\-\[\]\\`^{|}]")
_CHANNEL = re.compile(r"^#[\w\-]{1,48}$")

# Letters only on purpose: a pseudonym containing a digit or dash would be misread
# by the mesh as a hostname when someone is addressed as "Name: ...".
_NAMES = ["Wraith", "Banshee", "Specter", "Ghoul", "Phantom", "Revenant", "Poltergeist", "Shade",
          "Lich", "Vampyr", "Mummy", "Goblin", "Gorgon", "Harpy", "Kelpie", "Sylph", "Wisp",
          "Zombie", "Ghast", "Wendigo", "Dybbuk", "Draugr", "Strigoi", "Nuckelavee"]


def parse_ubuntu_line(line: str) -> tuple[str, str] | None:
    """Return (nick, text) for a chat line; joins, parts, actions and noise give None."""
    m = _LINE.match(line.rstrip("\r\n"))
    if not m:
        return None
    nick, text = m.group(2), m.group(3).strip()
    return (nick, text) if text else None


def safe_nick(nick: str) -> str:
    return _BAD_NICK.sub("", nick)[:30] or "anon"


def _letters(n: int) -> str:
    out = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        out = chr(ord("a") + r) + out
    return out


class Masker:
    """Deterministic nickname pseudonymizer (letters only, order of first appearance)."""

    def __init__(self, nicks: Iterable[str]) -> None:
        self.map: dict[str, str] = {}
        for nick in nicks:
            if nick not in self.map:
                i = len(self.map)
                self.map[nick] = _NAMES[i % len(_NAMES)] + _letters(i // len(_NAMES))
        # Whole-word replacement only for nicks long enough not to mangle ordinary words.
        long_nicks = sorted((n for n in self.map if len(n) >= 4), key=len, reverse=True)
        self._word = re.compile(r"(?<![\w])(" + "|".join(map(re.escape, long_nicks)) + r")(?![\w])") if long_nicks else None
        self._addr = re.compile(r"^(" + "|".join(map(re.escape, sorted(self.map, key=len, reverse=True))) + r")(?=[:,])") if self.map else None

    def nick(self, nick: str) -> str:
        return self.map.get(nick, nick)

    def text(self, text: str) -> str:
        if self._addr:
            text = self._addr.sub(lambda m: self.map[m.group(1)], text)
        if self._word:
            text = self._word.sub(lambda m: self.map[m.group(1)], text)
        return text


def load_chat(path: Path, limit: int | None = None) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            parsed = parse_ubuntu_line(line)
            if parsed:
                out.append(parsed)
                if limit is not None and len(out) >= limit:
                    break
    return out


class Session:
    """One IRC client connection per speaker."""

    def __init__(self, addr: tuple[str, int], nick: str, channel: str) -> None:
        self.sock = socket.create_connection(addr, timeout=5)
        self.nick = nick
        self._send(f"NICK {nick}")
        self._send(f"USER {nick} 0 * :{nick}")
        self._send(f"JOIN {channel}")

    def _send(self, line: str) -> None:
        # Only fixed commands and sanitized nicks/channels reach here; message
        # bodies always go through format_privmsg below.
        self.sock.sendall(line.encode("utf-8", errors="replace") + b"\r\n")

    def say(self, channel: str, text: str) -> None:
        for line in format_privmsg(channel, text):
            self._send(line)

    def close(self) -> None:
        try:
            self._send("QUIT")
        except OSError:
            pass
        self.sock.close()


def replay_chat(chat: list[tuple[str, str]], addr: tuple[str, int], channel: str,
                rate: float = 20.0, mask: bool = True, max_nicks: int = 50) -> dict:
    """Send chat to the mesh. Returns counts: sent, skipped, speakers."""
    if not _CHANNEL.match(channel):
        raise ValueError(f"invalid channel {channel!r}")
    masker = Masker(n for n, _ in chat) if mask else None
    sessions: dict[str, Session] = {}
    sent = skipped = 0
    delay = 1.0 / rate if rate > 0 else 0.0
    try:
        for nick, text in chat:
            shown = safe_nick(masker.nick(nick) if masker else nick)
            if shown not in sessions:
                if len(sessions) >= max_nicks:
                    skipped += 1
                    continue
                sessions[shown] = Session(addr, shown, channel)
                time.sleep(0.02)  # let the server register the nick before the first message
            sessions[shown].say(channel, masker.text(text) if masker else text)
            sent += 1
            if delay:
                time.sleep(delay)
    finally:
        for s in sessions.values():
            s.close()
    return {"sent": sent, "skipped": skipped, "speakers": len(sessions)}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Replay real IRC logs into ChimericMesh.")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--dataset", choices=sorted(IRC_DATASETS))
    src.add_argument("--file", type=Path, help="log in '[HH:MM] <nick> text' format")
    p.add_argument("--channel", default=None, help="IRC channel to speak in (default: the dataset's)")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--rate", type=float, default=20.0, help="messages per second (0 = unlimited)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=6667)
    p.add_argument("--max-nicks", type=int, default=50, help="max simultaneous connections")
    p.add_argument("--no-mask", action="store_true", help="keep real nicknames (not recommended)")
    p.add_argument("--data-dir", type=Path, default=Path("data"))
    args = p.parse_args(argv)

    if args.dataset:
        url, default_channel = IRC_DATASETS[args.dataset]
        path = args.data_dir / f"{args.dataset}.irc.log"
        if not path.exists():
            download(url, path)
        channel = args.channel or default_channel
        print(CITATION)
    else:
        path, channel = args.file, args.channel or "#ops"
    if not path.is_file():
        print(f"error: {path} not found", file=sys.stderr)
        return 2

    chat = load_chat(path, args.limit)
    try:
        stats = replay_chat(chat, (args.host, args.port), channel, args.rate, not args.no_mask, args.max_nicks)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"sent {stats['sent']} messages from {stats['speakers']} speakers to {channel} "
          f"on {args.host}:{args.port} ({stats['skipped']} skipped)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
