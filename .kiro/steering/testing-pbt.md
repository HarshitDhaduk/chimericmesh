---
inclusion: fileMatch
fileMatchPattern: "tests/**"
---

# Testing guidance

- Parsers and formatters get Hypothesis property tests in `tests/pbt/`:
  - never raise on arbitrary text or bytes
  - outputs stay within protocol limits (IRC lines <= 510 bytes, severity 0..7)
  - injection safety (no CR/LF/NUL in outbound IRC lines)
  - chunking preserves content
- Unit tests in `tests/unit/` cover concrete examples and the REST/MCP round trip.
- Async tests use `asyncio.run` inside plain test functions (no pytest-asyncio).
- `create_app` wires the real IRC broadcaster into the hub; install test fakes after creating the app.
