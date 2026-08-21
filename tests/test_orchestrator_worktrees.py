from __future__ import annotations

import json
import subprocess
from dataclasses import replace

import pytest

from conftest import make_ws
from ctx import hosts
from ctx.config import OrchestratePolicy
from ctx.orchestrator import RouteError, build_route_plan, run_route
from ctx.worktree_isolation import (
    IsolatedWorktree,
    apply_patches,
    clean_git_root,
    preflight_patch,
)


def _hosts(*installed):
    def which(binary):
        return f"/usr/bin/{binary}" if binary in installed else None

    return [item for item in hosts.detect_all(which=which) if item.installed and item.harnessable]


def _commit(root, *paths):
    subprocess.run(["git", "add", *paths], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=root, check=True)


def _parallel_mutation_plan(ws, cfg):
    raw = {
        "nodes": [
            {
                "id": "a", "goal": "edit a.txt", "role": "implement",
                "min_tier": "economy", "deps": [], "targets": ["a.txt"],
            },
            {
                "id": "b", "goal": "edit b.txt", "role": "implement",
                "min_tier": "economy", "deps": [], "targets": ["b.txt"],
            },
            {
                "id": "verify", "goal": "verify both", "role": "verify",
                "min_tier": "economy", "deps": ["a", "b"],
            },
        ]
    }
    return build_route_plan("update the two fixtures", raw, _hosts("claude", "codex"), cfg)


def test_disjoint_mutations_run_in_worktrees_and_apply_as_one_wave(
    state_home, git_workspace
):
    (git_workspace / "a.txt").write_text("old a\n", encoding="utf-8")
    (git_workspace / "b.txt").write_text("old b\n", encoding="utf-8")
    _commit(git_workspace, "a.txt", "b.txt")
    ws = make_ws(git_workspace)
    cfg = replace(ws.config.orchestrate, isolated_worktrees=True)
    plan = _parallel_mutation_plan(ws, cfg)
    roots = {}

    def launch(host, root, prompt, exe, *, timeout, model=""):
        if "node 'a'" in prompt:
            roots["a"] = root
            (root / "a.txt").write_text("new a\n", encoding="utf-8")
        elif "node 'b'" in prompt:
            roots["b"] = root
            (root / "b.txt").write_text("new b\n", encoding="utf-8")
        return 0, "worker completed", ""

    result = run_route(ws, plan, cfg, launch=launch)

    assert (git_workspace / "a.txt").read_text() == "new a\n"
    assert (git_workspace / "b.txt").read_text() == "new b\n"
    assert roots["a"] != git_workspace and roots["b"] != git_workspace
    assert roots["a"] != roots["b"]
    assert result.wave_policies[0].endswith("/parallel_worktrees")
    mutations = {outcome.node_id: outcome for outcome in result.outcomes[:2]}
    assert all(outcome.status == "ok" for outcome in mutations.values())
    assert all(outcome.isolation == "git_worktree" for outcome in mutations.values())
    assert all(outcome.merge_status == "applied" for outcome in mutations.values())
    assert mutations["a"].changed_paths == ("a.txt",)
    assert mutations["b"].changed_paths == ("b.txt",)
    listed = subprocess.run(
        ["git", "worktree", "list", "--porcelain"], cwd=git_workspace,
        text=True, capture_output=True, check=True,
    ).stdout
    assert listed.count("worktree ") == 1


def test_overlap_or_missing_targets_preserves_serial_shared_default(state_home, git_workspace):
    ws = make_ws(git_workspace)
    cfg = replace(ws.config.orchestrate, isolated_worktrees=True)
    raw = {
        "nodes": [
            {"id": "a", "goal": "a", "role": "implement", "min_tier": "economy",
             "deps": [], "targets": ["src"]},
            {"id": "b", "goal": "b", "role": "implement", "min_tier": "economy",
             "deps": [], "targets": ["src/x.py"]},
            {"id": "v", "goal": "v", "role": "verify", "min_tier": "economy",
             "deps": ["a", "b"]},
        ]
    }
    plan = build_route_plan("change", raw, _hosts("claude"), cfg)
    seen = []

    def launch(host, root, prompt, exe, *, timeout, model=""):
        seen.append(root)
        return 0, "ok", ""

    result = run_route(ws, plan, cfg, launch=launch)
    assert result.wave_policies[0].endswith("/serial_workspace")
    assert seen[0] == git_workspace and seen[1] == git_workspace


def test_dirty_parent_falls_back_to_serial_without_losing_user_changes(
    state_home, git_workspace
):
    user_file = git_workspace / "user-notes.txt"
    user_file.write_text("do not lose me\n", encoding="utf-8")
    ws = make_ws(git_workspace)
    cfg = replace(ws.config.orchestrate, isolated_worktrees=True)
    plan = _parallel_mutation_plan(ws, cfg)
    roots = []

    def launch(host, root, prompt, exe, *, timeout, model=""):
        roots.append(root)
        return 0, "ok", ""

    result = run_route(ws, plan, cfg, launch=launch)
    assert result.wave_policies[0].endswith("/serial_workspace")
    assert all(root == git_workspace for root in roots)
    assert user_file.read_text() == "do not lose me\n"


def test_untracked_worker_file_is_captured_and_applied(git_workspace):
    assert clean_git_root(git_workspace)
    with IsolatedWorktree(git_workspace, "new", ("generated.txt",)) as checkout:
        (checkout.path / "generated.txt").write_text("generated\n", encoding="utf-8")
        patch = checkout.capture()
    assert patch.changed_paths == ("generated.txt",)
    ok, detail = preflight_patch(git_workspace, patch)
    assert (ok, detail) == (True, "")
    ok, detail = apply_patches(git_workspace, [patch])
    assert (ok, detail) == (True, "")
    assert (git_workspace / "generated.txt").read_text() == "generated\n"


def test_out_of_scope_change_fails_and_worktree_is_cleaned(state_home, git_workspace):
    (git_workspace / "a.txt").write_text("a\n", encoding="utf-8")
    (git_workspace / "b.txt").write_text("b\n", encoding="utf-8")
    _commit(git_workspace, "a.txt", "b.txt")
    ws = make_ws(git_workspace)
    cfg = replace(ws.config.orchestrate, isolated_worktrees=True)
    plan = _parallel_mutation_plan(ws, cfg)

    def launch(host, root, prompt, exe, *, timeout, model=""):
        if "node 'a'" in prompt:
            (root / "b.txt").write_text("scope violation\n", encoding="utf-8")
        return 0, "done", ""

    result = run_route(ws, plan, cfg, launch=launch)
    by_id = {outcome.node_id: outcome for outcome in result.outcomes}
    assert by_id["a"].status == "failed"
    assert "outside declared targets" in by_id["a"].detail
    assert (git_workspace / "b.txt").read_text() == "b\n"
    listed = subprocess.run(
        ["git", "worktree", "list", "--porcelain"], cwd=git_workspace,
        text=True, capture_output=True, check=True,
    ).stdout
    assert listed.count("worktree ") == 1


def test_patch_preflight_detects_parent_conflict_without_partial_apply(git_workspace):
    (git_workspace / "target.txt").write_text("base\n", encoding="utf-8")
    _commit(git_workspace, "target.txt")
    with IsolatedWorktree(git_workspace, "n", ("target.txt",)) as checkout:
        (checkout.path / "target.txt").write_text("worker\n", encoding="utf-8")
        patch = checkout.capture()
    (git_workspace / "target.txt").write_text("parent\n", encoding="utf-8")
    ok, detail = preflight_patch(git_workspace, patch)
    assert ok is False and detail
    assert (git_workspace / "target.txt").read_text() == "parent\n"


def test_wave_preflight_failure_applies_none_of_its_patches(
    state_home, git_workspace, monkeypatch
):
    (git_workspace / "a.txt").write_text("old a\n", encoding="utf-8")
    (git_workspace / "b.txt").write_text("old b\n", encoding="utf-8")
    _commit(git_workspace, "a.txt", "b.txt")
    ws = make_ws(git_workspace)
    cfg = replace(ws.config.orchestrate, isolated_worktrees=True)
    plan = _parallel_mutation_plan(ws, cfg)

    def launch(host, root, prompt, exe, *, timeout, model=""):
        if "node 'a'" in prompt:
            (root / "a.txt").write_text("new a\n", encoding="utf-8")
        elif "node 'b'" in prompt:
            (root / "b.txt").write_text("new b\n", encoding="utf-8")
        return 0, "ok", ""

    real_preflight = preflight_patch

    def fail_b(root, patch):
        if patch.changed_paths == ("b.txt",):
            return False, "simulated parent conflict"
        return real_preflight(root, patch)

    monkeypatch.setattr("ctx.orchestrator.preflight_patch", fail_b)
    result = run_route(ws, plan, cfg, launch=launch)
    by_id = {outcome.node_id: outcome for outcome in result.outcomes}
    assert (git_workspace / "a.txt").read_text() == "old a\n"
    assert (git_workspace / "b.txt").read_text() == "old b\n"
    assert by_id["a"].merge_status == "aborted"
    assert by_id["b"].merge_status == "conflict"
    assert by_id["a"].status == by_id["b"].status == "failed"


def test_failed_attempt_is_reset_before_escalation_and_worktree_is_removed(
    state_home, git_workspace
):
    (git_workspace / "a.txt").write_text("old a\n", encoding="utf-8")
    (git_workspace / "b.txt").write_text("old b\n", encoding="utf-8")
    _commit(git_workspace, "a.txt", "b.txt")
    ws = make_ws(git_workspace)
    cfg = replace(ws.config.orchestrate, isolated_worktrees=True)
    plan = _parallel_mutation_plan(ws, cfg)
    attempts = {"a": 0, "b": 0}

    def launch(host, root, prompt, exe, *, timeout, model=""):
        node = "a" if "node 'a'" in prompt else ("b" if "node 'b'" in prompt else "v")
        if node in attempts:
            attempts[node] += 1
            if attempts[node] == 1:
                (root / f"{node}.txt").write_text("failed attempt\n", encoding="utf-8")
                return 124, "", "timed out"
            # reset() must have restored the baseline before the stronger retry.
            assert (root / f"{node}.txt").read_text() == f"old {node}\n"
            (root / f"{node}.txt").write_text(f"recovered {node}\n", encoding="utf-8")
        return 0, "ok", ""

    result = run_route(ws, plan, cfg, launch=launch)
    assert all(outcome.status == "ok" for outcome in result.outcomes)
    assert (git_workspace / "a.txt").read_text() == "recovered a\n"
    assert (git_workspace / "b.txt").read_text() == "recovered b\n"
    listed = subprocess.run(
        ["git", "worktree", "list", "--porcelain"], cwd=git_workspace,
        text=True, capture_output=True, check=True,
    ).stdout
    assert listed.count("worktree ") == 1


def test_strict_typed_yield_escalates_until_schema_matches(state_home, git_workspace):
    ws = make_ws(git_workspace)
    schema = {
        "type": "object",
        "required": ["summary", "evidence"],
        "properties": {
            "summary": {"type": "string"},
            "evidence": {"type": "array", "items": {"type": "string"}},
        },
        "additionalProperties": False,
    }
    raw = {"nodes": [{
        "id": "inspect", "goal": "inspect", "role": "inspect", "min_tier": "economy",
        "deps": [], "output_schema": schema, "strict_output_schema": True,
    }]}
    plan = build_route_plan("inspect", raw, _hosts("claude", "codex"), ws.config.orchestrate)
    calls = 0

    def launch(host, root, prompt, exe, *, timeout, model=""):
        nonlocal calls
        calls += 1
        assert "matching this schema" in prompt
        if calls == 1:
            return 0, json.dumps({"summary": "missing evidence"}), ""
        return 0, json.dumps({"summary": "ok", "evidence": ["repo:x.py:1"]}), ""

    result = run_route(ws, plan, ws.config.orchestrate, launch=launch)
    assert calls == 2
    assert result.outcomes[0].status == "ok"
    assert result.outcomes[0].output_schema_status == "valid"
    assert result.outcomes[0].escalated_to


def test_advisory_typed_yield_records_invalid_without_failing(state_home, git_workspace):
    ws = make_ws(git_workspace)
    raw = {"nodes": [{
        "id": "inspect", "goal": "inspect", "role": "inspect", "min_tier": "economy",
        "deps": [], "output_schema": {"type": "object", "required": ["answer"]},
    }]}
    plan = build_route_plan("inspect", raw, _hosts("claude"), ws.config.orchestrate)
    result = run_route(
        ws, plan, ws.config.orchestrate,
        launch=lambda *args, **kwargs: (0, json.dumps({"wrong": True}), ""),
    )
    assert result.outcomes[0].status == "ok"
    assert result.outcomes[0].output_schema_status == "invalid"


def test_strict_invalid_yield_is_failed_and_never_releases_dependent(
    state_home, git_workspace
):
    ws = make_ws(git_workspace)
    raw = {"nodes": [
        {
            "id": "typed", "goal": "typed", "role": "inspect", "min_tier": "frontier",
            "deps": [], "output_schema": {"type": "object", "required": ["answer"]},
            "strict_output_schema": True,
        },
        {"id": "consumer", "goal": "consume", "role": "inspect", "min_tier": "economy",
         "deps": ["typed"]},
    ]}
    plan = build_route_plan("inspect", raw, _hosts("claude"), ws.config.orchestrate)
    result = run_route(
        ws, plan, ws.config.orchestrate,
        launch=lambda *args, **kwargs: (0, json.dumps({"wrong": True}), ""),
    )
    by_id = {outcome.node_id: outcome for outcome in result.outcomes}
    assert by_id["typed"].status == "failed"
    assert by_id["typed"].checkpoint_ref  # failure evidence is retained
    assert by_id["typed"].output_schema_status == "invalid"
    assert by_id["consumer"].status == "skipped"


def test_new_orchestration_features_are_opt_in_by_default():
    cfg = OrchestratePolicy()
    assert cfg.isolated_worktrees is False
    assert cfg.strict_worker_yields is False


def test_route_rejects_unsupported_schema_keyword(git_workspace):
    ws = make_ws(git_workspace)
    raw = {"nodes": [{
        "id": "a", "goal": "a", "min_tier": "economy", "deps": [],
        "output_schema": {"type": "string", "pattern": "x"},
    }]}
    with pytest.raises(RouteError, match="unsupported keywords"):
        build_route_plan("inspect", raw, _hosts("claude"), ws.config.orchestrate)
