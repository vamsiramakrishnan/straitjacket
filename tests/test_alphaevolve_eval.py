from __future__ import annotations

from evals.alphaevolve.evaluate import (
    INITIAL_PROGRAM_CODE,
    INVALID_SCORE,
    METRIC_NAME,
    alphaevolve_evaluation_function,
    score_candidate,
)


def test_seed_is_deterministic_and_passes_hard_gates():
    first = score_candidate(INITIAL_PROGRAM_CODE)
    second = score_candidate(INITIAL_PROGRAM_CODE)
    assert first["error"] is None
    assert first["score"] == second["score"]
    assert set(first["cases"]) == set(second["cases"])
    assert 0.0 < first["score"] <= 100.0


def test_invalid_candidate_receives_hard_penalty():
    candidate = INITIAL_PROGRAM_CODE.replace(
        "    return sorted(chosen)[:budget]",
        "    return [len(lines) + 1]",
    )
    result = score_candidate(candidate)
    assert result["score"] == INVALID_SCORE
    assert "outside input" in result["error"]


def test_unsafe_import_inside_evolve_block_is_rejected():
    candidate = INITIAL_PROGRAM_CODE.replace(
        "# EVOLVE-BLOCK-START",
        "# EVOLVE-BLOCK-START\nimport os",
    )
    result = score_candidate(candidate)
    assert result["score"] == INVALID_SCORE
    assert result["error"] == "blocked import: os"


def test_pure_stdlib_import_and_lambda_are_allowed():
    candidate = INITIAL_PROGRAM_CODE.replace(
        "# EVOLVE-BLOCK-START",
        "# EVOLVE-BLOCK-START\nimport re\n_identity = lambda value: value",
    )
    result = score_candidate(candidate)
    assert result["error"] is None


def test_controller_adapter_has_expected_metric_shape():
    candidate = {"content": {"files": [{"content": INITIAL_PROGRAM_CODE}]}}
    result = alphaevolve_evaluation_function(candidate)
    score = result["scores"]["scores"][0]
    assert score["metric"] == METRIC_NAME
    assert score["score"] > INVALID_SCORE
