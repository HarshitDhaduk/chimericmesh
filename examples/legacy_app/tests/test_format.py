"""Behaviour that holds before and after any fix."""
import logging
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from blackhole import Blackhole
from syslog_log import SyslogHandler, clean_host, severity_for
from upstream import UpstreamClient

APP_DIR = Path(__file__).resolve().parents[1]


def record(level=logging.ERROR, msg="upstream timed out connecting to %s:%d", args=("127.0.0.1", 19099)):
    return logging.LogRecord("proxy-worker", level, __file__, 1, msg, args, None)


def test_datagram_format_and_pri():
    h = SyslogHandler(("127.0.0.1", 9), host="web02")
    text = h.format_datagram(record()).decode()
    m = re.fullmatch(r"<(\d+)>([A-Z][a-z]{2} [ \d]\d \d\d:\d\d:\d\d) web02 proxy-worker\[(\d+)\]: (.*)", text)
    assert m, text
    assert m.group(1) == "131"  # local0 (128) + error (3)
    assert m.group(3) == str(os.getpid())
    assert m.group(4) == "upstream timed out connecting to 127.0.0.1:19099"
    h.close()


@pytest.mark.parametrize("level,sev", [(logging.ERROR, 3), (logging.WARNING, 4), (logging.INFO, 6)])
def test_severity_mapping(level, sev):
    assert severity_for(level) == sev
    h = SyslogHandler(("127.0.0.1", 9), host="h")
    assert h.format_datagram(record(level, "x", ())).startswith(f"<{128 + sev}>".encode())
    h.close()


def test_host_is_sanitised():
    assert clean_host("web 02;\r\n<x>") == "web02x"
    assert len(clean_host("a" * 100)) == 63
    h = SyslogHandler(("127.0.0.1", 9), host="bad host\n")
    assert h.host == "badhost"
    h.close()


def test_message_cannot_inject_lines():
    h = SyslogHandler(("127.0.0.1", 9), host="h")
    data = h.format_datagram(record(msg="a\r\nb\x00c", args=()))
    assert b"\n" not in data and b"\r" not in data and b"\x00" not in data
    h.close()


def test_handler_sends_real_datagram():
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0))
    rx.settimeout(2)
    h = SyslogHandler(rx.getsockname(), host="web02")
    h.emit(record())
    assert b"upstream timed out connecting to" in rx.recvfrom(2048)[0]
    h.close()
    rx.close()


def test_blackhole_accepts_and_stays_silent():
    bh = Blackhole(0).start()
    try:
        c = socket.create_connection(("127.0.0.1", bh.port), timeout=1)
        c.sendall(b"PING\n")
        c.settimeout(0.3)
        with pytest.raises(socket.timeout):
            c.recv(10)
        c.close()
    finally:
        bh.stop()


def test_failed_check_logs_exact_line(caplog):
    class Dead:
        def sendall(self, d): pass
        def recv(self, n): raise socket.timeout()
        def close(self): pass

    client = UpstreamClient("10.1.2.3", 80, connect=lambda a, timeout=None: Dead())
    with caplog.at_level(logging.ERROR, logger="proxy-worker"):
        with pytest.raises(socket.timeout):
            client.check()
    assert [r.getMessage() for r in caplog.records] == ["upstream timed out connecting to 10.1.2.3:80"]
    assert caplog.records[0].levelno == logging.ERROR


def test_real_check_against_blackhole_times_out():
    bh = Blackhole(0).start()
    try:
        with pytest.raises(socket.timeout):
            UpstreamClient(port=bh.port, timeout=0.1).check()
    finally:
        bh.stop()


def test_run_exits_after_duration_and_reports_to_hub():
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0))
    rx.settimeout(5)
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    start = time.monotonic()
    out = subprocess.run(
        [sys.executable, str(APP_DIR / "run.py"), "--hub", f"127.0.0.1:{rx.getsockname()[1]}",
         "--upstream-port", str(port), "--duration", "1.5"],
        capture_output=True, text=True, timeout=30,
    )
    assert out.returncode == 0, out.stderr
    assert time.monotonic() - start < 20
    assert "Ctrl+C to stop" in out.stdout
    assert b"upstream timed out connecting to" in rx.recvfrom(2048)[0]
    rx.close()
