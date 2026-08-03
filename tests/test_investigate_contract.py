"""EvidenceGraph v2 relations (additive) + the investigate contract.

The v1 byte-compatibility property is the load-bearing one: a graph with
no relations must serialize byte-identically to before the field existed,
so every pinned golden and every extraction cache key is unchanged.
"""

import pytest


def _graph(**kw):
    from ctx.evidence import EvidenceGraph

    base = dict(
        family="investigate",
        profile_version="investigate/v1",
        outcome="fail",
        aggregate={"candidates": 1},
        items=(),
        artifacts={},
        coverage={"parsed": 1, "total_estimate": 1, "complete": True},
    )
    base.update(kw)
    return EvidenceGraph(**base)


def test_empty_relations_serialize_as_v1_byte_identical():
    from ctx.evidence import to_canonical_bytes

    g = _graph()
    payload = to_canonical_bytes(g)
    assert b'"schema":"ctx.evidence-graph/v1"' in payload
    assert b"relations" not in payload


def test_relations_flip_schema_to_v2():
    from ctx.evidence import graph_id, to_canonical_bytes

    g1 = _graph()
    g2 = _graph(relations=(("t1", "frame_of", "auth.py::from_request"),))
    assert b'"schema":"ctx.evidence-graph/v2"' in to_canonical_bytes(g2)
    assert b'"relations":[["t1","frame_of","auth.py::from_request"]]' in to_canonical_bytes(g2)
    assert graph_id(g1) != graph_id(g2)


def test_relation_vocabulary_is_closed():
    with pytest.raises(ValueError, match="closed vocabulary"):
        _graph(relations=(("a", "invented_relation", "b"),))


def test_investigate_contract_loads_and_shapes():
    from ctx.contracts import CENSUS_CLASS, contract_for_family

    c = contract_for_family("investigate")
    assert c.family == "investigate"
    fail = c.for_outcome("fail")
    assert CENSUS_CLASS in fail.required
    assert "counterevidence" in fail.required
    assert "coverage_attestation" in fail.required
    # No candidates is a legitimate pass: the census class (defined only
    # over a non-empty item set) must not be required there.
    assert CENSUS_CLASS not in c.for_outcome("pass").required
    assert "counterevidence" in c.for_outcome("pass").required
    assert c.loss_severity(CENSUS_CLASS) == "catastrophic"
    assert c.rendering.evidence_floor_tokens <= c.rendering.hard_ceiling_tokens


def test_contract_receipt_full_on_investigation_graph():
    from ctx.contracts import contract_for_family, validate_selection
    from ctx.evidence import EvidenceItem

    item = EvidenceItem(
        id="auth.py::from_request",
        kind="conclusion_candidate",
        severity="error",
        summary="1 failing test locates in changed symbol from_request",
        failure_class="ValueError",
        location="auth.py:6",
    )
    g = _graph(items=(item,), relations=(("t1", "frame_of", item.id),))
    receipt = validate_selection(
        [item.id],
        {
            "aggregate_counts",
            "complete_identity_census",
            "location",
            "one_line_summary",
            "counterevidence",
            "coverage_attestation",
        },
        contract_for_family("investigate"),
        g,
        # `counterevidence` is a PLAN-layer fact: whether a run produced any
        # is about which nodes ran, not about the extracted graph. It now
        # takes an explicit attestation from the layer that can witness it --
        # naming it in included_fields is no longer enough, because a public
        # seam that trusts a name is not enforcing anything.
        attested={"counterevidence"},
    )
    assert receipt.required_fraction == 1.0
    assert receipt.attested_complete


def test_naming_counterevidence_without_witnessing_it_is_not_enough():
    """The defect a bug bash found: `_class_present`'s catch-all returned
    True, so a class marked required with loss_severities = "major" --
    because "its absence is exactly the anchoring failure the plan exists to
    prevent" -- was satisfied by the word appearing in a set literal."""
    from ctx.contracts import contract_for_family, validate_selection
    from ctx.evidence import EvidenceItem

    item = EvidenceItem(
        id="tests/test_auth.py::test_login",
        kind="failing_test",
        severity="error",
        summary="1 failing test",
        failure_class="ValueError",
        location="auth.py:6",
    )
    g = _graph(items=(item,), relations=(("t1", "frame_of", item.id),))
    receipt = validate_selection(
        [item.id],
        {
            "aggregate_counts",
            "complete_identity_census",
            "location",
            "one_line_summary",
            "counterevidence",
            "coverage_attestation",
        },
        contract_for_family("investigate"),
        g,
    )
    assert receipt.required_fraction < 1.0, (
        "a graph with no counterevidence must not report full coverage"
    )
    assert receipt.required_fields_present == receipt.required_fields_total - 1


def test_an_unknown_required_class_is_named_not_counted():
    """Forward compatibility is a reason to TOLERATE an unknown class, not a
    reason to report it as delivered. An unverifiable requirement is a fact
    about the CONTRACT, so it is named on the receipt rather than quietly
    lowering a fraction nobody can explain."""
    from ctx.contracts import _CHECKED_CLASSES

    assert "counterevidence" in _CHECKED_CLASSES
    assert "coverage_attestation" in _CHECKED_CLASSES
    assert "something_invented_later" not in _CHECKED_CLASSES
