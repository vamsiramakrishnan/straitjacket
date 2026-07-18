"""Acceptance: session scorecard (mechanisms D+F) — cache economics,
timing split, and effort mix computed from wire ground truth."""

import json

import pytest


def _wire_record(seq, *, msgs, cre, read, inp=2, out=100, model="claude-sonnet-5",
                 tools=None, ttfb=1000.0, total=2000.0):
    return {
        "seq": seq,
        "path": "/v1/messages",
        "status": 200,
        "req_bytes": 1000,
        "messages": msgs,
        "model": model,
        "blocks": {},
        "tools": tools or {},
        "tool_result_top": [],
        "usage": {
            "input_tokens": inp,
            "output_tokens": out,
            "cache_read_input_tokens": read,
            "cache_creation_input_tokens": cre,
        },
        "ms": {"connect": 0.0, "ttfb": ttfb, "total": total},
        "reused_conn": seq > 1,
    }


@pytest.fixture()
def proxy_dir(tmp_path):
    d = tmp_path / "proxy"
    d.mkdir()
    records = [
        # side-channel (title gen): no usage → ignored entirely
        {"seq": 1, "path": "/v1/messages", "status": 200, "usage": {}},
        # cold prefix write: creates 50k reading nothing
        _wire_record(2, msgs=2, cre=50_000, read=0, out=120,
                     tools={"Bash": 1}),
        # normal suffix growth
        _wire_record(3, msgs=4, cre=400, read=50_000, out=200,
                     tools={"Bash": 1, "Read": 1}),
        # true invalidation: multi-message thread, read regressed
        _wire_record(4, msgs=6, cre=8_000, read=30_000, out=150,
                     tools={"Edit": 2}),
        # fork/title thread: single message, small read — NOT an invalidation
        _wire_record(5, msgs=1, cre=0, read=6_000, out=50),
        # non-messages path → ignored
        {"seq": 6, "path": "/v1/complete", "status": 200,
         "usage": {"output_tokens": 999}},
    ]
    (d / "wire.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8"
    )
    return d


def test_scorecard_token_and_cache_accounting(proxy_dir):
    from ctx.scorecard import compute_scorecard

    sc = compute_scorecard(proxy_dir)
    assert sc["requests"] == 4  # usage-bearing /messages exchanges only
    assert sc["tokens"]["creation"] == 58_400
    assert sc["tokens"]["read"] == 86_000
    assert sc["tokens"]["output"] == 520
    assert sc["cold_prefix_tok"] == 50_000  # one-time, separated out
    assert sc["invalidations"] == 1  # only the true regression counts
    assert sc["output_per_request"] == 130.0


def test_scorecard_effort_mix_and_timing(proxy_dir):
    from ctx.scorecard import compute_scorecard

    sc = compute_scorecard(proxy_dir)
    assert sc["tools"] == {"Bash": 2, "Edit": 2, "Read": 1}
    assert sc["edit_share_pct"] == 40.0  # 2 of 5
    assert sc["timing_s"]["ttfb"] == 4.0
    assert sc["timing_s"]["gen"] == 4.0
    assert sc["per_model"]["claude-sonnet-5"]["out"] == 520


def test_scorecard_none_without_observations(tmp_path):
    from ctx.scorecard import compute_scorecard

    assert compute_scorecard(tmp_path) is None
    (tmp_path / "wire.jsonl").write_text("", encoding="utf-8")
    assert compute_scorecard(tmp_path) is None


def test_render_and_summary_are_bounded_and_informative(proxy_dir):
    from ctx.scorecard import compute_scorecard, render_scorecard, summary_line

    sc = compute_scorecard(proxy_dir)
    text = render_scorecard(sc)
    assert "cold-prefix 50,000" in text
    assert "invalidations 1" in text
    assert "edit-share 40.0%" in text
    assert len(text.splitlines()) <= 10  # bounded by design
    one = summary_line(sc)
    assert "\n" not in one
    assert "cold-prefix" in one


def test_history_appends(tmp_path, proxy_dir):
    from ctx.scorecard import append_history, compute_scorecard

    sc = compute_scorecard(proxy_dir)
    append_history(tmp_path, sc)
    append_history(tmp_path, sc)
    lines = (tmp_path / ".ctx-session-reads" / "scorecards.jsonl").read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["schema"] == "ctx.scorecard/v1"


def test_cli_stats_session(tmp_path, proxy_dir, capsys, monkeypatch):
    import shutil

    from ctx.cli import main

    ws = tmp_path / "proj"
    ws.mkdir()
    (ws / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    rc = main(["--workspace", str(ws), "stats", "--session"])
    assert rc == 1  # no observations yet
    dest = ws / ".ctx-session-reads" / "proxy"
    dest.mkdir(parents=True)
    shutil.copy(proxy_dir / "wire.jsonl", dest / "wire.jsonl")
    rc = main(["--workspace", str(ws), "stats", "--session"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "session scorecard" in out
    assert "invalidations 1" in out
