"""Acceptance: host-neutral, data-driven model pricing.

The table must resolve the small/standard/premium tier of every supported
host from the shipped data file, match on tier tokens (not substrings),
honour repo overrides, and always fail open to a vendor-neutral fallback."""

import json

from ctx import pricing


def test_shipped_table_covers_three_vendors():
    tbl = pricing.load_table()
    vendors = {r["vendor"] for r in tbl["models"]}
    assert {"anthropic", "google", "openai"} <= vendors
    assert tbl["fallback"]["in"] > 0  # unknown model never prices at zero


def test_resolution_is_host_neutral():
    cases = {
        "claude-opus-4-8": ("anthropic", 15.0),
        "claude-sonnet-5": ("anthropic", 3.0),
        "claude-haiku-4-5-20251001": ("anthropic", 1.0),
        "gemini-3.5-flash": ("google", 1.50),
        "gemini-3.1-flash-lite": ("google", 0.25),
        "gemini-3-pro": ("google", 2.00),
        "gpt-5.5": ("openai", 5.00),
        "gpt-5.4-nano": ("openai", 0.20),
        "gpt-5.3-codex": ("openai", 1.75),
    }
    for model, (vendor, inprice) in cases.items():
        p = pricing.price_for(model)
        assert p.vendor == vendor, f"{model} -> {p.vendor}"
        assert abs(p.input - inprice) < 1e-9, f"{model} in=${p.input}"


def test_lite_matches_before_flash():
    # flash-lite must not be captured by the generic 'flash' row.
    assert pricing.price_for("gemini-3.1-flash-lite").tier == "economy"
    assert pricing.price_for("gemini-3.5-flash").tier == "standard"


def test_version_specific_flash_prices_differ():
    # 3.5-flash and 3-flash are different SKUs at different prices.
    assert pricing.price_for("gemini-3.5-flash").input == 1.50
    assert pricing.price_for("gemini-3-flash").input == 0.50


def test_unknown_model_uses_fallback():
    p = pricing.price_for("totally-unknown-model-xyz")
    assert p.vendor == "unknown"
    assert p.input > 0


def test_empty_model_uses_fallback():
    assert pricing.price_for("").vendor == "unknown"


def test_cost_prices_token_categories_independently():
    # cache reads cheap, cache writes premium, priced separately.
    c = pricing.cost_usd(
        {"input": 1_000_000, "cache_read": 1_000_000,
         "cache_write": 1_000_000, "output": 1_000_000},
        "claude-sonnet-5",
    )
    # 3.0 + 0.30 + 3.75 + 15.0 = 22.05
    assert abs(c - 22.05) < 1e-9


def test_repo_override_wins(tmp_path):
    (tmp_path / ".ctx-prices.json").write_text(
        json.dumps({
            "models": [{"match": "gemini-3.5-flash", "vendor": "google",
                        "tier": "custom", "in": 0.99, "out": 9.0,
                        "cache_write": 0.99, "cache_read": 0.1}],
        }),
        encoding="utf-8",
    )
    p = pricing.price_for("gemini-3.5-flash", workspace_root=tmp_path)
    assert p.input == 0.99 and p.tier == "custom"
    # a model the override doesn't mention still falls through to shipped rows
    assert pricing.price_for("claude-opus-4-8", workspace_root=tmp_path).input == 15.0


def test_override_can_replace_fallback(tmp_path):
    (tmp_path / ".ctx-prices.json").write_text(
        json.dumps({"fallback": {"vendor": "house", "tier": "flat",
                                  "in": 2.0, "out": 8.0,
                                  "cache_write": 2.0, "cache_read": 0.2}}),
        encoding="utf-8",
    )
    p = pricing.price_for("some-unlisted-model", workspace_root=tmp_path)
    assert p.vendor == "house" and p.input == 2.0
