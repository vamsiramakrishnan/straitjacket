from __future__ import annotations

import json

import pytest

from evals.alphaevolve import portfolio
from evals.alphaevolve.campaign import (
    CampaignEvaluator,
    _normalize,
    campaign_evaluator,
    write_campaign_artifacts,
)
from evals.alphaevolve.ensemble import DEFAULT_MODEL, model_mixture, parse_model_mix
from evals.alphaevolve.sandbox import INVALID_SCORE


def test_parse_model_mix_accepts_upstream_flash_pro_blend():
    entries = parse_model_mix("gemini-3.5-flash:0.7,gemini-3.1-pro-preview:0.3")
    assert [entry["name"] for entry in entries] == [
        "gemini-3.5-flash",
        "gemini-3.1-pro-preview",
    ]


@pytest.mark.parametrize(
    "spec",
    (
        "",
        "gpt-4:1",
        "gemini-3.5-flash:0",
        "gemini-3.5-flash:-1",
        "gemini-3.5-flash:0.5,gemini-3.5-flash:0.5",
        "gemini-3.5-flash",
    ),
)
def test_parse_model_mix_rejects_invalid_specs(spec):
    with pytest.raises(ValueError):
        parse_model_mix(spec)


def test_model_mixture_defaults_to_single_fast_model():
    assert model_mixture() == [{"name": DEFAULT_MODEL, "weight": 1.0}]


def test_model_mixture_normalizes_cli_weights():
    entries = model_mixture("gemini-3.5-flash:3,gemini-3.1-pro-preview:1")
    assert entries[0]["weight"] == pytest.approx(0.75)
    assert entries[1]["weight"] == pytest.approx(0.25)


def test_model_mixture_reads_upstream_env_names(monkeypatch):
    monkeypatch.setenv("MODEL_1", "gemini-3.5-flash")
    monkeypatch.setenv("MODEL_1_WEIGHT", "0.8")
    monkeypatch.setenv("MODEL_2", "gemini-3.1-pro-preview")
    monkeypatch.setenv("MODEL_2_WEIGHT", "0.2")
    entries = model_mixture()
    assert {entry["name"]: entry["weight"] for entry in entries} == {
        "gemini-3.5-flash": pytest.approx(0.8),
        "gemini-3.1-pro-preview": pytest.approx(0.2),
    }


def test_model_mixture_rejects_global_only_model_elsewhere(monkeypatch):
    monkeypatch.setenv("LOCATION", "us-central1")
    with pytest.raises(ValueError, match="global"):
        model_mixture("gemini-3.1-pro-preview:1")
    assert model_mixture("gemini-3.1-pro-preview:1", location="global")


def test_campaign_evaluator_memoizes_identical_source():
    module = portfolio.load_experiment("wave-policy")
    evaluator = campaign_evaluator(module)
    candidate = {"content": {"files": [{"content": module.INITIAL_PROGRAM_CODE}]}}
    first = evaluator(candidate)
    second = evaluator(candidate)
    assert evaluator.stats == {"evaluations": 1, "memo_hits": 1}
    assert first["scores"] == second["scores"]
    assert first["campaign"]["cache_key"] == second["campaign"]["cache_key"]


def test_campaign_evaluator_whitespace_only_resubmissions_hit_the_memo():
    module = portfolio.load_experiment("emission-policy")
    evaluator = campaign_evaluator(module)
    padded = module.INITIAL_PROGRAM_CODE.replace(
        "# EVOLVE-BLOCK-END", "\n# EVOLVE-BLOCK-END   "
    )
    first = evaluator({"content": {"files": [{"content": module.INITIAL_PROGRAM_CODE}]}})
    second = evaluator({"content": {"files": [{"content": padded}]}})
    assert first["campaign"]["cache_key"] == second["campaign"]["cache_key"]
    assert evaluator.stats["evaluations"] == 1


def test_campaign_evaluator_submits_primary_plus_component_scores():
    module = portfolio.load_experiment("wave-policy")
    evaluator = campaign_evaluator(module)
    envelope = evaluator(
        {"content": {"files": [{"content": module.INITIAL_PROGRAM_CODE}]}}
    )
    metrics = [entry["metric"] for entry in envelope["scores"]["scores"]]
    assert metrics[0] == module.METRIC_NAME
    component = [name for name in metrics if name.startswith(f"{module.METRIC_NAME}.")]
    assert any(name.endswith("_log2_gain") for name in component)


def test_campaign_evaluator_failure_insight_carries_a_hint():
    module = portfolio.load_experiment("context-budget")
    evaluator = campaign_evaluator(module)
    broken = module.INITIAL_PROGRAM_CODE.replace(
        "# EVOLVE-BLOCK-START", "# EVOLVE-BLOCK-START\nimport os"
    )
    envelope = evaluator({"content": {"files": [{"content": broken}]}})
    text = envelope["insights"]["insights"][0]["text"]
    assert "blocked import: os" in text
    assert "Hint:" in text


def test_campaign_evaluator_rejects_invalid_envelopes_without_spawning():
    module = portfolio.load_experiment("wave-policy")
    evaluator = campaign_evaluator(module)
    envelope = evaluator({"content": {"files": []}})
    assert envelope["scores"]["scores"][0]["score"] == INVALID_SCORE


def test_normalize_collapses_formatting_but_not_semantics():
    formatted = "# EVOLVE-BLOCK-START\ndef f():\n    return 1   \n\n\n# EVOLVE-BLOCK-END"
    tight = "# EVOLVE-BLOCK-START\ndef f():\n    return 1\n# EVOLVE-BLOCK-END"
    assert _normalize(formatted) == _normalize(tight)
    assert _normalize(tight) != _normalize(tight.replace("return 1", "return 2"))


def test_write_campaign_artifacts_persists_ranked_winner(tmp_path):
    module = portfolio.load_experiment("wave-policy")
    rows = [
        {"program": "evo-0007", "score": 128.5, "evolved": True,
         "state": "COMPLETED", "content": "# evolved winner"},
        {"program": "seed-0000", "score": 100.0, "evolved": False,
         "state": "COMPLETED", "content": "# seed"},
        {"program": "pending", "score": None, "evolved": True, "state": "PENDING"},
    ]
    paths = write_campaign_artifacts(
        module,
        "wave-policy",
        rows,
        config={"models": [{"name": "gemini-3.5-flash"}]},
        fingerprint="abcdef0123456789",
        results_dir=tmp_path,
    )
    summary = json.loads((tmp_path / paths["summary"].rsplit("/", 1)[-1]).read_text())
    winner = (tmp_path / paths["winner"].rsplit("/", 1)[-1]).read_text()
    assert summary["schema"] == "ctx.alphaevolve-managed-campaign/v1"
    assert summary["experiment"] == "wave-policy"
    assert summary["metric"] == module.METRIC_NAME
    assert summary["programs_ranked"] == 2
    assert summary["top_programs"][0]["program"] == "evo-0007"
    assert winner == "# evolved winner"


def test_experiment_config_embeds_the_requested_mixture():
    module = portfolio.load_experiment("wave-policy")
    mix = model_mixture("gemini-3.5-flash:0.7,gemini-3.1-pro-preview:0.3")
    config = portfolio.experiment_config(module, max_programs=12, concurrency=2, model_mix=mix)
    assert config["generation_settings"]["models"] == mix
    assert config["run_settings"] == {"max_programs": 12, "concurrency": 2}
    assert "problem_description" in config


def test_parallel_scorecard_matches_serial_evaluation():
    selected = ("surface-policy", "guard-policy")
    parallel = portfolio.local_scorecard(selected)
    serial_rows = {
        name: portfolio._experiment_row(name)[1]
        for name in selected
    }
    assert parallel["experiments"] == serial_rows
    assert parallel["all_gates_pass"] is True


def test_campaign_evaluator_is_a_drop_in_managed_evaluator():
    module = portfolio.load_experiment("surface-policy")
    evaluator: CampaignEvaluator = campaign_evaluator(module)
    candidate = {"content": {"files": [{"content": module.INITIAL_PROGRAM_CODE}]}}
    legacy = module.evaluation_function(dict(candidate))
    upgraded = evaluator(dict(candidate))
    assert (
        legacy["scores"]["scores"][0]["metric"]
        == upgraded["scores"]["scores"][0]["metric"]
    )
    assert upgraded["insights"]["insights"][0]["label"] == "Evaluator"
