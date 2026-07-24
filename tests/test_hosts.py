"""Acceptance: the data-driven host registry and CLI detection.

Detection is exercised with an injected ``which`` and ``env`` so no real
coding-agent CLI is ever launched; the model->price tie is asserted against the
shipped price table.
"""

from __future__ import annotations

from ctx import hosts


def _which_of(*installed):
    installed = set(installed)
    return lambda b: f"/usr/bin/{b}" if b in installed else None


def test_registry_has_three_harnessable_hosts():
    names = {s.name for s in hosts.harnessable_hosts()}
    assert names == {"antigravity", "claude", "codex"}
    # Every harnessable host names a wrapper and installer function.
    for s in hosts.harnessable_hosts():
        assert s.wrapper and s.installer


def test_detect_resolves_installed_and_price():
    d = hosts.detect(
        hosts.host_by_name("claude"), which=_which_of("claude"), env={}
    )
    assert d.installed and d.path == "/usr/bin/claude"
    assert d.model == "claude-sonnet"
    # Priced off the shipped table (sonnet tier).
    assert d.price.tier == "standard"
    assert d.price.output == 15.0


def test_detect_absent_host_is_not_installed():
    d = hosts.detect(hosts.host_by_name("codex"), which=_which_of("claude"), env={})
    assert not d.installed and d.path is None
    # Still priced (an estimate), so the picture is complete.
    assert d.price.output > 0


def test_model_env_override_wins():
    spec = hosts.host_by_name("claude")
    assert hosts.model_for(spec, {"ANTHROPIC_MODEL": "claude-opus-4"}) == "claude-opus-4"
    d = hosts.detect(spec, which=_which_of("claude"), env={"ANTHROPIC_MODEL": "claude-opus-4"})
    assert d.model == "claude-opus-4"
    assert d.price.tier == "premium"  # opus row


def test_installed_harnessable_filters_absent_and_unwired():
    got = hosts.installed_harnessable(which=_which_of("claude", "codex", "gemini"))
    names = [d.name for d in got]
    # gemini is installed but not yet harnessable -> excluded.
    assert names == ["claude", "codex"]


def test_detect_all_is_deterministic_order():
    a = [d.name for d in hosts.detect_all(which=_which_of())]
    b = [d.name for d in hosts.detect_all(which=_which_of())]
    assert a == b == [s.name for s in hosts.all_hosts()]


def test_detect_table_is_deterministic():
    from ctx.wrap import render_detect_table

    det = hosts.detect_all(which=_which_of("claude", "codex"))
    assert render_detect_table(det) == render_detect_table(det)
    out = render_detect_table(det)
    assert "harnessable now: claude, codex" in out
    assert "claude" in out and "codex" in out


def test_detect_table_when_nothing_installed():
    from ctx.wrap import render_detect_table

    out = render_detect_table(hosts.detect_all(which=_which_of()))
    assert "no harnessable CLI detected" in out


# ------------------------------------------------------------ capability × price


def test_capability_tiers_ordered():
    assert hosts.tier_rank("frontier") > hosts.tier_rank("standard") > hosts.tier_rank("economy")
    assert hosts.tier_rank("nonsense") == 0  # unknown fails safe (lowest)


def test_pick_worker_gates_on_tier_then_price():
    got = hosts.installed_harnessable(which=_which_of("claude", "codex", "antigravity"))
    # economy work -> cheapest eligible (antigravity)
    assert hosts.pick_worker(got, min_tier="economy", need_tags=("search",)).name == "antigravity"
    # frontier work -> only claude qualifies
    assert hosts.pick_worker(got, min_tier="frontier", need_tags=("edit",)).name == "claude"
    # standard code work -> cheapest coverer at standard (codex)
    assert hosts.pick_worker(got, min_tier="standard", need_tags=("code",)).name == "codex"


def test_pick_worker_falls_back_to_strongest_when_tier_unmet():
    got = hosts.installed_harnessable(which=_which_of("antigravity"))
    # frontier demanded but only economy installed -> strongest available, flagged by caller
    picked = hosts.pick_worker(got, min_tier="frontier", need_tags=("edit",))
    assert picked.name == "antigravity"
    assert picked.meets_tier("frontier") is False


def test_pick_coordinator_is_cheapest_planner():
    got = hosts.installed_harnessable(which=_which_of("claude", "codex", "antigravity"))
    coord = hosts.pick_coordinator(got)
    # Antigravity plans on Gemini-flash-lite — cheapest coordinator model.
    assert coord.name == "antigravity"
    assert coord.spec.coord_model == "gemini-3.5-flash-lite"
    assert coord.coordinator_price().output < coord.price.output
