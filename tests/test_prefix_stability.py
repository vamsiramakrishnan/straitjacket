"""Acceptance: the prefix-stability contract (mechanism A).

Any byte the harness injects into a model's prompt prefix is a cache-keyed
asset. Changing one silently costs every user a cold cache rewrite per model
(measured: ~56k tokens / ~$0.21 on sonnet for a 9-token edit — see
evals/matrix-2026-07-18.md). The golden manifest makes that change an
explicit, versioned decision instead of a side effect.
"""

import json


def test_prefix_manifest_matches_injected_bytes():
    from ctx.prefixassets import check

    problems = check()
    assert not problems, (
        "Prefix contract violated:\n  " + "\n  ".join(problems) + "\n\n"
        "Injected prefix text changed. This cold-invalidates every user's "
        "prompt cache (one full prefix rewrite per model). If intentional: "
        "bump PREFIX_VERSION in src/ctx/prefixassets.py, regenerate with "
        "python3 -c 'from ctx.prefixassets import write_manifest; write_manifest()' "
        "and note the cache impact in CHANGELOG.md."
    )


def test_manifest_is_committed_and_versioned():
    from ctx.prefixassets import PREFIX_VERSION, load_manifest, manifest_path

    doc = load_manifest()
    assert doc is not None, f"missing {manifest_path()}"
    assert doc["schema"] == "ctx.prefix/v1"
    assert doc["prefix_version"] == PREFIX_VERSION
    assert len(doc["assets"]) >= 4


def test_assets_are_byte_stable_across_calls():
    """Prefix assets must be pure constants: two computations, one result.
    (A timestamp, counter, or path leaking in would defeat the contract.)"""
    from ctx.prefixassets import compute_manifest

    assert json.dumps(compute_manifest()) == json.dumps(compute_manifest())


def test_discipline_prompt_carries_no_volatile_content():
    from ctx.wrap import _OUTPUT_DISCIPLINE

    import re

    assert "\n" not in _OUTPUT_DISCIPLINE  # single line: stable, diffable
    assert not re.search(r"\d{4}-\d{2}-\d{2}|/home/|/tmp/", _OUTPUT_DISCIPLINE)
