"""Evidence-plan executor: end-to-end investigation, determinism, guards,
skip cascades, foreach bounds, wall budget, addressability, MCP tier.
"""

import json
import os
import shlex
import subprocess
import sys

import pytest

from conftest import make_store, make_ws


@pytest.fixture()
def seeded_repo(tmp_path, state_home):
    """A git workspace with a committed baseline, a regression edit in
    auth.py (a raise inside changed source), and gitignored caches so the
    worktree state is stable across test executions."""
    ws = tmp_path / "proj"
    ws.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True, env=env)
    (ws / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", encoding="utf-8")
    (ws / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    (ws / "auth.py").write_text(
        "def normalize_tenant(t):\n"
        "    return (t or '').strip().lower()\n"
        "\n"
        "def from_request(tenant_id):\n"
        "    return {'tenant': tenant_id}\n",
        encoding="utf-8",
    )
    (ws / "test_auth.py").write_text(
        "from auth import from_request\n"
        "\n"
        "def test_tenant_none():\n"
        "    assert from_request(None)['tenant'] == ''\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=ws, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=ws, check=True, env=env)
    # The regression: a raise inside the CHANGED file, so the failure's
    # deepest frame lands in changed source (the root-cause join's shape).
    (ws / "auth.py").write_text(
        "def normalize_tenant(t):\n"
        "    return (t or '').strip().lower()\n"
        "\n"
        "def from_request(tenant_id):\n"
        "    if tenant_id is None:\n"
        "        raise ValueError('missing tenant')\n"
        "    return {'tenant': tenant_id}\n",
        encoding="utf-8",
    )
    return ws


def _diagnosis_plan():
    return {
        "version": "ctx.plan/v1",
        "objective": {"kind": "diagnose", "question": "which changed symbols fail?"},
        "budget": {"wall_seconds": 120},
        "steps": [
            {"id": "changes", "op": "repo.changed"},
            {"id": "tests", "op": "test.run",
             # sys.executable, not a bare python3: the interpreter running
             # the suite is the one guaranteed to carry pytest.
             "args": {"command": f"{shlex.quote(sys.executable)} -m pytest -q"}},
            {"id": "culprits", "op": "evidence.join",
             "args": {"on": "failing_in_changed"}, "after": ["tests", "changes"]},
            {"id": "counter", "op": "evidence.join",
             "args": {"on": "untouched_failures"}, "after": ["tests"]},
            {"id": "probe", "op": "code.search", "args": {"pattern": "from_request"},
             "when": "culprits.count > 0"},
        ],
    }


def _execute(ws_dir, plan_doc, **kw):
    from ctx.plan_exec import execute_plan

    ws = make_ws(ws_dir)
    store = make_store(ws)
    return execute_plan(ws, store, plan_doc, **kw), ws, store


def test_end_to_end_diagnosis(seeded_repo):
    (out_code, ws, store) = _execute(seeded_repo, _diagnosis_plan())
    text, code = out_code
    assert code == 0
    # The root-cause join names the changed symbol, symbol-precise, with
    # plane attribution — the conclusion, not a log concatenation.
    assert "conclusion candidates (census): 1" in text
    assert "from_request" in text
    assert "planes dynamic+temporal+static" in text
    assert "ValueError" in text
    # Counterevidence section is REQUIRED (anti-anchoring) and coverage
    # names every node with engine disclosure and typed skip reasons.
    assert "counterevidence:" in text
    assert "coverage:" in text
    assert "engine git" in text and "engine run-capture" in text
    assert "outcome fail" in text
    # The guard fired (culprits.count=1 > 0), so probe ran.
    assert "probe · code.search · engine" in text


def test_every_node_blob_resolves_via_ctx_get(seeded_repo):
    (out_code, ws, store) = _execute(seeded_repo, _diagnosis_plan())
    text, code = out_code
    assert code == 0
    # The investigation manifest records one blob per executed node; each
    # resolves through the ordinary retrieval path (no new ref grammar).
    from ctx.retrieval import Selector, get

    row = store.db.execute(
        "SELECT id FROM objects WHERE kind='investigation' ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    manifest = store.get_manifest(row[0])
    assert manifest["schema"] == "ctx.investigation/v1"
    blobs = [n["blob"] for n in manifest["nodes"].values() if n.get("blob")]
    assert blobs, "executed nodes must persist result blobs"
    for blob in blobs:
        got = get(store, ws, f"blob:{blob}", Selector(json_pointer="/rows"))
        assert isinstance(got, str)
    # And the digest's next: lines carry working blob addresses.
    assert "ctx get " in text


def test_observe_only_plan_is_byte_deterministic(seeded_repo):
    plan = {
        "version": "ctx.plan/v1",
        "objective": {"kind": "survey", "question": "where is from_request used?"},
        "budget": {"wall_seconds": 60},
        "steps": [
            {"id": "changes", "op": "repo.changed"},
            {"id": "sites", "op": "code.search", "args": {"pattern": "from_request"}},
            {"id": "n", "op": "evidence.count", "input": "sites"},
        ],
    }
    (first, _, _) = _execute(seeded_repo, plan)
    (second, _, _) = _execute(seeded_repo, plan)
    assert first[0] == second[0]
    assert first[1] == second[1] == 0


def test_guard_not_met_skips_and_declares(seeded_repo):
    plan = {
        "version": "ctx.plan/v1",
        "objective": {"kind": "survey", "question": "guard test"},
        "budget": {"wall_seconds": 60},
        "steps": [
            {"id": "sites", "op": "code.search", "args": {"pattern": "no_such_token_xyz"}},
            {"id": "probe", "op": "code.search", "args": {"pattern": "def "},
             "when": "sites.count > 0"},
        ],
    }
    (out_code, _, _) = _execute(seeded_repo, plan)
    text, code = out_code
    assert code == 0
    assert "SKIPPED: guard_not_met (sites.count=0)" in text


def test_error_cascade_skips_dependents_and_declares(seeded_repo):
    plan = {
        "version": "ctx.plan/v1",
        "objective": {"kind": "survey", "question": "cascade test"},
        "budget": {"wall_seconds": 60},
        "steps": [
            # symbol_neighbors with an empty symbol is a validation-passing
            # arg (non-empty enforced) — use a join that raises instead:
            {"id": "boom", "op": "q.pipe", "args": {"query": "search ((("}},
            {"id": "downstream", "op": "evidence.count", "input": "boom"},
            {"id": "independent", "op": "code.search", "args": {"pattern": "def "}},
        ],
    }
    (out_code, _, _) = _execute(seeded_repo, plan)
    text, code = out_code
    assert code == 3  # a node errored; the digest still renders
    assert "boom · q.pipe · ERROR:" in text
    assert "downstream · evidence.count · SKIPPED: upstream_failed" in text
    assert "independent · code.search · engine" in text  # unaffected branch ran


def test_on_error_fail_halts_plan(seeded_repo):
    plan = {
        "version": "ctx.plan/v1",
        "objective": {"kind": "survey", "question": "halt test"},
        "budget": {"wall_seconds": 60},
        "steps": [
            {"id": "boom", "op": "q.pipe", "args": {"query": "search ((("},
             "on_error": "fail"},
            {"id": "later", "op": "code.search", "args": {"pattern": "def "}},
        ],
    }
    (out_code, _, _) = _execute(seeded_repo, plan)
    text, code = out_code
    assert code == 3
    assert "later · code.search · SKIPPED: plan_halted" in text


def test_foreach_is_capped_with_declared_omission(seeded_repo):
    # Three files match; cap 1 → 2 foreach values declared omitted.
    plan = {
        "version": "ctx.plan/v1",
        "objective": {"kind": "survey", "question": "foreach test"},
        "budget": {"wall_seconds": 60},
        "steps": [
            {"id": "files", "op": "code.search", "args": {"pattern": "def "}},
            {"id": "per", "op": "code.search", "args": {"pattern": "{item}"},
             "input": "files", "foreach": "file", "cap": 1},
        ],
    }
    (out_code, ws, store) = _execute(seeded_repo, plan)
    text, code = out_code
    assert code == 0
    assert "omitted" in text  # the fan-out overflow is declared, never silent


def test_wall_budget_exhaustion_declared(seeded_repo):
    clock_values = iter([0.0, 1000.0, 2000.0, 3000.0, 4000.0, 5000.0])

    def clock():
        return next(clock_values)

    plan = {
        "version": "ctx.plan/v1",
        "objective": {"kind": "survey", "question": "wall test"},
        "budget": {"wall_seconds": 10},
        "steps": [
            {"id": "a", "op": "code.search", "args": {"pattern": "def "}},
            {"id": "b", "op": "code.search", "args": {"pattern": "class "}},
        ],
    }
    (out_code, _, _) = _execute(seeded_repo, plan, clock=clock)
    text, code = out_code
    assert code == 0
    assert "SKIPPED: budget_wall_exhausted" in text


def test_validation_rejection_renders_typed_lines(seeded_repo):
    plan = {
        "version": "ctx.plan/v1",
        "objective": {"kind": "survey", "question": "reject me"},
        "budget": {"wall_seconds": 60},
        "steps": [{"id": "x", "op": "no.such.op"}],
    }
    (out_code, _, _) = _execute(seeded_repo, plan)
    text, code = out_code
    assert code == 2
    assert "REJECTED" in text and "unknown_op" in text


def test_mcp_tier_rejects_execute_class(seeded_repo):
    from ctx.mcp import _dispatch

    result = _dispatch(
        {
            "op": "investigate",
            "workspace": str(seeded_repo),
            "options": {"plan": _diagnosis_plan()},
        }
    )
    assert "execute_on_observe_tier" in result


def test_mcp_tier_runs_observe_only_plan(seeded_repo):
    from ctx.mcp import _dispatch

    plan = {
        "version": "ctx.plan/v1",
        "objective": {"kind": "survey", "question": "observe only"},
        "budget": {"wall_seconds": 60},
        "steps": [
            {"id": "changes", "op": "repo.changed"},
            {"id": "sites", "op": "code.search", "args": {"pattern": "from_request"}},
        ],
    }
    result = _dispatch(
        {"op": "investigate", "workspace": str(seeded_repo), "options": {"plan": plan}}
    )
    assert "profile=investigate/v1" in result
    assert "coverage:" in result


def test_investigation_contract_satisfied(seeded_repo):
    """The digest satisfies the committed contract at the selection seam:
    a PARTIAL marker in the text would mean a required class was missed."""
    (out_code, _, _) = _execute(seeded_repo, _diagnosis_plan())
    text, code = out_code
    assert code == 0
    assert "contract: PARTIAL" not in text


def test_semantic_op_skips_declared_when_semgrep_absent(seeded_repo, monkeypatch):
    from ctx import semgrep_engine

    semgrep_engine.binary.cache_clear()
    monkeypatch.setenv("PATH", "/nonexistent")
    plan = {
        "version": "ctx.plan/v1",
        "objective": {"kind": "survey", "question": "semgrep absent"},
        "budget": {"wall_seconds": 60},
        "steps": [{"id": "sem", "op": "semantic.search", "args": {"rules": "rules.yaml"}}],
    }
    try:
        (out_code, _, _) = _execute(seeded_repo, plan)
    finally:
        semgrep_engine.binary.cache_clear()
    text, code = out_code
    assert code == 0
    assert "sem · semantic.search · SKIPPED: engine_missing" in text


def test_cli_plan_run_and_investigate(seeded_repo, monkeypatch, capsys):
    from ctx.cli import main

    plan_path = seeded_repo / "plan.json"
    plan_path.write_text(json.dumps(_diagnosis_plan()), encoding="utf-8")
    rc = main(["--workspace", str(seeded_repo), "plan", "validate", "plan.json"])
    assert rc == 0
    assert "OK" in capsys.readouterr().out
    rc = main(["--workspace", str(seeded_repo), "plan", "price", "plan.json"])
    assert rc == 0
    assert "1 model round" in capsys.readouterr().out
    rc = main(["--workspace", str(seeded_repo), "plan", "run", "plan.json"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "profile=investigate/v1" in out
    rc = main(["--workspace", str(seeded_repo), "investigate", "plan.json"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "profile=investigate/v1" in out


def test_replan_budget_banner(seeded_repo, capsys):
    from ctx.cli import main

    plan_path = seeded_repo / "plan.json"
    obs = {
        "version": "ctx.plan/v1",
        "objective": {"kind": "survey", "question": "replan budget?"},
        "budget": {"wall_seconds": 60},
        "steps": [{"id": "sites", "op": "code.search", "args": {"pattern": "def "}}],
    }
    plan_path.write_text(json.dumps(obs), encoding="utf-8")
    for _ in range(3):
        rc = main(
            ["--workspace", str(seeded_repo), "investigate", "plan.json", "--replans", "1"]
        )
        assert rc == 0
        out = capsys.readouterr().out
    # Third epoch for the same objective exceeds the 1-replan allowance:
    # declared banner, never a block.
    assert "replan budget" in out


def test_plan_ops_census_lists_capabilities(seeded_repo, capsys):
    from ctx.cli import main

    rc = main(["--workspace", str(seeded_repo), "plan", "ops"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "test.run" in out and "execute" in out
    assert "ast.search" in out and "evidence.join" in out
