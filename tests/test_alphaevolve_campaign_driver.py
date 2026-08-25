from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from evals.alphaevolve.campaign_driver import (
    DEFAULT_MIX_PLAN,
    CampaignState,
    Driver,
    next_mix,
    parse_artifacts,
    parse_experiment_name,
    should_stop,
)


def test_parse_experiment_name_and_artifacts_from_portfolio_stdout():
    stdout = "\n".join(
        [
            "experiment: projects/x/locations/global/collections/default_collection/alphaEvolveExperiments/e1",
            "evaluator: {\"evaluations\": 3, \"memo_hits\": 1}",
            "artifacts: {\"summary\": \"/tmp/s.json\", \"winner\": \"/tmp/w.py\"}",
        ]
    )
    assert parse_experiment_name(stdout).endswith("alphaEvolveExperiments/e1")
    assert parse_artifacts(stdout)["summary"] == "/tmp/s.json"


def test_next_mix_rotates_exploration_to_exploitation():
    plan = DEFAULT_MIX_PLAN
    assert next_mix(plan, 0).startswith("gemini-3.5-flash:0.8")
    assert next_mix(plan, 1).startswith("gemini-3.5-flash:0.5")
    assert next_mix(plan, 2).startswith("gemini-3.5-flash:0.2")
    assert next_mix(plan, 3) == next_mix(plan, 0)


def _state(**kw) -> CampaignState:
    defaults = dict(experiment="wave-policy", round_index=3, best_score=120.0,
                    ranked_total=200, flat_programs=10)
    defaults.update(kw)
    return CampaignState(**defaults)


def test_should_stop_reasons():
    assert should_stop(_state(round_index=12), plateau_k=30, max_rounds=12,
                       max_total_programs=5000, wall_hours=24) == "reached --max-rounds=12"
    assert "max-total-programs" in should_stop(
        _state(ranked_total=5001), plateau_k=30, max_rounds=12,
        max_total_programs=5000, wall_hours=24)
    assert "plateau" in should_stop(_state(flat_programs=30), plateau_k=30,
                                    max_rounds=999, max_total_programs=9999,
                                    wall_hours=99)
    fresh = CampaignState(experiment="x", round_index=1, ranked_total=50,
                          flat_programs=0)
    assert should_stop(fresh, plateau_k=30, max_rounds=12,
                       max_total_programs=5000, wall_hours=24) is None


class FakeProc:
    def __init__(self, stdout: str, returncode: int = 0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


def _fake_runner(responses):
    calls: list[list[str]] = []

    def run(cmd, **kwargs):
        calls.append(list(cmd))
        return responses.pop(0)

    run.calls = calls
    return run


def _summary_stdout(name: str, summary_path: str | None) -> str:
    lines = [f"experiment: {name}"]
    if summary_path:
        lines.append(f"artifacts: {json.dumps({'summary': summary_path})}")
    return "\n".join(lines)


def _write_summary(path: Path, best: float | None, ranked: int) -> str:
    top = [{"program": "seed", "score": best, "evolved": False, "state": "COMPLETED"}]
    payload = {"programs_ranked": ranked, "top_programs": top}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def test_driver_adopts_existing_managed_experiment_on_relaunch(tmp_path):
    """Crash recovery: a relaunch must resume, not create a duplicate."""
    state = tmp_path / "state"
    state.mkdir(parents=True)
    s1 = tmp_path / "s1.json"
    (state / "progress.jsonl").write_text(
        json.dumps(
            {
                "schema": "ctx.alphaevolve-campaign-round/v1",
                "experiment": "wave-policy",
                "managed_experiment": "managed/exp-adopted",
                "round": 1,
                "mix": "m1",
                "best_score": 110.0,
                "programs_ranked_total": 60,
                "flat_programs": 0,
                "wall_seconds": 1.0,
                "status": "ok",
                "artifacts": {"summary": None},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    s2 = tmp_path / "s2.json"
    responses = [
        FakeProc(_summary_stdout("managed/exp-adopted", _write_summary(s2, 130.0, 90))),
        FakeProc(_summary_stdout("managed/exp-adopted", _write_summary(tmp_path / "s3.json", 130.0, 125))),
    ]
    runner = _fake_runner(responses)
    driver = Driver(
        round_programs=40, concurrency=2, mix_plan=("m1",),
        runner=runner, state_dir=state,
        plateau_k=30, max_rounds=5, max_total_programs=5000, wall_hours=24,
    )
    verdict = driver.run("wave-policy")
    # First command of the relaunch resumed the adopted experiment.
    assert "--resume-experiment" in runner.calls[0]
    assert "managed/exp-adopted" in runner.calls[0]
    assert verdict["managed_experiment"] == "managed/exp-adopted"


def test_driver_stops_on_plateau_and_records_progress(tmp_path):
    s1 = tmp_path / "s1.json"
    s2 = tmp_path / "s2.json"
    responses = [
        FakeProc(_summary_stdout("managed/exp-1", _write_summary(s1, 100.0, 101))),
        FakeProc(_summary_stdout("managed/exp-1", _write_summary(s2, 100.0, 140))),
    ]
    runner = _fake_runner(responses)
    driver = Driver(
        round_programs=40,
        concurrency=2,
        mix_plan=("m1",),
        runner=runner,
        state_dir=tmp_path / "state",
        plateau_k=30,
        max_rounds=12,
        max_total_programs=5000,
        wall_hours=24,
    )
    verdict = driver.run("wave-policy")

    assert verdict["status"] == "completed"
    assert verdict["stop_reason"].startswith("plateau")
    assert verdict["rounds"] == 2
    assert runner.calls[0][-1] == "--run"
    assert "--resume-experiment" in runner.calls[1]
    progress = (tmp_path / "state" / "progress.jsonl").read_text().splitlines()
    rows = [json.loads(line) for line in progress]
    assert [row["status"] for row in rows] == ["ok", "ok"]
    assert rows[0]["best_score"] == 100.0 and rows[0]["flat_programs"] == 0
    # Round 2 evaluated ~39 new candidates with no improvement -> >= 30 flat.
    assert rows[1]["flat_programs"] >= 30


def test_driver_first_round_failure_returns_typed_error(tmp_path):
    responses = [
        FakeProc("boom\nengine rejected creation", returncode=2),
    ]
    runner = _fake_runner(responses)
    driver = Driver(
        round_programs=10, concurrency=1, mix_plan=("m1",),
        runner=runner, state_dir=tmp_path / "state",
    )
    verdict = driver.run("surface-policy")
    assert verdict["status"] == "failed"
    assert "engine rejected creation" in verdict["error"]
    rows = (tmp_path / "state" / "progress.jsonl").read_text().splitlines()
    assert json.loads(rows[0])["status"] == "failed"


def test_driver_respects_max_rounds_guard(tmp_path):
    def endless_run(cmd, **kwargs):
        return FakeProc(
            _summary_stdout("managed/exp-9",
                            _write_summary(tmp_path / f"{len(runner_calls)}.json",
                                           90.0 + len(runner_calls), 100 * (len(runner_calls) + 1)))
        )

    runner_calls = [0]

    def counting_run(cmd, **kwargs):
        runner_calls[0] += 1
        return endless_run(cmd, **kwargs)

    driver = Driver(
        round_programs=100, concurrency=1, mix_plan=("m1",),
        runner=counting_run, state_dir=tmp_path / "state",
        max_rounds=4, plateau_k=30, max_total_programs=5000, wall_hours=24,
    )
    verdict = driver.run("turn-policy")
    assert verdict["stop_reason"] == "reached --max-rounds=4"
    assert runner_calls[0] == 4
