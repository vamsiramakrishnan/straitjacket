"""Differential acceptance: a rewritten search must give the SAME answer.

The guard rewrites an agent's search command into something bounded. That is
only acceptable when the rewritten command reports every match the original
would have reported, restricted to exactly the files the original would have
searched -- or declares, in text a reader can parse, what it left out. A
symbol lookup standing in for a text search failed that silently: `grep -rn
NEEDLE .` became `ctx q 'refs NEEDLE | group file'`, which never looks at
prose or config and dropped two of three matches with nothing declared.

This test makes that class of bug unshippable. Over a fixture repository with
code, prose, config, hidden files, ignored files, a binary file, nested
directories and a symlink, it generates a matrix of grep and ripgrep commands
(identifier, phrase, regex, fixed-string and dash-leading patterns, crossed
with the file, case, mode, context, count and ignore flags), runs each one for
real, runs whatever the hook would run in its place, and compares the sets of
matching (file, line) pairs -- or file sets, for the file-listing modes.

ripgrep cases skip cleanly where `rg` is not installed; grep is always there.

No output is filtered. ctx writes no command text (and nothing else taken
from tool input) under the workspace -- those ledgers live in its state
directory (ctx.sessiondir.PRIVATE_LEDGERS) -- so a search for the user's own
strings answers exactly as in a copy ctx never touched, in a pipeline or not.
The one documented difference: a command that reports on every file or
directory it walks (find, ls -R, tree, du, and grep's -v / -L / -c modes)
also reports ctx's bookkeeping directory, as it reports .git.
"""

from __future__ import annotations

import contextlib
import io
import itertools
import json
import os
import re
import shlex
import shutil
import subprocess

import pytest

FILES = {
    "app.py": "def handler():\n    return 'ok'  # see NEEDLE_TOKEN_42\n\n"
              "def compute_total(x):\n    return x\n\nprint(compute_total(2))\n"
              "VALUE = 'a.b(c)'\n",
    "notes.md": "# Deploy notes\n\nThe rollout failed with NEEDLE_TOKEN_42 in staging.\n"
                "needle_token_42 lower case\n-dash leading line\n",
    "config.yaml": "retries: 3\nmarker: NEEDLE_TOKEN_42\nrollout failed: true\n",
    "nested/deep/mod.py": "from app import compute_total\n# NEEDLE_TOKEN_42 deep\n",
    "nested/readme.txt": "compute_total is documented here\n",
    ".hidden/secret.txt": "NEEDLE_TOKEN_42 hidden file\n",
    "build/out.txt": "NEEDLE_TOKEN_42 in an ignored build artifact\n",
}
GITIGNORE = "build/\n"
PATTERNS = ["NEEDLE_TOKEN_42", "compute_total", "rollout failed", "comp[a-z]*_total",
            "a.b(c)", "needle_token_42", "-dash"]

GREP_FLAGS = [
    ["-rn"], ["-rni"], ["-rnw"], ["-rnF"], ["-rnE"], ["-rn", "--include=*.md"],
    ["-rn", "--exclude=*.py"], ["-rn", "--exclude-dir=nested"], ["-rl"], ["-rL"],
    ["-rn", "-A1"], ["-rn", "-m1"], ["-rnI"], ["-Rn"], ["-rnc"], ["-rnv"],
]
GREP_PATHS = [["."], ["nested"], ["notes.md", "app.py"]]
RG_FLAGS = [
    ["-n"], ["-ni"], ["-nw"], ["-nF"], ["-n", "-g", "*.md"], ["-n", "-t", "py"],
    ["-n", "--hidden"], ["-n", "--no-ignore"], ["-l"], ["-n", "-A1"], ["-n", "-m1"],
    ["-n", "-uuu"],
]
RG_PATHS = [[], ["nested"]]


def _cases():
    for pat, flags, paths in itertools.product(PATTERNS, GREP_FLAGS, GREP_PATHS):
        yield ["grep", *flags, "-e", pat, *paths] if pat.startswith("-") else ["grep", *flags, pat, *paths]
    for pat, flags, paths in itertools.product(PATTERNS, RG_FLAGS, RG_PATHS):
        yield ["rg", *flags, "-e", pat, *paths] if pat.startswith("-") else ["rg", *flags, pat, *paths]


LISTINGS = [
    ["find", "."], ["find", ".", "-name", "*.txt"], ["find", ".", "-type", "f"],
    ["find", "nested"], ["ls", "-R"], ["ls", "-aR"], ["ls", "-1aR", "nested"], ["tree", "-a"],
    ["git", "grep", "--untracked", "-n", "NEEDLE_TOKEN_42"],
    ["grep", "-rn", "op", "."], ["grep", "-rl", "steer", "."], ["rg", "--hidden", "-n", "op"],
]

CASES = [shlex.join(argv) for argv in _cases()] + [shlex.join(a) for a in LISTINGS]


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    root = tmp_path_factory.mktemp("searchrepo")
    for rel, text in FILES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    (root / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    (root / "blob.bin").write_bytes(b"\x00\x01NEEDLE_TOKEN_42\x00compute_total\x00")
    os.symlink("app.py", root / "link_to_app.py")
    (root / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "."], cwd=root, check=True)
    # Ground truth comes from a copy ctx has never touched...
    pristine = root.parent / (root.name + "-pristine")
    shutil.copytree(root, pristine, symlinks=True)
    # ...and every substituted run happens after ctx has run in the real one
    # with the very patterns the matrix searches for: the hook, end to end,
    # and captured runs, which is what used to leave those patterns behind.
    for command in ("grep -rn NEEDLE_TOKEN_42 .", "rg compute_total", "grep -rn 'rollout failed' .",
                    "grep -rn needle_token_42 nested"):
        _hook_command(command, root)
        _real_hook(command, root)
    return root, pristine


def _real_hook(command: str, cwd) -> None:
    """The hook as the host runs it (writes its ledgers), then the capture."""
    import sys

    payload = {"tool_name": "Bash", "cwd": str(cwd), "session_id": "fixture",
               "tool_input": {"command": command}}
    out = subprocess.run([sys.executable, "-m", "ctx", "hook", "claude-code", "pre-tool-use"],
                         input=json.dumps(payload), capture_output=True, text=True, cwd=cwd)
    hso = (json.loads(out.stdout or "{}").get("hookSpecificOutput") or {})
    sub = (hso.get("updatedInput") or {}).get("command")
    if sub:
        _run_ctx(shlex.split(sub), cwd)


_MATCH_RE = re.compile(r"^(?:\./)?([^:\n]+?):(\d+):", re.M)


_STATE = ".ctx-session-reads"


def _walks_every_file(argv: list[str]) -> bool:
    """Reports on every file it walks, matched or not: lists ctx's
    bookkeeping directory as it lists .git (the documented difference)."""
    return argv[0] in ("find", "ls", "tree", "du") or (
        argv[0] == "grep" and any(f in argv for f in ("-rnv", "-rL", "-rnc"))) or (
        # A search for ctx's own vocabulary (its JSON keys) matches its
        # bookkeeping, as a search for git's matches .git.
        argv[-1] in ("op", "steer") or "op" in argv[-2:] or "steer" in argv[-2:])


def _pairs(out: str, files_only: bool) -> set:
    lines = [ln for ln in out.splitlines() if ln and not ln.startswith("[ctx")]
    if files_only:
        return {ln[2:] if ln.startswith("./") else ln for ln in lines}
    return {(m.group(1), int(m.group(2))) for m in _MATCH_RE.finditer("\n".join(lines))}


def _known_files(root) -> list[str]:
    out = []
    for dirpath, _dirs, names in os.walk(root):
        if ".git" in dirpath.split(os.sep):
            continue
        for n in names:
            out.append(os.path.relpath(os.path.join(dirpath, n), root).replace(os.sep, "/"))
    return out


_DECLARED_RE = re.compile(r"\b\d+ (?:more|omitted)\b|\bomitted: \d+")


def _run_ctx(argv: list[str], cwd) -> str:
    from ctx.cli import main

    buf = io.StringIO()
    old = os.getcwd()
    os.chdir(cwd)
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            try:
                main(argv[1:])
            except SystemExit:
                pass
    finally:
        os.chdir(old)
    return buf.getvalue()


def _hook_command(command: str, cwd) -> str | None:
    """What the hook would actually run in place of ``command`` (None: refused)."""
    from ctx import hook

    decision = hook.classify({"tool_name": "Bash", "cwd": str(cwd),
                              "tool_input": {"command": command}},
                             {**hook._load_guard_policy(str(cwd)), "_flavor": "claude-code"})
    rewrite = decision.get("rewrite") or {}
    if isinstance(rewrite.get("updatedInput"), dict):
        return rewrite["updatedInput"].get("command")
    if decision.get("decision") == "allow":
        return command
    return None


@pytest.mark.parametrize("command", CASES)
def test_rewritten_search_gives_the_same_answer(repo, command, monkeypatch, tmp_path):
    repo, pristine = repo
    argv = shlex.split(command)
    if shutil.which(argv[0]) is None:
        pytest.skip(f"{argv[0]} not installed")
    monkeypatch.setenv("CTX_STATE_HOME", str(tmp_path / "state"))
    files_only = any(f in argv for f in ("-l", "-rl", "-rL", "-L", "-rnc"))
    truth = subprocess.run(argv, cwd=pristine, capture_output=True, text=True).stdout
    sub = _hook_command(command, repo)
    if sub is None or sub == command:
        return  # refused, or run unchanged: the answer is the original's
    if sub.startswith("ctx "):
        got = _run_ctx(shlex.split(sub), repo)
    else:
        got = subprocess.run(sub, shell=True, cwd=repo, capture_output=True, text=True).stdout
    walks = _walks_every_file(argv)
    if not walks:
        # A search for the user's own strings never meets ctx's state.
        assert _STATE not in got, (command, sub, [ln for ln in got.splitlines() if _STATE in ln][:4])
    if argv[0] in ("find", "ls", "tree"):
        # Listings: the same set of lines as in the untouched workspace, plus
        # ctx's bookkeeping directory (documented). tree's count line counts it.
        keep = [ln for ln in got.splitlines() if not ln.startswith("[ctx")]
        want_l, have_l = set(truth.splitlines()), set(keep)
        if argv[0] == "tree":
            want_l = {ln for ln in want_l if "director" not in ln}
            have_l = {ln for ln in have_l if "director" not in ln}
        extra = {ln for ln in have_l - want_l if _STATE in ln or ln.strip() in (
            "", "e.count", "fixture.count")}
        if argv[0] == "ls":  # entries under the state directory's own block
            block = got.split(f"./{_STATE}:", 1)[-1].split("\n\n", 1)[0] if _STATE in got else ""
            extra |= {ln for ln in have_l - want_l if ln and ln in block.splitlines()}
        assert have_l - extra == want_l, (command, sub, sorted((have_l - extra) ^ want_l)[:8])
        return
    if walks:
        got = "\n".join(ln for ln in got.splitlines() if _STATE not in ln)
    want = _pairs(truth, files_only)
    have = _pairs(got, files_only)
    if not walks and not _DECLARED_RE.search(got):
        assert have == want, (command, sub, sorted(have ^ want)[:6])
    declared = bool(_DECLARED_RE.search(got))
    missing = want - have
    assert not missing or declared, (
        f"{command!r} -> {sub!r} lost {sorted(missing)[:6]} with nothing declared")
    truth_files = {p if isinstance(p, str) else p[0] for p in want}
    extra = {f for f in _known_files(repo)
             if re.search(rf"(?<![\w/.]){re.escape(f)}(?![\w/])", got)
             and f not in truth_files and f not in truth}
    assert not extra, f"{command!r} -> {sub!r} reported files the original never matched: {sorted(extra)}"


def test_matrix_is_wide():
    assert len(CASES) >= 500
    json.dumps(CASES)  # stable, printable ids


def test_no_command_text_is_left_under_the_workspace(repo):
    """The fixture ran the hook and captured runs with every pattern the
    matrix searches for; none of them is in any file ctx wrote under the
    workspace."""
    repo, _pristine = repo
    state = repo / _STATE
    assert state.is_dir()
    for path in state.rglob("*"):
        if path.is_file():
            text = path.read_bytes()
            for pattern in (b"NEEDLE_TOKEN_42", b"compute_total", b"rollout failed", b"needle_token_42"):
                assert pattern not in text, (path.name, pattern)


def test_rewrite_never_edits_the_command(tmp_path):
    from ctx import hook

    for command in ("grep -rn x .", "find . -name '*.py'", "pytest -q"):
        d = hook.classify({"tool_name": "Bash", "cwd": str(tmp_path),
                           "tool_input": {"command": command}})
        cmd = d["rewrite"]["updatedInput"]["command"]
        assert cmd == "ctx run --passthrough --shell -- " + shlex.quote(command), cmd
