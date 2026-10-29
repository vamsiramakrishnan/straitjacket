"""Plateau-bounded managed-campaign driver.

Runs one registered experiment through repeated bounded managed rounds until
the search stops improving, instead of stopping at an arbitrary program cap:

- Round 1 creates the managed experiment; later rounds reconnect with
  ``--resume-experiment`` so no duplicate engine state is created.
- The generation-model mixture rotates exploration-first to exploitation-first
  across rounds unless a fixed ``--model-mix`` is supplied.
- A round is *flat* when the best primary-metric score does not improve and no
  new candidates were evaluated into the ranking. The campaign stops when
  cumulative flat programs reach ``--plateau-k`` (default 30) — search turns
  are bounded by demonstrated stagnation, never by a fixed cap.
- Absolute guards still apply: ``--max-rounds``, ``--max-total-programs``, and
  ``--wall-hours`` bound runaway loops even when marginal noise keeps
  "improving". Billed truth lives cloud-side; these local guards bound what
  this process can request. ``--confirm-spend`` is required once for the whole
  campaign and is forwarded to every round.

Per-round progress lands as one JSONL row under
``evals/alphaevolve/results/campaigns/<experiment>/progress.jsonl``.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from evals.alphaevolve.registry import EXPERIMENT_MODULES

RESULTS = Path(__file__).resolve().parent / "results"

DEFAULT_MIX_PLAN: tuple[str, ...] = (
    "gemini-3.5-flash:0.8,gemini-3.1-pro-preview:0.2",
    "gemini-3.5-flash:0.5,gemini-3.1-pro-preview:0.5",
    "gemini-3.5-flash:0.2,gemini-3.1-pro-preview:0.8",
)


@dataclass
class CampaignState:
    experiment: str
    experiment_name: str | None = None
    round_index: int = 0
    best_score: float | None = None
    ranked_total: int = 0
    flat_programs: int = 0
    started: float = field(default_factory=time.monotonic)

    def as_row(self, *, mix: str, status: str, artifacts: dict | None) -> dict:
        return {
            "schema": "ctx.alphaevolve-campaign-round/v1",
            "experiment": self.experiment,
            "managed_experiment": self.experiment_name,
            "round": self.round_index,
            "mix": mix,
            "best_score": self.best_score,
            "programs_ranked_total": self.ranked_total,
            "flat_programs": self.flat_programs,
            "wall_seconds": round(time.monotonic() - self.started, 1),
            "status": status,
            "artifacts": artifacts or {},
        }


def parse_experiment_name(stdout: str) -> str | None:
    for line in stdout.splitlines():
        if line.startswith("experiment: "):
            return line.split("experiment: ", 1)[1].strip()
    return None


def parse_artifacts(stdout: str) -> dict:
    for line in stdout.splitlines():
        if line.startswith("artifacts: "):
            try:
                return json.loads(line.split("artifacts: ", 1)[1])
            except json.JSONDecodeError:
                return {}
    return {}


def next_mix(plan: tuple[str, ...] | list[str], round_index: int) -> str:
    """Round 1 uses the exploration-heavy entry; cycles thereafter."""
    return list(plan)[round_index % len(plan)]


def _summary_stats(summary_path: str | None) -> tuple[float | None, int]:
    if not summary_path:
        return None, 0
    try:
        summary = json.loads(Path(summary_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, 0
    best = None
    for row in summary.get("top_programs") or []:
        score = row.get("score")
        if isinstance(score, (int, float)) and not isinstance(score, bool):
            best = float(score)
            break
    return best, int(summary.get("programs_ranked") or 0)


def should_stop(
    state: CampaignState,
    *,
    plateau_k: int,
    max_rounds: int,
    max_total_programs: int,
    wall_hours: float,
) -> str | None:
    """Return a stop reason, or None to keep going."""
    if state.round_index >= max_rounds:
        return f"reached --max-rounds={max_rounds}"
    if state.ranked_total >= max_total_programs:
        return f"reached --max-total-programs={max_total_programs}"
    elapsed_h = (time.monotonic() - state.started) / 3600.0
    if elapsed_h >= wall_hours:
        return f"reached --wall-hours={wall_hours}"
    if state.flat_programs >= plateau_k:
        return f"plateau: {state.flat_programs} evaluated programs without improvement"
    return None


class Driver:
    """Runs managed rounds via the portfolio CLI and tracks the plateau."""

    def __init__(
        self,
        *,
        round_programs: int,
        concurrency: int,
        mix_plan: tuple[str, ...],
        runner: Callable[..., subprocess.CompletedProcess],
        state_dir: Path | None = None,
        plateau_k: int = 30,
        max_rounds: int = 12,
        max_total_programs: int = 5000,
        wall_hours: float = 24.0,
        hard_session_cap: int | None = None,
    ) -> None:
        self.round_programs = round_programs
        self.concurrency = concurrency
        self.mix_plan = mix_plan
        self._runner = runner
        self.state_dir = state_dir
        self.plateau_k = plateau_k
        self.max_rounds = max_rounds
        self.max_total_programs = max_total_programs
        self.wall_hours = wall_hours
        # CLI ceiling; keeps the engine-side creation request inside the
        # portfolio's documented bound.
        self.hard_session_cap = hard_session_cap or 500

    def _portfolio_cmd(self, state: CampaignState, mix: str) -> list[str]:
        # The managed engine fixes its generation cap at creation, so round 1
        # requests the whole campaign budget; later rounds are drain/recovery
        # sessions that spend only the remaining allowance.
        if state.round_index <= 1:
            budget = self.max_total_programs
        else:
            budget = max(2, self.max_total_programs - state.ranked_total + 1)
        cmd = [
            sys.executable, "-m", "evals.alphaevolve.portfolio",
            state.experiment, "--confirm-spend",
            "--max-programs", str(min(budget, self.hard_session_cap)),
            "--concurrency", str(self.concurrency),
            "--model-mix", mix,
        ]
        if state.experiment_name:
            cmd += ["--resume-experiment", state.experiment_name]
        else:
            cmd += ["--run"]
        return cmd

    def _record(self, progress_path: Path, row: dict) -> None:
        with progress_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")

    def _round(self, state: CampaignState, mix: str) -> tuple[dict, str | None]:
        proc = self._runner(
            self._portfolio_cmd(state, mix),
            capture_output=True,
            text=True,
        )
        artifacts = parse_artifacts(proc.stdout)
        if state.experiment_name is None:
            state.experiment_name = parse_experiment_name(proc.stdout)
        if proc.returncode != 0 or state.experiment_name is None:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-5:]
            return {}, "round failed: " + " | ".join(tail)
        return artifacts, None

    def run(self, experiment: str) -> dict:
        base = self.state_dir or (RESULTS / "campaigns" / experiment)
        base.mkdir(parents=True, exist_ok=True)
        progress_path = base / "progress.jsonl"

        # Crash/detach recovery: adopt the managed experiment recorded by a
        # previous invocation instead of creating a duplicate.
        resumed_name: str | None = None
        if progress_path.exists():
            for line in progress_path.read_text().splitlines():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("managed_experiment") and row.get("status") == "ok":
                    resumed_name = row["managed_experiment"]

        state = CampaignState(experiment=experiment, experiment_name=resumed_name)
        if resumed_name:
            logging.getLogger(__name__).info(
                "Adopting managed experiment %s from existing progress",
                resumed_name,
            )

        stop_reason: str | None = None
        while True:
            reason = should_stop(
                state,
                plateau_k=self.plateau_k,
                max_rounds=self.max_rounds,
                max_total_programs=self.max_total_programs,
                wall_hours=self.wall_hours,
            )
            if reason is not None:
                stop_reason = reason
                break

            state.round_index += 1
            mix = next_mix(self.mix_plan, state.round_index - 1)
            artifacts, error = self._round(state, mix)
            if error is not None:
                self._record(
                    progress_path,
                    state.as_row(mix=mix, status="failed", artifacts=None),
                )
                return {
                    "status": "failed",
                    "experiment": experiment,
                    "managed_experiment": state.experiment_name,
                    "error": error,
                    "progress": str(progress_path),
                }

            best, ranked = _summary_stats(artifacts.get("summary"))
            evaluated_delta = max(0, ranked - state.ranked_total)
            state.ranked_total = ranked
            improved = best is not None and (
                state.best_score is None or best > state.best_score + 1e-9
            )
            if improved:
                state.best_score = best
                state.flat_programs = 0
            else:
                state.flat_programs += evaluated_delta
            self._record(
                progress_path, state.as_row(mix=mix, status="ok", artifacts=artifacts)
            )

        return {
            "status": "completed",
            "experiment": experiment,
            "managed_experiment": state.experiment_name,
            "stop_reason": stop_reason,
            "best_score": state.best_score,
            "programs_ranked_total": state.ranked_total,
            "flat_programs": state.flat_programs,
            "rounds": state.round_index,
            "progress": str(progress_path),
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("experiment", choices=tuple(EXPERIMENT_MODULES))
    parser.add_argument("--confirm-spend", action="store_true")
    parser.add_argument(
        "--round-programs", type=int, default=100,
        help="per-drain-session evaluation cap for recovery rounds",
    )
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--max-rounds", type=int, default=12)
    parser.add_argument("--plateau-k", type=int, default=30)
    parser.add_argument(
        "--max-total-programs", type=int, default=300,
        help="engine-side generation budget requested at creation (2..500)",
    )
    parser.add_argument("--wall-hours", type=float, default=6.0)
    parser.add_argument("--model-mix", help="fix one mixture instead of rotating")
    parser.add_argument(
        "--state-dir",
        help="override the progress directory (default results/campaigns/<exp>)",
    )
    args = parser.parse_args()
    if not args.confirm_spend:
        raise SystemExit("campaign execution requires --confirm-spend")

    plan: tuple[str, ...] = (
        (args.model_mix,) if args.model_mix else DEFAULT_MIX_PLAN
    )
    driver = Driver(
        round_programs=args.round_programs,
        concurrency=args.concurrency,
        mix_plan=plan,
        runner=subprocess.run,
        state_dir=Path(args.state_dir) if args.state_dir else None,
        plateau_k=args.plateau_k,
        max_rounds=args.max_rounds,
        max_total_programs=args.max_total_programs,
        wall_hours=args.wall_hours,
    )
    verdict = driver.run(args.experiment)
    print(json.dumps(verdict, indent=2))
    sys.exit(0 if verdict["status"] == "completed" else 1)


if __name__ == "__main__":
    main()
