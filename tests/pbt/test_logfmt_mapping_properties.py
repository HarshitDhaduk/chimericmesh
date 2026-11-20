"""Task 1.8: NCSA and W3C mapping (properties 2 and 3)."""
from __future__ import annotations

import dataclasses

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from chimericmesh.parsers.formats import get_format
from chimericmesh.parsers.logctx import FormatContext, http_severity

S = settings(max_examples=500, deadline=None, suppress_health_check=list(HealthCheck))
CTX = FormatContext(machine="box", service_id="svc", app="app")

# ---- Property 2: NCSA ---------------------------------------------------------------------------

TOKEN = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789_-", min_size=1, max_size=12)
IP = st.sampled_from(["203.0.113.7", "198.51.100.1", "2001:db8::1", "10.0.0.1"])
METHOD = st.sampled_from(["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS", "PATCH"])
PATH = st.lists(TOKEN, max_size=4).map(lambda parts: "/" + "/".join(parts))
QUERY = st.one_of(st.just(""), TOKEN.map(lambda t: "?Qx" + t + "=v&e=a@b.c"), TOKEN.map(lambda t: "#Fx" + t))
STATUS = st.integers(min_value=100, max_value=599)
PROTO = st.sampled_from(["", " HTTP/1.0", " HTTP/1.1", " HTTP/2.0"])
COMBINED = st.one_of(
    st.just(""),
    st.tuples(TOKEN, QUERY, TOKEN).map(lambda t: f' "http://{t[0]}.example/p{t[1]}" "Agent/{t[2]}"'),
)


@S
@given(IP, METHOD, PATH, QUERY, PROTO, STATUS, st.one_of(st.just("-"), st.integers(0, 10**7).map(str)), COMBINED)
def test_property_2_ncsa_status_mapping_and_message(ip, method, path, query, proto, status, size, combined):
    line = f'{ip} - - [10/Oct/2026:13:55:36 -0700] "{method} {path}{query}{proto}" {status} {size}{combined}'
    out = get_format("ncsa").parse_line(line, CTX)
    assert out.matched and out.event is not None
    ev = out.event
    assert ev.severity == http_severity(status)
    assert ev.message == f"{method} {path} -> {status}"
    for text in (ev.message, ev.raw):
        assert "?" not in text and "#" not in text and "Qx" not in text and "Fx" not in text


# ---- Property 3: W3C ----------------------------------------------------------------------------

OPTIONAL = ["date", "time", "s-ip", "cs-uri-query", "s-port", "cs-username", "c-ip", "sc-substatus",
            "cs(User-Agent)"]
REQUIRED = ["cs-method", "cs-uri-stem", "sc-status"]


@st.composite
def w3c_case(draw):
    extra = draw(st.lists(st.sampled_from(OPTIONAL), unique=True, max_size=len(OPTIONAL)))
    with_win32 = draw(st.booleans())
    names = REQUIRED + extra + (["sc-win32-status"] if with_win32 else [])
    names = draw(st.permutations(names))
    method = draw(METHOD)
    stem = draw(PATH)
    status = draw(STATUS)
    win32 = draw(st.integers(0, 200)) if with_win32 else 0
    values = {n: "-" for n in names}
    values.update({"cs-method": method, "cs-uri-stem": stem, "sc-status": str(status), "sc-win32-status": str(win32)})
    if "cs-uri-query" in values:
        values["cs-uri-query"] = "Qx" + draw(TOKEN) + "=1"
    return names, values, method, stem, status, win32, with_win32


@S
@given(w3c_case())
def test_property_3_w3c_column_mapping(case):
    names, values, method, stem, status, win32, with_win32 = case
    fmt = get_format("w3c")
    ctx = fmt.update("#Fields: " + " ".join(names), CTX)
    assert ctx.w3c_fields == tuple(names)
    out = fmt.parse_line(" ".join(values[n] for n in names), ctx)
    assert out.matched and out.event is not None
    ev = out.event
    expected = http_severity(status)
    if with_win32 and win32 != 0:
        expected = min(expected, 4)
    assert ev.severity == expected
    assert ev.message == f"{method} {stem} -> {status}"
    assert "Qx" not in ev.message + ev.raw
    assert (ev.severity <= 4) or not (with_win32 and win32 != 0)


@S
@given(st.lists(st.sampled_from(["#Software: x", "#Version: 1.0", "#Date: 2026-11-18", "#Fields: a b", "#"]),
                min_size=1, max_size=3), st.sampled_from(["", "  ", "\t"]))
def test_property_3_directives_and_blanks_return_none(directives, blank):
    fmt = get_format("w3c")
    ctx = fmt.update("#Fields: sc-status cs-method cs-uri-stem", CTX)
    for line in [*directives, blank]:
        assert fmt.parse_line(line, ctx).event is None


@S
@given(st.lists(TOKEN, min_size=1, max_size=6), st.sampled_from(["with-fields", "no-fields"]))
def test_property_3_no_fields_or_wrong_count_is_plain(cols, mode):
    fmt = get_format("w3c")
    plain = get_format("plain")
    ctx = fmt.update("#Fields: sc-status cs-method cs-uri-stem", CTX) if mode == "with-fields" else CTX
    if mode == "with-fields" and len(cols) == 3:
        cols = [*cols, "extra"]
    line = " ".join(cols)
    out = fmt.parse_line(line, ctx)
    assert not out.matched
    expected = plain.parse_line(line, ctx).event
    assert dataclasses.replace(out.event, ts=0) == dataclasses.replace(expected, ts=0)
