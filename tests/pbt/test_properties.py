"""Property-based tests (Hypothesis) for the legacy protocol parsers and analyzer."""

from hypothesis import given, settings, strategies as st

from chimericmesh.diagnostics import analyze, signature
from chimericmesh.events import Event
from chimericmesh.parsers import format_privmsg, irc_to_event, parse_irc_line, parse_syslog, strip_telnet

text = st.text(max_size=600)


@given(text)
def test_syslog_never_raises_and_severity_valid(line):
    ev = parse_syslog(line)
    assert 0 <= ev.severity <= 7
    assert "\x00" not in ev.message and "\r" not in ev.message


@given(st.integers(0, 191), st.text(alphabet="abcdef ", min_size=1, max_size=40))
def test_syslog_pri_decodes_severity(pri, body):
    assert parse_syslog(f"<{pri}>{body}").severity == pri % 8


@given(text)
def test_irc_parse_never_raises(line):
    parse_irc_line(line)
    irc_to_event(line)


@given(st.text(max_size=3000), st.text(max_size=40))
def test_privmsg_is_injection_safe_and_bounded(body, target):
    for line in format_privmsg("#" + target, body):
        assert "\r" not in line and "\n" not in line and "\x00" not in line
        assert len(line.encode()) <= 510


@given(st.text(alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\r\n\x00"),
               min_size=1, max_size=300))
def test_privmsg_preserves_content_when_chunked(body):
    lines = format_privmsg("#ops", body)
    rebuilt = "".join(l.split(" :", 1)[1] for l in lines)
    assert rebuilt == body.strip()


@given(st.binary(max_size=500))
def test_telnet_output_has_no_iac_or_control_noise(data):
    out = strip_telnet(data)
    assert "\x00" not in out and "\x1b" not in out and "\r" not in out


@given(st.binary(max_size=300))
def test_telnet_plain_ascii_passthrough(data):
    plain = bytes(b for b in data if 32 <= b < 127)
    assert strip_telnet(plain) == plain.decode()


@given(st.text(max_size=200))
def test_signature_idempotent_and_bounded(msg):
    sig = signature(msg)
    assert signature(sig) == signature(sig) and len(sig) <= 200


@settings(max_examples=50)
@given(st.lists(st.tuples(st.sampled_from(["a", "b"]), st.integers(0, 7), st.text(min_size=1, max_size=40)), max_size=40))
def test_analyze_counts_never_exceed_events(items):
    events = [Event(source="syslog", host=h, app="x", severity=s, message=m) for h, s, m in items]
    incs = analyze(events)
    assert sum(i.count for i in incs) <= len(events)
    assert all(i.severity <= 4 and i.count >= 1 for i in incs)


@given(st.text(max_size=300))
def test_extract_host_total_and_host_is_clean(line):
    from chimericmesh.events import extract_host

    host, message = extract_host(line)
    assert message == message.strip()
    if host is not None:
        assert host and not any(c.isspace() for c in host) and ":" not in host
        assert len(host) <= 63
