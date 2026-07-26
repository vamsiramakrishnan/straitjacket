"""Acceptance: the routing catalog is sourced, coherent, and fails safe.

Routing on price and tier alone is thin, so the catalog adds specialities,
latency, throughput and benchmark slots. Every one of those is a claim about
the world, and a wrong claim here is worse than no claim: it silently steers
every routing decision while looking authoritative. So the rules are pinned:

* every quantitative claim names a source (``lint_catalog``);
* benchmarks ship empty rather than invented;
* absent data reads as UNKNOWN, never as bad;
* the catalog and the host registry describe the same set of models.
"""

import json

import pytest

from ctx import catalog
from ctx.hosts import all_hosts


def test_shipped_catalog_lints_clean():
    assert catalog.lint_catalog() == []


def test_lint_catches_an_unsourced_claim():
    """The guard has to actually fire, or it is decoration."""
    bad = {"models": [
        {"match": "m1", "latency_class": "fast"},                       # no source
        {"match": "m2", "throughput_output_tok_s": {"median": 10}},     # no source
        {"match": "m3", "benchmarks": {"swe": {"score": 70}}},          # no source
        {"match": "m4", "observed_behaviour": {"detail": "x"}},         # no source
    ]}
    problems = catalog.lint_catalog(bad)
    assert len(problems) == 4
    joined = " ".join(problems)
    for name in ("m1", "m2", "m3", "m4"):
        assert name in joined


def test_benchmarks_ship_empty_rather_than_invented():
    """Public scores for these model versions are not in this repo's evidence
    base. An empty map is the honest state; a populated one must carry sources,
    which `lint_catalog` enforces."""
    tbl = catalog.load_catalog()
    for row in tbl["models"]:
        assert row.get("benchmarks") == {}, (
            f"{row['match']}: benchmarks populated — every score needs a "
            f"`source`, and test_shipped_catalog_lints_clean will demand one"
        )


def test_unsourced_benchmark_is_never_returned():
    tbl = {"models": [{"match": "m", "benchmarks": {"swe": {"score": 99}}}]}
    assert catalog.benchmark("m", "swe", table=tbl) is None


def test_absent_data_is_unknown_not_bad():
    """A model nobody has measured must not be penalised for it."""
    assert catalog.throughput("brand-new-model") is None       # unknown, not 0
    assert catalog.speciality_score("brand-new-model", ("implement",)) == 0
    # ...and unknown latency sorts as moderate, never optimistically as fast
    assert catalog.latency_class("brand-new-model") == "moderate"


def test_speciality_score_rewards_fit_and_penalises_known_misfit():
    assert catalog.speciality_score("gemini-3.6-flash", ("implement", "edit")) == 2
    assert catalog.speciality_score("gemini-3.5-flash-lite", ("architecture",)) < 0
    assert catalog.speciality_score("gemini-3.6-flash", ()) == 0


def test_every_registry_model_has_a_catalog_row():
    """The two tables must describe the same world. A model added to hosts.py
    with no catalog row routes on price alone and nobody notices."""
    missing = []
    for spec in all_hosts():
        if not spec.harnessable:
            continue
        for model in spec.models:
            if not catalog.entry_for(model.id):
                missing.append(f"{spec.name}/{model.id}")
    assert not missing, f"models with no catalog entry: {missing}"


def test_catalog_rows_match_a_real_model():
    """And the reverse: a stale catalog row for a model no host runs is dead
    weight that will be trusted by a coordinator reading the skill."""
    known = {m.id for s in all_hosts() for m in s.models}
    for row in catalog.load_catalog()["models"]:
        assert any(catalog._token_matches(row["match"], mid) for mid in known), (
            f"catalog row {row['match']!r} matches no model in the registry"
        )


def test_workspace_override_merges_per_model(tmp_path):
    """A repo tuning one model must not have to restate the whole table."""
    (tmp_path / ".ctx-catalog.json").write_text(json.dumps({
        "models": [{"match": "gemini-3.6-flash", "latency_class": "deliberate",
                    "latency_source": "local measurement"}]
    }))
    assert catalog.latency_class("gemini-3.6-flash", workspace_root=tmp_path) == "deliberate"
    # untouched models survive the merge
    assert "plan" in catalog.specialities("claude-opus-4.8", workspace_root=tmp_path)


def test_broken_override_degrades_to_the_shipped_table(tmp_path):
    """A malformed override must not break routing — it must be ignored."""
    (tmp_path / ".ctx-catalog.json").write_text("{not json")
    assert catalog.latency_class("gemini-3.6-flash", workspace_root=tmp_path) == "fast"


@pytest.mark.parametrize("row", catalog.load_catalog()["models"],
                         ids=lambda r: r["match"])
def test_measured_claims_cite_a_receipt(row):
    """A source has to point somewhere a reader can check: a receipt path, a
    URL, or the explicit `declared-heuristic` admission that it is a judgement
    call rather than a measurement."""
    for key in ("throughput_output_tok_s", "observed_behaviour", "observed_cost_risk"):
        got = row.get(key)
        if isinstance(got, dict):
            src = str(got.get("source", ""))
            assert src.startswith(("evals/", "http", "docs/", "spec/")), (
                f"{row['match']}.{key}: source {src!r} is not checkable"
            )
    if row.get("latency_source"):
        assert row["latency_source"] == "declared-heuristic" or \
            str(row["latency_source"]).startswith(("evals/", "http", "docs/"))
