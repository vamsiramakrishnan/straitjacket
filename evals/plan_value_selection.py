#!/usr/bin/env python3
"""Seeded shadow-ranking eval — mechanistic acceptance (the ponytail cut).

Proves the lexicographic follow-up ranking behaves as specified on seeded
fixtures, model-free and deterministic. Scope matches the reshaped design:
this validates the SHADOW ranking (report only). Counterfactual value —
whether following the shadow ordering would cut turns/cost at equal task
success — is the paired referee's question, and no online behavior changes
before that referee passes.

Run:  python evals/plan_value_selection.py
Exit non-zero on any fixture failure (CI-friendly).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ctx.plan_value import (  # noqa: E402
    CandidateAction,
    rank_followup,
    render_shadow,
    wilson_lower_bound,
)

# Counts table exactly as `ctx policy compile --plan-value` emits it.
PRIORS = {
    "evidence.join": {
        "observations": 84, "used_exactly": 68, "validation_associated": 40,
        "equivalent_requery": 3, "censored": 5,
        "median_cost_ms": 12, "median_visible_tokens": 48,
    },
    "code.refs": {
        "observations": 51, "used_exactly": 27, "validation_associated": 14,
        "equivalent_requery": 7, "censored": 4,
        "median_cost_ms": 95, "median_visible_tokens": 73,
    },
    "semantic.taint": {
        "observations": 22, "used_exactly": 4, "validation_associated": 2,
        "equivalent_requery": 6, "censored": 3,
        "median_cost_ms": 1800, "median_visible_tokens": 121,
    },
    "code.tiny_sample": {
        "observations": 2, "used_exactly": 2, "validation_associated": 2,
        "equivalent_requery": 0, "censored": 0,
        "median_cost_ms": 1, "median_visible_tokens": 5,
    },
}

JOIN = CandidateAction(op="evidence.join", cost_class="index")
REFS = CandidateAction(op="code.refs", cost_class="scan")
TAINT = CandidateAction(op="semantic.taint", cost_class="process")
TINY = CandidateAction(op="code.tiny_sample", cost_class="scan")


def fixture_a() -> list[str]:
    """Strong-evidence join preferred: 68/84 exact-use at index cost beats
    every alternative on the lexicographic key."""
    ranked = rank_followup([JOIN, REFS, TAINT], PRIORS)
    assert ranked[0].op == "evidence.join", f"A: got {ranked[0].op}"
    return [
        "== Fixture A · strong follow-up record preferred ==",
        render_shadow("code.refs", ranked),
        "",
    ]


def fixture_b() -> list[str]:
    """Sample-size honesty: 2/2 ('100%') must not outrank 68/84 — the
    Wilson lower bound is the entire confidence treatment."""
    ranked = rank_followup([JOIN, TINY], PRIORS)
    assert ranked[0].op == "evidence.join", f"B: got {ranked[0].op}"
    lb_tiny = wilson_lower_bound(2, 2)
    lb_join = wilson_lower_bound(68, 84)
    assert lb_join > lb_tiny
    return [
        "== Fixture B · 2/2 cannot outrank 68/84 ==",
        f"wilson(2/2)  = {lb_tiny:.2f}",
        f"wilson(68/84) = {lb_join:.2f}",
        f"preferred: {ranked[0].op}",
        "",
    ]


def fixture_c() -> list[str]:
    """Shadow disagreement is a REPORT, not a reorder: a plan that declared
    semantic.taint first gets an agreement=no line with the lexicographic
    reason — nothing is suppressed or reordered."""
    ranked = rank_followup([TAINT, JOIN], PRIORS)
    out = render_shadow("semantic.taint", ranked)
    assert "agreement: no" in out, "C: expected disagreement report"
    assert "report only; never reorders" in out
    assert "value score" not in out  # no scalar exists to hide behind
    return ["== Fixture C · disagreement reported, never enforced ==", out, ""]


def main() -> int:
    out: list[str] = ["plan-value shadow-ranking eval (mechanistic acceptance)", ""]
    for fx in (fixture_a, fixture_b, fixture_c):
        out.extend(fx())
    out.append("ALL FIXTURES PASS")
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
