"""Regressions found by replaying real Loghub samples (Linux, OpenSSH)."""

import socket

import pytest

from chimericmesh.diagnostics import analyze, classify, signature
from chimericmesh.parsers import parse_syslog
from chimericmesh.replay import DATASETS, ensure_dataset, iter_lines, replay


def test_missing_pri_severity_inferred_from_text():
    ev = parse_syslog("Dec 10 06:55:46 LabSZ sshd[24200]: Failed password for root from 5.36.59.76 port 42393 ssh2")
    assert ev.severity == 3 and ev.host == "LabSZ" and ev.app == "sshd"


def test_missing_pri_benign_line_stays_info():
    assert parse_syslog("Jun 14 15:16:02 combo ftpd[1]: connection from 1.2.3.4").severity == 6


def test_explicit_pri_wins_over_text():
    assert parse_syslog("<30>Jun 14 15:16:02 combo daemon[1]: error handled fine").severity == 6


def test_parenthesised_app_name_parses():
    ev = parse_syslog("Jun 14 15:16:01 combo sshd(pam_unix)[19939]: authentication failure; rhost=1.2.3.4")
    assert ev.app == "sshd(pam_unix)" and ev.host == "combo"


def test_failed_password_variants_share_a_signature():
    a = signature("Failed password for root from 5.36.59.76 port 42393 ssh2")
    b = signature("Failed password for invalid user admin from 5.188.10.180 port 60682 ssh2")
    assert a == b


def test_distinct_problems_keep_distinct_signatures():
    assert signature("waiting for lock") != signature("waiting for disk")


@pytest.mark.parametrize("msg", [
    "Kerberos authentication failed",
    "Authentication failed from 1.2.3.4 (1.2.3.4): Software caused connection abort",
    "reverse mapping checking getaddrinfo failed - POSSIBLE BREAK-IN ATTEMPT!",
])
def test_authentication_failures_are_security(msg):
    assert classify(msg)[0] == "security"


def test_one_off_errors_are_not_incidents_but_repeats_are():
    one = [parse_syslog("Dec 10 06:55:46 h sshd[1]: error: something odd happened")]
    assert analyze(one) == []
    assert len(analyze(one * 3)) == 1


def test_iter_lines_skips_blank_and_respects_limit(tmp_path):
    f = tmp_path / "x.log"
    f.write_text("a\n\n  \nb\nc\n")
    assert list(iter_lines(f)) == ["a", "b", "c"]
    assert list(iter_lines(f, limit=2)) == ["a", "b"]


def test_iter_lines_caps_line_length(tmp_path):
    f = tmp_path / "x.log"
    f.write_text("z" * 5000 + "\n")
    assert len(next(iter_lines(f))) == 1024


def test_replay_sends_raw_and_with_pri():
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0))
    rx.settimeout(2)
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    addr = rx.getsockname()
    try:
        assert replay(iter(["hello"]), tx, addr, rate=0) == 1
        assert rx.recv(100) == b"hello"
        replay(iter(["hello"]), tx, addr, rate=0, add_pri=True, pri=27)
        assert rx.recv(100) == b"<27>hello"
    finally:
        rx.close()
        tx.close()


def test_dataset_allowlist_only(tmp_path):
    assert all(u.startswith("https://raw.githubusercontent.com/logpai/loghub/") for u in DATASETS.values())
    with pytest.raises(ValueError):
        ensure_dataset("../../etc/passwd", tmp_path)


def test_tracker_matches_batch_and_is_incremental():
    from chimericmesh.diagnostics import IncidentTracker

    evs = [parse_syslog(f"Dec 10 06:55:46 h sshd[1]: Failed password for user{i} from 1.2.3.{i} port {i} ssh2") for i in range(5)]
    tracker = IncidentTracker()
    opened = [tracker.qualifies(tracker.add(e)) for e in evs]  # same live object each time
    assert opened == [False, False, True, True, True]  # the third repeat opens it
    assert [(i.id, i.count) for i in tracker.incidents()] == [(i.id, i.count) for i in analyze(evs)]


def test_tracker_memory_is_bounded(monkeypatch):
    import chimericmesh.diagnostics as d

    monkeypatch.setattr(d, "MAX_CLUSTERS", 50)
    tracker = d.IncidentTracker()
    for i in range(500):
        tracker.add(parse_syslog(f"Dec 10 06:55:46 h{i} sshd[1]: error: unique problem number {'x' * i}"))
    assert len(tracker._clusters) <= 51
