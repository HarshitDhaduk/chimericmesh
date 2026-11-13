"""Legacy Stream Parser: Decodes and normalizes BSD Syslog (RFC 3164/5424) and IRC messages."""
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional
from pydantic import BaseModel, Field

class Severity(str, Enum):
    EMERGENCY = "emergency"
    ALERT = "alert"
    CRITICAL = "critical"
    ERROR = "error"
    WARNING = "warning"
    NOTICE = "notice"
    INFO = "info"
    DEBUG = "debug"

class ProtocolKind(str, Enum):
    SYSLOG = "syslog"
    IRC = "irc"
    RAW_TCP = "raw_tcp"

class TelemetryEvent(BaseModel):
    event_id: str
    timestamp: datetime
    protocol: ProtocolKind
    severity: Severity
    source_host: str
    subsystem: str
    raw_message: str
    clean_message: str
    metadata: Dict[str, Any] = Field(default_factory=dict)

class LegacyParser:
    """Robust parser engineered to handle erratic legacy network streams."""

    # RFC 3164: <PRI>TIMESTAMP HOSTNAME TAG[PID]: MSG
    RFC3164_PATTERN = re.compile(
        r"^<(?P<pri>\d{1,3})>(?P<timestamp>[A-Z][a-z]{2}\s+\d+\s+\d{2}:\d{2}:\d{2})\s+(?P<host>\S+)\s+(?P<tag>[^:\[\s]+)(?:\[(?P<pid>\d+)\])?:\s*(?P<msg>.*)$"
    )

    # IRC PRIVMSG: :NICK!USER@HOST PRIVMSG #CHANNEL :MESSAGE
    IRC_PATTERN = re.compile(
        r"^:(?P<nick>[^!]+)!(?P<user>[^@]+)@(?P<host>\S+)\s+PRIVMSG\s+(?P<channel>\S+)\s+:(?P<msg>.*)$"
    )

    SEVERITY_MAP = {
        0: Severity.EMERGENCY,
        1: Severity.ALERT,
        2: Severity.CRITICAL,
        3: Severity.ERROR,
        4: Severity.WARNING,
        5: Severity.NOTICE,
        6: Severity.INFO,
        7: Severity.DEBUG,
    }

    @classmethod
    def parse_line(cls, line: str, event_id: str = "evt_001") -> TelemetryEvent:
        """Parses a single legacy stream line into a structured TelemetryEvent."""
        if not line:
            return TelemetryEvent(
                event_id=event_id,
                timestamp=datetime.now(timezone.utc),
                protocol=ProtocolKind.RAW_TCP,
                severity=Severity.DEBUG,
                source_host="unknown",
                subsystem="null",
                raw_message="",
                clean_message="Empty payload",
            )

        # Truncate arbitrarily long inputs to prevent memory exhaustion
        sanitized = line[:8192].strip()

        # Check Syslog match
        syslog_match = cls.RFC3164_PATTERN.match(sanitized)
        if syslog_match:
            pri = int(syslog_match.group("pri"))
            severity_num = pri % 8
            severity = cls.SEVERITY_MAP.get(severity_num, Severity.NOTICE)
            return TelemetryEvent(
                event_id=event_id,
                timestamp=datetime.now(timezone.utc),
                protocol=ProtocolKind.SYSLOG,
                severity=severity,
                source_host=syslog_match.group("host"),
                subsystem=syslog_match.group("tag"),
                raw_message=sanitized,
                clean_message=syslog_match.group("msg").strip(),
                metadata={"pid": syslog_match.group("pid"), "pri": pri},
            )

        # Check IRC match
        irc_match = cls.IRC_PATTERN.match(sanitized)
        if irc_match:
            msg = irc_match.group("msg").strip()
            # Infer severity from message content keywords
            severity = Severity.INFO
            upper_msg = msg.upper()
            if any(k in upper_msg for k in ["FATAL", "CRITICAL", "PANIC"]):
                severity = Severity.CRITICAL
            elif any(k in upper_msg for k in ["ERROR", "FAIL"]):
                severity = Severity.ERROR
            elif "WARN" in upper_msg:
                severity = Severity.WARNING

            return TelemetryEvent(
                event_id=event_id,
                timestamp=datetime.now(timezone.utc),
                protocol=ProtocolKind.IRC,
                severity=severity,
                source_host=irc_match.group("host"),
                subsystem=f"irc/{irc_match.group('channel')}",
                raw_message=sanitized,
                clean_message=msg,
                metadata={"nick": irc_match.group("nick"), "user": irc_match.group("user")},
            )

        # Fallback for unformatted raw TCP line
        severity = Severity.INFO
        upper_line = sanitized.upper()
        if "ERROR" in upper_line or "PANIC" in upper_line:
            severity = Severity.ERROR

        return TelemetryEvent(
            event_id=event_id,
            timestamp=datetime.now(timezone.utc),
            protocol=ProtocolKind.RAW_TCP,
            severity=severity,
            source_host="legacy-terminal",
            subsystem="raw-stream",
            raw_message=sanitized,
            clean_message=sanitized,
        )
