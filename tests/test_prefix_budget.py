"""What the harness actually costs a session — asserted, not narrated.

`prefix_assets()` tracks five cache-keyed texts, and only some of them are
resident in every prompt. That distinction lived in a docstring, and the
docstring got misread: summing all five gives ~3,800 tokens and reads as "the
harness costs you 3,800 tokens a session", when the standing cost is ~708 and
the rest is paid only when the skill triggers or a subagent spawns — a 5.4×
overstatement of the project's own overhead, in the direction that makes it
look worse.

Prose cannot be asserted, so the split is now data (`RESIDENT_ASSETS` /
`DEFERRED_ASSETS`) and these tests hold it to the numbers we publish.
"""

from __future__ import annotations

from ctx.prefixassets import (
    DEFERRED_ASSETS,
    RESIDENT_ASSETS,
    budget_report,
    prefix_assets,
    resident_bytes,
)


def test_every_tracked_asset_is_classified():
    """A new prefix asset must be declared resident or deferred.

    Without this, the next asset lands unclassified and the budget silently
    stops describing reality — which is the failure this whole file exists to
    prevent, one step removed.
    """
    tracked = set(prefix_assets())
    classified = set(RESIDENT_ASSETS) | set(DEFERRED_ASSETS)
    assert tracked == classified, (
        f"unclassified prefix assets: {tracked - classified}; "
        f"stale classifications: {classified - tracked}"
    )


def test_the_two_classes_do_not_overlap():
    assert not (set(RESIDENT_ASSETS) & set(DEFERRED_ASSETS))


def test_the_skill_body_is_not_resident():
    """The single most important line in this file.

    The body is the largest tracked asset by 4×. Counting it as resident is
    exactly the error that produced the 3,800-token figure, and it is the one
    a reader is most likely to repeat.
    """
    assert "skill.ctx-harness.body" not in RESIDENT_ASSETS
    assert "skill.ctx-harness.body" in DEFERRED_ASSETS


def test_only_the_explorer_description_is_counted_as_resident():
    """The agent BODY travels with the subagent, not the parent prompt."""
    assert "agent.ctx-explorer" not in RESIDENT_ASSETS
    resident = resident_bytes()
    key = "agent.ctx-explorer(description)"
    assert key in resident, "the explorer description should be counted"
    full = len(prefix_assets()["agent.ctx-explorer"])
    assert resident[key] < full / 2, (
        "the whole agent file is being counted as resident again"
    )


def test_the_resident_budget_stays_under_a_thousand_tokens():
    """A budget, not an observation.

    ~708 tokens today. The ceiling is deliberately close: this is the number a
    user pays on every session in every repository forever, so growth in it
    should require someone to come here and raise the limit on purpose.
    """
    total = sum(resident_bytes().values())
    assert total // 4 < 1000, (
        f"resident prefix is now ~{total // 4} tokens. If that is intended, "
        "raise this ceiling deliberately and note the per-session cost in "
        "CHANGELOG.md — do not adjust it in passing."
    )


def test_the_report_states_both_classes():
    """The report is what a human reads; it must not be quotable out of
    context as a single scary number."""
    text = budget_report()
    assert "RESIDENT" in text and "DEFERRED" in text
    assert "loaded only when the skill triggers" in text
