"""Evidence-core acceptance (docs/EDC.md phases 1+3): the pytest/v2
extract/render split.

The layering law made real: :func:`ctx.digest.pytestprof.extract_pytest`
builds every fact class (identity, location, failure class, one-line
summary, span address, coverage attestation, quarantined volatiles) and
the plan-obeying pure renderer (ctx.digest.evidence_render) selects among
them. Rule 14 (same graph + contract + plan → identical bytes), the
committed degradation order (teaching → root detail → one-liner
compression; identities never drop outside FLOOD), and the FLOOD census
blob are property-tested here against REAL pytest output.
"""

import json
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

PASS_SEED = textwrap.dedent('''\
    def test_one():
        assert 1 + 1 == 2

    def test_two():
        assert "a" in "abc"

    def test_three():
        assert sorted([2, 1]) == [1, 2]
''')

# A second failing file so DENSE has a multi-file population to group.
SEED_B = textwrap.dedent('''\
    def test_omega():
        raise KeyError("omega")
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

ROW_RE = re.compile(
    r"^\s+\d+\. (\S+)  (\S+) · (\S+)(?: · .*?)?"
    r" · stdout:L(\d+)(?:-L(\d+))?(?: · span ([0-9a-f]{6,64}))?$",
    re.M,
)


@pytest.fixture(scope="module")
def real_outputs(tmp_path_factory):
    seed = tmp_path_factory.mktemp("evidence-seed")
    (seed / "test_seed.py").write_text(SEED, encoding="utf-8")
    (seed / "test_pass.py").write_text(PASS_SEED, encoding="utf-8")
    (seed / "test_more.py").write_text(SEED_B, encoding="utf-8")
    env = {**os.environ}
    env.pop("PYTEST_ADDOPTS", None)

    def run(*args):
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *args],
            cwd=seed, env=env, capture_output=True, text=True, timeout=120,
        )
        return proc.stdout

    return {
        "fail_q": run("test_seed.py", "-q"),
        "fail_v": run("test_seed.py", "-v"),
        "fail_two_files": run("test_seed.py", "test_more.py", "-q"),
        "pass_default": run("test_pass.py"),
    }


def _ctx_for(tmp_path, text, *, result_tokens=1200, argv=("pytest", "-q"), dense=False, plan=None):
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
    ctx = DigestContext(ws=ws, manifest=manifest, stdout=out, stderr=err)
    ctx.dense = dense
    ctx.plan = plan
    return ctx


def _graph(tmp_path, text, **kw):
    from ctx.digest.pytestprof import extract_pytest

    return extract_pytest(_ctx_for(tmp_path, text, **kw))


def _digest_for(ws, store, text, *, plan=None):
    from ctx.digest import render_run_digest
    from ctx.execution import run_capture

    (ws.root / "fixture.txt").write_text(text, encoding="utf-8")
    cap = run_capture(
        ws,
        [sys.executable, "-c", "import sys; sys.stdout.write(open('fixture.txt').read())"],
        store=store,
    )
    return render_run_digest(store, ws, cap.manifest, plan=plan)


def _plan(**kw):
    from ctx.digest.evidence_render import DefaultPlan

    kw.setdefault("mode", "fail_census")
    return DefaultPlan(**kw)


def _contract():
    from ctx.contracts import contract_for_family

    return contract_for_family("pytest")


# ================================================== layer 1: extraction
def test_extract_identities_locations_classes_summaries(tmp_path, real_outputs):
    g = _graph(tmp_path, real_outputs["fail_q"])
    assert g.family == "pytest" and g.profile_version == "pytest/v2"
    assert g.outcome == "fail"
    assert [i.id for i in g.items] == FAIL_IDS
    # Level 3: file:line location and exception-class per failing test.
    for item in g.items:
        assert re.fullmatch(r"test_seed\.py:\d+", item.location), item
        assert item.failure_class, item
        # Level 4: bounded one-line summary, default-extracted.
        assert item.summary and len(item.summary) <= 120
    by_id = {i.id: i for i in g.items}
    assert by_id["test_seed.py::test_beta"].failure_class == "ValueError"
    assert by_id["test_seed.py::test_eta"].failure_class == "KeyError"
    assert by_id["test_seed.py::test_alpha"].failure_class == "AssertionError"
    assert "beta exploded" in by_id["test_seed.py::test_beta"].summary
    # causal_rank v1 = occurrence order.
    assert [i.causal_rank for i in g.items] == list(range(8))


def test_extract_detail_refs_are_real_addresses(tmp_path, real_outputs):
    g = _graph(tmp_path, real_outputs["fail_q"])
    for item in g.items:
        ref = item.detail_ref
        assert ref is not None and ref.artifact == "stdout"
        # Store-free extraction falls back to the line-selector grammar;
        # both forms are validated at EvidenceRef construction.
        assert re.fullmatch(r"(span:[0-9a-f]{6,64}|lines:\d+:\d+)", ref.selector)
        a = item.attributes["stdout_a"]
        b = item.attributes["stdout_b"]
        assert 1 <= a <= b


def test_extract_verbose_shape_same_identities(tmp_path, real_outputs):
    q = _graph(tmp_path, real_outputs["fail_q"])
    v = _graph(tmp_path, real_outputs["fail_v"])
    assert [i.id for i in v.items] == [i.id for i in q.items] == FAIL_IDS


def test_extract_coverage_attestation_honest(tmp_path, real_outputs):
    full = _graph(tmp_path, real_outputs["fail_q"])
    assert full.coverage == {"parsed": 8, "total_estimate": 8, "complete": True}
    # Pipe truncation: fewer blocks than the parse can prove → never
    # attested complete (EDC §5 amendment 5).
    head = "\n".join(real_outputs["fail_q"].splitlines()[:20]) + "\n"
    part = _graph(tmp_path, head, argv=("bash",))
    assert part.coverage["complete"] is False
    assert 0 < part.coverage["parsed"] < 8


def test_extract_volatile_duration_quarantined(tmp_path, real_outputs):
    from ctx.digest.pytestprof import PytestProfile
    from ctx.evidence import graph_id

    text_a = real_outputs["fail_q"]
    text_b = re.sub(r"\bin [\d.]+s\b", "in 9.99s", text_a)
    assert text_a != text_b  # the fixture really carries a duration
    ga = _graph(tmp_path, text_a)
    gb = _graph(tmp_path, text_b)
    assert ga.volatile and gb.volatile["duration"] == "9.99s"
    # Volatile excluded from content identity AND from rendering.
    assert graph_id(ga) == graph_id(gb)
    assert PytestProfile().render(_ctx_for(tmp_path, text_a)) == PytestProfile().render(
        _ctx_for(tmp_path, text_b)
    )
    assert "9.99s" not in PytestProfile().render(_ctx_for(tmp_path, text_b))


def test_extract_pass_graph(tmp_path, real_outputs):
    g = _graph(tmp_path, real_outputs["pass_default"])
    assert g.outcome == "pass" and g.items == ()
    assert g.aggregate.get("passed") == 3
    assert g.coverage["complete"] is True


# ============================================ layer 3/4: plan-driven render
def test_meta_profile_version_split(state_home, workspace_dir, real_outputs):
    """Failure renders are pytest/v2 in digest meta; the pass path stays
    pytest/v1 with byte-identical rendering (the split, declared)."""
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    fail_digest, fail_m = _digest_for(ws, store, real_outputs["fail_q"])
    assert fail_m["digest"]["profile"] == "pytest/v2"
    assert "profile=pytest/v2]" in fail_digest.splitlines()[0]
    assert "[pytest/v2 · fail · coverage=8/8]" in fail_digest
    pass_digest, pass_m = _digest_for(ws, store, real_outputs["pass_default"])
    assert pass_m["digest"]["profile"] == "pytest/v1"
    assert "profile=pytest/v1]" in pass_digest.splitlines()[0]
    assert "census" not in pass_digest  # no census scaffolding on green


def test_render_run_digest_accepts_plan_kwarg():
    """The resolver engineer's cli seam: cli passes plan= once the digest
    layer accepts it (duck-typed; inspect-gated in cli.py)."""
    import inspect

    from ctx.digest import render_run_digest

    assert "plan" in inspect.signature(render_run_digest).parameters


def test_resolver_plan_flows_through_render(state_home, workspace_dir, real_outputs):
    """A real ctx.resolver.DeliveryPlan drives the rendering (duck-typed —
    no import edge from the digest layer into the resolver)."""
    from ctx.resolver import DeliveryPlan

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    plan = DeliveryPlan(
        mode="fail_census", census="complete", item_summary="one_line",
        inline_detail_count=1, include_addresses=True, include_teaching=True,
        token_budget=960, evidence_floor=0, hard_ceiling=4000,
    )
    digest, m = _digest_for(ws, store, real_outputs["fail_q"], plan=plan)
    assert m["digest"]["profile"] == "pytest/v2"
    rows = ROW_RE.findall(digest)
    assert [r[0] for r in rows] == FAIL_IDS


def test_rule14_renderer_is_pure(tmp_path, real_outputs):
    """Rule 14 verbatim: same evidence + contract + plan → identical bytes."""
    from ctx.digest.evidence_render import render_fail_evidence

    g = _graph(tmp_path, real_outputs["fail_q"])
    contract = _contract()
    for mode in ("fail_census", "dense", "flood"):
        plan = _plan(mode=mode, token_budget=700)
        a = render_fail_evidence(g, contract, plan)
        b = render_fail_evidence(g, contract, plan)
        assert a.text == b.text
        assert a.coverage == b.coverage


# ------------------------------------------------- degradation property
def _flags(text):
    return {
        "flood": "· flood ·" in text,
        "teaching": "\nnext:" in "\n" + text,
        "body": "    | " in text,
        "oneliners": "beta exploded" in text,
        "ids": [m[0] for m in ROW_RE.findall(text)],
    }


_BUDGETS = [4000, 1200, 700, 500, 380, 300, 240, 180, 120, 60, 20]


@pytest.mark.parametrize("budget", _BUDGETS)
def test_degradation_order_property(tmp_path, real_outputs, budget):
    """The committed degradation order (EDC §19): teaching drops before
    root detail, root detail before one-liner compression; identities
    never drop outside FLOOD, and FLOOD declares its omissions + blob."""
    from ctx.digest.evidence_render import render_fail_evidence

    g = _graph(tmp_path, real_outputs["fail_q"])
    text = render_fail_evidence(g, _contract(), _plan(token_budget=budget)).text
    f = _flags(text)
    if f["teaching"]:
        assert f["body"] and f["oneliners"]  # nothing below teaching dropped yet
    if f["body"]:
        assert f["oneliners"]  # root detail drops before one-liner compression
    if not f["flood"]:
        assert f["ids"] == FAIL_IDS  # complete identity census, in order
    else:
        assert "full census blob:" in text  # the blob always rides in FLOOD
        if len(f["ids"]) < 8:
            m = re.search(r"… \+(\d+) more identities", text)
            assert m and len(f["ids"]) + int(m.group(1)) == 8  # declared, exact


def test_degradation_is_monotone_across_budgets(tmp_path, real_outputs):
    from ctx.digest.evidence_render import render_fail_evidence

    g = _graph(tmp_path, real_outputs["fail_q"])
    contract = _contract()

    def rank(text):
        f = _flags(text)
        if f["flood"]:
            return 0
        return 1 + (1 if f["oneliners"] else 0) + (1 if f["body"] else 0) + (
            1 if f["teaching"] else 0
        )

    ranks = [
        rank(render_fail_evidence(g, contract, _plan(token_budget=b)).text)
        for b in _BUDGETS
    ]
    assert ranks == sorted(ranks, reverse=True)
    assert ranks[0] == 4 and ranks[-1] == 0  # both regimes exercised


def test_escalation_to_flood_never_truncates_identities(tmp_path, real_outputs):
    """A fail_census plan whose budget cannot fit the identity census
    escalates to FLOOD: partial inline coverage DECLARED, the complete
    census behind a content-addressed blob — never a silent cut."""
    from ctx.digest.evidence_render import render_fail_evidence

    g = _graph(tmp_path, real_outputs["fail_q"])
    out = render_fail_evidence(g, _contract(), _plan(token_budget=60))
    assert "· flood ·" in out.text
    assert "failure classes:" in out.text and "files:" in out.text
    shown = ROW_RE.findall(out.text)
    assert 1 <= len(shown) < 8
    assert re.search(r"… \+\d+ more identities · full census blob:[0-9a-f]{12}", out.text)
    # Receipt (selection seam, not re-parsed text): partial inline census.
    assert out.coverage.items_named_inline == len(shown)
    assert out.coverage.omitted_items == 8 - len(shown)
    assert out.coverage.required_fraction < 1.0  # census not fully inline: declared


def test_flood_blob_is_resolvable(state_home, workspace_dir, real_outputs):
    """FLOOD mints the complete census as a canonical-JSON blob in the
    store; the digest's blob:<id> reference resolves to those bytes."""
    from ctx.digest.base import DigestContext
    from ctx.digest.evidence_render import flood_census_payload
    from ctx.digest.pytestprof import extract_pytest
    import hashlib

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    digest, m = _digest_for(ws, store, real_outputs["fail_q"], plan=_plan(mode="flood", census="bounded", token_budget=200))
    ref = re.search(r"full census blob:([0-9a-f]{12})", digest)
    assert ref, digest
    # Reconstruct the graph from the stored bytes: the content address of
    # the derived census must match the digest's reference exactly.
    ctx = DigestContext.load(store, ws, m, focus=None)
    payload = flood_census_payload(extract_pytest(ctx))
    full = hashlib.sha256(payload).hexdigest()
    assert full[:12] == ref.group(1)
    assert store.get_blob(full) == payload
    doc = json.loads(payload)
    assert doc["schema"] == "ctx.pytest-census/v1"
    assert [i["id"] for i in doc["items"]] == FAIL_IDS  # complete, in order


def test_dense_group_labels_are_extracted_keys(tmp_path, real_outputs):
    """EDC §12 correction 2: DENSE group labels ARE extracted keys (file /
    failure class), never invented prose."""
    from ctx.digest.pytestprof import PytestProfile

    # Single file → grouped by failure class; labels are the class names.
    dense = PytestProfile().render(_ctx_for(tmp_path, real_outputs["fail_q"], dense=True))
    assert "failing tests (census · by class):" in dense
    labels = re.findall(r"^  (\S+) \(\d+\):$", dense, re.M)
    assert set(labels) == {"AssertionError", "ValueError", "KeyError"}
    # Expanded evidence lines ride under the rows (dense = level-6 descent).
    assert len(re.findall(r"^\s+E\b", dense, re.M)) >= 8
    # Two files → grouped by file; labels are the file keys.
    dense2 = PytestProfile().render(
        _ctx_for(tmp_path, real_outputs["fail_two_files"], dense=True)
    )
    assert "failing tests (census · by file):" in dense2
    labels2 = re.findall(r"^  (\S+) \(\d+\):$", dense2, re.M)
    assert set(labels2) == {"test_seed.py", "test_more.py"}


def test_receipt_from_selection_seam(tmp_path, real_outputs):
    """validate_selection (contracts.py) is the coverage authority: the
    renderer's receipt is computed over typed facts at the selection seam."""
    from ctx.digest.evidence_render import render_fail_evidence

    g = _graph(tmp_path, real_outputs["fail_q"])
    out = render_fail_evidence(g, _contract(), _plan(token_budget=2400))
    r = out.coverage
    assert r.items_total == 8 and r.items_named_inline == 8
    assert r.required_fields_total == 4  # counts, census, location, class
    assert r.required_fraction == 1.0
    assert r.attested_complete is True
    assert r.omitted_items == 0
    assert out.plan.mode == "fail_census"
