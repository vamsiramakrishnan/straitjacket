"""The CLI dispatch layer: the table, its laziness, and the handlers that
became callable in isolation once the if/elif chain was broken up.

test_cliux.py already holds the parser and the help surface to each other.
These tests add the third side of that triangle — the dispatch table — plus
the property that made the old inlined chain worth keeping:

  the table must stay lazy. Importing handlers at cli.py's module scope
  would put all 34 commands' dependencies on every invocation, including
  the hook fast path, which is the reason the bodies were inlined at all.
"""

import ast
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"


# ------------------------------------------------------------- the table
def _parser_commands() -> set[str]:
    import argparse

    from ctx.cli import _build_parser

    parser = _build_parser()
    top = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return set(top.choices)


def test_table_and_parser_describe_the_same_commands():
    """A parser entry with no table row is a crash at dispatch; a row with no
    parser entry is dead weight."""
    from ctx import cli

    assert set(cli._COMMANDS) == _parser_commands()


def test_table_and_help_surface_describe_the_same_commands():
    """The third side of the triangle test_cliux.py pins two sides of: adding
    a command means a parser block, a cliux line, and a table row."""
    from ctx import cli, cliux

    assert set(cli._COMMANDS) == set(cliux.all_commands())


def test_every_table_row_resolves_to_a_callable():
    from ctx import cli

    for name in cli._COMMANDS:
        handler, wants_workspace = cli._handler_for(name)
        assert callable(handler), name
        assert isinstance(wants_workspace, bool)


def test_unknown_command_resolves_to_nothing():
    from ctx import cli

    assert cli._handler_for("no-such-command") == (None, False)


def test_workspace_free_commands_are_exactly_the_self_resolving_ones():
    """wrap and antigravity resolve their own workspace (`--print-config`
    must work outside one); proxy and replay need none at all. Everything
    else — orchestrate included — gets its workspace from the dispatcher."""
    from ctx import cli

    free = {n for n, (_, _, ws) in cli._COMMANDS.items() if not ws}
    assert free == {"setup", "wrap", "antigravity", "proxy", "replay"}


# --------------------------------------------------------- the front door
def test_front_door_answers_bare_and_help_invocations(capsys):
    from ctx.cli import _front_door

    assert _front_door([]) == 0
    assert "Getting started:" in capsys.readouterr().out
    assert _front_door(["help"]) == 0
    assert "Getting started:" in capsys.readouterr().out


def test_front_door_lets_per_command_help_through_to_argparse(capsys):
    """`ctx run --help` must reach argparse, not the curated screen."""
    from ctx.cli import _front_door

    assert _front_door(["run", "--help"]) is None
    assert capsys.readouterr().out == ""


def test_front_door_suggests_instead_of_dispatching(capsys):
    from ctx.cli import _front_door

    assert _front_door(["serach", "foo"]) == 2
    assert "search" in capsys.readouterr().err


def test_front_door_passes_real_commands_through():
    from ctx import cli

    for name in cli._COMMANDS:
        assert cli._front_door([name]) is None, name


# ------------------------------------------------------------- laziness
def _sys_modules_after(stmt):
    out = subprocess.run(
        [sys.executable, "-c",
         f"import sys\n{stmt}\n"
         "print('\\n'.join(m for m in sys.modules if m.startswith('ctx')))"],
        capture_output=True, text=True, check=True,
        env={"PYTHONPATH": str(SRC), "PATH": "/usr/bin:/bin"},
    )
    return set(out.stdout.split())


def test_importing_the_cli_imports_no_command_module():
    """cli.py holds a table of names, not of functions. If this fails, every
    `ctx` invocation — the hook fast path included — just started paying for
    all 34 commands' imports."""
    loaded = _sys_modules_after("import ctx.cli")
    assert not [m for m in loaded if m.startswith("ctx.commands")]


def test_dispatching_one_command_imports_only_its_own_module():
    loaded = _sys_modules_after("import ctx.cli; ctx.cli._handler_for('gain')")
    families = {m for m in loaded if m.startswith("ctx.commands.")}
    assert families == {"ctx.commands.admin"}
    assert "ctx.commands.execute" not in loaded
    assert "ctx.commands.plans" not in loaded


@pytest.mark.parametrize(
    "path", sorted((SRC / "ctx" / "commands").glob("*.py")), ids=lambda p: p.name
)
def test_command_modules_keep_their_dependencies_inside_the_functions(path):
    """A handler module groups a whole family, so a module-scope
    `from ctx.jobs import …` would make `ctx jobs` pay for `ctx run`. The
    only module-scope imports allowed are __future__, sys, and the shared
    emission helpers (which have no module-scope imports of their own)."""
    allowed = {"__future__", "sys", "ctx.commands.emit"}
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders = []
    for node in tree.body:  # module scope only
        if isinstance(node, ast.Import):
            offenders += [a.name for a in node.names if a.name not in allowed]
        elif isinstance(node, ast.ImportFrom) and node.module not in allowed:
            offenders.append(node.module)
    assert not offenders, f"{path.name}: module-scope imports {offenders}"


# ------------------------------------------------- wrap, in isolation
# `ctx wrap` was ~150 lines inlined in the chain, so none of its REMAINDER
# handling could be reached without launching a host. It is a function now.
class _Recorder:
    """Stands in for ctx.wrap's launchers; records the call, launches nothing."""

    def __init__(self):
        self.calls = []

    def __call__(self, name):
        def fn(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return 0
        return fn


@pytest.fixture()
def wrap_ns():
    from argparse import Namespace

    def make(host, agent_args=(), print_config=False, workspace=None):
        return Namespace(host=host, agent_args=list(agent_args),
                         print_config=print_config, workspace=workspace)

    return make


@pytest.fixture()
def fake_hosts(monkeypatch):
    """Patch the launchers where cmd_wrap imports them from (ctx.wrap), which
    only works because the import happens at call time."""
    import ctx.wrap as wrap_mod

    rec = _Recorder()
    for name in ("guided_setup", "wrap_setup", "wrap_codex", "wrap_antigravity",
                 "wrap_claude", "wrap_detect"):
        monkeypatch.setattr(wrap_mod, name, rec(name))
    return rec


def test_wrap_print_config_needs_no_workspace(tmp_path, monkeypatch, capsys):
    """--print-config must work from a directory that is not a workspace:
    cmd_wrap returns before it resolves one."""
    from argparse import Namespace

    from ctx.commands.hosts import cmd_wrap

    monkeypatch.chdir(tmp_path)
    ns = Namespace(host="claude", agent_args=[], print_config=True, workspace=None)
    assert cmd_wrap(ns) == 0
    assert "hook claude-code pre-tool-use" in capsys.readouterr().out


def test_wrap_recognizes_print_config_after_the_host_positional(wrap_ns, capsys):
    """argparse.REMAINDER swallows options placed after `host`; cmd_wrap
    re-recognizes --print-config there."""
    from ctx.commands.hosts import cmd_wrap

    assert cmd_wrap(wrap_ns("claude", ["--print-config"])) == 0
    assert "hook claude-code pre-tool-use" in capsys.readouterr().out


def test_wrap_ignores_flags_past_the_double_dash(wrap_ns, fake_hosts, workspace_dir,
                                                 state_home, monkeypatch):
    """`ctx wrap claude -- -p "…--orchestrate…"` passes the flag to the agent;
    it must not be read as ours."""
    from ctx.commands.hosts import cmd_wrap

    monkeypatch.chdir(workspace_dir)
    ns = wrap_ns("claude", ["--", "-p", "--print-config", "--proxy", "--orchestrate"],
                 workspace=str(workspace_dir))
    assert cmd_wrap(ns) == 0
    name, args, kwargs = fake_hosts.calls[-1]
    assert name == "wrap_claude"
    assert args[1] == ["-p", "--print-config", "--proxy", "--orchestrate"]
    assert kwargs["use_proxy"] is False
    assert kwargs["orchestrate"] is False


def test_wrap_orchestrate_is_consumed_and_forwarded(wrap_ns, fake_hosts, workspace_dir,
                                                    state_home, monkeypatch):
    """Orchestration is a wrap mode, so it rides in before the `--` and never
    reaches the agent's own argv."""
    from ctx.commands.hosts import cmd_wrap

    monkeypatch.chdir(workspace_dir)
    ns = wrap_ns("claude", ["--orchestrate", "--", "-p", "hi"],
                 workspace=str(workspace_dir))
    assert cmd_wrap(ns) == 0
    name, args, kwargs = fake_hosts.calls[-1]
    assert name == "wrap_claude"
    assert args[1] == ["-p", "hi"]
    assert kwargs["orchestrate"] is True


def test_wrap_rescue_pct_is_consumed_and_implies_the_proxy(
        wrap_ns, fake_hosts, workspace_dir, state_home, monkeypatch):
    from ctx.commands.hosts import cmd_wrap

    monkeypatch.chdir(workspace_dir)
    ns = wrap_ns("claude", ["--rescue-pct", "70", "--", "-p", "hi"],
                 workspace=str(workspace_dir))
    assert cmd_wrap(ns) == 0
    _, args, kwargs = fake_hosts.calls[-1]
    assert args[1] == ["-p", "hi"]  # the flag and its value are consumed
    assert kwargs["rescue_pct"] == 70.0
    assert kwargs["use_proxy"] is True  # rescue implies the proxy


def test_wrap_rescue_pct_that_is_not_a_number_degrades_to_zero(
        wrap_ns, fake_hosts, workspace_dir, state_home, monkeypatch):
    from ctx.commands.hosts import cmd_wrap

    monkeypatch.chdir(workspace_dir)
    ns = wrap_ns("claude", ["--rescue-pct", "lots", "--", "-p", "hi"],
                 workspace=str(workspace_dir))
    assert cmd_wrap(ns) == 0
    _, _, kwargs = fake_hosts.calls[-1]
    assert kwargs["rescue_pct"] == 0.0
    assert kwargs["use_proxy"] is True


@pytest.mark.parametrize("host,expected,kw", [
    ("setup", "wrap_setup", {"force_all": False}),
    ("all", "wrap_setup", {"force_all": True}),
    ("codex", "wrap_codex", {}),
    ("antigravity", "wrap_antigravity", {}),
    ("detect", "wrap_detect", {"probe_version": False}),
])
def test_wrap_routes_each_host_to_its_own_setup(host, expected, kw, wrap_ns, fake_hosts,
                                                workspace_dir, state_home, monkeypatch):
    from ctx.commands.hosts import cmd_wrap

    monkeypatch.chdir(workspace_dir)
    assert cmd_wrap(wrap_ns(host, workspace=str(workspace_dir))) == 0
    name, _, kwargs = fake_hosts.calls[-1]
    assert [c[0] for c in fake_hosts.calls] == [expected]
    assert kwargs == kw


def test_wrap_detect_probes_versions_on_request(wrap_ns, fake_hosts, workspace_dir,
                                                state_home, monkeypatch):
    from ctx.commands.hosts import cmd_wrap

    monkeypatch.chdir(workspace_dir)
    assert cmd_wrap(wrap_ns("detect", ["--versions"],
                            workspace=str(workspace_dir))) == 0
    assert fake_hosts.calls[-1][2] == {"probe_version": True}


def test_wrap_claude_without_agent_args_installs_persistently(
        wrap_ns, fake_hosts, workspace_dir, state_home, monkeypatch, capsys):
    """The claude asymmetry: agent args mean an ephemeral launch, no agent
    args mean a persistent install."""
    from ctx.commands.hosts import cmd_wrap

    monkeypatch.chdir(workspace_dir)
    assert cmd_wrap(wrap_ns("claude", workspace=str(workspace_dir))) == 0
    name, _, kwargs = fake_hosts.calls[-1]
    assert name == "guided_setup"  # verified persistent path; nothing launched
    assert kwargs == {"hosts": ["claude"]}
    assert "now harnessed" not in capsys.readouterr().out


# --------------------------------------- other newly-isolatable handlers
def test_cmd_orchestrate_passes_the_exit_code_through(workspace_dir, monkeypatch,
                                                      capsys):
    from argparse import Namespace

    import ctx.orchestrator as orch
    from ctx.commands.hosts import cmd_orchestrate
    from ctx.workspace import resolve_workspace

    seen = {}

    def fake(ws, task, *, dry_run, force_run):
        seen.update(task=task, dry_run=dry_run, force_run=force_run)
        return 7, "the routing plan"

    monkeypatch.setattr(orch, "orchestrate", fake)
    ws = resolve_workspace(str(workspace_dir))
    ns = Namespace(task="do the thing", dry_run=True, force_run=False)
    assert cmd_orchestrate(ws, ns) == 7
    assert capsys.readouterr().out.strip() == "the routing plan"
    assert seen == {"task": "do the thing", "dry_run": True, "force_run": False}


def test_cmd_debt_add_list_resolve_round_trip(workspace_dir, capsys):
    from argparse import Namespace

    from ctx.commands.admin import cmd_debt
    from ctx.workspace import resolve_workspace

    ws = resolve_workspace(str(workspace_dir))
    assert cmd_debt(ws, Namespace(debt_cmd="add", note="defer the cache",
                                  ref="repo:src/x.py:1")) == 0
    eid = capsys.readouterr().out.split("declared:")[1].strip()

    assert cmd_debt(ws, Namespace(debt_cmd="list")) == 0
    assert "defer the cache" in capsys.readouterr().out

    assert cmd_debt(ws, Namespace(debt_cmd="resolve", id=eid)) == 0
    assert capsys.readouterr().out.strip() == "resolved"
    assert cmd_debt(ws, Namespace(debt_cmd="resolve", id="nope")) == 1
    assert "unknown debt id" in capsys.readouterr().out


def test_cmd_stats_session_without_wire_observations(workspace_dir, capsys):
    """The --session branch used to be inlined inside the stats branch."""
    from argparse import Namespace

    from ctx.commands.retrieve import cmd_stats
    from ctx.workspace import resolve_workspace

    ws = resolve_workspace(str(workspace_dir))
    assert cmd_stats(ws, Namespace(session=True)) == 1
    assert "no wire observations" in capsys.readouterr().out


def test_cmd_replay_needs_transcripts(capsys):
    from argparse import Namespace

    from ctx.commands.history import cmd_replay

    ns = Namespace(transcripts=[], all_projects=False, replay_outcomes=False,
                   replay_json=False, replay_regret=False, gaps=False,
                   workspace=None)
    assert cmd_replay(ns) == 1
    assert "no transcripts given" in capsys.readouterr().out


def test_cmd_checkpoint_requires_a_goal(workspace_dir, state_home, capsys):
    from argparse import Namespace

    from ctx.commands.admin import cmd_checkpoint
    from ctx.workspace import resolve_workspace

    ws = resolve_workspace(str(workspace_dir))
    assert cmd_checkpoint(ws, Namespace(show=None, goal=None)) == 2
    assert "--goal is required" in capsys.readouterr().err


def test_orchestrate_resume_needs_no_task_positional():
    """`ctx orchestrate --resume <task>` is the documented replay invocation;
    the task positional is optional so it parses without a dummy task."""
    from ctx.cli import _build_parser

    parser = _build_parser()
    ns = parser.parse_args(["orchestrate", "--resume", "task-000000000000"])
    assert ns.task == "" and ns.resume == "task-000000000000"
    ns = parser.parse_args(["orchestrate", "do the thing"])
    assert ns.task == "do the thing" and ns.resume is None
