"""Independent offline contracts; the comparison tokenizer is an optional extra."""
import importlib.metadata
import json

import pytest

from evals.cache_ab_offline import (
    CORPUS_SHA256, TOKENIZER_VERSION, _retrieved_lines, compression_probe,
    field_probe, prefix_probe, run_offline, sha256,
)
from evals.field_needle import build_corpus


@pytest.fixture
def pinned_tokenizer():
    pytest.importorskip("tiktoken")
    if importlib.metadata.version("tiktoken") != TOKENIZER_VERSION:
        pytest.skip(f"offline receipt requires tiktoken=={TOKENIZER_VERSION}")


def test_existing_fixture_has_reviewed_identity():
    raw = build_corpus().encode()
    assert len(raw) == 1_002_594
    assert raw.count(b"\n") == 20_001
    assert sha256(raw) == CORPUS_SHA256


def test_retrieval_comparison_keeps_exact_numbered_bodies():
    assert _retrieved_lines("header\nL12:  a  \nL13: b\nnext: go\n") == {12: " a  ", 13: "b"}


def test_visible_prefix_regression():
    result = prefix_probe()
    assert result["all_serialization_checks_passed"]
    assert result["prefix_stable"] and result["suffix_changes"]
    assert result["provider_cache_hit_proven"] is None


def test_monolithic_paragraph_refuses_protected_line_overflow():
    result = compression_probe()
    assert result["expected_refusal"]
    assert result["result"]["required_bytes"] == 1_002_594


def test_real_capture_retains_needles_and_exact_omitted_retrieval(tmp_path, pinned_tokenizer):
    result = field_probe(tmp_path, repeats=1)
    assert result["raw_tokens"] == 302_628
    assert result["repeats"][0]["canonical_visible_tokens"] == 531
    assert result["all_integrity_checks_passed"]
    assert result["head_tail_control"]["out_tokens"] == 1_219
    assert not result["head_tail_control"]["quiet_needle_survived"]
    persisted = json.loads((tmp_path / "field-00.json").read_text())
    assert persisted["raw_round_trip_exact"]


def test_receipt_distinguishes_calibration_from_model_success(tmp_path, pinned_tokenizer, monkeypatch):
    import http.client

    def forbidden(*args, **kwargs):
        raise AssertionError("offline called an HTTPS provider")

    monkeypatch.setattr(http.client, "HTTPSConnection", forbidden)
    result = run_offline(tmp_path, field_repeats=1, include_replay=False)
    assert result["all_checks_passed"]
    assert result["provider_calls"] == 0
    assert all(value is None for value in result["provider_usage"].values())
    assert result["provider_cost_usd"] is None
    assert result["provider_latency_s"] is None
    assert result["model_task_success"] is None
    assert len(result["referee_calibration"]) == 5
    assert all(row["calibration_passed"] for row in result["referee_calibration"])
    with pytest.raises(FileExistsError):
        run_offline(tmp_path)
