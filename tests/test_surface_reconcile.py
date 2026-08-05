"""Acceptance: Phase 5 — automatic reconciliation (shadow by default).

reveal-on-intent, hide-unused-after-phase, the governing law (never hide a
required or kernel family), shadow vs enforce, and the paired referee."""

import json
from pathlib import Path

import pytest

from ctx import surface_reconcile as sr
from ctx.surface_gateway import load_state


def _sig(**kw):
    base = dict(revealed={"harness"}, current_phase="explore", intent_text="",
                usage_since_reveal={}, family_tokens={}, required_families=set(),
                available_families=set())
    base.update(kw)
    return sr.Signals(**base)


def test_reveal_on_intent():
    acts = sr.reconcile(_sig(intent_text="open a pull request",
                             available_families={"harness", "remote-source-control"}))
    assert [(a.op, a.family) for a in acts] == [("reveal", "remote-source-control")]


def test_intent_reveal_skips_unavailable_family():
    # deploy intent but no deployment server configured here → no reveal
    acts = sr.reconcile(_sig(intent_text="deploy to prod",
                             available_families={"harness", "repository"}))
    assert acts == []


def test_hide_unused_after_phase():
    acts = sr.reconcile(_sig(
        revealed={"harness", "remote-source-control"}, current_phase="explore",
        usage_since_reveal={"remote-source-control": 0},
        family_tokens={"remote-source-control": 2000}))
    assert [(a.op, a.family) for a in acts] == [("hide", "remote-source-control")]


def test_used_family_is_not_hidden():
    acts = sr.reconcile(_sig(
        revealed={"harness", "remote-source-control"}, current_phase="explore",
        usage_since_reveal={"remote-source-control": 4}))
    assert acts == []


def test_governing_law_required_family_never_hidden():
    acts = sr.reconcile(_sig(
        revealed={"harness", "remote-source-control", "testing"},
        current_phase="explore",
        usage_since_reveal={"remote-source-control": 0, "testing": 0},
        family_tokens={"remote-source-control": 2000, "testing": 500},
        required_families={"remote-source-control"}))
    fams = {a.family for a in acts}
    assert "remote-source-control" not in fams   # protected
    assert "testing" in fams                     # unprotected, still hidden


def test_kernel_never_hidden():
    acts = sr.reconcile(_sig(revealed={"harness"}, current_phase="deliver",
                             family_tokens={"harness": 100}))
    assert acts == []


def test_hides_are_ordered_high_cost_first():
    acts = sr.reconcile(_sig(
        revealed={"harness", "remote-source-control", "collaboration"},
        current_phase="explore",
        usage_since_reveal={"remote-source-control": 0, "collaboration": 0},
        family_tokens={"remote-source-control": 3000, "collaboration": 200}))
    hides = [a.family for a in acts if a.op == "hide"]
    assert hides[0] == "remote-source-control"   # bigger first


def test_infer_phase():
    assert sr.infer_phase({"Edit": 5, "Read": 3}) == "edit"
    assert sr.infer_phase({"pytest": 2, "Edit": 1}) == "verify"
    assert sr.infer_phase({"Read": 3}) == "explore"


# ------------------------------------------------------- workspace + ledger
@pytest.fixture()
def ws(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "ctx.toml").write_text("version=1\n", encoding="utf-8")
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "github": {"command": "gh-mcp"},
        "ctx-harness": {"command": "ctx", "args": ["mcp"]}}}), encoding="utf-8")
    (root / ".ctx-surface").mkdir()
    (root / ".ctx-surface" / "gateway-state.json").write_text(
        json.dumps({"revealed": ["harness", "remote-source-control"]}), encoding="utf-8")
    return root


def test_shadow_does_not_mutate_state(ws):
    rep = sr.run_reconcile(ws, phase="explore", enforce=False)
    assert rep["enforced"] is False
    assert any(a["op"] == "hide" for a in rep["actions"])
    assert "remote-source-control" in load_state(ws)   # unchanged
    assert (ws / sr.SHADOW_LEDGER).is_file()             # logged


def test_enforce_applies_to_gateway_state(ws):
    sr.run_reconcile(ws, phase="explore", enforce=True)
    assert "remote-source-control" not in load_state(ws)  # hidden
    assert "harness" in load_state(ws)                    # kernel kept


def test_referee_scores_shadowed_hides(ws):
    sr.run_reconcile(ws, phase="explore", enforce=False)   # logs a hide
    rep = sr.referee(ws)
    # no github usage recorded → the hide was safe → promotable
    assert rep["unsafe"] == 0 and rep["safe"] >= 1
    assert "remote-source-control" in rep["promotable"]
    assert rep["verdict"] == "promote"


def test_referee_flags_unsafe_hide(ws):
    # an EARLIER shadow recommended hiding remote-source-control (it was unused
    # then); LATER the session actually used github → that hide was unsafe.
    (ws / sr.SHADOW_LEDGER).write_text(
        json.dumps({"schema": "ctx.surface-reconcile/v1", "op": "hide",
                    "family": "remote-source-control", "reason": "unused",
                    "tokens": 2000, "phase": "explore", "enforced": False}) + "\n",
        encoding="utf-8")
    proxy = ws / ".ctx-session-reads" / "proxy" / "s"
    proxy.mkdir(parents=True)
    (proxy / "wire.jsonl").write_text(
        json.dumps({"tools": {"mcp__github__search_code": 3}}), encoding="utf-8")
    rep = sr.referee(ws)
    assert rep["unsafe"] >= 1 and rep["verdict"] == "hold"


# ------------------------------------------- the boundary is in the keyword
def _intent(text: str) -> list[str]:
    sig = sr.Signals(
        revealed=set(), current_phase="explore", intent_text=text,
        usage_since_reveal={}, family_tokens={}, required_families=set(),
    )
    return [a.family for a in sr.reconcile(sig) if a.op == "reveal"]


def test_padded_keyword_does_not_match_inside_a_word():
    """A reveal is a cost decision: it puts a whole capability family into
    context. `" pr "` is padded precisely so it cannot fire on "sprint" --
    the matcher used to .strip() that padding off before the substring test,
    discarding the only thing that made the entry safe."""
    assert "remote-source-control" not in _intent(
        "we need to sprint through this backlog before the deadline"
    )
    assert "remote-source-control" not in _intent("the compression ratio")


def test_padded_keyword_still_matches_through_punctuation():
    """The other direction, which a bare f" {text} " pad got wrong: the
    boundary around a real mention is usually punctuation, not a space, so
    `" pr "` missed the very phrasings it exists to catch."""
    for text in ("open a PR.", "review the pr, please", "(pr)", "a PR!"):
        assert "remote-source-control" in _intent(text), text


def test_unpadded_keyword_is_still_a_deliberate_prefix():
    """Not every entry wants a whole word. `vulnerab` and `infra` are
    prefixes on purpose; the fix must not turn every trigger into \\b...\\b."""
    assert "semantic-analysis" in _intent("check for a vulnerability here")
    assert "cloud" in _intent("set up the infrastructure")


def test_multiword_and_hyphenated_keywords_survive_normalization():
    assert "semantic-analysis" in _intent("run a source-to-sink analysis")
    assert "semantic-analysis" in _intent("source to sink, please")
    assert "remote-source-control" in _intent("please open a pull request")


def test_every_padded_trigger_is_boundary_safe():
    """The invariant behind the fix: for each padded keyword, embedding it in
    a longer word must not fire. Asserted over the whole table so a new
    padded entry inherits the guarantee instead of re-earning it."""
    for fam, kws in sr.INTENT_TRIGGERS.items():
        for kw in kws:
            if kw == kw.strip():
                continue  # unpadded: a prefix match is the declared intent
            bare = kw.strip()
            assert fam not in _intent(f"xx{bare}yy"), f"{kw!r} matched inside a word"
            assert fam in _intent(f"a {bare} b"), f"{kw!r} stopped matching at all"
