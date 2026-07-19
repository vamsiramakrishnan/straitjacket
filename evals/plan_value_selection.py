#!/usr/bin/env python3
"""Seeded plan-value selection eval — mechanistic acceptance (Part 13).

Proves the compiled priors change investigation decisions usefully on three
seeded fixtures, model-free and deterministic. This is NOT a cost-savings
claim: it is an acceptance check that the selection layer (a) prefers the
cheap high-prior join when it fills the missing dimensions, (b) defers the
expensive semantic scan until cheaper actions have run, and (c) re-ranks
after a hypothesis-contradicting replan changes the missing dimensions.

Run:  python evals/plan_value_selection.py
Exit non-zero on any fixture failure (CI-friendly).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ctx.plan_value import (  # noqa: E402
    CandidateAction,
    rank_actions,
    render_ranking,
    select_batch,
    stopping_decision,
)

# Priors as `ctx policy compile --plan-value` emits them (counts + rates +
# confidence). Seeded here so the eval is hermetic; the shape is identical
# to the committed [plan_value] table.
PRIORS = {
    "version": 1,
    "minimum_observations": 5,
    "*": {
        "observations": 160, "confidence": "high",
        "landing_rate": 0.40, "narrowing_rate": 0.30, "discrimination_rate": 0.20,
        "validation_rate": 0.20, "retrieval_rate": 0.15,
        "equivalent_requery_rate": 0.08, "redundancy_rate": 0.10, "reversal_rate": 0.03,
    },
    "evidence.join": {
        "observations": 84, "confidence": "high",
        "landing_rate": 0.79, "narrowing_rate": 0.71, "discrimination_rate": 0.63,
        "validation_rate": 0.54, "retrieval_rate": 0.22,
        "equivalent_requery_rate": 0.03, "redundancy_rate": 0.05, "reversal_rate": 0.02,
    },
    "semantic.taint": {
        "observations": 22, "confidence": "medium",
        "landing_rate": 0.18, "narrowing_rate": 0.14, "discrimination_rate": 0.10,
        "validation_rate": 0.09, "retrieval_rate": 0.05,
        "equivalent_requery_rate": 0.10, "redundancy_rate": 0.32, "reversal_rate": 0.05,
    },
}

JOIN = CandidateAction(op="evidence.join",
                       provides={"causality": 1.0, "changedness": 0.8,
                                 "dynamic_failure": 0.4}, cost_class="index")
REFS = CandidateAction(op="code.refs",
                       provides={"topology": 0.8, "semantic_support": 0.3},
                       cost_class="scan")
TAINT = CandidateAction(op="semantic.taint",
                        provides={"semantic_support": 1.0, "counterevidence": 0.5},
                        cost_class="process")
TEST_RUN = CandidateAction(op="test.run",
                           provides={"dynamic_failure": 1.0, "freshness": 0.8,
                                     "coverage": 0.3},
                           cost_class="test", klass="execute")
TRACEBACK = CandidateAction(op="code.search",
                            provides={"topology": 0.4, "coverage": 0.3},
                            cost_class="scan")


def fixture_a() -> list[str]:
    """Changed files + failures known; causality missing → cheap join wins."""
    coverage = {"changedness": 1.0, "dynamic_failure": 1.0}
    floors = {"causality": 0.8, "changedness": 1.0, "dynamic_failure": 1.0,
              "counterevidence": 0.5}
    ranked = rank_actions([JOIN, REFS, TAINT, TRACEBACK], coverage, floors, PRIORS)
    lines = ["== Fixture A · cheap join should win ==", render_ranking(ranked), ""]
    assert ranked[0].op == "evidence.join", f"A: expected evidence.join, got {ranked[0].op}"
    return lines


def fixture_b() -> list[str]:
    """No dynamic evidence yet → semantic scan deferred; after the cheaper
    actions run and the source-to-sink question remains, taint ranks first."""
    floors = {"dynamic_failure": 1.0, "changedness": 1.0, "semantic_support": 0.5}
    first = rank_actions([TAINT, TEST_RUN, JOIN, REFS], {}, floors, PRIORS)
    assert first[0].op != "semantic.taint", f"B1: taint must be deferred, got {first[0].op}"
    after = {"dynamic_failure": 1.0, "changedness": 1.0, "causality": 1.0,
             "semantic_support": 0.3}
    second = rank_actions([TAINT, TEST_RUN], after, floors, PRIORS)
    assert second[0].op == "semantic.taint", f"B2: expected taint, got {second[0].op}"
    return [
        "== Fixture B · expensive semantic scan deferred, then chosen ==",
        f"initial best: {first[0].op} (score {first[0].score:.2f}) · "
        f"taint deferred at {[s.score for s in first if s.op == 'semantic.taint'][0]:.2f}",
        f"after cheap actions: {second[0].op} (score {second[0].score:.2f})",
        "",
    ]


def fixture_c() -> list[str]:
    """Hypothesis-sensitive replan: dynamic contradiction resets causality
    and raises the counterevidence floor — the ranking must follow the new
    missing dimensions (one replan epoch; node caching is plan_exec's own
    tested behavior, out of scope for this selection-layer eval)."""
    floors_before = {"causality": 0.8, "dynamic_failure": 1.0}
    cov_before = {"causality": 0.9, "dynamic_failure": 1.0}
    ranked_before = rank_actions([JOIN, TAINT, TEST_RUN], cov_before, floors_before, PRIORS)
    stop_before, _ = stopping_decision(ranked_before, cov_before, floors_before,
                                       priors=PRIORS)
    # Contradiction: candidate's causal story falsified → causality resets,
    # counterevidence becomes required.
    floors_after = {"causality": 0.8, "dynamic_failure": 1.0, "counterevidence": 0.5}
    cov_after = {"causality": 0.0, "dynamic_failure": 1.0}
    ranked_after = rank_actions([JOIN, TAINT, TEST_RUN], cov_after, floors_after, PRIORS)
    stop_after, receipt = stopping_decision(ranked_after, cov_after, floors_after,
                                            priors=PRIORS)
    assert stop_before, "C: should stop before contradiction (floors met, low value)"
    assert not stop_after, "C: must NOT stop after contradiction (floors unmet)"
    assert ranked_after[0].op == "evidence.join", (
        f"C: causality gap should re-select the join, got {ranked_after[0].op}"
    )
    batch = select_batch([JOIN, TAINT, TEST_RUN], cov_after, floors_after, PRIORS)
    return [
        "== Fixture C · hypothesis-sensitive replan ==",
        f"before contradiction: stop={stop_before} (floors met, best value below threshold)",
        f"after contradiction:  stop={stop_after} · re-selected {ranked_after[0].op} · "
        f"batch {[s.op for s in batch]}",
        receipt,
        "",
    ]


def main() -> int:
    out: list[str] = ["plan-value seeded selection eval (mechanistic acceptance)", ""]
    for fx in (fixture_a, fixture_b, fixture_c):
        out.extend(fx())
    out.append("ALL FIXTURES PASS")
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
