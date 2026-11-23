from __future__ import annotations

import json
from decimal import Decimal

import pytest

from evals.cache_ab_accounting import (
    BudgetExceeded,
    Ledger,
    LedgerBlocked,
    UsageError,
    cost_usd,
    normalize_usage,
    reservation_usd,
)


def usage(**overrides):
    result = {
        "input_tokens": 1000,
        "cache_creation_input_tokens": 2000,
        "cache_read_input_tokens": 3000,
        "output_tokens": 400,
    }
    result.update(overrides)
    return result


def test_disjoint_anthropic_usage_is_not_subtracted_or_double_counted():
    normalized = normalize_usage(usage())
    assert normalized["total_input_tokens"] == 6000
    assert normalized["input_tokens"] == 1000
    assert cost_usd(usage()) == Decimal("0.0058")
    assert cost_usd(normalized) == Decimal("0.0058")


@pytest.mark.parametrize("field", ["input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"])
@pytest.mark.parametrize("bad", [-1, True, False, 1.0, "1", None])
def test_bad_counters_are_not_coerced(field, bad):
    with pytest.raises(UsageError):
        normalize_usage(usage(**{field: bad}))


@pytest.mark.parametrize("field", ["input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"])
def test_missing_usage_is_not_silently_zero(field):
    raw = usage()
    del raw[field]
    with pytest.raises(UsageError, match="missing"):
        normalize_usage(raw)


def test_zero_usage_is_known_not_unknown():
    zero = dict.fromkeys(usage(), 0)
    assert cost_usd(zero) == Decimal(0)
    ledger = Ledger(Decimal("1"))
    reservation = ledger.reserve(2048)
    ledger.settle(reservation, zero)
    assert ledger.snapshot()["cost_complete"] is True


def test_optional_cache_breakdown_reconciles():
    raw = usage(cache_creation={"ephemeral_5m_input_tokens": 2000, "ephemeral_1h_input_tokens": 0})
    assert cost_usd(raw) == Decimal("0.0058")
    raw["cache_creation"]["ephemeral_5m_input_tokens"] = 1999
    with pytest.raises(UsageError, match="reconcile"):
        normalize_usage(raw)


@pytest.mark.parametrize("nested", [
    {"ephemeral_5m_input_tokens": 1999, "ephemeral_1h_input_tokens": 1},
    {"ephemeral_5m_input_tokens": 2000},
    {"ephemeral_5m_input_tokens": True, "ephemeral_1h_input_tokens": 0},
    [],
])
def test_unpriced_or_malformed_cache_creation_is_rejected(nested):
    with pytest.raises(UsageError):
        normalize_usage(usage(cache_creation=nested))


def test_total_must_reconcile_if_already_normalized():
    with pytest.raises(UsageError, match="disjoint"):
        cost_usd(usage(total_input_tokens=1000))


def test_nonstandard_tier_and_server_tool_charges_are_rejected():
    assert cost_usd(usage(service_tier="standard", server_tool_use={"web_search_requests": 0})) == Decimal("0.0058")
    for raw in [usage(service_tier="batch"), usage(server_tool_use={"web_search_requests": 1})]:
        with pytest.raises(UsageError):
            normalize_usage(raw)


def test_full_context_reservation_is_independent_of_input_estimate():
    assert reservation_usd(2048) == Decimal("0.26024")
    assert reservation_usd(64) == Decimal("0.25032")


@pytest.mark.parametrize("limit", [0, -1, 64_001, True, 2048.0])
def test_invalid_output_reservation(limit):
    with pytest.raises(UsageError):
        reservation_usd(limit)


def test_exact_cap_boundary_and_refund_of_unused_reservation():
    ledger = Ledger(Decimal("0.26024"))
    reservation = ledger.reserve(2048)
    with pytest.raises(LedgerBlocked):
        ledger.reserve(2048)
    assert ledger.settle(reservation, usage()) == Decimal("0.0058")
    assert ledger.snapshot()["reserved_usd"] == "0"
    with pytest.raises(BudgetExceeded):
        ledger.reserve(2048)
    # The smaller probe can fit once the unused reservation is released.
    ledger.reserve(64)


def test_unaffordable_request_does_not_create_reservation():
    ledger = Ledger(Decimal("0.260239999"))
    with pytest.raises(BudgetExceeded):
        ledger.reserve(2048)
    assert ledger.snapshot()["requests"] == []


def test_unknown_charge_keeps_reservation_and_blocks_the_invocation():
    ledger = Ledger(Decimal("15"))
    first = ledger.reserve(2048)
    ledger.settle(first, usage())
    second = ledger.reserve(2048)
    ledger.mark_unknown(second, "stream ended without final usage")
    snapshot = ledger.snapshot()
    assert snapshot["known_cost_lower_bound_usd"] == "0.0058"
    assert snapshot["reserved_usd"] == "0.26024"
    assert snapshot["exposure_estimate_usd"] == "0.26604"
    assert snapshot["cost_complete"] is False
    assert snapshot["unknown_requests"] == 1
    with pytest.raises(LedgerBlocked):
        ledger.reserve(64)
    assert json.loads(json.dumps(snapshot)) == snapshot


def test_invalid_final_usage_is_automatically_unknown():
    ledger = Ledger(Decimal("15"))
    reservation = ledger.reserve(2048)
    with pytest.raises(UsageError):
        ledger.settle(reservation, {"output_tokens": 1})
    assert ledger.blocked
    assert ledger.reserved == Decimal("0.26024")
    assert ledger.known_cost == 0


@pytest.mark.parametrize("invalid", [usage(input_tokens=200_001), usage(output_tokens=2049)])
def test_provider_usage_outside_reserved_limits_blocks_execution(invalid):
    ledger = Ledger(Decimal("15"))
    reservation = ledger.reserve(2048)
    with pytest.raises(UsageError):
        ledger.settle(reservation, invalid)
    assert ledger.snapshot()["blocked"] is True
    with pytest.raises(LedgerBlocked):
        ledger.reserve(2048)


def test_reconciliation_requires_known_usage_and_cannot_double_settle():
    ledger = Ledger(Decimal("15"))
    reservation = ledger.reserve(2048)
    ledger.mark_unknown(reservation, "connection lost")
    ledger.settle(reservation, usage())
    assert ledger.blocked is False
    with pytest.raises(ValueError, match="already"):
        ledger.settle(reservation, usage())
    with pytest.raises(ValueError, match="already"):
        ledger.mark_unknown(reservation, "late duplicate")
    assert ledger.known_cost == Decimal("0.0058")


@pytest.mark.parametrize("cap", [15.0, "15", Decimal("NaN"), Decimal("Infinity"), Decimal("-1")])
def test_budget_rejects_float_nonfinite_and_negative_values(cap):
    with pytest.raises(ValueError):
        Ledger(cap)


def test_successive_small_costs_remain_exact():
    ledger = Ledger(Decimal("15"))
    raw = {"input_tokens": 1, "cache_creation_input_tokens": 1, "cache_read_input_tokens": 1, "output_tokens": 1}
    for _ in range(100):
        ledger.settle(ledger.reserve(64), raw)
    assert ledger.known_cost == Decimal("0.000735")
    assert ledger.snapshot()["cost_complete"] is True
