# Specification 01: Legacy Stream Parser (Syslog & IRC Bridge)

## 1. Overview
The Legacy Stream Parser receives unformatted, archaic network protocol streams (RFC 3164 BSD Syslog, RFC 5424 Syslog, and IRC chat messages) and normalizes them into structured, type-safe telemetry incidents for consumption by the Kiro Crew agent swarm.

## 2. Requirements & Data Models
### 2.1 Raw Input Formats
- **Syslog RFC 3164:** `<PRI>TIMESTAMP HOSTNAME TAG[PID]: MSG`
- **Syslog RFC 5424:** `<PRI>1 TIMESTAMP HOSTNAME APP-NAME PROCID MSGID [STRUCTURED-DATA] MSG`
- **IRC Raw Protocol:** `:NICK!USER@HOST PRIVMSG #CHANNEL :MESSAGE`
- **Corrupted Streams:** Arbitrary chunks, split lines, non-ASCII escapes.

### 2.2 Output Schema (`TelemetryEvent`)
```python
from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional, Dict, Any
from enum import Enum

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
```

## 3. Error Handling & Invariants
- Ingestion must be asynchronous using non-blocking I/O.
- Any line exceeding 8192 bytes must be truncated safely without buffer overflow.
- All non-UTF-8 bytes must be decoded with replacement characters (`replace` mode) to prevent unhandled decoding exceptions.
