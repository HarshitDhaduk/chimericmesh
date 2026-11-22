"""Property 21 of auto-approval (optional): ``parse_command`` stays total with the new command names."""
from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from chimericmesh.commands import MAX_ARGS, MAX_COMMAND_CHARS, Command, parse_command

NAMES = ["rules", "revoke", "pause", "withdraw", "resume", "approve"]


@settings(max_examples=300, deadline=None)
@given(st.text(max_size=400))
def test_property_21_never_raises_and_bounds_hold(text):
    out = parse_command(text)
    assert out is None or isinstance(out, Command)
    if out is not None:
        assert len(out.args) <= MAX_ARGS and out.name and out.name == out.name.lower()
        assert text.startswith("!")


@settings(max_examples=200, deadline=None)
@given(st.sampled_from(NAMES), st.lists(st.text(max_size=60), max_size=8))
def test_property_21_new_commands_parse_with_arbitrary_arguments(name, args):
    out = parse_command("!" + name + " " + " ".join(args))
    assert out is not None and out.name == name
    assert len(out.args) <= MAX_ARGS
    assert len(" ".join([out.name, *out.args])) <= MAX_COMMAND_CHARS
