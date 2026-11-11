"""Installs written by pre-fix ctx (``python -m ctx``, which imports ``ctx``
from the host's working directory) are found by ``ctx doctor`` in every file
any installer writes, and in the hosts' own settings; ``ctx setup --host H``
really rewrites what doctor says it rewrites; and for entries ctx does not
own, doctor prints the exact replacement and setup leaves the file alone."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

import ctx.selfcmd as selfcmd
from ctx import launchform

OLD = [sys.executable, "-m", "ctx"]
#: The real home, recorded at import (before any fixture patches HOME).
_REAL_HOME = Path(__import__("pwd").getpwuid(os.getuid()).pw_dir).resolve()
#: Every host `ctx setup` knows, from the registry: a new host is covered
#: without editing this file.
from ctx.installer import SETUP_HOSTS as _SETUP_HOSTS  # noqa: E402

HOSTS = tuple(_SETUP_HOSTS)


def _with_old_form(fn, *args):
    """Run an installer writing the pre-fix command form. Scoped patch:
    `monkeypatch.undo()` would also undo conftest's isolated HOME."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(selfcmd, "ctx_argv", lambda: list(OLD))
        return fn(*args)


_BASETEMP: list[Path] = []


@pytest.fixture()
def ws(workspace_dir, state_home, tmp_path_factory):
    _BASETEMP[:] = [tmp_path_factory.getbasetemp().resolve()]
    return workspace_dir


def _setup(ws: Path, host: str) -> subprocess.CompletedProcess:
    """The real `ctx setup --host <host>`, as a user would run it — with the
    test's isolated HOME and state dir (asserted, never the real ones)."""
    env = {**os.environ}
    home = Path(env["HOME"]).resolve()
    assert home != _REAL_HOME and _BASETEMP[0] in home.parents, home
    assert env.get("CTX_STATE_HOME")
    return subprocess.run([*selfcmd.ctx_argv(), "--workspace", str(ws), "setup", "--host", host],
                          cwd=ws, env=env, capture_output=True, text=True, timeout=120)


def _findings(ws: Path, host: str):
    return [f for f in launchform.scan(ws) if f.location.host == host]


def _doctor_rows(ws: Path) -> list[tuple[str, bool, str]]:
    from ctx.installer import doctor_checks
    from ctx.workspace import resolve_workspace

    return [r for r in doctor_checks(resolve_workspace(str(ws))) if r[0] == "ctx launch form"]


def _install_old(ws: Path, host: str) -> None:
    from ctx.workspace import resolve_workspace

    from ctx.hosts import host_by_name, installer_for

    w = resolve_workspace(str(ws))
    if host in ("codex", "opencode"):
        # The ledger path a current setup takes for these two hosts.
        from ctx.host_config import plan_configuration
        plan_configuration(ws, [host]).apply()
        if host == "opencode":
            from ctx.native_hooks import install
            install("opencode", ws)
        return
    installer_for(host_by_name(host))(w)   # whatever the registry names


@pytest.mark.parametrize("host", HOSTS)
def test_old_install_is_flagged_then_setup_rewrites_it(ws, host):
    _with_old_form(_install_old, ws, host)   # from here on: the fixed ctx
    before = _findings(ws, host)
    assert before, f"the old {host} install must contain an unsafe command"
    rows = _doctor_rows(ws)
    assert rows and all(not ok for _, ok, _ in rows)
    for f in before:
        assert f.fixed_by_setup, f.message(ws)
        assert f"`ctx setup --host {host}` rewrites it" in f.message(ws)
    out = _setup(ws, host)
    after = _findings(ws, host)
    assert after == [], (out.stdout[-2000:], out.stderr[-2000:], [f.message(ws) for f in after])
    rows = _doctor_rows(ws)
    assert rows == [("ctx launch form", True,
                     "no installed command imports ctx from the working directory")]


def test_setup_says_it_upgraded_in_one_line(ws):
    _with_old_form(_install_old, ws, "omp")
    out = _setup(ws, "omp")
    lines = [l for l in (out.stdout + out.stderr).splitlines()
             if "from the unsafe `python -m ctx` form" in l]
    # one line per file changed, each naming its file
    assert len(lines) == 2
    assert any(".omp/mcp.json" in l for l in lines)
    assert any(".omp/hooks/pre/straitjacket.js" in l for l in lines)


def test_owned_entry_is_upgraded_even_when_setup_refuses_the_rest(ws):
    _with_old_form(_install_old, ws, "omp")
    path = ws / ".omp" / "mcp.json"
    doc = json.loads(path.read_text())
    doc["mcpServers"]["ctx-harness"]["env"] = {"X": "user-added"}  # setup will refuse this
    path.write_text(json.dumps(doc, indent=2) + "\n")
    out = _setup(ws, "omp")
    assert out.returncode != 0
    doc = json.loads(path.read_text())
    assert doc["mcpServers"]["ctx-harness"]["args"][:3] == ["-P", "-m", "ctx"]
    assert doc["mcpServers"]["ctx-harness"]["env"] == {"X": "user-added"}


@pytest.mark.parametrize("where", ["settings.local", "mcp.json", "user-settings", "user-claude-json"])
def test_unowned_entries_get_exact_replacement_and_setup_leaves_them(ws, where):
    home = Path(os.environ["HOME"])
    cmd = shlex.join([*OLD, "hook", "claude-code", "pre-tool-use"])
    files = {
        "settings.local": (ws / ".claude" / "settings.local.json",
                           {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
                               {"type": "command", "command": cmd}]}]}}),
        "mcp.json": (ws / ".mcp.json", {"mcpServers": {"mine": {
            "command": OLD[0], "args": ["-m", "ctx", "mcp"]}}}),
        "user-settings": (home / ".claude" / "settings.json",
                          {"statusLine": {"type": "command",
                                          "command": shlex.join([*OLD, "statusline", "claude-code"])}}),
        "user-claude-json": (home / ".claude.json", {"mcpServers": {"ctx-harness": {
            "command": OLD[0], "args": ["-m", "ctx", "mcp", "--bounded-only"]}}}),
    }
    path, doc = files[where]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n")
    before = path.read_bytes()
    found = [f for f in launchform.scan(ws) if f.location.path == path]
    assert len(found) == 1
    msg = found[0].message(ws)
    assert "ctx does not manage this entry; " in msg
    assert "rerun" not in msg and "ctx setup" not in msg
    # the edit in the entry's own shape: one command string, or command + args
    assert (shlex.join([OLD[0], "-P", *OLD[1:]]) in msg
            or ('"args": ["-P", "-m", "ctx"' in msg and json.dumps(OLD[0]) in msg))
    _setup(ws, "claude")
    assert path.read_bytes() == before


def test_registry_is_derived_from_the_installers(ws):
    from ctx import mcp_hosts, native_hooks
    from ctx.installer import SETUP_HOSTS, _GATEWAY_FILES, _PLUGIN_FILES

    locs = launchform.locations(ws)
    paths = {l.path for l in locs}
    managed_hosts = {l.host for l in locs if l.managed}
    assert set(SETUP_HOSTS) <= managed_hosts
    for host, rel in mcp_hosts.FILES.items():
        if host != "opencode":
            assert ws / rel in paths
    for rel in native_hooks.PATHS.values():
        assert ws / rel in paths
    for name in _PLUGIN_FILES:
        assert ws / ".agents" / "plugins" / "ctx-harness" / name in paths
    for rel in _GATEWAY_FILES.values():
        assert ws / rel in paths


SENTINEL = "/opt/SENTINEL-python"


@pytest.mark.parametrize("host", HOSTS)
def test_every_file_an_installer_writes_a_command_into_is_read(ws, monkeypatch, host):
    """Install with a sentinel interpreter, then every file under the
    workspace and HOME that contains it must be a registry location."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(selfcmd, "ctx_argv", lambda: [SENTINEL, "-P", "-m", "ctx"])
        _install_old(ws, host)
    paths = {l.path.resolve() for l in launchform.locations(ws)}
    home = Path(os.environ["HOME"])
    written = []
    for base in (ws, home):
        for p in base.rglob("*"):
            if p.is_file() and SENTINEL.encode() in p.read_bytes() and p.name != "host-config.json":
                written.append(p.resolve())
    assert written
    assert [p for p in written if p not in paths] == []


# ------------------------------------------------------------------ item 4/5/6
@pytest.mark.parametrize("cmd,expect", [
    ("python -m ctx", "unsafe"), ("python -P -m ctx", "safe"), ("python -I -m ctx", "safe"),
    ("python -IP -m ctx", "safe"), ("python -Pm ctx", "safe"), ("python -mctx", "unsafe"),
    ("python -X dev -m ctx", "unsafe"), ("python -W x -m ctx", "unsafe"),
    ("python -Wignore::PendingDeprecationWarning -m ctx", "unsafe"),
    ("python -X pycache_prefix=P -m ctx", "unsafe"), ("python -X importtime -P -m ctx", "safe"),
    ("python -E -s -m ctx", "unsafe"), ("python -m ctx.hook", "unsafe"),
    ("python -m pytest", None), ("python -c 'import ctx'", None), ("python -- -m ctx", None),
])
def test_detection_is_conservative(cmd, expect):
    hit = selfcmd.python_m_ctx(shlex.split(cmd))
    got = None if hit is None else ("safe" if hit[1] else "unsafe")
    assert got == expect


def test_rewrite_is_exact():
    assert selfcmd.make_safe(["python", "-m", "ctx", "q"]) == ["python", "-P", "-m", "ctx", "q"]
    for argv in (["python", "-X", "dev", "-m", "ctx"], ["python", "-mctx"], ["python", "-Pm", "ctx"]):
        assert selfcmd.make_safe(argv) == argv


def test_path_hit_inside_the_workspace_is_refused(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "bin").mkdir(parents=True)
    fake = repo / "bin" / "ctx"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    outside = tmp_path / "tools"
    outside.mkdir()
    real = outside / "ctx"
    real.write_text("#!/bin/sh\n")
    real.chmod(0o755)
    monkeypatch.chdir(repo)
    path = os.pathsep.join([str(repo / "bin"), str(outside)])
    assert selfcmd.which_absolute("ctx", path=path) == str(real)
    assert selfcmd.which_absolute("ctx", path=str(repo / "bin")) is None


def test_mixed_group_rewrites_only_the_ctx_command(ws):
    from ctx.installer import upgrade_unsafe_ctx_commands

    ctx_cmd = shlex.join([*OLD, "hook", "claude-code", "pre-tool-use"])
    user_cmd = f"{shlex.quote(OLD[0])} -m mytool check"
    hooks = {"PreToolUse": [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": ctx_cmd}, {"type": "command", "command": user_cmd}]}]}
    notes = upgrade_unsafe_ctx_commands(hooks)
    assert notes and notes[0].startswith("updated 1 ctx command(s) in .claude/settings.json")
    group = hooks["PreToolUse"][0]["hooks"]
    assert " -P -m ctx hook" in group[0]["command"] and group[1]["command"] == user_cmd


# ------------------------------------------------------------------ safe writes
def _old_omp(ws: Path) -> Path:
    _with_old_form(_install_old, ws, "omp")
    return ws / ".omp" / "mcp.json"


def test_rewrite_never_writes_through_a_planted_temp_symlink(ws, tmp_path):
    path = _old_omp(ws)
    victim = tmp_path / "victim"
    victim.write_text("precious\n")
    (path.parent / f".{path.name}.ctx-upgrade").symlink_to(victim)
    lines = launchform.upgrade(ws, ["omp"])
    assert victim.read_text() == "precious\n"
    assert not path.is_symlink() and '"-P"' in path.read_text()
    assert any(".omp/mcp.json" in l and l.startswith("updated") for l in lines)


def test_rewrite_keeps_the_files_mode(ws):
    path = _old_omp(ws)
    path.chmod(0o640)
    launchform.upgrade(ws, ["omp"])
    assert (path.stat().st_mode & 0o777) == 0o640 and '"-P"' in path.read_text()


@pytest.mark.parametrize("case", ["target-symlink", "dir-symlink-outside", "read-only"])
def test_rewrite_refuses_and_doctor_gives_the_manual_edit(ws, tmp_path, case):
    path = _old_omp(ws)
    outside = tmp_path / "outside"
    outside.mkdir()
    if case == "target-symlink":
        real = outside / "mcp.json"
        real.write_text(path.read_text())
        path.unlink()
        path.symlink_to(real)
        guarded = real
    elif case == "dir-symlink-outside":
        import shutil
        shutil.move(str(ws / ".omp"), str(outside / "omp"))
        (ws / ".omp").symlink_to(outside / "omp")
        guarded = outside / "omp" / "mcp.json"
    else:
        path.chmod(0o444)
        guarded = path
    before = guarded.read_bytes()
    lines = launchform.upgrade(ws, ["omp"])
    assert guarded.read_bytes() == before
    assert any(l.startswith("left .omp/mcp.json unchanged") for l in lines), lines
    f = next(f for f in launchform.scan(ws) if f.location.path == ws / ".omp" / "mcp.json")
    assert not f.fixed_by_setup
    msg = f.message(ws)
    assert "setup will not rewrite it" in msg and '"args": ["-P", "-m", "ctx"' in msg


def test_claude_installer_never_writes_through_a_symlinked_settings_file(ws, tmp_path):
    from ctx.installer import install_claude
    from ctx.workspace import resolve_workspace

    outside = tmp_path / "outside.json"
    outside.write_text("{}\n")
    (ws / ".claude").mkdir()
    (ws / ".claude" / "settings.json").symlink_to(outside)
    report = install_claude(resolve_workspace(str(ws)))
    assert outside.read_text() == "{}\n"
    assert "was not written" in report


# ------------------------------------------------------------------ items 3, 4
def test_planted_ctx_on_path_is_refused_from_a_subdirectory(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "bin").mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / "sub" / "deeper").mkdir(parents=True)
    fake = repo / "bin" / "ctx"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.chdir(repo / "sub" / "deeper")
    assert selfcmd.which_absolute("ctx", path=str(repo / "bin")) is None


def test_mcp_json_replacement_has_the_entrys_shape(ws):
    (ws / ".mcp.json").write_text(json.dumps({"mcpServers": {"ctx-harness": {
        "command": OLD[0], "args": ["-m", "ctx", "mcp", "--bounded-only"]}}}))
    f = next(f for f in launchform.scan(ws) if f.location.path == ws / ".mcp.json")
    msg = f.message(ws)
    want = (f'set "command": {json.dumps(OLD[0])}, "args": '
            '["-P", "-m", "ctx", "mcp", "--bounded-only"]')
    assert want in msg
