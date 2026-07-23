"""The guard hot path (ctx.hook._load_guard_policy) re-reads ctx.toml with its
own stdlib-only parser for latency, separate from the typed loader
(ctx.config.load_config). Two parsers of the same file is a drift hazard: for
years they were kept in sync by a hand-written "keep in sync" comment, and the
typed loader had silently stopped modelling four keys the hot path reads
(collapse, allow_commands, deny_commands, emission_nudge_tokens).

These tests turn that comment into an executable invariant: the two readers
must agree on both defaults and parsed values, for every shared key.
"""
from __future__ import annotations

import textwrap

from ctx.config import Config, load_config
from ctx.hook import _load_guard_policy


def test_default_policy_matches_typed_config():
    """No ctx.toml: the hot-path base defaults equal the typed-config defaults."""
    cfg = Config()
    pol = _load_guard_policy(None)

    assert pol["mode"] == cfg.guard.mode
    assert pol["unknown_command"] == cfg.guard.unknown_command
    assert pol["internal_error"] == cfg.guard.internal_error
    assert pol["steering"] == cfg.guard.steering
    assert pol["collapse"] == cfg.guard.collapse
    assert list(pol["allow_commands"]) == list(cfg.guard.allow_commands)
    assert list(pol["deny_commands"]) == list(cfg.guard.deny_commands)

    assert pol["max_inline_bytes"] == cfg.budgets.max_inline_bytes
    assert pol["max_inline_lines"] == cfg.budgets.max_inline_lines
    assert pol["session_read_budget_bytes"] == cfg.budgets.session_read_budget_bytes
    assert pol["window_pressure_pct"] == cfg.budgets.window_pressure_pct
    assert pol["max_tool_output_bytes"] == cfg.budgets.max_tool_output_bytes

    assert pol["engagement_mode"] == cfg.engagement.mode
    assert pol["engagement_activate_after"] == cfg.engagement.activate_after_calls
    assert pol["emission_nudge_tokens"] == cfg.engagement.emission_nudge_tokens


def test_typed_config_models_every_key_the_hot_path_reads(tmp_path):
    """A ctx.toml that sets the previously-dropped keys is reflected by the
    typed loader (regression guard for the config key-coverage gap)."""
    (tmp_path / "ctx.toml").write_text(
        textwrap.dedent(
            """
            [guard]
            collapse = false
            allow_commands = ["mytool", "safebin"]
            deny_commands = ["dangerbin"]

            [budgets]
            max_inline_bytes = 32768
            window_pressure_pct = 80

            [engagement]
            emission_nudge_tokens = 12345
            """
        ),
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)

    # Keys the typed loader used to silently drop:
    assert cfg.guard.collapse is False
    assert cfg.guard.allow_commands == ("mytool", "safebin")
    assert cfg.guard.deny_commands == ("dangerbin",)
    assert cfg.engagement.emission_nudge_tokens == 12345

    # And they match what the hot-path parser reads from the same file.
    pol = _load_guard_policy(str(tmp_path))
    assert pol["collapse"] == cfg.guard.collapse
    assert list(pol["allow_commands"]) == list(cfg.guard.allow_commands)
    assert list(pol["deny_commands"]) == list(cfg.guard.deny_commands)
    assert pol["max_inline_bytes"] == cfg.budgets.max_inline_bytes
    assert pol["window_pressure_pct"] == cfg.budgets.window_pressure_pct
    assert pol["emission_nudge_tokens"] == cfg.engagement.emission_nudge_tokens
