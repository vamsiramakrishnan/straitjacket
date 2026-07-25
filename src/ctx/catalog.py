"""Routing dimensions beyond price: specialities, latency, throughput, benchmarks.

:mod:`ctx.hosts` says which models a harness can run and what capability tier
each sits in. :mod:`ctx.pricing` says what they cost. This module carries the
*other* signals a coordinator may weigh when two candidates clear the same
capability bar — what a model is actually good at, how fast it feels, how much
it emits per second, and what a benchmark claims — so routing is not reduced to
"cheapest thing above the tier line".

The load-bearing rule is provenance. Every quantitative claim in
``data/model-catalog.json`` carries a ``source``, and :func:`lint_catalog`
rejects one that does not. A routing decision made on an invented benchmark is
worse than one made on price alone, because it *looks* informed — and this
project's own charter is receipts before doctrine.

Absent data means unknown, never bad. :func:`speciality_score` returns a
neutral score for a model with no catalog row, so a newly added model is never
silently deprioritised for lacking measurements nobody has taken yet.

Overridable per repo with a ``.ctx-catalog.json`` at the workspace root, in the
same spirit as ``.ctx-prices.json``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CATALOG_FILENAME = ".ctx-catalog.json"
LATENCY_CLASSES = ("fast", "moderate", "deliberate")
# Unknown latency sorts as `moderate`, not `fast`: the costly error is assuming
# a deliberate model is snappy and building a latency-sensitive route on it.
DEFAULT_LATENCY = "moderate"


def _builtin_path() -> Path:
    return Path(__file__).resolve().parent / "data" / "model-catalog.json"


def load_catalog(workspace_root: Path | str | None = None) -> dict[str, Any]:
    """The shipped catalog, overlaid by a workspace ``.ctx-catalog.json``.

    Fail-open like every other data read in this package: an unreadable or
    malformed override is ignored rather than raised, because a broken catalog
    must degrade routing to price-and-tier, not break the run.
    """
    try:
        table = json.loads(_builtin_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"schema": "ctx.model-catalog/v1", "models": []}
    if workspace_root:
        override = Path(workspace_root) / CATALOG_FILENAME
        try:
            doc = json.loads(override.read_text(encoding="utf-8"))
            if isinstance(doc, dict) and isinstance(doc.get("models"), list):
                # Overrides are merged per `match`, so a repo can adjust one
                # model without restating the whole table.
                by_match = {str(m.get("match", "")): dict(m)
                            for m in table.get("models", [])}
                for row in doc["models"]:
                    key = str(row.get("match", ""))
                    if key:
                        by_match.setdefault(key, {}).update(row)
                table = {**table, **{k: v for k, v in doc.items() if k != "models"},
                         "models": list(by_match.values())}
        except (OSError, ValueError):
            pass
    return table


def _token_matches(match: str, model: str) -> bool:
    """Same letter-boundary rule as ctx.pricing: `claude-opus` matches
    `claude-opus-4.8` but not `claude-opusx`."""
    m, low = (match or "").lower(), (model or "").lower()
    if not m or not low or m not in low:
        return False
    i = low.index(m)
    before_ok = i == 0 or not low[i - 1].isalnum()
    j = i + len(m)
    after_ok = j == len(low) or not low[j].isalnum()
    return before_ok and after_ok


def entry_for(model: str, *, table: dict[str, Any] | None = None,
              workspace_root: Path | str | None = None) -> dict[str, Any]:
    """The catalog row for a model id, or ``{}`` when nothing matches.

    Entries are ordered specific->general and the first match wins, mirroring
    the price table so the two never disagree about which row applies.
    """
    tbl = table if table is not None else load_catalog(workspace_root)
    for row in tbl.get("models") or []:
        if _token_matches(str(row.get("match", "")), model):
            return row
    return {}


def specialities(model: str, **kw) -> tuple[str, ...]:
    return tuple(entry_for(model, **kw).get("specialities") or ())


def anti_specialities(model: str, **kw) -> tuple[str, ...]:
    """Work this model is declared to be a poor fit for. Advisory: it never
    blocks a route, it only breaks ties away from a known-bad match."""
    return tuple(entry_for(model, **kw).get("anti_specialities") or ())


def latency_class(model: str, **kw) -> str:
    got = str(entry_for(model, **kw).get("latency_class") or "")
    return got if got in LATENCY_CLASSES else DEFAULT_LATENCY


def throughput(model: str, **kw) -> float | None:
    """Measured output tokens/second, or None when nobody has measured it.

    None is not zero and not slow — callers must treat it as unknown.
    """
    row = entry_for(model, **kw).get("throughput_output_tok_s") or {}
    val = row.get("median")
    return float(val) if isinstance(val, (int, float)) else None


def benchmark(model: str, suite: str, **kw) -> dict[str, Any] | None:
    """A sourced benchmark score, or None. Never returns an unsourced score:
    :func:`lint_catalog` keeps them out of the table in the first place."""
    got = (entry_for(model, **kw).get("benchmarks") or {}).get(suite)
    return got if isinstance(got, dict) and got.get("source") else None


def speciality_score(model: str, need_tags: tuple[str, ...], **kw) -> int:
    """How well a model's declared specialities cover the requested work.

    Positive per covered tag, negative per anti-speciality hit. A model with no
    catalog row scores 0 — neutral, because unknown is not bad. Advisory only:
    the router still gates on capability tier and breaks ties on price.
    """
    if not need_tags:
        return 0
    spec = set(specialities(model, **kw))
    anti = set(anti_specialities(model, **kw))
    want = set(need_tags)
    return len(want & spec) - 2 * len(want & anti)


def lint_catalog(table: dict[str, Any] | None = None) -> list[str]:
    """Provenance check: every quantitative claim must name a source.

    Returns a list of human-readable problems (empty when clean) rather than
    raising, so it can be used both as a test assertion and as a `ctx doctor`
    line without two code paths.
    """
    tbl = table if table is not None else load_catalog()
    problems: list[str] = []
    for row in tbl.get("models") or []:
        name = str(row.get("match", "?"))
        if row.get("latency_class") and not row.get("latency_source"):
            problems.append(f"{name}: latency_class without latency_source")
        if row.get("latency_class") and row["latency_class"] not in LATENCY_CLASSES:
            problems.append(f"{name}: unknown latency_class {row['latency_class']!r}")
        tp = row.get("throughput_output_tok_s")
        if isinstance(tp, dict):
            if not tp.get("source"):
                problems.append(f"{name}: throughput without a source")
            if not isinstance(tp.get("median"), (int, float)):
                problems.append(f"{name}: throughput without a numeric median")
        for suite, got in (row.get("benchmarks") or {}).items():
            if not isinstance(got, dict) or not got.get("source"):
                problems.append(f"{name}: benchmark {suite!r} without a source")
            elif not isinstance(got.get("score"), (int, float)):
                problems.append(f"{name}: benchmark {suite!r} without a numeric score")
        for key in ("observed_behaviour", "observed_cost_risk"):
            got = row.get(key)
            if isinstance(got, dict) and not got.get("source"):
                problems.append(f"{name}: {key} without a source")
    return problems
