"""Run or resume bounded AlphaEvolve optimization portfolio experiments."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import logging
import multiprocessing
import os
import queue
import time
from types import ModuleType
from typing import Any

from evals.alphaevolve.campaign import (
    CampaignEvaluator,
    campaign_evaluator,
    write_campaign_artifacts,
)
from evals.alphaevolve.ensemble import model_mixture
from evals.alphaevolve.registry import (
    EXPERIMENT_MODULES,
    WAVES,
    experiment_fingerprint,
    experiments_for_wave,
    levers_for_experiment,
    registry_document,
)

EXPERIMENTS = EXPERIMENT_MODULES
_SCORECARD_TIMEOUT_SECONDS = 120.0


def load_experiment(name: str) -> ModuleType:
    try:
        return importlib.import_module(EXPERIMENTS[name])
    except KeyError as exc:
        raise SystemExit(
            f"unknown experiment {name!r}; choose: {', '.join(EXPERIMENTS)}"
        ) from exc


def _gate_results(module: ModuleType) -> dict[str, Any]:
    code = getattr(module, "INTEGRATED_PROGRAM_CODE", module.INITIAL_PROGRAM_CODE)
    gates: dict[str, Any] = {"search": module.score_candidate(code)}
    for label in ("holdout", "adversarial"):
        scorer = getattr(module, f"score_{label}", None)
        if scorer is not None:
            gates[label] = scorer(code)
    return gates


def _experiment_row(name: str) -> tuple[str, Any]:
    module = load_experiment(name)
    gates = _gate_results(module)
    return name, {
        "metric": module.METRIC_NAME,
        "dataset_fingerprint": experiment_fingerprint(name),
        "levers": [lever.id for lever in levers_for_experiment(name)],
        "candidate": (
            "integrated"
            if hasattr(module, "INTEGRATED_PROGRAM_CODE")
            else "seed"
        ),
        "gates": {
            gate: {
                "score": result.get("score"),
                "passed": result.get("error") is None,
                "error": result.get("error"),
                "comparison": _comparison(result),
            }
            for gate, result in gates.items()
        },
        "promotion_ready": all(result.get("error") is None for result in gates.values()),
    }


def _scorecard_worker(name: str, output: multiprocessing.Queue[Any]) -> None:
    try:
        output.put(_experiment_row(name))
    except BaseException as exc:  # the fleet scorecard must stay bounded
        output.put((name, {"error": f"scorecard worker failed: {exc}"}))


def local_scorecard(
    experiments: tuple[str, ...] | None = None,
    *,
    timeout_seconds: float = _SCORECARD_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Evaluate integrated policies and every available promotion gate.

    Each experiment's full gate set runs in one isolated spawn child. All
    children are launched up front and then collected concurrently, so fleet
    wall time approaches the slowest single experiment while results are
    assembled back in registry order — byte-identical to serial evaluation.
    """
    selected = experiments or tuple(EXPERIMENTS)
    context = multiprocessing.get_context("spawn")
    jobs: list[tuple[str, multiprocessing.process.BaseProcess, multiprocessing.Queue[Any]]] = []
    for name in selected:
        output: multiprocessing.Queue[Any] = context.Queue()
        process = context.Process(target=_scorecard_worker, args=(name, output))
        process.start()
        jobs.append((name, process, output))

    rows: dict[str, Any] = {}
    deadline = time.monotonic() + float(timeout_seconds)
    for name, process, output in jobs:
        remaining = max(0.1, deadline - time.monotonic())
        try:
            returned_name, row = output.get(timeout=remaining)
        except queue.Empty:
            row = {
                "metric": None,
                "dataset_fingerprint": experiment_fingerprint(name),
                "levers": [lever.id for lever in levers_for_experiment(name)],
                "candidate": "seed",
                "gates": {},
                "promotion_ready": False,
                "error": "experiment scorecard timed out",
            }
            returned_name = name
        process.join(1.0)
        if process.is_alive():
            process.terminate()
            process.join(1.0)
        rows[returned_name] = row
    ordered = {name: rows[name] for name in selected}
    failures = [
        row.get("error") for row in ordered.values() if row.get("error")
    ]
    return {
        "schema": "ctx.alphaevolve-local-scorecard/v1",
        "experiments": ordered,
        "all_gates_pass": not failures
        and all(row["promotion_ready"] for row in ordered.values()),
    }


def _comparison(result: dict[str, Any]) -> dict[str, Any] | None:
    """Expose interpretable ratios when an evaluator defines a baseline."""
    totals = result.get("totals")
    baseline = result.get("baseline") or result.get("naive_direct_totals")
    if not isinstance(totals, dict) or not isinstance(baseline, dict):
        return None
    common = sorted(set(totals) & set(baseline))
    multipliers = {
        key: float(baseline[key]) / float(totals[key])
        for key in common
        if isinstance(totals[key], (int, float))
        and not isinstance(totals[key], bool)
        and float(totals[key]) > 0
        and isinstance(baseline[key], (int, float))
        and not isinstance(baseline[key], bool)
    }
    return {
        "totals": {key: totals[key] for key in common},
        "baseline": {key: baseline[key] for key in common},
        "baseline_over_candidate": multipliers,
        "reductions": result.get("reductions"),
        "cost_coverage": result.get("cost_coverage"),
    }


def readiness_report(experiments: tuple[str, ...] | None = None) -> dict[str, Any]:
    """Managed-search readiness; this is not a production-promotion claim."""
    scorecard = local_scorecard(experiments)
    rows = {}
    for name, result in scorecard["experiments"].items():
        specs = levers_for_experiment(name)
        rows[name] = {
            "ready": result["promotion_ready"],
            "risk": sorted({lever.risk for lever in specs}),
            "evidence": sorted({lever.evidence for lever in specs}),
            "rollout": sorted({lever.rollout for lever in specs}),
            "fingerprint": result["dataset_fingerprint"],
            "reason": (
                "all local search, holdout, and adversarial gates pass"
                if result["promotion_ready"]
                else "one or more local gates failed"
            ),
        }
    return {"schema": "ctx.alphaevolve-readiness/v1", "experiments": rows}


def promotion_report(experiments: tuple[str, ...] | None = None) -> dict[str, Any]:
    """State the strongest justified rollout stage for each current policy."""
    scorecard = local_scorecard(experiments)
    rows = {}
    for name, result in scorecard["experiments"].items():
        comparisons = [
            gate.get("comparison")
            for gate in result["gates"].values()
            if gate.get("comparison") is not None
        ]
        rows[name] = {
            "status": "managed_search_ready" if result["promotion_ready"] else "blocked",
            "production_promotion": False,
            "why_not_production": "no reviewed managed winner plus matched live canary receipt",
            "candidate": result["candidate"],
            "gates_pass": result["promotion_ready"],
            "comparisons": comparisons,
            "next_stage": sorted({lever.rollout for lever in levers_for_experiment(name)}),
        }
    return {"schema": "ctx.alphaevolve-promotion-report/v1", "experiments": rows}


def shadow_report(name: str) -> dict[str, Any]:
    """Counterfactual local decision report; performs no production mutation."""
    result = local_scorecard((name,))["experiments"][name]
    return {
        "schema": "ctx.alphaevolve-shadow/v1",
        "experiment": name,
        "mutated_production": False,
        "dataset_fingerprint": result["dataset_fingerprint"],
        "gates": result["gates"],
    }


def _env(name: str, fallback: str | None = None) -> str:
    return os.environ.get(name) or (os.environ.get(fallback) if fallback else "") or ""


def experiment_config(
    module: ModuleType,
    max_programs: int,
    concurrency: int,
    model_mix: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the managed experiment configuration (pure; unit-testable)."""
    return {
        "title": module.TITLE,
        "problem_description": module.PROBLEM_PATH.read_text(encoding="utf-8"),
        "program_language": "python",
        "run_settings": {"max_programs": max_programs, "concurrency": concurrency},
        "generation_settings": {
            "models": model_mix if model_mix is not None else model_mixture()
        },
    }


def _cloud_objects(module: ModuleType, max_programs: int, concurrency: int):
    project = _env("PROJECT_ID", "GOOGLE_CLOUD_PROJECT")
    engine = _env("GE_APP_ID")
    if not project or not engine:
        raise SystemExit("PROJECT_ID/GOOGLE_CLOUD_PROJECT and GE_APP_ID are required")
    try:
        from alpha_evolve.client import AlphaEvolveClient
        from alpha_evolve.controller import run_controller_loop
        from alpha_evolve.experiment import AlphaEvolveExperiment
    except ImportError as exc:
        raise SystemExit(
            "alpha_evolve is not installed; install Google's official client repo"
        ) from exc

    client = AlphaEvolveClient(
        project_id=project,
        location=_env("LOCATION") or "global",
        collection=_env("COLLECTION") or "default_collection",
        engine=engine,
        assistant=_env("ASSISTANT") or "default_assistant",
        base_url=_env("BASE_URL") or "discoveryengine.googleapis.com",
    )
    evaluator: Any = campaign_evaluator(module)
    experiment = AlphaEvolveExperiment(
        ae_client=client,
        evaluator_function=evaluator,
        max_programs_evaluated=max_programs - 1,
        parallel_evaluation=concurrency > 1,
    )
    return experiment, run_controller_loop, evaluator


def _run_loop(experiment: Any, controller: Any, concurrency: int) -> None:
    asyncio.run(
        controller(
            experiment,
            num_samplers=concurrency,
            num_evaluators=concurrency,
        )
    )


def _ranking(experiment: Any, module: ModuleType) -> list[dict[str, Any]]:
    response = experiment.list_programs(params={"pageSize": 100}) or {}
    rows: list[dict[str, Any]] = []
    for program in response.get("alphaEvolvePrograms", []):
        scores = program.get("evaluation", {}).get("scores", {}).get("scores", [])
        score = next(
            (
                item.get("score")
                for item in scores
                if item.get("metric") == module.METRIC_NAME
            ),
            None,
        )
        files = program.get("content", {}).get("files", [])
        content = files[0].get("content") if files else None
        rows.append(
            {
                "program": program.get("name", "").rsplit("/", 1)[-1],
                "score": score,
                "evolved": bool(program.get("parentPrograms")),
                "state": program.get("state"),
                "content": content if isinstance(content, str) else None,
            }
        )
    return sorted(
        rows,
        key=lambda row: float("-inf") if row["score"] is None else float(row["score"]),
        reverse=True,
    )


def _persist_campaign(
    module: ModuleType,
    name: str,
    evaluator: CampaignEvaluator,
    ranking: list[dict[str, Any]],
    config: dict[str, Any] | None,
) -> None:
    print(
        f"evaluator: {json.dumps(evaluator.stats)} "
        "(duplicate candidate source was replayed from memo)",
        flush=True,
    )
    artifacts = write_campaign_artifacts(
        module,
        name,
        ranking,
        config=config,
        fingerprint=experiment_fingerprint(name),
    )
    print(f"artifacts: {json.dumps(artifacts)}", flush=True)
    top = [
        {key: row[key] for key in ("program", "score", "evolved", "state")}
        for row in ranking[:5]
    ]
    print(json.dumps(top, indent=2))


def start(
    module: ModuleType,
    max_programs: int,
    concurrency: int,
    model_mix: list[dict[str, Any]] | None = None,
) -> None:
    experiment, controller, evaluator = _cloud_objects(module, max_programs, concurrency)
    config = experiment_config(module, max_programs, concurrency, model_mix)
    experiment.create_experiment(config)
    print(f"experiment: {experiment.experiment_name}", flush=True)
    seed = module.score_candidate(module.INITIAL_PROGRAM_CODE)
    if seed.get("error"):
        raise RuntimeError(f"seed failed local evaluator: {seed['error']}")
    initial_program = {
        "content": {
            "files": [{"path": "program.py", "content": module.INITIAL_PROGRAM_CODE}]
        },
        "evaluation": {
            "scores": {
                "scores": [{"metric": module.METRIC_NAME, "score": seed["score"]}]
            }
        },
    }
    experiment.create_initial_program(initial_program)
    experiment.start_experiment()
    _run_loop(experiment, controller, concurrency)
    _persist_campaign(
        module,
        _experiment_name_for(module),
        evaluator,
        _ranking(experiment, module),
        {key: value for key, value in config.items() if key != "problem_description"},
    )


def resume(
    module: ModuleType,
    experiment_name: str,
    max_programs: int,
    concurrency: int,
    model_mix: list[dict[str, Any]] | None = None,
) -> None:
    experiment, controller, evaluator = _cloud_objects(module, max_programs, concurrency)
    marker = "/alphaEvolveExperiments/"
    if marker not in experiment_name:
        raise SystemExit("--resume-experiment must be a full experiment resource name")
    experiment.session_name = experiment_name.split(marker, 1)[0]
    experiment.experiment_name = experiment_name
    ranking = _ranking(experiment, module)
    already = sum(1 for row in ranking if row["evolved"] and row["state"] == "COMPLETED")
    # Each resumed session spends its own fresh budget: --max-programs bounds
    # the additional candidates evaluated now, not lifetime history. Prior
    # completions stay visible in the ranking but must not consume this
    # round's allowance, or every follow-up round becomes a silent no-op.
    experiment.stats["num_programs_evaluated"] = 0
    logging.getLogger(__name__).info(
        "Resuming %s with %d evolved candidate(s) already evaluated; "
        "budgeting %d additional program(s)",
        experiment_name,
        already,
        max_programs - 1,
    )
    _run_loop(experiment, controller, concurrency)
    _persist_campaign(
        module,
        _experiment_name_for(module),
        evaluator,
        _ranking(experiment, module),
        {"resumed_experiment": experiment_name},
    )


def _experiment_name_for(module: ModuleType) -> str:
    for name, path in EXPERIMENTS.items():
        if path == getattr(module, "__name__", ""):
            return name
    return module.METRIC_NAME


def inspect_program(
    module: ModuleType,
    experiment_name: str,
    program_id: str,
) -> None:
    """Fetch one managed candidate and score it locally, including holdouts."""
    experiment, _controller, _evaluator = _cloud_objects(module, max_programs=2, concurrency=1)
    marker = "/alphaEvolveExperiments/"
    if marker not in experiment_name:
        raise SystemExit("--inspect-experiment must be a full experiment resource name")
    experiment.session_name = experiment_name.split(marker, 1)[0]
    experiment.experiment_name = experiment_name
    response = experiment.list_programs(params={"pageSize": 100}) or {}
    suffix = "/" + program_id.rsplit("/", 1)[-1]
    program = next(
        (
            item
            for item in response.get("alphaEvolvePrograms", [])
            if str(item.get("name", "")).endswith(suffix)
        ),
        None,
    )
    if program is None:
        raise SystemExit(f"program {program_id!r} not found in experiment")
    files = program.get("content", {}).get("files", [])
    if not files or not isinstance(files[0].get("content"), str):
        raise SystemExit("program has no inspectable source content")
    code = files[0]["content"]
    result: dict[str, Any] = {
        "experiment": experiment_name,
        "program": program.get("name"),
        "search": module.score_candidate(code),
    }
    for label in ("holdout", "adversarial"):
        scorer = getattr(module, f"score_{label}", None)
        if scorer is not None:
            result[label] = scorer(code)
    print(json.dumps(result, indent=2))


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser()
    parser.add_argument("experiment", nargs="?", choices=tuple(EXPERIMENTS))
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--list-levers", action="store_true")
    parser.add_argument("--wave", choices=WAVES)
    parser.add_argument("--all-local", action="store_true")
    parser.add_argument("--ready-for-managed", action="store_true")
    parser.add_argument("--promotion-report", action="store_true")
    parser.add_argument("--shadow", action="store_true")
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--resume-experiment")
    parser.add_argument("--inspect-experiment")
    parser.add_argument("--inspect-program")
    parser.add_argument("--confirm-spend", action="store_true")
    parser.add_argument("--max-programs", type=int, default=12)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument(
        "--model-mix",
        help=(
            "generation model mixture, e.g. "
            "'gemini-3.5-flash:0.7,gemini-3.1-pro-preview:0.3' "
            "(or MODEL_MIX/MODEL_1/MODEL_2 env)"
        ),
    )
    args = parser.parse_args()
    if args.list:
        print("\n".join(EXPERIMENTS))
        return
    if args.list_levers:
        print(json.dumps(registry_document(), indent=2))
        return
    selected = experiments_for_wave(args.wave)
    if args.all_local:
        print(json.dumps(local_scorecard(selected), indent=2))
        return
    if args.ready_for_managed:
        print(json.dumps(readiness_report(selected), indent=2))
        return
    if args.promotion_report:
        print(json.dumps(promotion_report(selected), indent=2))
        return
    if not args.experiment:
        parser.error("an experiment name is required")
    module = load_experiment(args.experiment)
    if args.shadow:
        print(json.dumps(shadow_report(args.experiment), indent=2))
        return
    if args.local or (not args.run and not args.resume_experiment):
        if args.inspect_experiment or args.inspect_program:
            if not args.inspect_experiment or not args.inspect_program:
                parser.error("inspection requires --inspect-experiment and --inspect-program")
            inspect_program(module, args.inspect_experiment, args.inspect_program)
            return
        print(json.dumps(module.score_candidate(module.INITIAL_PROGRAM_CODE), indent=2))
        return
    if not args.confirm_spend:
        raise SystemExit("cloud execution requires --confirm-spend")
    if not 2 <= args.max_programs <= 500:
        raise SystemExit("--max-programs must be between 2 and 500")
    if not 1 <= args.concurrency <= 8:
        raise SystemExit("--concurrency must be between 1 and 8")
    try:
        mix = model_mixture(args.model_mix)
    except ValueError as exc:
        raise SystemExit(f"invalid model mixture: {exc}") from exc
    if len(mix) > 1:
        print(
            "model mixture: "
            + ", ".join(f"{entry['name']}:{entry['weight']:.2f}" for entry in mix),
            flush=True,
        )
    if args.resume_experiment:
        resume(module, args.resume_experiment, args.max_programs, args.concurrency, mix)
    else:
        start(module, args.max_programs, args.concurrency, mix)


if __name__ == "__main__":
    main()
