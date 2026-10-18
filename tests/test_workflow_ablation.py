"""Benchmark controls preserve unknown costs and include failed trial spend."""
import importlib.util
from pathlib import Path
import zipfile
import subprocess

import pytest

spec = importlib.util.spec_from_file_location("workflow_ablation", Path(__file__).parents[1] / "evals/workflow_ablation.py")
ablation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ablation)


def test_profiles_change_one_mechanism_at_a_time():
    profiles = ablation.PROFILES
    assert profiles["rich"][2:] == profiles["compact"][2:]
    assert profiles["compact"][:-2] == profiles["navigation"][:-1]
    assert profiles["navigation"][:-1] == profiles["composed"]


def test_corpus_pin_rejects_dirty_content_at_the_same_commit(git_workspace):
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=git_workspace, text=True).strip()
    tasks = [{"dir": str(git_workspace), "corpus_commit": head}]
    ablation.verify_corpus(tasks)
    (git_workspace / "hello.py").write_text("changed at the same commit\n")
    with pytest.raises(ValueError, match="contents"):
        ablation.verify_corpus(tasks)


def test_release_lock_requires_pinned_tasks_wheel_and_matched_repeats(tmp_path):
    wheel = tmp_path / "release.whl"
    with zipfile.ZipFile(wheel, "w") as z:
        z.writestr("ctx/sdk.py", "fixture")
        z.writestr("ctx/data/ctx_agy.py", "fixture")
    kwargs = dict(source_sha="a"*40, sdk_version="1.2.3", model="pinned-model",
                  tasks=[{"id": "one", "base_commit": "b"*40, "corpus_commit": "c"*40}])
    lock = ablation.freeze(wheel, **kwargs)
    assert lock["wheel_sha256"] and lock["repeats"] == 3 and not lock["context_reuse"]
    with pytest.raises(ValueError, match="three"):
        ablation.freeze(wheel, repeats=1, **kwargs)
    with pytest.raises(ValueError, match="semantics"):
        ablation.freeze(wheel, rates={"uncached_input": 1, "cached_input": .1, "output": 2}, **kwargs)


def test_reporting_includes_failure_costs_and_does_not_turn_missing_usage_into_zero():
    lock = {"tasks": [{"id": "one"}], "profiles": {"rich": []}, "repeats": 3}
    rows = [{"task_id": "one", "arm": "rich", "repeat": r, "resolved": r == 1,
             "estimated_model_usd": cost} for r, cost in ((1, 1), (2, 2), (3, 3))]
    report = ablation.summarize(lock, rows)
    assert report["complete"]
    arm = report["arms"]["rich"]
    assert arm["estimated_usd_per_resolved_attempt"] == 6 and arm["failed_attempts"] == 2
    assert arm["model_requests"] is None
    rows[-1]["estimated_model_usd"] = None
    arm = ablation.summarize(lock, rows)["arms"]["rich"]
    assert arm["estimated_model_usd"] is None and arm["unknown_cost_attempts"] == 1
    assert arm["known_estimated_subtotal_usd"] == 3
    assert not ablation.summarize(lock, rows[:-1])["complete"]
    with pytest.raises(ValueError, match="duplicate"):
        ablation.summarize(lock, [*rows, rows[0]])


def test_estimates_require_all_input_cache_candidate_and_thinking_counts():
    usage = {"derived": {"uncached_input": 1_000_000, "cached_input": 1_000_000,
                         "candidate_output": 500_000, "thinking_output": 500_000}}
    rates = {"uncached_input": 1, "cached_input": .1, "output": 2}
    assert ablation.estimated_cost(usage, rates) == 3.1
    usage["derived"]["thinking_output"] = None
    assert ablation.estimated_cost(usage, rates) is None
