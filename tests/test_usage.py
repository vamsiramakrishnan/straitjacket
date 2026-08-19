from __future__ import annotations

import json

import pytest

from ctx.usage import (
    parse_antigravity_sdk_json,
    parse_claude_json,
    parse_codex_jsonl,
    parse_host_output,
    summarize_usage,
)


def test_claude_json_prefers_provider_reported_cost(tmp_path):
    stdout = json.dumps(
        {
            "type": "result",
            "result": "done",
            "total_cost_usd": 0.1234,
            "usage": {
                "input_tokens": 100,
                "cache_read_input_tokens": 40,
                "cache_creation_input_tokens": 10,
                "output_tokens": 20,
            },
        }
    )
    text, usage = parse_claude_json(
        stdout, model="claude-sonnet-4.6", workspace_root=tmp_path
    )
    assert text == "done"
    assert usage is not None
    assert usage.total_tokens == 170
    assert usage.cost_usd == pytest.approx(0.1234)
    assert usage.cost_basis == "host_reported"


def test_codex_jsonl_makes_cached_input_disjoint_before_pricing(tmp_path):
    stdout = "\n".join(
        [
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "complete"},
                }
            ),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 1_000,
                        "cached_input_tokens": 600,
                        "output_tokens": 100,
                        "reasoning_output_tokens": 25,
                    },
                }
            ),
        ]
    )
    text, usage = parse_codex_jsonl(
        stdout, model="gpt-5.6-terra", workspace_root=tmp_path
    )
    assert text == "complete"
    assert usage is not None
    assert usage.input_tokens == 400
    assert usage.cache_read_tokens == 600
    assert usage.output_tokens == 125
    assert usage.total_tokens == 1_125
    assert usage.cost_usd is not None
    assert usage.cost_basis == "priced_tokens"


def test_antigravity_sdk_json_preserves_agent_text_and_prices_tokens(tmp_path):
    stdout = 'line one\nline two\n{"input_tokens": 90, "output_tokens": 10}'
    text, usage = parse_antigravity_sdk_json(
        stdout, model="gemini-3.6-flash", workspace_root=tmp_path
    )
    assert text == "line one\nline two"
    assert usage is not None and usage.total_tokens == 100
    assert usage.cost_usd is not None


def test_plain_output_is_never_scraped_for_usage():
    prose = "Used 999 input tokens and cost $12.34"
    text, usage = parse_host_output("antigravity", prose, model="gemini")
    assert text == prose
    assert usage is None


def test_usage_summary_distinguishes_partial_from_zero():
    _, measured = parse_claude_json(
        json.dumps({"result": "ok", "total_cost_usd": 0.0, "usage": {}}),
        model="claude-haiku-4.5",
    )
    summary = summarize_usage([measured, None])
    assert summary["status"] == "partial"
    assert summary["attempts_measured"] == 1
    assert summary["cost_usd"] == 0.0
    assert summary["cost_complete"] is False
