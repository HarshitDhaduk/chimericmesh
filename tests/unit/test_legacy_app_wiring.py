"""The sample legacy service must produce events the hub turns into a findable incident."""
import logging
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "examples" / "legacy_app"
sys.path.insert(0, str(APP))

from syslog_log import SyslogHandler  # noqa: E402

from chimericmesh.diagnostics import IncidentTracker, analyze, find_code_hints  # noqa: E402
from chimericmesh.parsers import parse_syslog  # noqa: E402

MESSAGE = ("upstream timed out connecting to %s:%d", ("127.0.0.1", 19099))


def _datagram() -> str:
    rec = logging.LogRecord("proxy-worker", logging.ERROR, "x.py", 1, MESSAGE[0], MESSAGE[1], None)
    return SyslogHandler(("127.0.0.1", 9), host="web02").format_datagram(rec).decode()


def test_datagram_parses_as_error_from_host_and_app():
    ev = parse_syslog(_datagram())
    assert ev.severity == 3
    assert ev.host == "web02"
    assert ev.app == "proxy-worker"
    assert "upstream timed out connecting to 127.0.0.1:19099" in ev.message


def test_three_repeats_open_a_network_incident():
    events = [parse_syslog(_datagram()) for _ in range(3)]
    incidents = analyze(events)
    assert [i.category for i in incidents] == ["network"]
    tracker = IncidentTracker()
    assert tracker.add(events[0]) is not None and tracker.incidents() == []
    for ev in events[1:]:
        tracker.add(ev)
    assert len(tracker.incidents()) == 1


def test_incident_points_at_upstream_source():
    incident = analyze([parse_syslog(_datagram()) for _ in range(3)])[0]
    hits = find_code_hints(incident, APP)
    assert hits and hits[0]["file"] == "upstream.py"
    lines = (APP / "upstream.py").read_text(encoding="utf-8").splitlines()
    assert "upstream timed out connecting to" in lines[hits[0]["line"] - 1]


def test_handler_never_raises_when_hub_unreachable():
    h = SyslogHandler(("nonexistent.invalid", 5514), host="web02")
    log = logging.getLogger("wiring-test")
    log.addHandler(h)
    log.propagate = False
    log.error("upstream timed out connecting to %s:%d", "127.0.0.1", 19099)

    def boom(*a, **k):
        raise OSError("network down")

    h._sock = type("S", (), {"sendto": staticmethod(boom), "close": lambda self: None})()
    log.error("again")
    log.removeHandler(h)
    h.close()
