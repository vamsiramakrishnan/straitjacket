"""Preflight or explicitly run the straitjacket AlphaEvolve experiment."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
import subprocess
from pathlib import Path

from .evaluate import (
    INITIAL_PROGRAM_CODE,
    METRIC_NAME,
    alphaevolve_evaluation_function,
    score_candidate,
)

HERE = Path(__file__).resolve().parent


def _env(name: str, fallback: str | None = None) -> str:
    value = os.environ.get(name) or (os.environ.get(fallback) if fallback else None)
    return value or ""


def preflight() -> bool:
    """Check local inputs without changing cloud state or printing tokens."""
    checks: list[tuple[str, bool, str]] = []
    project = _env("PROJECT_ID", "GOOGLE_CLOUD_PROJECT")
    checks.append(("PROJECT_ID", bool(project), project or "not set"))
    checks.append(("GE_APP_ID", bool(_env("GE_APP_ID")), "set" if _env("GE_APP_ID") else "not set"))
    checks.append(("gcloud", shutil.which("gcloud") is not None, shutil.which("gcloud") or "not found"))

    if project and shutil.which("gcloud"):
        proc = subprocess.run(
            [
                "gcloud",
                "services",
                "list",
                "--enabled",
                f"--project={project}",
                "--filter=name:(aiplatform.googleapis.com OR discoveryengine.googleapis.com)",
                "--format=value(config.name)",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        enabled = set(proc.stdout.splitlines())
        for service in ("aiplatform.googleapis.com", "discoveryengine.googleapis.com"):
            checks.append((service, service in enabled, "enabled" if service in enabled else "missing"))

    local = score_candidate(INITIAL_PROGRAM_CODE)
    checks.append(("seed evaluator", local["score"] > -1_000_000, f"score={local['score']:.3f}"))
    for name, ok, detail in checks:
        print(f"[{'ok' if ok else 'missing'}] {name}: {detail}")
    print("[manual] verify Gemini Enterprise license, system user, service-account roles, and ADC")
    return all(ok for _name, ok, _detail in checks)


def _client_and_experiment(max_programs: int, concurrency: int):
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
            "alpha_evolve is not installed; install Google's official client repo first"
        ) from exc

    client = AlphaEvolveClient(
        project_id=project,
        location=_env("LOCATION") or "global",
        collection=_env("COLLECTION") or "default_collection",
        engine=engine,
        assistant=_env("ASSISTANT") or "default_assistant",
        base_url=_env("BASE_URL") or "discoveryengine.googleapis.com",
    )
    experiment = AlphaEvolveExperiment(
        ae_client=client,
        evaluator_function=alphaevolve_evaluation_function,
        max_programs_evaluated=max_programs - 1,
        parallel_evaluation=concurrency > 1,
    )
    return client, experiment, run_controller_loop


def run_cloud(max_programs: int, concurrency: int) -> None:
    _client, experiment, run_controller_loop = _client_and_experiment(
        max_programs, concurrency
    )
    config = {
        "title": "straitjacket generic evidence selection",
        "problem_description": (HERE / "PROBLEM.md").read_text(encoding="utf-8"),
        "program_language": "python",
        "run_settings": {"max_programs": max_programs, "concurrency": concurrency},
        "generation_settings": {
            "models": [{"name": _env("MODEL") or "gemini-3.5-flash"}]
        },
    }
    experiment.create_experiment(config)
    print(f"experiment: {experiment.experiment_name}", flush=True)
    seed_score = score_candidate(INITIAL_PROGRAM_CODE)["score"]
    initial_program = {
        "content": {"files": [{"path": "program.py", "content": INITIAL_PROGRAM_CODE}]},
        "evaluation": {
            "scores": {"scores": [{"metric": METRIC_NAME, "score": seed_score}]}
        },
    }
    experiment.create_initial_program(initial_program)
    experiment.start_experiment()
    asyncio.run(
        run_controller_loop(
            experiment,
            num_samplers=concurrency,
            num_evaluators=concurrency,
        )
    )
    print(experiment.list_programs(params={"order_by": f"{METRIC_NAME} desc"}))


def resume_cloud(experiment_name: str, max_programs: int, concurrency: int) -> None:
    """Reconnect the local evaluator/controller to one managed experiment."""
    _client, experiment, run_controller_loop = _client_and_experiment(
        max_programs, concurrency
    )
    marker = "/alphaEvolveExperiments/"
    if marker not in experiment_name:
        raise SystemExit("--resume-experiment must be a full experiment resource name")
    experiment.session_name = experiment_name.split(marker, 1)[0]
    experiment.experiment_name = experiment_name

    listing = experiment.list_programs(params={"pageSize": 100}) or {}
    programs = listing.get("alphaEvolvePrograms", [])
    # The completed seed is not part of max_programs_evaluated; completed
    # descendants are. Parent-less is the stable seed discriminator.
    already_evaluated = sum(
        1
        for program in programs
        if program.get("state") == "COMPLETED" and program.get("parentPrograms")
    )
    experiment.stats["num_programs_evaluated"] = already_evaluated
    logging.getLogger(__name__).info(
        "Resuming %s with %d evolved candidate(s) already evaluated",
        experiment_name,
        already_evaluated,
    )
    asyncio.run(
        run_controller_loop(
            experiment,
            num_samplers=concurrency,
            num_evaluators=concurrency,
        )
    )
    print(experiment.list_programs(params={"order_by": f"{METRIC_NAME} desc"}))


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="store_true", help="start a billed cloud experiment")
    parser.add_argument(
        "--resume-experiment",
        help="reconnect to a full AlphaEvolve experiment resource name",
    )
    parser.add_argument("--confirm-spend", action="store_true")
    parser.add_argument("--max-programs", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=2)
    args = parser.parse_args()
    if not args.run and not args.resume_experiment:
        raise SystemExit(0 if preflight() else 2)
    if not args.confirm_spend:
        raise SystemExit("cloud execution requires --confirm-spend")
    if not 2 <= args.max_programs <= 100:
        raise SystemExit("--max-programs must be between 2 and 100")
    if not 1 <= args.concurrency <= 8:
        raise SystemExit("--concurrency must be between 1 and 8")
    if args.resume_experiment:
        resume_cloud(args.resume_experiment, args.max_programs, args.concurrency)
    else:
        run_cloud(args.max_programs, args.concurrency)


if __name__ == "__main__":
    main()
