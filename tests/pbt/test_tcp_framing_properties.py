"""Task 3.2: properties 9, 10 and 11 (decoder part) of the TCP frame decoder."""
from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from chimericmesh.connectors.framing import FrameDecoder, FrameError, detect_mode

SET = settings(max_examples=300, deadline=None, suppress_health_check=list(HealthCheck))

LF_MESSAGES = st.lists(
    st.binary(min_size=1, max_size=200).filter(lambda b: b"\n" not in b and b"\r" not in b),
    min_size=1, max_size=12,
)
OCTET_MESSAGES = st.lists(
    st.binary(min_size=1, max_size=300).filter(lambda b: not b.startswith((b"\r", b"\n"))),
    min_size=1, max_size=12,
)


def partition(data: bytes, cuts: list[int]) -> list[bytes]:
    points = sorted({c % (len(data) + 1) for c in cuts} | {0, len(data)})
    return [data[a:b] for a, b in zip(points, points[1:])]


def run(chunks: list[bytes]) -> tuple[list, list]:
    d = FrameDecoder()
    out: list = []
    for c in chunks:
        out += d.feed(c)
    return out, d.finish()


@SET
@given(OCTET_MESSAGES, st.lists(st.integers(0, 10_000), max_size=30))
def test_octet_chunking_invariant(messages, cuts):
    stream = b"".join(b"%d " % len(m) + m for m in messages)
    # The first byte of a stream must select octet mode: it always starts with 1 to 3 digits and a space.
    whole, tail = run([stream])
    assert whole == messages and tail == []
    pieces, tail2 = run(partition(stream, cuts))
    assert pieces == messages and tail2 == []
    singles, _ = run([stream[i:i + 1] for i in range(len(stream))])
    assert singles == messages


@SET
@given(LF_MESSAGES, st.lists(st.integers(0, 10_000), max_size=30), st.booleans(), st.booleans())
def test_lf_chunking_invariant(messages, cuts, final_newline, crlf):
    # A leading digit-and-space would select octet mode, so prefix with a letter.
    messages = [b"x" + m for m in messages]
    eol = b"\r\n" if crlf else b"\n"
    stream = eol.join(messages) + (eol if final_newline else b"")
    expected = messages
    whole, tail = run([stream])
    assert whole + tail == expected
    pieces, tail2 = run(partition(stream, cuts))
    assert pieces + tail2 == expected


@SET
@given(st.binary(max_size=40))
def test_detection_rule(first):
    n = 0
    while n < len(first) and first[n:n + 1].isdigit() and first[n] < 128:
        n += 1
    mode = detect_mode(first)
    if not first or (n == len(first) and n < 5):
        assert mode is None
    elif 1 <= n <= 4 and first[n:n + 1] == b" ":
        assert mode == "octet"
    else:
        assert mode == "lf"
    d = FrameDecoder()
    out = d.feed(first)
    if mode == "lf":
        assert not d.failed and not any(isinstance(x, FrameError) for x in out)


@SET
@given(
    st.lists(st.binary(min_size=1, max_size=50), max_size=5),
    st.sampled_from([b"0 ", b"9999 ", b"8193 ", b"12345 ", b"102400 ", b"x", b"5x"]),
    st.binary(max_size=40),
    st.lists(st.integers(0, 10_000), max_size=10),
)
def test_invalid_header_closes_only_that_stream(messages, bad, tail, cuts):
    good = b"".join(b"%d " % len(m) + m for m in messages)
    if not messages:
        good = b"1 a"  # make sure octet mode is decided before the bad header
        messages = [b"a"]
    stream = good + bad + tail
    d = FrameDecoder()
    out: list = []
    for c in partition(stream, cuts):
        out += d.feed(c)
    errors = [x for x in out if isinstance(x, FrameError)]
    assert len(errors) == 1 and out[-1] is errors[0]
    assert out[:-1] == messages
    assert d.failed and d.feed(b"3 abc") == []
