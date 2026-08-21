import json

import pytest

from ctx.stream_rules import (
    STREAM_RULE_SCHEMA,
    StreamRule,
    StreamRuleEngine,
    load_state,
    save_state,
)


def test_rule_matches_across_stream_chunks_and_requests_retry():
    engine = StreamRuleEngine(
        [StreamRule("unsafe-leak", r"Box::leak\s*\(", "Do not leak memory; use owned shared state.")]
    )
    assert engine.feed("I will use Box::") is None
    match = engine.feed("leak(value) here")
    assert match is not None
    assert match.rule == "unsafe-leak"
    assert "Correct course" in match.injection
    assert match.receipt == {
        "schema": STREAM_RULE_SCHEMA,
        "event": "activated",
        "rule": "unsafe-leak",
        "fire": 1,
        "windowChars": 8192,
    }


def test_rule_is_deduplicated_and_respects_session_fire_budget():
    rule = StreamRule("no-force", r"git push --force", "Use force-with-lease only after approval.")
    engine = StreamRuleEngine([rule])
    assert engine.feed("git push --force") is not None
    assert engine.feed(" git push --force") is None
    engine.begin_turn()
    assert engine.feed("git push --force") is None


def test_activation_state_survives_compaction_and_restores_reminder(tmp_path):
    rule = StreamRule("no-force", r"git push --force", "Use force-with-lease only after approval.", max_fires=2)
    first = StreamRuleEngine([rule])
    assert first.feed("git push --force") is not None
    state_path = tmp_path / "stream-rules.json"
    save_state(state_path, first.state())

    second = StreamRuleEngine([rule], prior_state=load_state(state_path))
    assert second.persistent_reminders() == ("Use force-with-lease only after approval.",)
    second.begin_turn()
    assert second.feed("git push --force") is not None
    assert json.loads(state_path.read_text())["schema"] == STREAM_RULE_SCHEMA


def test_rolling_buffer_is_bounded_and_invalid_rules_are_rejected():
    engine = StreamRuleEngine([StreamRule("late", "needle", "stop")], window_chars=256)
    assert engine.feed("x" * 20_000) is None
    assert engine.buffered_chars == 256
    with pytest.raises(ValueError):
        StreamRuleEngine([StreamRule("", "x", "stop")])
    with pytest.raises(Exception):
        StreamRuleEngine([StreamRule("bad", "(", "stop")])


def test_load_state_fails_open(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("not json")
    assert load_state(path) == {}
