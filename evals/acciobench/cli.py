"""Command-line interface for AccioBench (``doctor``, ``validate``, ``run``, ``report``)."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any

from .config import BenchConfig, select_tasks
from .orchestrator import run_suite
from .report import load_baseline_records, load_run_results, render_summary_report
from .validate import run_doctor, validate_task_referee

__all__ = ["build_parser", "main"]


def _env_path(var_name: str, default: str | None = None) -> Path | None:
    raw = (os.environ.get(var_name) or "").strip()
    if raw:
        return Path(raw)
    if default is not None:
        return Path(default)
    return None


def _add_runtime_options(parser: argparse.ArgumentParser, *, dataset_required: bool) -> None:
    env_ds = _env_path("ACCIOBENCH_DATASET_ROOT")
    parser.add_argument(
        "--dataset-root",
        type=Path,
        required=dataset_required and env_ds is None,
        default=env_ds if dataset_required else (env_ds or Path(".acciobench/CommerceAgentBench")),
        help="Path to CommerceAgentBench repository root (or set ACCIOBENCH_DATASET_ROOT)",
    )
    parser.add_argument(
        "--workdir",
        type=Path,
        default=_env_path("ACCIOBENCH_WORKDIR", ".acciobench/workdir"),
        help="Output/scratch directory for trial runs and ctx sidecar state (default: ./.acciobench/workdir)",
    )
    parser.add_argument(
        "--runtime-dir",
        type=Path,
        default=_env_path("ACCIOBENCH_RUNTIME_DIR"),
        help="Optional runtime directory containing mock_services/, bin/, and logistics_tracker_deps/",
    )
    parser.add_argument(
        "--ctx-bin",
        type=Path,
        default=_env_path("ACCIOBENCH_CTX_BIN"),
        help="Optional explicit path to the Straitjacket 'ctx' executable",
    )
    parser.add_argument(
        "--chrome-bin",
        type=Path,
        default=_env_path("ACCIOBENCH_CHROME_BIN"),
        help="Optional explicit path to Chrome/Chromium executable",
    )


def _add_provider_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--provider",
        choices=("gemini", "vertex"),
        default=(os.environ.get("ACCIOBENCH_PROVIDER") or "vertex").strip().lower(),
        help="Model API backend: 'gemini' (GEMINI_API_KEY) or 'vertex' (GCP ADC) (default: vertex)",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("ACCIOBENCH_MODEL") or "gemini-3.8-flash",
        help="Model identifier (default: gemini-3.8-flash)",
    )
    parser.add_argument(
        "--project",
        dest="project_id",
        default="",
        help="GCP project ID for --provider vertex (defaults to VERTEX_PROJECT_ID / GOOGLE_CLOUD_PROJECT)",
    )
    parser.add_argument(
        "--location",
        default=os.environ.get("VERTEX_LOCATION") or "global",
        help="Vertex AI location (default: global)",
    )
    parser.add_argument(
        "--api-key",
        default="",
        help="API key for --provider gemini (defaults to GEMINI_API_KEY)",
    )
    parser.add_argument(
        "--judge-provider",
        choices=("vertex", "gemini", "openai", "mock"),
        default=None,
        help="LLM judge backend for tasks with rubric judge checks (defaults to match --provider)",
    )
    parser.add_argument(
        "--judge-model",
        default="gemini-4-argon",
        help="LLM judge model identifier (default: gemini-4-argon)",
    )


def _build_bench_config(args: argparse.Namespace) -> BenchConfig:
    provider = str(getattr(args, "provider", "vertex") or "vertex").strip().lower()
    raw_judge_prov = getattr(args, "judge_provider", None)
    judge_provider = raw_judge_prov if raw_judge_prov else ("gemini" if provider == "gemini" else "vertex")

    raw_arm = getattr(args, "arm", "sj")
    if isinstance(raw_arm, (list, tuple)):
        arms_tuple = tuple(
            part.strip().lower()
            for item in raw_arm
            for part in str(item).split(",")
            if part.strip()
        )
    else:
        arms_tuple = tuple(p.strip().lower() for p in str(raw_arm).split(",") if p.strip())

    return BenchConfig(
        dataset_root=args.dataset_root,
        workdir=args.workdir,
        model=getattr(args, "model", "gemini-3.8-flash"),
        provider=provider,
        project_id=getattr(args, "project_id", ""),
        location=getattr(args, "location", "global"),
        api_key=getattr(args, "api_key", ""),
        judge_provider=judge_provider,
        judge_model=getattr(args, "judge_model", "gemini-4-argon"),
        arms=arms_tuple or ("sj",),
        workers=int(getattr(args, "workers", 4)),
        slot_offset=int(getattr(args, "slot_offset", 0)),
        max_steps=int(getattr(args, "max_steps", 135)),
        entry_gate_bytes=int(getattr(args, "entry_gate_bytes", 24_576)),
        baseline_raw_cap=int(getattr(args, "baseline_raw_cap", 400_000)),
        resume=not bool(getattr(args, "no_cache", False)),
        runtime_dir=getattr(args, "runtime_dir", None),
        ctx_bin=getattr(args, "ctx_bin", None),
        chrome_bin=getattr(args, "chrome_bin", None),
        thinking_level=getattr(args, "thinking_level", None),
        max_output_tokens=getattr(args, "max_output_tokens", None),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="acciobench_run.py",
        description="AccioBench (CommerceAgentBench) modular paired A/B harness for Straitjacket.",
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    # 1. doctor
    p_doc = sub.add_parser(
        "doctor",
        help="Verify Python packages, ctx binary, browser, dataset layout, and credentials.",
    )
    _add_runtime_options(p_doc, dataset_required=False)
    _add_provider_options(p_doc)
    p_doc.add_argument("--json", action="store_true", help="Emit JSON output")

    # 2. validate
    p_val = sub.add_parser(
        "validate",
        help="Run deterministic model-free sandbox + ctx capture + official grader referee.",
    )
    _add_runtime_options(p_val, dataset_required=True)
    _add_provider_options(p_val)
    p_val.add_argument(
        "--task",
        "--tasks",
        dest="tasks",
        action="append",
        default=None,
        help="Task ID(s) to validate (repeatable or comma-separated)",
    )
    p_val.add_argument(
        "--category",
        dest="categories",
        action="append",
        default=None,
        help="Filter candidate tasks by category (file, api, cli, browser)",
    )
    p_val.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of tasks to validate (default: all explicit --task IDs, else 1; 0 = all)",
    )
    p_val.add_argument(
        "--slot",
        type=int,
        default=0,
        help="Worker slot index for port remapping (default: 0 -> ports 20000..20099)",
    )
    p_val.add_argument("--json", action="store_true", help="Emit JSON output")

    # 3. run
    p_run = sub.add_parser(
        "run",
        help="Execute single-arm or paired A/B benchmark trials across CommerceAgentBench tasks.",
    )
    _add_runtime_options(p_run, dataset_required=True)
    _add_provider_options(p_run)
    p_run.add_argument(
        "--arm",
        "--arms",
        dest="arm",
        default="sj",
        help="Benchmark arm(s): 'sj', 'raw', or 'raw,sj' for paired A/B sweeps (default: sj)",
    )
    p_run.add_argument(
        "--tasks",
        "--task",
        dest="tasks",
        action="append",
        default=None,
        help="Explicit task ID(s) (repeatable or comma-separated)",
    )
    p_run.add_argument(
        "--category",
        dest="categories",
        action="append",
        default=None,
        help="Filter by category: file, api, cli, browser (repeatable or comma-separated)",
    )
    p_run.add_argument(
        "--wave-file",
        type=Path,
        default=None,
        help="Optional wave plan JSON file mapping wave names to task ID lists",
    )
    p_run.add_argument(
        "--wave",
        default=None,
        help="Wave name in --wave-file ('wave1', 'all', etc.) or comma-separated task IDs",
    )
    p_run.add_argument(
        "--sample",
        type=int,
        default=None,
        help="Deterministic subsample count from the selected task pool",
    )
    p_run.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for --sample (default: 42)",
    )
    p_run.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Concurrent worker threads / isolated port slots (default: 4)",
    )
    p_run.add_argument(
        "--slot-offset",
        type=int,
        default=0,
        help="Slot offset added to worker slot indices to avoid port collisions (default: 0)",
    )
    p_run.add_argument(
        "--max-steps",
        type=int,
        default=135,
        help="Maximum agent turns per trial (default: 135)",
    )
    p_run.add_argument(
        "--entry-gate-bytes",
        type=int,
        default=24_576,
        help="Straitjacket Entry Gate byte threshold before digesting via ctx (default: 24576)",
    )
    p_run.add_argument(
        "--baseline-raw-cap",
        type=int,
        default=400_000,
        help="Character truncation ceiling for raw inline tool outputs (default: 400000)",
    )
    p_run.add_argument(
        "--no-cache",
        action="store_true",
        help="Re-run tasks from scratch instead of resuming from completed result.json files",
    )
    p_run.add_argument(
        "--thinking-level",
        default=None,
        help="Optional ablation override for generationConfig.thinkingConfig (unset = model default)",
    )
    p_run.add_argument(
        "--max-output-tokens",
        type=int,
        default=None,
        help="Optional ablation override for generationConfig.maxOutputTokens (unset = model default)",
    )
    p_run.add_argument(
        "--baseline-json",
        type=Path,
        default=None,
        help="Optional baseline JSON for bucket tagging and end-of-run delta reporting",
    )
    p_run.add_argument(
        "--format",
        choices=("table", "markdown", "md", "json", "html"),
        default="table",
        help="End-of-run summary report format (default: table)",
    )
    p_run.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional path to write the rendered summary report",
    )
    p_run.add_argument(
        "--html-output",
        type=Path,
        default=None,
        help="Path for auto-generated standalone HTML report (default: <workdir>/acciobench_report.html)",
    )
    p_run.add_argument(
        "--no-html-report",
        action="store_true",
        help="Disable automatic <workdir>/acciobench_report.html generation at the end of 'run'",
    )

    # 4. report
    p_rep = sub.add_parser(
        "report",
        help="Render single-arm and paired A/B delta summaries or standalone interactive HTML reports.",
    )
    p_rep.add_argument(
        "--workdir",
        "--runs-dir",
        dest="workdir",
        type=Path,
        default=_env_path("ACCIOBENCH_WORKDIR", ".acciobench/workdir"),
        help="Workdir (or runs/ directory) containing completed *_meta/result.json files",
    )
    p_rep.add_argument(
        "--dataset-root",
        type=Path,
        default=_env_path("ACCIOBENCH_DATASET_ROOT", ".acciobench/CommerceAgentBench"),
        help="Optional CommerceAgentBench root to embed task.md prompts in HTML reports",
    )
    p_rep.add_argument(
        "--ctx-state-dir",
        type=Path,
        default=None,
        help="Optional explicit ctx_state directory (default: <workdir>/ctx_state)",
    )
    p_rep.add_argument(
        "--arm",
        default=None,
        help="Optional filter to load only a single arm ('sj' or 'raw')",
    )
    p_rep.add_argument(
        "--baseline-json",
        type=Path,
        default=None,
        help="Optional historical baseline JSON to pair against",
    )
    p_rep.add_argument(
        "--baseline-html",
        dest="baseline_htmls",
        action="append",
        default=None,
        help="Optional Accio HTML baseline report ('LABEL=PATH' or 'PATH', repeatable) for multi-arm traces",
    )
    p_rep.add_argument(
        "--ablation-json",
        type=Path,
        default=None,
        help="Optional controlled multi-arm ablation suite_summary.json to include in HTML reports",
    )
    p_rep.add_argument(
        "--title",
        default=None,
        help="Optional custom title for HTML reports (default: 'Straitjacket Eval')",
    )
    p_rep.add_argument(
        "--format",
        choices=("table", "markdown", "md", "json", "html"),
        default="table",
        help="Output format: 'table', 'markdown', 'json', or 'html' (default: table)",
    )
    p_rep.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional file path to save the rendered report (defaults to <workdir>/acciobench_report.html when --format html)",
    )

    return parser


def _cmd_doctor(args: argparse.Namespace) -> int:
    config = _build_bench_config(args)
    ok, lines = run_doctor(config)
    if args.json:
        print(json.dumps({"ok": ok, "checks": lines}, indent=2, ensure_ascii=False))
    else:
        for ln in lines:
            print(ln)
        status_str = "READY (core checks passed)" if ok else "INCOMPLETE (resolve [MISSING] items above)"
        print(f"\nAccioBench doctor: {status_str}")
    return 0 if ok else 1


def _cmd_validate(args: argparse.Namespace) -> int:
    config = _build_bench_config(args)
    explicit = bool(args.tasks)
    # When no explicit --task or --category is supplied, prefer pure 'file' smoke tasks if mock_services aren't staged.
    categories = args.categories
    if not explicit and not categories:
        from .sandbox import _resolve_runtime_paths

        golden_mocks, _, _ = _resolve_runtime_paths(config)
        if not golden_mocks.is_dir():
            categories = ["file"]

    task_ids, _ = select_tasks(
        config.dataset_root,
        tasks=args.tasks,
        categories=categories,
    )
    if not task_ids:
        if args.json:
            print(json.dumps({"ok": False, "passed": 0, "total": 0, "results": []}, indent=2))
        else:
            print("No tasks matched validation filters.", file=sys.stderr)
        return 1
    limit = args.limit
    if limit is None:
        limit = len(task_ids) if explicit else 1
    if limit > 0:
        task_ids = task_ids[:limit]

    outcomes: list[dict[str, Any]] = []
    failures = 0
    for tid in task_ids:
        res = validate_task_referee(config, tid, slot=int(args.slot))
        outcomes.append(res)
        if not res["ok"]:
            failures += 1
        if not args.json:
            mark = "[PASS]" if res["ok"] else "[FAIL]"
            err_suffix = f" | errors={'; '.join(res['errors'])}" if res["errors"] else ""
            print(
                f"{mark} {res['task_id']:<50} | cat={res['category']:<7} | "
                f"mocks={'Y' if res['has_mocks'] else 'N'} | digests={res['digests_delivered']} | "
                f"base_pass={res['baseline_passed']} ({res['checks_passed']}/{res['checks_total']}) | "
                f"leaks={res['leaked_processes']} | {res['elapsed_s']:.2f}s{err_suffix}",
                flush=True,
            )

    if args.json:
        print(
            json.dumps(
                {
                    "ok": failures == 0,
                    "passed": len(outcomes) - failures,
                    "total": len(outcomes),
                    "results": outcomes,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        print(f"\nReferee validation: {len(outcomes) - failures}/{len(outcomes)} passed.")
    return 0 if failures == 0 else 1


def _extract_bucket_map(wave_file: Path | None, baseline_json: Path | None) -> dict[str, str]:
    buckets: dict[str, str] = {}
    if wave_file is not None and wave_file.is_file():
        try:
            wf_data = json.loads(wave_file.read_text(encoding="utf-8"))
            if isinstance(wf_data, dict):
                for tid in wf_data.get("bucket_worse") or []:
                    buckets[str(tid)] = "worse"
                for tid in wf_data.get("bucket_better") or []:
                    buckets[str(tid)] = "better"
        except Exception:
            pass
    if baseline_json is not None and baseline_json.is_file():
        try:
            for tid, rec in load_baseline_records(baseline_json).items():
                if rec.get("bucket") and rec["bucket"] != "unknown":
                    buckets.setdefault(tid, str(rec["bucket"]))
        except Exception:
            pass
    return buckets


def _cmd_run(args: argparse.Namespace) -> int:
    config = _build_bench_config(args)
    task_ids, wave_map = select_tasks(
        config.dataset_root,
        tasks=args.tasks,
        categories=args.categories,
        wave_file=args.wave_file,
        wave=args.wave,
        sample=args.sample,
        seed=args.seed,
    )
    if not task_ids:
        print("No tasks matched the selection filters.", file=sys.stderr)
        return 1

    bucket_map = _extract_bucket_map(args.wave_file, args.baseline_json)
    print(
        f"Running AccioBench: tasks={len(task_ids)} | arms={','.join(config.arms)} | "
        f"provider={config.provider} | model={config.model} | workers={config.workers}",
        flush=True,
    )
    raised: list[Any] = []
    suite_results = run_suite(config, task_ids, wave_map=wave_map, bucket_map=bucket_map, raised=raised)

    all_results = load_run_results(config.runs_dir)
    selected_set = set(task_ids)
    # A trial that raised wrote nothing. Whatever its slot in runs/ holds is an earlier
    # run's result (for example a previous configuration's that could not be moved
    # aside), so it must not stand in for this run's in the reports below.
    not_recorded = {(r.task_id, r.arm) for r in raised}
    for r in raised:
        print(
            f"[not recorded] {r.task_id} ({r.arm}): {'; '.join(str(n) for n in r.notes) or 'the trial raised'}. "
            "No result was written for it; any earlier result in its slot is left out of this run's reports.",
            file=sys.stderr,
            flush=True,
        )
    scoped_results = [
        r for r in all_results
        if r.task_id in selected_set and r.arm in config.arms and (r.task_id, r.arm) not in not_recorded
    ] or suite_results

    # The trials are done and their results are on disk: a report that fails to render
    # must not cost the run. Say which one failed and how to regenerate it.
    def _report_failed(kind: str, fmt_name: str, exc: BaseException) -> None:
        print(
            f"\n[report] the {kind} report failed to render ({type(exc).__name__}: {exc}); "
            f"the run's results are intact in {config.runs_dir}. Regenerate it with:\n"
            f"  python evals/acciobench_run.py report --workdir {config.workdir} --format {fmt_name}"
            + (f" --dataset-root {config.dataset_root}" if fmt_name == "html" else ""),
            file=sys.stderr,
            flush=True,
        )

    def _write_html(out_path: Path, label: str) -> None:
        try:
            html_doc = render_summary_report(
                scoped_results,
                baseline_json=args.baseline_json,
                format="html",
                workdir=config.workdir,
                dataset_root=config.dataset_root,
                ctx_state_dir=config.ctx_state_home,
            )
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(html_doc, encoding="utf-8")
        except Exception as exc:
            _report_failed("HTML", "html", exc)
            return
        print(f"\n{label}: {out_path}")

    fmt = (args.format or "table").strip().lower()
    if fmt == "html":
        out_path = Path(args.output or args.html_output or (config.workdir / "acciobench_report.html")).expanduser().resolve()
        _write_html(out_path, "Interactive HTML report written to")
        return 0

    try:
        rendered = render_summary_report(
            scoped_results,
            baseline_json=args.baseline_json,
            format=args.format,
        )
        print("\n" + rendered)
        if args.output is not None:
            out_path = Path(args.output).expanduser().resolve()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(rendered, encoding="utf-8")
    except Exception as exc:
        _report_failed(fmt, fmt, exc)

    if not getattr(args, "no_html_report", False):
        html_target = Path(args.html_output or (config.workdir / "acciobench_report.html")).expanduser().resolve()
        _write_html(html_target, "Auto-generated interactive HTML report")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    results = load_run_results(args.workdir, arm=args.arm)
    fmt = (args.format or "table").strip().lower()
    if fmt == "html":
        from .html_report import parse_baseline_html_spec

        baseline_specs = [parse_baseline_html_spec(s) for s in (args.baseline_htmls or [])]
        rendered = render_summary_report(
            results,
            baseline_json=args.baseline_json,
            format="html",
            workdir=args.workdir,
            dataset_root=args.dataset_root,
            ctx_state_dir=args.ctx_state_dir,
            baseline_htmls=baseline_specs,
            ablation_json=args.ablation_json,
            title=args.title,
        )
        out_path = Path(args.output or (Path(args.workdir) / "acciobench_report.html")).expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered, encoding="utf-8")
        print(f"Interactive HTML report written to: {out_path} ({len(rendered):,} bytes)")
        return 0

    rendered = render_summary_report(
        results,
        baseline_json=args.baseline_json,
        format=args.format,
    )
    print(rendered)
    if args.output is not None:
        out_path = Path(args.output).expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered, encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.subcommand == "doctor":
            return _cmd_doctor(args)
        if args.subcommand == "validate":
            return _cmd_validate(args)
        if args.subcommand == "run":
            return _cmd_run(args)
        if args.subcommand == "report":
            return _cmd_report(args)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    parser.error(f"Unknown subcommand: {args.subcommand}")
    return 2
