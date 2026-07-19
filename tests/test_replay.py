"""Acceptance: ctx replay — deterministic session-history simulator (M-F)."""

import json

from ctx.replay import parse_transcript, render_report, simulate_session


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


def _write_transcript(path, entries):
    path.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")


def test_simulate_session_end_to_end(tmp_path):
    flood = "\n".join(f"INFO line {i} routine" for i in range(400))
    flood += "\ntests/test_x.py:42: AssertionError\n"
    entries = [
        _tool_use("Bash", {"command": "pytest -v 2>&1 | tail -200"}, "t1"),
        _tool_result("t1", flood),
        # the model reuses a coordinate from t1's output -> downstream fact
        _tool_use("Bash", {"command": "sed -n 40,44p tests/test_x.py:42"}, "t2"),
        _tool_result("t2", "ok"),
        # a Read whose content is later edited: read-path, never digested
        _tool_use("Read", {"file_path": "/x/src/mod.py"}, "t3"),
        _tool_result("t3", "def compute_total(rows):\n    return sum(rows)\n" * 5),
        _tool_use("Edit", {"file_path": "/x/src/mod.py", "old_string": "def compute_total(rows):"}, "t4"),
        _tool_result("t4", "edited"),
        # an already-harnessed result passes through untouched
        _tool_use("Bash", {"command": "ctx run -- pytest"}, "t5"),
        _tool_result("t5", "[ctx run:abcdef123456 profile=pytest/v1]\nsummary: ok"),
    ]
    p = tmp_path / "session.jsonl"
    _write_transcript(p, entries)

    calls = parse_transcript(p)
    assert len(calls) == 5

    r = simulate_session(p)
    assert r["bash"] == 3
    assert set(r["verdicts"]) <= {"allow", "deny", "force_ask", "rewrite"}
    assert r["already_harnessed_results"] == 1
    # The flood digests smaller than it was recorded.
    assert r["simulated_residency_tok"] < r["recorded_residency_tok"]
    # Read results are counted under read-path, not shape-digested.
    assert "read-path" in r["raw_tok_by_profile"]
    # The reused coordinate is a downstream fact, scored somewhere.
    assert r["facts_used_downstream"] >= 1
    assert r["slicer_commands"] == 1

    report = render_report([r], gaps=True)
    assert "residency: recorded" in report and "% saved" in report
    assert "== gaps (aggregate) ==" in report


def test_partial_trailing_line_tolerated(tmp_path):
    p = tmp_path / "live.jsonl"
    good = json.dumps(_tool_use("Bash", {"command": "ls"}, "a1"))
    p.write_text(good + '\n{"type": "assistant", "message": {"cont', encoding="utf-8")
    assert len(parse_transcript(p)) == 1
