"""README states the access model and the IRC settings (R6.5a, R9.6, review 3)."""
from __future__ import annotations

import re
from pathlib import Path

README = Path(__file__).resolve().parents[2] / "README.md"


def text() -> str:
    return README.read_text(encoding="utf-8")


def section(title: str) -> str:
    body = text()
    start = body.index(title)
    nxt = re.search(r"\n## ", body[start + len(title):])
    return body[start: start + len(title) + (nxt.start() if nxt else len(body))]


def flat(s: str) -> str:
    return re.sub(r"\s+", " ", s)


def test_access_control_section_states_the_model():
    s = flat(section("## Access control"))
    s = s.replace("Pause, revoke", "pause, revoke").replace("Resume and", "resume and")
    assert "pause, revoke and withdraw need no token" in s
    assert "including the agent from a shell" in s
    assert "annoyance, not escalation" in s
    assert "resume and every other rule change need the user" in s
    assert "server password" in s and "admin token" in s                      # R9.6
    assert "~/.chimericmesh/admin.token" in s and "CHIMERICMESH_TOKEN_FILE" in s
    assert "python -m chimericmesh token --rotate" in s
    assert "next hub start" in s
    assert "does not stop the agent" in s and "shell prompt" in s
    for var in ("CHIMERICMESH_ALLOWED_HOSTS", "CHIMERICMESH_OPERATORS", "CHIMERICMESH_BIND"):
        assert var in s
    assert "share an incident" in s                                            # Open Question 4


def test_the_three_contradicted_lines_are_corrected():
    body = flat(text())
    # 1. unset operators: the any-nick rule only with enforcement off
    for m in re.finditer(r"[^.]*any registered nick counts as an operator[^.]*\.", body):
        assert "enforcement" in m.group(0) or "injected" in m.group(0)
    assert "unset" in body and "operator commands are refused" in body
    # 2. no advice to put dashboard in CHIMERICMESH_OPERATORS
    assert "include `dashboard`" not in body
    assert "CHIMERICMESH_OPERATORS=alice,dashboard" not in body
    # 3. step 5 of the demo
    demo = flat(section("## A real incident, a real fix"))
    step5 = re.search(r"5\. (.*?) 6\. ", demo).group(1)
    assert "admin token" in step5 and "asks for it once" in step5
    assert "server password" in step5


def test_no_authentication_claim_is_gone():
    assert "No authentication; intended for localhost" not in text()
