"""Shared completion-gated evaluator for discrete AlphaEvolve policies.

The production levers differ, but most make the same bounded decision: choose
one admissible strategy from a supplied menu. This module gives those campaigns
one metric contract so cost savings cannot outrank safety or completion.
"""

from __future__ import annotations

import math
import statistics
from typing import Any, Iterable, Mapping

from evals.alphaevolve.sandbox import (
    INVALID_SCORE,
    candidate_code,
    controller_evaluation,
    run_candidate,
)

METRICS = ("dollars", "visible_tokens", "model_turns", "tool_calls", "latency_ms")
DEFAULT_WEIGHTS: Mapping[str, float] = {
    "dollars": 0.30,
    "visible_tokens": 0.30,
    "model_turns": 0.20,
    "tool_calls": 0.10,
    "latency_ms": 0.10,
}


def option(
    option_id: str,
    provides: Iterable[str],
    *,
    dollars: float,
    visible_tokens: int,
    model_turns: int = 1,
    tool_calls: int = 1,
    latency_ms: int = 1,
    safe: bool = True,
) -> dict[str, Any]:
    return {
        "id": option_id,
        "provides": tuple(provides),
        "safe": safe,
        "dollars": dollars,
        "visible_tokens": visible_tokens,
        "model_turns": model_turns,
        "tool_calls": tool_calls,
        "latency_ms": latency_ms,
    }


def case(
    name: str,
    state: Mapping[str, Any],
    required: Iterable[str],
    *,
    baseline: str,
    allowed: Iterable[str] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "state": dict(state),
        "required": frozenset(required),
        "baseline": baseline,
        "allowed": frozenset(allowed) if allowed is not None else None,
    }


def _percentile_low(values: list[float]) -> float:
    """Conservative lower decile without interpolation surprises."""
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * 0.10) - 1)]


def score_choice_policy(
    code: str,
    function_name: str,
    cases: tuple[dict[str, Any], ...],
    options: tuple[dict[str, Any], ...],
    *,
    weights: Mapping[str, float] = DEFAULT_WEIGHTS,
) -> dict[str, Any]:
    """Score a deterministic choice policy with lexicographic hard gates."""
    calls = [{"state": row["state"], "options": options} for row in cases]
    run = run_candidate(code, function_name, calls)
    if run["error"]:
        return {"score": INVALID_SCORE, "error": run["error"], "cases": {}}

    by_id = {row["id"]: row for row in options}
    selected: list[dict[str, Any]] = []
    baselines: list[dict[str, Any]] = []
    failures: list[str] = []
    docs: dict[str, dict[str, Any]] = {}
    for row, selected_id in zip(cases, run["outputs"], strict=True):
        if not isinstance(selected_id, str) or selected_id not in by_id:
            return {
                "score": INVALID_SCORE,
                "error": (
                    f"{row['name']}: candidate must return one string option id; "
                    f"got {selected_id!r}"
                ),
                "cases": {},
            }
        selected_option = by_id[selected_id]
        baseline = by_id.get(row["baseline"])
        if baseline is None:
            return {
                "score": INVALID_SCORE,
                "error": f"{row['name']}: unknown baseline {row['baseline']!r}",
                "cases": {},
            }
        allowed = row.get("allowed")
        admissible = allowed is None or selected_id in allowed
        complete = row["required"].issubset(set(selected_option["provides"]))
        safe = bool(selected_option.get("safe", False))
        passed = admissible and complete and safe
        if not passed:
            failures.append(row["name"])
        selected.append(selected_option)
        baselines.append(baseline)
        docs[row["name"]] = {
            "option": selected_id,
            "baseline": row["baseline"],
            "completion": 1.0 if passed else 0.0,
            "safe": safe,
            "admissible": admissible,
        }

    if failures:
        return {
            "score": -100_000.0 - 1_000.0 * len(failures),
            "error": "unsafe, inadmissible, or incomplete cases: " + ", ".join(failures),
            "cases": docs,
        }

    totals = {
        metric: sum(float(row[metric]) for row in selected) for metric in METRICS
    }
    baseline_totals = {
        metric: sum(float(row[metric]) for row in baselines) for metric in METRICS
    }
    reductions = {
        metric: 1.0 - totals[metric] / baseline_totals[metric]
        for metric in METRICS
        if baseline_totals[metric] > 0
    }
    multipliers = {
        metric: baseline_totals[metric] / totals[metric]
        for metric in METRICS
        if totals[metric] > 0
    }

    # Optimize multiplicatively. The lower-decile term prevents a spectacular
    # flood win from hiding a reversal on small/naive cases.
    metric_scores: dict[str, float] = {}
    for metric in METRICS:
        case_gains = [
            math.log2(float(base[metric]) / float(got[metric]))
            for got, base in zip(selected, baselines, strict=True)
            if float(got[metric]) > 0 and float(base[metric]) > 0
        ]
        metric_scores[metric] = (
            statistics.median(case_gains) + 0.5 * _percentile_low(case_gains)
            if case_gains
            else 0.0
        )
    score = 100.0 + 10.0 * sum(
        float(weights.get(metric, 0.0)) * metric_scores[metric]
        for metric in METRICS
    )
    return {
        "score": score,
        "error": None,
        "cases": docs,
        "totals": totals,
        "baseline": baseline_totals,
        "reductions": reductions,
        "baseline_over_candidate": multipliers,
        "log2_gain": metric_scores,
        "pareto_beats_baseline": all(
            totals[m] <= baseline_totals[m] for m in METRICS
        )
        and any(totals[m] < baseline_totals[m] for m in METRICS),
        "elapsed_ms": run["elapsed_ms"],
    }


def controller_adapter(
    program_candidate: dict[str, Any],
    *,
    metric_name: str,
    function_name: str,
    cases: tuple[dict[str, Any], ...],
    options: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    try:
        result = score_choice_policy(
            candidate_code(program_candidate), function_name, cases, options
        )
    except (KeyError, IndexError, TypeError) as exc:
        result = {"score": INVALID_SCORE, "error": f"invalid envelope: {exc}"}
    detail = result.get("error") or (
        f"all hard gates passed; score={result['score']:.4f}; "
        f"dollar multiplier={result['baseline_over_candidate']['dollars']:.3f}; "
        f"visible-token multiplier={result['baseline_over_candidate']['visible_tokens']:.3f}"
    )
    return controller_evaluation(metric_name, result["score"], detail)
