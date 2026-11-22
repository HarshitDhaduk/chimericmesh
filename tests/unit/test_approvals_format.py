from __future__ import annotations

from approvals_helpers import collect_garbage  # noqa: F401  (autouse: frees apps and their stores)

from chimericmesh.approvals.format import auto_approved_line, cooldown_note, rule_line, safe_text


def test_safe_text_removes_controls_surrogates_and_cuts():
    assert safe_text("a\x00b\r\nc\x1bd\x7f\x9fe", 50) == "a b c d e"
    assert safe_text("x\ud800y", 10) == "xy"
    assert safe_text("abcdef", 3) == "abc"
    assert safe_text(None, 10) == "None"


def test_auto_approved_line_exact_text():
    line = auto_approved_line("p3", "r1", "api-gw", "fix <x>")
    assert line == "AUTO-APPROVED p3 by rule r1 (service api-gw): fix <x>. Revoke with !revoke r1"


def test_auto_approved_line_never_asks_for_a_reply():
    line = auto_approved_line("p3", "r1", "api-gw", "restart")
    assert "reply" not in line.lower() and "!approve" not in line


def test_long_action_with_controls_gives_a_short_clean_line():
    line = auto_approved_line("p3", "r1", "api-gw", ("fix\r\n\x00 it " * 800)[:5000])
    assert len(line.encode("utf-8")) <= 400
    assert not any(c in line for c in "\r\n\x00") and all(ord(c) >= 32 for c in line)
    assert line.startswith("AUTO-APPROVED p3 by rule r1") and line.endswith("!revoke r1")


def test_multibyte_action_is_cut_on_a_character_boundary():
    line = auto_approved_line("p3", "r1", "svc", "\u00e9\u4e2d" * 400)
    assert len(line.encode("utf-8")) <= 400
    line.encode("utf-8")  # valid


def test_cooldown_note_names_both_ids():
    note = cooldown_note("p2", "r1")
    assert "p2" in note and "r1" in note


def test_rule_lines_carry_the_cleaned_actor():
    for verb in ("created", "revoked", "rule_paused", "enabled", "suspended", "expired", "paused", "resumed",
                 "withdrawn", "denied"):
        line = rule_line(verb, "r1", "api-gw", "ni\r\nck\x00", "why")
        assert "ni ck" in line, verb
        assert not any(c in line for c in "\r\n\x00")
        assert len(line.encode("utf-8")) <= 400


def test_denied_line_text():
    assert rule_line("denied", "p3", "r1", "hub", "was revoked") == "PROPOSAL p3 denied by hub: rule r1 was revoked"
