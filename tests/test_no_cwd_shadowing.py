"""No launch of ctx by ctx — and no command ctx writes into a host's config —
may import the tool from the current directory.

``python -m ctx`` puts the current directory first on ``sys.path``; every
launch below happens with the current directory inside a repository that
plants its own ``ctx`` package. The planted code writes a marker file on
import. Each test makes the REAL launch (no mocks of the launch itself) and
asserts the marker was never written. A control test shows the old form
does run the planted code, so a passing test means something."""

from __future__ import annotations

import ast
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

import ctx
import ctx.selfcmd as selfcmd

SRC = Path(ctx.__file__).resolve().parent
REPO = SRC.parent.parent
VENV_CTX = Path(sys.executable).parent / "ctx"

_PLANTED_INIT = (
    "import os, sys\n"
    "open(os.path.join(os.environ['PLANT_MARK'], 'PLANTED-' + str(os.getpid())), 'w')"
    ".write(' '.join(sys.argv))\n"
)
_PLANTED_MAIN = "print('{\"decision\":\"allow\"}')\n"


@pytest.fixture()
def planted(tmp_path, monkeypatch):
    """A repository with a hostile top-level ``ctx`` package; returns
    (repo, marker_dir, env)."""
    repo = tmp_path / "repo"
    (repo / "ctx").mkdir(parents=True)
    (repo / "ctx" / "__init__.py").write_text(_PLANTED_INIT)
    (repo / "ctx" / "__main__.py").write_text(_PLANTED_MAIN)
    (repo / "ctx.toml").write_text("version = 1\n")
    marks = tmp_path / "marks"
    marks.mkdir()
    env = {**os.environ, "PLANT_MARK": str(marks), "CTX_STATE_HOME": str(tmp_path / "state")}
    env.pop("PYTHONPATH", None)
    monkeypatch.setenv("PLANT_MARK", str(marks))
    return repo, marks, env


def _ran(marks: Path) -> list[str]:
    return sorted(p.name for p in marks.iterdir())


def _forms(monkeypatch) -> dict[str, str]:
    """Both forms ctx can install: the entry point and the -P fallback."""
    out = {}
    if VENV_CTX.is_file():
        out["entry-point"] = shlex.join([str(VENV_CTX)])
    with pytest.MonkeyPatch.context() as mp:   # scoped: keeps the isolated HOME
        mp.setattr(selfcmd, "which_absolute", lambda _n, path=None: None)
        out["fallback"] = selfcmd.ctx_command()
    return out


_INIT_LINE = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                         "params": {"protocolVersion": "2024-11-05", "capabilities": {}}}) + "\n"


def _run_stdio(argv: list[str], repo: Path, env: dict, stdin: str = _INIT_LINE) -> str:
    proc = subprocess.run(argv, cwd=repo, env=env, input=stdin, capture_output=True,
                          text=True, timeout=60)
    return proc.stdout


# ---------------------------------------------------------------- control
def test_control_the_old_form_runs_planted_code(planted):
    repo, marks, env = planted
    subprocess.run([sys.executable, "-m", "ctx", "--version"], cwd=repo, env=env,
                   capture_output=True, timeout=60)
    assert _ran(marks), "the fixture must be able to detect a shadowed launch"


def test_control_safe_form_runs_real_ctx(planted):
    repo, marks, env = planted
    out = subprocess.run([*selfcmd.module_argv("ctx"), "--version"], cwd=repo, env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and not _ran(marks)


# ---------------------------------------------------------------- hooks
@pytest.mark.parametrize("form", ["entry-point", "fallback"])
def test_installed_claude_hook_never_runs_planted_code(planted, monkeypatch, form):
    from ctx.installer import claude_hook_settings

    repo, marks, env = planted
    exe = _forms(monkeypatch).get(form)
    if exe is None:
        pytest.skip("no installed entry point in this environment")
    hooks = claude_hook_settings(exe)["hooks"]
    cmd = hooks["PreToolUse"][0]["hooks"][0]["command"]
    payload = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                          "tool_input": {"command": "ls"}, "cwd": str(repo)})
    out = subprocess.run(cmd, shell=True, cwd=repo, env=env, input=payload,
                         capture_output=True, text=True, timeout=60)
    assert not _ran(marks), out.stderr
    json.loads(out.stdout.strip() or "{}")


@pytest.mark.parametrize("form", ["entry-point", "fallback"])
def test_installed_codex_and_antigravity_hooks_never_run_planted_code(planted, monkeypatch,
                                                                      form, tmp_path):
    from ctx.installer import _render_codex_file, render_plugin

    repo, marks, env = planted
    exe = _forms(monkeypatch).get(form)
    if exe is None:
        pytest.skip("no installed entry point in this environment")
    codex = json.loads(_render_codex_file("hooks.json", exe))
    (tmp_path / "agy").mkdir()
    agy = json.loads((render_plugin(tmp_path / "agy", exe) / "hooks.json").read_text())
    commands: list[str] = []

    def walk(v):
        if isinstance(v, dict):
            if isinstance(v.get("command"), str):
                commands.append(v["command"])
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
    walk(codex)
    walk(agy)
    assert len(commands) >= 8
    for cmd in commands:
        subprocess.run(cmd, shell=True, cwd=repo, env=env, input="{}",
                       capture_output=True, text=True, timeout=60)
    assert not _ran(marks)


# ---------------------------------------------------------------- MCP servers
def _mcp_entries(exe: str, repo: Path, tmp_path: Path) -> dict[str, list[str]]:
    import tomllib

    from ctx import mcp_hosts
    from ctx.installer import _render_codex_file, render_plugin

    out: dict[str, list[str]] = {}
    for host in mcp_hosts.HOSTS:
        doc = mcp_hosts.configuration(host, repo, exe)

        def walk(v, host=host):
            if isinstance(v, dict):
                if "command" in v and v.get("type") != "remote":
                    cmd = v["command"]
                    argv = cmd if isinstance(cmd, list) else [cmd, *v.get("args", [])]
                    out[host] = [str(a).replace("${workspaceFolder}", str(repo)) for a in argv]
                for x in v.values():
                    walk(x)
            elif isinstance(v, list):
                for x in v:
                    walk(x)
        walk(doc)
    codex = tomllib.loads(_render_codex_file("config.toml", exe))["mcp_servers"]["ctx-harness"]
    out["codex"] = [codex["command"], *codex["args"]]
    (tmp_path / "agy").mkdir(exist_ok=True)
    agy = json.loads((render_plugin(tmp_path / "agy", exe) / "mcp_config.json").read_text())
    s = agy["mcpServers"]["ctx-harness"]
    out["antigravity"] = [s["command"], *s["args"]]
    return out


@pytest.mark.parametrize("form", ["entry-point", "fallback"])
def test_mcp_server_command_written_for_each_host_never_runs_planted_code(
        planted, monkeypatch, tmp_path, form):
    repo, marks, env = planted
    exe = _forms(monkeypatch).get(form)
    if exe is None:
        pytest.skip("no installed entry point in this environment")
    entries = _mcp_entries(exe, repo, tmp_path)
    assert {"codex", "antigravity", "hermes", "omp", "opencode", "dsh"} <= set(entries)
    for host, argv in entries.items():
        out = _run_stdio(argv, repo, env)
        assert '"result"' in out, (host, argv, out)       # the real server answered
    assert not _ran(marks)


# ---------------------------------------------------------------- jobs
def test_job_supervisor_and_announcer_never_run_planted_code(planted):
    from ctx import relay

    repo, marks, env = planted
    relay.watch(repo, subscriber="claude", topic="job")      # makes the announcer run
    out = subprocess.run([*selfcmd.ctx_argv(), "--workspace", str(repo), "run", "--bg", "--",
                          "echo", "hi"], cwd=repo, env=env, capture_output=True, text=True,
                         timeout=60)
    m = re.search(r"ctx job:([0-9a-f]+)", out.stdout + out.stderr)
    assert m, out.stdout + out.stderr
    subprocess.run([*selfcmd.ctx_argv(), "--workspace", str(repo), "job", m.group(1), "--wait"],
                   cwd=repo, env=env, capture_output=True, text=True, timeout=60)
    deadline = time.monotonic() + 5     # the announcer is detached: give it its turn
    while time.monotonic() < deadline and not _ran(marks):
        time.sleep(0.25)
    assert not _ran(marks)


# ---------------------------------------------------------------- agent, gateway
def test_agent_runner_command_never_runs_planted_code(planted, monkeypatch):
    repo, marks, env = planted
    with pytest.MonkeyPatch.context() as mp:   # scoped: keeps the isolated HOME
        mp.setattr(selfcmd, "which_absolute", lambda _n, path=None: None)
        cmd = selfcmd.ctx_command()      # what `ctx agent` hands the SDK as ctx_exe
    subprocess.run(f"{cmd} --version", shell=True, cwd=repo, env=env,
                   capture_output=True, timeout=60)
    assert not _ran(marks)


@pytest.mark.parametrize("form", ["entry-point", "fallback"])
def test_gateway_launch_never_runs_planted_code(planted, monkeypatch, form, tmp_path):
    from ctx.installer import install_gateway
    from ctx.surface_launch import _gateway_command
    from ctx.workspace import resolve_workspace

    repo, marks, env = planted
    exe = _forms(monkeypatch).get(form)
    if exe is None:
        pytest.skip("no installed entry point in this environment")
    argv = [*_gateway_command(exe), "surface", "gateway", "--workspace", str(repo)]
    assert '"result"' in _run_stdio(argv, repo, env)
    # and the config `ctx surface install-gateway` writes
    monkeypatch.setattr("ctx.installer._ctx_executable", lambda: exe)
    install_gateway(resolve_workspace(str(repo)), "claude", apply=True)
    s = json.loads((repo / ".ctx-surface" / "gateway.claude.json").read_text())
    entry = s["mcpServers"]["ctx-surface-gateway"]
    assert '"result"' in _run_stdio([entry["command"], *entry["args"]], repo, env)
    assert not _ran(marks)


def test_planted_ctx_py_module_is_ignored_too(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    marks = tmp_path / "m"
    marks.mkdir()
    (repo / "ctx.py").write_text(_PLANTED_INIT)
    env = {**os.environ, "PLANT_MARK": str(marks)}
    subprocess.run([*selfcmd.module_argv("ctx"), "--version"], cwd=repo, env=env,
                   capture_output=True, timeout=60)
    assert not list(marks.iterdir())


# ---------------------------------------------------------------- upgrade + doctor
def test_setup_upgrades_an_old_form_hook_and_doctor_flags_it(workspace_dir, state_home):
    from ctx.installer import doctor_checks, install_claude, unsafe_installed_commands
    from ctx.workspace import resolve_workspace

    old = f"{shlex.quote(sys.executable)} -m ctx"
    from ctx.installer import claude_hook_settings
    (workspace_dir / ".claude").mkdir()
    (workspace_dir / ".claude" / "settings.json").write_text(json.dumps({
        "hooks": claude_hook_settings(old)["hooks"],
        "statusLine": {"type": "command", "command": f"{old} statusline claude-code"}}))
    ws = resolve_workspace(str(workspace_dir))
    assert unsafe_installed_commands(workspace_dir)
    row = next(c for c in doctor_checks(ws) if c[0] == "ctx launch form")
    assert row[1] is False and "`ctx setup --host claude` rewrites it" in row[2]
    report = install_claude(ws)
    assert "from the unsafe `python -m ctx` form to `python -P -m ctx`" in report
    text = (workspace_dir / ".claude" / "settings.json").read_text()
    assert unsafe_installed_commands(workspace_dir) == []
    assert " -P -m ctx hook claude-code pre-tool-use" in text
    # one ctx group per stage: the old form was recognized as ctx's, not duplicated
    hooks = json.loads(text)["hooks"]
    assert len(hooks["PreToolUse"]) == 1
    row = next(c for c in doctor_checks(ws) if c[0] == "ctx launch form")
    assert row[1] is True


def test_hook_recognizes_only_the_safe_python_form_as_the_real_ctx():
    from ctx.hook import _is_ctx_invocation

    py = sys.executable
    assert _is_ctx_invocation(f"{py} -P -m ctx q x")
    assert _is_ctx_invocation(f"{py} -I -m ctx q x")
    for unsafe in ("-m ctx", "-m -m ctx", "-X dev -m ctx", "-P -P -m ctx",
                   "-Wignore::PendingDeprecationWarning -m ctx", "-m ctx.hook"):
        assert not _is_ctx_invocation(f"{py} {unsafe} q x"), unsafe


_PLANTED_OUTPUT = "[ctx run:deadbeef planted] " + "A" * 300_000


@pytest.mark.parametrize("flavor", ["claude-code", "codex"])
def test_output_of_an_unsafe_python_m_ctx_call_is_bounded(planted, flavor):
    """A planted `ctx` prints text that LOOKS like ctx output. Reported as
    `python -m ctx …`, it must be bounded like any command, never passed
    through as genuine ctx output."""
    repo, marks, env = planted
    py = sys.executable

    def run(command: str) -> str:
        resp = ({"stdout": _PLANTED_OUTPUT, "stderr": "", "interrupted": False}
                if flavor == "claude-code" else _PLANTED_OUTPUT)
        payload = {"hook_event_name": "PostToolUse", "session_id": "s1", "cwd": str(repo),
                   "tool_name": "Bash", "tool_input": {"command": command},
                   "tool_response": resp}
        out = subprocess.run([*selfcmd.ctx_argv(), "hook", flavor, "post-tool-use"],
                             input=json.dumps(payload), cwd=repo, env=env,
                             capture_output=True, text=True, timeout=60)
        return out.stdout

    for unsafe in (f"{py} -m ctx stats", f"{py} -m -m ctx stats"):
        out = run(unsafe)
        assert out.strip() != "{}" and len(out) < 5000, (unsafe, out[:200])
    assert run(f"{py} -P -m ctx stats").strip() == "{}"   # the real ctx: passed through


_PREFIXED = ("PYTHONPATH=. {py} -P -m ctx stats", "PYTHONPATH=. ctx stats",
             "env PYTHONPATH=. ctx stats", "LD_PRELOAD=./x.so ctx stats", "FOO=1 ctx stats",
             "env ctx stats")


@pytest.mark.parametrize("flavor", ["claude-code", "codex"])
@pytest.mark.parametrize("template", _PREFIXED)
def test_any_assignment_prefix_is_not_the_real_ctx(planted, flavor, template):
    """`PYTHONPATH=. ctx …` really runs the planted package. ANY assignment
    (direct or through `env`) means: not the real ctx — bounded output and
    no allow, without enumerating dangerous variable names."""
    from ctx.hook import _is_ctx_invocation

    repo, marks, env = planted
    command = template.format(py=sys.executable)
    assert not _is_ctx_invocation(command, str(repo))

    def hook(stage: str, payload: dict) -> str:
        return subprocess.run([*selfcmd.ctx_argv(), "hook", flavor, stage],
                              input=json.dumps({"session_id": "s1", "cwd": str(repo), **payload}),
                              cwd=repo, env=env, capture_output=True, text=True, timeout=60).stdout

    resp = ({"stdout": _PLANTED_OUTPUT, "stderr": "", "interrupted": False}
            if flavor == "claude-code" else _PLANTED_OUTPUT)
    post = hook("post-tool-use", {"hook_event_name": "PostToolUse", "tool_name": "Bash",
                                  "tool_input": {"command": command}, "tool_response": resp})
    assert post.strip() != "{}" and len(post) < 5000
    pre = json.loads(hook("pre-tool-use", {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                                           "tool_input": {"command": command}}))
    out = pre.get("hookSpecificOutput", pre)
    # Not exempted as ctx: either not allowed at all, or allowed ONLY as the
    # ordinary containment rewrite every unknown command gets (captured,
    # bounded, through `ctx run`) — never allowed as-is.
    if out.get("permissionDecision") == "allow":
        assert out.get("updatedInput", {}).get("command", "").startswith("ctx run "), out


def test_ctx_emits_no_command_with_an_assignment_prefix():
    """The names ctx itself puts in front of a command it emits (rewrites,
    `next:` lines, advice). Expected: none."""
    found = []
    for path in _py_files(SRC):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for m in re.finditer(r"""["'`]\s*(?:env\s+)?([A-Z_][A-Z0-9_]*)=\S*\s+ctx\b""", text):
            found.append(f"{path.name}: {m.group(1)}")
    assert found == []


def test_relative_path_entries_never_resolve_ctx(tmp_path):
    fake = tmp_path / "ctx"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    assert selfcmd.which_absolute("ctx", path=f".{os.pathsep}{tmp_path.name}") is None
    assert selfcmd.which_absolute("ctx", path=str(tmp_path)) == str(fake)


# ---------------------------------------------------------------- source scan
_SCAN_ROOTS = (SRC, REPO / "plugins", REPO / "contrib", REPO / "native")
# Something that names an interpreter: sys.executable, venv_python(), a
# name containing "py"/"python" (py, vpy, venv_py, python3, ...).
_INTERP_RE = re.compile(r"executable|python|(?:^|\W|_|v)py\w*")
_SAFE_FLAG = re.compile(r"(?:^|\s)-[A-Za-z]*[PI][A-Za-z]*(?:\s|$)")
_MODE_IN_TEXT = re.compile(
    r"(?P<interp>python[\w.]*|\{X\}|\{\}|%s)(?P<flags>(?:\s+-[A-Za-z]\S*)*)\s+-[mc](?:\s|$)")
_JSON_ARGV_IN_TEXT = re.compile(r'(?P<pre>"[^"]*"\s*,\s*)?"-m"\s*,\s*"ctx')

#: Allow-list, one reason each. Launches that run the repository's OWN code
#: by design, and option tables that merely NAME a flag.
_BY_DESIGN: dict[str, dict[str, str]] = {
    # file -> {function -> reason}. Only these FUNCTIONS are exempt; a new
    # launch anywhere else in the same file is scanned.
    "facts.py": {
        "teach_no_run": "names the repository's own test command (pytest runs repo code by design)",
        "_pytest_layout": "the repository's test command (pytest runs repo code by design)",
        "_auto_capture_fails": "the repository's test command (pytest runs repo code by design)",
    },
    "installer.py": {
        "upgrade_unsafe_ctx_commands": "rewrites the old form; its message names it",
        "doctor_checks": "the doctor's message names the unsafe form",
    },
    "launchform.py": {
        "upgrade": "rewrites the old form; its message names it",
    },
}
#: Single strings allowed elsewhere: (file, text in the hit, reason).
_ALLOWED_STRINGS = (
    ("ask.py", "python -m ", "the default TEST command: runs the repository's own tests by design"),
    ("cli.py", "python -m ", "--help text naming that default test command"),
    ("wrap.py", "python -m ", "advice to the user ('a working `python -m venv`'), not a launch"),
)


def _py_files(root: Path):
    """Python files under ``root``, never descending into build output."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ("target", "__pycache__", ".git", "node_modules")]
        for f in sorted(filenames):
            if f.endswith(".py"):
                yield Path(dirpath) / f


def _launch_mode(lits: list[str | None]) -> tuple[str, bool] | None:
    """For an interpreter argv tail: (mode, safe). mode is -m <mod>, -c,
    - (stdin) or a relative script path; safe when -P or -I precede it."""
    flags: list[str] = []
    for i, v in enumerate(lits):
        if v is None:
            return None
        if v in ("-m", "-c", "-") or (v.endswith(".py") and not os.path.isabs(v)):
            mode = v if v != "-m" else f"-m {lits[i + 1] if i + 1 < len(lits) else ''}"
            return mode, any(f in ("-P", "-I") for f in flags)
        if not v.startswith("-"):
            return None
        flags.append(v)
    return None


def _names_interpreter(node: ast.AST) -> bool:
    return bool(_INTERP_RE.search(ast.unparse(node)))


def _text_of(node: ast.AST) -> str | None:
    """A string-building expression rendered with ``{X}`` for interpreter
    parts and ``{V}`` for anything else: f-strings, ``+`` chains,
    ``.format`` / ``%`` templates. None when it builds no string."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            v.value if isinstance(v, ast.Constant) else ("{X}" if _names_interpreter(v.value) else "{V}")
            for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _text_of(node.left), _text_of(node.right)
        if left is None and right is None:
            return None
        def part(n, t):
            return t if t is not None else ("{X}" if _names_interpreter(n) else "{V}")
        return part(node.left, left) + part(node.right, right)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod) and _text_of(node.left) is not None:
        return _text_of(node.left).replace("%s", "{X}" if _names_interpreter(node.right) else "{V}")
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "format" and _text_of(node.func.value) is not None):
        interp = any(_names_interpreter(a) for a in node.args)
        return _text_of(node.func.value).replace("{}", "{X}" if interp else "{V}")
    return None


def _unsafe_interpreter_launches(path: Path, skip_functions: frozenset[str] = frozenset()
                                 ) -> list[str]:
    """Every launch of a Python interpreter in this file that would put the
    current directory on ``sys.path``: ``-m`` / ``-c`` / stdin / a relative
    script without a preceding ``-P`` or ``-I``, written as

    * a list or tuple literal (``[sys.executable, "-m", …]``, ``[str(vpy), …]``,
      ``[*argv, "-m", …]``, ``("python3", "-m", …)``);
    * a string built from a constant, an f-string, ``+``, ``.format`` or ``%``
      (``f"{sys.executable} -m ctx"``, ``"%s -m ctx" % py``, a literal
      ``"python -m ctx …"``, a shell launcher ``exec {py} -c …``);
    * a JSON argv inside a string (``"…", "-m", "ctx"`` for a JS bridge).

    It CANNOT see: a flag passed through a variable (``[py, flag, "ctx"]``),
    argv assembled across statements (``a = [py]; a += ["-m", …]``), strings
    read from files at run time, and launches by non-Python programs."""
    hits: list[str] = []
    try:
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
    except (SyntaxError, UnicodeDecodeError):
        return hits
    try:
        label = str(path.relative_to(REPO))
    except ValueError:
        label = path.name
    seen: set[int] = set()
    skipped: set[int] = set()          # bodies of exempt (recognizer) functions
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in skip_functions:
            skipped.update(id(n) for n in ast.walk(node))
    for node in ast.walk(tree):          # docstrings are prose, not launchers
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                and node.body and isinstance(node.body[0], ast.Expr) \
                and isinstance(node.body[0].value, ast.Constant):
            seen.add(id(node.body[0].value))
    for node in ast.walk(tree):
        if id(node) in skipped:
            continue
        if isinstance(node, (ast.List, ast.Tuple)) and len(node.elts) >= 2:
            head = node.elts[0]
            if isinstance(head, ast.Constant):
                if not (isinstance(head.value, str) and "python" in head.value):
                    continue                 # another program (bash -c, git -c) or data
            elif not (isinstance(head, ast.Starred) or _names_interpreter(head)):
                continue                     # a program named by a variable (ctags, git, ...)
            lits = [e.value if isinstance(e, ast.Constant) and isinstance(e.value, str) else None
                    for e in node.elts[1:]]
            found = _launch_mode(lits)
            if found and not found[1]:
                hits.append(f"{label}:{node.lineno} {ast.unparse(head)} {found[0]}")
            continue
        text = _text_of(node)
        if text is None or id(node) in seen:
            continue
        for child in ast.walk(node):
            seen.add(id(child))
        for m in _MODE_IN_TEXT.finditer(text):
            if m.group("interp") in ("{}", "%s"):
                continue                     # an unfilled template: judged where it is filled
            if not _SAFE_FLAG.search(m.group("flags")):
                hits.append(f"{label}:{node.lineno} string launcher {m.group(0)[:40]!r}")
        for m in _JSON_ARGV_IN_TEXT.finditer(text):
            if not (m.group("pre") or "").strip().startswith('"-P"') and '"-P"' not in (m.group("pre") or ""):
                hits.append(f"{label}:{node.lineno} JSON argv {m.group(0)[:40]!r}")
    return hits


def test_source_has_no_unsafe_interpreter_launch():
    """See :func:`_unsafe_interpreter_launches` for what this can and cannot
    see; every exception is in ``_BY_DESIGN`` with its reason."""
    hits: list[str] = []
    for root in _SCAN_ROOTS:
        for path in _py_files(root) if root.is_dir() else []:
            skip = frozenset(_BY_DESIGN.get(path.name, {}))
            hits += [h for h in _unsafe_interpreter_launches(path, skip)
                     if not any(path.name == f and snip in h for f, snip, _ in _ALLOWED_STRINGS)]
    # text templates and generated launchers
    for root in (REPO / "plugins", SRC / "data"):
        for path in root.rglob("*") if root.is_dir() else []:
            if path.is_file() and path.suffix in (".json", ".toml", ".js", ".mjs", ".sh"):
                for m in re.finditer(r"\bpython[\w.]*((?:\s+-\w+)*)\s+-[mc]\b",
                                     path.read_text(errors="ignore")):
                    if not re.search(r"-[PI]\b", m.group(1)):
                        hits.append(f"{path.relative_to(REPO)}: {m.group(0)}")
    assert hits == [], "unsafe interpreter launch (cwd on sys.path): " + "; ".join(hits)


def test_generated_agy_launcher_is_safe(tmp_path, monkeypatch):
    from ctx import agysdk

    monkeypatch.setattr(agysdk, "launcher_path", lambda: tmp_path / "ctx-agy")
    text = agysdk._write_launcher().read_text()
    assert " -P -c " in text


_SCAN_CASES = {
    "list -m": ("import sys\nx=[sys.executable,'-m','ctx']\n", True),
    "list -c": ("import sys\nx=[sys.executable,'-c','pass']\n", True),
    "list stdin": ("import sys\nx=[sys.executable,'-']\n", True),
    "list rel script": ("import sys\nx=[sys.executable,'tool.py']\n", True),
    "fstring exec": ("x=f'exec {py} -c {boot}'\n", True),
    "fstring -m ctx": ("import sys\nx=f'{sys.executable} -m ctx hook'\n", True),
    "concat": ("import sys\nx=sys.executable+' -m ctx'\n", True),
    "shlex.join": ("import sys,shlex\nx=shlex.join([sys.executable,'-m','ctx'])\n", True),
    "star list": ("import sys\nx=[*sys.executable.split(),'-m','ctx']\n", True),
    "literal": ("x='python -m ctx hook claude-code pre-tool-use'\n", True),
    "format": ("import sys\nx='{} -m ctx'.format(sys.executable)\n", True),
    "percent": ("import sys\nx='%s -m ctx' % sys.executable\n", True),
    "shell launcher": ("import sys\ns=f'#!/bin/sh\\nexec {sys.executable} -c \"import x\"\\n'\n", True),
    "js argv": ("import sys\ns='const a = [\"'+sys.executable+'\", \"-m\", \"ctx\"]'\n", True),
    "python3 list": ("x=['python3','-m','ctx']\n", True),
    "tuple": ("import sys\nx=(sys.executable,'-m','ctx')\n", True),
    "vpy": ("x=[str(vpy),'-m','pip','install']\n", True),
    "safe -P list": ("import sys\nx=[sys.executable,'-P','-m','ctx']\n", False),
    "safe -P string": ("import sys\nx=f'{sys.executable} -P -m ctx'\n", False),
    "safe -I -c": ("import sys\nx=[sys.executable,'-I','-c','pass']\n", False),
    "bash -c": ("x=['bash','-c','echo']\n", False),
    "git -c": ("x=['git','-c','a=b','log']\n", False),
}


def test_exemptions_are_per_function_not_per_file(tmp_path):
    """A launch planted in an exempted FILE, outside its exempted functions,
    is still caught."""
    for name, funcs in _BY_DESIGN.items():
        copy = tmp_path / name
        copy.write_text((SRC / name).read_text(encoding="utf-8")
                        + "\n\ndef _planted_launch():\n"
                          "    import sys\n    return [sys.executable, '-m', 'ctx']\n")
        hits = _unsafe_interpreter_launches(copy, frozenset(funcs))
        assert any("sys.executable -m ctx" in h for h in hits), (name, hits)
        assert all(" -m ctx" in h for h in hits), (name, hits)   # nothing else leaks


@pytest.mark.parametrize("name", sorted(_SCAN_CASES))
def test_scan_catches_each_unsafe_form(tmp_path, name):
    """The scanner itself, on a probe file in a temporary directory only."""
    src, unsafe = _SCAN_CASES[name]
    probe = tmp_path / "probe.py"
    probe.write_text(src)
    assert bool(_unsafe_interpreter_launches(probe)) is unsafe, _unsafe_interpreter_launches(probe)


def test_rendered_host_configs_have_no_unsafe_launch(monkeypatch, tmp_path, workspace_dir):
    from ctx import mcp_hosts, native_hooks
    from ctx.installer import (_render_codex_file, claude_hook_settings,
                               claude_statusline_setting, render_plugin)

    monkeypatch.setattr(selfcmd, "which_absolute", lambda _n, path=None: None)
    exe = selfcmd.ctx_command()
    blobs = [json.dumps(claude_hook_settings(exe)), json.dumps(claude_statusline_setting(exe)),
             _render_codex_file("hooks.json", exe), _render_codex_file("config.toml", exe),
             _render_codex_file("AGENTS.md", exe)]
    blobs += [mcp_hosts.render_config(h, workspace_dir) for h in mcp_hosts.HOSTS]
    blobs += [native_hooks.render(h) for h in mcp_hosts.HOSTS]
    plugin = render_plugin(tmp_path, None)
    blobs += [p.read_text() for p in plugin.rglob("*.json")]
    for blob in blobs:
        toks = re.findall(r"[^\s\"',\[\]=]+", blob)
        for i, tok in enumerate(toks):
            if tok == "-m" and i + 1 < len(toks) and toks[i + 1].startswith("ctx"):
                assert toks[i - 1] in ("-P", "-I"), blob[:300]
