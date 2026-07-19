"""Acceptance: evidence regret — the measured rate–distortion frontier gap
(docs/THEORY.md). Pins the metric's formal properties: the one-sided bound
direction, the facts-bearing/unattributed partition, per-profile aggregation,
determinism, and the hop-price model."""

import json

from ctx.replay import (
    _HOP_CONTEXT_LINES,
    _HOP_SCAFFOLD_TOK,
    _hop_cost,
    render_regret,
    simulate_session,
)


def _tool_use(name, inp, uid):
    return {
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "id": uid, "name": name, "input": inp}]},
    }


def _tool_result(uid, text):
    return {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "tool_use_id": uid, "content": text}]},
    }


def _write(path, entries):
    path.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")


NEEDLE = "retry_backoff_ms = 4172  # tuned for apac"


def _transcript(tmp_path):
    """Three regimes in one session: an inline fact (pytest keeps its failure
    coordinate), a one-hop fact (text/v1 head/tail drops the middle needle),
    and a fact-free flood (unattributed by design)."""
    pytest_raw = (
        "\n".join(f"tests/test_mod.py::test_case_{i} PASSED" for i in range(200))
        + "\ntests/test_x.py::test_boundary FAILED\n"
        "=================================== FAILURES ===============\n"
        "tests/test_x.py:42: AssertionError\n"
        "1 failed, 200 passed in 1.23s\n"
    )
    text_lines = [
        f"processed batch {i:04d} shard={i % 7} elapsed={i % 9}ms status=done"
        for i in range(600)
    ]
    text_lines[300] = NEEDLE
    noise_raw = "\n".join(f"copied object {i} ok" for i in range(500))
    entries = [
        # A: pytest flood; the failing node-id is reused downstream (the
        # canonical re-run-the-failure move). pytest/v2's census keeps the
        # node-id inline by contract.
        _tool_use("Bash", {"command": "pytest -v"}, "a"),
        _tool_result("a", pytest_raw),
        _tool_use("Bash", {"command": "pytest tests/test_x.py::test_boundary -x"}, "a2"),
        _tool_result("a2", "ok"),
        # B: uniform text flood; the middle needle is reused in an Edit
        _tool_use("Bash", {"command": "python3 gen.py"}, "b"),
        _tool_result("b", "\n".join(text_lines)),
        _tool_use("Edit", {"file_path": "/x/cfg.py", "old_string": NEEDLE}, "b2"),
        _tool_result("b2", "edited"),
        # C: flood nothing downstream ever uses — unattributed, never regret
        _tool_use("Bash", {"command": "python3 sync.py"}, "c"),
        _tool_result("c", noise_raw),
    ]
    p = tmp_path / "session.jsonl"
    _write(p, entries)
    return p


def test_regret_partition_and_bound_direction(tmp_path):
    r = simulate_session(_transcript(tmp_path))
    buckets = r["regret_by_profile"]

    # pytest keeps its failure evidence inline: facts scored, no hops.
    py = next(b for k, b in buckets.items() if k.startswith("pytest/"))
    assert py["facts"] >= 1 and py["inline"] >= 1 and py["hops"] == 0

    # text/v1 drops the middle needle: exactly the one-hop regime.
    tx = buckets["text/v1"]
    assert tx["facts"] == 1 and tx["hops"] == 1 and tx["inline"] == 0

    for b in buckets.values():
        # Internal consistency of the derived fields. naive-R is computed
        # over raw-known calls only (harnessed calls have no counterfactual).
        assert b["regret_tok"] == b["actual_tok"] - b["oracle_tok"]
        assert b["naive_regret_tok"] == b["naive_tok"] - b["naive_oracle_tok"]
        assert b["naive_calls"] == b["calls"]  # this transcript is all-raw
        # Flood regimes: the harness closes gap vs naive, never widens it.
        assert 0 <= b["regret_tok"] <= b["naive_regret_tok"]

    # C's flood is fact-free: charged to the unattributed bucket, not to R.
    assert r["unattributed_calls"] >= 1
    assert r["unattributed_digest_tok"] > 0
    total_regret_calls = sum(b["calls"] for b in buckets.values())
    assert total_regret_calls == 2  # A and B only


def test_regret_is_deterministic(tmp_path):
    p = _transcript(tmp_path)
    r1, r2 = simulate_session(p), simulate_session(p)
    assert r1["regret_by_profile"] == r2["regret_by_profile"]
    assert r1["unattributed_digest_tok"] == r2["unattributed_digest_tok"]


def test_hop_cost_model():
    raw = "\n".join(f"line {i}" for i in range(10))
    # Fact mid-file: scaffold + a (2·context+1)-line window.
    c_mid = _hop_cost("line 5", raw)
    assert c_mid > _HOP_SCAFFOLD_TOK
    window = "\n".join(f"line {i}" for i in range(5 - _HOP_CONTEXT_LINES, 5 + _HOP_CONTEXT_LINES + 1))
    from ctx.textutil import estimate_tokens

    assert c_mid == _HOP_SCAFFOLD_TOK + estimate_tokens(len(window.encode()))
    # Fact on the first line: window clamps at the start, never negative.
    assert _hop_cost("line 0", raw) >= _HOP_SCAFFOLD_TOK
    # No raw bytes (already-harnessed): declared floor — scaffold + the fact.
    assert _hop_cost("some fact text", None) == _HOP_SCAFFOLD_TOK + estimate_tokens(
        len(b"some fact text")
    )


def test_render_regret_scoreboard(tmp_path):
    r = simulate_session(_transcript(tmp_path))
    out = render_regret([r])
    assert "evidence regret" in out
    assert "text/v1" in out
    assert "frontier" in out
    assert "UPPER bound" in out
    assert "unattributed digest spend" in out
    # Aggregation across sessions doubles the counts, deterministically.
    out2 = render_regret([r, r])
    assert "of the naive gap closed" in out2


def test_harnessed_rows_have_no_naive_counterfactual(tmp_path):
    """Already-harnessed results: raw stayed in the store, so naive-R is
    unknowable — the bucket must carry naive_calls == 0 and render '—',
    never a fake self-comparison."""
    digest = (
        "[ctx run:abcdef123456 profile=pytest/v1]\n"
        "failing tests (census):\n"
        "  1. tests/test_y.py::test_edge\n"
    )
    entries = [
        _tool_use("Bash", {"command": "ctx run -- pytest -q"}, "h"),
        _tool_result("h", digest),
        _tool_use("Bash", {"command": "pytest tests/test_y.py::test_edge -x"}, "h2"),
        _tool_result("h2", "ok"),
    ]
    p = tmp_path / "h.jsonl"
    _write(p, entries)
    r = simulate_session(p)
    b = r["regret_by_profile"]["pytest/v1"]
    assert b["naive_calls"] == 0 and b["inline"] >= 1
    assert b["known_actual_tok"] == 0
    out = render_regret([r])
    assert "—" in out
    assert "naive comparison" not in out  # no raw-known calls at all


def test_no_facts_no_scoreboard_noise(tmp_path):
    entries = [
        _tool_use("Bash", {"command": "ls"}, "x"),
        _tool_result("x", "a.py\nb.py\n"),
    ]
    p = tmp_path / "s.jsonl"
    _write(p, entries)
    r = simulate_session(p)
    assert r["regret_by_profile"] == {}
    assert "nothing scoreable" in render_regret([r])
