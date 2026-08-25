"""Campaign harness for managed AlphaEvolve runs.

Three upgrades from Google's alphaevolve-on-googlecloud doctrine, applied to
every registered experiment:

1. Duplicate candidate source is evaluated once per campaign and replayed
   from an in-memory memo, so billed search never pays twice for identical
   code.
2. Every evaluation submits the primary scalar plus the per-metric component
   gains as separate named scores, so the program database can rank on any
   axis instead of one collapsed number.
3. Failures return typed, actionable insight text (the upstream loop feeds
   insights back to the model ensemble to steer the next generation), and
   successes report the worst case so search targets it.

After a run, :func:`write_campaign_artifacts` persists the ranking summary
and best program under ``evals/alphaevolve/results/`` — the local half of
Google's ``result.json`` / ``evolved_program`` convention. Artifacts are
evidence records only; they never write into ``src/ctx``.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from evals.alphaevolve.sandbox import INVALID_SCORE, controller_evaluation

_RESULTS_DIR = Path(__file__).resolve().parent / "results"

_HINTS: tuple[tuple[str, str], ...] = (
    ("timed out", "use a linear, allocation-light heuristic; avoid nested full scans"),
    ("non-deterministic", "remove randomness, clocks, set iteration order, and dict-order dependence"),
    ("blocked import", "stay within the allowlisted pure-stdlib imports"),
    ("blocked name", "avoid reflection; use direct calls on the supplied inputs only"),
    ("blocked syntax", "keep the block a small set of plain functions"),
    ("unsafe", "never choose an option that fails safety even when it is cheaper"),
    ("inadmissible", "choose only options the case allows"),
    ("incomplete", "every required capability must be provided or the case is a failure"),
    ("outside input", "return only valid in-range indices"),
    ("exceeds budget", "respect the requested budget exactly"),
    ("unique and ordered", "return strictly ascending unique indices"),
    ("must define", "keep the public function name and signature unchanged"),
    ("must return", "return exactly the documented type"),
    ("without a result", "the candidate process died before returning; simplify and retry"),
)

_ERROR_DETAIL_LIMIT = 600


class ExperimentModule(Protocol):
    METRIC_NAME: str
    def score_candidate(self, code: str) -> dict[str, Any]: ...


def _normalize(code: str) -> str:
    """Collapse whitespace-only edits that cannot change behavior.

    Trailing whitespace and blank lines are semantically inert in Python
    source, so stripping them makes formatting-only resubmissions hit the
    memo while any semantic token change misses it.
    """
    lines = [line.rstrip() for line in code.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _candidate_key(code: str) -> str:
    return hashlib.sha256(_normalize(code).encode("utf-8")).hexdigest()


def _hint_for(error: str) -> str | None:
    lowered = error.lower()
    for marker, hint in _HINTS:
        if marker in lowered:
            return hint
    return None


def _component_scores(metric_name: str, result: dict[str, Any]) -> list[dict[str, Any]]:
    """Secondary named scores: component log2 gains then totals, if present."""
    entries: list[dict[str, Any]] = []
    gains = result.get("log2_gain")
    if isinstance(gains, dict):
        for name in sorted(gains):
            value = gains[name]
            if isinstance(value, (int, float)):
                entries.append({"metric": f"{metric_name}.{name}_log2_gain", "score": float(value)})
    return entries


def _insight_text(result: dict[str, Any]) -> str:
    error = result.get("error")
    if error:
        detail = str(error)
        hint = _hint_for(detail)
        text = detail[:_ERROR_DETAIL_LIMIT]
        return f"{text} Hint: {hint}" if hint else text
    cases = result.get("cases")
    parts = [f"passed all hard gates; score={float(result['score']):.4f}"]
    gains = result.get("baseline_over_candidate")
    if isinstance(gains, dict) and gains:
        best = max(gains, key=lambda name: float(gains[name]))
        parts.append(
            f"baseline-over-candidate {best}={float(gains[best]):.3f}x"
        )
    log_gains = result.get("log2_gain")
    if isinstance(log_gains, dict) and log_gains:
        worst = min(log_gains, key=lambda name: float(log_gains[name]))
        parts.append(f"weakest metric={worst} (log2 gain {float(log_gains[worst]):.3f})")
    if isinstance(cases, dict) and cases:
        incomplete = sorted(
            name for name, doc in cases.items() if isinstance(doc, dict) and not doc.get("completion", 1.0)
        )
        if incomplete:
            parts.append("failing cases: " + ", ".join(incomplete))
    return "; ".join(parts)


class CampaignEvaluator:
    """Memoized, multi-metric adapter around one experiment's evaluator."""

    def __init__(self, module: ExperimentModule) -> None:
        self._module = module
        self._memo: dict[str, dict[str, Any]] = {}
        self.hits = 0
        self.misses = 0

    @property
    def stats(self) -> dict[str, int]:
        return {"evaluations": self.misses, "memo_hits": self.hits}

    def __call__(self, program_candidate: dict[str, Any]) -> dict[str, Any]:
        try:
            files = program_candidate["content"]["files"]
            code = str(files[0]["content"])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            return controller_evaluation(
                self._module.METRIC_NAME,
                INVALID_SCORE,
                f"invalid candidate envelope: {exc}",
            )
        key = _candidate_key(code)
        cached = self._memo.get(key)
        if cached is not None:
            self.hits += 1
            return json.loads(json.dumps(cached))  # deep copy; callers may mutate

        self.misses += 1
        started = time.perf_counter()
        result = self._module.score_candidate(code)
        wall_ms = (time.perf_counter() - started) * 1000.0
        metric = self._module.METRIC_NAME
        scores = [{"metric": metric, "score": float(result.get("score", INVALID_SCORE))}]
        scores.extend(_component_scores(metric, result))
        envelope = {
            "scores": {"scores": scores},
            "insights": {"insights": [{"label": "Evaluator", "text": _insight_text(result)}]},
            "campaign": {
                "wall_ms": round(wall_ms, 3),
                "elapsed_ms": result.get("elapsed_ms"),
                "cache_key": key[:16],
            },
        }
        self._memo[key] = envelope
        return envelope


def campaign_evaluator(module: ExperimentModule) -> CampaignEvaluator:
    return CampaignEvaluator(module)


def _safe_slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    return slug or "experiment"


def write_campaign_artifacts(
    module: ExperimentModule,
    experiment_name: str,
    ranking_rows: list[dict[str, Any]],
    *,
    config: dict[str, Any] | None = None,
    fingerprint: str | None = None,
    results_dir: Path | str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Persist the managed-campaign summary and best program as dated evidence."""
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H%M%SZ")
    base = Path(results_dir) if results_dir is not None else _RESULTS_DIR
    base.mkdir(parents=True, exist_ok=True)
    slug = _safe_slug(experiment_name)

    ranked = [
        row
        for row in ranking_rows
        if row.get("score") is not None
    ]
    ranked.sort(key=lambda row: float(row["score"]), reverse=True)
    best = ranked[0] if ranked else None
    winner_path: str | None = None
    if best is not None:
        content = best.pop("content", None)
        if isinstance(content, str) and content.strip():
            winner_path = str(base / f"{stamp}-{slug}-winner.py")
            Path(winner_path).write_text(content, encoding="utf-8")

    summary = {
        "schema": "ctx.alphaevolve-managed-campaign/v1",
        "experiment": experiment_name,
        "experiment_module": getattr(module, "__name__", slug),
        "metric": module.METRIC_NAME,
        "dataset_fingerprint": fingerprint,
        "config": config or {},
        "recorded_at": stamp,
        "programs_ranked": len(ranked),
        "top_programs": [
            {key: row[key] for key in ("program", "score", "evolved", "state") if key in row}
            for row in ranked[:10]
        ],
        "winner_artifact": winner_path,
    }
    summary_path = base / f"{stamp}-{slug}-managed.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return {"summary": str(summary_path), "winner": winner_path}
