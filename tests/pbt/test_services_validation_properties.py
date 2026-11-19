"""Properties 1, 2, 4 and 5 of the services design (slug, text, code folder, path containment)."""
from __future__ import annotations

import os
import re
import tempfile

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from chimericmesh.services.errors import ValidationFailed
from chimericmesh.services.paths import normalize_code_folder, path_in_code_folder
from chimericmesh.services.validate import validate_service_fields, validate_slug

WIN = os.name == "nt"
SLUG_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789-"


# ---- Property 1: slug validity -------------------------------------------------------------------

def _oracle_slug(s: str) -> bool:
    """Independent of the regex used by the implementation."""
    return (
        1 <= len(s) <= 40
        and s[0] in SLUG_CHARS[:-1]
        and all(c in SLUG_CHARS for c in s)
        and s != "unassigned"
    )


slug_text = st.one_of(
    st.text(alphabet=SLUG_CHARS + "\n\x00 ABZ_é", max_size=45),
    st.text(max_size=50),
    st.builds(lambda s, tail: s + tail, st.text(alphabet=SLUG_CHARS, min_size=1, max_size=40), st.sampled_from(["", "\n", "\x00", " ", "A"])),
    st.just("unassigned"),
)


@settings(max_examples=400, deadline=None)
@given(slug_text)
def test_property_1_slug_validity(s):
    try:
        got = validate_slug(s)
    except ValidationFailed as exc:
        assert exc.field == "id"
        assert not _oracle_slug(s)
    else:
        assert got == s
        assert _oracle_slug(s)


@pytest.mark.parametrize("s", ["abc\n", "a\x00", "Ab", "a b", "a" * 41, "unassigned"])
def test_property_1_named_rejections(s):
    with pytest.raises(ValidationFailed):
        validate_slug(s)


# ---- Property 2: text is clean and capped --------------------------------------------------------

BAD_CHAR = re.compile(r"[\x00-\x1f\x7f-\x9f\ud800-\udfff]")
chars = st.one_of(
    st.characters(max_codepoint=0x7F),
    st.characters(min_codepoint=0x80, max_codepoint=0xA0),
    st.characters(max_codepoint=0x10FFFF),
    st.sampled_from(["\ud800", "\udfff", "\x00", "\n", " "]),
)
messy = st.text(alphabet=chars, max_size=130)
messy_list = st.lists(messy, max_size=25)


def _model_list(entries: list[str]) -> list[str]:
    out, seen = [], set()
    for e in entries:
        c = BAD_CHAR.sub("", e).strip()
        if not c:
            raise ValueError("empty entry")
        if len(c) > 100:
            raise ValueError("entry too long")
        if c.casefold() not in seen:
            seen.add(c.casefold())
            out.append(c)
    if len(out) > 20:
        raise ValueError("too many")
    return out


@settings(max_examples=300, deadline=None)
@given(messy, messy_list, messy_list)
def test_property_2_text_is_clean_and_capped(name, hosts, apps):
    raw = {"id": "svc", "name": name, "hosts": hosts, "app_names": apps}
    try:
        expected_name = BAD_CHAR.sub("", name).strip()
        assert expected_name and len(expected_name) <= 80
        expected_hosts = _model_list(hosts)
        expected_apps = _model_list(apps)
    except (AssertionError, ValueError):
        with pytest.raises(ValidationFailed):
            validate_service_fields(raw, for_create=True)
        return
    got = validate_service_fields(raw, for_create=True)
    assert got.name == expected_name
    assert list(got.hosts) == expected_hosts
    assert list(got.app_names) == expected_apps
    for value in (got.name, *got.hosts, *got.app_names):
        assert not BAD_CHAR.search(value)
    assert len({h.casefold() for h in got.hosts}) == len(got.hosts)
    assert len(got.hosts) <= 20 and len(got.app_names) <= 20


# ---- Property 4: code folder normalization -------------------------------------------------------

@pytest.fixture(scope="module")
def tree():
    with tempfile.TemporaryDirectory() as tmp:
        base = os.path.realpath(tmp)
        deep = os.path.join(base, "alpha", "Beta gamma", "delta")
        os.makedirs(deep)
        home = os.path.join(base, "home")
        os.makedirs(home)
        yield {"base": base, "deep": deep, "home": home}


def _case(text: str, flips: list[bool]) -> str:
    return "".join(c.swapcase() if flips[i % len(flips)] else c for i, c in enumerate(text))


spellings = st.fixed_dictionaries(
    {
        "seps": st.lists(st.sampled_from(["/", "\\"] if WIN else ["/"]), min_size=1, max_size=4),
        "double": st.booleans(),
        "dots": st.lists(st.booleans(), min_size=1, max_size=6),
        "trailing": st.booleans(),
        "flips": st.lists(st.booleans(), min_size=1, max_size=4) if WIN else st.just([False]),
    }
)


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(spec=spellings)
def test_property_4_spellings_normalize_and_are_idempotent(tree, spec):
    drive, rest = os.path.splitdrive(tree["deep"])
    comps = [c for c in re.split(r"[\\/]", rest) if c]
    raw = drive + os.sep + comps[0]
    for i, comp in enumerate(comps[1:]):
        sep = spec["seps"][i % len(spec["seps"])]
        raw += sep * (2 if spec["double"] else 1)
        if spec["dots"][i % len(spec["dots"])]:
            raw += "." + sep
        raw += comp
    if spec["trailing"]:
        raw += spec["seps"][0]
    raw = _case(raw, spec["flips"])
    got = normalize_code_folder(raw, protected=[], home=tree["home"])
    assert os.path.isabs(got)
    assert ".." not in re.split(r"[\\/]", got)
    assert got == os.path.realpath(tree["deep"])
    assert normalize_code_folder(got, protected=[], home=tree["home"]) == got


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(position=st.integers(0, 3), sep=st.sampled_from(["/", "\\"] if WIN else ["/"]), back=st.sampled_from(["", "x"]))
def test_property_4_any_dotdot_component_is_rejected(tree, position, sep, back):
    comps = [c for c in re.split(r"[\\/]", os.path.splitdrive(tree["deep"])[1]) if c]
    insert = [".." + back] if back else [".."]
    items = comps[: position + 1] + insert + comps[position + 1:]
    raw = os.path.splitdrive(tree["deep"])[0] + sep + sep.join(items)
    if back:  # "..x" is an ordinary name, not a parent reference
        try:
            normalize_code_folder(raw, protected=[], home=tree["home"])
        except ValidationFailed as exc:
            assert exc.field == "code_folder"
        return
    with pytest.raises(ValidationFailed) as exc:
        normalize_code_folder(raw, protected=[], home=tree["home"])
    assert exc.value.field == "code_folder"
    assert "parent" in exc.value.reason


# ---- Property 5: path containment ----------------------------------------------------------------

ROOT = os.path.abspath(os.sep)
BASE = os.path.join(ROOT, "w", "base")
FOLDER = os.path.join(BASE, "code")
OUTSIDE = os.path.join(BASE, "outside")
LINKS = {
    os.path.join(FOLDER, "lnk_out"): OUTSIDE,
    os.path.join(FOLDER, "src", "lnk_in"): os.path.join(FOLDER, "real"),
    os.path.join(FOLDER, "lnk_up"): BASE,
}


def _split(p: str) -> list[str]:
    return [c for c in re.split(r"[\\/]", os.path.splitdrive(p)[1]) if c]


def model_realpath(path: str) -> str:
    """Resolve links, ``.`` and ``..`` component by component (the model of ``os.path.realpath``)."""
    drive = os.path.splitdrive(path)[0]
    pending = _split(path)
    resolved: list[str] = []
    hops = 0
    while pending:
        comp = pending.pop(0)
        if comp == ".":
            continue
        if comp == "..":
            if resolved:
                resolved.pop()
            continue
        candidate = resolved + [comp]
        key = drive + os.sep + os.sep.join(candidate)
        hit = next((t for k, t in LINKS.items() if os.path.normcase(k) == os.path.normcase(key)), None)
        if hit is not None:
            hops += 1
            if hops > 10:
                raise OSError("symlink loop")
            resolved = []
            pending = _split(hit) + pending
        else:
            resolved = candidate
    return drive + os.sep + os.sep.join(resolved)


def _fold(s: str) -> str:
    return s.lower()


def _oracle(candidate_joined: str) -> bool:
    folder = [c.lower() for c in _split(model_realpath(FOLDER))]
    real = [c.lower() for c in _split(model_realpath(candidate_joined))]
    return len(real) > len(folder) and real[: len(folder)] == folder


def _check(candidate: str) -> bool:
    return path_in_code_folder(FOLDER, candidate, realpath=model_realpath, normcase=_fold)


pieces = st.sampled_from(["src", "a.py", "..", ".", "lnk_out", "lnk_in", "lnk_up", "SRC", "real", "x", "code"])
seps = st.sampled_from(["/", "\\"])


@st.composite
def candidates(draw):
    comps = draw(st.lists(pieces, min_size=1, max_size=7))
    out = comps[0]
    for c in comps[1:]:
        out += draw(seps) + c
    return out


@settings(max_examples=1000, deadline=None)
@given(candidates())
def test_property_5_matches_the_model(candidate):
    if os.name == "nt" and ":" in candidate:
        return
    joined = os.path.join(FOLDER, candidate.replace("\\", os.sep).replace("/", os.sep))
    assert _check(candidate) == _oracle(joined)


@st.composite
def children(draw):
    """Net depth of at least one, never above the folder, links only where they stay inside."""
    comps = draw(st.lists(st.sampled_from(["src", "a", "pkg", "SRC", "x.py"]), min_size=1, max_size=5))
    if draw(st.booleans()):
        comps = comps[:-1] + ["."] + comps[-1:]
    if draw(st.booleans()):
        comps = ["src", "lnk_in", "m.py"] if draw(st.booleans()) else ["src", "t", "..", comps[-1]]
    out = comps[0]
    for c in comps[1:]:
        out += draw(seps) + c
    return out


@st.composite
def escapes(draw):
    kind = draw(st.sampled_from(["up", "link", "abs", "uplink", "same"]))
    tail = draw(st.sampled_from(["x", "a/b", "y.py"]))
    if kind == "up":
        n = draw(st.integers(1, 4))
        return draw(seps).join([".."] * n + [tail])
    if kind == "link":
        return draw(seps).join(["lnk_out", tail])
    if kind == "uplink":
        return draw(seps).join(["lnk_up", "outside", tail])
    if kind == "same":
        return draw(st.sampled_from([".", "src/..", "src\\..", "lnk_up/code", "src/../."]))
    return os.path.join(ROOT, "w", "elsewhere", tail)


@settings(max_examples=1000, deadline=None)
@given(children())
def test_property_5_generated_children_are_inside(candidate):
    assert _check(candidate) is True


@settings(max_examples=1000, deadline=None)
@given(escapes())
def test_property_5_generated_escapes_are_outside(candidate):
    assert _check(candidate) is False


@settings(max_examples=200, deadline=None)
@given(st.text(max_size=40), st.sampled_from(["", "a.py", "src/a.py"]))
def test_property_5_nul_is_always_outside(text, ok):
    assert not _check(text + "\x00" + ok)
    assert not path_in_code_folder(FOLDER + "\x00", ok or "a", realpath=model_realpath, normcase=_fold)


@pytest.mark.skipif(not WIN, reason="Windows normalizes trailing dots and spaces")
@settings(max_examples=100, deadline=None)
@given(st.sampled_from(["a.py.", "a.py ", "src ", "src./a.py", "dir /f", "a...", "x:y"]))
def test_property_5_windows_name_tricks_are_outside(candidate):
    assert path_in_code_folder(FOLDER, candidate) is False
