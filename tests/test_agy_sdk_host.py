"""Acceptance: the ctx-owned Antigravity SDK host.

`antigravity-sdk` is deliberately a *separate* host from `antigravity`. The
latter is Google's `agy` CLI, which ctx only hooks and which the published hook
contract leaves without an output-side gate; the former is ctx's own agent on
the google-antigravity SDK, where containment lives in the tool implementations
and both gates therefore hold.

Conflating them would let one row report two capability sets, and would let a
`ctx wrap` silently substitute our implementation for the vendor's. These tests
pin that separation and the fail-open detection contract.
"""

import pytest

from ctx.hosts import all_hosts, detect, host_by_name, installer_for, wrapper_for

SDK = "antigravity-sdk"


def test_is_a_distinct_host_from_the_vendor_cli():
    sdk, vendor = host_by_name(SDK), host_by_name("antigravity")
    assert sdk is not None and vendor is not None
    assert sdk.name != vendor.name
    # same vendor and models, reached a different way
    assert sdk.vendor_hint == vendor.vendor_hint == "google"
    assert {m.id for m in sdk.models} == {m.id for m in vendor.models}
    # but not the same binary, and not the same capabilities
    assert sdk.cli_bins != vendor.cli_bins
    assert (sdk.input_substitution, sdk.output_substitution) == (True, True)
    assert (vendor.input_substitution, vendor.output_substitution) == (True, False)


def test_capabilities_are_honest_about_why_they_differ():
    """The SDK host claims both gates because it owns its tools, and claims no
    hooks because it has none — not because hooks were forgotten."""
    sdk = host_by_name(SDK)
    assert sdk.supports_hooks is False
    assert sdk.self_hosted is True
    assert sdk.harnessable is True  # harnessable without hooks


def test_installer_and_wrapper_resolve():
    spec = host_by_name(SDK)
    assert installer_for(spec) is not None
    assert wrapper_for(spec) is not None


def test_sdk_shim_is_a_packaged_runtime_asset():
    """The managed launcher must work from a wheel, not only a checkout."""
    from ctx.agysdk import shim_source

    source = shim_source()
    assert source.is_file()
    assert source.name in {"ctx_agy.py"}


def test_detection_ignores_path_and_reports_missing_when_absent(monkeypatch, tmp_path):
    """A self-hosted host is located in the state root, not on PATH. With no
    managed venv it must read as not-installed rather than raising."""
    monkeypatch.setenv("CTX_STATE_HOME", str(tmp_path / "state"))
    spec = host_by_name(SDK)
    host = detect(spec, which=lambda _b: None)
    assert host.installed is False
    assert host.path is None


def test_detection_finds_the_managed_launcher(monkeypatch, tmp_path):
    """The state-root probe runs only under the real `which` — injecting one is
    how this module says "this is the whole world", and a filesystem probe that
    ignored it would leak a developer's own install into synthetic host lists.
    So this test uses the real which() with an isolated state root."""
    import shutil

    monkeypatch.setenv("CTX_STATE_HOME", str(tmp_path / "state"))
    import ctx.agysdk as agysdk

    launcher = agysdk.launcher_path()
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text("#!/usr/bin/env bash\nexit 0\n")
    launcher.chmod(0o755)
    # a launcher alone is not enough — the venv must actually import the SDK
    monkeypatch.setattr(agysdk, "sdk_present", lambda *a, **k: True)
    host = detect(host_by_name(SDK), which=shutil.which)
    assert host.installed is True
    assert host.path == str(launcher)


def test_half_built_env_reads_as_missing(monkeypatch, tmp_path):
    """A venv whose install failed part-way is worse than none: it would detect
    as present and then fail at launch. The SDK import is the real check."""
    monkeypatch.setenv("CTX_STATE_HOME", str(tmp_path / "state"))
    import ctx.agysdk as agysdk

    launcher = agysdk.launcher_path()
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text("#!/usr/bin/env bash\nexit 0\n")
    monkeypatch.setattr(agysdk, "sdk_present", lambda *a, **k: False)
    assert agysdk.is_installed() is False
    assert detect(host_by_name(SDK), which=lambda _b: None).installed is False


def test_detection_never_raises(monkeypatch, tmp_path):
    """Fail-open, like every other host: an exploding probe degrades to
    not-installed and must not break `ctx wrap detect` for the others."""
    monkeypatch.setenv("CTX_STATE_HOME", str(tmp_path / "state"))
    import ctx.agysdk as agysdk

    def boom():
        raise RuntimeError("state root unreadable")

    monkeypatch.setattr(agysdk, "is_installed", boom)
    assert detect(host_by_name(SDK), which=lambda _b: None).installed is False


def test_plain_setup_does_not_silently_build_a_venv(tmp_path, monkeypatch):
    """`ctx wrap setup` harnesses what you already have. Downloading a vendor
    SDK and building an environment is an explicit choice, so an uninstalled
    SDK host must not appear in the setup set."""
    monkeypatch.setenv("CTX_STATE_HOME", str(tmp_path / "state"))
    installed = {h.name for h in
                 (detect(s, which=lambda _b: None) for s in all_hosts())
                 if h.installed}
    assert SDK not in installed


@pytest.mark.parametrize("name", ["antigravity", SDK])
def test_both_hosts_are_offered_by_the_cli(name):
    """Both must be reachable as `ctx wrap <host>`; the parser's choices list is
    hand-maintained and has drifted from the registry before."""
    from ctx.cli import _build_parser

    parser = _build_parser()
    ns = parser.parse_args(["wrap", name])
    assert ns.host == name
