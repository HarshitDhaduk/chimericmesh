"""Run the sample proxy worker against a local upstream and report to the ChimericMesh hub.

    python examples/legacy_app/run.py [--hub host:port] [--no-blackhole] [--duration seconds]
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from blackhole import DEFAULT_PORT, Blackhole  # noqa: E402
from syslog_log import DEFAULT_ADDRESS, SyslogHandler, parse_address  # noqa: E402
from upstream import UpstreamClient  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ChimericMesh sample service")
    ap.add_argument("--hub", default=None, help=f"syslog host:port (default env LEGACY_APP_SYSLOG or {DEFAULT_ADDRESS})")
    ap.add_argument("--upstream-port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--no-blackhole", action="store_true", help="use an upstream that is already running")
    ap.add_argument("--duration", type=float, default=None, help="stop after this many seconds")
    args = ap.parse_args(argv)

    handler = SyslogHandler(parse_address(args.hub) if args.hub else None)
    root = logging.getLogger("proxy-worker")
    root.addHandler(handler)
    root.setLevel(logging.INFO)

    blackhole = None if args.no_blackhole else Blackhole(args.upstream_port).start()
    port = blackhole.port if blackhole else args.upstream_port
    client = UpstreamClient(port=port)
    print(f"proxy-worker on host {handler.host}: logging to hub {handler.address[0]}:{handler.address[1]}, "
          f"upstream 127.0.0.1:{port}{' (blackhole)' if blackhole else ''}. Ctrl+C to stop.", flush=True)

    deadline = None if args.duration is None else time.monotonic() + args.duration
    try:
        client.run_forever(stop=lambda: deadline is not None and time.monotonic() >= deadline)
    except KeyboardInterrupt:
        print("stopping", flush=True)
    finally:
        if blackhole:
            blackhole.stop()
        root.removeHandler(handler)
        handler.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
