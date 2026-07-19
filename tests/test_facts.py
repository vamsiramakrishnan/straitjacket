"""Mechanical referee: M-G fact store + M-I Angle-lite joins (ctx.facts).

Seeded fixture repo (module-scoped, one REAL pytest subprocess):

    src/alpha.py   CHANGED   boom() raises ValueError inside its range;
                             shared_trip() (hidden from the top-level
                             skeleton census behind ``if True:``) raises a
                             second ValueError in the same file — the
                             shared-class pair, deliberately NOT inside
                             any indexed symbol.
    src/beta.py    CHANGED   crunch() raises KeyError inside its range.
    src/gamma.py   untouched stable() raises RuntimeError — flake lane.
    tests/test_suite.py      4 failing tests + passing ballast.

One generation where exactly 2 files changed (alpha, beta). The referee
asserts, over facts derived by the REAL extractor from the REAL captured
bytes:

- root-cause join returns EXACTLY the 2 changed-symbol failures, with
  symbol names (boom, crunch);
- untouched_failures returns EXACTLY the 1 gamma failure;
- shared_cause_groups groups the (src/alpha.py, ValueError) pair;
- determinism: two independent derivations (separate state roots) give
  byte-identical results;
- idempotency: re-deriving everything changes no row counts;
- the whole answer fits one bounded census digest, and is < 1/20 of the
  raw pytest bytes it replaced (both numbers printed);
- degraded mode: no ctx.skeleton → decl/imp empty, fail/changed joins
  still work at declared file-level precision;
- corrupt facts.sqlite → recreated, never raised.

Skeleton facts come from a CONTRACT STUB of ``ctx.skeleton/v1`` (the
frozen schema in src/ctx/skeleton.py's docstring) so the referee is
deterministic regardless of which skeleton backend (tree-sitter / ctags /
ast) is installed; one tolerant integration test exercises the real
module when importable.
"""

from __future__ import annotations

import ast
import json
import os
import re
import sqlite3
import subprocess
import sys
import textwrap
import types
from pathlib import Path

import pytest
from conftest import make_store, make_ws

# --------------------------------------------------------------- seed files
ALPHA_V1 = textwrap.dedent('''\
    """Alpha module (healthy committed version)."""

    def helper_a(x):
        return x + 1

    def boom():
        return "ok"
''')

ALPHA_V2 = textwrap.dedent('''\
    """Alpha module: seeded changed file #1."""

    def helper_a(x):
        return x + 1

    def boom():
        # a body long enough that the real traceback carries real weight
        staged = []
        for step in range(3):
            staged.append(step * 2)
        total = sum(staged)
        marker = "alpha marker %d" % total
        payload = {"marker": marker, "staged": staged}
        checked = payload["marker"].upper()
        audit = [checked, marker, str(total)]
        joined = " | ".join(audit)
        witness = joined.lower()
        assert witness
        raise ValueError("alpha boom: changed-symbol failure (" + checked + ")")

    if True:  # hides shared_trip from the top-level skeleton census (v1 tier)
        def shared_trip():
            # second ValueError in alpha.py -> the shared-cause pair; the
            # raise line is deliberately OUTSIDE every indexed decl range
            basket = {"kind": "pair", "cls": "ValueError"}
            label = "%(kind)s/%(cls)s" % basket
            trail = [label] * 3
            merged = ";".join(trail)
            stamped = merged.title()
            assert stamped
            raise ValueError("alpha shared trip: paired failure class " + stamped)
''')

BETA_V1 = textwrap.dedent('''\
    """Beta module (healthy committed version)."""

    def crunch(config):
        return 1

    def helper_b():
        return 2
''')

BETA_V2 = textwrap.dedent('''\
    """Beta module: seeded changed file #2."""

    def crunch(config):
        # long body: the traceback block should look like real life
        table = {"present": 1, "config": bool(config)}
        keys = sorted(table)
        audit = ["%s=%s" % (k, table[k]) for k in keys]
        joined = ", ".join(audit)
        prefix = "beta crunch over " + joined
        witness = prefix.upper()
        ledger = {"prefix": prefix, "witness": witness}
        checked = ledger["witness"].strip()
        assert checked
        return table["missing-key"]

    def helper_b():
        return 2
''')

GAMMA = textwrap.dedent('''\
    """Gamma module: untouched since the last commit."""

    def stable(flag):
        # untouched code that still fails -> the flake/suspect triage lane
        channel = ["a", "b", str(flag)]
        label = "-".join(channel)
        folded = label.upper()
        trace = {"label": label, "folded": folded}
        detail = "%(label)s :: %(folded)s" % trace
        stamped = detail.center(40, ".")
        assert stamped
        raise RuntimeError("gamma stable failure in untouched code: " + stamped)
''')

SUITE = textwrap.dedent('''\
    """Referee suite: 4 seeded failures + passing ballast.

    Failing tests reach the modules through two local hops each — real
    suites rarely call straight into the failing symbol, and the referee's
    token comparison should price realistic traceback depth.
    """

    import alpha
    import beta
    import gamma


    def call_alpha():
        # first hop of the seeded call chain
        settings = {"lane": "alpha", "hop": 1}
        badge = "%(lane)s#%(hop)d" % settings
        assert badge
        return drive_alpha(badge)


    def drive_alpha(badge):
        # second hop, with enough locals to look like real glue code
        context = {"badge": badge, "attempt": "first"}
        checked = sorted(context)
        assert checked == ["attempt", "badge"]
        return alpha.boom()


    def call_beta():
        options = {"cfg": True, "retries": 2}
        summary = ",".join(sorted(options))
        assert summary
        return drive_beta(options)


    def drive_beta(options):
        staged = dict(options)
        staged["stage"] = "drive"
        assert staged["stage"] == "drive"
        return beta.crunch(staged)


    def call_gamma():
        flags = ["z", "y"]
        chosen = flags[0]
        assert chosen == "z"
        return drive_gamma(chosen)


    def drive_gamma(flag):
        trail = {"flag": flag, "hop": 2}
        assert trail["hop"] == 2
        return gamma.stable(trail["flag"])


    def call_shared():
        route = ("alpha", "shared_trip")
        assert len(route) == 2
        return drive_shared(route)


    def drive_shared(route):
        label = "::".join(route)
        assert label == "alpha::shared_trip"
        return alpha.shared_trip()


    def test_alpha_boom():
        assert call_alpha() == "ok"


    def test_beta_crunch():
        assert call_beta() == 1


    def test_gamma_stable():
        assert call_gamma() == "z"


    def test_alpha_shared_trip():
        assert call_shared() is None


    def test_ballast_helper_a():
        assert alpha.helper_a(1) == 2


    def test_ballast_helper_b():
        assert beta.helper_b() == 2


    def test_ballast_join():
        assert "-".join(["a", "b"]) == "a-b"


    def test_ballast_sort():
        assert sorted([3, 1, 2]) == [1, 2, 3]
''')

FIX_CONFTEST = textwrap.dedent('''\
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
''')

CHANGED_FILES = ["src/alpha.py", "src/beta.py"]
ROOT_CAUSE_TESTS = {
    "tests/test_suite.py::test_alpha_boom": ("boom", "src/alpha.py", "ValueError"),
    "tests/test_suite.py::test_beta_crunch": ("crunch", "src/beta.py", "KeyError"),
}

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
}


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, env=_GIT_ENV,
                   capture_output=True, timeout=30)


# ------------------------------------------------------------------ fixtures
@pytest.fixture(scope="module")
def fixture_repo(tmp_path_factory):
    """Seeded git repo + ONE real pytest subprocess run (raw bytes kept)."""
    root = tmp_path_factory.mktemp("facts-fixture")
    (root / "src").mkdir()
    (root / "tests").mkdir()
    (root / "conftest.py").write_text(FIX_CONFTEST, encoding="utf-8")
    (root / "src" / "alpha.py").write_text(ALPHA_V1, encoding="utf-8")
    (root / "src" / "beta.py").write_text(BETA_V1, encoding="utf-8")
    (root / "src" / "gamma.py").write_text(GAMMA, encoding="utf-8")
    (root / "tests" / "test_suite.py").write_text(SUITE, encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "seed")
    # THE generation: exactly two files changed since the commit.
    (root / "src" / "alpha.py").write_text(ALPHA_V2, encoding="utf-8")
    (root / "src" / "beta.py").write_text(BETA_V2, encoding="utf-8")

    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("PYTEST_ADDOPTS", None)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-v", "--tb=long",
         "-p", "no:cacheprovider"],
        cwd=root, env=env, capture_output=True, text=True, timeout=180,
    )
    assert "4 failed" in proc.stdout, proc.stdout
    return {"root": root, "stdout": proc.stdout}


def _stub_skeleton_module() -> types.ModuleType:
    """Contract stub of the FROZEN ctx.skeleton/v1 seam: top-level symbols
    (+ class methods) via stdlib ast, exact end_lineno ranges, minted
    region spans, import module names."""
    mod = types.ModuleType("ctx.skeleton")
    mod.SKELETON_SCHEMA = "ctx.skeleton/v1"

    def skeleton_for(store, ws, rel_path):
        full = ws.confine(rel_path, must_exist=True)
        data = full.read_bytes()
        blob = store.put_blob(data)
        tree = ast.parse(data.decode("utf-8", "replace"))
        symbols, imports = [], []

        def visit(body, scope):
            for node in body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    symbols.append((node.name, "method" if scope else "function",
                                    node.lineno, int(node.end_lineno or node.lineno),
                                    scope))
                elif isinstance(node, ast.ClassDef):
                    symbols.append((node.name, "class", node.lineno,
                                    int(node.end_lineno or node.lineno), scope))
                    visit(node.body, node.name)

        visit(tree.body, None)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)
        rel = ws.relativize(full)
        return {
            "schema": "ctx.skeleton/v1",
            "file": rel,
            "blob": f"sha256:{blob}",
            "language": "python",
            "parser": "referee-stub-ast",
            "symbols": [
                {"name": n, "kind": k, "signature": f"{n}(...)", "range": [a, b],
                 "scope": s, "span": store.register_span(blob, "region", a=a, b=b)}
                for n, k, a, b, s in symbols
            ],
            "imports": sorted(dict.fromkeys(imports)),
        }

    mod.skeleton_for = skeleton_for
    return mod


@pytest.fixture()
def stub_skeleton(monkeypatch):
    monkeypatch.setitem(sys.modules, "ctx.skeleton", _stub_skeleton_module())


def _derive_all(store, ws, raw_stdout: str):
    """Capture the real pytest bytes into the store and derive every plane.
    Returns (cap, results-dict)."""
    import ctx.facts as facts
    from ctx.execution import generation_hash, run_capture

    cap = run_capture(
        ws,
        [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"],
        store=store,
        stdin_bytes=raw_stdout.encode("utf-8"),
    )
    files = {}
    for rel in ["src/alpha.py", "src/beta.py", "src/gamma.py",
                "tests/test_suite.py"]:
        files[rel] = facts.derive_file(store, ws, rel)
    gen = facts.derive_generation(ws, generation_hash(ws.root), None, store=store)
    run = facts.derive_run(store, ws, cap.manifest)
    return cap, {"files": files, "gen": gen, "run": run}


@pytest.fixture()
def derived(fixture_repo, state_home, stub_skeleton):
    """Fresh state root + full derivation of the fixture's facts."""
    ws = make_ws(fixture_repo["root"])
    store = make_store(ws)
    cap, results = _derive_all(store, ws, fixture_repo["stdout"])
    return {"ws": ws, "store": store, "cap": cap, "raw": fixture_repo["stdout"],
            **results}


# ------------------------------------------------------------- derivation
def test_derivation_populates_all_planes(derived):
    import ctx.facts as facts

    for rel, r in derived["files"].items():
        assert r["ok"] and r["skeleton"] and r["decl"] >= 1, (rel, r)
    assert derived["gen"]["ok"]
    assert derived["gen"]["changed"] == 2  # exactly alpha + beta
    assert derived["run"]["ok"]
    assert derived["run"]["fail"] == 4  # all four failures carried file:line loci
    assert derived["run"]["no_location"] == 0
    assert facts.facts_db_path(derived["store"]).is_file()
    # Short-id discipline: run and generation ids are 12-hex, reflex-style.
    con = sqlite3.connect(facts.facts_db_path(derived["store"]))
    try:
        for run_id, gen in con.execute("SELECT run_id, generation FROM fail"):
            assert re.fullmatch(r"[0-9a-f]{12}", run_id)
            assert gen is None or re.fullmatch(r"[0-9a-f]{12}", gen)
        for (gen,) in con.execute("SELECT DISTINCT generation FROM changed"):
            assert re.fullmatch(r"[0-9a-f]{12}", gen)
    finally:
        con.close()


# ------------------------------------------------------ the root-cause join
def test_root_cause_join_exactly_the_two_changed_symbol_failures(derived):
    import ctx.facts as facts

    rows = facts.failing_in_changed(derived["ws"], derived["store"])
    assert len(rows) == 2, rows
    by_test = {r["test"]: r for r in rows}
    assert set(by_test) == set(ROOT_CAUSE_TESTS)
    for test, (symbol, file, cls) in ROOT_CAUSE_TESTS.items():
        row = by_test[test]
        assert row["symbol"] == symbol
        assert row["file"] == file
        assert row["failure_class"] == cls
        assert isinstance(row["line"], int) and row["line"] >= 1
        assert row["span"], row  # symbol-range precision carries a minted span
    # Deterministic order: (file, line, test) — alpha before beta.
    assert [r["file"] for r in rows] == ["src/alpha.py", "src/beta.py"]
    # The shared_trip ValueError is in a CHANGED file but outside every
    # indexed symbol: the symbol-precision join must exclude it.
    assert "tests/test_suite.py::test_alpha_shared_trip" not in by_test


def test_untouched_failures_returns_exactly_the_gamma_failure(derived):
    import ctx.facts as facts

    rows = facts.untouched_failures(derived["ws"], derived["store"])
    assert len(rows) == 1, rows
    assert rows[0]["test"] == "tests/test_suite.py::test_gamma_stable"
    assert rows[0]["file"] == "src/gamma.py"
    assert rows[0]["failure_class"] == "RuntimeError"


def test_shared_cause_groups_the_valueerror_pair(derived):
    import ctx.facts as facts

    groups = facts.shared_cause_groups(derived["ws"], derived["store"])
    pairs = [g for g in groups if g["group"] == "file+class"]
    assert len(pairs) == 1, groups
    g = pairs[0]
    assert (g["file"], g["failure_class"], g["count"]) == ("src/alpha.py", "ValueError", 2)
    assert g["tests"] == [
        "tests/test_suite.py::test_alpha_boom",
        "tests/test_suite.py::test_alpha_shared_trip",
    ]
    # No symbol shares two failures in this fixture — symbol-axis empty.
    assert not [g for g in groups if g["group"] == "symbol"]


def test_symbol_neighbors_v1_with_honest_precision(derived):
    import ctx.facts as facts

    rows = facts.symbol_neighbors(derived["ws"], derived["store"], "boom")
    rels = {r["rel"] for r in rows}
    assert {"decl", "importer", "scope-sibling"} <= rels
    decl = [r for r in rows if r["rel"] == "decl"][0]
    assert decl["file"] == "src/alpha.py" and decl["span"]
    assert decl["precision"] == "exact (skeleton line range)"
    importers = [r for r in rows if r["rel"] == "importer"]
    assert {r["file"] for r in importers} == {"tests/test_suite.py"}
    assert "not symbol-precise" in importers[0]["precision"]  # v1 tier, declared
    siblings = {r["symbol"] for r in rows if r["rel"] == "scope-sibling"}
    assert "helper_a" in siblings


# --------------------------------------------------- determinism/idempotency
def test_two_independent_derivations_are_byte_identical(derived, tmp_path):
    import ctx.facts as facts
    from ctx.store import Store

    ws = derived["ws"]
    store2 = Store(ws.workspace_id, state_root=tmp_path / "state2")
    _derive_all(store2, ws, derived["raw"])

    def answers(store):
        return json.dumps(
            {
                "root_cause": facts.failing_in_changed(ws, store),
                "untouched": facts.untouched_failures(ws, store),
                "shared": facts.shared_cause_groups(ws, store),
            },
            sort_keys=True,
        )

    assert answers(derived["store"]) == answers(store2)
    # And pure re-query in the same store is trivially stable too.
    a = facts.render_census(facts.failing_in_changed(ws, derived["store"]),
                            kind="root-cause")
    b = facts.render_census(facts.failing_in_changed(ws, derived["store"]),
                            kind="root-cause")
    assert a == b


def test_rederivation_is_idempotent(derived):
    import ctx.facts as facts

    ws, store = derived["ws"], derived["store"]
    before = facts.fact_counts(store)
    assert before["decl"] >= 4 and before["fail"] == 4 and before["changed"] == 2
    for rel in derived["files"]:
        r = facts.derive_file(store, ws, rel)
        assert r["ok"] and r["skipped"], r  # content-keyed: no-op
    g = facts.derive_generation(ws, None, None, store=store)
    assert g["ok"] and g["skipped"], g
    r = facts.derive_run(store, ws, derived["cap"].manifest)
    assert r["ok"] and r["skipped"] and r["fail"] == 4, r
    assert facts.fact_counts(store) == before


# ------------------------------------------------- one bounded digest + cost
def test_answer_is_one_bounded_census_within_token_budget(derived, capsys):
    import ctx.facts as facts

    rows = facts.failing_in_changed(derived["ws"], derived["store"])
    census = facts.render_census(rows, kind="root-cause: failing in changed symbols")
    raw_bytes = int(derived["cap"].manifest["streams"]["stdout"]["bytes"])
    answer_bytes = len(census.encode("utf-8"))
    print("\n--- root-cause join census (verbatim) ---")
    print(census)
    print(f"raw pytest stdout: {raw_bytes} bytes · join answer: {answer_bytes} "
          f"bytes · ratio {raw_bytes / answer_bytes:.1f}x")
    # Census contract: rows REQUIRED (both identities named), omission declared.
    assert "2 of 2 rows" in census
    assert "symbol=boom" in census and "symbol=crunch" in census
    assert "test_alpha_boom" in census and "test_beta_crunch" in census
    # Declared omission under a squeezed cap — identity is never silently cut.
    capped = facts.render_census(rows, kind="root-cause", cap=1)
    assert "1 of 2 rows" in capped and "+1 rows omitted (declared)" in capped
    # The token claim, mechanically: the answer replaced the raw output at
    # <1/20 of its bytes.
    assert answer_bytes * 20 < raw_bytes, (answer_bytes, raw_bytes)


# ------------------------------------------------------------- degraded mode
def test_degraded_mode_without_skeleton_module(fixture_repo, state_home, monkeypatch):
    """No ctx.skeleton anywhere: decl/imp stay empty, fail/changed planes
    and their joins keep working at declared file-level precision."""
    import ctx.facts as facts

    monkeypatch.setitem(sys.modules, "ctx.skeleton", None)  # import → ImportError
    ws = make_ws(fixture_repo["root"])
    store = make_store(ws)
    _cap, results = _derive_all(store, ws, fixture_repo["stdout"])
    for r in results["files"].values():
        assert r["ok"] and not r["skeleton"] and r["decl"] == 0  # degraded, not broken
    assert results["gen"]["ok"] and results["gen"]["changed"] == 2
    assert results["run"]["ok"] and results["run"]["fail"] == 4
    counts = facts.fact_counts(store)
    assert counts["decl"] == 0 and counts["imp"] == 0
    assert counts["fail"] == 4 and counts["changed"] == 2

    # File-level root-cause: all 3 failures in changed files, honestly labeled.
    rows = facts.failing_in_changed(ws, store)
    assert len(rows) == 3, rows
    assert all(r["symbol"] is None and r["span"] is None for r in rows)
    assert all(r["precision"] == "file-level (no skeleton facts)" for r in rows)
    assert {r["test"] for r in rows} == {
        "tests/test_suite.py::test_alpha_boom",
        "tests/test_suite.py::test_alpha_shared_trip",
        "tests/test_suite.py::test_beta_crunch",
    }
    # Untouched triage is skeleton-independent: still exactly gamma.
    untouched = facts.untouched_failures(ws, store)
    assert [r["test"] for r in untouched] == ["tests/test_suite.py::test_gamma_stable"]
    # Class-axis shared cause is skeleton-independent too.
    pairs = [g for g in facts.shared_cause_groups(ws, store)
             if g["group"] == "file+class"]
    assert len(pairs) == 1 and pairs[0]["count"] == 2


def test_corrupt_db_is_recreated_never_raised(derived):
    import ctx.facts as facts

    ws, store = derived["ws"], derived["store"]
    path = facts.facts_db_path(store)
    for suffix in ("-wal", "-shm"):
        p = Path(str(path) + suffix)
        if p.exists():
            p.unlink()
    path.write_bytes(b"this is not a sqlite database, referee says so")
    # Queries fail open (empty), never raise; the file is recreated.
    assert facts.failing_in_changed(ws, store) == []
    assert facts.untouched_failures(ws, store) == []
    # Re-derivation rebuilds the store and the join comes back whole.
    _cap, results = _derive_all(store, ws, derived["raw"])
    assert results["run"]["ok"] and results["gen"]["ok"]
    rows = facts.failing_in_changed(ws, store)
    assert {r["symbol"] for r in rows} == {"boom", "crunch"}


# ------------------------------------------------------------------ q stages
def test_stage_registration_against_a_local_registry():
    """The guarded registration path, no ctx.query needed: names, kinds,
    and source/filter shapes match the frozen registry contract."""
    import ctx.facts as facts

    seen = {}

    def register(name, fn, *, input_kinds, output_kind, doc):
        seen[name] = (tuple(input_kinds), output_kind, doc)

    assert facts.register_facts_stages(register) is True
    assert seen["fails"] == ((), "sites", seen["fails"][2])
    assert seen["in-changed"][0] == ("sites",) and seen["in-changed"][1] == "sites"
    assert seen["decls"] == ((), "symbols", seen["decls"][2])
    assert seen["shared-cause"][0] == ("sites",)
    assert seen["shared-cause"][1] == "records"

    def refuses(*a, **k):
        raise RuntimeError("registry says no")

    assert facts.register_facts_stages(refuses) is False  # guarded, never raises


def test_q_pipeline_end_to_end(derived):
    """Integration with engineer B's ctx.query (skipped only if their
    module is absent/unimportable): the composed root-cause pipeline."""
    query = pytest.importorskip("ctx.query")
    import ctx.facts as facts

    assert facts.register_facts_stages() is True  # idempotent re-register
    ws, store = derived["ws"], derived["store"]
    for name in ("fails", "in-changed", "decls", "shared-cause"):
        assert name in query.STAGES

    out, code = query.run_query(ws, store, "fails last | in-changed")
    assert code == 0, out
    assert "test_alpha_boom" in out and "test_beta_crunch" in out
    assert "test_gamma_stable" not in out
    assert "test_alpha_shared_trip" not in out  # outside every changed symbol

    out2, code2 = query.run_query(ws, store, "fails last | shared-cause")
    assert code2 == 0, out2
    assert "src/alpha.py" in out2 and "ValueError" in out2

    out3, code3 = query.run_query(ws, store, "decls --kind function | where file=src/alpha.py")
    assert code3 == 0, out3
    assert "boom" in out3 and "shared_trip" not in out3  # hidden def stays hidden


# ----------------------------------------------------- real-skeleton contact
def test_derive_file_with_real_skeleton_module(fixture_repo, state_home):
    """Tolerant integration: whatever backend engineer A's module picked
    (tree-sitter/ctags/ast), derive_file must ingest its frozen schema."""
    pytest.importorskip("ctx.skeleton")
    import ctx.facts as facts

    ws = make_ws(fixture_repo["root"])
    store = make_store(ws)
    r = facts.derive_file(store, ws, "src/gamma.py")
    assert r["ok"], r
    if r["skeleton"]:
        assert r["decl"] >= 1
        decls = facts.decls_rows(ws, store)
        assert any(d["symbol"] == "stable" for d in decls)
