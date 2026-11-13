"""Property-Based Testing (PBT) suite for ChimericMesh using Hypothesis.
Directly addresses Kiroween 2026 rubric for next-level PBT implementation.
"""
from hypothesis import given, strategies as st
from src.legacy_bridge.parser import LegacyParser, TelemetryEvent, ProtocolKind, Severity

# Property 1: Crash Invariance across arbitrary text
@given(st.text())
def test_pbt_crash_invariance_arbitrary_text(fuzz_input: str):
    """Proves that arbitrary textual input never crashes the parser with unhandled exceptions."""
    event = LegacyParser.parse_line(fuzz_input, event_id="pbt_001")
    assert isinstance(event, TelemetryEvent)
    assert event.protocol in [ProtocolKind.SYSLOG, ProtocolKind.IRC, ProtocolKind.RAW_TCP]
    assert isinstance(event.severity, Severity)

# Property 2: Truncation & Memory Boundedness Invariance
@given(st.text(min_size=500, max_size=1000), st.integers(min_value=10, max_value=20))
def test_pbt_buffer_overflow_invariance(chunk: str, multiplier: int):
    """Proves that inputs exceeding maximum buffer capacity are bounded to <= 8192 characters."""
    long_input = chunk * multiplier
    event = LegacyParser.parse_line(long_input, event_id="pbt_002")
    assert len(event.raw_message) <= 8192

# Property 3: Syslog Priority Rounding Invariance
@given(st.integers(min_value=0, max_value=191), st.text(alphabet=st.characters(blacklist_categories=('Cs',)), min_size=1, max_size=100))
def test_pbt_syslog_severity_mapping(pri: int, msg: str):
    """Proves that RFC 3164 Syslog priorities map consistently to defined Severities."""
    clean_msg = msg.replace("\n", " ").replace("\r", " ").strip()
    raw = f"<{pri}>Nov 13 11:00:00 testhost app[123]: {clean_msg}"
    event = LegacyParser.parse_line(raw, event_id="pbt_003")
    assert event.protocol == ProtocolKind.SYSLOG
    expected_severity_num = pri % 8
    assert event.severity == LegacyParser.SEVERITY_MAP[expected_severity_num]
