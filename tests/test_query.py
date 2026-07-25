"""Acceptance: ``ctx q`` — the M-H composition algebra (docs/ALGEBRA.md).

THE MECHANICAL REFEREE: the eval-collapse scenarios (S-B data-dependent
branch, S-A fan-out aggregate — lite) re-expressed as one-line q queries.
For each: (a) the answer is correct against independently seeded ground
truth, (b) the authored-query token count (len//4) is ≤ 1/4 of the
equivalent ``ctx py`` script's token count (scripts embedded verbatim
from evals/evalset_collapse.py as comparison constants), (c) the output
is bounded. Plus totality (9 stages rejected, unknown stage teaches,
kind mismatch teaches), registry late-binding, determinism (byte-identical
twice), row-cap declared omission, and --trace shape.
"""

from __future__ import annotations

import re
import subprocess

import pytest

# --------------------------------------------------------------- constants
# The eval-arm scripts, verbatim from evals/evalset_collapse.py — the token
# baseline the algebra must beat 4x (ALGEBRA.md M-H referee).
S_B_EVAL_SCRIPT = (
    "import pathlib, subprocess, sys\n"
    "files = sorted(str(p) for p in pathlib.Path('tests').glob('test_*.py')\n"
    "               if 'legacy_helper' in p.read_text())\n"
    "print('importers:', ' '.join(files))\n"
    "r = subprocess.run([sys.executable, '-m', 'pytest', '-q', *files],\n"
    "                   capture_output=True, text=True)\n"
    "tail = [l for l in r.stdout.splitlines() if l.strip()][-3:]\n"
    "print(*tail, sep='\\n')\n"
    "sys.exit(r.returncode)\n"
)
S_A_EVAL_SCRIPT = (
    "import json, pathlib\n"
    "tot = {}\n"
    "for p in sorted(pathlib.Path('runs').glob('*.jsonl')):\n"
    "    for line in p.read_text().splitlines():\n"
    "        r = json.loads(line)\n"
    "        n, ok = tot.get(r['module'], (0, 0))\n"
    "        tot[r['module']] = (n + 1, ok + (1 if r['ok'] else 0))\n"
    "for m, (n, ok) in sorted(tot.items()):\n"
    "    rate = ok / n\n"
    "    if rate < 0.8:\n"
    "        print(f'LOW {m} {rate:.4f} ({ok}/{n})')\n"
)

S_B_QUERY = "search legacy_helper --glob 'tests/*.py' | files"
S_A_QUERY = "search FAIL --glob 'logs/*.log' | group file | count"


def qtok(s: str) -> int:
    return len(s) // 4


# ---------------------------------------------------------------- fixture
@pytest.fixture()
def ws_store(tmp_path, monkeypatch, state_home):
    """Seeded eval-collapse S-B shape (pkg + 8 test files, importers
    {2, 5, 6} on the legacy path, test_05 the seeded failure) plus an
    S-A-lite log corpus with a known low performer."""
    monkeypatch.setenv("CTX_CODE_ENGINE", "ast")  # deterministic engine
    root = tmp_path / "proj"
    (root / "mod").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "logs").mkdir()
    (root / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    (root / "mod" / "__init__.py").write_text("", encoding="utf-8")
    (root / "mod" / "legacy_helper.py").write_text(
        "def fold(xs):\n    # legacy: drops the final element on odd lengths\n"
        "    return sum(xs[: len(xs) - len(xs) % 2])\n",
        encoding="utf-8",
    )
    (root / "mod" / "helper.py").write_text(
        "def fold(xs):\n    return sum(xs)\n", encoding="utf-8"
    )
    importers = {2, 5, 6}  # test files on the legacy path; test_05 fails
    for i in range(8):
        mod = "legacy_helper" if i in importers else "helper"
        expect = "6" if (mod == "helper" or i != 5) else "10"
        (root / "tests" / f"test_{i:02d}.py").write_text(
            f"from mod.{mod} import fold\n\n\n"
            f"def test_fold_{i}():\n    assert fold([1, 2, 3]) == {expect}\n",
            encoding="utf-8",
        )
    # S-A-lite: seeded per-file FAIL counts; gateway is the low performer.
    for name, fails in (("gateway", 12), ("quota", 7), ("auth", 2)):
        lines = [f"FAIL case-{i:03d}" for i in range(fails)]
        lines += [f"ok case-{i:03d}" for i in range(20)]
        (root / "logs" / f"{name}.log").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )
    subprocess.run(["git", "init", "-q", "."], cwd=root, check=True)

    from ctx.store import Store
    from ctx.workspace import resolve_workspace

    ws = resolve_workspace(str(root))
    return ws, Store(ws.workspace_id)


def run_q(ws_store, query: str, **kw):
    from ctx.query import run_query

    ws, store = ws_store
    return run_query(ws, store, query, **kw)


def assert_bounded(ws_store, text: str) -> None:
    ws, _ = ws_store
    budget = ws.config.budgets.result_tokens
    assert len(text.encode("utf-8")) // 4 <= budget + 32, "output not bounded"


# ------------------------------------------------------- mechanical referee
def test_referee_s_b_branch_shape(ws_store):
    """S-B: 'which test files sit on the legacy path' as ONE line of q."""
    out, code = run_q(ws_store, S_B_QUERY)
    assert code == 0
    # (a) correct: exactly the 3 seeded importers, including the failing one.
    for f in ("tests/test_02.py", "tests/test_05.py", "tests/test_06.py"):
        assert f in out
    for f in ("tests/test_00.py", "tests/test_01.py", "tests/test_03.py",
              "tests/test_04.py", "tests/test_07.py"):
        assert f not in out
    assert "rows (census): 3" in out
    # (b) token referee: q intent ≤ 1/4 of the eval script's tokens.
    assert qtok(S_B_QUERY) * 4 <= qtok(S_B_EVAL_SCRIPT), (
        f"q={qtok(S_B_QUERY)} tok vs eval={qtok(S_B_EVAL_SCRIPT)} tok"
    )
    # (c) bounded.
    assert_bounded(ws_store, out)


def test_referee_s_a_lite_fanout(ws_store):
    """S-A-lite: per-file failure census + low performer, one line of q."""
    out, code = run_q(ws_store, S_A_QUERY)
    assert code == 0
    lines = out.splitlines()
    # (a) correct: exact per-file counts, low performer ranked first.
    recs = [ln for ln in lines if ln.startswith("group=")]
    assert recs == [
        "group=logs/gateway.log · n=12",
        "group=logs/quota.log · n=7",
        "group=logs/auth.log · n=2",
    ]
    # (b) token referee.
    assert qtok(S_A_QUERY) * 4 <= qtok(S_A_EVAL_SCRIPT), (
        f"q={qtok(S_A_QUERY)} tok vs eval={qtok(S_A_EVAL_SCRIPT)} tok"
    )
    # (c) bounded.
    assert_bounded(ws_store, out)


def test_flagship_pipeline_group_top_get(ws_store):
    """The ALGEBRA.md flagship shape: refs X | group file | top N | get."""
    out, code = run_q(ws_store, "refs fold | group file | top 2 | get --context 1")
    assert code == 0
    assert "· text ·" in out.splitlines()[0]
    assert "[ctx get repo:" in out and "L1:" in out
    assert_bounded(ws_store, out)
    # The group census renders while grouping is live on the final stream.
    out2, code2 = run_q(ws_store, "refs fold | group file | top 2")
    assert code2 == 0 and "groups (census):" in out2


# ----------------------------------------------------------------- totality
def test_totality_nine_stages_rejected(ws_store):
    q = "search x" + " | top 1" * 8  # 9 stages
    out, code = run_q(ws_store, q)
    assert code == 2
    assert "max 8" in out and "totality" in out


def test_unknown_stage_teaches(ws_store):
    out, code = run_q(ws_store, "frobnicate x | count")
    assert code == 2
    assert "unknown stage 'frobnicate'" in out
    for known in ("refs", "search", "group", "count"):
        assert known in out  # the teaching line lists the registry


def test_kind_mismatch_teaches(ws_store):
    out, code = run_q(ws_store, "search x | outline")
    assert code == 2
    assert "needs files, got sites" in out
    assert "valid:" in out and "outline(files→text)" in out


def test_source_must_open_pipeline(ws_store):
    out, code = run_q(ws_store, "search x | refs y")
    assert code == 2
    assert "source stage" in out and "must open" in out


def test_combinator_cannot_open_pipeline(ws_store):
    out, code = run_q(ws_store, "count")
    assert code == 2
    assert "opens the pipeline" in out


def test_no_silent_nontermination_constructs():
    """No loop/recursion constructs exist to register against: the grammar
    is stages-only and the stage count is hard-capped — totality by
    construction (the property that makes the algebra MCP-tier-safe)."""
    from ctx import query

    assert query.MAX_STAGES == 8
    src = (query.__file__ and open(query.__file__, encoding="utf-8").read()) or ""
    assert "MAX_STAGES" in src


# ---------------------------------------------------------------- registry
def test_registry_late_binding(ws_store):
    """Engineer C's contract: register a stage post-import, use it in q."""
    from ctx.query import STAGES, Stream, register_stage

    def fake_fails(qc, stream, args):
        rows = [
            {"test": "tests/test_05.py::test_fold_5", "failure_class": "AssertionError"},
            {"test": "tests/test_02.py::test_fold_2", "failure_class": "AssertionError"},
        ]
        return Stream("records", sorted(rows, key=lambda r: r["test"]))

    register_stage(
        "fails_fake", fake_fails, input_kinds=(), output_kind="records",
        doc="test-only fact stage",
    )
    try:
        out, code = run_q(ws_store, "fails_fake | where test~test_05 | count")
        assert code == 0
        assert "n=1" in out
        out2, code2 = run_q(ws_store, "fails_fake | count")
        assert code2 == 0 and "n=2" in out2
    finally:
        del STAGES["fails_fake"]


def test_registry_contract_surface():
    """The frozen surface facts.py binds against: module-level STAGES dict
    and register_stage with the exact keyword contract."""
    import inspect

    from ctx.query import STAGES, register_stage

    assert isinstance(STAGES, dict) and "search" in STAGES
    params = inspect.signature(register_stage).parameters
    for name in ("name", "fn", "input_kinds", "output_kind", "doc"):
        assert name in params
    st = STAGES["search"]
    assert st.input_kinds == () and st.output_kind == "sites" and st.doc


def test_facts_import_fail_open(ws_store, monkeypatch):
    """q must work when ctx.facts is absent or broken (lazy, fail-open)."""
    import sys

    monkeypatch.setitem(sys.modules, "ctx.facts", None)  # import → ImportError
    out, code = run_q(ws_store, "search FAIL --glob 'logs/*.log' | count")
    assert code == 0 and "n=21" in out


# ------------------------------------------------------------- determinism
def test_determinism_byte_identical(ws_store):
    a, code_a = run_q(ws_store, S_A_QUERY, trace=True)
    b, code_b = run_q(ws_store, S_A_QUERY, trace=True)
    assert code_a == code_b == 0
    assert a == b  # identical bytes: sorted orderings, content-addressed blob
    c, _ = run_q(ws_store, S_B_QUERY)
    d, _ = run_q(ws_store, S_B_QUERY)
    assert c == d


# --------------------------------------------------------- bounds + census
def test_row_cap_declared_omission(ws_store, tmp_path):
    ws, store = ws_store
    big = "\n".join(f"needle row {i:04d}" for i in range(300)) + "\n"
    (ws.root / "big.txt").write_text(big, encoding="utf-8")
    out, code = run_q(ws_store, "search needle --glob 'big.txt'")
    assert code == 0
    # Stage cap 200 declared; render cap 100 declared with continuation.
    assert "rows (census): 200 · shown: 100" in out
    assert "capped: 100 rows omitted upstream (declared" in out
    assert "… +100 more rows" in out
    assert "ctx get blob:" in out  # continuation hint to the stored result set
    assert_bounded(ws_store, out)


def test_result_blob_addressable(ws_store):
    """v1-lite provenance: the final stream is a derived canonical-JSON blob
    named in the header and retrievable from the store."""
    import json

    ws, store = ws_store
    out, code = run_q(ws_store, S_B_QUERY)
    assert code == 0
    m = re.search(r"blob:([0-9a-f]{12})", out)
    assert m
    blob = store.get_blob(store.resolve_id(m.group(1), kinds=("blob",)))
    doc = json.loads(blob)
    assert doc["format"] == "ctx.q/v1" and doc["kind"] == "files"
    assert [r["file"] for r in doc["rows"]] == [
        "tests/test_02.py", "tests/test_05.py", "tests/test_06.py"
    ]


# ------------------------------------------------------------------- trace
def test_trace_shape(ws_store):
    out, code = run_q(ws_store, S_A_QUERY, trace=True)
    assert code == 0
    tlines = out.splitlines()
    i = tlines.index("trace:")
    rows = tlines[i + 1:]
    assert len(rows) == 3  # one line per stage
    pat = re.compile(r"^  \d+ .+ · in \d+ → out \d+")
    for ln in rows:
        assert pat.match(ln), ln
    assert rows[0].startswith("  1 search FAIL")
    # trace is opt-in: absent without the flag
    out2, _ = run_q(ws_store, S_A_QUERY)
    assert "trace:" not in out2


# ------------------------------------------------------------- combinators
def test_where_ops(ws_store):
    out, code = run_q(ws_store, "search fold | where file~mod/ | files")
    assert code == 0
    assert "mod/helper.py" in out and "mod/legacy_helper.py" in out
    assert "tests/" not in out
    out2, code2 = run_q(ws_store, "search fold | where file=mod/helper.py | count")
    assert code2 == 0 and "n=1" in out2
    out3, code3 = run_q(ws_store, "where nonsense", )
    assert code3 == 2


def test_callgraph_stages(ws_store):
    """callers/callees/impact ride the existing ast call graph as sites."""
    ws, _ = ws_store
    (ws.root / "mod" / "chain.py").write_text(
        "from mod.helper import fold\n\n\ndef use():\n    return fold([1])\n"
        "\n\ndef outer():\n    return use()\n",
        encoding="utf-8",
    )
    out, code = run_q(ws_store, "callers fold | files")
    assert code == 0 and "mod/chain.py" in out
    out2, code2 = run_q(ws_store, "impact fold --depth 6")
    assert code2 == 0 and "use" in out2 and "outer" in out2 and "depth 2" in out2
    out3, code3 = run_q(ws_store, "callees outer")
    assert code3 == 0 and "use" in out3


# --------------------------------------------------------------------- CLI
def test_cli_q_verb(ws_store, capsys):
    from ctx.cli import main

    ws, _ = ws_store
    rc = main(["--workspace", str(ws.root), "q", S_B_QUERY, "--trace"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.splitlines()[0].startswith("[ctx q · 2 stages · files · blob:")
    assert "tests/test_05.py" in out and "trace:" in out


def test_cli_q_error_is_exit_2(ws_store, capsys):
    from ctx.cli import main

    ws, _ = ws_store
    rc = main(["--workspace", str(ws.root), "q", "search x | outline"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "needs files, got sites" in err


# ---------------------- self-healing empty results (debt fac2339eff)
# Live A/B receipt (evals/spec3-haiku-2026-07-18.md): an empty result that
# teaches nothing converts to re-execution — 3 identical dry joins + 1
# malformed variant, then abandonment. Empty results diagnose themselves;
# identical dry re-issues banner the missing precondition, never block.
def test_empty_diagnosis_at_source_stage(ws_store):
    """0 rows is never a bare census: the diagnosis names the stage that
    went empty and gives a why-shaped hint (generic, absent a note)."""
    out, code = run_q(ws_store, "search zzz_absent_needle")
    assert code == 0
    assert "rows (census): 0" in out
    assert "0 rows after stage 1 (search):" in out
    assert "may not exist yet" in out  # generic source-stage hint
    assert_bounded(ws_store, out)


def test_empty_diagnosis_at_filter_stage(ws_store):
    """The diagnosis walks the per-stage trace unconditionally (no --trace
    flag here) and points at the stage where the stream went empty."""
    out, code = run_q(ws_store, "search fold | where file~zzz_nowhere")
    assert code == 0
    assert "0 rows after stage 2 (where):" in out
    assert "upstream rows were filtered out here" in out
    assert "trace:" not in out  # diagnosis rides without the printed trace


def test_empty_diagnosis_with_stream_note(ws_store):
    """A Stream-carried note (the facts note channel) wins over the static
    empty_hint."""
    from ctx.query import STAGES, Stream, register_stage

    def dry(qc, stream, args):
        return Stream(
            "records", [], note="no captured run — run: ctx run -- pytest -q"
        )

    register_stage(
        "dry_fake", dry, input_kinds=(), output_kind="records",
        doc="test-only", empty_hint="static hint that must lose to the note",
    )
    try:
        out, code = run_q(ws_store, "dry_fake")
        assert code == 0
        assert (
            "0 rows after stage 1 (dry_fake): "
            "no captured run — run: ctx run -- pytest -q" in out
        )
        assert "static hint" not in out
    finally:
        del STAGES["dry_fake"]


def test_empty_diagnosis_with_static_empty_hint(ws_store):
    """Without a runtime note, the registered empty_hint teaches."""
    from ctx.query import STAGES, Stream, register_stage

    def dry(qc, stream, args):
        return Stream("records", [])

    register_stage(
        "dry_hint_fake", dry, input_kinds=(), output_kind="records",
        doc="test-only", empty_hint="derive facts first: ctx facts derive",
    )
    try:
        out, code = run_q(ws_store, "dry_hint_fake")
        assert code == 0
        assert (
            "0 rows after stage 1 (dry_hint_fake): "
            "derive facts first: ctx facts derive" in out
        )
    finally:
        del STAGES["dry_hint_fake"]


def test_nonempty_rendering_carries_no_diagnosis(ws_store):
    """Non-empty pipelines gain no diagnosis/banner lines (byte identity
    with the pre-fix rendering is pinned by the untouched census,
    determinism, and trace tests above)."""
    out, code = run_q(ws_store, S_B_QUERY)
    assert code == 0
    assert "0 rows after" not in out and "moments ago" not in out


# --------------------------------------------------------- did-you-mean
def test_did_you_mean_exact_live_failure(ws_store):
    """The exact live failure string from the A/B transcript: the model
    inverted stage and argument ('last | fails')."""
    out, code = run_q(ws_store, "last | fails")
    assert code == 2
    assert "unknown stage 'last'" in out
    assert "did you mean: fails last | in-changed" in out
    assert "known:" in out  # the registry list still teaches


def test_kind_mismatch_names_working_example(ws_store):
    out, code = run_q(ws_store, "search x | outline")
    assert code == 2
    assert "needs files, got sites" in out
    assert "example: search TODO --glob 'src/*.py' | files | outline" in out


# ------------------------------------------------------ dry-run guard rail
def test_dry_rerun_banner_ledger_and_still_executes(ws_store):
    import json

    ws, _ = ws_store
    q = "search zzz_dry_probe"
    out1, code1 = run_q(ws_store, q)
    assert code1 == 0 and "moments ago" not in out1  # first issue: no banner
    out2, code2 = run_q(ws_store, q)
    assert code2 == 0  # never blocks
    assert (
        "this exact query returned 0 rows moments ago — "
        "the missing precondition is:" in out2
    )
    assert "re-running unchanged will not differ" in out2
    # STILL executes: header, census, and diagnosis all present.
    assert out2.splitlines()[0].startswith("[ctx q ·")
    assert "rows (census): 0" in out2
    assert "0 rows after stage 1 (search):" in out2
    # One ledger line per dry re-issue, for the reflex plane.
    lines = (
        (ws.root / ".ctx-session-reads" / "q-dry.jsonl")
        .read_text(encoding="utf-8").splitlines()
    )
    recs = [json.loads(ln) for ln in lines]
    assert [r["op"] for r in recs] == ["q_dry_rerun"]
    assert recs[0]["pipeline"] == q
    # A different pipeline is not banner-ed.
    out3, _ = run_q(ws_store, "search zzz_other_probe")
    assert "moments ago" not in out3


def test_dry_guard_fail_open(ws_store):
    """Garbage guard state must never break a query (house ledger rule)."""
    ws, _ = ws_store
    state = ws.root / ".ctx-session-reads" / "q-dry.json"
    state.parent.mkdir(exist_ok=True)
    state.write_text("{not json", encoding="utf-8")
    out, code = run_q(ws_store, "search zzz_failopen_probe")
    assert code == 0
    assert "0 rows after stage 1 (search):" in out


def test_dry_memory_clears_when_rows_return(ws_store):
    """A pipeline that produces rows again drops out of the dry set — the
    banner is deterministic given the session's query sequence."""
    from ctx.query import STAGES, Stream, register_stage

    results = [[], [{"n": 1}], []]

    def flaky(qc, stream, args):
        return Stream("records", list(results.pop(0)))

    register_stage(
        "flaky_fake", flaky, input_kinds=(), output_kind="records", doc="test-only"
    )
    try:
        out1, _ = run_q(ws_store, "flaky_fake")  # empty → remembered
        assert "moments ago" not in out1
        out2, _ = run_q(ws_store, "flaky_fake")  # rows → memory cleared
        assert "moments ago" not in out2
        out3, _ = run_q(ws_store, "flaky_fake")  # empty again, but first-dry
        assert "moments ago" not in out3
    finally:
        del STAGES["flaky_fake"]


def test_search_never_reads_the_session_ledger(ws_store):
    """The ledger dir is bookkeeping, never evidence (hook.py rule) — and
    the guard state echoes pipeline texts, so a ledger-scanning search
    would match its own guard rail."""
    ws, _ = ws_store
    d = ws.root / ".ctx-session-reads"
    d.mkdir(exist_ok=True)
    (d / "q-dry.json").write_text(
        '{"dry": ["search zzz_ledger_probe"]}', encoding="utf-8"
    )
    out, code = run_q(ws_store, "search zzz_ledger_probe")
    assert code == 0
    assert "rows (census): 0" in out  # the ledger file itself never matches


# --------------------------------------- emission: teaching is evidence
def test_empty_teaching_survives_engagement_filter(ws_store):
    """Teaching-on-emptiness is evidence, not a suggestion: the engagement
    filter (cap 0 strips 'next:' affordance blocks) must pass it whole."""
    from ctx.engagement import filter_digest

    q = "search zzz_filter_probe"
    run_q(ws_store, q)
    out, code = run_q(ws_store, q)  # diagnosis + banner present
    assert code == 0 and "moments ago" in out
    assert filter_digest(out, 0) == out


def test_cli_q_empty_teaching_reaches_stdout(ws_store, capsys):
    """End-to-end through _cmd_q's emission boundary (plan + filter +
    bounded): the diagnosis and the dry-rerun banner reach the model."""
    from ctx.cli import main

    ws, _ = ws_store
    q = "search zzz_cli_probe"
    assert main(["--workspace", str(ws.root), "q", q]) == 0
    capsys.readouterr()
    assert main(["--workspace", str(ws.root), "q", q]) == 0
    out = capsys.readouterr().out
    assert "0 rows after stage 1 (search):" in out
    assert "re-running unchanged will not differ" in out


# ------------------------------------------------- additive-field contracts
def test_stream_note_additive_backcompat():
    """Stream gains ``note`` additively: existing positional/keyword call
    shapes are untouched and default to None."""
    from ctx.query import Stream

    s = Stream("sites", [{"a": 1}])
    assert s.note is None and s.omitted == 0 and s.groups is None
    s2 = Stream("sites", [], 3, [("k", 3)])  # pre-existing positional shape
    assert s2.note is None and s2.omitted == 3 and s2.groups == [("k", 3)]
    s3 = Stream("records", [], note="hint")
    assert s3.note == "hint"


def test_register_stage_empty_hint_additive():
    """The frozen registration contract is extended additively: empty_hint
    is optional, defaults None, and existing stages carry no hint."""
    import inspect

    from ctx.query import STAGES, register_stage

    params = inspect.signature(register_stage).parameters
    assert "empty_hint" in params and params["empty_hint"].default is None
    for name in ("name", "fn", "input_kinds", "output_kind", "doc"):
        assert name in params  # original surface intact
    assert STAGES["search"].empty_hint is None
