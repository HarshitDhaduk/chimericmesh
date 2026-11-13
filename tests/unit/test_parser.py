"""Unit tests for LegacyParser and KiroCrew."""
import pytest
from src.legacy_bridge.parser import LegacyParser, Severity, ProtocolKind
from src.agents.crew import KiroCrew

def test_syslog_rfc3164_parsing():
    raw = "<34>Oct 11 22:14:15 myhost su[2304]: 'su root' failed for lonvick on /dev/pts/8"
    event = LegacyParser.parse_line(raw, event_id="test_001")
    assert event.protocol == ProtocolKind.SYSLOG
    assert event.source_host == "myhost"
    assert event.subsystem == "su"
    assert "failed for lonvick" in event.clean_message
    assert event.metadata.get("pid") == "2304"

def test_irc_parsing():
    raw = ":monitor_bot!daemon@srv.internal PRIVMSG #infra-alerts :FATAL: Database connection pool exhausted"
    event = LegacyParser.parse_line(raw, event_id="test_002")
    assert event.protocol == ProtocolKind.IRC
    assert event.severity == Severity.CRITICAL
    assert "Database connection pool exhausted" in event.clean_message

def test_kiro_crew_pipeline():
    crew = KiroCrew()
    raw = "<19>Nov 13 10:45:00 border-gw bgpd[801]: BGP peer down: connection reset by peer"
    event = LegacyParser.parse_line(raw, event_id="test_003")
    incident = crew.process(event)
    assert incident is not None
    assert incident.recommended_mcp_tool == "restart_legacy_daemon"
    assert "bgpd" in incident.remediation_payload["service_name"]
