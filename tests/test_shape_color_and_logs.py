"""Coloured pastes are claimed; bracketed-prefix logs are logs; quoted comments
do not veto a real table."""

from __future__ import annotations

import csv
import io
import os
import random
import re
import subprocess

import pytest
from conftest import make_store, make_ws

from ctx.intake import intake_bytes


@pytest.fixture()
def env(state_home, workspace_dir):
    ws = make_ws(workspace_dir)
    return ws, make_store(ws)


def take(env, text: str):
    ws, store = env
    return intake_bytes(store, ws, text.encode("utf-8"), label="paste")


# ------------------------------------------------------------------ item 1
@pytest.fixture()
def git_diff_color(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

    def git(*a):
        return subprocess.run(["git", *a], cwd=repo, env=env, check=True, capture_output=True, text=True).stdout

    git("init", "-q")
    for i in range(6):
        (repo / f"mod{i}.py").write_text("".join(f"line {k} of module {i}\n" for k in range(80)))
    git("add", ".")
    git("-c", "commit.gpgsign=false", "commit", "-qm", "init")
    for i in range(6):
        (repo / f"mod{i}.py").write_text("".join(f"line {k} of module {i}\n" if k % 4 else f"changed {k} in {i}\n" for k in range(80)))
    return git("-c", "color.ui=always", "diff", "--color=always")


def test_real_git_diff_color_output_is_claimed(env, git_diff_color):
    d = git_diff_color
    assert "\x1b[" in d and len(d) > 3600
    r = take(env, d)
    assert r.profile == "udiff/v1", r.digest
    assert "diff: 6 files" in r.digest and "\x1b" not in r.digest
    # without the `diff --git` lines too
    plain_headers = "".join(ln for ln in d.splitlines(True) if "diff --git" not in ln and "index " not in ln)
    r = take(env, plain_headers)
    assert r.profile == "udiff/v1" and "diff: 6 files" in r.digest


def _paint(lines: list[str], code: str) -> str:
    return "".join(f"\x1b[{code}m{ln}\x1b[0m\n" for ln in lines)


def test_coloured_python_and_node_traces_are_claimed(env):
    py = ["Traceback (most recent call last):"]
    for i in range(60):
        py += [f'  File "/app/svc/mod{i}.py", line {i + 1}, in handler{i}', f"    run_{i}()"]
    py.append("ValueError: bad input")
    r = take(env, _paint(py, "90"))
    assert r.profile == "trace/v1", r.digest
    assert 'exception: "ValueError: bad input"' in r.digest and "\x1b" not in r.digest
    js = ["TypeError: Cannot read properties of undefined (reading 'id')"]
    js += [f"    at handler{i} (/srv/app/src/h{i}.js:{i + 1}:5)" for i in range(80)]
    r = take(env, _paint(js, "31"))
    assert r.profile == "trace/v1", r.digest
    assert "Cannot read properties of undefined (reading 'id')" in r.digest and "\x1b" not in r.digest


# ------------------------------------------------------------------ item 2
def log_lines(kind: str, n: int = 300) -> str:
    if kind == "bracketed time, pipes":
        return "\n".join(f"[12:00:{i % 60:02d}] INFO | worker-{i % 3} | job {i} done" for i in range(n))
    if kind == "bracketed level, commas":
        return "\n".join(f"[INFO] [worker-{i % 3}] processed {i}, took {i % 9}ms" for i in range(n))
    if kind == "bracketed date, commas":
        return "\n".join(f"[2026-01-02 10:00:{i % 60:02d}] ERROR, worker-{i % 3}, failed job {i}, retrying" for i in range(n))
    if kind == "level only, semicolons":
        return "\n".join(f"[WARN] ;worker-{i % 3}; slow request {i}; {i * 7}ms" for i in range(n))
    raise AssertionError(kind)


@pytest.mark.parametrize("kind", ["bracketed time, pipes", "bracketed level, commas", "bracketed date, commas",
                                  "level only, semicolons"])
def test_bracketed_prefix_logs_are_not_tables(env, kind):
    assert take(env, log_lines(kind) + "\n").profile != "delimited/v1", kind


def test_interval_bins_are_values_not_a_log_prefix(env):
    text = "bin,count\n" + "".join(f"[{i}-{i + 5}),{(i * 37) % 97}\n" for i in range(300))
    assert take(env, text).profile == "delimited/v1"


def test_the_reviewers_bracketed_log_case_from_the_review_scratch_dir(env):
    # [12:00:xx] INFO | worker-n | job n done  (p_delim.py "pipe_log")
    text = "\n".join(f"[12:00:{i % 60:02d}] INFO | worker-{i % 3} | job {i} done" for i in range(300)) + "\n"
    assert take(env, text).profile in ("logtemplate/v1", "chunkmap/v1")


# ------------------------------------------------------------------ item 3
def comments_csv(share: float, comment) -> str:
    rnd = random.Random(3)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n", quoting=csv.QUOTE_NONNUMERIC)
    w.writerow(["id", "date", "score", "comment"])
    for i in range(400):
        w.writerow([i, f"2026-03-{1 + i % 28:02d}", i % 11, comment(i) if rnd.random() < share else f"fine number {i}"])
    return buf.getvalue()


@pytest.mark.parametrize("name,comment", [
    ("smiley", lambda i: f"works great :) thanks {i}"),
    ("regex", lambda i: f"use ^(a|b)+[0-9 pattern {i}"),
    ("open paren", lambda i: f"(see note {i}"),
    ("numbering", lambda i: f"1) first thing {i}"),
])
@pytest.mark.parametrize("share", [0.24, 0.26, 0.6, 0.95])
def test_quoted_comments_with_unbalanced_brackets_do_not_veto_a_typed_table(env, name, comment, share):
    assert take(env, comments_csv(share, comment)).profile == "delimited/v1", (name, share)


def test_unquoted_split_code_is_still_declined(env):
    text = "\n".join(f"draw_rect(ctx, {(i * 37) % 97}, {i}, 40)" for i in range(300)) + "\n"
    assert take(env, text).profile != "delimited/v1"
    text = "\n".join(f"({i}, 'a{i}', {i * 2})" for i in range(300)) + "\n"
    assert take(env, text).profile != "delimited/v1"
