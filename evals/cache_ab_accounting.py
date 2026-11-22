"""Exact list-price accounting for the approved Haiku 4.5 experiment.

This module performs no I/O. Anthropic's four token counters are disjoint:
``input_tokens`` excludes both cached reads and cache creation. Provider usage,
not the offline tokenizer or the count-tokens estimate, determines observed cost.
The ledger is deliberately sequential and refuses more requests after unknown
charges. It is a local spending guard, not an invoice or a provider-side limit.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any


MODEL = "claude-haiku-4-5-20251001"
PRICE_DATE = "2026-11-23"
PRICE_SOURCE = "https://platform.claude.com/docs/en/models/haiku-4-5/overview"
CONTEXT_TOKENS = 200_000
MODEL_MAX_OUTPUT_TOKENS = 64_000
MILLION = Decimal(1_000_000)
RATES = {
    "input_tokens": Decimal("1"),
    "cache_creation_input_tokens": Decimal("1.25"),
    "cache_read_input_tokens": Decimal("0.10"),
    "output_tokens": Decimal("5"),
}
TOKEN_FIELDS = tuple(RATES)


class UsageError(ValueError):
    """Usage cannot be priced under this experiment's frozen rate card."""


class BudgetExceeded(ValueError):
    """The next request's full-context reservation exceeds remaining budget."""


class LedgerBlocked(ValueError):
    """An unresolved request prevents further execution."""


def _token_count(value: Any, name: str) -> int:
    # bool is an int subclass; accepting it conceals malformed provider usage.
    if type(value) is not int or value < 0:
        raise UsageError(f"{name} must be a non-negative integer")
    return value


def normalize_usage(raw: Mapping[str, Any]) -> dict[str, int]:
    """Validate final provider usage; never infer omitted counters as zero.

    The nested cache-creation breakdown is optional on API version 2023-06-01.
    When supplied it must reconcile exactly, and one-hour writes are rejected:
    the experiment requests only five-minute caching and prices that contract.
    Unknown metadata is retained by the caller's raw response, not priced here.
    """
    if not isinstance(raw, Mapping):
        raise UsageError("usage must be an object")
    missing = [name for name in TOKEN_FIELDS if name not in raw]
    if missing:
        raise UsageError("missing usage counters: " + ", ".join(missing))
    result = {name: _token_count(raw[name], name) for name in TOKEN_FIELDS}
    result["total_input_tokens"] = sum(result[name] for name in TOKEN_FIELDS if name != "output_tokens")
    if "total_input_tokens" in raw:
        supplied_total = _token_count(raw["total_input_tokens"], "total_input_tokens")
        if supplied_total != result["total_input_tokens"]:
            raise UsageError("total_input_tokens does not reconcile with disjoint counters")

    creation = raw.get("cache_creation")
    if creation is not None:
        if not isinstance(creation, Mapping):
            raise UsageError("cache_creation must be an object or null")
        expected = {"ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"}
        if set(creation) != expected:
            raise UsageError("cache_creation must contain exactly the 5m and 1h counters")
        five = _token_count(creation["ephemeral_5m_input_tokens"], "ephemeral_5m_input_tokens")
        hour = _token_count(creation["ephemeral_1h_input_tokens"], "ephemeral_1h_input_tokens")
        if hour:
            raise UsageError("one-hour cache writes are outside the approved rate card")
        if five + hour != result["cache_creation_input_tokens"]:
            raise UsageError("cache_creation breakdown does not reconcile")

    if raw.get("service_tier") not in (None, "standard"):
        raise UsageError("usage is not from the requested standard service tier")
    # These runs have no provider-hosted tools. Any nonzero counter would add
    # costs that the four token prices cannot represent.
    server_tools = raw.get("server_tool_use")
    if server_tools is not None:
        if not isinstance(server_tools, Mapping):
            raise UsageError("server_tool_use must be an object or null")
        for name, count in server_tools.items():
            if _token_count(count, f"server_tool_use.{name}"):
                raise UsageError("server tool charges are outside the approved rate card")
    return result


def cost_usd(usage: Mapping[str, Any]) -> Decimal:
    """Return unrounded USD at the frozen standard list prices."""
    normalized = normalize_usage(usage)
    return sum((Decimal(normalized[name]) * rate for name, rate in RATES.items()), Decimal(0)) / MILLION


def reservation_usd(max_output_tokens: int) -> Decimal:
    """Reserve the full 200K input window at the most expensive allowed rate.

    Reserving the model window avoids treating a count-tokens estimate as a
    guaranteed billing bound. Counting output in addition is conservative even
    if the provider includes it within the context window.
    """
    max_output_tokens = _token_count(max_output_tokens, "max_output_tokens")
    if not 1 <= max_output_tokens <= MODEL_MAX_OUTPUT_TOKENS:
        raise UsageError("max_output_tokens is outside the model's supported range")
    return (Decimal(CONTEXT_TOKENS) * RATES["cache_creation_input_tokens"] + Decimal(max_output_tokens) * RATES["output_tokens"]) / MILLION


@dataclass
class _Reservation:
    maximum_output_tokens: int
    reserved: Decimal
    status: str = "pending"
    usage: dict[str, int] | None = None
    cost: Decimal | None = None
    reason: str | None = None


class Ledger:
    """One in-flight request, exact Decimal budget, explicit unknown charges.

    Persist :meth:`snapshot` before sending each request and after every state
    change. This object does not silently resume or retry a prior invocation.
    A known final response may reconcile an unknown reservation via ``settle``;
    without such evidence, unknown charges permanently block that invocation.
    """

    def __init__(self, cap: Decimal):
        if not isinstance(cap, Decimal) or not cap.is_finite() or cap < 0:
            raise ValueError("cap must be a finite non-negative Decimal")
        self.cap = cap
        self._requests: dict[str, _Reservation] = {}

    @property
    def known_cost(self) -> Decimal:
        return sum((r.cost for r in self._requests.values() if r.cost is not None), Decimal(0))

    @property
    def reserved(self) -> Decimal:
        return sum((r.reserved for r in self._requests.values() if r.status != "settled"), Decimal(0))

    @property
    def blocked(self) -> bool:
        return any(r.status == "unknown" for r in self._requests.values())

    def reserve(self, max_output_tokens: int) -> str:
        if self.blocked:
            raise LedgerBlocked("an unknown provider charge must be reconciled before further execution")
        if any(r.status == "pending" for r in self._requests.values()):
            raise LedgerBlocked("the prior reservation has not been settled")
        amount = reservation_usd(max_output_tokens)
        if self.known_cost + self.reserved + amount > self.cap:
            raise BudgetExceeded("insufficient budget for the next full-context reservation")
        request_id = f"request-{len(self._requests) + 1:06d}"
        self._requests[request_id] = _Reservation(max_output_tokens, amount)
        return request_id

    def _unsettled(self, request_id: str) -> _Reservation:
        if request_id not in self._requests:
            raise ValueError("unknown reservation ID")
        reservation = self._requests[request_id]
        if reservation.status == "settled":
            raise ValueError("reservation has already been settled")
        return reservation

    def settle(self, reservation_id: str, usage: Mapping[str, Any]) -> Decimal:
        reservation = self._unsettled(reservation_id)
        try:
            normalized = normalize_usage(usage)
            if normalized["total_input_tokens"] > CONTEXT_TOKENS:
                raise UsageError("provider usage exceeds the approved model context")
            if normalized["output_tokens"] > reservation.maximum_output_tokens:
                raise UsageError("provider usage exceeds the request's output limit")
            observed_cost = cost_usd(normalized)
            if observed_cost > reservation.reserved:
                raise UsageError("provider cost exceeds its full-context reservation")
        except UsageError as exc:
            self.mark_unknown(reservation_id, str(exc))
            raise
        reservation.usage = normalized
        reservation.cost = observed_cost
        reservation.status = "settled"
        reservation.reason = None
        return observed_cost

    def mark_unknown(self, reservation_id: str, reason: str) -> None:
        reservation = self._unsettled(reservation_id)
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("an unknown charge requires a nonempty reason")
        reservation.status = "unknown"
        reservation.reason = reason

    def snapshot(self) -> dict[str, Any]:
        """Return JSON-safe evidence, keeping observed cost and exposure apart.

        ``known_cost_lower_bound_usd`` is observed spending only. Outstanding
        reservations are estimates of possible exposure, never observed cost or
        an asserted invoice upper bound for malformed/unpriced provider usage.
        """
        return {
            "currency": "USD",
            "model": MODEL,
            "price_date": PRICE_DATE,
            "price_source": PRICE_SOURCE,
            "usd_per_million_tokens": {name: str(rate) for name, rate in RATES.items()},
            "cap_usd": str(self.cap),
            "known_cost_usd": str(self.known_cost),
            "known_cost_lower_bound_usd": str(self.known_cost),
            "reserved_usd": str(self.reserved),
            "exposure_estimate_usd": str(self.known_cost + self.reserved),
            "remaining_usd": str(self.cap - self.known_cost - self.reserved),
            "cost_complete": all(r.status == "settled" for r in self._requests.values()),
            "blocked": self.blocked,
            "pending_requests": sum(r.status == "pending" for r in self._requests.values()),
            "unknown_requests": sum(r.status == "unknown" for r in self._requests.values()),
            "requests": [
                {
                    "reservation_id": key,
                    "status": r.status,
                    "max_output_tokens": r.maximum_output_tokens,
                    "reserved_usd": str(r.reserved),
                    "cost_usd": str(r.cost) if r.cost is not None else None,
                    "usage": r.usage,
                    "reason": r.reason,
                }
                for key, r in self._requests.items()
            ],
        }
