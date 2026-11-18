"""Minimal terminal IRC client for talking to the ChimericMesh bot.

    python scripts/irc_client.py                    # nick "operator", channel #ops
    python scripts/irc_client.py --nick alice --channel "#ops"

Type a line and press Enter to say it in the channel (try !help). Ctrl+C or /quit exits.
Lines are piped through the same sanitiser the server uses, so CR/LF can never be injected.
"""

from __future__ import annotations

import argparse
import re
import socket
import sys
import threading
import time

try:
    from chimericmesh.parsers import format_privmsg
except ImportError:  # running from a checkout without `pip install -e .`
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "src"))
    from chimericmesh.parsers import format_privmsg

_NICK = re.compile(r"^[A-Za-z][A-Za-z0-9_\-\[\]\\`^{|}]{0,29}$")
_CHANNEL = re.compile(r"^#[\w\-]{1,48}$")
_PRIVMSG = re.compile(r"^:([^!\s]+)[^ ]* PRIVMSG (\S+) :(.*)$")


def reader(sock: socket.socket, stop: threading.Event) -> None:
    buf = b""
    while not stop.is_set():
        try:
            chunk = sock.recv(4096)
        except OSError:
            break
        if not chunk:
            print("\n* disconnected by server")
            break
        buf += chunk
        while b"\n" in buf:
            raw, buf = buf.split(b"\n", 1)
            line = raw.decode("utf-8", errors="replace").strip()
            m = _PRIVMSG.match(line)
            print(f"<{m.group(1)}> {m.group(3)}" if m else f"* {line}")
    stop.set()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--nick", default="operator")
    p.add_argument("--channel", default="#ops")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=6667)
    p.add_argument("--linger", type=float, default=1.5, help="seconds to wait for replies after input ends")
    args = p.parse_args()
    if not _NICK.match(args.nick) or not _CHANNEL.match(args.channel):
        print("error: invalid nick or channel", file=sys.stderr)
        return 2

    try:
        sock = socket.create_connection((args.host, args.port), timeout=5)
    except OSError as exc:
        print(f"error: cannot reach {args.host}:{args.port} ({exc}). Is 'python -m chimericmesh' running?", file=sys.stderr)
        return 1
    sock.settimeout(None)
    stop = threading.Event()
    threading.Thread(target=reader, args=(sock, stop), daemon=True).start()

    for line in (f"NICK {args.nick}", f"USER {args.nick} 0 * :{args.nick}", f"JOIN {args.channel}"):
        sock.sendall(line.encode() + b"\r\n")
    print(f"connected as {args.nick} in {args.channel}. Try: !help   (/quit to leave)")

    try:
        for text in sys.stdin:
            text = text.strip()
            if stop.is_set() or text == "/quit":
                break
            if text:
                for out in format_privmsg(args.channel, text):
                    sock.sendall(out.encode("utf-8", errors="replace") + b"\r\n")
        if not sys.stdin.isatty():
            time.sleep(args.linger)  # piped input: give the bot time to answer
    except KeyboardInterrupt:
        pass
    finally:
        try:
            sock.sendall(b"QUIT\r\n")
        except OSError:
            pass
        stop.set()
        sock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
