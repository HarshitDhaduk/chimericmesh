"""Properties 5 and 6 of agent-loop: verify is exactly the conjunction; verify is monotone in the files."""
from __future__ import annotations

import dataclasses
import ntpath
import os

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from chimericmesh.agent.verify import VerifyInputs, evaluate_verify
from chimericmesh.approvals.scope import SOURCE_SUFFIXES

NOW = 1_700_000_000.0
ROOT = os.path.abspath(os.sep)
FOLDER = os.path.join(ROOT, "proj")
OTHER = os.path.join(ROOT, "elsewhere")
CANARY = "CANARYX"
KW = {"realpath": os.path.normpath, "normcase": lambda s: s}

NAMES = ["a.py", "b.js", "m.json", ".env", ".env.local", "k.pem", "s.key", "x.txt", "dir", "sub", "id_rsa",
         ".kiro", ".git", ".github", "c.PY", "d.php", "e.cs", "deep", "..", f"{CANARY}.py", "f.pfx", "g.go"]


def join(base: str, parts) -> str:
    return os.path.join(base, *parts)


inside_paths = st.lists(st.sampled_from(NAMES), min_size=1, max_size=4).map(lambda p: join(FOLDER, p))
outside_paths = st.lists(st.sampled_from(NAMES), min_size=1, max_size=3).map(lambda p: join(OTHER, p))
relative_paths = st.lists(st.sampled_from(NAMES), min_size=1, max_size=3).map("/".join)
junk_entries = st.sampled_from([None, 5, b"x", "", "a\x00b", "x\ny", "z" * 400, ["a"], f"/{CANARY}\x1b.py"])
any_path = st.one_of(inside_paths, inside_paths, outside_paths, relative_paths, junk_entries)

approvals = st.one_of(
    st.none(), st.just({}), st.just("x"),
    st.builds(lambda age: {"approved_at": NOW - age}, st.sampled_from([0, 5, 1799, 1800, 1800.5, 1801, 99999, -50])),
    st.just({"approved_at": float("nan")}), st.just({"approved_at": True}), st.just({"approved_at": "soon"}))


@st.composite
def inputs(draw):
    rule = draw(st.one_of(st.none(), st.just("r1")))
    folder = draw(st.sampled_from([FOLDER, FOLDER, FOLDER, None]))
    rule_folder = draw(st.sampled_from([FOLDER, FOLDER + os.sep, OTHER, None]))
    declared_pool = draw(st.lists(st.one_of(inside_paths, outside_paths, relative_paths), max_size=6))
    return VerifyInputs(
        proposal_id=f"p-{CANARY}", status=draw(st.sampled_from(["approved", "approved", "approved", "pending",
                                                                 "denied", "done", "x\n"])),
        approved_by_rule=rule, declared_files=draw(st.one_of(st.none(), st.just(tuple(declared_pool)),
                                                              st.just(tuple(declared_pool)))),
        approval=draw(approvals),
        rule_status=draw(st.sampled_from([None, "active", "active", "active", "revoked", "suspended", "expired"])),
        rule_code_folder=rule_folder, rule_expires_at=draw(st.sampled_from([None, NOW - 1, NOW, NOW + 1, NOW + 9999])),
        paused=draw(st.booleans()), approvals_available=draw(st.sampled_from([True, True, True, False])),
        code_folder=folder, has_service=draw(st.booleans()), now=NOW, protected_dirs=())


def is_abs(p: str) -> bool:
    return os.path.isabs(p) or ntpath.isabs(p)


def valid_entry(p) -> bool:
    return (isinstance(p, str) and 0 < len(p) <= 300 and not any(ord(ch) < 32 or 127 <= ord(ch) < 160 for ch in p)
            and is_abs(p))


def strictly_inside(path: str, folder: str) -> bool:
    p, f = os.path.normpath(path), os.path.normpath(folder)
    try:
        return p != f and os.path.commonpath([p, f]) == f
    except ValueError:
        return False


def protected_name(path: str, folder: str) -> bool:
    rel = os.path.relpath(os.path.normpath(path), os.path.normpath(folder)).replace(os.sep, "/")
    parts = rel.split("/")
    base = parts[-1]
    return (any(p in (".git", ".kiro", ".github") for p in parts[:-1]) or base == ".env" or base.startswith(".env.")
            or base.endswith((".pem", ".key", ".pfx")) or base.startswith("id_rsa"))


def declared_key(raw, folder):
    if not isinstance(raw, str) or not raw or len(raw) > 300:
        return None
    if is_abs(raw):
        return os.path.normpath(raw)
    return os.path.normpath(os.path.join(folder, raw)) if folder else None


def oracle(inp: VerifyInputs, files) -> tuple[bool, int]:
    """(ok, number of failed checks among 3.1a, b, c, d, e (O13), f) written from the requirement text."""
    a = inp.status == "approved"
    if inp.approved_by_rule is None:
        b = True
    else:
        t = inp.approval.get("approved_at") if isinstance(inp.approval, dict) else None
        timed = isinstance(t, (int, float)) and not isinstance(t, bool) and t == t and abs(t) != float("inf")
        b = (inp.approvals_available and inp.rule_status == "active" and not inp.paused
             and inp.rule_expires_at is not None and NOW < inp.rule_expires_at
             and inp.rule_code_folder is not None and inp.code_folder is not None
             and os.path.normpath(inp.rule_code_folder) == os.path.normpath(inp.code_folder)
             and timed and NOW - t <= 1800)
    seq = files if isinstance(files, (list, tuple)) else []
    c = isinstance(files, (list, tuple)) and 1 <= len(files) <= 20 and all(valid_entry(x) for x in files)
    entries = [x for x in seq[:20] if valid_entry(x)]
    declared = inp.declared_files
    if declared is None and inp.approved_by_rule is not None:
        declared = ()
    d = True
    if declared is not None:
        keys = {declared_key(x, inp.code_folder) for x in declared} - {None}
        for x in entries:
            if os.path.normpath(x) not in keys:
                d = False
                break
            if inp.code_folder and (not strictly_inside(x, inp.code_folder) or protected_name(x, inp.code_folder)
                                    or os.path.splitext(x)[1].lower() not in SOURCE_SUFFIXES):
                d = False
                break
    e = True                                                  # 3.1e (O13): a user approval without a declared list
    if declared is None and inp.code_folder:
        e = not any(strictly_inside(x, inp.code_folder) and protected_name(x, inp.code_folder) for x in entries)
    f = True if inp.code_folder is None else all(strictly_inside(x, inp.code_folder) for x in entries)
    checks = [a, bool(b), c, d, e, f]
    return all(checks), checks.count(False)


file_lists = st.one_of(st.lists(any_path, max_size=23), st.just(None), st.just("abc"), st.just(7))


@settings(max_examples=250, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(inputs(), file_lists)
def test_property_5_verify_is_exactly_the_conjunction(inp, files):
    result = evaluate_verify(inp, files, **KW)
    ok, failed = oracle(inp, files)
    assert result.ok is ok
    note = 1 if inp.code_folder is None else 0
    assert len(result.reasons) == failed + note
    for reason in result.reasons:                                  # fixed texts: no log, file or proposal text
        assert CANARY not in reason and "\n" not in reason and "\x1b" not in reason
    if result.ok:
        assert inp.status == "approved"                            # never ok for pending, denied or done


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(inputs(), file_lists)
def test_property_5_a_none_rule_column_is_a_user_approval_whatever_the_rule_fields_say(inp, files):
    """``decided_by`` is not an input at all, so text such as ``rule:r1`` cannot matter; with the proposal
    column ``approved_by_rule`` of ``None`` the rule fields (revoked, paused, expired) are never read."""
    user = dataclasses.replace(inp, approved_by_rule=None)
    noisy = dataclasses.replace(user, rule_status="revoked", paused=True, rule_expires_at=NOW - 1,
                                rule_code_folder=OTHER, approvals_available=False)
    assert evaluate_verify(user, files, **KW) == evaluate_verify(noisy, files, **KW)
    assert "decided_by" not in {f.name for f in dataclasses.fields(VerifyInputs)}

@settings(max_examples=100, deadline=None)
@given(inputs(), file_lists)
def test_property_5_never_raises_and_status_is_a_fixed_word(inp, files):
    result = evaluate_verify(inp, files, **KW)
    assert result.status in ("pending", "approved", "denied", "done", "unknown")


GOOD = ["a.py", "b.js", "c.php", "g.go", "d.cs"]


@st.composite
def near_ok(draw):
    """A case that is ok, then up to two small mutations, so both outcomes are well covered."""
    files = [join(FOLDER, (draw(st.sampled_from(["pkg", "lib"])), n)) for n in draw(st.lists(st.sampled_from(GOOD), min_size=1, max_size=5, unique=True))]
    rule = draw(st.booleans())
    inp = VerifyInputs(
        proposal_id="p1", status="approved", approved_by_rule="r1" if rule else None,
        declared_files=tuple(files), approval={"approved_at": NOW - 60} if rule else None,
        rule_status="active" if rule else None, rule_code_folder=FOLDER if rule else None,
        rule_expires_at=NOW + 100 if rule else None, paused=False, approvals_available=True,
        code_folder=FOLDER, has_service=True, now=NOW, protected_dirs=())
    sent = list(files)
    for _ in range(draw(st.integers(0, 2))):
        kind = draw(st.sampled_from(["status", "rulestatus", "paused", "expiry", "folder", "age", "extra_out",
                                     "extra_rel", "extra_undeclared", "extra_protected", "undeclare", "nofolder",
                                     "junk", "approval_none", "unavailable", "none_declared"]))
        if kind == "status":
            inp = dataclasses.replace(inp, status=draw(st.sampled_from(["pending", "denied", "done"])))
        elif kind == "rulestatus":
            inp = dataclasses.replace(inp, rule_status=draw(st.sampled_from(["revoked", "suspended", None])))
        elif kind == "paused":
            inp = dataclasses.replace(inp, paused=True)
        elif kind == "expiry":
            inp = dataclasses.replace(inp, rule_expires_at=NOW)
        elif kind == "folder":
            inp = dataclasses.replace(inp, rule_code_folder=OTHER)
        elif kind == "age":
            inp = dataclasses.replace(inp, approval={"approved_at": NOW - draw(st.sampled_from([1800, 1801]))})
        elif kind == "extra_out":
            sent.append(join(OTHER, ("x.py",)))
        elif kind == "extra_rel":
            sent.append("pkg/a.py")
        elif kind == "extra_undeclared":
            sent.append(join(FOLDER, ("pkg", "undeclared.py")))
        elif kind == "extra_protected":
            sent.append(join(FOLDER, ("pkg", ".env")))
            inp = dataclasses.replace(inp, declared_files=inp.declared_files + (sent[-1],))
        elif kind == "undeclare":
            inp = dataclasses.replace(inp, declared_files=inp.declared_files[1:])
        elif kind == "nofolder":
            inp = dataclasses.replace(inp, code_folder=None, has_service=False)
        elif kind == "junk":
            sent.append(draw(junk_entries))
        elif kind == "approval_none":
            inp = dataclasses.replace(inp, approval=None)
        elif kind == "unavailable":
            inp = dataclasses.replace(inp, approvals_available=False)
        else:
            inp = dataclasses.replace(inp, declared_files=None)
    if draw(st.booleans()):
        sent = draw(st.permutations(sent))
    return inp, sent


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(near_ok())
def test_property_5_near_ok_cases_match_the_oracle(case):
    inp, sent = case
    result = evaluate_verify(inp, sent, **KW)
    ok, failed = oracle(inp, sent)
    assert result.ok is ok
    assert len(result.reasons) == failed + (1 if inp.code_folder is None else 0)
    assert all(CANARY not in r for r in result.reasons)


# ---- property 6 (optional): monotone in the files -------------------------------------------------------

good_names = st.lists(st.sampled_from(["a.py", "b.js", "c.php", "d.cs", "g.go", "h.ts"]), min_size=1, max_size=4)


@settings(max_examples=80, deadline=None)
@given(st.lists(st.tuples(st.sampled_from(["pkg", "lib", "src"]), st.sampled_from(["a.py", "b.js", "c.php", "g.go"])),
                min_size=1, max_size=20, unique=True), st.data())
def test_property_6_verify_is_monotone_in_the_files(pairs, data):
    files = [join(FOLDER, p) for p in pairs]
    inp = VerifyInputs(proposal_id="p1", status="approved", approved_by_rule=None, declared_files=tuple(files),
                       approval=None, rule_status=None, rule_code_folder=None, rule_expires_at=None, paused=False,
                       approvals_available=True, code_folder=FOLDER, has_service=True, now=NOW, protected_dirs=())
    assert evaluate_verify(inp, files, **KW).ok
    subset = data.draw(st.lists(st.sampled_from(files), min_size=1, max_size=len(files), unique=True))
    assert evaluate_verify(inp, subset, **KW).ok
    extra = join(FOLDER, ("undeclared", "z.py"))
    if len(files) < 20:
        assert not evaluate_verify(inp, files + [extra], **KW).ok
