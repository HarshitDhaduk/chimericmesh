"""Replay real public log samples into a running mesh over UDP syslog.

Data comes from Loghub (https://github.com/logpai/loghub). It is free for research
and academic use; cite: Zhu et al., "Loghub: A Large Collection of System Log
Datasets for AI-driven Log Analytics", ISSRE 2023. Samples are downloaded at run
time into ``data/`` (git-ignored) and are never committed.

Usage:
    python -m chimericmesh.replay --dataset openssh --rate 200
    python -m chimericmesh.replay --file my.log --limit 500 --add-pri
"""

from __future__ import annotations

import argparse
import socket
import sys
import time
import urllib.request
from pathlib import Path
from typing import Iterator

_BASE = "https://raw.githubusercontent.com/logpai/loghub/master"
# Fixed allowlist: the downloader never fetches an arbitrary URL.
DATASETS = {
    "linux": f"{_BASE}/Linux/Linux_2k.log",
    "openssh": f"{_BASE}/OpenSSH/OpenSSH_2k.log",
}
CITATION = (
    "Data: Loghub (logpai). Free for research/academic use; cite Zhu et al., "
    "ISSRE 2023. https://github.com/logpai/loghub"
)
MAX_LINE = 1024


def download(url: str, path: Path) -> None:
    """Fetch an https URL to ``path``. Callers pass only allowlisted URLs."""
    if not url.startswith("https://"):
        raise ValueError("only https downloads are allowed")
    path.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=60) as resp:  # noqa: S310 (https, allowlisted)
        path.write_bytes(resp.read())


def ensure_dataset(name: str, data_dir: Path) -> Path:
    """Return the local path of a dataset, downloading it on first use."""
    if name not in DATASETS:
        raise ValueError(f"unknown dataset {name!r}; choose from {sorted(DATASETS)}")
    path = data_dir / Path(DATASETS[name]).name
    if not path.exists():
        download(DATASETS[name], path)
    return path


def iter_lines(path: Path, limit: int | None = None) -> Iterator[str]:
    """Yield non-empty, size-capped lines from a log file."""
    sent = 0
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\r\n")[:MAX_LINE]
            if not line.strip():
                continue
            yield line
            sent += 1
            if limit is not None and sent >= limit:
                return


def replay(lines: Iterator[str], sock: socket.socket, addr: tuple[str, int],
           rate: float = 100.0, add_pri: bool = False, pri: int = 38) -> int:
    """Send each line as one UDP datagram at about ``rate`` lines/second."""
    delay = 1.0 / rate if rate > 0 else 0.0
    count = 0
    for line in lines:
        payload = f"<{pri}>{line}" if add_pri else line
        sock.sendto(payload.encode("utf-8", errors="replace"), addr)
        count += 1
        if delay:
            time.sleep(delay)
    return count


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Replay real log samples into ChimericMesh.")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--dataset", choices=sorted(DATASETS))
    src.add_argument("--file", type=Path)
    p.add_argument("--limit", type=int, default=None, help="max lines to send")
    p.add_argument("--rate", type=float, default=100.0, help="lines per second (0 = as fast as possible)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5514)
    p.add_argument("--data-dir", type=Path, default=Path("data"))
    p.add_argument("--add-pri", action="store_true", help="prefix lines with a <PRI> header")
    args = p.parse_args(argv)

    path = ensure_dataset(args.dataset, args.data_dir) if args.dataset else args.file
    if not path.is_file():
        print(f"error: {path} not found", file=sys.stderr)
        return 2
    print(CITATION if args.dataset else f"Replaying {path}")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        n = replay(iter_lines(path, args.limit), sock, (args.host, args.port), args.rate, args.add_pri)
    print(f"sent {n} lines to {args.host}:{args.port}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
