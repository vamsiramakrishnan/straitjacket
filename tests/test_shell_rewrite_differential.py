"""Differential acceptance: a rewritten shell command must MEAN the same thing.

The guard rewrites an agent's shell command into a bounded `ctx run`
capture. That is only acceptable if the agent gets exactly what its own
command would have produced. The rewriter used to re-split the command into
words and re-quote each one, which is only correct for a command containing
nothing the shell interprets: `cat *.txt` became `cat '*.txt'`, a trailing
`# comment` became three arguments, `FOO=bar cmd` became a program name.

For every command in a matrix covering globs, every kind of quoting,
comments, environment prefixes, variable / command / arithmetic expansion,
tilde and brace expansion, redirections, pipelines, sequencing, here-docs,
background jobs, subshells, non-ASCII and spaced file names, dash-leading
arguments, and exit statuses above 1 (including >125 and signals), this test
runs the original in the reference shell and the hook's rewrite in the same
shell, then compares the exit status and the COMPLETE captured stdout and
stderr -- retrieved from the store through the run handle, never a digest --
byte for byte. Commands the hook declines to rewrite must be in DECLINED.

`CTX_DIFF_SRC` points the hook and `ctx run` at another source tree, which is
how the pre-fix numbers in the commit message were measured.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest

REF_SHELL = shutil.which("bash") or "/bin/sh"
SRC = os.environ.get("CTX_DIFF_SRC") or str(Path(__file__).resolve().parent.parent / "src")

BIG = "".join(f"row {i} payload payload payload payload\n" for i in range(1500))
FILES = {
    "a.txt": "A-CONTENT\nalpha line\n",
    "b.txt": "B-CONTENT\nbeta line\n",
    "c.log": "C-LOG\n",
    "big.log": BIG,
    "my file.txt": "SPACE-CONTENT\n",
    "ünïcødé.txt": "UNICODE-CONTENT\n",
    "-dash.txt": "DASH-CONTENT\n",
    "dir/one.py": "print('one')\n",
    "dir/sub/two.py": "print('two')\n",
    "x1.txt": "x1\n", "x2.txt": "x2\n", "xb.txt": "xb\n",
}

COMMANDS = [
    # globs
    "cat *.txt", "cat x?.txt", "cat x[1-2].txt", "ls *.py dir/*.py", "wc -l *.txt",
    "shopt -s globstar; ls dir/**/*.py", "cat nomatch*.txt", "cat big.log",
    # quoting
    "cat 'my file.txt'", 'cat "my file.txt"', "cat my\\ file.txt",
    "echo 'it'\"'\"'s'", "echo \"say \\\"hi\\\"\"", "echo 'a\"b' \"c'd\"", "echo $'tab\\there'",
    # comments
    "cat a.txt # trailing comment", "echo one #two three", "echo a#b",
    # environment prefixes and variables
    "FOO=bar sh -c 'echo $FOO'", "FOO=bar env | grep '^FOO='", "echo $HOME", "echo \"$FOO_UNSET\"x",
    "X=1; echo $X", "echo ${UNSET:-default}", "echo $((6*7))", "echo $(echo nested)", "echo `echo tick`",
    # tilde and brace expansion
    "echo ~ | wc -c", "cat ./{a,b}.txt", "echo {1..5}", "echo x{a,b}y",
    # redirections and pipelines
    "cat a.txt > out.txt; cat out.txt", "cat a.txt nope.txt 2>/dev/null", "cat nope.txt 2>&1",
    "cat a.txt b.txt | sort -r", "cat a.txt | wc -l", "set -o pipefail; false | true; echo $?",
    "cat a.txt >&2", "cat < a.txt", "grep -c line a.txt b.txt",
    # sequencing
    "cat a.txt; cat b.txt", "cat a.txt && cat b.txt", "cat nope.txt || cat b.txt",
    "cat a.txt\ncat b.txt", "false; echo after", "true && false || echo recovered",
    # here-documents and here-strings
    "cat <<'EOF'\nline one\n$NOT_EXPANDED\nEOF", "cat <<EOF\nhome=${HOME:+set}\nEOF", "cat <<< 'here string'",
    "while read l; do echo \"<$l>\"; done < a.txt",
    # background jobs and subshells
    "(cd dir && ls)", "{ echo g1; echo g2; } | sort -r", "sleep 0.1 & wait; echo bg-done",
    "echo $(cat a.txt | head -1)", "diff <(cat a.txt) <(cat b.txt)", "[[ -f a.txt ]] && echo is-file",
    "arr=(p q r); echo ${arr[1]}",
    # names
    "cat ünïcødé.txt", "cat -- -dash.txt", "ls -- -dash.txt", "cat ./-dash.txt",
    "find dir -name '*.py' | sort", "grep -rn CONTENT . | sort",
    # exit statuses
    "exit 3", "exit 126", "exit 200", "sh -c 'exit 7'", "cat nope.txt", "false",
    "kill -TERM $$", "sh -c 'kill -KILL $$'",
    # ordinary commands the guard captures
    "pytest --version >/dev/null 2>&1; echo $?", "git status --short | head -3",
    "python3 -c 'import sys; print(\"out\"); print(\"err\", file=sys.stderr); sys.exit(4)'",
    "head -n 3 big.log", "tail -n 2 big.log", "sed -n '2,4p' big.log", "grep -n beta *.txt",
    "find . -name 'x*.txt' | sort", "ls -R dir", "cat a.txt | tee copy.txt | wc -c; cat copy.txt",
    "printf '%s\\n' 'é ü' | wc -m", "echo \"$(printf 'a\\nb')\" | wc -l",
]

#: Shapes the hook deliberately does not rewrite (it allows them natively,
#: denies without a rewrite, or asks): the command then runs as itself or not
#: at all, never in a changed form. Keyed by command, valued by the reason.
DECLINED_REASONS = {
    "interactive or stdin-consuming program (never wrapped)": re.compile(
        r"^(while read|cat <|cat <<|sleep )"),
    "unknown program: the configured unknown_command boundary": re.compile(r"^(exit |kill |\[\[|arr=|X=1|shopt|sh -c 'exit)"),
}


def _env(tmp, src=SRC):
    env = dict(os.environ)
    env.update(PYTHONPATH=src, CTX_STATE_HOME=str(tmp / "state"), SHELL=REF_SHELL,
               HOME=str(tmp / "home"), LC_ALL="C.UTF-8")
    env.pop("FOO", None)
    return env


def _materialise(root: Path):
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    for rel, text in FILES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    (root / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "."], cwd=root, check=True)


def _status(rc: int) -> int:
    """One convention for both sides: death by signal N is 128+N."""
    return 128 - rc if rc < 0 else rc


def _hook(command: str, root: Path, env, cwd: Path | None = None) -> dict:
    cwd = cwd or root
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": str(cwd),
               "session_id": "diff", "tool_input": {"command": command}}
    out = subprocess.run([sys.executable, "-m", "ctx", "hook", "claude-code", "pre-tool-use"],
                         input=json.dumps(payload), capture_output=True, text=True, env=env,
                         cwd=cwd, timeout=60).stdout
    return json.loads(out).get("hookSpecificOutput", {})


#: Run from a subdirectory: one per program family. A capture runs where the
#: caller was, exactly as the command would have (the review found 9 of 13
#: differing when ctx ran everything at the workspace root).
SUBDIR_COMMANDS = [
    "cat one.py", "cat ../a.txt", "head -n 1 one.py", "tail -n 1 ../big.log", "sed -n 1p one.py",
    "wc -l one.py", "awk 'NR==1' one.py", "cat *.py", "pwd", "pwd; cat one.py",
    "cat $(pwd)/one.py", "find . -type f | sort", "find .. -name '*.py' | sort", "ls -R",
    "ls ../dir/sub", "grep -rn print . | sort", "grep -n CONTENT ../a.txt", "git status --short .",
    "git ls-files", "python3 -c 'import os; print(os.path.basename(os.getcwd()))'",
    "rg -n print | sort", "du -a --apparent-size one.py sub", "tree", "cat one.py | head -1",
]


@pytest.mark.parametrize("command", SUBDIR_COMMANDS)
def test_rewrite_runs_in_the_callers_directory(command, tmp_path):
    if not REF_SHELL.endswith("bash"):
        pytest.skip("the matrix is written for bash")
    argv0 = command.split()[0]
    if shutil.which(argv0) is None:
        pytest.skip(f"{argv0} not installed")
    root = tmp_path / "ws"
    env = _env(tmp_path)
    (tmp_path / "home").mkdir()
    _materialise(root)
    sub = root / "dir"
    truth = subprocess.run([REF_SHELL, "-c", command], cwd=sub, env=env, capture_output=True,
                           stdin=subprocess.DEVNULL, timeout=60)
    _materialise(root)
    hso = _hook(command, root, env, cwd=sub)
    rewrite = (hso.get("updatedInput") or {}).get("command")
    if not rewrite:
        assert hso.get("permissionDecision") in ("allow", None), (command, hso)
        return  # runs as itself
    got = subprocess.run([REF_SHELL, "-c", rewrite], cwd=sub, env=env, capture_output=True,
                         stdin=subprocess.DEVNULL, timeout=60)
    assert _status(got.returncode) == _status(truth.returncode), (command, got.stdout[-300:])
    if not rewrite.startswith("ctx run "):
        # A bounded native form (grep's -m cap): same directory, same answer here.
        assert (got.stdout, got.stderr) == (truth.stdout, truth.stderr), (command, rewrite)
        return
    stored = _stored_streams(root, env, got.stdout.decode("utf-8", "replace"))
    assert stored is not None, (command, rewrite, got.stdout[-300:])
    out, err = stored
    if argv0 in ("find", "ls", "du", "tree") or command.startswith("git status"):
        # Documented difference: a walk from the workspace root lists ctx's
        # bookkeeping directory, as it lists .git. From dir/ it is not walked.
        out = b"\n".join(ln for ln in out.split(b"\n") if b".ctx-session-reads" not in ln)
    assert out == truth.stdout, (command, rewrite, truth.stdout[:300], out[:300])
    assert err == truth.stderr, (command, rewrite, truth.stderr[:300], err[:300])


def _stored_streams(root: Path, env, shown: str) -> tuple[bytes, bytes] | None:
    m = re.search(r"\[ctx run:([0-9a-f]{6,})", shown)
    if not m:
        return None
    code = (
        "import json, sys\n"
        "from ctx.store import Store\nfrom ctx.workspace import resolve_workspace\n"
        "ws = resolve_workspace('.')\nst = Store(ws.workspace_id)\n"
        f"m = st.get_manifest(st.resolve_id({m.group(1)!r}))\n"
        "out = {k: st.get_blob(v['blob'].split(':', 1)[-1]).hex() for k, v in m['streams'].items()}\n"
        "print(json.dumps(out))\n"
    )
    r = subprocess.run([sys.executable, "-c", code], cwd=root, env=env, capture_output=True,
                       text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    streams = json.loads(r.stdout)
    return bytes.fromhex(streams["stdout"]), bytes.fromhex(streams["stderr"])


@pytest.mark.parametrize("command", COMMANDS)
def test_rewrite_preserves_shell_semantics(command, tmp_path):
    if not REF_SHELL.endswith("bash"):
        pytest.skip("the matrix is written for bash")
    root = tmp_path / "ws"
    env = _env(tmp_path)
    (tmp_path / "home").mkdir()
    _materialise(root)
    truth = subprocess.run([REF_SHELL, "-c", command], cwd=root, env=env, capture_output=True,
                           stdin=subprocess.DEVNULL, timeout=60)
    _materialise(root)
    hso = _hook(command, root, env)
    rewrite = (hso.get("updatedInput") or {}).get("command")
    if not rewrite:
        decision = hso.get("permissionDecision")
        if decision in ("allow", None):
            return  # runs as itself: identical by construction
        reasons = [r for r, rx in DECLINED_REASONS.items() if rx.search(command)]
        assert reasons, f"{command!r} declined ({decision}) but is not in the declared set"
        return
    assert rewrite.startswith("ctx run "), rewrite
    got = subprocess.run([REF_SHELL, "-c", rewrite], cwd=root, env=env, capture_output=True,
                         stdin=subprocess.DEVNULL, timeout=60)
    assert _status(got.returncode) == _status(truth.returncode), (
        command, rewrite, truth.returncode, got.returncode, got.stdout[-300:], got.stderr[-300:])
    stored = _stored_streams(root, env, got.stdout.decode("utf-8", "replace"))
    assert stored is not None, (command, rewrite, got.stdout[-300:])
    out, err = stored
    assert out == truth.stdout, (command, rewrite, truth.stdout[:300], out[:300])
    assert err == truth.stderr, (command, rewrite, truth.stderr[:300], err[:300])


def test_signal_and_high_status_contract(tmp_path):
    """--passthrough exists so the wrapped command's own status is what the
    agent sees: 200 stays 200, death by SIGTERM reads as 143, by SIGKILL 137."""
    root = tmp_path / "ws"
    env = _env(tmp_path)
    (tmp_path / "home").mkdir()
    _materialise(root)
    for command, want in (("exit 200", 200), ("exit 255", 255), ("kill -TERM $$", 143),
                          ("kill -KILL $$", 128 + signal.SIGKILL)):
        r = subprocess.run([sys.executable, "-m", "ctx", "run", "--passthrough", "--shell", "--",
                            command], cwd=root, env=env, capture_output=True, timeout=60)
        assert r.returncode == want, (command, r.returncode, r.stdout[-200:], r.stderr[-200:])


def test_matrix_is_wide():
    assert len(COMMANDS) >= 80


#: Capture primitives added for Antigravity (text readers on their own, git
#: listings, a globbed ls). Under steering = "rewrite" each is captured; the
#: capture must print exactly what the command prints.
CAPTURED_ALONE = [
    "nl a.txt", "seq 1 5", "sort -r a.txt b.txt", "cut -c1-3 a.txt", "uniq -c a.txt",
    "base64 a.txt", "rev a.txt", "fold -w 4 a.txt", "paste a.txt b.txt", "fmt a.txt",
    "expand a.txt", "unexpand a.txt", "comm a.txt b.txt", "column -t a.txt",
    "ls *.txt dir/*", "git ls-files", "git grep -n CONTENT", "git ls-tree -r HEAD",
    "git rev-list --all",
]


@pytest.mark.parametrize("command", CAPTURED_ALONE)
def test_new_capture_primitives_print_what_the_command_prints(command, tmp_path):
    if not REF_SHELL.endswith("bash"):
        pytest.skip("the matrix is written for bash")
    if shutil.which(command.split()[0]) is None:
        pytest.skip(f"{command.split()[0]} not installed")
    root = tmp_path / "ws"
    env = _env(tmp_path)
    (tmp_path / "home").mkdir()
    _materialise(root)
    truth = subprocess.run([REF_SHELL, "-c", command], cwd=root, env=env, capture_output=True,
                           stdin=subprocess.DEVNULL, timeout=60)
    _materialise(root)
    (root / "ctx.toml").write_text('version = 1\n[guard]\nsteering = "rewrite"\n')
    rewrite = (_hook(command, root, env).get("updatedInput") or {}).get("command")
    assert rewrite and rewrite.startswith("ctx run "), (command, rewrite)
    (root / "ctx.toml").write_text("version = 1\n")  # the file the truth run saw
    got = subprocess.run([REF_SHELL, "-c", rewrite], cwd=root, env=env, capture_output=True,
                         stdin=subprocess.DEVNULL, timeout=60)
    assert _status(got.returncode) == _status(truth.returncode), (command, got.stdout[-300:])
    out, err = _stored_streams(root, env, got.stdout.decode("utf-8", "replace"))
    if command.startswith("ls"):
        out = b"\n".join(ln for ln in out.split(b"\n") if b".ctx-session-reads" not in ln)
    assert (out, err) == (truth.stdout, truth.stderr), (command, truth.stdout[:300], out[:300])
