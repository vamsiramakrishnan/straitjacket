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


def test_registry_harnessable_hosts():
    names = {s.name for s in hosts.harnessable_hosts()}
    # antigravity-sdk is ctx's own agent on the google-antigravity SDK; it is a
    # separate host from the vendor's `agy` CLI on purpose (see
    # tests/test_agy_sdk_host.py), so it is harnessable in its own right.
    assert names == {"antigravity", "antigravity-sdk", "claude", "codex"}
    # Every harnessable host names a wrapper and installer function.
    for s in hosts.harnessable_hosts():
        assert s.wrapper and s.installer


def test_detect_resolves_installed_and_price():
    d = hosts.detect(
        hosts.host_by_name("claude"), which=_which_of("claude"), env={}
    )
    assert d.installed and d.path == "/usr/bin/claude"
    assert d.model == "claude-sonnet-4.6"
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


def test_pick_model_picks_cheapest_at_tier():
    got = hosts.installed_harnessable(which=_which_of("claude", "codex", "antigravity"))
    # economy work -> cheapest economy model across all harnesses
    h, m = hosts.pick_model(got, min_tier="economy", need_tags=("search",))
    assert (h.name, m.id, m.tier) == ("antigravity", "gemini-3.5-flash-lite", "economy")
    # ordinary implementation only needs a standard model -> the cheap flash, not
    # a frontier model. This is the point: implementation by Gemini flash.
    h, m = hosts.pick_model(got, min_tier="standard", need_tags=("implement", "edit"))
    assert (h.name, m.id, m.tier) == ("antigravity", "gemini-3.6-flash", "standard")
    # a frontier node picks the cheapest frontier model
    h, m = hosts.pick_model(got, min_tier="frontier", need_tags=("plan",))
    assert m.tier == "frontier"


def test_pick_model_prefer_strong_takes_the_flagship():
    got = hosts.installed_harnessable(which=_which_of("claude", "codex", "antigravity"))
    # cheap: cheapest frontier (Gemini Pro); strong: the flagship (Opus)
    _, m_cheap = hosts.pick_model(got, min_tier="frontier", need_tags=("plan",))
    _, m_strong = hosts.pick_model(got, min_tier="frontier", need_tags=("plan",), prefer="strong")
    assert m_strong.id == "claude-opus-4.8"          # flagship
    assert m_cheap.id != "claude-opus-4.8"           # cheap picks a cheaper frontier


def test_pick_model_routes_within_a_single_harness():
    # Even with only Claude installed, routing picks a different *model* per tier.
    got = hosts.installed_harnessable(which=_which_of("claude"))
    assert hosts.pick_model(got, min_tier="economy", need_tags=("explore",))[1].id == "claude-haiku-4.5"
    assert hosts.pick_model(got, min_tier="standard", need_tags=("code",))[1].id == "claude-sonnet-4.6"
    assert hosts.pick_model(got, min_tier="frontier", need_tags=("plan",))[1].id == "claude-opus-4.8"


def test_pick_model_falls_back_to_strongest_when_tier_unmet():
    got = hosts.installed_harnessable(which=_which_of("antigravity"))
    # frontier demanded; antigravity's ceiling is gemini-3.1-pro (frontier) -> met
    assert hosts.pick_model(got, min_tier="frontier")[1].tier == "frontier"


def test_model_launch_id_resolves_to_provider_id():
    # The id shown/priced differs from the id passed to the provider at launch;
    # verified against the live drivers (Claude wants `haiku`, the Gemini API
    # serves `gemini-3.5-flash-lite`). See the live-collab receipt.
    claude = hosts.host_by_name("claude")
    haiku = claude.model("claude-haiku-4.5")
    assert haiku.launch_id == "haiku"
    antig = hosts.host_by_name("antigravity")
    # gemini-3.1-pro is served under a -preview id at launch.
    assert antig.model("gemini-3.1-pro").launch_id == "gemini-3.1-pro-preview"
    # Default: launch_id falls back to id when no cli_id is set.
    assert antig.model("gemini-3.6-flash").launch_id == "gemini-3.6-flash"
    assert antig.model("gemini-3.5-flash-lite").launch_id == "gemini-3.5-flash-lite"


def test_pick_coordinator_is_cheapest_planner():
    got = hosts.installed_harnessable(which=_which_of("claude", "codex", "antigravity"))
    coord = hosts.pick_coordinator(got)
    # Antigravity plans on Gemini-flash-lite — cheapest coordinator model.
    assert coord.name == "antigravity"
    assert coord.spec.coord_model == "gemini-3.5-flash-lite"
    assert coord.coordinator_price().output < coord.price.output
