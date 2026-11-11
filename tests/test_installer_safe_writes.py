"""Every installer write refuses to follow a symlink out of the workspace
(or, for a user-scope file, out of the home directory), naming the file,
and directory copies follow no link on either side."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import ctx.selfcmd as selfcmd
from ctx import launchform


@pytest.fixture()
def ws(workspace_dir, state_home):
    return workspace_dir


@pytest.fixture()
def outside(tmp_path):
    d = tmp_path / "outside"
    d.mkdir()
    return d


def _w(ws):
    from ctx.workspace import resolve_workspace

    return resolve_workspace(str(ws))


# ------------------------------------------------------------------ Antigravity
@pytest.mark.parametrize("name", ["hooks.json", "mcp_config.json", "plugin.json"])
def test_plugin_file_symlink_is_refused(ws, outside, name):
    from ctx.installer import install_antigravity

    victim = outside / name
    victim.write_text("precious\n")
    d = ws / ".agents" / "plugins" / "ctx-harness"
    d.mkdir(parents=True)
    (d / name).symlink_to(victim)
    out = install_antigravity(_w(ws))
    assert victim.read_text() == "precious\n"
    assert "cannot install the Antigravity plugin" in out and name in out


def test_plugin_dir_symlinked_outside_is_refused(ws, outside):
    from ctx.installer import install_antigravity

    (ws / ".agents").mkdir()
    (ws / ".agents" / "plugins").symlink_to(outside)
    out = install_antigravity(_w(ws))
    assert list(outside.iterdir()) == [] and "cannot install the Antigravity plugin" in out


def test_skills_copy_never_follows_a_destination_link(ws, outside):
    from ctx.installer import install_antigravity

    d = ws / ".agents" / "plugins" / "ctx-harness"
    d.mkdir(parents=True)
    (outside / "keep.md").write_text("mine\n")
    (d / "skills").symlink_to(outside)
    out = install_antigravity(_w(ws))
    assert sorted(p.name for p in outside.iterdir()) == ["keep.md"]
    assert "skills" in out


def test_copy_tree_refuses_a_link_inside_the_source(tmp_path, outside):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.md").write_text("a")
    (src / "link.md").symlink_to(outside / "x")
    with pytest.raises(launchform.UnsafeWrite, match="symlink"):
        launchform.copy_tree_safely(src, tmp_path / "dst", root=tmp_path)
    assert not (tmp_path / "dst").exists()


def test_status_line_never_writes_through_links(tmp_path, monkeypatch, outside):
    from ctx.installer import antigravity_settings_path, install_antigravity_statusline

    home = Path(os.environ["HOME"])
    path = antigravity_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    victim = outside / "settings.json"
    victim.write_text("{}\n")
    path.symlink_to(victim)
    out = install_antigravity_statusline("/opt/ctx")
    assert victim.read_text() == "{}\n" and "not installed" in out and "settings.json" in out
    path.unlink()
    # ~/.gemini symlinked OUT of the home directory
    import shutil
    shutil.rmtree(home / ".gemini")
    (outside / "gem").mkdir()
    (home / ".gemini").symlink_to(outside / "gem")
    out = install_antigravity_statusline("/opt/ctx")
    assert not (outside / "gem" / "antigravity-cli" / "settings.json").exists()
    assert "not installed" in out


# ------------------------------------------------------------------ gateway
@pytest.mark.parametrize("host,rel", [("claude", ".ctx-surface/gateway.claude.json"),
                                      ("codex", ".ctx-surface/config.codex.gateway.toml"),
                                      ("antigravity", ".ctx-surface/mcp_config.gateway.json"),
                                      ("claude", ".ctx-surface/backends.json")])
def test_gateway_configs_never_written_through_a_link(ws, outside, host, rel):
    from ctx.installer import install_gateway

    victim = outside / "v"
    victim.write_text("precious\n")
    (ws / ".ctx-surface").mkdir()
    (ws / rel).symlink_to(victim)
    out = install_gateway(_w(ws), host, apply=True)
    assert victim.read_text() == "precious\n"
    assert f"refused {rel}" in out


def test_compile_apply_refuses_a_link(ws, outside):
    from ctx import surface_profiles as sp

    (ws / ".mcp.json").write_text(json.dumps({"mcpServers": {"fs": {"command": "fs"}}}))
    (ws / ".ctx-surface").mkdir()
    victim = outside / "v"
    victim.write_text("precious\n")
    (ws / ".ctx-surface" / "mcp.claude.json").symlink_to(victim)
    rep = sp.compile_profile(ws, "full", host="claude", apply=True)
    assert victim.read_text() == "precious\n"
    assert any("refused to write .ctx-surface/mcp.claude.json" in i for i in rep["issues"])


# ------------------------------------------------------------------ Claude files
def test_claude_md_symlink_is_refused(ws, outside):
    from ctx.installer import install_claude

    victim = outside / "CLAUDE.md"
    victim.write_text("precious\n")
    (ws / "CLAUDE.md").symlink_to(victim)
    out = install_claude(_w(ws))
    assert victim.read_text() == "precious\n" and "did not update CLAUDE.md" in out


@pytest.mark.parametrize("rel", ["ctx.toml", ".ctxignore", ".claude/agents/ctx-explorer.md"])
def test_dangling_links_are_never_written_through(ws, outside, rel):
    from ctx.installer import install_claude

    target = ws / rel
    if rel == "ctx.toml":
        target.unlink()
    target.parent.mkdir(parents=True, exist_ok=True)
    dangling = outside / ("dangling-" + target.name)
    target.symlink_to(dangling)
    install_claude(_w(ws))
    assert not dangling.exists()


# ------------------------------------------------------------------ MCP hosts
def test_mcp_host_write_refuses_a_link_by_construction(ws, outside):
    from ctx import mcp_hosts

    victim = outside / "v.json"
    victim.write_text("{}\n")
    (ws / ".omp").mkdir()
    (ws / ".omp" / "mcp.json").symlink_to(victim)
    with pytest.raises(mcp_hosts.IntegrationError, match="Refusing to write .omp/mcp.json"):
        mcp_hosts._write(ws / ".omp" / "mcp.json", {"a": 1}, root=ws)
    assert victim.read_text() == "{}\n"


def test_native_hook_write_refuses_a_link(ws, outside):
    from ctx import mcp_hosts, native_hooks

    victim = outside / "v.js"
    victim.write_text("x")
    path = ws / native_hooks.PATHS["omp"]
    path.parent.mkdir(parents=True)
    path.symlink_to(victim)
    with pytest.raises(mcp_hosts.IntegrationError):
        native_hooks._write_owned(path, "y")
    assert victim.read_text() == "x"


# ------------------------------------------------------------------ boundary
@pytest.mark.parametrize("layout", ["nested-git", "ctx-toml-above-git", "submodule"])
def test_path_hit_anywhere_in_the_enclosing_workspace_is_refused(tmp_path, monkeypatch, layout):
    outer = tmp_path / "outer"
    inner = outer / "inner"
    (inner / "sub").mkdir(parents=True)
    if layout == "nested-git":
        (outer / ".git").mkdir()
        (inner / ".git").mkdir()
    elif layout == "ctx-toml-above-git":
        (outer / "ctx.toml").write_text("version = 1\n")
        (inner / ".git").mkdir()
    else:
        (outer / ".git").mkdir()
        (inner / ".git").write_text("gitdir: ../.git/modules/inner\n")
    (outer / "bin").mkdir()
    fake = outer / "bin" / "ctx"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.chdir(inner / "sub")
    assert selfcmd.workspace_boundary() == os.path.realpath(outer)
    assert selfcmd.which_absolute("ctx", path=str(outer / "bin")) is None


def test_boundary_never_reaches_home_or_above(tmp_path, monkeypatch):
    home = Path(os.environ["HOME"])
    (home / ".git").mkdir(exist_ok=True)
    proj = home / "proj"
    proj.mkdir(exist_ok=True)
    monkeypatch.chdir(proj)
    assert selfcmd.workspace_boundary() == os.path.realpath(proj)
