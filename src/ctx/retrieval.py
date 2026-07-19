"""Bounded retrieval: ``ctx search``, ``ctx get``, ``ctx stats`` (SPEC §6.3-6.5).

Every result is deterministic, budget-capped, provenance-bearing, and — for
repository targets — snapshot-on-read so evidence stays retrievable after
the working tree changes.

This module is a byte-compatible facade: the implementation lives in the
private ``ctx._retrieval`` subpackage (split by concern — targets, the
optional ripgrep engine, search, get, spans, stats, telemetry — so no single
file carries the whole surface). Every name below, including the
underscore-prefixed ones other modules already depend on (``_span``,
``_emit``, ``_python_symbol_span``, ``_resolve_repo_targets``), is
re-exported here unchanged: ``from ctx.retrieval import X`` and
``ctx.retrieval.X`` both keep working exactly as before the split.
"""

from __future__ import annotations

from ctx._retrieval.common import (
    RetrievalError,
    _emit,
    _parse,
    _peek_blob,
    _read_bytes_range,
    _route_workspace,
    _span,
)
from ctx._retrieval.get import Selector, _python_symbol_span, get
from ctx._retrieval.rg_engine import RgMatch, _rg_available, _rg_repo_search
from ctx._retrieval.search import Match, _render_rg_search, search
from ctx._retrieval.spans import _resolve_span, _zoom_region
from ctx._retrieval.stats import _LANG_BY_EXT, _OUTLINE_MAX_ENTRIES, _stats_outline, stats
from ctx._retrieval.targets import (
    SearchTarget,
    _glob_match,
    _resolve_repo_targets,
    _resolve_run_targets,
    _stream_text,
)
from ctx._retrieval.telemetry import charge_turn_budget, record_telemetry, telemetry_summary

__all__ = [
    "RetrievalError",
    "Selector",
    "SearchTarget",
    "Match",
    "RgMatch",
    "search",
    "get",
    "stats",
    "charge_turn_budget",
    "record_telemetry",
    "telemetry_summary",
    "_span",
    "_emit",
    "_parse",
    "_peek_blob",
    "_read_bytes_range",
    "_route_workspace",
    "_python_symbol_span",
    "_resolve_span",
    "_zoom_region",
    "_resolve_run_targets",
    "_resolve_repo_targets",
    "_glob_match",
    "_stream_text",
    "_rg_available",
    "_rg_repo_search",
    "_render_rg_search",
    "_stats_outline",
    "_LANG_BY_EXT",
    "_OUTLINE_MAX_ENTRIES",
]
