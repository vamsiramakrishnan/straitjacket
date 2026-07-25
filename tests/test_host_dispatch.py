"""HostSpec.installer / HostSpec.wrapper actually dispatch (R1).

Both fields were set on every wired host and read by nothing —
``harnessable`` truth-tested ``wrapper`` and that was the whole of it.
Meanwhile the branching they were designed to replace existed in three
places:

* ``ctx.installer._HOST_INSTALLERS`` — a second name→installer table,
  hand-maintained alongside the registry;
* ``ctx.installer.SETUP_HOSTS`` — a second list of the wired hosts;
* ``ctx.commands.hosts.cmd_wrap`` — two ``if host == ...`` chains selecting
  a wrapper, plus a third hardcoded host tuple in the ``--gateway`` path.

So the fields were wired up rather than deleted. These tests pin that the
duplicate tables are gone and that dispatch really goes through the
registry: a host added to the registry alone reaches its wrapper without
any edit to the command layer.
"""

from __future__ import annotations

from argparse import Namespace

import pytest

from ctx import hosts


def test_every_wired_host_resolves_both_fields_to_a_callable():
    for spec in hosts.harnessable_hosts():
        assert callable(hosts.installer_for(spec)), spec.name
        assert callable(hosts.wrapper_for(spec)), spec.name


def test_unwired_hosts_resolve_to_none():
    gemini = hosts.host_by_name("gemini")
    assert gemini is not None and not gemini.harnessable
    assert hosts.installer_for(gemini) is None
    assert hosts.wrapper_for(gemini) is None


def test_setup_hosts_is_derived_from_the_registry():
    """It used to be a hand-written tuple that could drift from the registry."""
    from ctx.installer import SETUP_HOSTS

    assert tuple(SETUP_HOSTS) == tuple(s.name for s in hosts.harnessable_hosts())


def test_no_second_installer_table_exists():
    import ctx.installer as installer_mod

    assert not hasattr(installer_mod, "_HOST_INSTALLERS"), (
        "the name→installer mapping belongs to HostSpec.installer, not to a "
        "second table in ctx.installer"
    )


def test_setup_hosts_dispatches_through_the_registry(monkeypatch, workspace_dir,
                                                     state_home):
    """setup_hosts must call whatever HostSpec.installer names — not a
    hardcoded function."""
    import ctx.installer as installer_mod
    from conftest import make_ws

    called: list[str] = []

    def fake(ws, *, init_policy=True):
        called.append("antigravity")
        return "faked"

    monkeypatch.setattr(installer_mod, "install_antigravity", fake)
    out = installer_mod.setup_hosts(make_ws(workspace_dir), ["antigravity"])
    assert called == ["antigravity"] and "faked" in out


def _ns(host, agent_args=(), workspace=None):
    return Namespace(host=host, agent_args=list(agent_args),
                     print_config=False, workspace=workspace)


@pytest.mark.parametrize("host,fn", [("codex", "wrap_codex"),
                                     ("antigravity", "wrap_antigravity")])
def test_cmd_wrap_dispatches_via_the_registry(host, fn, monkeypatch,
                                              workspace_dir, state_home):
    import ctx.wrap as wrap_mod
    from ctx.commands.hosts import cmd_wrap

    seen: list[tuple] = []
    monkeypatch.setattr(wrap_mod, fn, lambda *a, **k: seen.append((a, k)) or 0)
    monkeypatch.chdir(workspace_dir)
    assert cmd_wrap(_ns(host, workspace=str(workspace_dir))) == 0
    assert len(seen) == 1


def test_a_registry_only_host_reaches_its_wrapper(monkeypatch, workspace_dir,
                                                  state_home):
    """The load-bearing claim: adding a host is a registry edit. A spec whose
    wrapper names a real ctx.wrap function dispatches with NO change to
    ctx.commands.hosts."""
    import ctx.wrap as wrap_mod
    from ctx.commands.hosts import cmd_wrap

    reached: list[str] = []
    monkeypatch.setattr(wrap_mod, "wrap_newhost",
                        lambda *a, **k: reached.append("yes") or 0, raising=False)
    spec = hosts.HostSpec(
        name="newhost", cli_bins=("newhost",), default_model="x",
        installer="install_antigravity", wrapper="wrap_newhost",
    )
    monkeypatch.setattr(hosts, "_REGISTRY", hosts._REGISTRY + (spec,))
    monkeypatch.chdir(workspace_dir)
    assert cmd_wrap(_ns("newhost", workspace=str(workspace_dir))) == 0
    assert reached == ["yes"]


def test_unknown_host_is_refused_rather_than_silently_wrapped(
        monkeypatch, workspace_dir, state_home, capsys):
    """cmd_wrap used to fall through every branch and run antigravity for any
    unrecognized host — including `ctx wrap gemini`, a host that is detected
    but has no wrapper."""
    import ctx.wrap as wrap_mod
    from ctx.commands.hosts import cmd_wrap

    monkeypatch.setattr(wrap_mod, "wrap_antigravity",
                        lambda *a, **k: pytest.fail("silent antigravity fallback"))
    monkeypatch.chdir(workspace_dir)
    assert cmd_wrap(_ns("gemini", workspace=str(workspace_dir))) != 0
    err = capsys.readouterr().err
    assert "gemini" in err and "antigravity" in err  # names the wired hosts


def test_antigravity_detects_the_agy_binary():
    """The Antigravity CLI installs as `agy`, not `antigravity`. Probing only
    the latter reported the host as missing on machines that had it, so
    `ctx wrap setup` silently skipped harnessing it."""
    from ctx.hosts import detect, host_by_name

    spec = host_by_name("antigravity")
    assert "agy" in spec.cli_bins

    seen = []

    def which(binary):
        seen.append(binary)
        return "/home/u/.local/bin/agy" if binary == "agy" else None

    host = detect(spec, which=which)
    assert host.installed is True
    assert host.path == "/home/u/.local/bin/agy"
    assert seen[0] == "agy"  # the real binary is probed first
