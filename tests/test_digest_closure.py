"""Acceptance: the digest-closure audit (docs/DIGEST-CLOSURE.md).

Closure is a total function of the `ctx q` type signature, and the
single-refinement-boundary theorem is a structural invariant of the kind
graph — pinned here so neither can silently drift.
"""

import ctx.query as q

# Register the fact-plane stages if present (fail-open, same as run_query).
try:  # pragma: no cover - depends on optional facts module
    import ctx.facts  # noqa: F401
except Exception:
    pass


# The audit table (docs/DIGEST-CLOSURE.md), pinned. Every core `ctx q` stage's
# closure class is derived from (input_kinds, output_kind) alone.
CORE_CLOSURE = {
    # source: opens a pipeline, lifts fact store / repo → the `sites` representation
    "refs": "source",
    "callers": "source",
    "callees": "source",
    "impact": "source",
    "search": "source",
    # M-K2/M-K3 sources: the file-set and stored-record planes (SUBSTRATE.md)
    "corpus": "source",
    "records": "source",
    # closed: representation → representation, digest-rate, no byte rehydration
    "files": "closed",
    "group": "closed",
    "top": "closed",
    "where": "closed",
    "count": "closed",
    "distinct": "closed",
    "histogram": "closed",
    # materialize: emits the terminal `text` payload — the priced refinement boundary
    "get": "materialize",
    "outline": "materialize",
}


def test_core_stage_closure_classes_are_pinned():
    for name, expected in CORE_CLOSURE.items():
        assert name in q.STAGES, f"stage {name!r} vanished from the registry"
        assert q.STAGES[name].closure == expected, (
            f"{name}: closure drifted to {q.STAGES[name].closure!r}, expected {expected!r}"
        )


def test_design_law_materializers_emit_terminal_kind():
    """Byte materialization MUST emit the terminal `text` kind (so it cannot be
    fed back into a representation-producing stage)."""
    for name, st in q.STAGES.items():
        if st.closure == "materialize":
            assert st.output_kind == q.TERMINAL_KIND, (
                f"{name} materializes but does not emit {q.TERMINAL_KIND!r}"
            )


def test_single_refinement_boundary_theorem():
    """No stage maps the terminal kind back to a *materializer input* kind, so a
    pipeline materializes bytes at most once and only terminally."""
    materializer_inputs = set()
    for st in q.STAGES.values():
        if st.closure == "materialize":
            materializer_inputs.update(st.input_kinds)
    # get←sites, outline←files ⇒ the only doors back to byte-rate.
    assert materializer_inputs <= {"sites", "files"}
    for name, st in q.STAGES.items():
        if q.TERMINAL_KIND in st.input_kinds:
            assert st.output_kind not in materializer_inputs, (
                f"{name} maps {q.TERMINAL_KIND} back to a materializer input "
                f"({st.output_kind}) — re-materialization would break the theorem"
            )


def test_representation_kinds_partition():
    assert q.TERMINAL_KIND not in q.REPRESENTATION_KINDS
    assert set(q.REPRESENTATION_KINDS) | {q.TERMINAL_KIND} == set(q.KINDS)


def test_pipeline_closure_verdicts():
    assert q.pipeline_closure(["refs", "files"]) == "closed"
    assert q.pipeline_closure(["search", "group", "top", "count"]) == "closed"
    assert q.pipeline_closure(["refs", "files", "outline"]) == "refinement@3:outline"
    assert q.pipeline_closure(["search", "get"]) == "refinement@2:get"
    # the closed prefix before the boundary is real: everything up to `get` is
    # digest-rate, so a longer closed prefix still reports the same boundary index.
    assert q.pipeline_closure(["search", "where", "top", "get"]) == "refinement@4:get"


def test_source_stages_open_the_pipeline():
    for name, st in q.STAGES.items():
        if st.closure == "source":
            assert st.input_kinds == (), f"{name} is a source but consumes a stream"
