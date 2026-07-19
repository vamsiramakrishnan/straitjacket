"""Acceptance: evidence_outcome/v1 — deterministic evidence-to-action
attribution (docs/EVIDENCE-PLANS.md §plan-value). Every required scenario
from the attribution contract, plus determinism and censoring."""

from ctx.evidence_outcomes import (
    OUTCOME_VOCABULARY,
    REASON_CONFIDENCE,
    Action,
    EvidenceEmission,
    ObservationWindow,
    attribute,
    combine_confidence,
    make_event,
)


def _em(index=0, operator="op:test", **kw):
    kw.setdefault("signature", "pytest")
    return EvidenceEmission(index=index, operator=operator, **kw)


def _bash(index, command, result=""):
    from ctx import reflex

    return Action(
        index=index,
        kind="retrieval" if reflex.landing_ref(command) else "bash",
        command=command,
        signature=reflex.command_signature(command),
        result_text=result,
    )


def _edit(index, file, old, new=""):
    return Action(index=index, kind="edit", file=file, old_string=old, new_string=new)


# ------------------------------------------------------------ core rules


def test_exact_handle_landing():
    em = _em(handles=frozenset({"run:abc123def456"}))
    acts = [_bash(1, "ctx get run:abc123def456#stdout --lines 10:20")]
    (ev,) = attribute([em], acts, session_complete=True)
    assert "retrieved" in ev.outcomes and "landed" in ev.outcomes
    assert "narrowed" in ev.outcomes  # addressed-span retrieval narrows
    assert "exact_handle" in ev.attribution_reasons
    assert ev.attribution_confidence == 1.0
    assert not ev.censored


def test_edit_span_overlap():
    em = _em(raw_text="def apply_discount(cart):\n    return cart.total * 0.9\n")
    acts = [_edit(1, "src/cart.py", "def apply_discount(cart):", "def apply_discount(cart, pct):")]
    (ev,) = attribute([em], acts, session_complete=True)
    assert "landed" in ev.outcomes
    assert "edit_span_overlap" in ev.attribution_reasons
    assert ev.attribution_confidence == REASON_CONFIDENCE["edit_span_overlap"]


def test_validated_after_edit():
    em = _em(
        raw_text=(
            "tests/test_x.py::test_edge FAILED\n"
            "    return total_with_tax(cart)\n"
        ),
        test_ids=frozenset({"tests/test_x.py::test_edge"}),
        failing_ids=frozenset({"tests/test_x.py::test_edge"}),
    )
    acts = [
        _edit(1, "src/cart.py", "    return total_with_tax(cart)"),
        _bash(2, "pytest tests/test_x.py", "3 passed in 0.2s"),
    ]
    (ev,) = attribute([em], acts, session_complete=True)
    assert "validated_after_edit" in ev.outcomes
    assert "mapped_failures_resolved" in ev.attribution_reasons


def test_narrowing_via_exact_test_id():
    em = _em(
        signature="pytest",
        test_ids=frozenset({"tests/test_x.py::test_edge"}),
        raw_text="tests/test_x.py::test_edge FAILED",
    )
    acts = [_bash(1, "pytest tests/test_x.py::test_edge -x")]
    (ev,) = attribute([em], acts, session_complete=True)
    assert "landed" in ev.outcomes and "narrowed" in ev.outcomes
    assert "exact_test_id" in ev.attribution_reasons


def test_equivalent_requery_without_intervening_edit():
    em = _em(signature="pytest", test_ids=frozenset({"tests/test_x.py::t"}))
    acts = [_bash(1, "pytest -v 2>&1 | tail -50")]  # same signature, cosmetic flags
    (ev,) = attribute([em], acts, session_complete=True)
    assert "equivalent_requery" in ev.outcomes
    assert "equivalent_signature" in ev.attribution_reasons


def test_requery_after_edit_is_legitimate():
    em = _em(signature="pytest", test_ids=frozenset({"tests/test_x.py::t"}))
    acts = [
        _edit(1, "src/mod.py", "some_long_original_line_here"),
        _bash(2, "pytest -q"),
    ]
    (ev,) = attribute([em], acts, session_complete=True)
    assert "equivalent_requery" not in ev.outcomes


def test_scope_flags_stay_distinct():
    from ctx import reflex

    sigs = {
        reflex.command_signature(c)
        for c in ("pytest", "pytest -k auth", "pytest -m integration", "pytest --lf")
    }
    assert len(sigs) == 4  # scope flags survive normalization (EDC §7 tables)


def test_censored_session():
    em = _em(test_ids=frozenset({"tests/test_x.py::t"}))
    acts = [_bash(1, "ls")]  # window still open when the transcript ends
    (ev,) = attribute([em], acts, session_complete=False)
    assert ev.censored is True
    assert "abandoned" not in ev.outcomes  # censoring is never negative


def test_attribution_conflict_degrades_deterministically():
    e1 = _em(index=0, operator="op:a", test_ids=frozenset({"tests/t.py::x"}))
    e2 = _em(index=1, operator="op:b", test_ids=frozenset({"tests/t.py::x"}))
    acts = [_bash(2, "pytest tests/t.py::x")]
    ev1, ev2 = attribute([e1, e2], acts, session_complete=True)
    # Both open windows claim the identity: no arbitrary winner, both land
    # with the degraded shared_identity reason — deterministic confidence.
    for ev in (ev1, ev2):
        assert "landed" in ev.outcomes
        assert "shared_identity" in ev.attribution_reasons
    assert attribute([e1, e2], acts, session_complete=True) == [ev1, ev2]


def test_candidate_reversal_then_other_candidate_validates():
    em_a = _em(index=0, operator="op:a", candidates=(("cand_alpha", 1),),
               raw_text="cand_alpha implicated by trace")
    em_b = _em(index=3, operator="op:b", candidates=(("cand_beta", 1),),
               raw_text="cand_beta implicated by dynamic run",
               failing_ids=frozenset({"tests/t.py::x"}),
               test_ids=frozenset({"tests/t.py::x"}))
    acts = [
        _edit(1, "src/alpha.py", "original cand_alpha body line", "patched"),
        _edit(2, "src/alpha.py", "patched", "original cand_alpha body line"),  # revert
        _edit(4, "src/beta.py", "original cand_beta body line", "fixed"),
        _bash(5, "pytest tests/t.py", "5 passed"),
    ]
    events = attribute([em_a, em_b], acts, session_complete=True)
    by_op = {e.operator: e for e in events}
    assert "reversed" in by_op["op:a"].outcomes
    assert "edit_reverted" in by_op["op:a"].attribution_reasons
    assert "validated_after_edit" in by_op["op:b"].outcomes


def test_redundant_emission():
    e1 = _em(index=0, operator="op:a", test_ids=frozenset({"tests/t.py::x"}))
    e2 = _em(index=1, operator="op:b", test_ids=frozenset({"tests/t.py::x"}))
    events = attribute([e1, e2], [], session_complete=True)
    by_op = {e.operator: e for e in events}
    assert "redundant" in by_op["op:b"].outcomes
    assert "identity_subset" in by_op["op:b"].attribution_reasons
    assert "redundant" not in by_op["op:a"].outcomes


def test_abandoned_only_on_clean_window_expiry():
    em = _em(test_ids=frozenset({"tests/t.py::x"}))
    acts = [_bash(i, "ls") for i in range(1, 8)]  # 7 unrelated actions > window
    (ev,) = attribute([em], acts, window=ObservationWindow(max_actions=6),
                      session_complete=True)
    assert "abandoned" in ev.outcomes and not ev.censored


# ------------------------------------------------------------ schema laws


def test_event_identity_is_content_derived_and_deterministic():
    kw = dict(
        investigation_id=None, plan_node_id=None,
        evidence_ids=("b", "a"), candidate_ids=(),
        downstream_action_kind="bash", downstream_action_ref=None,
        outcomes=("landed",), attribution_reasons=("exact_symbol",),
        generation_before="g0", generation_after="g1",
        actions_observed=2, censored=False, operator="op:x",
    )
    e1, e2 = make_event(**kw), make_event(**kw)
    assert e1.event_id == e2.event_id
    assert e1.evidence_ids == ("a", "b")  # sorted deterministically


def test_confidence_is_max_of_reasons():
    assert combine_confidence(("exact_handle", "scope_narrowing")) == 1.0
    assert combine_confidence(("scope_narrowing",)) == 0.75


def test_closed_vocabularies_reject_unknowns():
    import pytest as _pytest

    with _pytest.raises(ValueError):
        make_event(
            investigation_id=None, plan_node_id=None, evidence_ids=(),
            candidate_ids=(), downstream_action_kind="bash",
            downstream_action_ref=None, outcomes=("mystery",),
            attribution_reasons=(), generation_before=None,
            generation_after=None, actions_observed=0, censored=False,
        )
    assert "landed" in OUTCOME_VOCABULARY
