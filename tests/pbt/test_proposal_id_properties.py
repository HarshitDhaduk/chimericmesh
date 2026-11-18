"""Properties of per-run proposal ids."""
import asyncio

from hypothesis import given, settings, strategies as st

from chimericmesh.commands import parse_command
from chimericmesh.hub import RUN_TAG_ALPHABET, RUN_TAG_LEN, Hub, new_run_tag
from chimericmesh.parsers import parse_syslog

DISK = "<27>Nov 13 11:10:08 db01 pg[1]: FATAL: could not write block %d: No space left on device"
tags = st.text(alphabet=RUN_TAG_ALPHABET, min_size=RUN_TAG_LEN, max_size=RUN_TAG_LEN)


@settings(max_examples=100, deadline=None)
@given(st.integers(0, 50))
def test_generated_tag_matches_alphabet_and_length(_):
    tag = new_run_tag()
    assert len(tag) == RUN_TAG_LEN and set(tag) <= set(RUN_TAG_ALPHABET)
    assert len(Hub().run_tag) == RUN_TAG_LEN


@settings(max_examples=40, deadline=None)
@given(tags, st.integers(1, 5), st.sampled_from(["approve", "deny"]), st.booleans())
def test_ids_round_trip_through_parse_command(tag, count, verb, upper):
    async def run():
        hub = Hub(run_tag=tag)
        for i in range(3):
            await hub.ingest(parse_syslog(DISK % i))
        iid = hub.incidents()[0].id
        ids = [(await hub.create_proposal(iid, "x")).id for _ in range(count)]
        assert len(set(ids)) == count
        for n, pid in enumerate(ids, 1):
            assert pid == f"p{n}-{tag}"
            text = f"!{verb} {pid.upper() if upper else pid}"
            cmd = parse_command(text)
            assert cmd is not None and cmd.name == verb and cmd.args[0].lower() == pid
    asyncio.run(run())