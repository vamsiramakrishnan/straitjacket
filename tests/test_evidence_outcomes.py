"""Acceptance: evidence_followup/v1 — deterministic evidence→follow-up
association (docs/EVIDENCE-PLANS.md). Match classes not floats, four
states, censoring that is never negative, content-derived event identity."""

from ctx.evidence_outcomes import (
    LANGUAGE_FAMILY_OF_EXTENSION,
    MATCH_CLASSES,
    Action,
    EvidenceEmission,
    ObservationWindow,
    emissions_from_calls,
    followup_join,
    language_family,
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


# ------------------------------------------------------------ match rules


def test_exact_handle_followup():
    em = _em(handles=frozenset({"run:abc123def456"}))
    acts = [_bash(1, "ctx get run:abc123def456#stdout --lines 10:20")]
    (ev,) = followup_join([em], acts, session_complete=True)
    assert ev.used_exactly and "exact_handle" in ev.match_classes
    assert not ev.censored


def test_exact_span_overlap():
    em = _em(raw_text="def apply_discount(cart):\n    return cart.total * 0.9\n")
    acts = [_edit(1, "src/cart.py", "def apply_discount(cart):", "def apply_discount(cart, pct):")]
    (ev,) = followup_join([em], acts, session_complete=True)
    assert ev.used_exactly and "exact_span_overlap" in ev.match_classes


def test_validation_associated_not_causal():
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
    (ev,) = followup_join([em], acts, session_complete=True)
    assert ev.validation_associated  # association, and named as such
    assert "exact_span_overlap" in ev.match_classes


def test_exact_test_id_followup():
    em = _em(
        signature="pytest",
        test_ids=frozenset({"tests/test_x.py::test_edge"}),
        raw_text="tests/test_x.py::test_edge FAILED",
    )
    acts = [_bash(1, "pytest tests/test_x.py::test_edge -x")]
    (ev,) = followup_join([em], acts, session_complete=True)
    assert ev.used_exactly and "exact_test_id" in ev.match_classes


def test_equivalent_requery_without_intervening_edit():
    em = _em(signature="pytest", test_ids=frozenset({"tests/test_x.py::t"}))
    acts = [_bash(1, "pytest -v 2>&1 | tail -50")]  # same signature, cosmetic flags
    (ev,) = followup_join([em], acts, session_complete=True)
    assert ev.equivalent_requery


def test_requery_after_edit_is_legitimate():
    em = _em(signature="pytest", test_ids=frozenset({"tests/test_x.py::t"}))
    acts = [
        _edit(1, "src/mod.py", "some_long_original_line_here"),
        _bash(2, "pytest -q"),
    ]
    (ev,) = followup_join([em], acts, session_complete=True)
    assert not ev.equivalent_requery


def test_scope_flags_stay_distinct():
    from ctx import reflex

    sigs = {
        reflex.command_signature(c)
        for c in ("pytest", "pytest -k auth", "pytest -m integration", "pytest --lf")
    }
    assert len(sigs) == 4  # scope flags survive normalization (EDC §7 tables)


def test_censored_session_never_negative():
    em = _em(test_ids=frozenset({"tests/test_x.py::t"}))
    acts = [_bash(1, "ls")]  # window still open when the transcript ends
    (ev,) = followup_join([em], acts, session_complete=False)
    assert ev.censored is True
    assert not ev.used_exactly and not ev.equivalent_requery


def test_shared_identity_associates_with_every_window():
    e1 = _em(index=0, operator="op:a", test_ids=frozenset({"tests/t.py::x"}))
    e2 = _em(index=1, operator="op:b", test_ids=frozenset({"tests/t.py::x"}))
    acts = [_bash(2, "pytest tests/t.py::x")]
    ev1, ev2 = followup_join([e1, e2], acts, session_complete=True)
    # No arbitrary winner and no pseudo-confidence discount: both windows
    # record the exact match; the shared evidence id is in both events.
    for ev in (ev1, ev2):
        assert ev.used_exactly and "exact_test_id" in ev.match_classes
        assert "tests/t.py::x" in ev.evidence_ids
    assert followup_join([e1, e2], acts, session_complete=True) == [ev1, ev2]


def test_window_expiry_closes_without_negative_state():
    em = _em(test_ids=frozenset({"tests/t.py::x"}))
    acts = [_bash(i, "ls") for i in range(1, 8)]
    (ev,) = followup_join(
        [em], acts, window=ObservationWindow(max_actions=6), session_complete=True
    )
    assert not ev.censored and not ev.used_exactly
    assert ev.actions_observed == 6


# ------------------------------------------------------------ schema laws


def test_event_identity_is_content_derived_and_deterministic():
    kw = dict(
        investigation_id=None, plan_node_id=None, operator="op:x",
        evidence_ids=("b", "a"), match_classes=("exact_symbol",),
        validation_associated=False, equivalent_requery=False, censored=False,
        generation_before="g0", generation_after="g1", actions_observed=2,
    )
    e1, e2 = make_event(**kw), make_event(**kw)
    assert e1.event_id == e2.event_id
    assert e1.evidence_ids == ("a", "b")  # sorted deterministically
    assert e1.used_exactly  # derived from a non-empty match set


def test_no_confidence_float_exists():
    ev = make_event(
        investigation_id=None, plan_node_id=None, operator="op:x",
        evidence_ids=(), match_classes=(), validation_associated=False,
        equivalent_requery=False, censored=False, generation_before=None,
        generation_after=None, actions_observed=0,
    )
    payload = ev.payload()
    assert "confidence" not in str(sorted(payload))  # the class IS the signal
    assert not ev.used_exactly


def test_closed_match_vocabulary_rejected():
    import pytest as _pytest

    with _pytest.raises(ValueError):
        make_event(
            investigation_id=None, plan_node_id=None, operator="op:x",
            evidence_ids=(), match_classes=("vibes_match",),
            validation_associated=False, equivalent_requery=False,
            censored=False, generation_before=None, generation_after=None,
            actions_observed=0,
        )
    assert "exact_handle" in MATCH_CLASSES


def test_optional_fields_do_not_reid_plain_events():
    kw = dict(
        investigation_id=None, plan_node_id=None, operator="op:x",
        evidence_ids=("a",), match_classes=("exact_file",),
        validation_associated=False, equivalent_requery=False, censored=False,
        generation_before=None, generation_after=None, actions_observed=1,
    )
    plain = make_event(**kw)
    explicit_none = make_event(**kw, cost_ms=None, visible_tokens=None, language=None)
    assert plain.event_id == explicit_none.event_id
    with_cost = make_event(**kw, cost_ms=12, visible_tokens=48, language="python")
    assert with_cost.event_id != plain.event_id
    assert with_cost.payload()["language"] == "python"


# ------------------------------------------------------------ language


def test_language_family_majority_and_ties():
    assert language_family(["a.py", "b.py", "c.rs"]) == "python"
    assert language_family(["a.go", "b.rs"]) == "go"  # tie → alphabetical
    assert language_family(["README", "LICENSE"]) is None
    assert language_family(["X.PY"]) == "python"  # case-insensitive
    assert LANGUAGE_FAMILY_OF_EXTENSION["ts"] == "js"


def test_emissions_capture_language():
    calls = [
        {
            "tool": "Bash",
            "input": {"command": "pytest -q"},
            "result": "tests/test_a.py:12: AssertionError in src/mod.py:40",
        }
    ]
    (em,) = emissions_from_calls(calls)
    assert em.language == "python"
