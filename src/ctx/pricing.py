"""Host-neutral model pricing.

None of the supported hosts' SDKs expose per-model dollar pricing — they
report token *usage* (Antigravity ``UsageMetadata``, Anthropic/OpenAI usage
blocks). Dollar cost is therefore ``usage x price``, and the price comes from
published, refreshable pricing pages — not from any SDK. This module keeps
that price table as *data* (``ctx/data/model-prices.json``), matched to a
session's model by tier token, so adding a host or repricing a tier is an
edit to a JSON file, never a code change.

Two rules keep it honest and host-neutral:

1. **Prefer host-reported cost.** When a host computes the real spend
   (Claude Code's status line ``cost.total_cost_usd``), use it verbatim; the
   table is only for hosts that report usage but not dollars.
2. **Match on tier tokens, not substrings.** ``mini`` must match
   ``gpt-5-mini`` and ``gpt-4o-mini-2024`` but not ``gemini-3-pro`` — the
   real collision this rule exists to prevent, and one a plain substring
   test gets wrong. The rule is a LETTER boundary: a tier token may sit
   between any non-letters, so ``mini`` does match a contrived
   ``ge-mini-3-pro``. That is deliberate and not fixable by a stricter
   segment rule without also breaking ``gpt-4o-mini-2024`` — distinguishing
   the two needs vendor knowledge, not lexing. An earlier version of this
   docstring used ``ge-mini-3-pro`` as the counter-example, which no
   boundary rule can satisfy; the example was wrong, not the matcher.
   The same letter-boundary rule
   the engagement lean-model matcher uses. Entry order is specific->general;
   the first tier token that matches wins.

Overridable per repo: a ``.ctx-prices.json`` at the workspace root, on the
same schema, replaces or extends rows — published prices go stale, and a
repo on negotiated rates should be able to say so. That file is the *only*
override seam. ``ctx.toml`` deliberately has no ``[pricing]`` block: it
carries repository policy (budgets, guard, scopes), while a price table is
vendor data on a versioned schema, and one override mechanism beats two
with a precedence rule between them. Every entry point fails open to the
vendor-neutral fallback so a cost estimate can never crash a session.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = "ctx.model-prices/v1"
_DATA_NAME = "model-prices.json"
_REPO_OVERRIDE = ".ctx-prices.json"

# Vendor-neutral fallback used only if the shipped data file is unreadable
# (packaging mishap) or a model matches nothing. Standard mid-tier shape so an
# unknown model is never priced at zero (which would read as "free").
_EMBEDDED_FALLBACK = {
    "vendor": "unknown",
    "tier": "unknown",
    "in": 3.0,
    "out": 15.0,
    "cache_write": 3.75,
    "cache_read": 0.30,
    "source": "",
}


@dataclass(frozen=True)
class Price:
    """Per-1M-token published list prices for one model tier."""

    match: str
    vendor: str
    tier: str
    input: float
    output: float
    cache_write: float
    cache_read: float
    source: str = ""

    def cost_usd(
        self,
        *,
        input_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        output_tokens: int = 0,
    ) -> float:
        """Dollar cost of a token-usage breakdown at this tier's list prices.
        Token categories are priced independently (cache reads are cheap,
        cache writes carry a premium) exactly as the vendors bill them."""
        return (
            input_tokens * self.input
            + cache_read_tokens * self.cache_read
            + cache_write_tokens * self.cache_write
            + output_tokens * self.output
        ) / 1_000_000


def _price_from_row(row: dict[str, Any]) -> Price:
    return Price(
        match=str(row.get("match", "")),
        vendor=str(row.get("vendor", "unknown")),
        tier=str(row.get("tier", "unknown")),
        input=float(row.get("in", 0.0)),
        output=float(row.get("out", 0.0)),
        cache_write=float(row.get("cache_write", 0.0)),
        cache_read=float(row.get("cache_read", 0.0)),
        source=str(row.get("source", "")),
    )


def _shipped_data_path() -> Path:
    """The bundled price table. ``Path(__file__).with_name('data')`` resolves
    to ``ctx/data`` in both a source checkout (``src/ctx/data``) and an
    installed wheel (force-included ``ctx/data``)."""
    return Path(__file__).with_name("data") / _DATA_NAME


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else None
    except Exception:
        return None


def load_table(workspace_root: Path | str | None = None) -> dict[str, Any]:
    """Shipped table, then repo override merged on top. The override's rows
    are tried *before* the shipped rows (repo wins), and an override
    ``fallback`` replaces the shipped one. Never raises."""
    shipped = _read_json(_shipped_data_path()) or {}
    models = list(shipped.get("models") or [])
    fallback = dict(shipped.get("fallback") or _EMBEDDED_FALLBACK)

    if workspace_root is not None:
        override = _read_json(Path(workspace_root) / _REPO_OVERRIDE)
        if override:
            models = list(override.get("models") or []) + models
            if isinstance(override.get("fallback"), dict):
                fallback = dict(override["fallback"])

    if not models and not shipped:
        # Shipped data unreadable: still return a usable one-row table.
        fallback = dict(_EMBEDDED_FALLBACK)
    return {"models": models, "fallback": fallback}


def _token_matches(tag: str, model_low: str) -> bool:
    tag = tag.lower().strip()
    if not tag:
        return False
    return bool(re.search(rf"(?<![a-z]){re.escape(tag)}(?![a-z])", model_low))


def price_for(
    model: str, *, table: dict[str, Any] | None = None,
    workspace_root: Path | str | None = None,
) -> Price:
    """Resolve the price tier for a model id. First tier token that matches on
    letter boundaries wins (entries are ordered specific->general); no match
    returns the vendor-neutral fallback. Case-insensitive, host-neutral."""
    tbl = table if table is not None else load_table(workspace_root)
    low = (model or "").lower()
    if low:
        for row in tbl.get("models") or []:
            if _token_matches(str(row.get("match", "")), low):
                return _price_from_row(row)
    return _price_from_row(tbl.get("fallback") or _EMBEDDED_FALLBACK)


def cost_usd(
    usage: dict[str, int], model: str, *,
    table: dict[str, Any] | None = None,
    workspace_root: Path | str | None = None,
) -> float:
    """Dollar cost of a usage breakdown for ``model``. ``usage`` keys:
    ``input``, ``cache_read``, ``cache_write``, ``output`` (missing = 0)."""
    p = price_for(model, table=table, workspace_root=workspace_root)
    return p.cost_usd(
        input_tokens=int(usage.get("input", 0) or 0),
        cache_read_tokens=int(usage.get("cache_read", 0) or 0),
        cache_write_tokens=int(usage.get("cache_write", 0) or 0),
        output_tokens=int(usage.get("output", 0) or 0),
    )


__all__ = ["SCHEMA", "Price", "load_table", "price_for", "cost_usd"]
