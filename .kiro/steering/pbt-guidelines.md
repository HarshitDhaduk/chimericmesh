# Property-Based Testing (PBT) Steering Guidelines

This document steers Kiro to implement robust Property-Based Testing rather than relying solely on example-based unit tests.

## Why PBT for ChimericMesh?
Legacy telemetry streams (Syslog, IRC, serial output) produce chaotic, malformed, and adversarial inputs in production:
- Fragmented packets, broken UTF-8 byte sequences.
- Extreme timestamps (epoch 0, year 2038 overflow, leap seconds).
- Buffer overflow attempts and null-byte injection.

Example-based tests only verify edge cases the engineer already thought of. Property-Based Testing (via `hypothesis` in Python or `fast-check` in TypeScript) generates thousands of pseudo-random inputs to verify universal mathematical invariants.

## Core Invariants to Enforce
1. **Crash Invariance:**
   - *Property:* For any arbitrary byte stream `b: bytes`, `parse_syslog_stream(b)` MUST either return a valid `ParsedEvent` or raise a typed `LegacyParsingError`. It must NEVER crash with `Panic`, `IndexError`, or `MemoryError`.
2. **Round-Trip Serialization:**
   - *Property:* For any valid structured `TelemetryIncident`, `deserialize(serialize(incident)) == incident`.
3. **Agent Action Boundedness:**
   - *Property:* No combination of user/network messages can cause the Remediation Agent to generate an MCP tool invocation with unauthorized privileges or outside allowable parameters.
4. **Rate Limiting & Memory Cap:**
   - *Property:* Stream buffer consumption must remain $O(1)$ with respect to unbounded stream length.
