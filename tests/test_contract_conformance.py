"""Contract-conformance suite (docs/EDC.md layering law, §18-§19).

The missing test class that let pytest/v1 ship structurally starved:
extraction completeness is asserted against RAW output (census-vs-raw
cross-validation, exact identity lists), coverage receipts are asserted
against the committed pytest contract (census named 8/8 with
required_fraction 1.0 at default budgets), and every inline identity's
address resolves to that item's own evidence (address integrity, §19).
These are layer-1/2 tests — distinct from rendering goldens by design.
"""

import os
import re
import subprocess
import sys
import textwrap

import pytest
from conftest import make_store, make_ws

SEED = textwrap.dedent('''\
    class TestBox:
        def test_edge(self):
            box = {"lo": 1, "hi": 0}
            assert box["lo"] <= box["hi"]

    def test_alpha():
        assert 2 + 2 == 5

    def test_beta():
        raise ValueError("beta exploded")

    def test_gamma():
        assert "needle" in "haystack"

    def test_delta():
        assert [1, 2] == [1, 3]

    def test_epsilon():
        x = None
        assert x is not None

    def test_zeta():
        assert 10 / 3 > 4

    def test_eta():
        d = {}
        assert d["missing"] == 1

    def test_ok():
        assert True
''')

FAIL_IDS = [
    "test_seed.py::TestBox::test_edge",
    "test_seed.py::test_alpha",
    "test_seed.py::test_beta",
    "test_seed.py::test_gamma",
    "test_seed.py::test_delta",
    "test_seed.py::test_epsilon",
    "test_seed.py::test_zeta",
    "test_seed.py::test_eta",
]

BARE_NAMES = [nid.split("::")[-1] for nid in FAIL_IDS]

# Independent raw-output parser for cross-validation: deliberately NOT the
# extractor's regexes — the census must agree with a second reading.
RAW_FAILED_RE = re.compile(r"^(?:FAILED|ERROR) (\S+)", re.M)


@pytest.fixture(scope="module")
def real_outputs(tmp_path_factory):
    seed = tmp_path_factory.mktemp("conformance-seed")
    (seed / "test_seed.py").write_text(SEED, encoding="utf-8")
    env = {**os.environ}
    env.pop("PYTEST_ADDOPTS", None)

    def run(*args):
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *args],
            cwd=seed, env=env, capture_output=True, text=True, timeout=120,
        )
        return proc.stdout

    return {"fail_q": run("test_seed.py", "-q"), "fail_v": run("test_seed.py", "-v")}


def _ctx_for(tmp_path, text, *, result_tokens=1200, argv=("pytest", "-q")):
    from ctx.digest.base import DigestContext, StreamView
    from ctx.workspace import resolve_workspace

    (tmp_path / "ctx.toml").write_text(
        f"version = 1\n[budgets]\nresult_tokens = {result_tokens}\n", encoding="utf-8"
    )
    ws = resolve_workspace(str(tmp_path))
    out = StreamView("stdout", len(text.encode()), len(text.splitlines()), "text/plain", text, True)
    err = StreamView("stderr", 0, 0, "text/plain", "", True)
    manifest = {
        "argv": list(argv), "cwd": ".", "shell": False,
        "result": {"exitCode": 1, "signal": None, "timedOut": False},
        "streams": {"stdout": {"blob": "sha256:x"}, "stderr": {"blob": "sha256:y"}},
    }
    return DigestContext(ws=ws, manifest=manifest, stdout=out, stderr=err)


def _graph(tmp_path, text, **kw):
    from ctx.digest.pytestprof import extract_pytest

    return extract_pytest(_ctx_for(tmp_path, text, **kw))


# ------------------------------------------------------ the contract itself
def test_pytest_contract_requires_the_census():
    from ctx.contracts import contract_for_family

    contract = contract_for_family("pytest")
    assert contract.profile == "pytest/v2"
    for outcome in ("fail", "error"):
        req = contract.for_outcome(outcome)
        assert set(req.required) == {
            "aggregate_counts", "complete_identity_census", "location", "failure_class",
        }
        assert set(req.preferred) == {"one_line_summary", "root_detail"}
    assert contract.loss_severity("complete_identity_census") == "catastrophic"


# --------------------------------------------- exact identity lists (raw)
@pytest.mark.parametrize("shape", ["fail_q", "fail_v"])
def test_exact_identity_list_against_real_fixture(tmp_path, real_outputs, shape):
    g = _graph(tmp_path, real_outputs[shape])
    assert [i.id for i in g.items] == FAIL_IDS
    assert g.coverage["complete"] is True


@pytest.mark.parametrize("shape", ["fail_q", "fail_v"])
def test_census_vs_raw_cross_validation(tmp_path, real_outputs, shape):
    """The typed census must agree with an independent reading of the raw
    bytes — extraction can never name fewer (or other) identities than
    the output itself declares."""
    raw_ids = set(RAW_FAILED_RE.findall(real_outputs[shape]))
    g = _graph(tmp_path, real_outputs[shape])
    assert {i.id for i in g.items} == raw_ids
    assert g.coverage["parsed"] == len(raw_ids)
    counted = g.aggregate.get("failed", 0) + g.aggregate.get("error", 0)
    assert counted == len(raw_ids)  # summary counts corroborate the census


def test_truncated_verbose_census_from_progress_rows(tmp_path, real_outputs):
    """-v output cut before FAILURES: identities still complete (progress
    rows), and the missing fact classes are DECLARED, not faked — the
    receipt's required_fraction drops, completeness is unattested."""
    from ctx.contracts import contract_for_family, validate_selection

    lines = real_outputs["fail_v"].splitlines()
    cut = next(i for i, ln in enumerate(lines) if "FAILURES" in ln)
    g = _graph(tmp_path, "\n".join(lines[:cut]) + "\n")
    assert [i.id for i in g.items] == FAIL_IDS  # identities survive truncation
    assert g.coverage["complete"] is False
    receipt = validate_selection(
        [i.id for i in g.items],
        {"aggregate_counts", "complete_identity_census", "location", "failure_class"},
        contract_for_family("pytest"),
        g,
    )
    assert receipt.attested_complete is False
    assert receipt.required_fraction < 1.0  # failure_class/counts honestly absent


# ------------------------------------------------------- address integrity
def test_address_integrity_every_span_names_its_test(state_home, workspace_dir, real_outputs):
    """§19 gate: each census row's span resolves to text containing that
    test's own name — addresses point at their evidence, exactly."""
    from ctx.digest import render_run_digest
    from ctx.execution import run_capture
    from ctx.retrieval import Selector, get

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    (ws.root / "fixture.txt").write_text(real_outputs["fail_q"], encoding="utf-8")
    cap = run_capture(
        ws,
        [sys.executable, "-c", "import sys; sys.stdout.write(open('fixture.txt').read())"],
        store=store,
    )
    digest, m = render_run_digest(store, ws, cap.manifest)
    short = str(m["id"]).removeprefix("sha256:")[:12]
    rows = re.findall(
        r"^  \d+\. (\S+)  \S+ · \S+ · .*? · stdout:L\d+-L\d+ · span ([0-9a-f]{10})$",
        digest,
        re.M,
    )
    assert [r[0] for r in rows] == FAIL_IDS  # every row carries a span
    for nodeid, sid in rows:
        evidence = get(store, ws, f"run:{short}#stdout", Selector(span=sid))
        assert nodeid.split("::")[-1] in evidence, (nodeid, sid)


# ------------------------------------------------------- contract coverage
def test_census_survives_budget_600_with_full_required_coverage(tmp_path, real_outputs):
    """§18: census named 8/8 with required_fraction 1.0 at a default-ish
    budget — contract violations fail CI, not benchmarks."""
    from ctx.contracts import contract_for_family
    from ctx.digest.evidence_render import DefaultPlan, render_fail_evidence

    g = _graph(tmp_path, real_outputs["fail_q"])
    out = render_fail_evidence(
        g, contract_for_family("pytest"), DefaultPlan(mode="fail_census", token_budget=600)
    )
    assert "· flood ·" not in out.text
    ids = re.findall(r"^  \d+\. (\S+)  ", out.text, re.M)
    assert ids == FAIL_IDS
    assert out.coverage.items_named_inline == 8
    assert out.coverage.required_fraction == 1.0
    assert out.coverage.attested_complete is True


def test_default_budget_receipt_through_digest_pipeline(state_home, workspace_dir, real_outputs):
    """End-to-end: the store-backed digest exposes the selection-seam
    receipt (never re-parsed from text) with full required coverage."""
    from ctx.digest.base import DigestContext
    from ctx.digest.evidence_render import default_fail_plan, render_fail_evidence
    from ctx.contracts import contract_for_family
    from ctx.execution import run_capture

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    (ws.root / "fixture.txt").write_text(real_outputs["fail_q"], encoding="utf-8")
    cap = run_capture(
        ws,
        [sys.executable, "-c", "import sys; sys.stdout.write(open('fixture.txt').read())"],
        store=store,
    )
    from ctx.digest.pytestprof import extract_pytest

    ctx = DigestContext.load(store, ws, cap.manifest, focus=None)
    g = extract_pytest(ctx)
    out = render_fail_evidence(
        g, contract_for_family("pytest"), default_fail_plan(ws.config.budgets)
    )
    assert out.coverage.required_fraction == 1.0
    assert out.coverage.items_total == out.coverage.items_named_inline == 8
    assert out.coverage.items_addressable == 8  # every identity has a detail_ref
    # Spans were minted through the store: real span: selectors in the graph.
    assert all(
        i.detail_ref.selector.startswith("span:") for i in g.items
    )


def test_graph_is_content_addressed_and_stable(tmp_path, real_outputs):
    from ctx.evidence import graph_id, to_canonical_bytes

    g1 = _graph(tmp_path, real_outputs["fail_q"])
    g2 = _graph(tmp_path, real_outputs["fail_q"])
    assert graph_id(g1) == graph_id(g2)
    blob = to_canonical_bytes(g1).decode("utf-8")
    assert '"duration"' not in blob  # volatile never enters identity
    assert '"schema":"ctx.evidence-graph/v1"' in blob
