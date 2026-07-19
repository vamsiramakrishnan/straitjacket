"""Acceptance: ctx replay --outcomes — deterministic evidence_outcome/v1
events from recorded transcripts (read-only, censored disclosed)."""

import json

from ctx.replay import parse_transcript, render_outcomes, session_outcomes


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


def _session(tmp_path):
    digest = (
        "[ctx run:abc123def456 profile=pytest/v2]\n"
        "failing tests (census):\n"
        "  1. tests/test_x.py::test_edge  tests/test_x.py FAILED\n"
        "next:\n  ctx get run:abc123def456#stdout --lines 10:14\n"
    )
    entries = [
        _tool_use("Bash", {"command": "ctx run -- pytest -q"}, "a"),
        _tool_result("a", digest),
        # lands the emitted handle with an addressed slice
        _tool_use("Bash", {"command": "ctx get run:abc123def456#stdout --lines 10:14"}, "b"),
        _tool_result("b", "10: assert x == 1"),
        # narrows to the surfaced failing test id
        _tool_use("Bash", {"command": "pytest tests/test_x.py::test_edge -x"}, "c"),
        _tool_result("c", "1 failed"),
        # an emission whose window is still open at transcript end → censored
        _tool_use("Bash", {"command": "pytest tests/other.py"}, "d"),
        _tool_result("d", "tests/other.py::test_late FAILED"),
    ]
    p = tmp_path / "s.jsonl"
    _write(p, entries)
    return p


def test_outcomes_from_transcript(tmp_path):
    p = _session(tmp_path)
    events = session_outcomes(p)
    assert events, "expected attributable events"
    by_op = {}
    for e in events:
        by_op.setdefault(e.operator, []).append(e)
    (pytest_ev,) = by_op["profile:pytest/v2"]
    assert "retrieved" in pytest_ev.outcomes and "landed" in pytest_ev.outcomes
    assert "exact_handle" in pytest_ev.attribution_reasons
    assert pytest_ev.attribution_confidence == 1.0
    assert pytest_ev.investigation_id is None  # never invented for old sessions
    # The trailing emission's window never closed: censored, not negative.
    last = [e for e in events if "test_late" in " ".join(e.evidence_ids)]
    assert last and all(e.censored for e in last)
    assert all("abandoned" not in e.outcomes for e in last)


def test_outcomes_deterministic_and_read_only(tmp_path):
    p = _session(tmp_path)
    before = p.read_bytes()
    e1, e2 = session_outcomes(p), session_outcomes(p)
    assert [x.payload() for x in e1] == [x.payload() for x in e2]
    assert p.read_bytes() == before  # replay never mutates the transcript


def test_scoreboard_shape(tmp_path):
    events = session_outcomes(_session(tmp_path))
    board = render_outcomes(events)
    assert "Evidence outcomes" in board
    assert "profile:pytest/v2" in board
    assert "censored" in board
    assert "attributable:" in board
    # JSON payloads expose the underlying event fields, not just rates.
    payloads = [e.payload() for e in events]
    assert all("attribution_reasons" in p and "censored" in p for p in payloads)


def test_transcript_parses_all_calls(tmp_path):
    p = _session(tmp_path)
    assert len(parse_transcript(p)) == 4
