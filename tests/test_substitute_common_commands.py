"""The replacement surface for common commands: retired, and why.

`head`, `sed -n 'A,Bp'`, `wc -l`, `cat <source>`, `find -name`, `ls -R`/`tree`
and the whole-suite pytest re-run used to be swapped for a "collapsed" ctx op
that was supposed to mean the same thing. Audited against the rule a
substitution has to meet -- the original's answer, or a precise declaration
of what was left out -- none did, so none is substituted. These tests pin the
retirement and, where it matters, show the defect that motivated it by
running both commands for real.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys

import pytest

from ctx.substitute import collapse

RETIRED = [
    "head -n 20 src/app.py", "head -5 a.py", "sed -n '10,20p' a.py", "sed -n 1,5p a.py",
    "wc -l a.py", "cat src/app.py", "cat handlers.ts", "find . -name '*.py'",
    "find src -name '*.ts' -type f", "ls -R", "ls -laR src", "tree", "tree src",
    "pytest", "python -m pytest",
]


@pytest.mark.parametrize("command", RETIRED)
def test_no_common_command_is_substituted(command):
    assert collapse(command, failure_available=True, symbols_resolvable=True) is None


@pytest.mark.parametrize("command", RETIRED)
def test_the_hook_runs_the_command_itself(command, tmp_path):
    """Whatever the guard does with these, the program that runs is the
    caller's own: either untouched, or wrapped whole in `ctx run`."""
    from ctx.hook import classify

    d = classify({"tool_name": "Bash", "cwd": str(tmp_path),
                  "tool_input": {"command": command}})
    rewrite = (d.get("rewrite") or {}).get("updatedInput", {}).get("command")
    if rewrite is None:
        return
    argv = shlex.split(rewrite)
    assert argv[:2] == ["ctx", "run"], rewrite
    inner = shlex.split(argv[-1]) if "--shell" in argv else argv[argv.index("--") + 1:]
    assert inner == shlex.split(command), rewrite


def _ctx(*args, cwd, env):
    return subprocess.run([sys.executable, "-m", "ctx", *args], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=60)


def test_why_head_was_retired_repo_paths_ignore_the_shells_cwd(tmp_path):
    """From a subdirectory, `head f.txt` reads `sub/f.txt`; the old
    substitute, `ctx get repo:f.txt`, read the root's `f.txt`."""
    (tmp_path / "ctx.toml").write_text("version = 1\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "f.txt").write_text("root line\n")
    (tmp_path / "sub" / "f.txt").write_text("sub line\n")
    env = dict(os.environ, CTX_STATE_HOME=str(tmp_path / "state"))
    head = subprocess.run(["head", "-n", "1", "f.txt"], cwd=tmp_path / "sub",
                          capture_output=True, text=True).stdout
    old = _ctx("get", "repo:f.txt", "--lines", "1:1", cwd=tmp_path / "sub", env=env).stdout
    assert "sub line" in head and "root line" in old  # different files


def test_why_find_was_retired_corpus_honours_ignore_files(tmp_path):
    (tmp_path / "ctx.toml").write_text("version = 1\n")
    (tmp_path / ".gitignore").write_text("build/\n")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "b.txt").write_text("x\n")
    (tmp_path / "a.txt").write_text("x\n")
    subprocess.run(["git", "init", "-q", "."], cwd=tmp_path, check=True)
    env = dict(os.environ, CTX_STATE_HOME=str(tmp_path / "state"))
    found = subprocess.run(["find", ".", "-name", "*.txt"], cwd=tmp_path,
                           capture_output=True, text=True).stdout
    corpus = _ctx("q", "corpus --glob *.txt", cwd=tmp_path, env=env).stdout
    assert "build/b.txt" in found and "build/b.txt" not in corpus  # different file sets
