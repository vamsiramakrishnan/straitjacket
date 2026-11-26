"""Paired A/B and historical baseline reporting for AccioBench (CommerceAgentBench).

Loads per-trial ``RunMetrics`` records from ``runs_dir``, collapses repeats per
``(task_id, arm)`` using majority pass and median telemetry, and renders
single-arm summaries, Straitjacket lever telemetry, and task-aligned A/B deltas
stratified by category and bucket across ``table``, ``markdown``, and ``json``
formats.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
import statistics
import sys
from typing import Any

from .config import RunMetrics

_ORDERED_CATEGORIES = ("api", "browser", "cli", "file")
_ORDERED_BUCKETS = ("worse", "better", "unknown")
_EXCLUDED_PATH_PARTS = frozenset(
    {"workspace", "outputs", "mock_case", "mock_services", "mock_state", "ctx_state", "ctxws", "extracts", "tmp",
     # previous configurations' trials moved aside by resume (loop._supersede)
     "superseded"}
)
_INFRA_ERROR_TERMINATIONS = frozenset({"api_error", "setup_error"})


def _infer_arm_from_legacy(raw: dict[str, Any]) -> str:
    arm = str(raw.get("arm") or "").strip()
    if arm:
        return arm
    arm_key = str(raw.get("arm_key") or "").strip()
    if not arm_key:
        return "sj"
    low = arm_key.lower()
    if "raw" in low or "base" in low or "naive" in low:
        return "raw"
    if "sj" in low or "ctx" in low:
        return "sj"
    return arm_key


def load_run_results(runs_dir: Path, arm: str | None = None) -> list[RunMetrics]:
    """Load ``RunMetrics`` records from ``runs_dir`` (optionally filtered by ``arm``)."""
    root = Path(runs_dir)
    if not root.exists():
        return []

    search_roots = [root]
    if (root / "runs").is_dir() and root.name != "runs":
        search_roots = [root / "runs", root]

    seen_paths: set[Path] = set()
    loaded: list[RunMetrics] = []
    for base in search_roots:
        for rf in sorted(base.rglob("result.json")):
            resolved = rf.resolve()
            if resolved in seen_paths:
                continue
            rel_parts = set(rf.relative_to(base).parts[:-1])
            if rel_parts & _EXCLUDED_PATH_PARTS:
                continue
            seen_paths.add(resolved)
            try:
                raw = json.loads(rf.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(raw, dict) or not raw.get("task_id"):
                continue
            normalized = dict(raw)
            normalized["arm"] = _infer_arm_from_legacy(normalized)
            normalized.setdefault("task_rel", str(normalized.get("task_id", "")))
            normalized.setdefault("category", "unknown")
            normalized.setdefault("model_id", "unknown")
            try:
                metric = RunMetrics.from_dict(normalized)
            except Exception:
                continue
            if arm is not None and metric.arm != arm:
                continue
            loaded.append(metric)

    loaded.sort(key=lambda m: (m.task_id, m.arm))
    return loaded


def _normalize_baseline_row(row: dict[str, Any]) -> dict[str, Any] | None:
    """Convert a historical or RunMetrics baseline dict into a uniform baseline record."""
    tid = str(row.get("task_id") or row.get("task") or "").strip()
    if not tid:
        return None

    # Historical combined_analysis.json format (steps38/tok38/dur38/status38/reward38)
    if "steps38" in row or "tok38" in row:
        diff_tok = row.get("diff_tok")
        bucket = str(row.get("bucket") or ("better" if (diff_tok is not None and float(diff_tok) <= 0) else "worse"))
        cap = float(row.get("cap38", row.get("reward38", 0.0)) or 0.0)
        passed = bool(row["passed38"]) if "passed38" in row else (str(row.get("status38", "")).lower() == "success")
        tot_tok = int(round(float(row.get("tok38", 0.0) or 0.0)))
        gen_tok = int(round(float(row.get("reas_tok38", 0.0) or 0.0) + float(row.get("msg_tok38", 0.0) or 0.0)))
        return {
            "task_id": tid,
            "category": str(row.get("category") or ""),
            "bucket": bucket,
            "steps": int(row.get("steps38", 0) or 0),
            "passed": passed,
            "capacity_score": cap,
            "wall_clock_s": float(row.get("dur38", 0.0) or 0.0),
            "prompt_tokens": max(0, tot_tok - gen_tok),
            "cached_tokens": 0,
            "thought_tokens": int(round(float(row.get("reas_tok38", 0.0) or 0.0))),
            "output_tokens": int(round(float(row.get("msg_tok38", 0.0) or 0.0))),
            "gen_tokens": gen_tok,
            "total_tokens": tot_tok,
        }

    prompt_tok = int(row.get("prompt_tokens", 0) or 0)
    cached_tok = int(row.get("cached_tokens", 0) or 0)
    thought_tok = int(row.get("thought_tokens", 0) or 0)
    output_tok = int(row.get("output_tokens", 0) or 0)
    gen_tok = int(row.get("gen_tokens", thought_tok + output_tok) or (thought_tok + output_tok))
    tot_tok = int(row.get("total_tokens", prompt_tok + gen_tok) or (prompt_tok + gen_tok))

    checks_passed = int(row.get("checks_passed", 0) or 0)
    checks_total = int(row.get("checks_total", 0) or 0)
    if "capacity_score" in row and row["capacity_score"] is not None:
        cap = float(row["capacity_score"])
    elif checks_total > 0:
        cap = checks_passed / checks_total
    else:
        cap = float(row.get("score", row.get("raw_verifier_score", 0.0)) or 0.0)

    if "passed" in row and row["passed"] is not None:
        passed = bool(row["passed"])
    else:
        passed = cap >= 0.9999

    wall_s = float(row.get("wall_clock_s", row.get("elapsed_sec", row.get("dur", 0.0))) or 0.0)
    category = str(row.get("category") or row.get("modality") or "").strip()
    bucket = str(row.get("bucket") or "unknown").strip() or "unknown"

    return {
        "task_id": tid,
        "category": category,
        "bucket": bucket,
        "steps": int(row.get("steps", 0) or 0),
        "passed": passed,
        "capacity_score": cap,
        "wall_clock_s": wall_s,
        "prompt_tokens": prompt_tok,
        "cached_tokens": cached_tok,
        "thought_tokens": thought_tok,
        "output_tokens": output_tok,
        "gen_tokens": gen_tok,
        "total_tokens": tot_tok,
    }


def load_baseline_records(baseline_json: Path) -> dict[str, dict[str, Any]]:
    """Load baseline task records keyed by ``task_id`` from a JSON file."""
    data = json.loads(Path(baseline_json).read_text(encoding="utf-8"))
    raw_rows: list[dict[str, Any]] = []

    if isinstance(data, list):
        raw_rows = [x for x in data if isinstance(x, dict)]
    elif isinstance(data, dict):
        if isinstance(data.get("results"), list):
            raw_rows = [x for x in data["results"] if isinstance(x, dict)]
        elif isinstance(data.get("runs"), list):
            raw_rows = [x for x in data["runs"] if isinstance(x, dict)]
        elif isinstance(data.get("records"), list):
            raw_rows = [x for x in data["records"] if isinstance(x, dict)]
        else:
            # Mapping of {task_id: metrics_dict}
            for k, v in data.items():
                if isinstance(v, dict):
                    item = dict(v)
                    item.setdefault("task_id", k)
                    raw_rows.append(item)

    # If multiple arms are stored in the baseline JSON, prefer "raw" records when present.
    arms_present = {_infer_arm_from_legacy(r) for r in raw_rows if "arm" in r or "arm_key" in r}
    if len(arms_present) > 1 and "raw" in arms_present:
        raw_rows = [r for r in raw_rows if _infer_arm_from_legacy(r) == "raw"]

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in raw_rows:
        norm = _normalize_baseline_row(row)
        if norm is not None:
            grouped.setdefault(norm["task_id"], []).append(norm)

    collapsed: dict[str, dict[str, Any]] = {}
    for tid, items in grouped.items():
        if len(items) == 1:
            collapsed[tid] = items[0]
            continue
        wins = sum(1 for x in items if x["passed"])
        first = items[0]
        collapsed[tid] = {
            "task_id": tid,
            "category": first["category"],
            "bucket": first["bucket"],
            "steps": int(round(statistics.median(x["steps"] for x in items))),
            "passed": wins * 2 > len(items),
            "capacity_score": float(statistics.median(x["capacity_score"] for x in items)),
            "wall_clock_s": float(statistics.median(x["wall_clock_s"] for x in items)),
            "prompt_tokens": int(round(statistics.median(x["prompt_tokens"] for x in items))),
            "cached_tokens": int(round(statistics.median(x["cached_tokens"] for x in items))),
            "thought_tokens": int(round(statistics.median(x["thought_tokens"] for x in items))),
            "output_tokens": int(round(statistics.median(x["output_tokens"] for x in items))),
            "gen_tokens": int(round(statistics.median(x["gen_tokens"] for x in items))),
            "total_tokens": int(round(statistics.median(x["total_tokens"] for x in items))),
        }
    return collapsed


def count_grading_errors(runs: list[RunMetrics]) -> int:
    """Trials whose agent run finished but whose grader failed (ungraded, not failed)."""
    return sum(1 for r in runs if r.grading_error and r.termination not in _INFRA_ERROR_TERMINATIONS)


def count_unsandboxed(runs: list[RunMetrics]) -> int:
    """Trials recorded as run without the bwrap sandbox (no filesystem isolation), whatever
    their outcome. Results whose isolation is unknown are counted by
    :func:`count_isolation_unknown` instead."""
    return sum(1 for r in runs if r.sandbox == "none")


def count_isolation_unknown(runs: list[RunMetrics]) -> int:
    """Trials whose isolation is unknown: written before the sandbox was recorded and
    never re-recorded (a legacy result reused on ``model_id`` alone). Every result is
    "bwrap", "none" or unknown; reports show the last two."""
    return sum(1 for r in runs if r.sandbox not in ("bwrap", "none"))


def _is_scorable(r: RunMetrics) -> bool:
    return r.termination not in _INFRA_ERROR_TERMINATIONS and not r.grading_error


def _collapse_arm_runs(runs: list[RunMetrics]) -> tuple[dict[str, RunMetrics], int]:
    """Collapse repeat trials per task_id for a single arm; return (per_task, error_count).

    Infra errors and grading errors are left out of every aggregate: neither says
    whether the agent solved the task. Grading errors are counted separately by
    :func:`count_grading_errors`."""
    by_task: dict[str, list[RunMetrics]] = {}
    error_count = 0
    for r in runs:
        if r.termination in _INFRA_ERROR_TERMINATIONS:
            error_count += 1
        by_task.setdefault(r.task_id, []).append(r)

    collapsed: dict[str, RunMetrics] = {}
    for tid, truns in sorted(by_task.items()):
        valid = [r for r in truns if _is_scorable(r)]
        if not valid:
            continue
        if len(valid) == 1:
            collapsed[tid] = valid[0]
            continue
        first = valid[0]
        wins = sum(1 for r in valid if r.passed)
        merged_tools: dict[str, int] = {}
        for k in {tk for r in valid for tk in r.tool_calls}:
            merged_tools[k] = int(round(statistics.median(r.tool_calls.get(k, 0) for r in valid)))
        merged_ctx: dict[str, int] = {}
        for k in {ck for r in valid for ck in r.ctx_retrievals}:
            merged_ctx[k] = int(round(statistics.median(r.ctx_retrievals.get(k, 0) for r in valid)))

        collapsed[tid] = RunMetrics(
            task_id=tid,
            task_rel=first.task_rel,
            category=first.category,
            arm=first.arm,
            model_id=first.model_id,
            wave=first.wave,
            bucket=first.bucket,
            steps=int(round(statistics.median(r.steps for r in valid))),
            prompt_tokens=int(round(statistics.median(r.prompt_tokens for r in valid))),
            cached_tokens=int(round(statistics.median(r.cached_tokens for r in valid))),
            thought_tokens=int(round(statistics.median(r.thought_tokens for r in valid))),
            output_tokens=int(round(statistics.median(r.output_tokens for r in valid))),
            max_step_prompt_tokens=int(round(statistics.median(r.max_step_prompt_tokens for r in valid))),
            message_chars=int(round(statistics.median(r.message_chars for r in valid))),
            wall_clock_s=float(statistics.median(r.wall_clock_s for r in valid)),
            tool_calls=merged_tools,
            dup_blocked=int(round(statistics.median(r.dup_blocked for r in valid))),
            digests_delivered=int(round(statistics.median(r.digests_delivered for r in valid))),
            inline_delivered=int(round(statistics.median(r.inline_delivered for r in valid))),
            bytes_kept_out=int(round(statistics.median(r.bytes_kept_out for r in valid))),
            ctx_retrievals=merged_ctx,
            compaction_epochs=int(round(statistics.median(r.compaction_epochs for r in valid))),
            thoughts_compacted=int(round(statistics.median(r.thoughts_compacted for r in valid))),
            file_dedup_hits=int(round(statistics.median(r.file_dedup_hits for r in valid))),
            state_notes_emitted=int(round(statistics.median(r.state_notes_emitted for r in valid))),
            state_notes_preserved=int(round(statistics.median(r.state_notes_preserved for r in valid))),
            mutations_preserved=int(round(statistics.median(r.mutations_preserved for r in valid))),
            cli_lsp_enrichments=int(round(statistics.median(r.cli_lsp_enrichments for r in valid))),
            emission_gate_hits=int(round(statistics.median(r.emission_gate_hits for r in valid))),
            ui_controls_stripped_chars=int(round(statistics.median(r.ui_controls_stripped_chars for r in valid))),
            loop_gate_triggers=int(round(statistics.median(r.loop_gate_triggers for r in valid))),
            termination=first.termination,
            passed=wins * 2 > len(valid),
            capacity_score=float(statistics.median(r.capacity_score for r in valid)),
            raw_verifier_score=float(statistics.median(r.raw_verifier_score for r in valid)),
            checks_passed=int(round(statistics.median(r.checks_passed for r in valid))),
            checks_total=int(round(statistics.median(r.checks_total for r in valid))),
        )
    return collapsed, error_count


def _pct_delta(treat: float, base: float) -> float | None:
    if base <= 0:
        return None
    return round((treat - base) / base * 100.0, 2)


def _summarize_metrics_slice(rows: list[RunMetrics], label: str) -> dict[str, Any]:
    n = len(rows)
    if n == 0:
        return {
            "label": label,
            "n": 0,
            "passed": 0,
            "solve_pct": 0.0,
            "capacity_mean": 0.0,
            "steps_total": 0,
            "steps_median": 0.0,
            "prompt_tokens": 0,
            "cached_tokens": 0,
            "cache_hit_pct": 0.0,
            "thought_tokens": 0,
            "output_tokens": 0,
            "gen_tokens": 0,
            "total_tokens": 0,
            "wall_clock_s": 0.0,
            "wall_clock_min": 0.0,
            "wall_median_s": 0.0,
        }

    passed = sum(1 for r in rows if r.passed)
    prompt_tok = sum(r.prompt_tokens for r in rows)
    cached_tok = sum(r.cached_tokens for r in rows)
    thought_tok = sum(r.thought_tokens for r in rows)
    out_tok = sum(r.output_tokens for r in rows)
    gen_tok = sum(r.gen_tokens for r in rows)
    tot_tok = sum(r.total_tokens for r in rows)
    wall_s = sum(r.wall_clock_s for r in rows)

    return {
        "label": label,
        "n": n,
        "passed": passed,
        "solve_pct": round(passed / n * 100.0, 2),
        "capacity_mean": round(statistics.mean(r.capacity_score for r in rows), 4),
        "steps_total": sum(r.steps for r in rows),
        "steps_median": round(float(statistics.median(r.steps for r in rows)), 1),
        "prompt_tokens": prompt_tok,
        "cached_tokens": cached_tok,
        "cache_hit_pct": round(cached_tok / prompt_tok * 100.0, 2) if prompt_tok > 0 else 0.0,
        "thought_tokens": thought_tok,
        "output_tokens": out_tok,
        "gen_tokens": gen_tok,
        "total_tokens": tot_tok,
        "wall_clock_s": round(wall_s, 1),
        "wall_clock_min": round(wall_s / 60.0, 2),
        "wall_median_s": round(float(statistics.median(r.wall_clock_s for r in rows)), 1),
    }


def _summarize_lever_telemetry(rows: list[RunMetrics]) -> dict[str, Any]:
    prompt_tok = sum(r.prompt_tokens for r in rows)
    cached_tok = sum(r.cached_tokens for r in rows)
    bytes_out = sum(r.bytes_kept_out for r in rows)
    ctx_ret: dict[str, int] = {}
    for r in rows:
        for k, v in r.ctx_retrievals.items():
            ctx_ret[k] = ctx_ret.get(k, 0) + v
    return {
        "tasks": len(rows),
        "digests_delivered": sum(r.digests_delivered for r in rows),
        "inline_delivered": sum(r.inline_delivered for r in rows),
        "bytes_kept_out": bytes_out,
        "mb_kept_out": round(bytes_out / (1024 * 1024), 2),
        "dup_blocked": sum(r.dup_blocked for r in rows),
        "file_dedup_hits": sum(r.file_dedup_hits for r in rows),
        "compaction_epochs": sum(r.compaction_epochs for r in rows),
        "thoughts_compacted": sum(r.thoughts_compacted for r in rows),
        "state_notes_emitted": sum(r.state_notes_emitted for r in rows),
        "state_notes_preserved": sum(r.state_notes_preserved for r in rows),
        "mutations_preserved": sum(r.mutations_preserved for r in rows),
        "cli_lsp_enrichments": sum(r.cli_lsp_enrichments for r in rows),
        "emission_gate_hits": sum(r.emission_gate_hits for r in rows),
        "ui_controls_stripped_chars": sum(r.ui_controls_stripped_chars for r in rows),
        "loop_gate_triggers": sum(r.loop_gate_triggers for r in rows),
        "cache_hit_pct": round(cached_tok / prompt_tok * 100.0, 2) if prompt_tok > 0 else 0.0,
        "ctx_retrievals": dict(sorted(ctx_ret.items())),
    }


def _metric_to_baseline_dict(m: RunMetrics) -> dict[str, Any]:
    return {
        "task_id": m.task_id,
        "category": m.category,
        "bucket": m.bucket,
        "steps": m.steps,
        "passed": m.passed,
        "capacity_score": m.capacity_score,
        "wall_clock_s": m.wall_clock_s,
        "prompt_tokens": m.prompt_tokens,
        "cached_tokens": m.cached_tokens,
        "thought_tokens": m.thought_tokens,
        "output_tokens": m.output_tokens,
        "gen_tokens": m.gen_tokens,
        "total_tokens": m.total_tokens,
    }


def _summarize_paired_slice(
    pairs: list[tuple[dict[str, Any], RunMetrics]],
    label: str,
) -> dict[str, Any]:
    n = len(pairs)
    if n == 0:
        return {"label": label, "n": 0}

    b_pass = sum(1 for b, _ in pairs if b["passed"])
    t_pass = sum(1 for _, t in pairs if t.passed)
    both_pass = sum(1 for b, t in pairs if b["passed"] and t.passed)
    t_only = sum(1 for b, t in pairs if (not b["passed"]) and t.passed)
    b_only = sum(1 for b, t in pairs if b["passed"] and (not t.passed))
    both_fail = sum(1 for b, t in pairs if (not b["passed"]) and (not t.passed))

    b_solve_pct = round(b_pass / n * 100.0, 2)
    t_solve_pct = round(t_pass / n * 100.0, 2)
    ev_pres = round(t_pass / b_pass, 4) if b_pass > 0 else (1.0 if t_pass == 0 else None)

    b_cap = statistics.mean(b["capacity_score"] for b, _ in pairs)
    t_cap = statistics.mean(t.capacity_score for _, t in pairs)

    b_steps = sum(b["steps"] for b, _ in pairs)
    t_steps = sum(t.steps for _, t in pairs)
    b_med_steps = float(statistics.median(b["steps"] for b, _ in pairs))
    t_med_steps = float(statistics.median(t.steps for _, t in pairs))

    b_wall = sum(b["wall_clock_s"] for b, _ in pairs)
    t_wall = sum(t.wall_clock_s for _, t in pairs)
    b_med_wall = float(statistics.median(b["wall_clock_s"] for b, _ in pairs))
    t_med_wall = float(statistics.median(t.wall_clock_s for _, t in pairs))

    b_prompt = sum(b["prompt_tokens"] for b, _ in pairs)
    t_prompt = sum(t.prompt_tokens for _, t in pairs)
    b_cached = sum(b["cached_tokens"] for b, _ in pairs)
    t_cached = sum(t.cached_tokens for _, t in pairs)
    b_gen = sum(b["gen_tokens"] for b, _ in pairs)
    t_gen = sum(t.gen_tokens for _, t in pairs)
    b_tot = sum(b["total_tokens"] for b, _ in pairs)
    t_tot = sum(t.total_tokens for _, t in pairs)

    return {
        "label": label,
        "n": n,
        "baseline_passed": b_pass,
        "treatment_passed": t_pass,
        "baseline_solve_pct": b_solve_pct,
        "treatment_solve_pct": t_solve_pct,
        "solve_delta_pp": round(t_solve_pct - b_solve_pct, 2),
        "evidence_preservation": ev_pres,
        "both_passed": both_pass,
        "treatment_only_passed": t_only,
        "baseline_only_passed": b_only,
        "both_failed": both_fail,
        "baseline_capacity_mean": round(b_cap, 4),
        "treatment_capacity_mean": round(t_cap, 4),
        "capacity_delta": round(t_cap - b_cap, 4),
        "baseline_steps": b_steps,
        "treatment_steps": t_steps,
        "steps_delta": t_steps - b_steps,
        "steps_delta_pct": _pct_delta(t_steps, b_steps),
        "baseline_median_steps": round(b_med_steps, 1),
        "treatment_median_steps": round(t_med_steps, 1),
        "baseline_wall_s": round(b_wall, 1),
        "treatment_wall_s": round(t_wall, 1),
        "baseline_wall_min": round(b_wall / 60.0, 2),
        "treatment_wall_min": round(t_wall / 60.0, 2),
        "wall_delta_pct": _pct_delta(t_wall, b_wall),
        "baseline_median_wall_s": round(b_med_wall, 1),
        "treatment_median_wall_s": round(t_med_wall, 1),
        "baseline_prompt_tokens": b_prompt,
        "treatment_prompt_tokens": t_prompt,
        "prompt_tokens_delta_pct": _pct_delta(t_prompt, b_prompt),
        "baseline_gen_tokens": b_gen,
        "treatment_gen_tokens": t_gen,
        "gen_tokens_delta_pct": _pct_delta(t_gen, b_gen),
        "baseline_total_tokens": b_tot,
        "treatment_total_tokens": t_tot,
        "total_tokens_delta_pct": _pct_delta(t_tot, b_tot),
        "baseline_cache_hit_pct": round(b_cached / b_prompt * 100.0, 2) if b_prompt > 0 else 0.0,
        "treatment_cache_hit_pct": round(t_cached / t_prompt * 100.0, 2) if t_prompt > 0 else 0.0,
    }


def _ordered_keys(found: set[str], canonical: tuple[str, ...]) -> list[str]:
    known = [k for k in canonical if k in found]
    extras = sorted(k for k in found if k and k not in canonical)
    return known + extras


def compute_summary_payload(
    results: list[RunMetrics],
    *,
    baseline_json: Path | None = None,
) -> dict[str, Any]:
    """Build structured summary and paired A/B delta dictionary."""
    by_arm_raw: dict[str, list[RunMetrics]] = {}
    for r in results:
        by_arm_raw.setdefault(r.arm, []).append(r)

    arm_names = _ordered_keys(set(by_arm_raw.keys()), ("raw", "sj"))
    collapsed_by_arm: dict[str, dict[str, RunMetrics]] = {}
    errors_by_arm: dict[str, int] = {}
    for a in arm_names:
        collapsed_by_arm[a], errors_by_arm[a] = _collapse_arm_runs(by_arm_raw[a])

    ext_baseline: dict[str, dict[str, Any]] | None = None
    if baseline_json is not None:
        ext_baseline = load_baseline_records(Path(baseline_json))
        # Enrich unknown buckets/categories on live runs from baseline metadata when available
        for a_map in collapsed_by_arm.values():
            for tid, m in a_map.items():
                b_rec = ext_baseline.get(tid)
                if b_rec is None:
                    continue
                if (not m.bucket or m.bucket == "unknown") and b_rec.get("bucket") and b_rec["bucket"] != "unknown":
                    m.bucket = b_rec["bucket"]
                if (not m.category or m.category == "unknown") and b_rec.get("category"):
                    m.category = b_rec["category"]

    arms_payload: dict[str, Any] = {}
    for a in arm_names:
        rows = list(collapsed_by_arm[a].values())
        cats = _ordered_keys({r.category for r in rows}, _ORDERED_CATEGORIES)
        buckets = _ordered_keys({r.bucket or "unknown" for r in rows}, _ORDERED_BUCKETS)
        arms_payload[a] = {
            "arm": a,
            "raw_runs": len(by_arm_raw[a]),
            "completed_tasks": len(rows),
            "error_runs": errors_by_arm[a],
            "grading_error_runs": count_grading_errors(by_arm_raw[a]),
            "unsandboxed_runs": count_unsandboxed(by_arm_raw[a]),
            "isolation_unknown_runs": count_isolation_unknown(by_arm_raw[a]),
            "overall": _summarize_metrics_slice(rows, "All Tasks"),
            "by_category": {c: _summarize_metrics_slice([r for r in rows if r.category == c], f"Category: {c}") for c in cats},
            "by_bucket": {
                b: _summarize_metrics_slice([r for r in rows if (r.bucket or "unknown") == b], f"Bucket: {b}")
                for b in buckets
            },
        }

    # Determine paired baseline vs treatment if available
    paired_payload: dict[str, Any] | None = None
    baseline_label = ""
    treatment_label = ""
    baseline_map: dict[str, dict[str, Any]] = {}
    treatment_map: dict[str, RunMetrics] = {}

    if ext_baseline is not None and arm_names:
        treatment_label = "sj" if "sj" in collapsed_by_arm else arm_names[-1]
        baseline_label = "baseline_json"
        baseline_map = ext_baseline
        treatment_map = collapsed_by_arm[treatment_label]
    elif "raw" in collapsed_by_arm and "sj" in collapsed_by_arm:
        baseline_label = "raw"
        treatment_label = "sj"
        baseline_map = {tid: _metric_to_baseline_dict(m) for tid, m in collapsed_by_arm["raw"].items()}
        treatment_map = collapsed_by_arm["sj"]
    elif len(arm_names) >= 2:
        baseline_label = arm_names[0]
        treatment_label = arm_names[-1]
        baseline_map = {tid: _metric_to_baseline_dict(m) for tid, m in collapsed_by_arm[baseline_label].items()}
        treatment_map = collapsed_by_arm[treatment_label]

    unpaired: list[dict[str, Any]] = []
    if baseline_label and treatment_label:
        # A task one arm lacks (ungraded, API/setup error, not run, superseded) cannot be
        # compared: it is left out of every paired figure and counted here instead.
        # (An external baseline file covers the whole suite; only live tasks it lacks count.)
        candidates = (
            set(treatment_map) - set(baseline_map)
            if baseline_label == "baseline_json"
            else set(baseline_map) ^ set(treatment_map)
        )
        for tid in sorted(candidates):
            unpaired.append({
                "task_id": tid,
                "present": baseline_label if tid in baseline_map else treatment_label,
                "missing": treatment_label if tid in baseline_map else baseline_label,
            })

    if baseline_map and treatment_map:
        shared_ids = sorted(set(baseline_map.keys()) & set(treatment_map.keys()))
        pairs = [(baseline_map[tid], treatment_map[tid]) for tid in shared_ids]
        if pairs:
            pair_cats = _ordered_keys(
                {(t.category if t.category and t.category != "unknown" else b.get("category", "unknown")) for b, t in pairs},
                _ORDERED_CATEGORIES,
            )
            pair_buckets = _ordered_keys(
                {(t.bucket if t.bucket and t.bucket != "unknown" else b.get("bucket", "unknown")) or "unknown" for b, t in pairs},
                _ORDERED_BUCKETS,
            )
            by_cat_paired = {}
            for c in pair_cats:
                c_pairs = [
                    (b, t)
                    for b, t in pairs
                    if (t.category if t.category and t.category != "unknown" else b.get("category", "unknown")) == c
                ]
                if c_pairs:
                    by_cat_paired[c] = _summarize_paired_slice(c_pairs, f"Category: {c}")

            by_bucket_paired = {}
            for bk in pair_buckets:
                bk_pairs = [
                    (b, t)
                    for b, t in pairs
                    if ((t.bucket if t.bucket and t.bucket != "unknown" else b.get("bucket", "unknown")) or "unknown") == bk
                ]
                if bk_pairs:
                    by_bucket_paired[bk] = _summarize_paired_slice(bk_pairs, f"Bucket: {bk}")

            per_task_rows = []
            for b, t in pairs:
                per_task_rows.append(
                    {
                        "task_id": t.task_id,
                        "category": t.category if t.category and t.category != "unknown" else b.get("category", "unknown"),
                        "bucket": (t.bucket if t.bucket and t.bucket != "unknown" else b.get("bucket", "unknown")) or "unknown",
                        "baseline_passed": b["passed"],
                        "treatment_passed": t.passed,
                        "baseline_capacity": round(b["capacity_score"], 4),
                        "treatment_capacity": round(t.capacity_score, 4),
                        "baseline_steps": b["steps"],
                        "treatment_steps": t.steps,
                        "steps_delta": t.steps - b["steps"],
                        "baseline_gen_tokens": b["gen_tokens"],
                        "treatment_gen_tokens": t.gen_tokens,
                        "baseline_total_tokens": b["total_tokens"],
                        "treatment_total_tokens": t.total_tokens,
                        "baseline_wall_s": round(b["wall_clock_s"], 1),
                        "treatment_wall_s": round(t.wall_clock_s, 1),
                    }
                )

            paired_payload = {
                "baseline_arm": baseline_label,
                "treatment_arm": treatment_label,
                "paired_tasks": len(pairs),
                "overall": _summarize_paired_slice(pairs, "All Paired"),
                "by_category": by_cat_paired,
                "by_bucket": by_bucket_paired,
                "per_task": per_task_rows,
            }

    sj_rows = list(collapsed_by_arm.get("sj", {}).values())
    sj_telemetry = _summarize_lever_telemetry(sj_rows) if sj_rows else None

    return {
        "total_runs": len(results),
        "grading_error_runs": sum(a["grading_error_runs"] for a in arms_payload.values()),
        # Trials that ran without filesystem isolation (--allow-unsandboxed); always present.
        "unsandboxed_runs": sum(a["unsandboxed_runs"] for a in arms_payload.values()),
        # Trials whose isolation is unknown (results that predate sandbox recording).
        "isolation_unknown_runs": sum(a["isolation_unknown_runs"] for a in arms_payload.values()),
        "unpaired_tasks": len(unpaired),
        "unpaired": unpaired,
        "arms": arms_payload,
        "paired": paired_payload,
        "straitjacket_telemetry": sj_telemetry,
    }


def _fmt_pct(val: float | None) -> str:
    if val is None:
        return "n/a"
    return f"{val:+.1f}%"


def _fmt_ev(val: float | None) -> str:
    if val is None:
        return "n/a"
    return f"{val:.3f}"


def _sandbox_note(payload: dict[str, Any]) -> str:
    """Lines naming trials that ran without filesystem isolation and trials whose isolation
    is unknown, or "" when every trial ran in the bwrap sandbox."""

    def per_arm(key: str) -> str:
        return ", ".join(f"{name}={a[key]}" for name, a in payload["arms"].items() if a.get(key))

    lines: list[str] = []
    total = int(payload.get("unsandboxed_runs") or 0)
    if total:
        lines.append(
            f"NO FILESYSTEM ISOLATION: {total} trial(s) ran without the bwrap sandbox "
            f"({per_arm('unsandboxed_runs')}; --allow-unsandboxed). The agent could read grader "
            "and answer files; these scores are not comparable to sandboxed results."
        )
    unknown = int(payload.get("isolation_unknown_runs") or 0)
    if unknown:
        lines.append(
            f"ISOLATION UNKNOWN: {unknown} trial(s) predate sandbox recording "
            f"({per_arm('isolation_unknown_runs')}); they may have run without the bwrap sandbox."
        )
    return "\n".join(lines)


def _grading_error_note(payload: dict[str, Any]) -> str:
    """Lines naming ungraded trials per arm and unpaired tasks, or "" when there are none."""
    lines: list[str] = []
    per_arm = [
        f"{name}={a['grading_error_runs']}"
        for name, a in payload["arms"].items()
        if a.get("grading_error_runs")
    ]
    if per_arm:
        lines.append(
            f"Grading errors: {payload.get('grading_error_runs', 0)} trial(s) ungraded "
            f"({', '.join(per_arm)}); excluded from solve rate and score. "
            "Re-run with resume to re-grade them."
        )
    unpaired = payload.get("unpaired") or []
    if unpaired:
        shown = ", ".join(f"{u['task_id']} (no {u['missing']})" for u in unpaired[:8])
        more = f", +{len(unpaired) - 8} more" if len(unpaired) > 8 else ""
        lines.append(
            f"Unpaired tasks: {len(unpaired)} (an arm has no scorable result); "
            f"left out of paired deltas: {shown}{more}"
        )
    return "\n".join(lines)


def _render_table(payload: dict[str, Any]) -> str:
    arms = payload["arms"]
    gnote = _grading_error_note(payload)
    snote = _sandbox_note(payload)
    if not arms or all(a["completed_tasks"] == 0 for a in arms.values()):
        return "\n".join(x for x in (snote, "No completed runs found.", gnote) if x)

    lines: list[str] = []
    lines.append("=" * 136)
    lines.append(" ACCIOBENCH SUMMARY REPORT")
    lines.append("=" * 136)
    if snote:
        lines.extend(snote.splitlines())
        lines.append("-" * 136)
    lines.append(
        f"{'Arm / Slice':<28} | {'N':>4} | {'Solve':>14} | {'Cap':>6} | "
        f"{'Steps (sum/med)':>16} | {'GenTok':>11} | {'TotTok':>12} | {'Cache%':>6} | {'Wall(m)':>7}"
    )
    lines.append("-" * 136)

    for arm_name, arm_data in arms.items():
        ov = arm_data["overall"]
        if ov["n"] == 0:
            continue
        slices = [(f"[{arm_name}] All Tasks", ov)]
        for c, cs in arm_data["by_category"].items():
            if cs["n"] > 0:
                slices.append((f"  cat:{c}", cs))
        for b, bs in arm_data["by_bucket"].items():
            if bs["n"] > 0 and not (b == "unknown" and len(arm_data["by_bucket"]) == 1):
                slices.append((f"  bucket:{b}", bs))

        for label, s in slices:
            solve_str = f"{s['passed']}/{s['n']} ({s['solve_pct']:5.1f}%)"
            stp_str = f"{s['steps_total']:>5}/{s['steps_median']:>4.1f}"
            lines.append(
                f"{label:<28} | {s['n']:>4} | {solve_str:>14} | {s['capacity_mean']:>6.3f} | "
                f"{stp_str:>16} | {s['gen_tokens']:>11,} | {s['total_tokens']:>12,} | "
                f"{s['cache_hit_pct']:>5.1f}% | {s['wall_clock_min']:>7.1f}"
            )
    if gnote:
        lines.append(gnote)

    paired = payload.get("paired")
    if paired:
        b_lbl = paired["baseline_arm"]
        t_lbl = paired["treatment_arm"]
        lines.append("")
        lines.append("=" * 136)
        lines.append(f" PAIRED A/B DELTAS ({b_lbl} -> {t_lbl}, n={paired['paired_tasks']} shared tasks)")
        lines.append("=" * 136)
        lines.append(
            f"{'Slice':<24} | {'N':>3} | {'Solve (B->T)':<18} | {'EvPres':>6} | "
            f"{'Cap (B->T)':<15} | {'Steps (B->T / d%)':<22} | {'dGenTok':>8} | {'dTotTok':>8} | {'dWall':>8}"
        )
        lines.append("-" * 136)
        p_slices = [("All Paired", paired["overall"])]
        for c, cs in paired["by_category"].items():
            p_slices.append((f"Category: {c}", cs))
        for bk, bks in paired["by_bucket"].items():
            p_slices.append((f"Bucket: {bk}", bks))

        for label, ps in p_slices:
            if ps.get("n", 0) == 0:
                continue
            sol_s = f"{ps['baseline_solve_pct']:5.1f}->{ps['treatment_solve_pct']:5.1f}%"
            cap_s = f"{ps['baseline_capacity_mean']:.3f}->{ps['treatment_capacity_mean']:.3f}"
            stp_s = f"{ps['baseline_steps']:>4}->{ps['treatment_steps']:<4} ({_fmt_pct(ps['steps_delta_pct']):>6})"
            lines.append(
                f"{label:<24} | {ps['n']:>3} | {sol_s:<18} | {_fmt_ev(ps['evidence_preservation']):>6} | "
                f"{cap_s:<15} | {stp_s:<22} | {_fmt_pct(ps['gen_tokens_delta_pct']):>8} | "
                f"{_fmt_pct(ps['total_tokens_delta_pct']):>8} | {_fmt_pct(ps['wall_delta_pct']):>8}"
            )

    tel = payload.get("straitjacket_telemetry")
    if tel:
        lines.append("")
        lines.append("Straitjacket Lever Telemetry (sj):")
        lines.append(
            f"  digests={tel['digests_delivered']:,} ({tel['mb_kept_out']:.2f} MB kept out), "
            f"dup_blocked={tel['dup_blocked']:,} (file_dedup={tel['file_dedup_hits']:,}), "
            f"compaction_epochs={tel['compaction_epochs']:,}, thoughts_compacted={tel['thoughts_compacted']:,} tok"
        )
        lines.append(
            f"  state_notes={tel['state_notes_emitted']:,} emitted / {tel['state_notes_preserved']:,} preserved, "
            f"mutations_preserved={tel['mutations_preserved']:,}, cli_lsp={tel['cli_lsp_enrichments']:,}, "
            f"emission_gate={tel['emission_gate_hits']:,}, ui_stripped={tel['ui_controls_stripped_chars']:,} chars, "
            f"cache_hit={tel['cache_hit_pct']:.1f}%"
        )

    return "\n".join(lines)


def _render_markdown(payload: dict[str, Any]) -> str:
    arms = payload["arms"]
    gnote = _grading_error_note(payload)
    snote = _sandbox_note(payload)
    head = "## AccioBench Summary Report\n\n" + "".join(
        f"> **Warning:** {ln}\n\n" for ln in snote.splitlines()
    )
    if not arms or all(a["completed_tasks"] == 0 for a in arms.values()):
        return head + "No completed runs found.\n" + (
            "".join(f"\n> {ln}" for ln in gnote.splitlines()) + "\n" if gnote else ""
        )

    lines: list[str] = head.rstrip("\n").split("\n") + [""]
    lines.append("### Per-Arm Summary")
    lines.append("")
    lines.append(
        "| Arm / Slice | N | Solve | Capacity | Steps (sum / med) | Gen Tokens | Total Tokens | Cache Hit | Wall (min) |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")

    for arm_name, arm_data in arms.items():
        ov = arm_data["overall"]
        if ov["n"] == 0:
            continue
        slices = [(f"**`{arm_name}` (All)**", ov)]
        for c, cs in arm_data["by_category"].items():
            if cs["n"] > 0:
                slices.append((f"`{arm_name}` / cat:`{c}`", cs))
        for b, bs in arm_data["by_bucket"].items():
            if bs["n"] > 0 and not (b == "unknown" and len(arm_data["by_bucket"]) == 1):
                slices.append((f"`{arm_name}` / bucket:`{b}`", bs))

        for label, s in slices:
            lines.append(
                f"| {label} | {s['n']} | {s['passed']}/{s['n']} ({s['solve_pct']:.1f}%) | "
                f"{s['capacity_mean']:.3f} | {s['steps_total']} / {s['steps_median']:.1f} | "
                f"{s['gen_tokens']:,} | {s['total_tokens']:,} | {s['cache_hit_pct']:.1f}% | {s['wall_clock_min']:.1f} |"
            )
    if gnote:
        lines.append("")
        lines.extend(f"> {ln}" for ln in gnote.splitlines())

    paired = payload.get("paired")
    if paired:
        b_lbl = paired["baseline_arm"]
        t_lbl = paired["treatment_arm"]
        lines.append("")
        lines.append(f"### Paired A/B Deltas (`{b_lbl}` -> `{t_lbl}`, n={paired['paired_tasks']})")
        lines.append("")
        lines.append(
            "| Slice | N | Solve (Base -> Treat) | Evidence Pres. | Capacity (Base -> Treat) | Steps (Base -> Treat) | GenTok Δ% | TotTok Δ% | Wall Δ% |"
        )
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
        p_slices = [("**All Paired**", paired["overall"])]
        for c, cs in paired["by_category"].items():
            p_slices.append((f"Category: `{c}`", cs))
        for bk, bks in paired["by_bucket"].items():
            p_slices.append((f"Bucket: `{bk}`", bks))

        for label, ps in p_slices:
            if ps.get("n", 0) == 0:
                continue
            lines.append(
                f"| {label} | {ps['n']} | {ps['baseline_solve_pct']:.1f}% -> {ps['treatment_solve_pct']:.1f}% "
                f"({ps['solve_delta_pp']:+.1f} pp) | {_fmt_ev(ps['evidence_preservation'])} | "
                f"{ps['baseline_capacity_mean']:.3f} -> {ps['treatment_capacity_mean']:.3f} ({ps['capacity_delta']:+.3f}) | "
                f"{ps['baseline_steps']} -> {ps['treatment_steps']} ({_fmt_pct(ps['steps_delta_pct'])}) | "
                f"{_fmt_pct(ps['gen_tokens_delta_pct'])} | {_fmt_pct(ps['total_tokens_delta_pct'])} | "
                f"{_fmt_pct(ps['wall_delta_pct'])} |"
            )

    tel = payload.get("straitjacket_telemetry")
    if tel:
        lines.append("")
        lines.append("### Straitjacket Lever Telemetry (`sj`)")
        lines.append("")
        lines.append(
            f"- **Entry Gate**: `{tel['digests_delivered']:,}` digests delivered "
            f"(`{tel['mb_kept_out']:.2f} MB` kept out of prompt window), `{tel['inline_delivered']:,}` inline deliveries"
        )
        lines.append(
            f"- **Deduplication**: `{tel['dup_blocked']:,}` duplicate readonly calls blocked "
            f"(`{tel['file_dedup_hits']:,}` content-addressed `sha256` file re-read hits)"
        )
        lines.append(
            f"- **Residence & Compaction**: `{tel['compaction_epochs']:,}` compaction epochs, "
            f"`{tel['thoughts_compacted']:,}` thought tokens compacted, "
            f"`{tel['state_notes_emitted']:,}` `state_note` checkpoints emitted (`{tel['state_notes_preserved']:,}` preserved), "
            f"`{tel['mutations_preserved']:,}` mutating turns preserved, "
            f"`{tel['ui_controls_stripped_chars']:,}` stale UI control chars stripped"
        )
        lines.append(
            f"- **Interface & Finalization Gates**: `{tel['cli_lsp_enrichments']:,}` parallel `--help` enrichments, "
            f"`{tel['emission_gate_hits']:,}` `present_files` emission-gate catches, "
            f"`{tel['loop_gate_triggers']:,}` loop-gate interventions, `{tel['cache_hit_pct']:.1f}%` prompt cache hit rate"
        )

    return "\n".join(lines) + "\n"


def render_summary_report(
    results: list[RunMetrics],
    *,
    baseline_json: Path | None = None,
    format: str = "table",
    workdir: Path | None = None,
    dataset_root: Path | None = None,
    ctx_state_dir: Path | None = None,
    baseline_htmls: list[tuple[str, Path]] | None = None,
    ablation_json: Path | None = None,
    title: str | None = None,
) -> str:
    """Render single-arm and paired A/B benchmark summary in ``table``, ``markdown``, ``json``, or ``html`` format."""
    fmt = (format or "table").strip().lower()
    if fmt == "md":
        fmt = "markdown"
    if fmt not in ("table", "markdown", "json", "html"):
        raise ValueError(
            f"Unsupported report format {format!r}; choose from 'table', 'markdown', 'json', or 'html'."
        )

    if fmt == "html":
        from .html_report import render_html_report

        return render_html_report(
            results,
            workdir=workdir,
            dataset_root=dataset_root,
            ctx_state_dir=ctx_state_dir,
            baseline_json=baseline_json,
            baseline_htmls=baseline_htmls,
            ablation_json=ablation_json,
            title=title,
        )

    payload = compute_summary_payload(results, baseline_json=baseline_json)
    if fmt == "json":
        return json.dumps(payload, indent=2, ensure_ascii=False)
    if fmt == "markdown":
        return _render_markdown(payload)
    return _render_table(payload)


# ---------------------------------------------------------------------------
# Multi-run / multi-model side-by-side tabulation (`acciobench_run.py tabulate`)
# ---------------------------------------------------------------------------


def _load_ctx_pricing(workspace_root: Path | None = None) -> tuple[Any, dict[str, Any]]:
    """Load ``ctx.pricing`` and its price table (honoring optional ``.ctx-prices.json``)."""
    try:
        from ctx import pricing as ctx_pricing  # type: ignore[import-not-found]
    except ImportError:
        repo_src = Path(__file__).resolve().parents[2] / "src"
        if repo_src.is_dir() and str(repo_src) not in sys.path:
            sys.path.insert(0, str(repo_src))
        from ctx import pricing as ctx_pricing  # type: ignore[import-not-found]
    return ctx_pricing, ctx_pricing.load_table(workspace_root)


def parse_tabulate_spec(spec: str) -> dict[str, Any]:
    """Parse a ``--run`` spec for ``tabulate``.

    Accepted forms::

        PATH                         # auto-label from model + arm (expands raw & sj)
        LABEL=PATH                   # explicit label for directory (or JSON file)
        LABEL=PATH:ARM               # select a single arm ('raw' / 'sj') or JSON key
        LABEL=BASE_DIR+OVERLAY[:ARM] # overlay task results from OVERLAY on top of BASE_DIR
    """
    raw = (spec or "").strip()
    if not raw:
        raise ValueError("Empty --run spec.")

    label: str | None = None
    target = raw
    if "=" in raw:
        cand_label, cand_rhs = raw.split("=", 1)
        if (
            cand_label.strip()
            and "/" not in cand_label
            and "\\" not in cand_label
            and cand_rhs.strip()
        ):
            label = cand_label.strip()
            target = cand_rhs.strip()

    selector: str | None = None
    path_part = target
    if ":" in target:
        cand_path, cand_sel = target.rsplit(":", 1)
        if (
            cand_sel.strip()
            and "/" not in cand_sel
            and "\\" not in cand_sel
            and len(cand_path.strip()) > 1
        ):
            path_part = cand_path.strip()
            selector = cand_sel.strip()

    single = Path(path_part).expanduser()
    if "+" in path_part and not single.exists():
        paths = [Path(seg.strip()).expanduser() for seg in path_part.split("+") if seg.strip()]
    else:
        paths = [single]

    if not paths:
        raise ValueError(f"Could not parse path from --run spec: {spec!r}")
    return {
        "label": label,
        "paths": paths,
        "selector": selector,
        "raw_spec": raw,
    }


def _trial_cost_usd(
    m: RunMetrics,
    *,
    ctx_pricing: Any,
    pricing_table: dict[str, Any],
) -> tuple[float, Any]:
    price = ctx_pricing.price_for(m.model_id or "", table=pricing_table)
    prompt = int(m.prompt_tokens or 0)
    cached = int(m.cached_tokens or 0)
    uncached = max(0, prompt - cached)
    gen = int(m.gen_tokens or (int(m.thought_tokens or 0) + int(m.output_tokens or 0)))
    usd = price.cost_usd(
        input_tokens=uncached,
        cache_read_tokens=cached,
        output_tokens=gen,
    )
    return float(usd), price


def _dominant_model(rows: list[RunMetrics]) -> str:
    counts: dict[str, int] = {}
    for r in rows:
        mid = str(r.model_id or "").strip()
        if mid and mid != "unknown":
            counts[mid] = counts.get(mid, 0) + 1
    if not counts:
        return "unknown"
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def _summarize_tabulate_slice(
    rows: list[RunMetrics],
    *,
    ctx_pricing: Any,
    pricing_table: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, dict[str, float]]]:
    scorable = [r for r in rows if _is_scorable(r)]
    infra_errors = sum(
        1
        for r in rows
        if r.grading_error or str(r.termination or "").strip() in _INFRA_ERROR_TERMINATIONS
    )
    unsandboxed = count_unsandboxed(scorable)
    isolation_unknown = count_isolation_unknown(scorable)

    n = len(scorable)
    solved = sum(1 for r in scorable if r.passed)
    caps = [float(r.capacity_score or 0.0) for r in scorable]
    steps_list = [int(r.steps or 0) for r in scorable]
    steps_total = sum(steps_list)
    prompt = sum(int(r.prompt_tokens or 0) for r in scorable)
    cached = sum(int(r.cached_tokens or 0) for r in scorable)
    uncached = sum(max(0, int(r.prompt_tokens or 0) - int(r.cached_tokens or 0)) for r in scorable)
    thought = sum(int(r.thought_tokens or 0) for r in scorable)
    output = sum(int(r.output_tokens or 0) for r in scorable)
    gen = sum(
        int(r.gen_tokens or (int(r.thought_tokens or 0) + int(r.output_tokens or 0)))
        for r in scorable
    )
    tot = prompt + gen
    wall_s = sum(float(r.wall_clock_s or 0.0) for r in scorable)
    med_wall_s = statistics.median([float(r.wall_clock_s or 0.0) for r in scorable]) if scorable else 0.0
    epochs = sum(int(r.compaction_epochs or 0) for r in scorable)
    gates = sum(int(r.emission_gate_hits or 0) for r in scorable)

    cost_usd = 0.0
    pricing_used: dict[str, dict[str, float]] = {}
    for r in scorable:
        usd, price = _trial_cost_usd(r, ctx_pricing=ctx_pricing, pricing_table=pricing_table)
        cost_usd += usd
        mid = str(r.model_id or "unknown").strip() or "unknown"
        if mid not in pricing_used:
            pricing_used[mid] = {
                "uncached_input_usd": round(price.input, 4),
                "cached_input_usd": round(price.cache_read, 4),
                "generated_usd": round(price.output, 4),
            }

    wall_m = wall_s / 60.0
    summary = {
        "n": n,
        "solved": solved,
        "solve_pct": round(100.0 * solved / n, 2) if n else 0.0,
        "macro_cap": round(sum(caps) / n, 4) if n else 0.0,
        "steps": steps_total,
        "med_steps": round(float(statistics.median(steps_list)), 1) if steps_list else 0.0,
        "steps_per_solve": round(steps_total / solved, 1) if solved else 0.0,
        "total_tokens": tot,
        "prompt_tokens": prompt,
        "cached_tokens": cached,
        "cache_hit_pct": round(100.0 * cached / prompt, 2) if prompt else 0.0,
        "uncached_tokens": uncached,
        "thought_tokens": thought,
        "output_tokens": output,
        "gen_tokens": gen,
        "cost_usd": round(cost_usd, 2),
        "cost_per_solve_usd": round(cost_usd / solved, 2) if solved else 0.0,
        "wall_s": round(wall_s, 1),
        "wall_m": round(wall_m, 1),
        "wall_h": round(wall_s / 3600.0, 2),
        "med_wall_s": round(float(med_wall_s), 1),
        "wall_m_per_solve": round(wall_m / solved, 1) if solved else 0.0,
        "compaction_epochs": epochs,
        "emission_gate_hits": gates,
        "infra_errors": infra_errors,
        "unsandboxed": unsandboxed,
        "isolation_unknown": isolation_unknown,
    }
    return summary, pricing_used


def _normalize_precomputed_slice(raw: dict[str, Any]) -> dict[str, Any]:
    n = int(raw.get("n") or 0)
    solved = int(raw.get("solved") or 0)
    steps = int(raw.get("steps", raw.get("steps_total", 0)) or 0)
    med_steps = float(raw.get("med_steps", raw.get("steps_median", 0.0)) or 0.0)
    prompt = int(raw.get("prompt_tokens") or 0)
    cached = int(raw.get("cached_tokens") or 0)
    uncached = int(raw.get("uncached_tokens", max(0, prompt - cached)) or 0)
    thought = int(raw.get("thought_tokens") or 0)
    output = int(raw.get("output_tokens") or 0)
    gen = int(raw.get("gen_tokens", thought + output) or 0)
    tot = int(raw.get("total_tokens", prompt + gen) or 0)
    cost = float(raw.get("cost_usd") or 0.0)
    wall_s = float(raw.get("wall_s", float(raw.get("wall_m", raw.get("wall_clock_min", 0.0)) or 0.0) * 60.0) or 0.0)
    wall_m = float(raw.get("wall_m", wall_s / 60.0) or 0.0)
    wall_h = float(raw.get("wall_h", wall_s / 3600.0) or 0.0)
    return {
        "n": n,
        "solved": solved,
        "solve_pct": round(float(raw.get("solve_pct", (100.0 * solved / n) if n else 0.0) or 0.0), 2),
        "macro_cap": round(float(raw.get("macro_cap", raw.get("capacity_mean", 0.0)) or 0.0), 4),
        "steps": steps,
        "med_steps": round(med_steps, 1),
        "steps_per_solve": round(float(raw.get("steps_per_solve", (steps / solved) if solved else 0.0) or 0.0), 1),
        "total_tokens": tot,
        "prompt_tokens": prompt,
        "cached_tokens": cached,
        "cache_hit_pct": round(
            float(raw.get("cache_hit_pct", (100.0 * cached / prompt) if prompt else 0.0) or 0.0),
            2,
        ),
        "uncached_tokens": uncached,
        "thought_tokens": thought,
        "output_tokens": output,
        "gen_tokens": gen,
        "cost_usd": round(cost, 2),
        "cost_per_solve_usd": round(
            float(raw.get("cost_per_solve_usd", (cost / solved) if solved else 0.0) or 0.0),
            2,
        ),
        "wall_s": round(wall_s, 1),
        "wall_m": round(wall_m, 1),
        "wall_h": round(wall_h, 2),
        "med_wall_s": round(float(raw.get("med_wall_s") or 0.0), 1),
        "wall_m_per_solve": round(
            float(raw.get("wall_m_per_solve", (wall_m / solved) if solved else 0.0) or 0.0),
            1,
        ),
        "compaction_epochs": int(raw.get("compaction_epochs") or 0),
        "emission_gate_hits": int(raw.get("emission_gate_hits") or 0),
        "infra_errors": int(raw.get("infra_errors", raw.get("grading_errors", 0)) or 0),
        "unsandboxed": int(raw.get("unsandboxed", raw.get("unsandboxed_runs", 0)) or 0),
        "isolation_unknown": int(raw.get("isolation_unknown", raw.get("isolation_unknown_runs", 0)) or 0),
    }


def _load_tabulate_json_entries(
    json_path: Path,
    *,
    label: str | None,
    selector: str | None,
) -> list[dict[str, Any]]:
    doc = json.loads(json_path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise ValueError(f"Expected JSON object in {json_path}")

    entries: list[dict[str, Any]] = []
    if isinstance(doc.get("overall"), dict):
        overall_map = doc["overall"]
        by_cat_map = doc.get("by_category") if isinstance(doc.get("by_category"), dict) else {}
        keys = [selector] if selector else list(overall_map.keys())
        for k in keys:
            if k not in overall_map or not isinstance(overall_map[k], dict):
                avail = ", ".join(sorted(overall_map.keys()))
                raise ValueError(f"Key {k!r} not found in {json_path} overall (available: {avail})")
            cat_out: dict[str, dict[str, Any]] = {}
            for cat_name, cat_arms in by_cat_map.items():
                if isinstance(cat_arms, dict) and isinstance(cat_arms.get(k), dict):
                    cat_out[str(cat_name)] = _normalize_precomputed_slice(cat_arms[k])
            row_label = label if (label and len(keys) == 1) else (f"{label} ({k})" if label else str(k))
            model_hint = str(
                overall_map[k].get("model")
                or (doc.get("model") if len(overall_map) == 1 else "")
                or "precomputed"
            )
            arm_hint = str(overall_map[k].get("arm") or k)
            entries.append(
                {
                    "label": row_label,
                    "model": model_hint,
                    "arm": arm_hint,
                    "source": str(json_path),
                    "source_kind": "json",
                    "per_task": None,
                    "overall": _normalize_precomputed_slice(overall_map[k]),
                    "by_category": cat_out,
                }
            )
        return entries

    if isinstance(doc.get("arms"), dict):
        arms_map = doc["arms"]
        keys = [selector] if selector else list(arms_map.keys())
        for k in keys:
            arm_doc = arms_map.get(k)
            if not isinstance(arm_doc, dict) or not isinstance(arm_doc.get("overall"), dict):
                avail = ", ".join(sorted(arms_map.keys()))
                raise ValueError(f"Arm {k!r} not found in {json_path} arms (available: {avail})")
            cat_out = {}
            if isinstance(arm_doc.get("by_category"), dict):
                for cat_name, cat_val in arm_doc["by_category"].items():
                    if isinstance(cat_val, dict):
                        cat_out[str(cat_name)] = _normalize_precomputed_slice(cat_val)
            row_label = label if (label and len(keys) == 1) else (f"{label} ({k})" if label else f"{json_path.stem} ({k})")
            entries.append(
                {
                    "label": row_label,
                    "model": str(arm_doc.get("model") or "precomputed"),
                    "arm": str(k),
                    "source": str(json_path),
                    "source_kind": "json",
                    "per_task": None,
                    "overall": _normalize_precomputed_slice(arm_doc["overall"]),
                    "by_category": cat_out,
                }
            )
        return entries

    raise ValueError(
        f"Unsupported JSON summary schema in {json_path}; expected 'overall' or 'arms' top-level object."
    )


def compute_tabulation_payload(
    run_specs: list[str],
    *,
    common_tasks_only: bool = False,
    pricing_workspace: Path | None = None,
    title: str | None = None,
) -> dict[str, Any]:
    """Load multiple workdirs (with optional ``:arm`` and ``+overlay``) or verdict JSONs and tabulate side-by-side."""
    if not run_specs:
        raise ValueError("Pass at least one --run spec to tabulate.")

    ctx_pricing, pricing_table = _load_ctx_pricing(pricing_workspace)
    raw_entries: list[dict[str, Any]] = []

    for raw_spec in run_specs:
        spec = parse_tabulate_spec(raw_spec)
        paths: list[Path] = spec["paths"]
        label: str | None = spec["label"]
        selector: str | None = spec["selector"]

        if len(paths) == 1 and paths[0].is_file() and paths[0].suffix.lower() == ".json":
            raw_entries.extend(_load_tabulate_json_entries(paths[0], label=label, selector=selector))
            continue

        for p in paths:
            if not p.exists():
                raise FileNotFoundError(f"Run path does not exist: {p}")

        per_path_results = [load_run_results(p, arm=selector) for p in paths]
        all_arms_found = {r.arm for batch in per_path_results for r in batch}
        if not all_arms_found:
            raise ValueError(
                f"No completed result.json files found for --run {raw_spec!r}"
                + (f" (arm={selector!r})" if selector else "")
            )
        ordered_arms = _ordered_keys(all_arms_found, ("raw", "sj"))

        for arm_name in ordered_arms:
            merged_by_task: dict[str, RunMetrics] = {}
            for batch in per_path_results:
                arm_rows = [r for r in batch if r.arm == arm_name]
                if not arm_rows:
                    continue
                collapsed, _ = _collapse_arm_runs(arm_rows)
                for tid, metric in collapsed.items():
                    merged_by_task[tid] = metric

            dom_model = _dominant_model(list(merged_by_task.values()))
            if label:
                row_label = label if len(ordered_arms) == 1 else f"{label} ({arm_name})"
            else:
                base_name = dom_model if dom_model != "unknown" else paths[0].name
                row_label = f"{base_name} ({arm_name})"

            raw_entries.append(
                {
                    "label": row_label,
                    "model": dom_model,
                    "arm": arm_name,
                    "source": "+".join(str(p) for p in paths),
                    "source_kind": "workdir",
                    "per_task": merged_by_task,
                    "overall": None,
                    "by_category": None,
                }
            )

    common_task_ids: set[str] | None = None
    if common_tasks_only:
        dir_entries = [e for e in raw_entries if isinstance(e.get("per_task"), dict)]
        if dir_entries:
            common_task_ids = set(dir_entries[0]["per_task"].keys())
            for e in dir_entries[1:]:
                common_task_ids &= set(e["per_task"].keys())

    pricing_by_model: dict[str, dict[str, float]] = {}
    categories_found: set[str] = set()
    final_runs: list[dict[str, Any]] = []

    for e in raw_entries:
        if e["source_kind"] == "workdir":
            per_task: dict[str, RunMetrics] = e["per_task"]
            if common_task_ids is not None:
                rows = [m for tid, m in sorted(per_task.items()) if tid in common_task_ids]
            else:
                rows = [m for _, m in sorted(per_task.items())]
            overall, used_prices = _summarize_tabulate_slice(
                rows,
                ctx_pricing=ctx_pricing,
                pricing_table=pricing_table,
            )
            pricing_by_model.update(used_prices)
            by_cat: dict[str, dict[str, Any]] = {}
            row_cats = _ordered_keys({r.category for r in rows}, _ORDERED_CATEGORIES)
            for c in row_cats:
                c_rows = [r for r in rows if r.category == c]
                c_sum, _ = _summarize_tabulate_slice(
                    c_rows,
                    ctx_pricing=ctx_pricing,
                    pricing_table=pricing_table,
                )
                by_cat[c] = c_sum
                categories_found.add(c)
        else:
            overall = e["overall"]
            by_cat = e["by_category"] or {}
            categories_found.update(by_cat.keys())

        final_runs.append(
            {
                "label": e["label"],
                "model": e["model"],
                "arm": e["arm"],
                "source": e["source"],
                "source_kind": e["source_kind"],
                "overall": overall,
                "by_category": by_cat,
            }
        )

    ordered_cats = _ordered_keys(categories_found, _ORDERED_CATEGORIES)
    return {
        "title": title or "AccioBench Multi-Run Comparison",
        "common_tasks_only": bool(common_tasks_only),
        "common_task_count": len(common_task_ids) if common_task_ids is not None else None,
        "categories": ordered_cats,
        "pricing_per_1m": pricing_by_model,
        "runs": final_runs,
    }


def _fmt_tok_m(tokens: int) -> str:
    if not tokens:
        return "—"
    return f"{tokens / 1_000_000:.1f}M"


def _fmt_gen_m(tokens: int) -> str:
    if not tokens:
        return "—"
    return f"{tokens / 1_000_000:.2f}M"


def _fmt_usd(val: float, *, has_tokens: bool) -> str:
    if val <= 0 and not has_tokens:
        return "—"
    return f"${val:.2f}"


def _render_tabulate_table(payload: dict[str, Any], *, include_categories: bool) -> str:
    runs = payload["runs"]
    cats = payload["categories"]
    lines: list[str] = []
    label_w = max(24, min(42, max((len(r["label"]) for r in runs), default=24)))

    title = payload.get("title") or "AccioBench Multi-Run Comparison"
    if payload.get("common_tasks_only") and payload.get("common_task_count") is not None:
        title += f" (common tasks n={payload['common_task_count']})"

    header = (
        f"{'Run / Arm':<{label_w}} | {'Solved':>14} | {'Cap':>6} | {'Steps (med)':>13} | "
        f"{'TotTok':>7} | {'Cache%':>6} | {'GenTok':>6} | {'Cost($)':>8} | {'$/Slv':>6} | {'Wall':>6} | {'m/Slv':>5}"
    )
    sep = "=" * len(header)
    lines.append(sep)
    lines.append(f" {title}")
    lines.append(sep)
    lines.append(header)
    lines.append("-" * len(header))

    warnings: list[str] = []
    for r in runs:
        m = r["overall"]
        has_tok = int(m["total_tokens"] or 0) > 0
        solved_s = f"{m['solved']}/{m['n']} ({m['solve_pct']:4.1f}%)"
        steps_s = f"{m['steps']} ({m['med_steps']:.1f})"
        cache_s = f"{m['cache_hit_pct']:.1f}%" if m["prompt_tokens"] else "—"
        wall_s = f"{m['wall_h']:.2f}h" if m["wall_s"] else "—"
        mps_s = f"{m['wall_m_per_solve']:.1f}" if (m["wall_s"] and m["solved"]) else "—"
        lines.append(
            f"{r['label']:<{label_w}} | {solved_s:>14} | {m['macro_cap']:>6.4f} | {steps_s:>13} | "
            f"{_fmt_tok_m(m['total_tokens']):>7} | {cache_s:>6} | {_fmt_gen_m(m['gen_tokens']):>6} | "
            f"{_fmt_usd(m['cost_usd'], has_tokens=has_tok):>8} | "
            f"{_fmt_usd(m['cost_per_solve_usd'], has_tokens=has_tok):>6} | {wall_s:>6} | {mps_s:>5}"
        )
        if m["infra_errors"] > 0:
            warnings.append(f"NOTE: {r['label']} has {m['infra_errors']} unscorable infra/grading error(s).")
        if m["unsandboxed"] > 0:
            warnings.append(f"WARNING: {r['label']} has {m['unsandboxed']} unsandboxed trial(s) (sandbox='none').")

    if include_categories and cats:
        lines.append("")
        col_w = max(14, min(22, max((len(r["label"]) for r in runs), default=14)))
        cat_hdr = f"{'Category':<12}" + "".join(f" | {r['label'][:col_w]:>{col_w}}" for r in runs)
        lines.append(cat_hdr)
        lines.append("-" * len(cat_hdr))
        for c in cats:
            row_cells = []
            for r in runs:
                cm = (r.get("by_category") or {}).get(c)
                if cm and cm.get("n"):
                    row_cells.append(f"{cm['solved']}/{cm['n']} ({cm['solve_pct']:4.1f}%)")
                else:
                    row_cells.append("—")
            lines.append(f"{c:<12}" + "".join(f" | {cell:>{col_w}}" for cell in row_cells))

    if warnings:
        lines.append("")
        lines.extend(warnings)

    return "\n".join(lines) + "\n"


def _render_tabulate_markdown(payload: dict[str, Any], *, include_categories: bool) -> str:
    runs = payload["runs"]
    cats = payload["categories"]
    title = payload.get("title") or "AccioBench Multi-Run Comparison"
    if payload.get("common_tasks_only") and payload.get("common_task_count") is not None:
        title += f" (common tasks n={payload['common_task_count']})"

    lines: list[str] = [f"## {title}", ""]
    lines.append(
        "| Run / Arm | Solved (%) | Macro Cap | Steps (Med / Per Solve) | Total Tokens | Cache Hit | Gen Tokens | Cost (USD) | $/Solve | Wall Time (min/Solve) | Epochs |"
    )
    lines.append("| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |")

    warnings: list[str] = []
    for r in runs:
        m = r["overall"]
        has_tok = int(m["total_tokens"] or 0) > 0
        cache_s = f"{m['cache_hit_pct']:.1f}%" if m["prompt_tokens"] else "—"
        wall_s = f"{m['wall_h']:.2f}h ({m['wall_m_per_solve']:.1f}m)" if m["wall_s"] else "—"
        lines.append(
            f"| {r['label']} | {m['solved']}/{m['n']} ({m['solve_pct']:.1f}%) | {m['macro_cap']:.4f} | "
            f"{m['steps']:,} ({m['med_steps']:.1f} / {m['steps_per_solve']:.1f}) | "
            f"{_fmt_tok_m(m['total_tokens'])} | {cache_s} | {_fmt_gen_m(m['gen_tokens'])} | "
            f"{_fmt_usd(m['cost_usd'], has_tokens=has_tok)} | "
            f"{_fmt_usd(m['cost_per_solve_usd'], has_tokens=has_tok)} | {wall_s} | {m['compaction_epochs']} |"
        )
        if m["infra_errors"] > 0:
            warnings.append(f"> Note: `{r['label']}` had {m['infra_errors']} unscorable infra/grading error(s).")
        if m["unsandboxed"] > 0:
            warnings.append(f"> Warning: `{r['label']}` had {m['unsandboxed']} unsandboxed trial(s) (`sandbox=\"none\"`).")

    if include_categories and cats:
        lines.append("")
        lines.append("### Category Breakdown (Solve Rate)")
        lines.append("")
        lines.append("| Category | " + " | ".join(r["label"] for r in runs) + " |")
        lines.append("| :--- | " + " | ".join(":---:" for _ in runs) + " |")
        for c in cats:
            cells = []
            for r in runs:
                cm = (r.get("by_category") or {}).get(c)
                if cm and cm.get("n"):
                    cells.append(f"{cm['solved']}/{cm['n']} ({cm['solve_pct']:.1f}%)")
                else:
                    cells.append("—")
            lines.append(f"| {c} | " + " | ".join(cells) + " |")

    if warnings:
        lines.append("")
        lines.extend(warnings)

    return "\n".join(lines) + "\n"


def _render_tabulate_csv(payload: dict[str, Any], *, include_categories: bool) -> str:
    buf = io.StringIO()
    cols = [
        "run",
        "slice",
        "model",
        "arm",
        "n",
        "solved",
        "solve_pct",
        "macro_cap",
        "steps",
        "med_steps",
        "steps_per_solve",
        "total_tokens",
        "prompt_tokens",
        "cached_tokens",
        "cache_hit_pct",
        "uncached_tokens",
        "gen_tokens",
        "cost_usd",
        "cost_per_solve_usd",
        "wall_h",
        "wall_m",
        "wall_m_per_solve",
        "compaction_epochs",
        "infra_errors",
        "unsandboxed",
    ]
    writer = csv.DictWriter(buf, fieldnames=cols)
    writer.writeheader()
    cats = payload["categories"] if include_categories else []
    for r in payload["runs"]:
        slices: list[tuple[str, dict[str, Any]]] = [("overall", r["overall"])]
        for c in cats:
            cm = (r.get("by_category") or {}).get(c)
            if cm:
                slices.append((c, cm))
        for slice_name, m in slices:
            writer.writerow(
                {
                    "run": r["label"],
                    "slice": slice_name,
                    "model": r["model"],
                    "arm": r["arm"],
                    "n": m["n"],
                    "solved": m["solved"],
                    "solve_pct": m["solve_pct"],
                    "macro_cap": m["macro_cap"],
                    "steps": m["steps"],
                    "med_steps": m["med_steps"],
                    "steps_per_solve": m["steps_per_solve"],
                    "total_tokens": m["total_tokens"],
                    "prompt_tokens": m["prompt_tokens"],
                    "cached_tokens": m["cached_tokens"],
                    "cache_hit_pct": m["cache_hit_pct"],
                    "uncached_tokens": m["uncached_tokens"],
                    "gen_tokens": m["gen_tokens"],
                    "cost_usd": m["cost_usd"],
                    "cost_per_solve_usd": m["cost_per_solve_usd"],
                    "wall_h": m["wall_h"],
                    "wall_m": m["wall_m"],
                    "wall_m_per_solve": m["wall_m_per_solve"],
                    "compaction_epochs": m["compaction_epochs"],
                    "infra_errors": m["infra_errors"],
                    "unsandboxed": m["unsandboxed"],
                }
            )
    return buf.getvalue()


def render_tabulation(
    run_specs_or_payload: list[str] | dict[str, Any],
    *,
    format: str = "table",
    include_categories: bool = True,
    common_tasks_only: bool = False,
    pricing_workspace: Path | None = None,
    title: str | None = None,
) -> str:
    """Render a multi-run / multi-model comparison table in ``table``, ``markdown``, ``csv``, or ``json`` format."""
    fmt = (format or "table").strip().lower()
    if fmt == "md":
        fmt = "markdown"
    if fmt not in ("table", "markdown", "csv", "json"):
        raise ValueError(
            f"Unsupported tabulate format {format!r}; choose from 'table', 'markdown', 'csv', or 'json'."
        )
    payload = (
        run_specs_or_payload
        if isinstance(run_specs_or_payload, dict)
        else compute_tabulation_payload(
            run_specs_or_payload,
            common_tasks_only=common_tasks_only,
            pricing_workspace=pricing_workspace,
            title=title,
        )
    )
    if fmt == "json":
        return json.dumps(payload, indent=2, ensure_ascii=False)
    if fmt == "csv":
        return _render_tabulate_csv(payload, include_categories=include_categories)
    if fmt == "markdown":
        return _render_tabulate_markdown(payload, include_categories=include_categories)
    return _render_tabulate_table(payload, include_categories=include_categories)
