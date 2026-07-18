"""Acceptance: ctx debt (declared omission for decisions) and
deliverable-level scorecard metrics (measure the artifact, not just the wire)."""

import json
import subprocess

import pytest


@pytest.fixture()
def ws(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    (d / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "."], cwd=d, check=True)
    return d


def test_debt_add_list_resolve(ws, capsys):
    from ctx.cli import main

    rc = main(["--workspace", str(ws), "debt", "add",
               "skip caching layer for map — revisit past 5k files",
               "--ref", "repo:src/ctx/repomap.py:455"])
    assert rc == 0
    eid = capsys.readouterr().out.split(":")[1].strip()

    main(["--workspace", str(ws), "debt", "list"])
    out = capsys.readouterr().out
    assert "1 open · 0 resolved" in out
    assert "skip caching layer" in out and "repomap.py:455" in out

    assert main(["--workspace", str(ws), "debt", "resolve", eid]) == 0
    capsys.readouterr()
    main(["--workspace", str(ws), "debt", "list"])
    out = capsys.readouterr().out
    assert "0 open · 1 resolved" in out
    # History is append-only: the ledger file keeps both operations.
    lines = (ws / ".ctx-debt.jsonl").read_text().splitlines()
    assert len(lines) == 2

    assert main(["--workspace", str(ws), "debt", "resolve", "nonexist99"]) == 1


def test_debt_is_idempotent(ws):
    from ctx.debt import add, outstanding

    a = add(ws, "same note", ref="repo:x.py:1")
    b = add(ws, "same  note", ref="repo:x.py:1")  # whitespace-normalized
    assert a == b
    assert len(outstanding(ws)) == 1


def test_debt_empty_note_rejected(ws):
    from ctx.debt import add

    with pytest.raises(ValueError):
        add(ws, "   ")


def test_deliverable_metrics_from_git(ws):
    from ctx.scorecard import attach_deliverable, summary_line

    (ws / "a.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=ws, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "base"],
        cwd=ws, check=True,
    )
    (ws / "a.py").write_text("x = 1\nz = 3\nw = 4\n", encoding="utf-8")  # +2 -1
    (ws / "new.py").write_text("n = 1\n", encoding="utf-8")  # untracked

    sc = {"requests": 1, "est_cost_usd": 0.1, "tokens": {"output": 10},
          "output_per_request": 10.0, "cache_hit_pct": 90.0,
          "invalidations": 0, "cold_prefix_tok": 0}
    attach_deliverable(sc, ws)
    d = sc["deliverable"]
    assert d == {"insertions": 2, "deletions": 1, "files_changed": 1,
                 "files_new": 1, "lines_new": 1}
    assert "Δcode +2/-1 in 1+1 files" in summary_line(sc)
    json.dumps(sc)  # history-serializable


def test_deliverable_fails_open_without_git(tmp_path):
    from ctx.scorecard import attach_deliverable

    d = tmp_path / "nogit"
    d.mkdir()
    sc = {"requests": 1}
    attach_deliverable(sc, d)  # git diff HEAD fails (no repo/HEAD)
    assert "deliverable" not in sc or sc["deliverable"]["files_changed"] == 0
