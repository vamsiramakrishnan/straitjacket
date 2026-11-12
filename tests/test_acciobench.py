from __future__ import annotations

import sys

# Prevent Python from writing host-specific source paths into evals/acciobench/__pycache__/*.pyc.
sys.dont_write_bytecode = True

from dataclasses import dataclass
import http.client
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import threading
import time
import types
import urllib.request
from typing import Any

import pytest

import evals.acciobench as acciobench
from evals.acciobench import cli as ab_cli
from evals.acciobench.browser import BrowserPool, BrowserSession
from evals.acciobench.config import (
    BenchConfig,
    RunMetrics,
    discover_all_tasks,
    load_cab_task,
    resolve_ctx_bin,
    select_tasks,
)
from evals.acciobench.host import ToolHost
from evals.acciobench.loop import run_trial
from evals.acciobench.orchestrator import run_suite
from evals.acciobench.ports import (
    CompiledPortMap,
    discover_task_ports,
    rewrite_ports_surgical,
)
from evals.acciobench.providers import (
    AdcTokenCache,
    GeminiProvider,
    VertexProvider,
    create_provider,
)
from evals.acciobench.report import load_run_results, render_summary_report
from evals.acciobench.sandbox import TaskSandbox, stage_mock_services_tree
from evals.acciobench.schemas import (
    SJ_EXECUTION_PROTOCOL,
    build_prompt,
    build_tools,
    canonical_tool_sig,
    format_workspace_inventory,
    is_mutating_tool_call,
)
from evals.acciobench.validate import _find_slot_pids, run_doctor, validate_task_referee
import evals.acciobench_run as acciobench_run


REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Helpers & Synthetic Dataset Builders
# ---------------------------------------------------------------------------


def _write_executable(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _create_synthetic_task(
    dataset_root: Path,
    *,
    category: str = "file",
    task_id: str = "file-margin-audit",
    mock_services: bool = False,
    service_port: int | None = None,
    requires_browser: bool = False,
    leaky_grader: bool = False,
) -> Path:
    """Create a self-contained CommerceAgentBench-compatible task directory."""
    case_dir = dataset_root / "datasets_domain_v1" / category / task_id
    (case_dir / "workspace").mkdir(parents=True, exist_ok=True)
    (case_dir / "grader").mkdir(parents=True, exist_ok=True)

    env_lines = [
        f"mock_services = {'true' if mock_services else 'false'}",
        f"requires_browser = {'true' if requires_browser else 'false'}",
    ]
    if service_port is not None:
        env_lines.append(f"host_published_port = {service_port}")

    (case_dir / "task.toml").write_text(
        "\n".join(
            [
                "[task]",
                f'id = "{task_id}"',
                f'name = "{task_id}"',
                'language = "en"',
                "",
                "[agent]",
                "timeout_sec = 120",
                "max_actions = 40",
                "",
                "[verifier]",
                "timeout_sec = 30",
                "",
                "[environment]",
                *env_lines,
                "",
            ]
        ),
        encoding="utf-8",
    )

    endpoint_note = (
        f"Query the supplier quote API at http://127.0.0.1:{service_port}/quote.\n"
        if service_port is not None
        else ""
    )
    (case_dir / "task.md").write_text(
        f"# Task: {task_id}\n\n"
        f"{endpoint_note}"
        "Inspect `/task/workspace/orders.json` and write `/task/outputs/summary.json` "
        'containing `{"total_usd": 420, "status": "verified"}`.\n',
        encoding="utf-8",
    )

    (case_dir / "workspace" / "orders.json").write_text(
        json.dumps(
            {
                "orders": [{"sku": "SKU-1", "usd": 120}, {"sku": "SKU-2", "usd": 300}],
                "endpoint": f"http://127.0.0.1:{service_port}/quote" if service_port else "local",
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    if mock_services and service_port is not None:
        server_py = case_dir / "services" / "quote_server.py"
        server_py.parent.mkdir(parents=True, exist_ok=True)
        server_py.write_text(
            "import http.server, json, os, subprocess, sys, time\n"
            f"DEFAULT_PORT = {service_port}\n"
            "port = int(os.environ.get('PORT', DEFAULT_PORT))\n"
            "# Spawn a child helper inside the same session/env to test full group cleanup.\n"
            "helper = subprocess.Popen([sys.executable, '-B', '-c', 'import time; time.sleep(300)'])\n"
            "class Handler(http.server.BaseHTTPRequestHandler):\n"
            "    def log_message(self, fmt, *args):\n"
            "        pass\n"
            "    def do_GET(self):\n"
            "        body = json.dumps({'ok': True, 'port': port, 'quote_usd': 420}).encode('utf-8')\n"
            "        self.send_response(200)\n"
            "        self.send_header('Content-Type', 'application/json')\n"
            "        self.send_header('Content-Length', str(len(body)))\n"
            "        self.end_headers()\n"
            "        self.wfile.write(body)\n"
            "http.server.ThreadingHTTPServer(('127.0.0.1', port), Handler).serve_forever()\n",
            encoding="utf-8",
        )
        _write_executable(
            case_dir / "services" / "start_services.sh",
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f"PORT=${{PORT:-{service_port}}}\n"
            "export PORT\n"
            f'nohup "{sys.executable}" -B "$(dirname "$0")/quote_server.py" >/dev/null 2>&1 &\n'
            "for _ in $(seq 1 50); do\n"
            '  if "' + sys.executable + '" -B -c "import socket; s=socket.create_connection((\'127.0.0.1\', int($PORT)), 0.2); s.close()" 2>/dev/null; then\n'
            "    exit 0\n"
            "  fi\n"
            "  sleep 0.05\n"
            "done\n"
            "exit 1\n",
        )

    if leaky_grader:
        grader_body = (
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            'echo \'{"passed": true, "score": 1.0, "checks_passed": 2, "checks_total": 2, "failed_checks": []}\' > "${REWARD_JSON:-reward.json}"\n'
        )
    else:
        grader_body = (
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            'OUT_DIR="${BENCH_OUTPUT_DIR:-/task/outputs}"\n'
            'REWARD_OUT="${REWARD_JSON:-reward.json}"\n'
            '"' + sys.executable + '" -B - "$OUT_DIR" "$REWARD_OUT" <<\'PY\'\n'
            "import json, pathlib, sys\n"
            "out_dir = pathlib.Path(sys.argv[1])\n"
            "reward_path = pathlib.Path(sys.argv[2])\n"
            "target = out_dir / 'summary.json'\n"
            "passed = False\n"
            "if target.is_file():\n"
            "    try:\n"
            "        data = json.loads(target.read_text(encoding='utf-8'))\n"
            "        passed = data.get('total_usd') == 420 and data.get('status') == 'verified'\n"
            "    except Exception:\n"
            "        passed = False\n"
            "payload = {\n"
            "    'passed': passed,\n"
            "    'score': 1.0 if passed else 0.0,\n"
            "    'checks_passed': 2 if passed else 0,\n"
            "    'checks_total': 2,\n"
            "    'failed_checks': [] if passed else ['summary_mismatch'],\n"
            "}\n"
            "reward_path.parent.mkdir(parents=True, exist_ok=True)\n"
            "reward_path.write_text(json.dumps(payload), encoding='utf-8')\n"
            "PY\n"
            'cat "$REWARD_OUT"\n'
        )
    _write_executable(case_dir / "grader" / "run.sh", grader_body)
    return case_dir


# ---------------------------------------------------------------------------
# 0. Public Hygiene & Package Exports
# ---------------------------------------------------------------------------


def test_public_hygiene_and_package_exports() -> None:
    expected_exports = {
        "BenchConfig",
        "BrowserPool",
        "BrowserSession",
        "CompiledPortMap",
        "ModelProvider",
        "RunMetrics",
        "SJ_EXECUTION_PROTOCOL",
        "TaskSandbox",
        "ToolHost",
        "build_html_report_payload",
        "build_prompt",
        "build_tools",
        "canonical_tool_sig",
        "create_provider",
        "discover_all_tasks",
        "discover_task_ports",
        "format_workspace_inventory",
        "is_mutating_tool_call",
        "load_cab_task",
        "load_run_results",
        "render_html_report",
        "render_summary_report",
        "resolve_ctx_bin",
        "rewrite_ports_surgical",
        "run_doctor",
        "run_suite",
        "run_trial",
        "select_tasks",
        "validate_task_referee",
    }
    assert expected_exports.issubset(set(acciobench.__all__))
    for sym in expected_exports:
        assert getattr(acciobench, sym) is not None
    assert callable(acciobench_run.main)

    # Construct forbidden tokens dynamically so this test file itself stays 100% clean.
    forbidden_patterns = [
        "vam" + "ramak",
        "vital-" + "octagon",
        "googlers" + r"\.com",
        "corp" + r"\.google",
        "google" + "3",
        "HIST" + "FILE",
    ]
    combined_re = re.compile("|".join(forbidden_patterns))

    targets = [
        *sorted((REPO_ROOT / "evals" / "acciobench").rglob("*")),
        REPO_ROOT / "evals" / "acciobench_run.py",
        REPO_ROOT / "src" / "ctx" / "deliverables.py",
        REPO_ROOT / "src" / "ctx" / "residence.py",
        Path(__file__).resolve(),
    ]
    offenders: list[str] = []
    for path in targets:
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        raw = path.read_bytes()
        text = raw.decode("utf-8", errors="ignore")
        m = combined_re.search(text)
        if m:
            offenders.append(f"{path.relative_to(REPO_ROOT)}: matched {m.group(0)!r}")
    assert offenders == []


# ---------------------------------------------------------------------------
# 1. Ports: Discovery, Caching & Single-Pass Surgical Rewriting
# ---------------------------------------------------------------------------


def test_compiled_port_map_no_chaining_and_exact_boundaries() -> None:
    empty = CompiledPortMap({})
    assert not empty
    assert empty.mapping == {}
    sample = "http://localhost:3000 and PORT=3000"
    assert empty.rewrite(sample) == sample

    # Adjacent ports must rewrite in a single pass without chaining 3000 -> 3001 -> 3002.
    cmap = CompiledPortMap({3000: 3001, 3001: 3002})
    assert bool(cmap) is True
    assert cmap.mapping == {3000: 3001, 3001: 3002}

    src = (
        "primary=http://localhost:3000/api "
        "secondary=http://127.0.0.1:3001/v1 "
        "bind=0.0.0.0:3000 "
        "five_digit=http://localhost:30005/keep "
        "env1=PORT=3000 env2=SERVICE_PORT=3001 "
        'json={"port": 3000} '
        "shell=${API_PORT:-3000} "
        "cli=--port 3000 --port=3001"
    )
    out = rewrite_ports_surgical(src, cmap, is_service_script=False)
    assert "primary=http://localhost:3001/api" in out
    assert "secondary=http://127.0.0.1:3002/v1" in out
    assert "bind=0.0.0.0:3001" in out
    assert "five_digit=http://localhost:30005/keep" in out
    assert "env1=PORT=3001" in out
    assert "env2=SERVICE_PORT=3002" in out
    assert 'json={"port": 3001}' in out
    assert "shell=${API_PORT:-3001}" in out
    assert "cli=--port 3001 --port=3002" in out


def test_compiled_port_map_service_script_patterns() -> None:
    cmap = CompiledPortMap({4100: 20400, 5200: 20401})
    script = (
        "start_one slack_api 4100\n"
        "const PORT = 5200;\n"
        "parser.add_argument('--port', type=int, default=4100)\n"
        "max_items = 5200\n"
    )
    non_svc = cmap.rewrite(script, is_service_script=False)
    assert "start_one slack_api 4100" in non_svc
    assert "default=4100" in non_svc

    svc = cmap.rewrite(script, is_service_script=True)
    assert "start_one slack_api 20400" in svc
    assert "const PORT = 20401;" in svc
    assert "default=20400" in svc
    assert "max_items = 5200" in svc

    script2 = "let x = (default = 4100);\n"
    svc2 = cmap.rewrite(script2, is_service_script=True)
    assert "default = 20400" in svc2


def test_discover_task_ports_and_lru_cache(tmp_path: Path) -> None:
    discover_task_ports.cache_clear()  # type: ignore[attr-defined]
    case_dir = tmp_path / "api-sample"
    (case_dir / "services").mkdir(parents=True)
    (case_dir / "grader").mkdir(parents=True)
    (case_dir / "task.md").write_text(
        "Connect to http://localhost:3100 and ignore privileged http://localhost:80.\n",
        encoding="utf-8",
    )
    (case_dir / "services" / "start_services.sh").write_text(
        "python3 server.py --port 3200 &\nPORT=3300\n",
        encoding="utf-8",
    )
    (case_dir / "grader" / "verify_task.py").write_text(
        "URL = 'http://127.0.0.1:3400/health'\n",
        encoding="utf-8",
    )

    @dataclass
    class DummyMock:
        port: int | None

    @dataclass
    class DummySpec:
        case_dir: Path
        host_published_port: int | None
        runtime_mocks: tuple[Any, ...]

    spec = DummySpec(
        case_dir=case_dir,
        host_published_port=3050,
        runtime_mocks=(DummyMock(port=3100), DummyMock(port=3150)),
    )
    ports = discover_task_ports(spec)
    assert ports == [3050, 3100, 3150, 3200, 3300, 3400]

    # Mutate task.md on disk without clearing cache: cached result must be returned without rescanning.
    (case_dir / "task.md").write_text("Connect to http://localhost:9999\n", encoding="utf-8")
    assert discover_task_ports(spec) == [3050, 3100, 3150, 3200, 3300, 3400]
    assert discover_task_ports.cache_info().hits >= 1  # type: ignore[attr-defined]

    discover_task_ports.cache_clear()  # type: ignore[attr-defined]
    refreshed = discover_task_ports(spec)
    assert 9999 in refreshed


# ---------------------------------------------------------------------------
# 2. Config, Metrics, ctx Resolution & Task Selection
# ---------------------------------------------------------------------------


def test_bench_config_and_run_metrics_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("VERTEX_PROJECT_ID", "test-cloud-proj")

    cfg = BenchConfig(
        dataset_root=tmp_path / "cab",
        workdir=tmp_path / "work",
        arms="raw, sj",  # type: ignore[arg-type]
    )
    assert cfg.arms == ("raw", "sj")
    assert cfg.api_key == "test-gemini-key"
    assert cfg.project_id == "test-cloud-proj"
    assert cfg.thinking_level is None
    assert cfg.max_output_tokens is None
    assert cfg.suite_dir == cfg.dataset_root / "datasets_domain_v1"
    assert cfg.runs_dir == cfg.workdir / "runs"
    assert cfg.ctx_state_home == cfg.workdir / "ctx_state"

    with pytest.raises(ValueError, match="Unsupported arm"):
        BenchConfig(dataset_root=tmp_path / "cab", workdir=tmp_path / "work", arms=("invalid",))

    rm = RunMetrics(
        task_id="cli-inventory-reconcile",
        task_rel="cli/cli-inventory-reconcile",
        category="cli",
        arm="sj",
        model_id="gemini-2.5-flash",
        steps=9,
        prompt_tokens=12_000,
        cached_tokens=8_000,
        thought_tokens=700,
        output_tokens=300,
        tool_calls={"bash": 4, "present_files": 1},
        ctx_retrievals={"get": 2},
        passed=True,
        capacity_score=1.0,
    )
    assert rm.gen_tokens == 1_000
    assert rm.total_tokens == 13_000
    as_dict = rm.to_dict()
    assert as_dict["gen_tokens"] == 1_000
    assert as_dict["total_tokens"] == 13_000
    restored = RunMetrics.from_dict(as_dict)
    assert restored == rm

    legacy = RunMetrics.from_dict(
        {
            "task_id": "api-order-sync",
            "arm_key": "v2_raw_baseline",
            "prompt_tokens": 500,
            "thought_tokens": 50,
            "output_tokens": 25,
        }
    )
    assert legacy.arm == "raw"
    assert legacy.category == "api"
    assert legacy.total_tokens == 575


def test_resolve_ctx_bin_and_select_tasks(tmp_path: Path) -> None:
    resolved = resolve_ctx_bin()
    assert resolved.is_file()
    assert os.access(resolved, os.X_OK)

    custom_ctx = _write_executable(tmp_path / "custom-ctx", "#!/usr/bin/env bash\necho 'ctx 0.0-test'\n")
    assert resolve_ctx_bin(custom_ctx) == custom_ctx.resolve()

    with pytest.raises(FileNotFoundError):
        resolve_ctx_bin(tmp_path / "missing-ctx")

    ds_root = tmp_path / "CommerceAgentBench"
    _create_synthetic_task(ds_root, category="file", task_id="file-alpha")
    _create_synthetic_task(ds_root, category="file", task_id="file-beta")
    _create_synthetic_task(ds_root, category="api", task_id="api-gamma", mock_services=True, service_port=4110)
    _create_synthetic_task(ds_root, category="cli", task_id="cli-delta")

    all_ids = discover_all_tasks(ds_root)
    assert all_ids == ["api-gamma", "cli-delta", "file-alpha", "file-beta"]

    spec = load_cab_task(ds_root, "api-gamma")
    assert spec.task_id == "api-gamma"
    assert spec.mock_services is True
    assert spec.host_published_port == 4110

    by_cat, _ = select_tasks(ds_root, categories=["file"])
    assert by_cat == ["file-alpha", "file-beta"]

    explicit, _ = select_tasks(ds_root, tasks=["cli-delta,file-alpha"])
    assert explicit == ["cli-delta", "file-alpha"]

    wave_path = tmp_path / "waves.json"
    wave_path.write_text(
        json.dumps({"wave1": ["file-beta", "api-gamma"], "wave2": ["cli-delta"]}),
        encoding="utf-8",
    )
    w1_ids, w1_map = select_tasks(ds_root, wave_file=wave_path, wave="wave1")
    assert w1_ids == ["file-beta", "api-gamma"]
    assert w1_map["file-beta"] == "wave1"

    all_waves, all_map = select_tasks(ds_root, wave_file=wave_path, wave="all")
    assert set(all_waves) == {"file-beta", "api-gamma", "cli-delta"}
    assert all_map["cli-delta"] == "wave2"

    sample_a, _ = select_tasks(ds_root, sample=2, seed=99)
    sample_b, _ = select_tasks(ds_root, sample=2, seed=99)
    assert len(sample_a) == 2
    assert sample_a == sample_b

    with pytest.raises(FileNotFoundError):
        select_tasks(ds_root, tasks=["nonexistent-task"])


# ---------------------------------------------------------------------------
# 3. Sandbox: Symlink Staging, Port Isolation, Grading & Fast PGID Teardown
# ---------------------------------------------------------------------------


def test_stage_mock_services_symlinks_assets_and_copies_mutable_dbs(tmp_path: Path) -> None:
    golden = tmp_path / "golden_mocks"
    (golden / "svc" / "node_modules" / "express").mkdir(parents=True)
    (golden / "svc" / "node_modules" / "express" / "index.js").write_text("module.exports = {};\n", encoding="utf-8")
    (golden / "svc" / "assets").mkdir(parents=True)
    (golden / "svc" / "assets" / "banner.png").write_bytes(b"\x89PNG\r\n\x1a\nfakepng")
    (golden / "svc" / "assets" / "spec.pdf").write_bytes(b"%PDF-1.4\nfakepdf")

    db_src = golden / "svc" / "seed.sqlite"
    conn = sqlite3.connect(db_src)
    conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO items (name) VALUES ('anchor')")
    conn.commit()
    conn.close()

    (golden / "svc" / "runner.py").write_text(
        "MOCK_ROOT = '/opt/mock_services'\nSTATE_ROOT = '/var/lib/mocksvc'\n",
        encoding="utf-8",
    )

    staged = tmp_path / "staged_mocks"
    state_dir = tmp_path / "mock_state"
    stage_mock_services_tree(golden, staged, state_dir)

    assert (staged / "svc" / "node_modules").is_symlink()
    assert (staged / "svc" / "assets" / "banner.png").is_symlink()
    assert (staged / "svc" / "assets" / "spec.pdf").is_symlink()

    # SQLite databases must be independent copies so mutations never corrupt golden seed files.
    staged_db = staged / "svc" / "seed.sqlite"
    assert staged_db.is_file()
    assert not staged_db.is_symlink()

    rewritten_py = (staged / "svc" / "runner.py").read_text(encoding="utf-8")
    assert str(staged) in rewritten_py
    assert str(state_dir) in rewritten_py
    assert "/opt/mock_services" not in rewritten_py


def test_task_sandbox_http_daemon_isolation_grade_and_fast_cleanup(tmp_path: Path) -> None:
    ds_root = tmp_path / "cab"
    runtime_dir = tmp_path / "runtime"
    (runtime_dir / "mock_services").mkdir(parents=True)
    _create_synthetic_task(
        ds_root,
        category="api",
        task_id="api-quote-daemon",
        mock_services=True,
        service_port=4321,
    )

    cfg = BenchConfig(dataset_root=ds_root, workdir=tmp_path / "work", runtime_dir=runtime_dir, judge_provider="mock")
    spec = load_cab_task(ds_root, "api-quote-daemon")
    run_dir = cfg.runs_dir / "api-quote-daemon__sj"
    meta_dir = cfg.runs_dir / "api-quote-daemon__sj_meta"

    sandbox = TaskSandbox(cfg, spec, run_dir, meta_dir, slot=84)
    try:
        sandbox.setup()
        assert sandbox.has_mocks is True
        mapped_port = sandbox.port_map[4321]
        assert 28400 <= mapped_port < 28500

        # Workspace files must have port 4321 surgically rewritten to mapped_port.
        orders_txt = (run_dir / "workspace" / "orders.json").read_text(encoding="utf-8")
        assert f"http://127.0.0.1:{mapped_port}/quote" in orders_txt
        assert "4321" not in orders_txt

        # Live HTTP mock service should answer on the slot-isolated port.
        with urllib.request.urlopen(f"http://127.0.0.1:{mapped_port}/quote", timeout=3) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        assert payload == {"ok": True, "port": mapped_port, "quote_usd": 420}
        assert len(_find_slot_pids(sandbox.slot_tag)) >= 1

        # Baseline empty outputs must score 0.0; valid deliverable must score 1.0.
        empty_grade = sandbox.grade(meta_dir / "reward_empty.json")
        assert empty_grade["passed"] is False
        assert empty_grade["score"] == 0.0

        (run_dir / "outputs" / "summary.json").write_text(
            json.dumps({"total_usd": 420, "status": "verified"}),
            encoding="utf-8",
        )
        pass_grade = sandbox.grade(meta_dir / "reward_pass.json")
        assert pass_grade["passed"] is True
        assert pass_grade["score"] == 1.0
    finally:
        t0 = time.perf_counter()
        sandbox.cleanup()
        cleanup_elapsed = time.perf_counter() - t0

    assert cleanup_elapsed < 1.0
    deadline = time.perf_counter() + 0.2
    leaked = _find_slot_pids(sandbox.slot_tag)
    while leaked and time.perf_counter() < deadline:
        time.sleep(0.01)
        leaked = _find_slot_pids(sandbox.slot_tag)
    assert leaked == []


# ---------------------------------------------------------------------------
# 4. Schemas & Pooled Browser Lifecycle
# ---------------------------------------------------------------------------


def test_schemas_tools_prompt_inventory_and_classifiers(tmp_path: Path) -> None:
    raw_tools = build_tools(requires_browser=False, requires_network=False, enable_state_note=False)
    raw_decls = {d["name"]: d for d in raw_tools[0]["functionDeclarations"]}
    assert set(raw_decls) == {"bash", "read", "write", "edit", "list", "glob", "grep", "present_files"}
    for decl in raw_decls.values():
        assert "state_note" not in decl["parameters"]["properties"]

    sj_tools = build_tools(requires_browser=True, requires_network=True, enable_state_note=True)
    sj_decls = {d["name"]: d for d in sj_tools[0]["functionDeclarations"]}
    assert "browser_open" in sj_decls
    assert "browser_snapshot" in sj_decls
    assert "state_note" in sj_decls["bash"]["parameters"]["properties"]
    assert "Execution & Verification Protocol" in SJ_EXECUTION_PROTOCOL
    assert "present_files" in SJ_EXECUTION_PROTOCOL

    case_dir = _create_synthetic_task(
        tmp_path / "cab",
        category="api",
        task_id="api-prompt-check",
        mock_services=True,
        service_port=4500,
    )
    prompt = build_prompt(case_dir, {4500: 20900})
    assert "http://127.0.0.1:20900/quote" in prompt
    assert "4500" not in prompt
    assert "/task/outputs" in prompt

    run_dir = tmp_path / "run_inv"
    (run_dir / "workspace" / "nested").mkdir(parents=True)
    (run_dir / "workspace" / "nested" / "ledger.csv").write_text("id,amount\n1,50\n", encoding="utf-8")
    inv = format_workspace_inventory(run_dir)
    assert "nested/ledger.csv" in inv

    # Canonical tool signatures ignore state_note / description metadata while sorting keys deterministically.
    sig1 = canonical_tool_sig("read", {"path": "/task/workspace/orders.json", "state_note": "step 1"})
    sig2 = canonical_tool_sig(
        "read",
        {"description": "inspect orders", "path": "/task/workspace/orders.json", "state_note": "step 2"},
    )
    assert sig1 == sig2

    assert is_mutating_tool_call("read", {"path": "/task/workspace/orders.json"}) is False
    assert is_mutating_tool_call("bash", {"command": "ls -la /task/workspace"}) is False
    assert is_mutating_tool_call("bash", {"command": "ctx get run:abcdef12#stdout --lines 1:20"}) is False
    assert is_mutating_tool_call("bash", {"command": "python3 generate_report.py > /task/outputs/out.json"}) is True
    assert is_mutating_tool_call("write", {"path": "/task/outputs/out.json", "content": "{}"}) is True
    assert is_mutating_tool_call("browser_click", {"ref": "e12"}) is True
    assert is_mutating_tool_call("browser_snapshot", {}) is False


def test_browser_pool_reuses_browser_per_thread_and_isolates_contexts(monkeypatch: pytest.MonkeyPatch) -> None:
    events: dict[str, int] = {
        "pw_start": 0,
        "pw_stop": 0,
        "browser_launch": 0,
        "browser_close": 0,
        "context_new": 0,
        "context_close": 0,
    }
    visited_urls: list[str] = []

    class FakeMouse:
        def wheel(self, dx: int, dy: int) -> None:
            pass

    class FakeKeyboard:
        def press(self, key: str) -> None:
            pass

    class FakePage:
        def __init__(self, ctx: FakeContext) -> None:
            self._ctx = ctx
            self.url = "about:blank"
            self.mouse = FakeMouse()
            self.keyboard = FakeKeyboard()
            self._closed = False

        def is_closed(self) -> bool:
            return self._closed

        def on(self, event: str, handler: Any) -> None:
            pass

        def goto(self, url: str, **kwargs: Any) -> None:
            self.url = url
            visited_urls.append(url)

        def title(self) -> str:
            return "Mock Commerce Portal"

        def evaluate(self, script: str, *args: Any) -> Any:
            return {
                "url": self.url,
                "title": self.title(),
                "controlsCount": 1,
                "controls": "[e1] button 'Submit'",
                "bodyText": "Portal body",
            }

        def wait_for_timeout(self, ms: int) -> None:
            pass

        def wait_for_load_state(self, state: str, **kwargs: Any) -> None:
            pass

    class FakeContext:
        def __init__(self, viewport: Any) -> None:
            self.viewport = viewport
            self.pages: list[FakePage] = []
            self.closed = False

        def on(self, event: str, handler: Any) -> None:
            pass

        def new_page(self) -> FakePage:
            page = FakePage(self)
            self.pages.append(page)
            return page

        def close(self) -> None:
            self.closed = True
            events["context_close"] += 1

    class FakeBrowser:
        def __init__(self) -> None:
            self.connected = True

        def is_connected(self) -> bool:
            return self.connected

        def new_context(self, **kwargs: Any) -> FakeContext:
            events["context_new"] += 1
            return FakeContext(kwargs.get("viewport"))

        def close(self) -> None:
            self.connected = False
            events["browser_close"] += 1

    class FakeChromium:
        def launch(self, **kwargs: Any) -> FakeBrowser:
            events["browser_launch"] += 1
            return FakeBrowser()

    class FakePlaywright:
        def __init__(self) -> None:
            self.chromium = FakeChromium()

        def stop(self) -> None:
            events["pw_stop"] += 1

    class FakeSyncPlaywrightCM:
        def start(self) -> FakePlaywright:
            events["pw_start"] += 1
            return FakePlaywright()

    fake_pw_pkg = types.ModuleType("playwright")
    fake_sync_mod = types.ModuleType("playwright.sync_api")
    fake_sync_mod.sync_playwright = lambda: FakeSyncPlaywrightCM()  # type: ignore[attr-defined]
    fake_pw_pkg.sync_api = fake_sync_mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", fake_pw_pkg)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_sync_mod)

    pool = BrowserPool()
    s1 = pool.acquire_session({3000: 20500})
    assert isinstance(s1, BrowserSession)
    out1 = s1.open_tab("http://localhost:3000/dashboard")
    assert "http://localhost:20500/dashboard" in visited_urls[-1]
    assert "Mock Commerce Portal" in out1
    s1.close()

    # Closing a task session must close ONLY its BrowserContext, keeping the worker browser warm.
    assert events["browser_launch"] == 1
    assert events["context_new"] == 1
    assert events["context_close"] == 1
    assert events["browser_close"] == 0

    # Acquiring a second session on the same worker thread reuses the warm browser.
    s2 = pool.acquire_session({3000: 20600})
    s2.open_tab("http://localhost:3000/orders")
    assert "http://localhost:20600/orders" in visited_urls[-1]
    s2.close()
    assert events["browser_launch"] == 1
    assert events["context_new"] == 2
    assert events["context_close"] == 2

    pool.close_all()
    assert events["browser_close"] == 1
    assert events["pw_stop"] == 1


# ---------------------------------------------------------------------------
# 5. ToolHost: raw vs sj Parity, ctx Capture, Dedup, Parallel --help & Gate
# ---------------------------------------------------------------------------


def _make_host(
    tmp_path: Path,
    *,
    arm: str,
    entry_gate_bytes: int = 4_096,
    baseline_raw_cap: int = 20_000,
    extra_bin: Path | None = None,
) -> tuple[ToolHost, RunMetrics, Path, Path]:
    ds_root = tmp_path / "cab"
    ds_root.mkdir(parents=True, exist_ok=True)
    workdir = tmp_path / f"work_{arm}"
    cfg = BenchConfig(
        dataset_root=ds_root,
        workdir=workdir,
        arms=(arm,),
        entry_gate_bytes=entry_gate_bytes,
        baseline_raw_cap=baseline_raw_cap,
    )
    run_dir = cfg.runs_dir / f"task__{arm}"
    meta_dir = cfg.runs_dir / f"task__{arm}_meta"
    (run_dir / "workspace").mkdir(parents=True, exist_ok=True)
    (run_dir / "outputs").mkdir(parents=True, exist_ok=True)
    meta_dir.mkdir(parents=True, exist_ok=True)

    mock_env = dict(os.environ)
    if extra_bin is not None:
        mock_env["PATH"] = f"{extra_bin}{os.pathsep}{mock_env.get('PATH', '')}"

    metrics = RunMetrics(
        task_id="file-host-check",
        task_rel="file/file-host-check",
        category="file",
        arm=arm,
        model_id="test-model",
    )
    host = ToolHost(cfg, run_dir, meta_dir, {3000: 20100}, mock_env, metrics, None, arm=arm)
    return host, metrics, run_dir, meta_dir


def test_tool_host_raw_vs_sj_entry_gate_and_ctx_retrieval(tmp_path: Path) -> None:
    extra_bin = tmp_path / "mock_bin"
    _write_executable(extra_bin / "mockping", "#!/usr/bin/env bash\necho PONG_OK\n")

    raw_host, raw_metrics, _, _ = _make_host(
        tmp_path, arm="raw", entry_gate_bytes=4_096, baseline_raw_cap=100_000, extra_bin=extra_bin
    )
    sj_host, sj_metrics, _, _ = _make_host(
        tmp_path, arm="sj", entry_gate_bytes=4_096, baseline_raw_cap=100_000, extra_bin=extra_bin
    )

    hist_key = "HIST" + "FILE"
    less_hist_key = "LESSHIST" + "FILE"
    probe_cmd = (
        "python3 -B -c \"import os, sys; "
        f"print('PY=' + sys.executable); print('H=' + str({hist_key!r} in os.environ or {less_hist_key!r} in os.environ))\" "
        "&& mockping"
    )
    raw_host.step = 1
    sj_host.step = 1
    fr_raw_env, _, _ = raw_host.execute_part("bash", {"command": probe_cmd})
    fr_sj_env, _, _ = sj_host.execute_part("bash", {"command": probe_cmd})
    out_raw_env = fr_raw_env["functionResponse"]["response"]["output"]
    out_sj_env = fr_sj_env["functionResponse"]["response"]["output"]

    # Both arms must resolve the exact same venv python3 and preserve mock_env PATH without shell-history env vars.
    assert out_raw_env == out_sj_env
    assert "PONG_OK" in out_raw_env
    assert "H=False" in out_raw_env

    # Flood command (>40 KB > ctx Entry Gate threshold, < 100 KB baseline_raw_cap).
    flood_cmd = (
        "python3 -B -c \"for i in range(650): print(f'LEDGER_ITEM_{i:04d}_USD_420_' + 'z'*44)\""
    )
    raw_host.step = 2
    fr_raw, _, _ = raw_host.execute_part("bash", {"command": flood_cmd})
    raw_out = fr_raw["functionResponse"]["response"]["output"]
    assert raw_metrics.digests_delivered == 0
    assert "LEDGER_ITEM_0000_USD_420_" in raw_out
    assert "LEDGER_ITEM_0649_USD_420_" in raw_out

    sj_host.step = 2
    fr_sj, _, _ = sj_host.execute_part("bash", {"command": flood_cmd, "state_note": "captured ledger flood"})
    sj_out = fr_sj["functionResponse"]["response"]["output"]
    assert sj_metrics.digests_delivered == 1
    assert sj_metrics.bytes_kept_out > 0
    assert sj_metrics.state_notes_emitted == 1

    m = re.search(r"\brun:([0-9a-f]{6,64})\b", sj_out)
    assert m is not None, f"Expected run:<hash> handle in sj digest output, got: {sj_out[:300]}"
    handle = f"run:{m.group(1)}"

    sj_host.step = 3
    fr_get, _, _ = sj_host.execute_part("bash", {"command": f"ctx get {handle}#stdout --lines 1:4"})
    get_out = fr_get["functionResponse"]["response"]["output"]
    assert "LEDGER_ITEM_0000_USD_420_" in get_out
    assert sj_metrics.ctx_retrievals.get("get", 0) >= 1

    sj_host.step = 4
    fr_search, _, _ = sj_host.execute_part("bash", {"command": f'ctx search {handle} "LEDGER_ITEM_0649"'})
    search_out = fr_search["functionResponse"]["response"]["output"]
    assert "LEDGER_ITEM_0649_USD_420_" in search_out
    assert sj_metrics.ctx_retrievals.get("search", 0) >= 1


def test_tool_host_readonly_dedup_and_sha256_file_dedup(tmp_path: Path) -> None:
    host, metrics, run_dir, _ = _make_host(tmp_path, arm="sj")
    doc_path = run_dir / "workspace" / "policy.txt"
    doc_path.write_text(
        "Primary margin floor rule: minimum gross margin is 18.5%.\n"
        + ("Supporting regional compliance clause line.\n" * 20),
        encoding="utf-8",
    )

    # 1. Identical read-only `list` call in the same mutation_epoch is blocked.
    host.step = 1
    fr1, _, _ = host.execute_part("list", {"path": "/task/workspace"})
    assert "policy.txt" in fr1["functionResponse"]["response"]["output"]
    assert host.last_call_was_dup is False

    host.step = 2
    fr2, _, _ = host.execute_part("list", {"path": "/task/workspace"})
    assert host.last_call_was_dup is True
    assert metrics.dup_blocked == 1
    assert "identical call already ran" in fr2["functionResponse"]["response"]["output"].lower()

    # 2. Content-addressed sha256 file re-read dedup across distinct line-unbound reads after epoch bump.
    host.step = 3
    r1, _, _ = host.execute_part("read", {"path": "/task/workspace/policy.txt"})
    assert "18.5%" in r1["functionResponse"]["response"]["output"]

    # Mutate an unrelated scratch file to advance mutation_epoch, then re-read unchanged policy.txt.
    host.step = 4
    host.execute_part("write", {"path": "/task/workspace/notes.txt", "content": "checked"})
    host.step = 5
    r2, _, _ = host.execute_part("read", {"path": "/task/workspace/policy.txt"})
    assert metrics.file_dedup_hits == 1
    assert "sha256" in r2["functionResponse"]["response"]["output"].lower() or "unchanged" in r2["functionResponse"]["response"]["output"].lower()

    # Modifying policy.txt changes its sha256 so the next read returns the fresh text in full.
    host.step = 6
    host.execute_part(
        "edit",
        {
            "file_path": "/task/workspace/policy.txt",
            "old_string": "18.5%",
            "new_string": "22.0%",
        },
    )
    host.step = 7
    r3, _, _ = host.execute_part("read", {"path": "/task/workspace/policy.txt"})
    assert "22.0%" in r3["functionResponse"]["response"]["output"]


def test_bash_dedup_signature_keeps_paging_and_words_with_hash_distinct(tmp_path: Path) -> None:
    from evals.acciobench.host import _normalize_bash_cmd_for_dedup as sig

    # `#` inside a word is text; `:` never splits a word.
    a = sig("ctx get run:4088c70a0973#stdout --lines 1:5")
    b = sig("ctx get run:4088c70a0973#stdout --lines 121:240")
    assert a != b and "run:4088c70a0973#stdout" in a and "1:5" in a
    assert sig("ctx get run:4088c70a0973#stdout --lines 1:5") != sig("ctx get run:4088c70a0974#stdout --lines 1:5")
    assert sig("ctx get run:aaaaaa#stdout --lines 1:5") != sig("ctx get run:aaaaaa#stderr --lines 1:5")
    assert sig("echo a#b c") != sig("echo a#b d")
    assert sig("echo a:b") != sig("echo a : b")
    assert sig("curl http://x/y#one") != sig("curl http://x/y#two")
    assert sig("grep 'a#b' f") != sig("grep 'a#c' f")
    # A real trailing comment, blanks and flag clusters still normalize.
    assert sig("ls -la # list") == sig("ls -la") == sig("ls   -al")
    assert sig("cat x|wc -l") == sig("cat x | wc  -l")
    assert sig("echo '#' x") == "echo '#' x" and sig('echo "#" x') == 'echo "#" x'
    assert sig("echo '#' x") != sig("echo")

    host, metrics, _, _ = _make_host(tmp_path, arm="sj", entry_gate_bytes=2_048)
    host.step = 1
    fr, _, _ = host.execute_part("bash", {"command": "seq 1 3000 | sed 's/^/PAGE_ROW_/'"})
    m = re.search(r"\brun:([0-9a-f]{6,64})\b", fr["functionResponse"]["response"]["output"])
    assert m is not None
    h = f"run:{m.group(1)}"

    def bash(step: int, cmd: str) -> str:
        host.step = step
        out, _, _ = host.execute_part("bash", {"command": cmd})
        return out["functionResponse"]["response"]["output"]

    p1 = bash(2, f"ctx get {h}#stdout --lines 1:5")
    assert "PAGE_ROW_1" in p1 and host.last_call_was_dup is False
    p2 = bash(3, f"ctx get {h}#stdout --lines 121:240")
    assert host.last_call_was_dup is False and "PAGE_ROW_121" in p2 and "Identical call" not in p2
    p3 = bash(4, f"ctx get {h}#stdout --lines 2:6")
    assert host.last_call_was_dup is False and "PAGE_ROW_6" in p3
    dup_before = metrics.dup_blocked
    p4 = bash(5, f"ctx get {h}#stdout --lines 1:5")
    assert host.last_call_was_dup is True and metrics.dup_blocked == dup_before + 1
    assert "Identical call already ran" in p4
    bash(6, "ls -la")
    bash(7, "ls -la # list again")
    assert host.last_call_was_dup is True


def test_tool_host_parallel_cli_help_enrichment(tmp_path: Path) -> None:
    cli_dir = tmp_path / "cli_bin"
    _write_executable(
        cli_dir / "shopctl",
        "#!" + sys.executable + "\n"
        "import sys, time\n"
        "args = sys.argv[1:]\n"
        "if args == ['--help']:\n"
        "    print('Usage: shopctl <command> [options]\\n\\nCommands:\\n  orders     Manage orders\\n  refunds    Issue refunds\\n  inventory  Sync stock\\n  shipments  Track parcels')\n"
        "elif len(args) == 2 and args[1] == '--help':\n"
        "    time.sleep(0.45)\n"
        "    print(f'Usage: shopctl {args[0]} --store-id ID --format json')\n"
        "else:\n"
        "    print('ok')\n",
    )

    host, metrics, _, _ = _make_host(tmp_path, arm="sj", extra_bin=cli_dir)
    top_help = "Usage: shopctl <command> [options]\n\nCommands:\n  orders     Manage orders\n  refunds    Issue refunds\n  inventory  Sync stock\n  shipments  Track parcels\n"
    t0 = time.perf_counter()
    out = host._maybe_enrich_cli_help("shopctl --help", top_help)
    elapsed = time.perf_counter() - t0

    assert metrics.cli_lsp_enrichments == 1
    assert "[cli-lsp subcommand signatures]" in out
    for sub in ("orders", "refunds", "inventory", "shipments"):
        assert f"shopctl {sub} --store-id ID" in out
    # 4 subcommands x 0.45s sequential would take >= 1.80s; parallel 4-worker probe runs well under 1.25s.
    assert elapsed < 1.25


def test_tool_host_present_files_emission_gate_and_window_telemetry(tmp_path: Path) -> None:
    # 1. Missing deliverable -> rejected on first attempt.
    h_miss, m_miss, _, _ = _make_host(tmp_path / "c1", arm="sj")
    h_miss.step = 1
    fr_missing, _, _ = h_miss.execute_part("present_files", {"files": ["/task/outputs/report.json"]})
    assert h_miss.completed is False
    assert m_miss.emission_gate_hits == 1
    assert "emission-gate" in fr_missing["functionResponse"]["response"]["output"].lower()

    # 2. Empty 0-byte deliverable -> rejected.
    h_empty, m_empty, r_empty, _ = _make_host(tmp_path / "c2", arm="sj")
    (r_empty / "outputs" / "report.json").write_text("", encoding="utf-8")
    h_empty.step = 1
    h_empty.execute_part("present_files", {"files": ["/task/outputs/report.json"]})
    assert h_empty.completed is False
    assert m_empty.emission_gate_hits == 1

    # 3. Malformed JSON deliverable -> rejected.
    h_json, m_json, r_json, _ = _make_host(tmp_path / "c3", arm="sj")
    (r_json / "outputs" / "report.json").write_text("{broken json", encoding="utf-8")
    h_json.step = 1
    h_json.execute_part("present_files", {"files": ["/task/outputs/report.json"]})
    assert h_json.completed is False
    assert m_json.emission_gate_hits == 1

    # 4. Valid JSON deliverable, but stray helper script inside /task/outputs -> rejected once, then clean retry passes.
    host, metrics, run_dir, _ = _make_host(tmp_path / "c4", arm="sj")
    out_json = run_dir / "outputs" / "report.json"
    out_json.write_text(json.dumps({"status": "ok", "rows": [1, 2]}), encoding="utf-8")
    stray_py = run_dir / "outputs" / "calc_tmp.py"
    stray_py.write_text("print('scratch')\n", encoding="utf-8")
    host.step = 1
    host.execute_part("present_files", {"files": ["/task/outputs/report.json"]})
    assert host.completed is False
    assert metrics.emission_gate_hits == 1

    # 5. Remove stray script -> present_files passes cleanly!
    stray_py.unlink()
    host.step = 2
    fr_ok, _, _ = host.execute_part("present_files", {"files": ["/task/outputs/report.json"]})
    assert host.completed is True
    assert "PRESENTED" in fr_ok["functionResponse"]["response"]["output"].upper() or "report.json" in fr_ok["functionResponse"]["response"]["output"]

    # Window telemetry writes sidecar window.json cleanly.
    host.update_window_telemetry("gemini-2.5-flash", 5, {"promptTokenCount": 4200, "cachedContentTokenCount": 3100})
    win_file = host.ctx_ws / ".ctx-session-reads" / "proxy" / "window.json"
    assert win_file.is_file()
    win_doc = json.loads(win_file.read_text(encoding="utf-8"))
    assert win_doc["last_input_tokens"] == 4200
    assert win_doc["cached_tokens"] == 3100


# ---------------------------------------------------------------------------
# 6. Providers, Agent Loop & Multi-Arm Suite Orchestrator
# ---------------------------------------------------------------------------


def test_providers_keepalive_pooling_untouched_defaults_and_adc_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created_conns: list[FakeHTTPSConnection] = []
    sent_requests: list[dict[str, Any]] = []
    fail_next_with_stale_socket = {"armed": False}
    fail_next_with_401 = {"armed": False}

    class FakeHTTPResponse:
        def __init__(self, status: int, body: dict[str, Any]) -> None:
            self.status = status
            self._raw = json.dumps(body).encode("utf-8")

        def read(self) -> bytes:
            return self._raw

    class FakeHTTPSConnection:
        def __init__(self, host: str, timeout: float = 300.0) -> None:
            self.host = host
            self.timeout = timeout
            self.closed = False
            self.sock: object | None = None
            self._resp: FakeHTTPResponse | None = None
            created_conns.append(self)

        def request(self, method: str, path: str, body: bytes | None = None, headers: dict[str, str] | None = None) -> None:
            if fail_next_with_stale_socket["armed"]:
                fail_next_with_stale_socket["armed"] = False
                raise http.client.RemoteDisconnected("Simulated idle keep-alive close")
            self.sock = object()
            parsed = json.loads(body.decode("utf-8")) if body else {}
            sent_requests.append(
                {
                    "host": self.host,
                    "method": method,
                    "path": path,
                    "headers": dict(headers or {}),
                    "body": parsed,
                }
            )
            if fail_next_with_401["armed"]:
                fail_next_with_401["armed"] = False
                self._resp = FakeHTTPResponse(401, {"error": {"message": "token expired"}})
            else:
                self._resp = FakeHTTPResponse(
                    200,
                    {
                        "candidates": [{"content": {"role": "model", "parts": [{"text": "ok"}]}}],
                        "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 10},
                    },
                )

        def getresponse(self) -> FakeHTTPResponse:
            assert self._resp is not None
            return self._resp

        def close(self) -> None:
            self.closed = True
            self.sock = None

    monkeypatch.setattr(http.client, "HTTPSConnection", FakeHTTPSConnection)

    # 1. GeminiProvider: untouched defaults (no generationConfig), keep-alive reuse, stale reconnect.
    gem_cfg = BenchConfig(
        dataset_root=tmp_path / "cab",
        workdir=tmp_path / "work",
        provider="gemini",
        api_key="gem-secret-key",
        model="gemini-2.5-flash",
    )
    gem = create_provider(gem_cfg)
    assert isinstance(gem, GeminiProvider)

    gem.generate("gemini-2.5-flash", [{"role": "user", "parts": [{"text": "hi"}]}], [])
    gem.generate("gemini-2.5-flash", [{"role": "user", "parts": [{"text": "turn 2"}]}], [])
    assert len(created_conns) == 1  # Same socket reused across turns!
    assert "generationConfig" not in sent_requests[-1]["body"]
    assert sent_requests[-1]["headers"].get("x-goog-api-key") == "gem-secret-key"
    assert "/v1beta/models/gemini-2.5-flash:generateContent" in sent_requests[-1]["path"]

    # Stale keep-alive disconnect triggers transparent reconnect on a second socket.
    fail_next_with_stale_socket["armed"] = True
    gem.generate("gemini-2.5-flash", [{"role": "user", "parts": [{"text": "turn 3"}]}], [])
    assert len(created_conns) == 2
    gem.close()
    assert created_conns[-1].closed is True

    # 2. Explicit ablation overrides populate generationConfig; VertexProvider refreshes ADC on 401.
    token_calls = {"n": 0}

    def _fake_fetch_adc(self: AdcTokenCache) -> str:
        token_calls["n"] += 1
        return f"fake-bearer-token-{token_calls['n']}"

    monkeypatch.setattr(AdcTokenCache, "_default_fetch", _fake_fetch_adc)
    monkeypatch.delenv("VERTEX_ACCESS_TOKEN", raising=False)

    vtx_cfg = BenchConfig(
        dataset_root=tmp_path / "cab",
        workdir=tmp_path / "work_vtx",
        provider="vertex",
        project_id="demo-gcp-project",
        location="global",
        thinking_level="HIGH",
        max_output_tokens=8192,
    )
    vtx = create_provider(vtx_cfg)
    assert isinstance(vtx, VertexProvider)

    fail_next_with_401["armed"] = True
    vtx.generate("gemini-2.5-flash", [{"role": "user", "parts": [{"text": "hello vertex"}]}], [])
    assert token_calls["n"] == 2  # Initial fetch + automatic refresh after 401!
    last_req = sent_requests[-1]
    assert last_req["headers"].get("Authorization") == "Bearer fake-bearer-token-2"
    assert last_req["body"]["generationConfig"]["maxOutputTokens"] == 8192
    assert last_req["body"]["generationConfig"]["thinkingConfig"]["thinkingLevel"] == "HIGH"
    vtx.close()


def test_run_trial_and_run_suite_paired_arms_with_residence_and_resume(tmp_path: Path) -> None:
    ds_root = tmp_path / "cab"
    _create_synthetic_task(ds_root, category="file", task_id="file-suite-ab")

    class ScriptedProvider:
        def __init__(self) -> None:
            self.lock = threading.Lock()
            self.calls = 0

        def generate(
            self,
            model_id: str,
            contents: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            *,
            session_id: str | None = None,
            system_instruction: str | None = None,
            tool_config: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            with self.lock:
                self.calls += 1
            user_turns = len([c for c in contents if c.get("role") == "user"])
            decl_props = (
                tools[0]["functionDeclarations"][0]["parameters"]["properties"]
                if tools and tools[0].get("functionDeclarations")
                else {}
            )
            supports_note = "state_note" in decl_props

            if user_turns == 1:
                args: dict[str, Any] = {"path": "/task/workspace/orders.json"}
                if supports_note:
                    args["state_note"] = "reading orders.json to sum SKU totals"
                part = {"functionCall": {"name": "read", "args": args}}
            elif user_turns == 2:
                args = {
                    "path": "/task/outputs/summary.json",
                    "content": json.dumps({"total_usd": 420, "status": "verified"}),
                }
                if supports_note:
                    args["state_note"] = "wrote verified total_usd=420 summary.json"
                part = {"functionCall": {"name": "write", "args": args}}
            elif user_turns == 3:
                args = {"files": ["/task/outputs/summary.json"]}
                part = {"functionCall": {"name": "present_files", "args": args}}
            else:
                part = {"text": "Completed."}

            return {
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [
                                {"thought": True, "text": "Reasoning through order totals..." * 10},
                                part,
                            ],
                        }
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 1200,
                    "cachedContentTokenCount": 600,
                    "thoughtsTokenCount": 120,
                    "candidatesTokenCount": 40,
                },
            }

        def judge_access_token(self) -> str:
            return ""

        def close(self) -> None:
            pass

    provider = ScriptedProvider()
    cfg = BenchConfig(
        allow_unsandboxed=True,  # explicit opt-in: CI has no bwrap
        dataset_root=ds_root,
        workdir=tmp_path / "ab_work",
        provider="gemini",
        api_key="dummy",
        judge_provider="mock",
        arms=("raw", "sj"),
        workers=2,
        slot_offset=80,
    )

    import evals.acciobench.orchestrator as orch_mod

    orig_create = orch_mod.create_provider
    orch_mod.create_provider = lambda _c: provider  # type: ignore[assignment]
    try:
        results = run_suite(cfg, ["file-suite-ab"], wave_map={"file-suite-ab": "wave1"}, bucket_map={"file-suite-ab": "worse"})
        assert len(results) == 2
        by_arm = {r.arm: r for r in results}
        assert set(by_arm) == {"raw", "sj"}
        assert by_arm["raw"].passed is True
        assert by_arm["sj"].passed is True
        assert by_arm["raw"].capacity_score == 1.0
        assert by_arm["sj"].capacity_score == 1.0
        assert by_arm["sj"].state_notes_emitted >= 1
        assert by_arm["raw"].state_notes_emitted == 0
        calls_after_first_run = provider.calls
        assert calls_after_first_run >= 6

        # Resume mode must reload completed result.json files without invoking the model again.
        cached_results = run_suite(cfg, ["file-suite-ab"])
        assert len(cached_results) == 2
        assert provider.calls == calls_after_first_run
    finally:
        orch_mod.create_provider = orig_create


class _SolvingProvider:
    """Reads orders.json, writes the expected summary and presents it."""

    def __init__(self) -> None:
        self.calls = 0
        self.models: list[str] = []

    def generate(self, model_id: str, contents: list[dict[str, Any]], tools: list[dict[str, Any]], **_kw: Any) -> dict[str, Any]:
        self.calls += 1
        self.models.append(model_id)
        user_turns = len([c for c in contents if c.get("role") == "user"])
        if user_turns == 1:
            part: dict[str, Any] = {"functionCall": {"name": "read", "args": {"path": "/task/workspace/orders.json"}}}
        elif user_turns == 2:
            part = {"functionCall": {"name": "write", "args": {
                "path": "/task/outputs/summary.json",
                "content": json.dumps({"total_usd": 420, "status": "verified"}),
            }}}
        elif user_turns == 3:
            part = {"functionCall": {"name": "present_files", "args": {"files": ["/task/outputs/summary.json"]}}}
        else:
            part = {"text": "Completed."}
        return {
            "candidates": [{"content": {"role": "model", "parts": [part]}}],
            "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 10},
        }

    def judge_access_token(self) -> str:
        return ""

    def close(self) -> None:
        pass


class _MustNotGenerateProvider(_SolvingProvider):
    def generate(self, *a: Any, **kw: Any) -> dict[str, Any]:
        pytest.fail("resume made a model call")


def _flaky_grader_task(ds_root: Path, task_id: str, flag: Path) -> Path:
    """A task whose grader crashes (no reward.json) until ``flag`` exists, then grades for real."""
    case_dir = _create_synthetic_task(ds_root, category="file", task_id=task_id)
    grader = case_dir / "grader"
    (grader / "run.sh").rename(grader / "real.sh")
    _write_executable(
        grader / "run.sh",
        "#!/usr/bin/env bash\n"
        f'FLAG="{flag}"\n'
        'if [ ! -e "$FLAG" ]; then : > "$FLAG"; echo "grader crashed: judge timeout" >&2; exit 2; fi\n'
        'exec bash "$(dirname "$0")/real.sh"\n',
    )
    return case_dir


def test_grading_error_is_ungraded_reported_apart_and_regraded_on_resume(tmp_path: Path) -> None:
    from evals.acciobench.html_report import build_html_report_payload
    from evals.acciobench.report import compute_summary_payload

    ds_root = tmp_path / "cab"
    flag = tmp_path / "grader_has_run_once"
    _flaky_grader_task(ds_root, "file-flaky-grader", flag)
    cfg = BenchConfig(
        allow_unsandboxed=True,  # explicit opt-in: CI has no bwrap
        dataset_root=ds_root, workdir=tmp_path / "work", provider="gemini", api_key="dummy",
        judge_provider="mock", arms=("sj",), slot_offset=70,
    )

    first = run_trial(cfg, "file-flaky-grader", "sj", 71, _SolvingProvider())  # type: ignore[arg-type]
    assert first.termination == "present_files"
    assert first.grading_error and "grader crashed" in first.grading_error
    assert first.passed is False
    result_file = cfg.runs_dir / "file-flaky-grader__sj__t1_meta" / "result.json"
    saved = json.loads(result_file.read_text(encoding="utf-8"))
    assert saved["grading_error"]

    # Reports: an ungraded trial is not a failed one; it is counted apart in every format.
    loaded = load_run_results(cfg.workdir)
    payload = compute_summary_payload(loaded)
    assert payload["arms"]["sj"]["grading_error_runs"] == 1
    assert payload["arms"]["sj"]["overall"]["n"] == 0
    assert payload["grading_error_runs"] == 1
    assert "Grading errors: 1" in render_summary_report(loaded, format="table")
    assert "Grading errors: 1" in render_summary_report(loaded, format="markdown")
    assert json.loads(render_summary_report(loaded, format="json"))["grading_error_runs"] == 1
    html_meta = build_html_report_payload(loaded, workdir=cfg.workdir)["meta"]
    assert html_meta["grading_errors_total"] == 1 and html_meta["grading_errors"] == {"sj": 1}

    # Resume re-grades the existing outputs: no model call, run directory untouched.
    run_dir = cfg.runs_dir / "file-flaky-grader__sj__t1"
    keep = run_dir / "tmp" / "agent_scratch.txt"
    keep.write_text("keep me", encoding="utf-8")
    second = run_trial(cfg, "file-flaky-grader", "sj", 71, _MustNotGenerateProvider())  # type: ignore[arg-type]
    assert second.grading_error == ""
    assert second.passed is True and second.capacity_score == 1.0
    assert second.steps == first.steps and second.prompt_tokens == first.prompt_tokens
    assert keep.read_text(encoding="utf-8") == "keep me"
    assert json.loads(result_file.read_text(encoding="utf-8"))["passed"] is True
    payload2 = compute_summary_payload(load_run_results(cfg.workdir))
    assert payload2["arms"]["sj"]["grading_error_runs"] == 0
    assert payload2["arms"]["sj"]["overall"]["passed"] == 1

    # A graded result is then plainly cached (no model call, no grader run).
    flag.unlink()
    third = run_trial(cfg, "file-flaky-grader", "sj", 71, _MustNotGenerateProvider())  # type: ignore[arg-type]
    assert third.passed is True and not flag.exists()

    # Re-grading impossible (run directory gone): only then is the trial re-run.
    doc = json.loads(result_file.read_text(encoding="utf-8"))
    doc.update(grading_error="grader timeout", passed=False)
    result_file.write_text(json.dumps(doc), encoding="utf-8")
    import shutil as _shutil

    _shutil.rmtree(run_dir)
    flag.touch()
    rerun_provider = _SolvingProvider()
    fourth = run_trial(cfg, "file-flaky-grader", "sj", 71, rerun_provider)  # type: ignore[arg-type]
    assert rerun_provider.calls >= 3
    assert fourth.passed is True and fourth.grading_error == ""


def test_resume_accepts_cached_results_only_for_the_same_configuration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import dataclasses as _dc

    ds_root = tmp_path / "cab"
    _create_synthetic_task(ds_root, category="file", task_id="file-fp")
    base = BenchConfig(
        allow_unsandboxed=True,  # explicit opt-in: CI has no bwrap
        dataset_root=ds_root, workdir=tmp_path / "work", model="model-a", provider="gemini",
        api_key="dummy", judge_provider="mock", arms=("sj",), slot_offset=60,
    )
    meta = base.runs_dir / "file-fp__sj__t1_meta"
    result_file = meta / "result.json"

    def run(cfg: BenchConfig, provider: Any) -> RunMetrics:
        return run_trial(cfg, "file-fp", "sj", 61, provider)

    p0 = _SolvingProvider()
    first = run(base, p0)
    assert p0.calls >= 3 and first.passed
    fp_a = json.loads(result_file.read_text(encoding="utf-8"))["config_fingerprint"]
    assert fp_a

    # Same configuration (other workers, slot and resume settings): cached, no model call.
    same = _dc.replace(base, workers=9, slot_offset=3)
    assert run(same, _MustNotGenerateProvider()).passed

    # Each behaviour-changing setting re-runs the trial and says why in one line.
    for i, change in enumerate(({"model": "model-b"}, {"thinking_level": "HIGH"}, {"entry_gate_bytes": 1_000})):
        capsys.readouterr()
        cfg = _dc.replace(base, **change)
        prov = _SolvingProvider()
        res = run(cfg, prov)
        assert prov.calls >= 3, change
        assert set(prov.models) == {cfg.model}
        assert res.model_id == cfg.model
        out = capsys.readouterr().out
        assert "configuration changed" in out, (change, out)
        new_fp = json.loads(result_file.read_text(encoding="utf-8"))["config_fingerprint"]
        assert new_fp != fp_a
        # The previous configuration's trial is kept aside, never loaded by reports.
        assert any((tmp_path / "work" / "superseded").glob("*/file-fp__sj__t1_meta/result.json"))
        assert len(load_run_results(base.workdir)) == 1
        fp_a = new_fp

    import evals.acciobench.loop as loop_mod

    def reruns(cfg: BenchConfig) -> bool:
        prov = _SolvingProvider()
        run(cfg, prov)
        return prov.calls > 0

    # The task itself: same id, changed prompt, under another dataset root.
    last_cfg = _dc.replace(base, entry_gate_bytes=1_000)
    assert not reruns(last_cfg)
    ds2 = tmp_path / "cab2"
    import shutil as _shutil

    _shutil.copytree(ds_root, ds2)
    task_md = ds2 / "datasets_domain_v1" / "file" / "file-fp" / "task.md"
    task_md.write_text(task_md.read_text(encoding="utf-8") + "\nAlso report the median.\n", encoding="utf-8")
    cfg_ds2 = _dc.replace(last_cfg, dataset_root=ds2)
    assert reruns(cfg_ds2)
    assert not reruns(cfg_ds2)
    # A grader-only change also re-runs (the recorded score must come from the current grader).
    grader = ds2 / "datasets_domain_v1" / "file" / "file-fp" / "grader" / "run.sh"
    grader.write_text(grader.read_text(encoding="utf-8") + "\n# v2\n", encoding="utf-8")
    assert reruns(cfg_ds2)
    # The ctx being benchmarked: a ctx that reports another version (same behaviour otherwise).
    real_ctx = resolve_ctx_bin(None)
    other_ctx = _write_executable(
        tmp_path / "otherctx" / "ctx",
        f'#!/usr/bin/env bash\nif [ "$1" = --version ]; then echo "ctx 99.0.0"; exit 0; fi\nexec "{real_ctx}" "$@"\n',
    )
    assert reruns(_dc.replace(cfg_ds2, ctx_bin=other_ctx))
    assert not reruns(_dc.replace(cfg_ds2, ctx_bin=other_ctx))
    # Whether the bwrap sandbox is active.
    real_probe = loop_mod.bwrap_sandbox_executable
    try:
        loop_mod.bwrap_sandbox_executable = lambda: None  # type: ignore[assignment]
        assert reruns(cfg_ds2)
        loop_mod.bwrap_sandbox_executable = lambda: "/usr/bin/bwrap"  # type: ignore[assignment]
        assert reruns(cfg_ds2)
        assert not reruns(cfg_ds2)
    finally:
        loop_mod.bwrap_sandbox_executable = real_probe  # type: ignore[assignment]
    capsys.readouterr()

    # Legacy results (no fingerprint): accepted only for the same model, with a notice.
    doc = json.loads(result_file.read_text(encoding="utf-8"))
    doc.pop("config_fingerprint")
    result_file.write_text(json.dumps(doc), encoding="utf-8")
    capsys.readouterr()
    assert run(last_cfg, _MustNotGenerateProvider()).model_id == "model-a"
    assert "predates config fingerprints" in capsys.readouterr().out

    doc["model_id"] = "model-old"
    result_file.write_text(json.dumps(doc), encoding="utf-8")
    prov = _SolvingProvider()
    res = run(last_cfg, prov)
    out = capsys.readouterr().out
    assert prov.calls >= 3 and res.model_id == "model-a"
    assert "model_id=model-old, not model-a" in out
    assert (tmp_path / "work" / "superseded" / "legacy-model-old" / "file-fp__sj__t1_meta").is_dir()


def test_supersede_never_deletes_a_previous_configurations_trial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import dataclasses as _dc

    import evals.acciobench.loop as loop_mod

    ds_root = tmp_path / "cab"
    _create_synthetic_task(ds_root, category="file", task_id="file-sup")
    cfg_a = BenchConfig(
        allow_unsandboxed=True,  # explicit opt-in: CI has no bwrap
        dataset_root=ds_root, workdir=tmp_path / "work", model="model-a", provider="gemini",
        api_key="dummy", judge_provider="mock", arms=("sj",),
    )
    cfg_b = _dc.replace(cfg_a, model="model-b")
    run_dir = cfg_a.runs_dir / "file-sup__sj__t1"
    sup = cfg_a.workdir / "superseded"

    def go(cfg: BenchConfig) -> RunMetrics:
        return run_trial(cfg, "file-sup", "sj", 33, _SolvingProvider())  # type: ignore[arg-type]

    # A#1 -> B supersedes it -> A#2 supersedes B -> B supersedes A#2 under A's fingerprint again.
    go(cfg_a)
    (run_dir / "workspace" / "MARK_A1.txt").write_text("A1", encoding="utf-8")
    go(cfg_b)
    go(cfg_a)
    (run_dir / "workspace" / "MARK_A2.txt").write_text("A2", encoding="utf-8")
    go(cfg_b)
    a1 = list(sup.glob("*/file-sup__sj__t1/workspace/MARK_A1.txt"))
    a2 = list(sup.glob("*/file-sup__sj__t1/workspace/MARK_A2.txt"))
    assert len(a1) == 1 and len(a2) == 1 and a1[0].parent.parent.parent != a2[0].parent.parent.parent
    assert {p.name for p in sup.iterdir()} >= {a1[0].parents[2].name, a2[0].parents[2].name}
    assert all((p / "file-sup__sj__t1_meta" / "result.json").is_file() for p in sup.iterdir())

    # The move cannot start (superseded/ is a file): the trial stops, nothing is deleted.
    (run_dir / "workspace" / "MARK_B2.txt").write_text("B2", encoding="utf-8")
    moved_aside = sup.rename(tmp_path / "superseded_kept")
    sup.write_text("blocking", encoding="utf-8")
    with pytest.raises(loop_mod.SupersedeError):
        go(cfg_a)
    assert (run_dir / "workspace" / "MARK_B2.txt").read_text(encoding="utf-8") == "B2"
    assert json.loads((cfg_a.runs_dir / "file-sup__sj__t1_meta" / "result.json").read_text())["model_id"] == "model-b"
    sup.unlink()
    moved_aside.rename(sup)

    # The second rename fails: the first is undone, both directories stay in runs/.
    real_rename = os.rename
    calls = {"n": 0}

    def flaky_rename(src: Any, dst: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(18, "Invalid cross-device link")
        real_rename(src, dst)

    monkeypatch.setattr(loop_mod.os, "rename", flaky_rename)
    with pytest.raises(loop_mod.SupersedeError):
        go(cfg_a)
    monkeypatch.setattr(loop_mod.os, "rename", real_rename)
    assert (run_dir / "workspace" / "MARK_B2.txt").is_file()
    assert (cfg_a.runs_dir / "file-sup__sj__t1_meta" / "result.json").is_file()
    assert len(list(sup.glob("*/file-sup__sj__t1/workspace/MARK_A1.txt"))) == 1
    assert all(any(d.iterdir()) for d in sup.iterdir()), "a failed move left an empty directory"


def _write_two_arm_results(workdir: Path, missing_arm: str, how: str) -> None:
    """Tasks a and b on raw and sj; task b's ``missing_arm`` has no scorable result, by ``how``."""
    import shutil as _shutil

    def write(task: str, arm: str, **kw: Any) -> Path:
        meta = workdir / "runs" / f"{task}__{arm}__t1_meta"
        meta.mkdir(parents=True, exist_ok=True)
        doc = dict(task_id=task, task_rel=f"file/{task}", category="file", arm=arm, model_id="m",
                   steps=3, termination="present_files", passed=True, capacity_score=1.0,
                   checks_passed=2, checks_total=2)
        doc.update(kw)
        (meta / "result.json").write_text(json.dumps(doc), encoding="utf-8")
        return meta

    for t in ("a", "b"):
        for arm in ("raw", "sj"):
            if (t, arm) == ("b", missing_arm) and how == "not_run":
                continue
            kw: dict[str, Any] = {}
            if (t, arm) == ("b", missing_arm):
                if how == "ungraded":
                    kw = dict(passed=False, capacity_score=0.0, grading_error="grader timeout")
                elif how == "api_error":
                    kw = dict(termination="api_error", passed=False, capacity_score=0.0)
            meta = write(t, arm, **kw)
            if (t, arm) == ("b", missing_arm) and how == "superseded":
                dest = workdir / "superseded" / "0123456789abcdef"
                dest.mkdir(parents=True)
                _shutil.move(str(meta), str(dest / meta.name))


@pytest.mark.parametrize("missing_arm", ["raw", "sj"])
@pytest.mark.parametrize("how", ["ungraded", "api_error", "not_run", "superseded"])
def test_reports_leave_unpaired_tasks_out_and_count_them(tmp_path: Path, missing_arm: str, how: str) -> None:
    from evals.acciobench.html_report import build_html_report_payload

    workdir = tmp_path / "w"
    _write_two_arm_results(workdir, missing_arm, how)
    results = load_run_results(workdir)

    table = render_summary_report(results, format="table")
    assert "Unpaired tasks: 1" in table and "b (no " + missing_arm + ")" in table
    md = render_summary_report(results, format="markdown")
    assert "> Unpaired tasks: 1" in md
    js = json.loads(render_summary_report(results, format="json"))
    assert js["unpaired_tasks"] == 1 and js["unpaired"][0]["missing"] == missing_arm
    assert js["paired"]["paired_tasks"] == 1
    html = render_summary_report(results, format="html", workdir=workdir)
    assert "<html" in html.lower()
    meta = build_html_report_payload(results, workdir=workdir)["meta"]
    assert meta["unpaired_tasks"] == 1 and meta["unpaired"][0]["missing"] == [missing_arm]
    assert meta["total_tasks"] == 1


def test_run_command_survives_a_report_that_fails_to_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ds_root = tmp_path / "cab"
    _create_synthetic_task(ds_root, category="file", task_id="file-rep")
    workdir = tmp_path / "w"
    _write_two_arm_results(workdir, "raw", "ungraded")
    monkeypatch.setattr(ab_cli, "run_suite", lambda *a, **k: [])

    def boom(*a: Any, **k: Any) -> str:
        raise KeyError("raw")

    monkeypatch.setattr(ab_cli, "render_summary_report", boom)
    args = ab_cli.build_parser().parse_args(
        ["run", "--allow-unsandboxed", "--dataset-root", str(ds_root), "--workdir", str(workdir), "--tasks", "file-rep",
         "--provider", "gemini", "--api-key", "dummy", "--judge-provider", "mock"]
    )
    assert ab_cli._cmd_run(args) == 0
    err = capsys.readouterr().err
    assert "the table report failed to render" in err and "the HTML report failed to render" in err
    assert f"report --workdir {workdir} --format html" in err


def test_run_reports_leave_out_an_earlier_result_for_a_trial_that_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A trial that raises writes nothing, so its slot still holds whatever an earlier run
    left there (a previous configuration's result that could not be moved aside). The
    run's reports must not present that as this run's result."""
    ds_root = tmp_path / "cab"
    workdir = tmp_path / "w"
    for task in ("file-a", "file-b"):
        _create_synthetic_task(ds_root, category="file", task_id=task)
        for arm in ("raw", "sj"):
            meta = workdir / "runs" / f"{task}__{arm}__t1_meta"
            meta.mkdir(parents=True)
            (meta / "result.json").write_text(json.dumps(dict(
                task_id=task, task_rel=f"file/{task}", category="file", arm=arm, model_id="earlier-model",
                steps=3, termination="present_files", passed=True, capacity_score=1.0,
                checks_passed=2, checks_total=2)), encoding="utf-8")

    def fake_suite(config: Any, task_ids: list[str], *, raised: list[RunMetrics], **_: Any) -> list[RunMetrics]:
        on_disk = [r for r in load_run_results(config.runs_dir) if (r.task_id, r.arm) != ("file-b", "sj")]
        stand_in = RunMetrics(task_id="file-b", task_rel="file-b", category="file", arm="sj",
                              model_id=config.model, termination="error",
                              notes=["file-b__sj__t1: could not move the previous configuration's trial aside"])
        raised.append(stand_in)
        return [*on_disk, stand_in]

    monkeypatch.setattr(ab_cli, "run_suite", fake_suite)
    out_json = tmp_path / "report.json"
    args = ab_cli.build_parser().parse_args(
        ["run", "--allow-unsandboxed", "--dataset-root", str(ds_root), "--workdir", str(workdir),
         "--tasks", "file-a,file-b",
         "--arms", "raw,sj", "--provider", "gemini", "--api-key", "dummy", "--judge-provider", "mock",
         "--format", "json", "--output", str(out_json), "--no-html-report"]
    )
    assert ab_cli._cmd_run(args) == 0
    err = capsys.readouterr().err
    assert "[not recorded] file-b (sj)" in err and "could not move" in err
    doc = json.loads(out_json.read_text(encoding="utf-8"))
    assert doc["unpaired_tasks"] == 1
    assert doc["unpaired"][0]["task_id"] == "file-b" and doc["unpaired"][0]["missing"] == "sj"
    assert doc["paired"]["paired_tasks"] == 1
    # The earlier result itself is untouched on disk.
    assert (workdir / "runs" / "file-b__sj__t1_meta" / "result.json").is_file()


def test_run_reports_show_no_completed_run_when_every_trial_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With nothing usable on disk the run falls back to its in-memory results; the
    stand-ins for trials that raised are not results and must not come back that way."""
    ds_root = tmp_path / "cab"
    workdir = tmp_path / "w"
    for task in ("file-a", "file-b"):
        _create_synthetic_task(ds_root, category="file", task_id=task)
        meta = workdir / "runs" / f"{task}__sj__t1_meta"
        meta.mkdir(parents=True)
        (meta / "result.json").write_text(json.dumps(dict(
            task_id=task, task_rel=f"file/{task}", category="file", arm="sj", model_id="earlier-model",
            steps=3, termination="present_files", passed=True, capacity_score=1.0,
            checks_passed=2, checks_total=2)), encoding="utf-8")

    def all_raise(config: Any, task_ids: list[str], *, raised: list[RunMetrics], **_: Any) -> list[RunMetrics]:
        for task in task_ids:
            raised.append(RunMetrics(task_id=task, task_rel=task, category="file", arm="sj",
                                     model_id=config.model, termination="error", notes=["could not move"]))
        return list(raised)

    monkeypatch.setattr(ab_cli, "run_suite", all_raise)
    out_json = tmp_path / "report.json"
    args = ab_cli.build_parser().parse_args(
        ["run", "--allow-unsandboxed", "--dataset-root", str(ds_root), "--workdir", str(workdir),
         "--tasks", "file-a,file-b",
         "--provider", "gemini", "--api-key", "dummy", "--judge-provider", "mock",
         "--format", "json", "--output", str(out_json), "--no-html-report"]
    )
    assert ab_cli._cmd_run(args) == 0
    assert capsys.readouterr().err.count("[not recorded]") == 2
    doc = json.loads(out_json.read_text(encoding="utf-8"))
    assert doc["total_runs"] == 0 and doc["arms"] == {}


def test_ctx_code_identity_hashes_the_launched_ctx_code_not_its_version_or_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An edit to ctx that keeps its version changes the identity; moving the checkout or
    a new __pycache__ does not; nothing is imported from the caller's directory; a ctx
    that is not a Python entry point is identified by its own bytes."""
    from evals.acciobench import config as cfg_mod

    def fresh(ctx_bin: Path) -> str:
        cfg_mod._CODE_ID_CACHE.clear()  # one computation per process: start a "new process"
        return cfg_mod.ctx_code_identity(BenchConfig(dataset_root=tmp_path / "cab", workdir=tmp_path / "w", ctx_bin=ctx_bin))

    def fake_ctx(root: Path, body: str = "VALUE = 1\n") -> Path:
        pkg = root / "ctx"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("__version__ = '1.0'\n", encoding="utf-8")
        (pkg / "cli.py").write_text(body + "def main():\n    return 0\n", encoding="utf-8")
        return pkg

    pkg = fake_ctx(tmp_path / "checkout_a")
    monkeypatch.setenv("PYTHONPATH", str(pkg.parent))
    entry = _write_executable(
        tmp_path / "bin" / "ctx",
        f"#!{sys.executable}\nimport sys\nfrom ctx.cli import main\nif __name__ == '__main__':\n    sys.exit(main())\n",
    )
    first = fresh(entry)
    assert first.startswith("pkg:")
    assert fresh(entry) == first
    # A planted ctx/ in the current directory is never imported by the probe.
    planted = tmp_path / "rundir"
    marker = tmp_path / "planted_ran"
    (planted / "ctx").mkdir(parents=True)
    (planted / "ctx" / "__init__.py").write_text(f"open({str(marker)!r}, 'w').close()\n", encoding="utf-8")
    monkeypatch.chdir(planted)
    assert fresh(entry) == first and not marker.exists()
    # Byte-code caches are not code.
    (pkg / "__pycache__").mkdir()
    (pkg / "__pycache__" / "cli.cpython-311.pyc").write_bytes(b"\0" * 32)
    assert fresh(entry) == first
    # Same content elsewhere (a moved checkout): same identity.
    moved = fake_ctx(tmp_path / "checkout_b")
    monkeypatch.setenv("PYTHONPATH", str(moved.parent))
    assert fresh(entry) == first
    # An edit that keeps the version: a different identity.
    (moved / "cli.py").write_text("VALUE = 22\ndef main():\n    return 0\n", encoding="utf-8")
    edited = fresh(entry)
    assert edited.startswith("pkg:") and edited != first
    # A Python interpreter launched as `-P -m ctx` resolves the same package.
    assert fresh(Path(sys.executable)) == edited
    # Not a Python entry point (a shell wrapper): its own bytes, stably.
    wrapper = _write_executable(tmp_path / "wrap" / "ctx", "#!/usr/bin/env bash\nexec ctx-real \"$@\"\n")
    w1 = fresh(wrapper)
    assert w1.startswith("exe:") and fresh(wrapper) == w1
    wrapper.write_text("#!/usr/bin/env bash\nexec ctx-real --v2 \"$@\"\n", encoding="utf-8")
    assert fresh(wrapper) not in (w1, edited)

    # The ctx package this process imports: hashed by content, once per process.
    monkeypatch.setitem(sys.modules, "ctx", types.SimpleNamespace(__file__=str(moved / "__init__.py")))
    cfg_mod._CODE_ID_CACHE.clear()
    assert cfg_mod.harness_ctx_code_identity() == edited


def _venv_with_fake_ctx(root: Path, marker: str | None) -> tuple[Path, Path | None]:
    """A virtualenv (no pip) whose site-packages holds a fake ``ctx`` printing ``marker``
    (no ctx at all when ``marker`` is None). Returns (venv bin dir, package dir)."""
    import venv

    venv.create(root, with_pip=False, symlinks=True, clear=True)
    bin_dir = root / "bin"
    if marker is None:
        return bin_dir, None
    site = subprocess.run(
        [str(bin_dir / "python"), "-I", "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
        capture_output=True, text=True, timeout=60, check=True,
    ).stdout.strip()
    pkg = Path(site) / "ctx"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "cli.py").write_text(f"def main():\n    print({marker!r})\n    return 0\n", encoding="utf-8")
    (pkg / "__main__.py").write_text("import sys\nfrom ctx.cli import main\nsys.exit(main())\n", encoding="utf-8")
    return bin_dir, pkg


def test_ctx_code_identity_sees_the_package_the_trial_really_launches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``#!/usr/bin/env python3`` resolves on the trial's PATH (the ctx's own directory
    first), not the harness's; a venv's python given as --ctx-bin launches that venv's
    ctx; a Python launcher with no visible ctx package is ``unavailable``, never a hash
    of the interpreter's bytes."""
    from evals.acciobench import config as cfg_mod

    monkeypatch.delenv("PYTHONPATH", raising=False)
    venv_bin, pkg = _venv_with_fake_ctx(tmp_path / "venv_a", "fake-ctx-a")
    assert pkg is not None
    entry = _write_executable(
        venv_bin / "ctx", "#!/usr/bin/env python3\nimport sys\nfrom ctx.cli import main\nsys.exit(main())\n"
    )
    # The harness's own python3 is another interpreter, which has no such package.
    assert shutil.which("python3") != str(venv_bin / "python3")

    def identity(ctx_bin: Path) -> str:
        cfg_mod._CODE_ID_CACHE.clear()
        return cfg_mod.ctx_code_identity(BenchConfig(dataset_root=tmp_path / "cab", workdir=tmp_path / "w", ctx_bin=ctx_bin))

    want = "pkg:" + cfg_mod._code_digest(pkg)
    assert identity(entry) == want
    (pkg / "cli.py").write_text("def main():\n    print('fake-ctx-a2')\n    return 0\n", encoding="utf-8")
    edited = "pkg:" + cfg_mod._code_digest(pkg)
    assert edited != want and identity(entry) == edited

    # --ctx-bin <venv>/bin/python: not symlink-resolved, so the venv's ctx is what runs.
    vpy = venv_bin / "python"
    assert vpy.is_symlink()
    cfg = BenchConfig(dataset_root=tmp_path / "cab", workdir=tmp_path / "w", ctx_bin=vpy)
    assert cfg.ctx_bin == vpy and resolve_ctx_bin(cfg.ctx_bin) == vpy
    assert identity(vpy) == edited
    run_dir, meta_dir = tmp_path / "w" / "runs" / "x", tmp_path / "w" / "runs" / "x_meta"
    run_dir.mkdir(parents=True)
    meta_dir.mkdir(parents=True)
    host = ToolHost(cfg, run_dir, meta_dir, {}, dict(os.environ),
                    RunMetrics(task_id="t", task_rel="t", category="file", arm="sj", model_id="m"), None, arm="sj")
    cp = host.ctx(["--version"])
    assert "fake-ctx-a2" in (cp.stdout or ""), (cp.stdout, cp.stderr)

    # A Python launcher whose interpreter has no ctx: unavailable, with one warning.
    bare_bin, _ = _venv_with_fake_ctx(tmp_path / "venv_bare", None)
    bare_entry = _write_executable(
        tmp_path / "bare_entry" / "ctx", f"#!{bare_bin / 'python'}\nfrom ctx.cli import main\nmain()\n"
    )
    capsys.readouterr()
    cfg_mod._WARNED_NO_PKG.clear()
    for target in (bare_entry, bare_bin / "python"):
        got = identity(target)
        assert got.startswith("unavailable (") and "no ctx package" in got, got
        assert identity(target) == got
    err = capsys.readouterr().err
    assert err.count("[warn] ctx code identity") == 2, err


def test_resume_reruns_sj_when_ctx_code_changes_and_raw_does_not(tmp_path: Path) -> None:
    import evals.acciobench.loop as loop_mod

    ds_root = tmp_path / "cab"
    _create_synthetic_task(ds_root, category="file", task_id="file-ctxcode")
    cfg = BenchConfig(allow_unsandboxed=True,  # explicit opt-in: CI has no bwrap
                      dataset_root=ds_root, workdir=tmp_path / "w", provider="gemini", api_key="d",
                      judge_provider="mock", arms=("raw", "sj"))

    def reruns(arm: str) -> bool:
        prov = _SolvingProvider()
        run_trial(cfg, "file-ctxcode", arm, 41, prov)  # type: ignore[arg-type]
        return prov.calls > 0

    real = (loop_mod.ctx_code_identity, loop_mod.harness_ctx_code_identity)
    try:
        loop_mod.ctx_code_identity = lambda _bin=None: "pkg:aaaa"  # type: ignore[assignment]
        loop_mod.harness_ctx_code_identity = lambda: "pkg:bbbb"  # type: ignore[assignment]
        assert reruns("raw") and reruns("sj")
        assert not reruns("raw") and not reruns("sj")
        loop_mod.ctx_code_identity = lambda _bin=None: "pkg:cccc"  # type: ignore[assignment]
        assert reruns("sj") and not reruns("raw")
        loop_mod.harness_ctx_code_identity = lambda: "pkg:dddd"  # type: ignore[assignment]
        assert reruns("sj") and not reruns("raw")
        assert not reruns("sj")
    finally:
        loop_mod.ctx_code_identity, loop_mod.harness_ctx_code_identity = real  # type: ignore[assignment]


def test_task_inputs_digest_follows_directory_links_like_staging_does(tmp_path: Path) -> None:
    """Staging copies through a directory symlink, so a change beneath one changes what a
    trial receives and must change the fingerprint; a link back to an ancestor must end."""
    from evals.acciobench import config as cfg_mod

    shared = tmp_path / "shared_inputs"
    shared.mkdir()
    (shared / "prices.csv").write_text("sku,usd\nA,1\n", encoding="utf-8")
    case = tmp_path / "case"
    (case / "workspace").mkdir(parents=True)
    (case / "task.md").write_text("prompt", encoding="utf-8")
    without_link = cfg_mod.task_inputs_digest(case)

    (case / "workspace" / "data").symlink_to(shared, target_is_directory=True)
    linked = cfg_mod.task_inputs_digest(case)
    assert linked != without_link

    (shared / "prices.csv").write_text("sku,usd\nA,2\n", encoding="utf-8")
    edited = cfg_mod.task_inputs_digest(case)
    assert edited != linked, "a file beneath a linked input directory changed"

    other = tmp_path / "other_inputs"
    other.mkdir()
    (other / "prices.csv").write_text("sku,usd\nA,2\n", encoding="utf-8")
    (case / "workspace" / "data").unlink()
    (case / "workspace" / "data").symlink_to(other, target_is_directory=True)
    assert cfg_mod.task_inputs_digest(case) == edited, "same content through another target"
    (other / "extra.csv").write_text("x", encoding="utf-8")
    assert cfg_mod.task_inputs_digest(case) != edited

    # Links that lead back up (a cycle) or to an already hashed directory end the walk
    # there, and adding one is itself a change.
    before_cycle = cfg_mod.task_inputs_digest(case)
    (case / "workspace" / "loop").symlink_to(case, target_is_directory=True)
    (case / "workspace" / "again").symlink_to(other, target_is_directory=True)
    with_cycle = cfg_mod.task_inputs_digest(case)
    assert with_cycle != before_cycle and with_cycle == cfg_mod.task_inputs_digest(case)


def test_sandboxes_take_turns_on_a_host_port(tmp_path: Path) -> None:
    import socket
    import threading

    from evals.acciobench import sandbox as sb

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    assert port not in sb._HELD_PORTS

    # 1. Two claims of the same port: the second returns only after the first is released.
    sb._claim_saved_ports({port}, "first")
    order: list[str] = []
    got_it = threading.Event()

    def second() -> None:
        sb._claim_saved_ports({port}, "second", wait_s=20)
        order.append("second-claimed")
        got_it.set()

    th = threading.Thread(target=second)
    th.start()
    assert not got_it.wait(0.4), "the second claim did not wait for the first"
    order.append("first-released")
    sb._release_ports({port})
    assert got_it.wait(10)
    th.join(5)
    assert order == ["first-released", "second-claimed"]

    # 2. A new trial never allocates a port another sandbox holds, listening or not.
    mapping, held = sb._claim_fresh_ports([8080], port, has_notion=False)
    assert mapping[8080] != port and held == {mapping[8080]}
    sb._release_ports(held)

    # 3. A claim that cannot be had in time raises and holds nothing more.
    with pytest.raises(RuntimeError, match="held by another trial"):
        sb._claim_saved_ports({port}, "third", wait_s=0.3)
    sb._release_ports({port})
    assert port not in sb._HELD_PORTS

    # 4. A listener outside this run: the claim raises instead of grading against it.
    with socket.socket() as foreign:
        foreign.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        foreign.bind(("127.0.0.1", port))
        foreign.listen(1)
        with pytest.raises(RuntimeError, match="in use outside this run"):
            sb._claim_saved_ports({port}, "fourth", grace_s=0.4)
    assert port not in sb._HELD_PORTS


def test_concurrent_regrades_with_the_same_saved_port_each_get_their_own_service(tmp_path: Path) -> None:
    """Trials that ran one after another in one worker slot recorded the same host port.
    Re-grading them at the same time must not let the second one's readiness probe be
    answered by the first one's service: each waits for the port and starts its own."""
    import socket
    import threading

    ds_root = tmp_path / "cab"
    runtime_dir = tmp_path / "runtime"
    (runtime_dir / "mock_services").mkdir(parents=True)
    cfg = BenchConfig(dataset_root=ds_root, workdir=tmp_path / "work", runtime_dir=runtime_dir, judge_provider="mock")

    def listening(port: int) -> bool:
        with socket.socket() as s:
            s.settimeout(0.5)
            return s.connect_ex(("127.0.0.1", port)) == 0

    trials = []
    for name in ("api-turn-a", "api-turn-b"):
        _create_synthetic_task(ds_root, category="api", task_id=name, mock_services=True, service_port=4477)
        spec = load_cab_task(ds_root, name)
        run_dir, meta_dir = cfg.runs_dir / f"{name}__sj__t1", cfg.runs_dir / f"{name}__sj__t1_meta"
        first = TaskSandbox(cfg, spec, run_dir, meta_dir, slot=86)  # both in the same slot, in turn
        try:
            first.setup()
            port = first.port_map[4477]
        finally:
            first.cleanup()
        trials.append((spec, run_dir, meta_dir, port))
    assert trials[0][3] == trials[1][3], "sequential trials in one slot share the port"
    port = trials[0][3]
    for _ in range(50):
        if not listening(port):
            break
        time.sleep(0.05)

    a = TaskSandbox(cfg, trials[0][0], trials[0][1], trials[0][2], slot=87)
    b = TaskSandbox(cfg, trials[1][0], trials[1][1], trials[1][2], slot=88)
    b_ready = threading.Event()
    errors: list[BaseException] = []

    def regrade_b() -> None:
        try:
            b.prepare_regrade()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            b_ready.set()

    try:
        a.prepare_regrade()
        assert listening(port)
        th = threading.Thread(target=regrade_b)
        th.start()
        assert not b_ready.wait(1.0), "the second re-grade did not wait for the first one's port"
        a.cleanup()
        assert b_ready.wait(30)
        th.join(5)
        assert not errors, errors
        assert listening(port), "the second re-grade has no service of its own"
    finally:
        a.cleanup()
        b.cleanup()
    for _ in range(50):
        if not listening(port):
            break
        time.sleep(0.05)
    assert not listening(port)


def test_task_inputs_digest_never_blocks_or_raises_on_odd_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evals.acciobench import config as cfg_mod

    case = tmp_path / "case"
    case.mkdir()
    (case / "task.md").write_text("prompt", encoding="utf-8")
    base = cfg_mod.task_inputs_digest(case)

    os.mkfifo(case / "pipe")  # opening this for reading would block forever
    (case / "dangling").symlink_to(case / "nowhere")
    with_odd = cfg_mod.task_inputs_digest(case)
    assert with_odd != base and with_odd == cfg_mod.task_inputs_digest(case)

    secret = case / "private.txt"
    secret.write_text("x", encoding="utf-8")
    readable = cfg_mod.task_inputs_digest(case)
    real_open = Path.open

    def deny(self: Path, *a: Any, **k: Any) -> Any:
        if self.name == "private.txt":
            raise PermissionError(13, "Permission denied")
        return real_open(self, *a, **k)

    cfg_mod._FILE_DIGEST_CACHE.clear()
    monkeypatch.setattr(Path, "open", deny)
    unreadable = cfg_mod.task_inputs_digest(case)
    assert unreadable != readable and unreadable == cfg_mod.task_inputs_digest(case)


@pytest.mark.parametrize(
    "bad_reward",
    ["[]", '{"score": "abc"}', "{}", '{"checks_passed": 3, "checks_total": 2}', '{"score": true}',
     '{"score": 1.0, "checks_breakdown": "x"}', '"passed"', "null", '{"score": NaN}',
     '{"score": 5}', '{"score": 85, "checks_passed": 3, "checks_total": 4}', '{"score": 1.001}'],
)
def test_uninterpretable_reward_is_a_grading_error_and_is_regraded(tmp_path: Path, bad_reward: str) -> None:
    ds_root = tmp_path / "cab"
    flag = tmp_path / "grader_fixed"
    case_dir = _create_synthetic_task(ds_root, category="file", task_id="file-badreward")
    grader = case_dir / "grader"
    (grader / "run.sh").rename(grader / "real.sh")
    (grader / "bad.json").write_text(bad_reward, encoding="utf-8")
    _write_executable(
        grader / "run.sh",
        "#!/usr/bin/env bash\n"
        f'if [ ! -e "{flag}" ]; then cp "$(dirname "$0")/bad.json" "$REWARD_JSON"; exit 0; fi\n'
        'exec bash "$(dirname "$0")/real.sh"\n',
    )
    cfg = BenchConfig(allow_unsandboxed=True,  # explicit opt-in: CI has no bwrap
                      dataset_root=ds_root, workdir=tmp_path / "w", provider="gemini", api_key="d",
                      judge_provider="mock", arms=("sj",))
    first = run_trial(cfg, "file-badreward", "sj", 21, _SolvingProvider())  # type: ignore[arg-type]
    assert first.termination == "present_files"
    assert first.grading_error and first.passed is False, first.notes
    flag.touch()
    second = run_trial(cfg, "file-badreward", "sj", 21, _MustNotGenerateProvider())  # type: ignore[arg-type]
    assert second.grading_error == "" and second.passed is True


def test_a_reused_result_without_a_sandbox_field_is_recorded_or_reported_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cached result written before ``sandbox`` existed: with a matching fingerprint it
    ran in the current mode, so that mode is recorded on reuse; a legacy result accepted on
    model_id alone stays unknown and reports count it as such."""
    from evals.acciobench.report import compute_summary_payload

    monkeypatch.setenv("ACCIOBENCH_NO_BWRAP", "1")
    ds_root = tmp_path / "cab"
    _create_synthetic_task(ds_root, category="file", task_id="file-oldbox")
    cfg = BenchConfig(dataset_root=ds_root, workdir=tmp_path / "w", provider="gemini", api_key="d",
                      judge_provider="mock", arms=("raw",), allow_unsandboxed=True)
    result_file = cfg.runs_dir / "file-oldbox__raw__t1_meta" / "result.json"
    assert run_trial(cfg, "file-oldbox", "raw", 51, _SolvingProvider()).sandbox == "none"  # type: ignore[arg-type]

    doc = json.loads(result_file.read_text(encoding="utf-8"))
    doc.pop("sandbox")
    result_file.write_text(json.dumps(doc), encoding="utf-8")
    assert load_run_results(cfg.workdir)[0].sandbox == ""
    reused = run_trial(cfg, "file-oldbox", "raw", 51, _MustNotGenerateProvider())  # type: ignore[arg-type]
    assert reused.sandbox == "none"
    assert json.loads(result_file.read_text(encoding="utf-8"))["sandbox"] == "none"
    assert compute_summary_payload(load_run_results(cfg.workdir))["unsandboxed_runs"] == 1

    doc = json.loads(result_file.read_text(encoding="utf-8"))
    doc.pop("sandbox")
    doc.pop("config_fingerprint")
    result_file.write_text(json.dumps(doc), encoding="utf-8")
    legacy = run_trial(cfg, "file-oldbox", "raw", 51, _MustNotGenerateProvider())  # type: ignore[arg-type]
    assert legacy.sandbox == ""
    payload = compute_summary_payload(load_run_results(cfg.workdir))
    assert payload["unsandboxed_runs"] == 0 and payload["isolation_unknown_runs"] == 1


def test_a_stored_score_above_one_loads_as_a_grading_error_and_is_regraded(tmp_path: Path) -> None:
    """A result graded before scores above 1 were refused (old shape: no grading_error
    key) loads as ungraded: reports leave it out and resume re-grades it."""
    from evals.acciobench.report import compute_summary_payload

    ds_root = tmp_path / "cab"
    _create_synthetic_task(ds_root, category="file", task_id="file-oldscore")
    cfg = BenchConfig(allow_unsandboxed=True,  # explicit opt-in: CI has no bwrap
                      dataset_root=ds_root, workdir=tmp_path / "w", provider="gemini", api_key="d",
                      judge_provider="mock", arms=("raw",))
    assert run_trial(cfg, "file-oldscore", "raw", 52, _SolvingProvider()).passed  # type: ignore[arg-type]
    result_file = cfg.runs_dir / "file-oldscore__raw__t1_meta" / "result.json"
    doc = json.loads(result_file.read_text(encoding="utf-8"))
    for key in ("grading_error", "sandbox"):
        doc.pop(key)
    doc.update(passed=True, capacity_score=5.0, raw_verifier_score=5.0, checks_passed=0, checks_total=0)
    result_file.write_text(json.dumps(doc), encoding="utf-8")

    (loaded,) = load_run_results(cfg.workdir)
    assert loaded.passed is False and "above the 0..1 score scale" in loaded.grading_error
    payload = compute_summary_payload([loaded])
    assert payload["grading_error_runs"] == 1 and payload["arms"]["raw"]["overall"]["n"] == 0
    for ok in ({"capacity_score": 1.0, "raw_verifier_score": 1.0 + 1e-9}, {"capacity_score": 0.5}):
        assert RunMetrics.from_dict({"task_id": "t", "arm": "raw", "model_id": "m", **ok}).grading_error == ""

    regraded = run_trial(cfg, "file-oldscore", "raw", 52, _MustNotGenerateProvider())  # type: ignore[arg-type]
    assert regraded.grading_error == "" and regraded.passed and regraded.capacity_score == 1.0
    assert json.loads(result_file.read_text(encoding="utf-8"))["raw_verifier_score"] == 1.0


def test_reward_score_above_one_is_a_grading_error_not_a_pass(tmp_path: Path) -> None:
    """Scores are on a 0..1 scale: anything above 1 (beyond float noise) is not
    interpretable, with or without checks; float noise just above 1 is a full score."""
    from evals.acciobench.loop import _grade_into

    class _Sandbox:
        def __init__(self, reward: Any) -> None:
            self.reward = reward

        def grade(self, reward_json: Path, *, judge_token: str = "") -> Any:
            return self.reward

    cfg = BenchConfig(dataset_root=tmp_path / "cab", workdir=tmp_path / "w", judge_provider="mock")

    def grade(reward: Any) -> RunMetrics:
        m = RunMetrics(task_id="t", task_rel="t", category="file", arm="sj", model_id="m")
        _grade_into(cfg, _SolvingProvider(), _Sandbox(reward), tmp_path, m)  # type: ignore[arg-type]
        return m

    for bad in ({"score": 5}, {"score": 85, "checks_passed": 3, "checks_total": 4}, {"score": 1.5}):
        m = grade(bad)
        assert m.grading_error and "above 1" in m.grading_error, bad
        assert m.passed is False and m.capacity_score == 0.0 and m.raw_verifier_score == 0.0
    for ok, passed, cap in (({"score": 1.0 + 1e-9}, True, 1.0), ({"score": 0.5}, False, 0.5),
                            ({"score": 1.0, "checks_passed": 3, "checks_total": 4}, True, 0.75)):
        m = grade(ok)
        assert m.grading_error == "" and m.passed is passed and m.capacity_score == cap, ok
        assert m.raw_verifier_score <= 1.0


def test_legacy_verifier_error_note_loads_as_grading_error() -> None:
    legacy = {"task_id": "t", "arm": "sj", "model_id": "m", "termination": "present_files",
              "passed": False, "notes": ["verifier error: Traceback ... TimeoutError"]}
    m = RunMetrics.from_dict(legacy)
    assert m.grading_error.startswith("Traceback")
    ok = RunMetrics.from_dict({"task_id": "t", "arm": "sj", "model_id": "m", "passed": True})
    assert ok.grading_error == ""


def test_scored_run_refuses_to_start_without_the_bwrap_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No bwrap and no explicit override: nothing is staged, graded or sent to a model,
    whoever calls (CLI, run_suite, run_trial), and ACCIOBENCH_NO_BWRAP alone is no override."""
    import evals.acciobench.orchestrator as orch_mod
    from evals.acciobench.loop import UnsandboxedRunRefused

    monkeypatch.setenv("ACCIOBENCH_NO_BWRAP", "1")
    ds_root = tmp_path / "cab"
    _create_synthetic_task(ds_root, category="file", task_id="file-nobwrap")
    cfg = BenchConfig(dataset_root=ds_root, workdir=tmp_path / "w", provider="gemini", api_key="d",
                      judge_provider="mock", arms=("raw", "sj"))
    assert cfg.allow_unsandboxed is False

    with pytest.raises(UnsandboxedRunRefused, match="ACCIOBENCH_NO_BWRAP is set"):
        run_trial(cfg, "file-nobwrap", "raw", 31, _MustNotGenerateProvider())  # type: ignore[arg-type]
    monkeypatch.setattr(orch_mod, "create_provider", lambda *_a: pytest.fail("provider created"))
    with pytest.raises(UnsandboxedRunRefused):
        run_suite(cfg, ["file-nobwrap"])
    assert not cfg.workdir.exists()

    monkeypatch.setattr(ab_cli, "run_suite", lambda *a, **k: pytest.fail("run_suite called"))
    args = ab_cli.build_parser().parse_args(
        ["run", "--dataset-root", str(ds_root), "--workdir", str(cfg.workdir), "--tasks", "file-nobwrap",
         "--provider", "gemini", "--api-key", "dummy", "--judge-provider", "mock"]
    )
    assert ab_cli._cmd_run(args) != 0
    err = capsys.readouterr().err
    assert "Refusing a scored run" in err and "ACCIOBENCH_NO_BWRAP is set" in err
    assert "apt-get install -y bubblewrap" in err and "--allow-unsandboxed" in err
    assert not cfg.workdir.exists()

    # doctor still runs, and says a scored run would be refused.
    _ok, lines = run_doctor(cfg)
    assert any("bwrap sandbox: unavailable" in ln and "REFUSED" in ln for ln in lines)


def test_allow_unsandboxed_runs_warns_and_every_report_counts_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import evals.acciobench.orchestrator as orch_mod
    from evals.acciobench.html_report import build_html_report_payload
    from evals.acciobench.report import compute_summary_payload

    ds_root = tmp_path / "cab"
    _create_synthetic_task(ds_root, category="file", task_id="file-unsandboxed")
    cfg = BenchConfig(dataset_root=ds_root, workdir=tmp_path / "w", provider="gemini", api_key="d",
                      judge_provider="mock", arms=("sj",), allow_unsandboxed=True)
    monkeypatch.setenv("ACCIOBENCH_NO_BWRAP", "1")
    monkeypatch.setattr(orch_mod, "create_provider", lambda *_a: _SolvingProvider())
    capsys.readouterr()
    (res,) = run_suite(cfg, ["file-unsandboxed"])
    assert "WARNING: --allow-unsandboxed" in capsys.readouterr().err
    assert res.sandbox == "none" and res.passed
    saved = json.loads((cfg.runs_dir / "file-unsandboxed__sj__t1_meta" / "result.json").read_text(encoding="utf-8"))
    assert saved["sandbox"] == "none"

    # One sandboxed result next to it, and one written before sandboxing was recorded.
    for arm, extra in (("raw", {"sandbox": "bwrap"}), ("raw", {})):
        meta = cfg.runs_dir / f"file-old-{len(extra)}__{arm}__t1_meta"
        meta.mkdir(parents=True)
        (meta / "result.json").write_text(json.dumps(dict(
            task_id=f"file-old-{len(extra)}", task_rel="file/x", category="file", arm=arm, model_id="m",
            steps=3, termination="present_files", passed=True, capacity_score=1.0, **extra)), encoding="utf-8")
    loaded = load_run_results(cfg.workdir)
    assert sorted(r.sandbox for r in loaded) == ["", "bwrap", "none"]
    payload = compute_summary_payload(loaded)
    assert payload["unsandboxed_runs"] == 1 and payload["arms"]["sj"]["unsandboxed_runs"] == 1
    assert payload["arms"]["raw"]["unsandboxed_runs"] == 0
    for fmt in ("table", "markdown"):
        assert "NO FILESYSTEM ISOLATION: 1 trial(s)" in render_summary_report(loaded, format=fmt), fmt
    assert json.loads(render_summary_report(loaded, format="json"))["unsandboxed_runs"] == 1
    html_meta = build_html_report_payload(loaded, workdir=cfg.workdir)["meta"]
    assert html_meta["unsandboxed_total"] == 1 and html_meta["unsandboxed"] == {"sj": 1, "raw": 0}
    assert 'id="unsandboxed-runs"' in render_summary_report(loaded, format="html", workdir=cfg.workdir)
    # The result written before sandboxing was recorded is counted as unknown, everywhere.
    assert payload["isolation_unknown_runs"] == 1 and payload["arms"]["raw"]["isolation_unknown_runs"] == 1
    for fmt in ("table", "markdown"):
        assert "ISOLATION UNKNOWN: 1 trial(s)" in render_summary_report(loaded, format=fmt), fmt
    assert json.loads(render_summary_report(loaded, format="json"))["isolation_unknown_runs"] == 1
    assert html_meta["isolation_unknown_total"] == 1 and html_meta["isolation_unknown"] == {"sj": 0, "raw": 1}
    assert 'id="isolation-unknown-runs"' in render_summary_report(loaded, format="html", workdir=cfg.workdir)
    # Sandboxed results alone: no warning, and the JSON still states both counts (0).
    sandboxed = [r for r in loaded if r.sandbox == "bwrap"]
    for fmt in ("table", "markdown"):
        assert "ISOLATION" not in render_summary_report(sandboxed, format=fmt), fmt
    doc = json.loads(render_summary_report(sandboxed, format="json"))
    assert doc["unsandboxed_runs"] == 0 and doc["isolation_unknown_runs"] == 0

    # With the real sandbox available, the same trial records "bwrap".
    from evals.acciobench.host import bwrap_sandbox_executable

    monkeypatch.delenv("ACCIOBENCH_NO_BWRAP")
    if bwrap_sandbox_executable() is not None:
        import dataclasses as _dc

        again = run_trial(_dc.replace(cfg, allow_unsandboxed=False), "file-unsandboxed", "sj", 32,
                          _SolvingProvider())  # type: ignore[arg-type]
        assert again.sandbox == "bwrap" and again.passed


# ---------------------------------------------------------------------------
# 7. Report, Doctor, Referee Validator & CLI Entrypoints
# ---------------------------------------------------------------------------


def test_report_load_repeat_collapse_and_paired_formats(tmp_path: Path) -> None:
    runs_dir = tmp_path / "workdir" / "runs"

    def _write_result(folder: str, metric: RunMetrics) -> None:
        p = runs_dir / folder / "result.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(metric.to_dict(), indent=2), encoding="utf-8")

    # Task 1: 3 repeats for sj (2 pass, 1 fail -> majority pass) paired against raw.
    _write_result(
        "api-orders__raw_meta",
        RunMetrics(
            task_id="api-orders",
            task_rel="api/api-orders",
            category="api",
            arm="raw",
            model_id="gemini-2.5-flash",
            bucket="worse",
            steps=20,
            prompt_tokens=50_000,
            cached_tokens=20_000,
            thought_tokens=2_000,
            output_tokens=1_000,
            wall_clock_s=40.0,
            passed=True,
            capacity_score=1.0,
        ),
    )
    for idx, (passed, steps, ptok) in enumerate([(True, 12, 24_000), (True, 14, 26_000), (False, 18, 30_000)], start=1):
        _write_result(
            f"api-orders__sj__r{idx}_meta",
            RunMetrics(
                task_id="api-orders",
                task_rel="api/api-orders",
                category="api",
                arm="sj",
                model_id="gemini-2.5-flash",
                bucket="worse",
                steps=steps,
                prompt_tokens=ptok,
                cached_tokens=15_000,
                thought_tokens=1_200,
                output_tokens=600,
                wall_clock_s=25.0,
                digests_delivered=3,
                bytes_kept_out=48_000,
                passed=passed,
                capacity_score=1.0 if passed else 0.0,
            ),
        )

    # Task 2: paired file task.
    _write_result(
        "file-budget__raw_meta",
        RunMetrics(
            task_id="file-budget",
            task_rel="file/file-budget",
            category="file",
            arm="raw",
            model_id="gemini-2.5-flash",
            bucket="better",
            steps=10,
            prompt_tokens=15_000,
            thought_tokens=800,
            output_tokens=400,
            wall_clock_s=15.0,
            passed=True,
            capacity_score=1.0,
        ),
    )
    _write_result(
        "file-budget__sj_meta",
        RunMetrics(
            task_id="file-budget",
            task_rel="file/file-budget",
            category="file",
            arm="sj",
            model_id="gemini-2.5-flash",
            bucket="better",
            steps=8,
            prompt_tokens=11_000,
            thought_tokens=600,
            output_tokens=300,
            wall_clock_s=12.0,
            passed=True,
            capacity_score=1.0,
        ),
    )

    loaded_all = load_run_results(tmp_path / "workdir")
    assert len(loaded_all) == 6
    loaded_sj = load_run_results(tmp_path / "workdir", arm="sj")
    assert len(loaded_sj) == 4

    json_txt = render_summary_report(loaded_all, format="json")
    parsed = json.loads(json_txt)
    assert set(parsed["arms"]) == {"raw", "sj"}
    assert parsed["paired"] is not None
    assert parsed["paired"]["overall"]["n"] == 2
    assert parsed["paired"]["overall"]["evidence_preservation"] == pytest.approx(1.0)
    assert "api" in parsed["paired"]["by_category"]
    assert "worse" in parsed["paired"]["by_bucket"]

    table_txt = render_summary_report(loaded_all, format="table")
    md_txt = render_summary_report(loaded_all, format="markdown")
    assert "Paired" in table_txt or "api" in table_txt
    assert "## " in md_txt

    # Historical baseline JSON pairing when only sj results are passed.
    baseline_file = tmp_path / "historical_baseline.json"
    baseline_file.write_text(
        json.dumps(
            [
                {
                    "task_id": "api-orders",
                    "category": "api",
                    "bucket": "worse",
                    "steps38": 22,
                    "tok38": 55000,
                    "reas_tok38": 2000,
                    "msg_tok38": 1000,
                    "dur38": 44.0,
                    "status38": "success",
                    "cap38": 1.0,
                }
            ]
        ),
        encoding="utf-8",
    )
    hist_json = json.loads(render_summary_report(loaded_sj, baseline_json=baseline_file, format="json"))
    assert hist_json["paired"]["overall"]["n"] == 1


def test_doctor_validate_referee_and_cli_entrypoints(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ds_root = tmp_path / "cab"
    _create_synthetic_task(ds_root, category="file", task_id="file-clean-referee", leaky_grader=False)
    _create_synthetic_task(ds_root, category="file", task_id="file-leaky-referee", leaky_grader=True)

    cfg = BenchConfig(dataset_root=ds_root, workdir=tmp_path / "work", judge_provider="mock")
    ok_doc, doc_lines = run_doctor(cfg)
    assert ok_doc is True
    assert any("2 tasks discovered" in ln for ln in doc_lines)

    missing_cfg = BenchConfig(dataset_root=tmp_path / "missing_cab", workdir=tmp_path / "work")
    ok_missing, _ = run_doctor(missing_cfg)
    assert ok_missing is False

    # Clean task passes model-free referee validation ($0 API cost).
    res_clean = validate_task_referee(cfg, "file-clean-referee", slot=86)
    assert res_clean["ok"] is True
    assert res_clean["setup_ok"] is True
    assert res_clean["ctx_capture_ok"] is True
    assert res_clean["digests_delivered"] >= 1
    assert res_clean["grader_ok"] is True
    assert res_clean["baseline_passed"] is False
    assert res_clean["leaked_processes"] == 0

    # Leaky grader (passing on untouched empty /task/outputs) MUST be flagged by the referee!
    res_leaky = validate_task_referee(cfg, "file-leaky-referee", slot=87)
    assert res_leaky["ok"] is False
    assert res_leaky["baseline_passed"] is True
    assert any("leakage" in err for err in res_leaky["errors"])

    # Exercise CLI subcommands (`doctor`, `validate`, `report`) end-to-end.
    rc_doc = ab_cli.main(["doctor", "--dataset-root", str(ds_root), "--json"])
    assert rc_doc == 0
    doc_payload = json.loads(capsys.readouterr().out)
    assert doc_payload["ok"] is True

    rc_val = ab_cli.main(
        [
            "validate",
            "--dataset-root",
            str(ds_root),
            "--task",
            "file-clean-referee",
            "--slot",
            "88",
            "--json",
        ]
    )
    assert rc_val == 0
    val_payload = json.loads(capsys.readouterr().out)
    assert val_payload["ok"] is True
    assert val_payload["passed"] == 1

    rep_out = tmp_path / "summary.md"
    rc_rep = ab_cli.main(
        [
            "report",
            "--workdir",
            str(tmp_path / "work"),
            "--format",
            "markdown",
            "--output",
            str(rep_out),
        ]
    )
    assert rc_rep == 0
    assert rep_out.is_file()


def test_html_report_auto_generation_and_gzip_payload_roundtrip(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import base64
    import gzip

    ds_root = tmp_path / "cab"
    _create_synthetic_task(ds_root, category="file", task_id="file-margin-audit")
    wdir = tmp_path / "workdir"
    runs_dir = wdir / "runs"

    for arm, passed, steps, p_tok, c_tok, epochs, digests in [
        ("raw", False, 18, 48_000, 30_000, 0, 0),
        ("sj", True, 9, 19_000, 15_000, 1, 2),
    ]:
        mdir = runs_dir / f"file-margin-audit__{arm}_meta"
        mdir.mkdir(parents=True, exist_ok=True)
        rm = RunMetrics(
            task_id="file-margin-audit",
            task_rel="file/file-margin-audit",
            category="file",
            arm=arm,
            model_id="gemini-2.5-flash",
            bucket="worse",
            steps=steps,
            prompt_tokens=p_tok,
            cached_tokens=c_tok,
            thought_tokens=1_400,
            output_tokens=500,
            wall_clock_s=21.5 if arm == "sj" else 42.0,
            compaction_epochs=epochs,
            thoughts_compacted=6_200 if epochs else 0,
            state_notes_emitted=8 if arm == "sj" else 0,
            state_notes_preserved=6 if arm == "sj" else 0,
            digests_delivered=digests,
            bytes_kept_out=64_000 if digests else 0,
            passed=passed,
            capacity_score=1.0 if passed else 0.5,
            checks_passed=2 if passed else 1,
            checks_total=2,
        )
        (mdir / "result.json").write_text(json.dumps(rm.to_dict()), encoding="utf-8")
        (mdir / "reward.json").write_text(
            json.dumps(
                {
                    "passed": passed,
                    "score": rm.capacity_score,
                    "checks_breakdown": [
                        {"id": "schema_valid", "passed": True, "reason": "ok"},
                        {"id": "totals_match", "passed": passed, "reason": "ok" if passed else "expected 420"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        traj_lines = [
            json.dumps(
                {
                    "step": 1,
                    "lat_s": 2.1,
                    "p_tok": 9_000,
                    "c_tok": 6_000,
                    "t_tok": 700,
                    "o_tok": 200,
                    "text": "",
                    "calls": [{"tool": "bash", "result_chars": 420, "head": 'run:abc123 next: ctx get'}],
                }
            ),
            json.dumps(
                {
                    "step": 2,
                    "lat_s": 1.8,
                    "p_tok": 6_500 if epochs else 14_000,
                    "c_tok": 5_000,
                    "t_tok": 300,
                    "o_tok": 120,
                    "text": "",
                    "calls": [{"tool": "present_files", "result_chars": 40, "head": "presented summary.json"}],
                }
            ),
        ]
        (mdir / "trajectory.jsonl").write_text("\n".join(traj_lines) + "\n", encoding="utf-8")

    html_out = tmp_path / "auto_report.html"
    rc = ab_cli.main(
        [
            "report",
            "--workdir",
            str(wdir),
            "--dataset-root",
            str(ds_root),
            "--format",
            "html",
            "--title",
            "Synthetic Eval Suite",
            "--output",
            str(html_out),
        ]
    )
    assert rc == 0
    _ = capsys.readouterr()
    assert html_out.is_file()

    doc = html_out.read_text(encoding="utf-8")
    assert "<!DOCTYPE html>" in doc
    assert "Synthetic Eval Suite" in doc
    assert "DecompressionStream" in doc

    m_b64 = re.search(
        r'<script id="payload-gz" type="application/octet-stream">(.*?)</script>',
        doc,
        re.DOTALL,
    )
    assert m_b64 is not None
    decoded = json.loads(gzip.decompress(base64.b64decode(m_b64.group(1).strip())).decode("utf-8"))
    assert decoded["meta"]["total_tasks"] == 1
    assert decoded["meta"]["primary_arm"] == "sj"
    assert decoded["meta"]["baseline_arm"] == "raw"
    assert len(decoded["arms_order"]) == 2
    assert decoded["scopes"][0]["arms"]["sj"]["pass_pct"] == 100.0
    assert decoded["scopes"][0]["arms"]["raw"]["pass_pct"] == 0.0
    assert "Inspect `/task/workspace/orders.json`" in decoded["traces"]["file-margin-audit"]["prompt"]
    assert decoded["traces"]["file-margin-audit"]["arms"]["sj"]["steps"][1]["ep"] == 1


# ---------------------------------------------------------------------------
# 8. Structural Failure-Prevention Mechanisms (Sandbox, Browser, Host & Loop)
# ---------------------------------------------------------------------------


def test_sandbox_preserves_canonical_case_dir_name_and_skips_occupied_ports(tmp_path: Path) -> None:
    import socket

    ds_root = tmp_path / "cab"
    runtime_dir = tmp_path / "runtime"
    (runtime_dir / "mock_services").mkdir(parents=True)
    case_dir = _create_synthetic_task(
        ds_root,
        category="browser/freightos",
        task_id="booking",
        mock_services=True,
        service_port=3000,
    )

    cfg = BenchConfig(dataset_root=ds_root, workdir=tmp_path / "work", runtime_dir=runtime_dir, judge_provider="mock")
    spec = load_cab_task(ds_root, "booking")
    run_dir = cfg.runs_dir / "booking__sj"
    meta_dir = cfg.runs_dir / "booking__sj_meta"

    # Hold slot 89's primary port (28900) occupied so _allocate_free_ports must advance to 28901.
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 28900))
    blocker.listen(1)
    sandbox = TaskSandbox(cfg, spec, run_dir, meta_dir, slot=89)
    try:
        sandbox.setup()
        # Canonical `<parent>/<name>` (`freightos/booking`) preserved under `mock_case/`.
        assert sandbox.mock_case_dir.name == case_dir.name == "booking"
        assert sandbox.mock_case_dir.parent.name == "freightos"
        assert 3000 in sandbox.port_map
        assigned_port = sandbox.port_map[3000]
        assert assigned_port >= 28901

        with urllib.request.urlopen(f"http://127.0.0.1:{assigned_port}/quote", timeout=3) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        assert payload["port"] == assigned_port
    finally:
        sandbox.cleanup()
        blocker.close()

    assert _find_slot_pids(sandbox.slot_tag) == []


def test_browser_bracketed_ref_closed_loop_fill_and_error_draining() -> None:
    assert BrowserSession._normalize_ref("[e14]") == "e14"
    assert BrowserSession._normalize_ref("  e7 ") == "e7"
    assert BrowserSession._normalize_ref("#save-draft") == "#save-draft"

    listeners: dict[str, list[Any]] = {}
    state: dict[str, Any] = {
        '[data-sj-ref="e1"]': {"tag": "input", "type": "checkbox", "disabled": False, "checked": False, "value": ""},
        '[data-sj-ref="e2"]': {"tag": "select", "type": "select-one", "disabled": True, "checked": False, "value": ""},
        '[data-sj-ref="e3"]': {"tag": "input", "type": "text", "disabled": False, "checked": False, "value": ""},
    }
    queried_selectors: list[str] = []

    class FakeLoc:
        def __init__(self, sel: str) -> None:
            self.sel = sel
            self.first = self

        def evaluate(self, expr: str) -> Any:
            st = state[self.sel]
            if "contentEditable" in expr and "disabled" in expr:
                return {
                    "tag": st["tag"],
                    "type": st["type"],
                    "disabled": st["disabled"],
                    "contentEditable": False,
                }
            if "selectedOptions" in expr:
                return f"{st['value']} (Parent Label)"
            if "el.checked" in expr:
                return st["checked"]
            return st["value"]

        def set_checked(self, val: bool, **kwargs: Any) -> None:
            state[self.sel]["checked"] = val
            # Simulate checkbox toggling dependent select enabled!
            if self.sel == '[data-sj-ref="e1"]' and val:
                state['[data-sj-ref="e2"]']["disabled"] = False

        def select_option(self, value: str | None = None, label: str | None = None, **kwargs: Any) -> None:
            state[self.sel]["value"] = value or label or ""

        def fill(self, val: str, **kwargs: Any) -> None:
            # Simulate a UI handler clamping/truncating input to test mismatch detection.
            state[self.sel]["value"] = val[:8]

    class FakeRichPage:
        url = "http://127.0.0.1:20500/mail"

        def on(self, event: str, fn: Any) -> None:
            listeners.setdefault(event, []).append(fn)

        def goto(self, url: str, **kwargs: Any) -> None:
            self.url = url

        def title(self) -> str:
            return "Mail"

        def locator(self, sel: str) -> FakeLoc:
            queried_selectors.append(sel)
            return FakeLoc(sel)

        def wait_for_timeout(self, ms: int) -> None:
            pass

        def wait_for_load_state(self, st: str, **kwargs: Any) -> None:
            pass

        def evaluate(self, script: str, arg: Any = None) -> Any:
            if arg is not None:
                return 'Container actions: [e8]#save-draft "Save draft", [e9]#close-compose "Discard"'
            return {
                "url": self.url,
                "title": "Mail",
                "controlsCount": 3,
                "controls": '[e1] <input type=checkbox #nested value="unchecked">',
                "bodyText": "Compose modal",
            }

        def close(self) -> None:
            pass

    class FakeCtx:
        def new_page(self) -> FakeRichPage:
            return FakeRichPage()

        def close(self) -> None:
            pass

    sess = BrowserSession(FakeCtx(), {})
    sess.open_tab("http://127.0.0.1:20500/mail")

    # Emit a simulated HTTP 500 response during fill_fields to verify error draining.
    class FakeResp:
        status = 500
        url = "http://127.0.0.1:20500/api/drafts"

    res = sess.fill_fields(
        "vtab-1",
        [
            {"ref": "[e2]", "type": "select", "value": "ops"},  # Still disabled before e1 is checked!
            {"ref": "[e1]", "type": "checkbox", "value": "true"},  # Enables e2!
            {"ref": "[e2]", "type": "select", "value": "ops"},  # Now succeeds!
            {"ref": "[e3]", "type": "text", "value": " Quarterly Forecast "},  # Clamped -> MISMATCH!
        ],
    )
    for fn in listeners.get("response", []):
        fn(FakeResp())
    err_tail = sess._drain_tab_errors("vtab-1")

    assert '[data-sj-ref="e1"]' in queried_selectors
    assert "[e2]=[SKIPPED: control is disabled]" in res
    assert "[e1]=checked" in res
    assert "[e2]=ops (Parent Label)" in res
    assert "MISMATCH" in res
    assert '#save-draft "Save draft"' in res
    assert "HTTP 500" in err_tail
    sess.close()


def test_host_empirical_fs_mutation_untitled_hygiene_and_loop_exit_guards(tmp_path: Path) -> None:
    # 1. Empirical filesystem mutation bumps mutation_epoch even when command text looks non-mutating,
    #    and untrashed "Untitled" scratch creations gate present_files until trashed.
    host, metrics, run_dir, _ = _make_host(tmp_path / "emp", arm="sj")
    host.step = 1
    host.execute_part(
        "write",
        {
            "file_path": "/task/tmp/tmp_writer.py",
            "content": (
                "import pathlib\n"
                "p = pathlib.Path('/task/outputs/result.json')\n"
                "p.write_bytes(b'{\"ok\": true}')\n"
                'print(\'✓ Created page "Untitled" (page_probe_01)\')\n'
            ),
        },
    )
    assert host.mutation_epoch == 1

    host.step = 2
    host.execute_part("list", {"path": "/task/outputs"})
    assert host.mutation_epoch == 1

    # `python3 /task/tmp/tmp_writer.py` has NO `>`, `write_text`, or CLI verb in the command string itself!
    host.step = 3
    host.execute_part("bash", {"command": "python3 /task/tmp/tmp_writer.py"})
    assert host.mutation_epoch == 2
    assert "page_probe_01" in host.untrashed_untitled_ids

    # Read-after-write `list /task/outputs` must NOT be blocked as duplicate because mutation_epoch advanced!
    host.step = 4
    fr_list2, _, _ = host.execute_part("list", {"path": "/task/outputs"})
    assert host.last_call_was_dup is False
    assert "result.json" in fr_list2["functionResponse"]["response"]["output"]

    # First present_files gates on the untrashed "Untitled" probe creation.
    host.step = 5
    fr_gate, _, _ = host.execute_part("present_files", {"files": ["/task/outputs/result.json"]})
    assert host.completed is False
    assert "page_probe_01" in fr_gate["functionResponse"]["response"]["output"]

    # Trashing `page_probe_01` clears the untrashed tracker.
    host.step = 6
    host.execute_part("bash", {"command": 'echo \'✓ Page "Untitled" (page_probe_01) moved to trash\''})
    assert host.untrashed_untitled_ids == {}

    # 2. Loop exit guard: survives an empty STOP turn AND blocks premature text exit when prompt-declared
    #    deliverables (`/task/outputs/summary.json`) are still missing!
    ds_root = tmp_path / "cab_guard"
    _create_synthetic_task(ds_root, category="cli", task_id="cli-deck-guard")

    class PrematureStopProvider:
        def __init__(self) -> None:
            self.turn = 0

        def generate(self, model_id: str, contents: list[dict[str, Any]], tools: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
            self.turn += 1
            if self.turn == 1:
                # Simulate empty STOP turn (0 tool calls, empty text).
                parts: list[dict[str, Any]] = [{"text": ""}]
            elif self.turn in (2, 3, 4, 5):
                if self.turn < 5:
                    parts = [{"functionCall": {"name": "list", "args": {"path": f"/task/workspace"}}}]
                else:
                    # Step 5 (>= _MIN_STEPS_FOR_TEXT_COMPLETE on a `cli` task!), model tries to exit with prose
                    # BEFORE creating prompt-declared `/task/outputs/summary.json`!
                    parts = [{"text": "I have reviewed the workspace and everything looks good."}]
            elif self.turn == 6:
                # After receiving `[ctx-emission-gate]` missing-deliverable nudge, model writes summary.json!
                last_user = contents[-1]["parts"][0].get("text", "")
                assert "[ctx-emission-gate]" in last_user and "outputs/summary.json" in last_user
                parts = [
                    {
                        "functionCall": {
                            "name": "write",
                            "args": {
                                "path": "/task/outputs/summary.json",
                                "content": json.dumps({"total_usd": 420, "status": "verified"}),
                            },
                        }
                    }
                ]
            else:
                parts = [{"functionCall": {"name": "present_files", "args": {"files": ["/task/outputs/summary.json"]}}}]

            return {
                "candidates": [{"finishReason": "STOP", "content": {"role": "model", "parts": parts}}],
                "usageMetadata": {"promptTokenCount": 500, "candidatesTokenCount": 25},
            }

        def judge_access_token(self) -> str:
            return ""

        def close(self) -> None:
            pass

    cfg = BenchConfig(
        allow_unsandboxed=True,  # explicit opt-in: CI has no bwrap
        dataset_root=ds_root,
        workdir=tmp_path / "guard_work",
        provider="gemini",
        api_key="dummy",
        judge_provider="mock",
        arms=("sj",),
    )
    res = run_trial(cfg, "cli-deck-guard", "sj", 90, PrematureStopProvider())
    assert res.passed is True
    assert res.capacity_score == 1.0
    assert res.emission_gate_hits >= 1
    assert res.termination == "present_files"


def test_wave2_dom_diff_browser_batch_ctx_retrieve_and_external_mutation_audit(tmp_path: Path) -> None:
    # 1. Schema parity: raw arm stays 100% untouched; sj arm exposes ctx_retrieve and browser_batch.
    raw_decls = build_tools(requires_browser=True, requires_network=True, enable_state_note=False)[0][
        "functionDeclarations"
    ]
    raw_names = {d["name"] for d in raw_decls}
    assert "ctx_retrieve" not in raw_names
    assert "browser_batch" not in raw_names

    sj_browser_decls = build_tools(requires_browser=True, requires_network=True, enable_state_note=True)[0][
        "functionDeclarations"
    ]
    sj_browser_names = {d["name"] for d in sj_browser_decls}
    assert "ctx_retrieve" in sj_browser_names
    assert "browser_batch" in sj_browser_names
    assert is_mutating_tool_call("ctx_retrieve", {"handle": "run:abc123"}) is False
    assert is_mutating_tool_call("browser_batch", {"steps": [{"action": "click", "ref": "e1"}]}) is True

    sj_file_decls = build_tools(requires_browser=False, requires_network=False, enable_state_note=True)[0][
        "functionDeclarations"
    ]
    sj_file_names = {d["name"] for d in sj_file_decls}
    assert "ctx_retrieve" in sj_file_names
    assert "browser_batch" not in sj_file_names

    # 2. BrowserSession [dom-diff] state feedback + browser_batch with #id fallback when data-sj-ref re-renders.
    state = {"starred": False, "archived": False, "subject": "", "label": "inbox", "stale_refs": False}

    class FakeBatchLoc:
        def __init__(self, sel: str) -> None:
            self.sel = sel

        @property
        def first(self) -> FakeBatchLoc:
            return self

        def count(self) -> int:
            if state["stale_refs"] and self.sel.startswith("[data-sj-ref="):
                return 0
            return 1

        def click(self, **kwargs: Any) -> None:
            if state["stale_refs"] and self.sel.startswith("[data-sj-ref="):
                raise RuntimeError("Timeout waiting for stale data-sj-ref")
            if self.sel in ('[data-sj-ref="e1"]', "#star-btn"):
                state["starred"] = not state["starred"]
            elif self.sel in ('[data-sj-ref="e2"]', "#noop-btn"):
                pass  # Inert control: produces NO CHANGE!

        def fill(self, value: str, **kwargs: Any) -> None:
            if state["stale_refs"] and self.sel.startswith("[data-sj-ref="):
                raise RuntimeError("Timeout waiting for stale data-sj-ref")
            state["subject"] = value

        def select_option(self, value: str, **kwargs: Any) -> None:
            state["label"] = value

        def set_checked(self, checked: bool, **kwargs: Any) -> None:
            state["archived"] = checked

        def press(self, key: str, **kwargs: Any) -> None:
            pass

    class FakeBatchKeyboard:
        def __init__(self) -> None:
            self.keys: list[str] = []

        def press(self, key: str) -> None:
            self.keys.append(key)

    class FakeBatchPage:
        def __init__(self) -> None:
            self.url = "http://127.0.0.1:20600/inbox"
            self.keyboard = FakeBatchKeyboard()

        def is_closed(self) -> bool:
            return False

        def on(self, event: str, handler: Any) -> None:
            pass

        def goto(self, url: str, **kwargs: Any) -> None:
            self.url = url

        def title(self) -> str:
            return "Launch Inbox"

        def locator(self, sel: str) -> FakeBatchLoc:
            return FakeBatchLoc(sel)

        def evaluate(self, script: str, *args: Any) -> Any:
            pressed_s = "true" if state["starred"] else "false"
            chk_val = "checked" if state["archived"] else "unchecked"
            controls = "\n".join(
                [
                    f'[e1] <button #star-btn aria="Star > thread" pressed={pressed_s}> Star thread',
                    "[e2] <button #noop-btn> Header spacer",
                    f"[e3] <input type=text #subj-input value={json.dumps(state['subject'])}> Subject",
                    f"[e4] <select #lbl-sel value={json.dumps(state['label'])}> Move to",
                    f'[e5] <input type=checkbox #arch-chk value={json.dumps(chk_val)}> Archive',
                ]
            )
            return {
                "url": self.url,
                "title": self.title(),
                "controlsCount": 5,
                "controls": controls,
                "bodyText": f"Inbox starred={state['starred']} label={state['label']}",
            }

        def wait_for_timeout(self, ms: int) -> None:
            pass

        def wait_for_load_state(self, state_name: str, **kwargs: Any) -> None:
            pass

        def close(self) -> None:
            pass

    class FakeBatchCtx:
        def new_page(self) -> FakeBatchPage:
            return FakeBatchPage()

        def close(self) -> None:
            pass

    sess = BrowserSession(FakeBatchCtx(), {}, enable_dom_diff=True)
    sess.open_tab("http://127.0.0.1:20600/inbox")
    sess.snapshot("vtab-1")

    # Inert click surfaces explicit NO CHANGE so the model stops clicking dead wrappers.
    noop_out = sess.click("vtab-1", "e2")
    assert "[dom-diff] NO CHANGE" in noop_out

    # Toggling click surfaces exact pressed=false->true delta.
    star_out = sess.click("vtab-1", "e1")
    assert "[dom-diff]" in star_out and "pressed=false->true" in star_out

    # Simulate React re-render wiping data-sj-ref before browser_batch runs -> falls back to #star-btn / #subj-input!
    state["stale_refs"] = True
    batch_out = sess.batch(
        "vtab-1",
        [
            {"action": "click", "ref": "[e1]"},
            {"action": "fill", "ref": "e3", "value": "Launch Go/No-Go"},
            {"action": "select", "ref": "e4", "value": "launch-p0"},
            {"action": "check", "ref": "e5"},
            {"action": "press", "key": "Escape"},
        ],
    )
    assert "[Batch 5/5 steps on vtab-1]" in batch_out
    assert state["starred"] is False
    assert state["subject"] == "Launch Go/No-Go"
    assert state["label"] == "launch-p0"
    assert state["archived"] is True
    assert "[dom-diff]" in batch_out and "pressed=true->false" in batch_out
    sess.close()

    # 3. ToolHost ctx_retrieve structured retrieval + Q2 duplicate link gate + external mutation audit.
    extra_bin = tmp_path / "cli_bin"
    _write_executable(
        extra_bin / "jira",
        (
            "#!/usr/bin/env bash\n"
            'if [ "$1" = "issue" ] && [ "$2" = "link" ]; then\n'
            '  echo "Linked $3 -> $4 ($5)"\n'
            'elif [ "$1" = "issue" ] && [ "$2" = "comment" ] && [ "$3" = "add" ]; then\n'
            '  echo "Commented on $4"\n'
            'elif [ "$1" = "issue" ] && [ "$2" = "view" ]; then\n'
            '  echo "Issue $3: links=[PROJ-2 (Duplicates)]"\n'
            "fi\n"
        ),
    )
    host, metrics, run_dir, _ = _make_host(
        tmp_path / "w2_host",
        arm="sj",
        entry_gate_bytes=2_048,
        baseline_raw_cap=100_000,
        extra_bin=extra_bin,
    )

    # Capture a >35 KB JSON array via bash so Straitjacket returns a run:<id> handle.
    host.step = 1
    gen_json_cmd = (
        "python3 -B -c \"import json; "
        "print(json.dumps([{'sku': f'ITEM_{i:04d}', 'price': i * 10, 'pad': 'x' * 60} for i in range(500)]))\""
    )
    fr_cap, _, _ = host.execute_part("bash", {"command": gen_json_cmd})
    cap_txt = fr_cap["functionResponse"]["response"]["output"]
    m_run = re.search(r"\brun:([0-9a-f]{6,64})\b", cap_txt)
    assert m_run is not None
    run_handle = f"run:{m_run.group(1)}"

    host.step = 2
    fr_ptr, _, _ = host.execute_part("ctx_retrieve", {"handle": run_handle, "mode": "get", "json_pointer": "/42"})
    assert "ITEM_0042" in fr_ptr["functionResponse"]["response"]["output"]

    host.step = 3
    fr_srch, _, _ = host.execute_part("ctx_retrieve", {"handle": run_handle, "mode": "search", "pattern": "ITEM_0055"})
    assert "ITEM_0055" in fr_srch["functionResponse"]["response"]["output"]

    host.step = 4
    fr_stats, _, _ = host.execute_part("ctx_retrieve", {"handle": run_handle, "mode": "stats"})
    assert fr_stats["functionResponse"]["response"]["output"]

    host.step = 5
    fr_q, _, _ = host.execute_part("ctx_retrieve", {"handle": run_handle, "mode": "query", "query": "count"})
    q_out = fr_q["functionResponse"]["response"]["output"]
    assert "[ctx q" in q_out and "n=" in q_out
    assert metrics.ctx_retrievals == {"get": 1, "search": 1, "stats": 1, "q": 1}

    # Q2 Entry Gate blocks exact duplicate `jira issue link`; non-gated duplicate mutation triggers emission audit.
    (run_dir / "outputs" / "triage.json").write_text('{"status": "triaged"}', encoding="utf-8")
    host.step = 6
    host.execute_part("bash", {"command": "jira issue link PROJ-29 PROJ-2 Duplicates"})
    host.step = 7
    fr_dup_link, _, _ = host.execute_part("bash", {"command": "jira issue link PROJ-29 PROJ-2 Duplicates"})
    assert "[ctx-entry-gate] Blocked duplicate `jira issue link`" in fr_dup_link["functionResponse"]["response"]["output"]
    assert host.external_mutation_count == 1

    host.step = 8
    host.execute_part("bash", {"command": 'jira issue comment add PROJ-29 "Follow-up pending"'})
    host.step = 9
    host.execute_part("bash", {"command": 'jira issue comment add PROJ-29 "Follow-up pending"'})
    assert host.external_mutation_count == 3
    assert len(host.duplicate_external_mutations) == 1

    # First present_files gates once, surfacing both duplicate mutation and missing post-mutation readback.
    host.step = 10
    fr_gate1, _, _ = host.execute_part("present_files", {"files": ["/task/outputs/triage.json"]})
    gate1_txt = fr_gate1["functionResponse"]["response"]["output"]
    assert host.completed is False
    assert metrics.emission_gate_hits == 1
    assert "Duplicate non-idempotent" in gate1_txt
    assert "without a subsequent read-only check" in gate1_txt

    # Readback updates last_external_readback_step, and second present_files completes cleanly.
    host.step = 11
    host.execute_part("bash", {"command": "jira issue view PROJ-29"})
    assert host.last_external_readback_step == 11
    assert host.unverified_mutated_services() == set()
    host.step = 12
    fr_done, _, _ = host.execute_part("present_files", {"files": ["/task/outputs/triage.json"]})
    assert host.completed is True
    assert "Deliverables presented:" in fr_done["functionResponse"]["response"]["output"]


def _assert_private_tmp_round_trip(host: ToolHost, tag: str, host_tmp_marker: Path) -> None:
    """File tools and bash agree on a private /tmp, in both directions, with or without bwrap."""

    def bash(cmd: str) -> str:
        host.mutation_epoch += 1  # never answered by the readonly dedup
        fr, _, _ = host.execute_part("bash", {"command": cmd})
        return fr["functionResponse"]["response"]["output"]

    def tool(name: str, args: dict[str, Any]) -> str:
        host.mutation_epoch += 1
        fr, _, _ = host.execute_part(name, args)
        return fr["functionResponse"]["response"]["output"]

    note, made = f"step_note_{tag}.txt", f"bash_made_{tag}.txt"
    host_tmp_before = set(os.listdir("/tmp"))

    # write tool -> bash
    tool("write", {"file_path": f"/tmp/{note}", "content": f"NOTE_{tag}"})
    assert not (Path("/tmp") / note).exists()
    assert f"NOTE_{tag}" in bash(f"cat /tmp/{note}")
    # bash -> read tool (a new file lands in the private directory)
    bash(f"echo BASH_{tag} > /tmp/{made} && mkdir -p /tmp/sub_{tag} && echo NESTED > /tmp/sub_{tag}/n.txt")
    assert (host.sandbox_tmp / made).read_text(encoding="utf-8").strip() == f"BASH_{tag}"
    assert f"BASH_{tag}" in tool("read", {"file_path": f"/tmp/{made}"})
    assert "NESTED" in tool("read", {"file_path": f"/tmp/sub_{tag}/n.txt"})
    # ls /tmp shows the run's files
    listing = bash("ls /tmp")
    assert note in listing and made in listing and f"sub_{tag}" in listing
    assert "NESTED" in bash(f"cd /tmp/sub_{tag} && cat n.txt")
    # look-alikes are untouched
    looks = ["/tmpfoo", "/tmp.txt", "http://x/tmp/y", "~/tmp", "/var/tmp", "file:///tmp/z",
             "/tmp~/z", "${X}/tmp/zz", "$(pwd)/tmp/q", "a/tmp/b", "x/tmp"]
    if not host._use_bwrap:
        priv = str(host.sandbox_tmp)
        for s in looks:
            assert host._rewrite_tmp(s) == s, s
            assert host._rewrite_tmp(f"cat {s} | wc") == f"cat {s} | wc", s
        for raw, want in (
            ("cat /tmp/zz_new", f"cat {priv}/zz_new"),
            ("X=/tmp/zz_new", f"X={priv}/zz_new"),
            ("--out=/tmp/zz_new", f"--out={priv}/zz_new"),
            ('cat "/tmp/zz_new"', f'cat "{priv}/zz_new"'),
            ("ls /tmp", f"ls {priv}"),
            ("ls /tmp/", f"ls {priv}/"),
            ("(cd /tmp;ls)", f"(cd {priv};ls)"),
            ("a</tmp/zz_new>/tmp/zz2", f"a<{priv}/zz_new>{priv}/zz2"),
            ("PATH=/x:/tmp/zz_new", f"PATH=/x:{priv}/zz_new"),
        ):
            assert host._rewrite_tmp(raw) == want, raw
    out_lines = bash("printf '%s\\n' " + " ".join(f"'{s}'" for s in looks)).splitlines()
    assert out_lines[-len(looks):] == looks
    # host paths under /tmp the harness itself uses keep working (run dir, ctx, python)
    if str(host.run_dir).startswith("/tmp/"):
        (host.run_dir / "workspace").mkdir(parents=True, exist_ok=True)
        (host.run_dir / "workspace" / f"hp_{tag}.txt").write_text("HOSTPATH_OK", encoding="utf-8")
        assert "HOSTPATH_OK" in bash(f"cat {host.run_dir}/workspace/hp_{tag}.txt")
    if not host._use_bwrap:
        # an existing host /tmp entry the run does not shadow stays reachable
        assert "HOST_TMP_SECRET" in bash(f"cat {host_tmp_marker}")
    # nothing new appeared in the host's /tmp
    assert not (Path("/tmp") / made).exists()
    assert not (Path("/tmp") / f"sub_{tag}").exists()
    leaked = {n for n in set(os.listdir("/tmp")) - host_tmp_before if tag in n}
    assert leaked == set()


def test_tool_host_bwrap_namespace_isolation_fallback_and_doctor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evals.acciobench.host import bwrap_sandbox_executable

    ds_root = tmp_path / "cab"
    secret_grader = ds_root / "datasets_domain_v1" / "file" / "t1" / "grader"
    secret_grader.mkdir(parents=True)
    (secret_grader / "expected.json").write_text('{"secret_answer": 9999}', encoding="utf-8")

    workdir = tmp_path / "work_iso"
    cfg = BenchConfig(dataset_root=ds_root, workdir=workdir, arms=("raw", "sj"))
    sibling_dir = cfg.runs_dir / "other_task__raw" / "outputs"
    sibling_dir.mkdir(parents=True)
    (sibling_dir / "sibling_leak.json").write_text('{"leaked": true}', encoding="utf-8")

    # Plant a host /tmp file outside sandbox_tmp to verify /tmp isolation.
    host_tmp_marker = Path("/tmp") / f"sj_host_tmp_probe_{os.getpid()}.txt"
    host_tmp_marker.write_text("HOST_TMP_SECRET", encoding="utf-8")
    try:
        for arm in ("raw", "sj"):
            run_dir = cfg.runs_dir / f"t1__{arm}"
            meta_dir = cfg.runs_dir / f"t1__{arm}_meta"
            (run_dir / "workspace").mkdir(parents=True, exist_ok=True)
            (run_dir / "workspace" / "input.txt").write_text("TRIAL_OK\n", encoding="utf-8")
            (run_dir / "outputs").mkdir(parents=True, exist_ok=True)
            (meta_dir / "mock_case" / "grader").mkdir(parents=True, exist_ok=True)
            (meta_dir / "mock_case" / "grader" / "run.sh").write_text("echo SECRET_GRADER\n", encoding="utf-8")

            metrics = RunMetrics(
                task_id="t1",
                task_rel="file/t1",
                category="file",
                arm=arm,
                model_id="test-model",
            )
            host = ToolHost(cfg, run_dir, meta_dir, {}, dict(os.environ), metrics, None, arm=arm)

            # Host-side tools block dataset_root, sibling runs, and meta_dir/mock_case in both modes.
            fr_rd_sec, _, _ = host.execute_part("read", {"file_path": str(secret_grader / "expected.json")})
            assert "File not found" in fr_rd_sec["functionResponse"]["response"]["output"]
            fr_ls_sib, _, _ = host.execute_part("list", {"path": str(sibling_dir)})
            assert "Path not found" in fr_ls_sib["functionResponse"]["response"]["output"]
            fr_gr_mc, _, _ = host.execute_part("grep", {"pattern": "SECRET_GRADER", "path": str(meta_dir)})
            assert fr_gr_mc["functionResponse"]["response"]["output"] == "(no matches)"

            # The write tool keeps /tmp in the run's private directory, never the host's,
            # and bash sees that same directory as /tmp: with bwrap through the mount
            # namespace, without it through the fallback's /tmp rule.
            _assert_private_tmp_round_trip(host, f"{arm}_default", host_tmp_marker)

            if bwrap_sandbox_executable() is not None:
                assert host._use_bwrap is True
                probe_sh = (
                    "pwd && cat /task/workspace/input.txt && "
                    f"test ! -e {str(host_tmp_marker)!r} && echo TMP_ISOLATED && "
                    f"test ! -e {str(secret_grader / 'expected.json')!r} && echo DS_HIDDEN && "
                    f"test ! -e {str(sibling_dir / 'sibling_leak.json')!r} && echo SIB_HIDDEN && "
                    f"test ! -e {str(meta_dir / 'mock_case')!r} && echo META_HIDDEN && "
                    "ps -eo pid= | wc -l"
                )
                fr_iso, _, _ = host.execute_part("bash", {"command": probe_sh})
                iso_out = fr_iso["functionResponse"]["response"]["output"]
                iso_lines = [ln.strip() for ln in iso_out.strip().splitlines() if ln.strip()]
                assert iso_lines[0] == "/task"
                assert "TRIAL_OK" in iso_lines
                assert "TMP_ISOLATED" in iso_lines
                assert "DS_HIDDEN" in iso_lines
                assert "SIB_HIDDEN" in iso_lines
                assert "META_HIDDEN" in iso_lines
                assert int(iso_lines[-1]) <= 8

        # Explicit fallback (use_bwrap=False / ACCIOBENCH_NO_BWRAP=1) works cleanly on non-bwrap hosts.
        fb_run = cfg.runs_dir / "t_fb__sj"
        fb_meta = cfg.runs_dir / "t_fb__sj_meta"
        (fb_run / "workspace").mkdir(parents=True, exist_ok=True)
        (fb_run / "workspace" / "w.txt").write_text("FB_OK", encoding="utf-8")
        (fb_run / "outputs").mkdir(parents=True, exist_ok=True)
        fb_metrics = RunMetrics(
            task_id="t_fb", task_rel="file/t_fb", category="file", arm="sj", model_id="test-model"
        )
        fb_host = ToolHost(
            cfg, fb_run, fb_meta, {}, dict(os.environ), fb_metrics, None, arm="sj", use_bwrap=False
        )
        assert fb_host._use_bwrap is False
        fr_fb, _, _ = fb_host.execute_part("bash", {"command": "cat /task/workspace/w.txt"})
        assert "FB_OK" in fr_fb["functionResponse"]["response"]["output"]
        # The same /tmp checks on an explicit fallback host, also where bwrap exists.
        _assert_private_tmp_round_trip(fb_host, "sj_fallback", host_tmp_marker)
        fb_host.mutation_epoch += 1
        fr_tmpd, _, _ = fb_host.execute_part(
            "bash", {"command": "python3 -c 'import tempfile; print(tempfile.gettempdir())'"}
        )
        assert str(fb_host.sandbox_tmp) in fr_tmpd["functionResponse"]["response"]["output"]
        raw_fb_run = cfg.runs_dir / "t_fb__raw"
        (raw_fb_run / "workspace").mkdir(parents=True, exist_ok=True)
        raw_fb_host = ToolHost(
            cfg, raw_fb_run, cfg.runs_dir / "t_fb__raw_meta", {}, dict(os.environ),
            RunMetrics(task_id="t_fb", task_rel="file/t_fb", category="file", arm="raw", model_id="m"),
            None, arm="raw", use_bwrap=False,
        )
        _assert_private_tmp_round_trip(raw_fb_host, "raw_fallback", host_tmp_marker)

        # Broken inline python3 -c quote syntax surfaces 1-line heredoc hint on sj arm.
        fr_syn, _, _ = fb_host.execute_part("bash", {"command": 'python3 -c "x = \'unterminated"'})
        syn_out = fr_syn["functionResponse"]["response"]["output"]
        assert "[exit " in syn_out
        assert "python3 - <<'PY'" in syn_out
    finally:
        host_tmp_marker.unlink(missing_ok=True)

    # Doctor checks full data/document library suite + bwrap status.
    monkeypatch.setenv("ACCIOBENCH_NO_BWRAP", "1")
    assert bwrap_sandbox_executable() is None
    monkeypatch.delenv("ACCIOBENCH_NO_BWRAP", raising=False)
    ok_doc, doc_lines = run_doctor(cfg)
    doc_report = "\n".join(doc_lines)
    assert "Python package 'pandas'" in doc_report
    assert "Python package 'pdfplumber'" in doc_report
    assert "Python package 'fitz'" in doc_report
    assert "bwrap sandbox:" in doc_report


_DS_SENTINEL = "DS_SENTINEL_7731"
_MC_SENTINEL = "MC_SENTINEL_4412"

# Every ctx form a model could use to read a file, run a program or reach the host
# through the bash tool. {p} is the absolute path of a grader-side sentinel, {h} a
# real run: handle (so a store read is attached to an escape).
_CTX_ESCAPE_BASH_FORMS = (
    'ctx run --shell "cat {p}"',
    "ctx run --shell 'cat {p}'",
    "ctx run -- cat {p}",
    "ctx run --json -- cat {p}",
    "ctx py \"print(open('{p}').read())\"",
    "ctx py 'print(open(\"{p}\").read())'",
    "ctx seq 'cat {p}'",
    "ctx get {p}",
    "ctx get {p} --lines 1:5",
    "ctx get repo:{p}",
    "ctx search SENTINEL {p}",
    "ctx search {p} SENTINEL",
    "ctx search {h} SENTINEL --glob {p}",
    "ctx stats {p}",
    "ctx diff {p} {p}",
    "ctx diff {h} {p}",
    "ctx intake {p}",
    "ctx intake --chunks {p}",
    "ctx view {p}",
    "ctx q 'lines {p}'",
    "ctx --workspace /tmp get {p}",
    "ctx get {h} --lines 1:5 # {p}",
    "ctx get $(cat {p})",
    "ctx get `cat {p}`",
    "ctx get {h} --lines 1:5 < {p}",
    "ctx get {h} & cat {p}",
    "ctx get {h} <<< $(cat {p})",
    "FOO=1 ctx get {p}",
    "ctx get \"{p}\"",
)


def _ctx_retrieve_escape_forms(p: str, h: str) -> list[dict[str, Any]]:
    return [
        {"handle": p},
        {"handle": p, "lines": "1:5"},
        {"handle": p, "mode": "search", "pattern": "SENTINEL"},
        {"handle": p, "mode": "stats"},
        {"handle": h, "mode": "query", "query": f"lines {p}"},
        {"handle": p, "mode": "query", "query": "count"},
        {"handle": h, "mode": "search", "pattern": f"--glob={p}"},
        {"handle": f"repo:{p}"},
        {"handle": f"blob:{'a' * 12}"},
    ]


@pytest.mark.parametrize("use_bwrap", [None, False], ids=["bwrap_if_available", "fallback"])
def test_ctx_host_fast_path_serves_only_store_handle_reads(
    tmp_path: Path, use_bwrap: bool | None
) -> None:
    from evals.acciobench.host import bwrap_sandbox_executable

    ds_root = tmp_path / "cab"
    (ds_root / "secret").mkdir(parents=True)
    ds_secret = ds_root / "secret" / "expected.json"
    ds_secret.write_text(f'{{"answer": "{_DS_SENTINEL}"}}\n', encoding="utf-8")
    cfg = BenchConfig(dataset_root=ds_root, workdir=tmp_path / "work", arms=("sj",), entry_gate_bytes=2_048)
    run_dir = cfg.runs_dir / "t__sj__t1"
    meta_dir = cfg.runs_dir / "t__sj__t1_meta"
    (run_dir / "workspace").mkdir(parents=True)
    (run_dir / "outputs").mkdir(parents=True)
    mc_secret = meta_dir / "mock_case" / "grader" / "expected.json"
    mc_secret.parent.mkdir(parents=True)
    mc_secret.write_text(f"{_MC_SENTINEL}\n", encoding="utf-8")
    metrics = RunMetrics(task_id="t", task_rel="file/t", category="file", arm="sj", model_id="m")
    host = ToolHost(cfg, run_dir, meta_dir, {}, dict(os.environ), metrics, None, arm="sj", use_bwrap=use_bwrap)
    isolated = host._use_bwrap
    assert isolated == (use_bwrap is None and bwrap_sandbox_executable() is not None)

    sandbox_calls: list[list[str]] = []
    real_run_in_sandbox = host._run_in_sandbox

    def counting_run_in_sandbox(wd: Path, inner_argv: list[str], **kw: Any) -> Any:
        sandbox_calls.append(list(inner_argv))
        return real_run_in_sandbox(wd, inner_argv, **kw)

    host_ctx_calls: list[list[str]] = []
    real_ctx = host.ctx

    def counting_ctx(args: list[str], timeout: int = 120) -> Any:
        host_ctx_calls.append(list(args))
        return real_ctx(args, timeout=timeout)

    host._run_in_sandbox = counting_run_in_sandbox  # type: ignore[method-assign]
    host.ctx = counting_ctx  # type: ignore[method-assign]

    step = [0]

    # Each call gets a fresh mutation epoch so the readonly dedup never answers in place of routing.
    def bash(cmd: str) -> str:
        step[0] += 1
        host.step = step[0]
        host.mutation_epoch += 1
        fr, _, _ = host.execute_part("bash", {"command": cmd})
        return fr["functionResponse"]["response"]["output"]

    def retrieve(args: dict[str, Any]) -> str:
        step[0] += 1
        host.step = step[0]
        host.mutation_epoch += 1
        fr, _, _ = host.execute_part("ctx_retrieve", args)
        return fr["functionResponse"]["response"]["output"]

    flood = bash("seq 1 3000 | sed 's/^/LEDGER_ROW_/'")
    m = re.search(r"\brun:([0-9a-f]{6,64})\b", flood)
    assert m is not None, flood[:400]
    h = f"run:{m.group(1)}"

    # Store-handle reads: answered correctly, by the host fast path, never the sandbox.
    store_reads: list[tuple[str, str]] = [
        (f"ctx get {h} --lines 1:3", "LEDGER_ROW_1"),
        (f"ctx get {h}#stdout --lines 2:4", "LEDGER_ROW_3"),
        (f"ctx search {h} LEDGER_ROW_2999", "LEDGER_ROW_2999"),
        (f"ctx stats {h}", ""),
    ]
    for cmd, expect in store_reads:
        n_sb, n_host = len(sandbox_calls), len(host_ctx_calls)
        out = bash(cmd)
        assert expect in out, (cmd, out[:300])
        assert len(sandbox_calls) == n_sb, f"store read went to the sandbox: {cmd}"
        assert len(host_ctx_calls) == n_host + 1, f"store read not served by the host path: {cmd}"
    for args, expect in (
        ({"handle": h, "lines": "5:7"}, "LEDGER_ROW_6"),
        ({"handle": h, "mode": "search", "pattern": "LEDGER_ROW_2998"}, "LEDGER_ROW_2998"),
        ({"handle": h, "mode": "stats"}, ""),
    ):
        n_sb, n_host = len(sandbox_calls), len(host_ctx_calls)
        out = retrieve(args)
        assert expect in out, (args, out[:300])
        assert len(sandbox_calls) == n_sb and len(host_ctx_calls) == n_host + 1, args

    # Every escape form: never the host path, always the sandbox; with bwrap it cannot see either sentinel.
    for secret, sentinel in ((ds_secret, _DS_SENTINEL), (mc_secret, _MC_SENTINEL)):
        for tmpl in _CTX_ESCAPE_BASH_FORMS:
            cmd = tmpl.format(p=secret, h=h)
            n_sb, n_host = len(sandbox_calls), len(host_ctx_calls)
            out = bash(cmd)
            assert len(host_ctx_calls) == n_host, f"host fast path served: {cmd}"
            assert len(sandbox_calls) > n_sb, f"not sandboxed: {cmd}"
            if isolated:
                assert sentinel not in out, f"sandbox escape through: {cmd}\n{out[:400]}"
        for args in _ctx_retrieve_escape_forms(str(secret), h):
            n_sb, n_host = len(sandbox_calls), len(host_ctx_calls)
            out = retrieve(args)
            assert len(host_ctx_calls) == n_host, f"host fast path served ctx_retrieve {args}"
            assert len(sandbox_calls) > n_sb, f"ctx_retrieve not sandboxed: {args}"
            if isolated:
                assert sentinel not in out, f"ctx_retrieve escape through {args}\n{out[:400]}"

    # The sandboxed ctx calls still work: a run inside the sandbox sees /task.
    (run_dir / "workspace" / "note.txt").write_text("INSIDE_OK\n", encoding="utf-8")
    assert "INSIDE_OK" in bash("ctx run -- cat /task/workspace/note.txt")
    assert "INSIDE_OK" in bash('ctx run --shell "cat /task/workspace/note.txt"')
    assert host.ctx_host_fastpath_calls == len(host_ctx_calls)


_PLANT_BODY = (
    "import os, sys\n"
    "open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'PLANTED_RAN_' + str(os.getpid())), 'w').close()\n"
    "print('PLANTED_CODE_RAN')\n"
    "sys.exit(3)\n"
)


def _plant_markers(root: Path) -> list[Path]:
    return sorted(root.rglob("PLANTED_RAN_*"))


@pytest.mark.parametrize("use_bwrap", [None, False], ids=["bwrap_if_available", "fallback"])
@pytest.mark.parametrize("plant", ["package", "module"])
def test_ctx_module_launch_never_imports_from_run_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, use_bwrap: bool | None, plant: str
) -> None:
    # A Python interpreter as ctx_bin takes the `<python> -m ctx` branch (prefix and shim).
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT / "src"))
    ds_root = tmp_path / "cab"
    ds_root.mkdir()
    cfg = BenchConfig(
        dataset_root=ds_root, workdir=tmp_path / "work", arms=("sj",),
        entry_gate_bytes=2_048, ctx_bin=Path(sys.executable),
    )
    run_dir = cfg.runs_dir / "t__sj__t1"
    meta_dir = cfg.runs_dir / "t__sj__t1_meta"
    (run_dir / "workspace").mkdir(parents=True)
    (run_dir / "outputs").mkdir(parents=True)
    if plant == "package":
        (run_dir / "ctx").mkdir()
        (run_dir / "ctx" / "__init__.py").write_text(_PLANT_BODY, encoding="utf-8")
        (run_dir / "ctx" / "__main__.py").write_text(_PLANT_BODY, encoding="utf-8")
    else:
        (run_dir / "ctx.py").write_text(_PLANT_BODY, encoding="utf-8")

    # Control: the old launch form does run the planted code from this directory.
    ctl = subprocess.run(
        [str(cfg.ctx_bin), "-m", "ctx", "--version"], cwd=str(run_dir),
        capture_output=True, text=True, timeout=60,
    )
    assert "PLANTED_CODE_RAN" in ctl.stdout
    for mk in _plant_markers(run_dir):
        mk.unlink()

    metrics = RunMetrics(task_id="t", task_rel="file/t", category="file", arm="sj", model_id="m")
    host = ToolHost(cfg, run_dir, meta_dir, {}, dict(os.environ), metrics, None, arm="sj", use_bwrap=use_bwrap)
    assert host._ctx_cmd_prefix[1:] == ["-P", "-m", "ctx"]
    assert " -P -m ctx " in (host.bin_dir / "ctx").read_text(encoding="utf-8")

    outs: list[str] = []
    for i, cmd in enumerate(
        [
            "seq 1 3000 | sed 's/^/ROW_/'",  # ordinary sj bash: ctx run inside the sandbox
            "ctx run -- echo SANDBOXED_CTX_OK",  # plain non-store ctx call: sandboxed argv
            'ctx run --shell "echo SHELL_CTX_OK"',  # non-plain ctx call: sandboxed bash -c
            "ctx --version | cat",  # the shim on PATH, via the ordinary path
        ],
        1,
    ):
        host.step = i
        fr, _, _ = host.execute_part("bash", {"command": cmd})
        outs.append(fr["functionResponse"]["response"]["output"])
    m = re.search(r"\brun:([0-9a-f]{6,64})\b", outs[0])
    assert m is not None, outs[0][:300]
    host.step = 9
    fr, _, _ = host.execute_part("bash", {"command": f"ctx get run:{m.group(1)} --lines 1:2"})  # host path
    outs.append(fr["functionResponse"]["response"]["output"])
    host.step = 10
    fr, _, _ = host.execute_part("ctx_retrieve", {"handle": f"run:{m.group(1)}", "lines": "3:4"})
    outs.append(fr["functionResponse"]["response"]["output"])
    assert host.ctx_host_fastpath_calls == 2 and host.ctx_sandboxed_calls >= 2

    assert "SANDBOXED_CTX_OK" in outs[1]
    assert "SHELL_CTX_OK" in outs[2]
    assert "ROW_1" in outs[4] and "ROW_3" in outs[5]
    assert not any("PLANTED_CODE_RAN" in o for o in outs), outs
    assert _plant_markers(run_dir) == []


def test_deliverable_contract_and_q4_cross_row_outlier_detection(tmp_path: Path) -> None:
    from ctx.deliverables import (
        CLOSED_WORLD_CONTRACT_RULE,
        audit_deliverables,
        format_deliverable_contract,
        verify_declared_deliverables,
    )

    task_md = (
        "# Reconciliation Report\n\n"
        "Write `/task/outputs/report.json` with array `items` containing keys "
        "`id`, `amount`, and `status` (`ok` or `missing`).\n"
    )
    run_dir = tmp_path / "contract_ws"
    (run_dir / "workspace").mkdir(parents=True)
    (run_dir / "outputs").mkdir(parents=True)

    contract = format_deliverable_contract(task_md, run_dir / "workspace")
    assert "[deliverable-contract]" in contract
    assert "/task/outputs/report.json" in contract
    assert "amount" in contract and "status" in contract
    assert CLOSED_WORLD_CONTRACT_RULE in contract

    inv = format_workspace_inventory(run_dir, task_markdown=task_md)
    assert "[deliverable-contract]" in inv
    assert "[cli-quickref]" in inv

    # Write deliverables containing both a formatted numeric outlier ("2,400") and a
    # singleton status enum outlier ("needs_rounding_note") not mentioned in task_markdown.
    report_path = run_dir / "outputs" / "report.json"
    bad_rows = [
        {"id": "INV-1", "amount": 1200, "status": "ok"},
        {"id": "INV-2", "amount": 1500, "status": "ok"},
        {"id": "INV-3", "amount": 1800, "status": "ok"},
        {"id": "INV-4", "amount": 2100, "status": "ok"},
        {"id": "INV-5", "amount": "2,400", "status": "needs_rounding_note"},
    ]
    report_path.write_text(json.dumps({"items": bad_rows}), encoding="utf-8")

    ok_bad, issues_bad = verify_declared_deliverables(
        ["outputs/report.json"],
        task_markdown=task_md,
        workspace_root=run_dir,
    )
    assert ok_bad is False
    joined = "\n".join(issues_bad)
    assert "2,400" in joined
    assert "needs_rounding_note" in joined

    audit_bad = audit_deliverables(
        run_dir,
        ["/task/outputs/report.json"],
        declared_paths=["outputs/report.json"],
        task_markdown=task_md,
    )
    assert audit_bad.should_gate is True
    assert any("2,400" in adv for adv in audit_bad.advisories)
    assert any("needs_rounding_note" in adv for adv in audit_bad.advisories)

    # Clean, uniform rows pass without Q4 advisories.
    good_rows = [
        {"id": f"INV-{i}", "amount": 1000 + i * 200, "status": "ok" if i < 5 else "missing"}
        for i in range(1, 6)
    ]
    report_path.write_text(json.dumps({"items": good_rows}), encoding="utf-8")
    ok_good, issues_good = verify_declared_deliverables(
        ["outputs/report.json"],
        task_markdown=task_md,
        workspace_root=run_dir,
    )
    assert ok_good is True
    assert issues_good == []


def test_structured_data_shape_zero_loss_and_error_tail_first(tmp_path: Path) -> None:
    from ctx.toolresult import compress_tool_result, prepend_structured_data_shape

    rows = [{"sku": f"SKU-{i:03d}", "qty": i + 1, "amount": (i + 1) * 12.5, "note": "x" * 20} for i in range(40)]
    raw_json = json.dumps(rows, indent=2)
    assert len(raw_json) >= 1500

    shaped = prepend_structured_data_shape(raw_json)
    assert shaped.startswith("[data-shape: format=json, rows=40")
    assert "[compute-hint:" in shaped
    assert shaped.endswith(raw_json)  # strictly additive: zero character loss

    # Small outputs stay untouched.
    small_json = json.dumps(rows[:2])
    assert prepend_structured_data_shape(small_json) == small_json

    # ToolHost applies [data-shape] in sj mode on large reads while leaving raw untouched.
    host_sj, _, run_sj, _ = _make_host(tmp_path / "shape_sj", arm="sj", entry_gate_bytes=100_000, baseline_raw_cap=100_000)
    host_raw, _, run_raw, _ = _make_host(tmp_path / "shape_raw", arm="raw", entry_gate_bytes=100_000, baseline_raw_cap=100_000)
    for rdir in (run_sj, run_raw):
        (rdir / "workspace" / "orders_big.json").write_text(raw_json, encoding="utf-8")
    fr_sj, _, _ = host_sj.execute_part("read", {"path": "/task/workspace/orders_big.json"})
    fr_raw, _, _ = host_raw.execute_part("read", {"path": "/task/workspace/orders_big.json"})
    sj_out = fr_sj["functionResponse"]["response"]["output"]
    raw_out = fr_raw["functionResponse"]["response"]["output"]
    assert "[data-shape: format=json, rows=40" in sj_out
    assert "[data-shape:" not in raw_out

    # Error-tail-first surfaces final lines before middle truncation when Bash fails.
    noise = "\n".join(f"log step {i:04d} ok" for i in range(300))
    long_err = f"{noise}\nTraceback (most recent call last):\n  File \"app.py\", line 99\nValueError: FINAL_SENTINEL_ERR"
    compressed = compress_tool_result(
        "Bash",
        long_err,
        max_chars=1200,
        exit_code=1,
        ref="run:deadbeef#stdout",
    )
    assert compressed.startswith("[error-tail-first]")
    assert "ValueError: FINAL_SENTINEL_ERR" in compressed


def test_write_receipt_normalized_bash_cache_entry_gate_and_cli_subproc_err(tmp_path: Path) -> None:
    extra_bin = tmp_path / "cli_bin"
    _write_executable(
        extra_bin / "ntn",
        "#!/usr/bin/env bash\necho 'ntn ok'\n",
    )
    _write_executable(
        extra_bin / "stripe",
        "#!/usr/bin/env bash\necho 'Error: invalid request (HTTP 400 Bad Request)' >&2\nexit 1\n",
    )
    host_sj, _, run_sj, _ = _make_host(tmp_path / "host_sj", arm="sj", extra_bin=extra_bin)
    host_raw, _, _, _ = _make_host(tmp_path / "host_raw", arm="raw", extra_bin=extra_bin)

    # 1. [write-receipt] on write and edit (raw unaffected).
    host_sj.step = 1
    fr_w, _, _ = host_sj.execute_part(
        "write",
        {"path": "/task/workspace/sample.json", "content": json.dumps({"alpha": 1, "beta": 2})},
    )
    w_out = fr_w["functionResponse"]["response"]["output"]
    assert "[write-receipt: lines=1" in w_out and "schema=json-dict keys=[alpha, beta]" in w_out

    fr_e, _, _ = host_sj.execute_part(
        "edit",
        {"path": "/task/workspace/sample.json", "old_string": '"beta": 2', "new_string": '"beta": 3'},
    )
    e_out = fr_e["functionResponse"]["response"]["output"]
    assert "[write-receipt: lines=1" in e_out and "schema=json-dict" in e_out

    fr_rw, _, _ = host_raw.execute_part(
        "write",
        {"path": "/task/workspace/sample.json", "content": '{"alpha": 1}'},
    )
    assert "[write-receipt:" not in fr_rw["functionResponse"]["response"]["output"]

    # 2. Flag-cluster normalization treats `ls -la` and `ls -l -a` at same epoch as cached duplicate.
    host_sj.step = 2
    host_sj.execute_part("bash", {"command": "ls -la /task/workspace"})
    assert host_sj.last_call_was_dup is False
    host_sj.step = 3
    fr_dup, _, _ = host_sj.execute_part("bash", {"command": "ls -l -a /task/workspace"})
    assert host_sj.last_call_was_dup is True
    assert "[straitjacket] Identical call already ran" in fr_dup["functionResponse"]["response"]["output"]

    # 3. Entry Gate blocks synthetic scratch probe on `ntn files create test-file.txt` unless staged in workspace.
    host_sj.step = 4
    fr_probe, _, _ = host_sj.execute_part("bash", {"command": "ntn files create test-file.txt"})
    probe_out = fr_probe["functionResponse"]["response"]["output"]
    assert "[ctx-entry-gate] Blocked synthetic scratch-probe creation" in probe_out

    (run_sj / "workspace" / "real-deliverable.txt").write_text("hello", encoding="utf-8")
    host_sj.step = 5
    fr_real, _, _ = host_sj.execute_part("bash", {"command": "ntn files create real-deliverable.txt"})
    assert "[ctx-entry-gate]" not in fr_real["functionResponse"]["response"]["output"]

    # 4. Q5 subprocess CLI error inside a Python script surfaces `[cli-subprocess-error]`.
    host_sj.step = 6
    py_subproc = (
        "python3 - <<'PY'\n"
        "import subprocess\n"
        "r = subprocess.run(['stripe', 'invoices', 'create'], capture_output=True, text=True)\n"
        "print(r.stderr.strip())\n"
        "PY"
    )
    fr_sub, _, _ = host_sj.execute_part("bash", {"command": py_subproc})
    sub_out = fr_sub["functionResponse"]["response"]["output"]
    assert "[cli-subprocess-error]" in sub_out


def test_browser_batch_step_digests_and_widget_state_attribute_diff() -> None:
    state = {
        "starred": False,
        "page": "inbox",
        "detail": "",
        "status_cls": "idle",
    }

    class FakeLoc:
        def __init__(self, sel: str) -> None:
            self.sel = sel

        @property
        def first(self) -> FakeLoc:
            return self

        def count(self) -> int:
            return 1

        def click(self, **kwargs: Any) -> None:
            if "e1" in self.sel or "star" in self.sel:
                state["starred"] = not state["starred"]
                state["status_cls"] = "starred" if state["starred"] else "idle"
            elif "e2" in self.sel or "open" in self.sel:
                state["page"] = "thread-42"
                state["detail"] = "Quarterly margin target confirmed at 28.5%"

    class FakePage:
        def __init__(self) -> None:
            self.url = "http://127.0.0.1:20610/inbox"

        def is_closed(self) -> bool:
            return False

        def on(self, event: str, handler: Any) -> None:
            pass

        def goto(self, url: str, **kwargs: Any) -> None:
            self.url = url

        def go_back(self, **kwargs: Any) -> None:
            state["page"] = "inbox"
            state["detail"] = ""

        def title(self) -> str:
            return f"Mail - {state['page']}"

        def locator(self, sel: str) -> FakeLoc:
            return FakeLoc(sel)

        def evaluate(self, script: str, *args: Any) -> Any:
            s_val = "true" if state["starred"] else "false"
            d_state = "active" if state["starred"] else "default"
            if state["page"] == "inbox":
                controls = "\n".join(
                    [
                        f'[e1] <button #star-widget data-starred="{s_val}" data-state="{d_state}" state-classes={state["status_cls"]}> Star',
                        "[e2] <button #open-thread> Open thread 42",
                    ]
                )
                body = "Inbox list (1 unread)"
                self.url = "http://127.0.0.1:20610/inbox"
                cnt = 2
            else:
                controls = "[e9] <button #reply-btn> Reply"
                body = f"Thread 42 detail\n{state['detail']}"
                self.url = "http://127.0.0.1:20610/thread/42"
                cnt = 1
            return {
                "url": self.url,
                "title": self.title(),
                "controlsCount": cnt,
                "controls": controls,
                "bodyText": body,
            }

        def wait_for_timeout(self, ms: int) -> None:
            pass

        def wait_for_load_state(self, state_name: str, **kwargs: Any) -> None:
            pass

        def close(self) -> None:
            pass

    class FakeCtx:
        def new_page(self) -> FakePage:
            return FakePage()

        def close(self) -> None:
            pass

    sess = BrowserSession(FakeCtx(), {}, enable_dom_diff=True)
    sess.open_tab("http://127.0.0.1:20610/inbox")
    sess.snapshot("vtab-1")

    # Widget attribute diff tracks data-starred, data-state, and state-classes on single click.
    star_out = sess.click("vtab-1", "e1")
    assert "[dom-diff]" in star_out
    assert "data-starred=false->true" in star_out
    assert "data-state=default->active" in star_out
    assert "classes=idle->starred" in star_out

    # browser_batch with include_step_text=True emits compact per-step digests even across click -> back.
    batch_out = sess.batch(
        "vtab-1",
        [
            {"action": "click", "ref": "e2"},
            {"action": "read"},
            {"action": "back"},
        ],
        include_step_text=True,
    )
    assert "[Batch 3/3 steps on vtab-1]" in batch_out
    assert "[step 1 (click(e2)):" in batch_out
    assert "Quarterly margin target confirmed at 28.5%" in batch_out
    assert "[step 3 (back):" in batch_out
    assert "url=http://127.0.0.1:20610/inbox" in batch_out
    sess.close()


def test_loop_turn0_contract_q3_per_service_gates_and_residence_wiring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ctx.residence import WatermarkResidence

    ds_root = tmp_path / "cab_q3"
    _create_synthetic_task(ds_root, category="cli", task_id="cli-multi-svc")

    extra_bin = tmp_path / "cli_bin"
    _write_executable(
        extra_bin / "stripe",
        (
            "#!/usr/bin/env bash\n"
            'if [ "$1" = "invoices" ] && [ "$2" = "create" ]; then echo "inv_1"; '
            'elif [ "$1" = "invoices" ] && [ "$2" = "list" ]; then echo "inv_1 paid"; fi\n'
        ),
    )
    _write_executable(
        extra_bin / "jira",
        (
            "#!/usr/bin/env bash\n"
            'if [ "$1" = "issue" ] && [ "$2" = "create" ]; then echo "PROJ-10"; '
            'elif [ "$1" = "issue" ] && [ "$2" = "list" ]; then echo "PROJ-10 open"; fi\n'
        ),
    )
    monkeypatch.setenv("PATH", f"{extra_bin}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}")

    # Spy on WatermarkResidence.maybe_compact kwargs to verify loop.py C1/C2 wiring.
    compact_calls: list[dict[str, Any]] = []
    orig_maybe_compact = WatermarkResidence.maybe_compact

    def spy_maybe_compact(
        self: WatermarkResidence,
        contents: list[dict[str, Any]],
        *,
        remaining_steps: int | None = None,
        has_final_output_write: bool = False,
        min_turn_gap: int = 6,
        deliverable_contract: str = "",
    ) -> tuple[int, int]:
        compact_calls.append(
            {
                "has_final_output_write": has_final_output_write,
                "min_turn_gap": min_turn_gap,
                "deliverable_contract": deliverable_contract,
                "remaining_steps": remaining_steps,
            }
        )
        return orig_maybe_compact(
            self,
            contents,
            remaining_steps=remaining_steps,
            has_final_output_write=has_final_output_write,
            min_turn_gap=min_turn_gap,
            deliverable_contract=deliverable_contract,
        )

    monkeypatch.setattr(WatermarkResidence, "maybe_compact", spy_maybe_compact)

    class MultiServiceGateProvider:
        def __init__(self) -> None:
            self.turn = 0
            self.first_prompt = ""
            self.saw_jira_gate = False

        def generate(
            self,
            model_id: str,
            contents: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            **kwargs: Any,
        ) -> dict[str, Any]:
            self.turn += 1
            if self.turn == 1:
                self.first_prompt = contents[0]["parts"][0]["text"]
                parts: list[dict[str, Any]] = [
                    {"functionCall": {"name": "bash", "args": {"command": "stripe invoices create"}}},
                ]
            elif self.turn == 2:
                parts = [{"functionCall": {"name": "bash", "args": {"command": "jira issue create"}}}]
            elif self.turn == 3:
                # Verify ONLY stripe -> jira remains in unverified_mutated_services()!
                parts = [{"functionCall": {"name": "bash", "args": {"command": "stripe invoices list"}}}]
            elif self.turn == 4:
                parts = [
                    {
                        "functionCall": {
                            "name": "write",
                            "args": {
                                "path": "/task/outputs/summary.json",
                                "content": json.dumps({"total_usd": 420, "status": "verified"}),
                            },
                        }
                    }
                ]
            elif self.turn == 5:
                # Premature completed_text attempt while `jira` is still unverified.
                parts = [{"text": "Done! Created invoice and Jira issue."}]
            elif self.turn == 6:
                last_msg = contents[-1]["parts"][0].get("text", "")
                assert "[ctx-emission-gate]" in last_msg
                assert "jira" in last_msg
                assert "stripe" not in last_msg.split("without a subsequent read-only check for:", 1)[-1]
                self.saw_jira_gate = True
                parts = [{"functionCall": {"name": "bash", "args": {"command": "jira issue list"}}}]
            else:
                parts = [{"functionCall": {"name": "present_files", "args": {"files": ["/task/outputs/summary.json"]}}}]

            return {
                "candidates": [{"finishReason": "STOP", "content": {"role": "model", "parts": parts}}],
                "usageMetadata": {"promptTokenCount": 600, "candidatesTokenCount": 30},
            }

        def judge_access_token(self) -> str:
            return ""

        def close(self) -> None:
            pass

    cfg = BenchConfig(
        allow_unsandboxed=True,  # explicit opt-in: CI has no bwrap
        dataset_root=ds_root,
        workdir=tmp_path / "q3_work",
        provider="gemini",
        api_key="dummy",
        judge_provider="mock",
        arms=("sj",),
    )
    prov = MultiServiceGateProvider()
    res = run_trial(cfg, "cli-multi-svc", "sj", 91, prov)
    assert res.passed is True
    assert res.termination == "present_files"
    assert res.emission_gate_hits == 1
    assert prov.saw_jira_gate is True
    assert "[deliverable-contract]" in prov.first_prompt
    assert "[cli-quickref]" in prov.first_prompt

    assert len(compact_calls) >= 5
    # Before output write (turns 1..3), has_final_output_write is False; after write (turn 4+), it is True.
    assert any(c["has_final_output_write"] is False for c in compact_calls[:3])
    assert any(c["has_final_output_write"] is True for c in compact_calls[3:])
    assert all(c["min_turn_gap"] == 6 for c in compact_calls)
    assert all("[deliverable-contract]" in c["deliverable_contract"] for c in compact_calls)

    # Also confirm present_files blocks when an unverified service remains.
    ds_pf = tmp_path / "cab_q3_pf"
    _create_synthetic_task(ds_pf, category="cli", task_id="cli-pf-svc")

    class PresentFilesGateProvider:
        def __init__(self) -> None:
            self.turn = 0
            self.saw_pf_gate = False

        def generate(
            self,
            model_id: str,
            contents: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            **kwargs: Any,
        ) -> dict[str, Any]:
            self.turn += 1
            if self.turn == 1:
                parts: list[dict[str, Any]] = [
                    {"functionCall": {"name": "bash", "args": {"command": "jira issue create"}}},
                ]
            elif self.turn == 2:
                parts = [
                    {
                        "functionCall": {
                            "name": "write",
                            "args": {
                                "path": "/task/outputs/summary.json",
                                "content": json.dumps({"total_usd": 420, "status": "verified"}),
                            },
                        }
                    }
                ]
            elif self.turn == 3:
                parts = [{"functionCall": {"name": "present_files", "args": {"files": ["/task/outputs/summary.json"]}}}]
            elif self.turn == 4:
                fr_out = contents[-1]["parts"][0]["functionResponse"]["response"]["output"]
                assert "[ctx-emission-gate]" in fr_out and "jira" in fr_out
                self.saw_pf_gate = True
                parts = [{"functionCall": {"name": "bash", "args": {"command": "jira issue list"}}}]
            else:
                parts = [{"functionCall": {"name": "present_files", "args": {"files": ["/task/outputs/summary.json"]}}}]
            return {
                "candidates": [{"finishReason": "STOP", "content": {"role": "model", "parts": parts}}],
                "usageMetadata": {"promptTokenCount": 500, "candidatesTokenCount": 25},
            }

        def judge_access_token(self) -> str:
            return ""

        def close(self) -> None:
            pass

    cfg_pf = BenchConfig(
        allow_unsandboxed=True,  # explicit opt-in: CI has no bwrap
        dataset_root=ds_pf,
        workdir=tmp_path / "q3_pf_work",
        provider="gemini",
        api_key="dummy",
        judge_provider="mock",
        arms=("sj",),
    )
    pf_prov = PresentFilesGateProvider()
    res_pf = run_trial(cfg_pf, "cli-pf-svc", "sj", 92, pf_prov)
    assert res_pf.passed is True
    assert res_pf.termination == "present_files"
    assert res_pf.emission_gate_hits == 1
    assert pf_prov.saw_pf_gate is True


def test_wave3_deliverables_conditional_slots_manifest_and_dual_ratio(tmp_path: Path) -> None:
    """Fix 1 checks: conditional non-remove sibling fields, numbered slots, manifest alias, and dual ratio."""
    from ctx.deliverables import audit_deliverables, format_deliverable_contract

    # 1. Conditional non-empty sibling-field check (non-remove row CLM-002 missing replacement_text).
    run1 = tmp_path / "case_cond"
    (run1 / "outputs").mkdir(parents=True)
    claims_bad = {
        "claims": [
            {"claim_id": "CLM-001", "decision": "modify", "replacement_text": "Verified 10%"},
            {"claim_id": "CLM-002", "decision": "modify", "replacement_text": ""},
            {"claim_id": "CLM-003", "decision": "rewrite", "replacement_text": "Certified ISO"},
            {"claim_id": "CLM-004", "decision": "remove", "replacement_text": ""},
        ]
    }
    (run1 / "outputs" / "claims.json").write_text(json.dumps(claims_bad), encoding="utf-8")
    audit_bad = audit_deliverables(run1, ["/task/outputs/claims.json"], declared_paths=["outputs/claims.json"])
    assert audit_bad.should_gate is True
    assert any("CLM-002" in iss and "replacement_text" in iss for iss in audit_bad.advisories)

    claims_bad["claims"][1]["replacement_text"] = "Updated tested copy"
    (run1 / "outputs" / "claims.json").write_text(json.dumps(claims_bad), encoding="utf-8")
    audit_ok = audit_deliverables(run1, ["/task/outputs/claims.json"], declared_paths=["outputs/claims.json"])
    assert audit_ok.should_gate is False

    # 2. Numbered slots + workspace candidate images, plus manifest alias check.
    run2 = tmp_path / "case_slots"
    (run2 / "outputs").mkdir(parents=True)
    (run2 / "workspace" / "assets").mkdir(parents=True)
    for idx in range(1, 5):
        (run2 / "workspace" / "assets" / f"photo_{idx}.jpg").write_bytes(b"img")
    (run2 / "outputs" / "listing.json").write_text(
        json.dumps(
            {
                "image_1": "photo_1.jpg",
                "image_2": "photo_2.jpg",
                "image_3": "",
                "image_4": "",
            }
        ),
        encoding="utf-8",
    )
    (run2 / "outputs" / "manifest.json").write_text(
        json.dumps(
            {
                "navigation_sections": [{"id": "s1"}, {"id": "s2"}, {"id": "s3"}],
            }
        ),
        encoding="utf-8",
    )
    audit_slot_manifest = audit_deliverables(
        run2,
        ["/task/outputs/listing.json", "/task/outputs/manifest.json"],
        declared_paths=["outputs/listing.json", "outputs/manifest.json"],
        task_markdown="Write outputs/manifest.json with top-level sections and backend_required=false.",
    )
    assert audit_slot_manifest.should_gate is True
    assert any("image_3" in iss and "4 candidate image" in iss for iss in audit_slot_manifest.advisories)
    assert any("'sections' is missing" in iss for iss in audit_slot_manifest.advisories)
    assert any("backend_required" in iss for iss in audit_slot_manifest.advisories)

    # 3. Deliverable contract audit-precision dual-ratio reminder.
    contract = format_deliverable_contract(
        "Audit all supplier claims and write /task/outputs/reconciliation.json",
        workspace_root=run1,
    )
    assert "audit-precision" in contract
    assert "74.4% (29/39)" in contract


def test_wave3_host_service_delta_entry_gate_and_cli_loop_hint(tmp_path: Path) -> None:
    """Fixes 2a & 2b: Jira/Notion SQLite service-delta audit, inline ntn probe gate, and 3-turn CLI hint."""
    import sqlite3

    run_dir = tmp_path / "run_svc"
    meta_dir = tmp_path / "meta_svc"
    (run_dir / "workspace").mkdir(parents=True)
    jira_dir = meta_dir / "mock_state" / "jira_cli"
    ntn_dir = meta_dir / "mock_state" / "notion_cli"
    jira_dir.mkdir(parents=True)
    ntn_dir.mkdir(parents=True)

    jira_db = jira_dir / "jira_mock.db"
    jcon = sqlite3.connect(jira_db)
    jcon.execute("CREATE TABLE issue_statuses (id INTEGER PRIMARY KEY, name TEXT, category TEXT)")
    jcon.execute("CREATE TABLE issues (key TEXT PRIMARY KEY, status_id INTEGER)")
    jcon.execute("CREATE TABLE issue_links (inward_key TEXT, outward_key TEXT, link_type TEXT)")
    jcon.executemany(
        "INSERT INTO issue_statuses VALUES (?, ?, ?)",
        [(1, "To Do", "new"), (2, "Done", "done")],
    )
    jcon.executemany("INSERT INTO issues VALUES (?, ?)", [("PROJ-35", 1), ("PROJ-8", 2)])
    jcon.commit()
    jcon.close()

    ntn_db = ntn_dir / "mock.db"
    ncon = sqlite3.connect(ntn_db)
    ncon.execute("CREATE TABLE entities (id TEXT, type TEXT, data TEXT)")
    ncon.execute(
        "INSERT INTO entities VALUES (?, ?, ?)",
        ("page_root", "pages", json.dumps({"title": "Root"})),
    )
    ncon.commit()
    ncon.close()

    metrics_sj = RunMetrics(
        task_id="cli-svc-delta",
        task_rel="cli/cli-svc-delta",
        category="cli",
        arm="sj",
        model_id="mock-model",
    )
    host_sj = ToolHost(
        BenchConfig(dataset_root=tmp_path, workdir=tmp_path, judge_provider="mock"),
        run_dir,
        meta_dir,
        {},
        {"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        metrics_sj,
        None,
        arm="sj",
        task_markdown="Link duplicate defects to PROJ-8 and create a Notion handoff page (do not upload files).",
    )

    # Mutate Jira (duplicate link + Duplicates still To Do) and Notion (orphan page + probe file).
    jcon2 = sqlite3.connect(jira_db)
    jcon2.executemany(
        "INSERT INTO issue_links VALUES (?, ?, ?)",
        [("PROJ-35", "PROJ-8", "Duplicates"), ("PROJ-35", "PROJ-8", "Duplicates")],
    )
    jcon2.commit()
    jcon2.close()

    ncon2 = sqlite3.connect(ntn_db)
    ncon2.execute(
        "INSERT INTO entities VALUES (?, ?, ?)",
        ("p_new", "pages", json.dumps({"title": "Orphan Handoff", "parentId": None})),
    )
    ncon2.execute(
        "INSERT INTO entities VALUES (?, ?, ?)",
        ("f_probe", "files", json.dumps({"filename": "test_probe.txt"})),
    )
    ncon2.commit()
    ncon2.close()

    blocking, issues, receipt = host_sj.audit_service_state_delta()
    assert blocking is True
    assert receipt.startswith("[service-delta:")
    assert any("duplicate issue link" in iss.lower() and "PROJ-35->PROJ-8" in iss for iss in issues)
    assert any("not transitioned to 'Done'" in iss and "PROJ-35" in iss for iss in issues)
    assert any("parentId=None" in iss for iss in issues)
    assert any("synthetic probe" in iss.lower() or "unrequested uploaded file" in iss.lower() for iss in issues)

    def _bash(cmd: str) -> str:
        fr, _, _ = host_sj.execute_part("bash", {"command": cmd})
        return str(fr["functionResponse"]["response"]["output"])

    # Inline Python subprocess ntn files upload of synthetic probe name must be blocked by entry gate.
    blocked_out = _bash(
        "python3 - <<'PY'\nimport subprocess\nsubprocess.run(['ntn', 'files', 'create', 'test_dummy.txt'])\nPY"
    )
    assert blocked_out.startswith("[ctx-entry-gate]")

    # 3 consecutive single-item CLI bash turns trigger [batch-compute-hint:] in sj mode, not raw.
    host_sj.step = 1
    assert "[batch-compute-hint:" not in _bash("dws --help >/dev/null || true")
    host_sj.step = 2
    assert "[batch-compute-hint:" not in _bash("echo dws doc 1")
    for idx, step_n in enumerate((3, 4, 5), start=1):
        host_sj.step = step_n
        out = _bash(f"dws doc get id_{idx} 2>&1 || true")
        if idx < 3:
            assert "[batch-compute-hint:" not in out
        else:
            assert "[batch-compute-hint:" in out

    (tmp_path / "run_raw" / "workspace").mkdir(parents=True)
    (tmp_path / "meta_raw").mkdir(parents=True)
    host_raw = ToolHost(
        BenchConfig(dataset_root=tmp_path, workdir=tmp_path, judge_provider="mock"),
        tmp_path / "run_raw",
        tmp_path / "meta_raw",
        {},
        {"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        RunMetrics(
            task_id="cli-raw",
            task_rel="cli/cli-raw",
            category="cli",
            arm="raw",
            model_id="mock-model",
        ),
        None,
        arm="raw",
    )
    assert host_raw.audit_service_state_delta() == (False, [], "")


def test_wave3_sandbox_box_item_id_and_notion_port(tmp_path: Path) -> None:
    """Fix 4: staged box-bench exposes item_id fallback and Notion runtime port detection strips env collisions."""
    from evals.acciobench.sandbox import _patch_staged_mock_services, _spec_uses_notion

    staged = tmp_path / "mock_services"
    box_bin = staged / "box_cli" / "bin" / "box-bench"
    box_bin.parent.mkdir(parents=True)
    box_bin.write_text(
        "function dump() {\n  return {\n    tasks: db.prepare('SELECT * FROM tasks WHERE is_deleted = 0').all(),\n  };\n}\n",
        encoding="utf-8",
    )
    _patch_staged_mock_services(staged)
    _patch_staged_mock_services(staged)
    patched = box_bin.read_text(encoding="utf-8")
    assert patched.count("item_id: t.item_id ?? t.file_id") == 1

    case_dir = tmp_path / "cli-ntn-task"
    (case_dir / "services").mkdir(parents=True)
    (case_dir / "services" / "start_services.sh").write_text("#!/bin/bash\nntn-daemon-start\n", encoding="utf-8")
    assert _spec_uses_notion(object(), case_dir=case_dir) is True

    case_none = tmp_path / "cli-plain-task"
    case_none.mkdir()
    assert _spec_uses_notion(object(), case_dir=case_none) is False


def test_wave3_schemas_protocol_and_loop_service_delta_gates(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """Wave 3 schemas & loop wiring: protocol text plus service-delta/UI gate caps (<2) at present_files & completed_text."""
    from evals.acciobench.schemas import SJ_EXECUTION_PROTOCOL

    assert "Parent/Child" in SJ_EXECUTION_PROTOCOL
    assert "[row_id=" in SJ_EXECUTION_PROTOCOL and "labels=[...]" in SJ_EXECUTION_PROTOCOL
    assert "Apply" in SJ_EXECUTION_PROTOCOL

    class _FakeBrowser:
        def __init__(self, ui_sequence: list[list[str]]) -> None:
            self._seq = list(ui_sequence)

        def check_uncommitted_ui_state(self) -> list[str]:
            return self._seq.pop(0) if self._seq else []

    # Case 1: present_files blocks up to max 2 service_delta_gate_hits when blocking issues persist, then allows exit on 3rd.
    ds_pf = tmp_path / "cab_w3_pf"
    _create_synthetic_task(ds_pf, category="cli", task_id="cli-w3-pf")
    audit_calls: list[int] = []

    def _always_blocking_audit(self: ToolHost) -> tuple[bool, list[str], str]:
        audit_calls.append(1)
        return (
            True,
            ["Jira duplicate link PROJ-35->PROJ-8 found"],
            "[service-delta: jira: links=2(+2) done=0(+0)]",
        )

    monkeypatch.setattr(ToolHost, "audit_service_state_delta", _always_blocking_audit)

    class _PfGateCapProvider:
        def __init__(self) -> None:
            self.turn = 0
            self.seen_gate_outputs: list[str] = []

        def generate(self, model_id: str, contents: list[dict[str, Any]], tools: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
            self.turn += 1
            if self.turn == 1:
                parts: list[dict[str, Any]] = [
                    {
                        "functionCall": {
                            "name": "write",
                            "args": {
                                "path": "/task/outputs/summary.json",
                                "content": json.dumps({"total_usd": 420, "status": "ok"}),
                            },
                        }
                    }
                ]
            else:
                if self.turn >= 3:
                    fr = contents[-1]["parts"][0]["functionResponse"]["response"]["output"]
                    self.seen_gate_outputs.append(fr)
                parts = [
                    {"functionCall": {"name": "present_files", "args": {"files": ["/task/outputs/summary.json"]}}}
                ]
            return {
                "candidates": [{"finishReason": "STOP", "content": {"role": "model", "parts": parts}}],
                "usageMetadata": {"promptTokenCount": 400, "candidatesTokenCount": 20},
            }

        def judge_access_token(self) -> str:
            return ""

        def close(self) -> None:
            pass

    cfg_pf = BenchConfig(
        allow_unsandboxed=True,  # explicit opt-in: CI has no bwrap
        dataset_root=ds_pf,
        workdir=tmp_path / "w3_pf_work",
        provider="gemini",
        api_key="dummy",
        judge_provider="mock",
        arms=("sj",),
    )
    pf_prov = _PfGateCapProvider()
    res_pf = run_trial(cfg_pf, "cli-w3-pf", "sj", 93, pf_prov)
    assert res_pf.termination == "present_files"
    assert res_pf.emission_gate_hits == 2
    assert len(pf_prov.seen_gate_outputs) == 2
    assert all("[service-delta:" in txt and "PROJ-35->PROJ-8" in txt for txt in pf_prov.seen_gate_outputs)

    # Case 2: completed_text gate triggers both service-delta and browser check_uncommitted_ui_state even if emission_gated_once=True.
    ds_txt = tmp_path / "cab_w3_txt"
    _create_synthetic_task(ds_txt, category="cli", task_id="cli-w3-txt")
    monkeypatch.setattr(
        ToolHost,
        "audit_service_state_delta",
        lambda self: (False, [], "[service-delta: ntn: +pages=1(orphan=0)]"),
    )
    fake_browser = _FakeBrowser(
        [
            ["Open label menu has unclicked Apply button"],
            [],
        ]
    )
    orig_host_init = ToolHost.__init__

    def _patched_host_init(self: ToolHost, *args: Any, **kwargs: Any) -> None:
        orig_host_init(self, *args, **kwargs)
        self.browser = fake_browser  # type: ignore[assignment]
        self.emission_gated_once = True

    monkeypatch.setattr(ToolHost, "__init__", _patched_host_init)

    class _TxtGateProvider:
        def __init__(self) -> None:
            self.turn = 0
            self.saw_ui_gate = False

        def generate(self, model_id: str, contents: list[dict[str, Any]], tools: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
            self.turn += 1
            if self.turn == 1:
                parts: list[dict[str, Any]] = [
                    {
                        "functionCall": {
                            "name": "write",
                            "args": {
                                "path": "/task/outputs/summary.json",
                                "content": json.dumps({"total_usd": 420, "status": "ok"}),
                            },
                        }
                    }
                ]
            elif self.turn == 2:
                parts = [{"text": "Finished labeling threads."}]
            else:
                nudge = contents[-1]["parts"][0].get("text", "")
                assert "[uncommitted-ui-state:" in nudge
                assert "Apply" in nudge
                assert "[service-delta:" in nudge
                self.saw_ui_gate = True
                parts = [{"text": "Applied label and verified."}]
            return {
                "candidates": [{"finishReason": "STOP", "content": {"role": "model", "parts": parts}}],
                "usageMetadata": {"promptTokenCount": 400, "candidatesTokenCount": 20},
            }

        def judge_access_token(self) -> str:
            return ""

        def close(self) -> None:
            pass

    cfg_txt = BenchConfig(
        allow_unsandboxed=True,  # explicit opt-in: CI has no bwrap
        dataset_root=ds_txt,
        workdir=tmp_path / "w3_txt_work",
        provider="gemini",
        api_key="dummy",
        judge_provider="mock",
        arms=("sj",),
    )
    txt_prov = _TxtGateProvider()
    res_txt = run_trial(cfg_txt, "cli-w3-txt", "sj", 94, txt_prov)
    assert res_txt.termination == "completed_text"
    assert res_txt.emission_gate_hits == 1
    assert txt_prov.saw_ui_gate is True


def test_wave4_loop_collect_service_and_ui_gate_check_exit() -> None:
    """Wave 4 loop wiring: _collect_service_and_ui_gate passes check_exit=True with fallback & handles str/list returns."""
    from evals.acciobench.loop import _collect_service_and_ui_gate

    class _BrowserWithCheckExitStr:
        def __init__(self) -> None:
            self.calls: list[bool] = []

        def check_uncommitted_ui_state(self, *, check_exit: bool = False) -> str | None:
            self.calls.append(check_exit)
            return "[uncommitted-ui-state: missing label on shp-001-brief]"

    class _BrowserWithEmptyStr:
        def __init__(self) -> None:
            self.calls: list[bool] = []

        def check_uncommitted_ui_state(self, *, check_exit: bool = False) -> str | None:
            self.calls.append(check_exit)
            return None

    class _LegacyMockBrowser:
        def __init__(self) -> None:
            self.called = False

        def check_uncommitted_ui_state(self) -> list[str]:
            self.called = True
            return ["Open Draft editor has unclicked Save"]

    class _Host:
        def __init__(self, browser: Any, delta: tuple[bool, list[str], str] = (False, [], "")) -> None:
            self.browser = browser
            self._delta = delta

        def audit_service_state_delta(self) -> tuple[bool, list[str], str]:
            return self._delta

    b_str = _BrowserWithCheckExitStr()
    blocking, issues, receipt = _collect_service_and_ui_gate(_Host(b_str))
    assert b_str.calls == [True]
    assert blocking is True
    assert issues == ["[uncommitted-ui-state: missing label on shp-001-brief]"]
    assert receipt == "[uncommitted-ui-state: missing label on shp-001-brief]"

    b_none = _BrowserWithEmptyStr()
    blocking_ok, issues_ok, receipt_ok = _collect_service_and_ui_gate(_Host(b_none))
    assert b_none.calls == [True]
    assert blocking_ok is False
    assert issues_ok == []
    assert receipt_ok == ""

    b_legacy = _LegacyMockBrowser()
    blocking_leg, issues_leg, receipt_leg = _collect_service_and_ui_gate(
        _Host(b_legacy, (False, [], "[service-delta: gmail=1]"))
    )
    assert b_legacy.called is True
    assert blocking_leg is True
    assert issues_leg == ["Open Draft editor has unclicked Save"]
    assert "[service-delta: gmail=1]" in receipt_leg
    assert "[uncommitted-ui-state: Open Draft editor has unclicked Save]" in receipt_leg


def test_wave4_host_cross_fk_stripe_dws_todoist_jira_gdocs(tmp_path: Path) -> None:
    ds_root = tmp_path / "cab"
    ds_root.mkdir(parents=True, exist_ok=True)
    cfg = BenchConfig(dataset_root=ds_root, workdir=tmp_path / "work")

    def _build_host(
        name: str,
        *,
        arm: str = "sj",
        task_md: str = "",
        setup_fn: Any = None,
    ) -> tuple[ToolHost, Path, Path]:
        run_dir = cfg.runs_dir / f"{name}__{arm}"
        meta_dir = cfg.runs_dir / f"{name}__{arm}_meta"
        (run_dir / "workspace").mkdir(parents=True, exist_ok=True)
        (run_dir / "outputs").mkdir(parents=True, exist_ok=True)
        meta_dir.mkdir(parents=True, exist_ok=True)
        if setup_fn is not None:
            setup_fn(run_dir, meta_dir)
        metrics = RunMetrics(
            task_id=f"cli-{name}",
            task_rel=f"cli/{name}",
            category="cli",
            arm=arm,
            model_id="test-model",
        )
        host = ToolHost(
            cfg,
            run_dir,
            meta_dir,
            {3000: 20100},
            dict(os.environ),
            metrics,
            None,
            arm=arm,
            task_markdown=task_md,
            use_bwrap=False,
        )
        return host, run_dir, meta_dir

    # (a)+(e)+(f) Setup helper for Jira, Box, Google Docs, Stripe, DWS, Todoist states
    def _seed_dirty(run_dir: Path, meta_dir: Path) -> None:
        mstate = meta_dir / "mock_state"
        # Jira DB with active PROJ-1 (High, Open, SupplierAlpha), PROJ-2 (High, Open, SupplierBeta), deleted PROJ-99
        jdb = mstate / "jira_cli" / "jira_mock.db"
        jdb.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(jdb) as conn:
            conn.execute(
                "CREATE TABLE issues (key TEXT PRIMARY KEY, summary TEXT, status_id INTEGER, priority TEXT, labels TEXT, is_deleted INTEGER DEFAULT 0)"
            )
            conn.execute(
                "CREATE TABLE issue_statuses (id INTEGER PRIMARY KEY, name TEXT, category TEXT)"
            )
            conn.execute(
                "CREATE TABLE issue_links (inward_key TEXT, outward_key TEXT, link_type TEXT)"
            )
            conn.executemany(
                "INSERT INTO issue_statuses (id, name, category) VALUES (?, ?, ?)",
                [(1, "Open", "open"), (2, "Done", "done")],
            )
            conn.executemany(
                "INSERT INTO issues (key, summary, status_id, priority, labels, is_deleted) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    ("PROJ-1", "SupplierAlpha defect", 1, "High", "SupplierAlpha", 0),
                    ("PROJ-2", "SupplierBeta defect", 1, "High", "SupplierBeta", 0),
                    ("PROJ-99", "Deleted defect", 1, "High", "SupplierAlpha", 1),
                ],
            )
            conn.commit()

        # Box DB with active folder/file and missing stale 179439999999 referenced in output
        bdb = mstate / "box_cli" / ".box" / "box_mock.db"
        bdb.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(bdb) as conn:
            conn.execute("CREATE TABLE folders (id TEXT PRIMARY KEY, name TEXT, is_deleted INTEGER DEFAULT 0)")
            conn.execute("CREATE TABLE files (id TEXT PRIMARY KEY, name TEXT, is_deleted INTEGER DEFAULT 0)")
            conn.execute("CREATE TABLE shared_links (id TEXT PRIMARY KEY, is_deleted INTEGER DEFAULT 0)")
            conn.execute("CREATE TABLE collaborations (id TEXT PRIMARY KEY, is_deleted INTEGER DEFAULT 0)")
            conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, is_deleted INTEGER DEFAULT 0)")
            conn.execute("INSERT INTO folders (id, name, is_deleted) VALUES ('179430000001', 'SupplierDocs', 0)")
            conn.commit()

        # Stripe DB with forbidden synthetic 'myproduct' and <=1 webhook enabled_events
        sdb = mstate / "stripe_cli" / "stripe_mock.sqlite"
        sdb.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(sdb) as conn:
            conn.execute("CREATE TABLE objects (id TEXT PRIMARY KEY, resource TEXT, data_json TEXT, deleted INTEGER DEFAULT 0)")
            conn.execute(
                "INSERT INTO objects (id, resource, data_json, deleted) VALUES (?, ?, ?, 0)",
                ("prod_1", "products", json.dumps({"id": "prod_1", "name": "myproduct"})),
            )
            conn.execute(
                "INSERT INTO objects (id, resource, data_json, deleted) VALUES (?, ?, ?, 0)",
                ("we_1", "webhook_endpoints", json.dumps({"id": "we_1", "url": "https://example.com/wh", "enabled_events": ["invoice.paid"]})),
            )
            conn.commit()

        # DWS JSON with files=0 while workspace has CSVs requiring upload
        dws_state = mstate / "dws_doc_cli" / "state.json"
        dws_state.parent.mkdir(parents=True, exist_ok=True)
        dws_state.write_text(json.dumps({"files": 0}), encoding="utf-8")
        (run_dir / "workspace" / "supplier_metrics.csv").write_text("sku,score\nA,1\n", encoding="utf-8")

        # Todoist JSON missing handoff label and Re-source process-gap / spec-gap labels
        td_state = mstate / "todoist_cli" / "state.json"
        td_state.parent.mkdir(parents=True, exist_ok=True)
        td_state.write_text(
            json.dumps(
                {
                    "sections": [{"id": "s1", "name": "Handoff"}, {"id": "s2", "name": "Re-source"}],
                    "items": [
                        {"content": "Weekly Handoff Note", "description": "", "section_id": "s1", "labels": ["general"]},
                        {
                            "content": "Silicone Boot",
                            "description": "Compression-mould quote vs LSR; Class 2A thread tolerance unverified.",
                            "section_id": "s2",
                            "labels": ["capability-gap"],
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )

        # Google Docs rollup state under mock_workdir/tmp/runtime_mock_state/google_docs_mock/state.json
        gdocs_state = meta_dir / "mock_workdir" / "tmp" / "runtime_mock_state" / "google_docs_mock" / "state.json"
        gdocs_state.parent.mkdir(parents=True, exist_ok=True)
        gdocs_state.write_text(
            json.dumps(
                {
                    "files": [
                        {
                            "id": "doc_a",
                            "name": "SupplierAlpha — Q4 Audit",
                            "type": "document",
                            "content": {
                                "blocks": [
                                    {"type": "paragraph", "text": "Finding 1: Severity: High | Fire suppression system is non-operational | Status: Open"}
                                ]
                            },
                        },
                        {
                            "id": "doc_b",
                            "name": "SupplierBeta — Q4 Audit",
                            "type": "document",
                            "content": {
                                "blocks": [
                                    {"type": "paragraph", "text": "Finding 1: Severity: High | EU RoHS regulatory violation | Status: Open"}
                                ]
                            },
                        },
                        {
                            "id": "doc1",
                            "name": "Supplier Risk Rollup",
                            "type": "document",
                            "content": {
                                "blocks": [
                                    {"type": "paragraph", "text": "SupplierAlpha | High+Open: 0"}
                                ]
                            },
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )

        # Output artifact referencing active PROJ-1, deleted PROJ-99, nonexistent PROJ-404, stale Box 179439999999
        (run_dir / "outputs" / "triage.md").write_text(
            "Valid PROJ-1, deleted PROJ-99, missing PROJ-404, stale box 179439999999, valid box 179430000001.\n",
            encoding="utf-8",
        )

    dirty_task_md = (
        "# Combined External Hygiene Audit\n"
        "Audit Stripe lifecycle trial invoice subscription webhooks without synthetic products.\n"
        "Upload supplier_metrics.csv to DWS dossier (上传 DWS 档案).\n"
        "Create Todoist handoff follow-up tasks tagged process-gap and spec-gap.\n"
        "Link duplicate Jira defects using Duplicates link type.\n"
        "Roll up High+Open Jira issues per supplier into Google Docs.\n"
    )

    host_sj, run_sj, _ = _build_host("dirty", arm="sj", task_md=dirty_task_md, setup_fn=_seed_dirty)
    host_raw, _, _ = _build_host("dirty_raw", arm="raw", task_md=dirty_task_md, setup_fn=_seed_dirty)

    # (b) Entry gate blocks unrequested `stripe trigger` on sj, allows on raw or when explicitly requested
    gate_msg = host_sj._check_bash_entry_gate("stripe trigger invoice.created")
    assert gate_msg is not None and "myproduct" in gate_msg
    assert host_raw._check_bash_entry_gate("stripe trigger invoice.created") is None

    explicit_host, _, _ = _build_host("explicit_trigger", arm="sj", task_md="Run `stripe trigger invoice.created`.")
    assert explicit_host._check_bash_entry_gate("stripe trigger invoice.created") is None

    # Mock clean service activity baseline check? Run audit_service_state_delta directly
    blocking, issues, receipt = host_sj.audit_service_state_delta()
    joined = "\n".join(issues)
    assert blocking is True
    assert "PROJ-99" in joined
    assert "PROJ-404" in joined
    assert "179439999999" in joined
    box_fk_line = next(ln for ln in issues if "179439999999" in ln)
    assert "179430000001" not in box_fk_line.split("box_mock.db:")[0]
    assert "myproduct" in joined.lower()
    assert "enabled_events" in joined or "webhook" in joined.lower()
    assert "dws" in joined.lower()
    assert "process-gap" in joined and "spec-gap" in joined
    assert "duplicate" in joined.lower()
    assert "SupplierBeta" in joined or "High+Open" in joined

    # Clean state produces zero false-positive Wave 4 findings.
    def _seed_clean(run_dir: Path, meta_dir: Path) -> None:
        mstate = meta_dir / "mock_state"
        jdb = mstate / "jira_cli" / "jira_mock.db"
        jdb.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(jdb) as conn:
            conn.execute(
                "CREATE TABLE issues (key TEXT PRIMARY KEY, summary TEXT, status_id INTEGER, priority TEXT, labels TEXT, is_deleted INTEGER DEFAULT 0)"
            )
            conn.execute(
                "CREATE TABLE issue_statuses (id INTEGER PRIMARY KEY, name TEXT, category TEXT)"
            )
            conn.execute(
                "CREATE TABLE issue_links (inward_key TEXT, outward_key TEXT, link_type TEXT)"
            )
            conn.executemany(
                "INSERT INTO issue_statuses (id, name, category) VALUES (?, ?, ?)",
                [(1, "Open", "open"), (2, "Done", "done")],
            )
            conn.executemany(
                "INSERT INTO issues (key, summary, status_id, priority, labels, is_deleted) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    ("PROJ-1", "SupplierAlpha defect", 1, "High", "SupplierAlpha", 0),
                    ("PROJ-2", "SupplierBeta defect", 1, "High", "SupplierBeta", 0),
                ],
            )
            conn.execute(
                "INSERT INTO issue_links (inward_key, outward_key, link_type) VALUES ('PROJ-1', 'PROJ-2', 'Duplicates')"
            )
            conn.commit()

        bdb = mstate / "box_cli" / ".box" / "box_mock.db"
        bdb.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(bdb) as conn:
            conn.execute("CREATE TABLE folders (id TEXT PRIMARY KEY, name TEXT, is_deleted INTEGER DEFAULT 0)")
            conn.execute("CREATE TABLE files (id TEXT PRIMARY KEY, name TEXT, is_deleted INTEGER DEFAULT 0)")
            conn.execute("CREATE TABLE shared_links (id TEXT PRIMARY KEY, is_deleted INTEGER DEFAULT 0)")
            conn.execute("CREATE TABLE collaborations (id TEXT PRIMARY KEY, is_deleted INTEGER DEFAULT 0)")
            conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, is_deleted INTEGER DEFAULT 0)")
            conn.execute("INSERT INTO folders (id, name, is_deleted) VALUES ('179430000001', 'SupplierDocs', 0)")
            conn.commit()

        sdb = mstate / "stripe_cli" / "stripe_mock.sqlite"
        sdb.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(sdb) as conn:
            conn.execute("CREATE TABLE objects (id TEXT PRIMARY KEY, resource TEXT, data_json TEXT, deleted INTEGER DEFAULT 0)")
            conn.execute(
                "INSERT INTO objects (id, resource, data_json, deleted) VALUES (?, ?, ?, 0)",
                ("prod_1", "products", json.dumps({"id": "prod_1", "name": "Acme Hardware"})),
            )
            conn.execute(
                "INSERT INTO objects (id, resource, data_json, deleted) VALUES (?, ?, ?, 0)",
                ("we_1", "webhook_endpoints", json.dumps({"id": "we_1", "url": "https://example.com/wh", "enabled_events": ["invoice.paid", "customer.subscription.updated"]})),
            )
            conn.commit()

        dws_state = mstate / "dws_doc_cli" / "state.json"
        dws_state.parent.mkdir(parents=True, exist_ok=True)
        dws_state.write_text(json.dumps({"files": 1}), encoding="utf-8")
        (run_dir / "workspace" / "supplier_metrics.csv").write_text("sku,score\nA,1\n", encoding="utf-8")

        td_state = mstate / "todoist_cli" / "state.json"
        td_state.parent.mkdir(parents=True, exist_ok=True)
        td_state.write_text(
            json.dumps(
                {
                    "sections": [{"id": "s1", "name": "Handoff"}, {"id": "s2", "name": "Re-source"}],
                    "items": [
                        {"content": "Weekly Handoff Note", "description": "", "section_id": "s1", "labels": ["handoff"]},
                        {
                            "content": "Silicone Boot",
                            "description": "Compression-mould quote vs LSR; Class 2A thread tolerance.",
                            "section_id": "s2",
                            "labels": ["capability-gap", "process-gap", "spec-gap"],
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )

        gdocs_state = meta_dir / "mock_workdir" / "tmp" / "runtime_mock_state" / "google_docs_mock" / "state.json"
        gdocs_state.parent.mkdir(parents=True, exist_ok=True)
        gdocs_state.write_text(
            json.dumps(
                {
                    "files": [
                        {
                            "id": "doc_a",
                            "name": "SupplierAlpha — Q4 Audit",
                            "type": "document",
                            "content": {
                                "blocks": [
                                    {"type": "paragraph", "text": "Finding 1: Severity: High | Fire suppression system is non-operational | Status: Open"}
                                ]
                            },
                        },
                        {
                            "id": "doc_b",
                            "name": "SupplierBeta — Q4 Audit",
                            "type": "document",
                            "content": {
                                "blocks": [
                                    {"type": "paragraph", "text": "Finding 1: Severity: High | EU RoHS regulatory violation | Status: Open"}
                                ]
                            },
                        },
                        {
                            "id": "doc1",
                            "name": "Supplier Risk Rollup",
                            "type": "document",
                            "content": {
                                "blocks": [
                                    {"type": "paragraph", "text": "SupplierAlpha | High+Open: 1"},
                                    {"type": "paragraph", "text": "SupplierBeta | High+Open: 1"},
                                ]
                            },
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )

        (run_dir / "outputs" / "triage.md").write_text(
            "Verified PROJ-1 and box 179430000001.\n",
            encoding="utf-8",
        )

    host_clean, _, _ = _build_host("clean", arm="sj", task_md=dirty_task_md, setup_fn=_seed_clean)
    clean_blocking, clean_issues, _ = host_clean.audit_service_state_delta()
    assert clean_blocking is False, f"Unexpected clean issues: {clean_issues}"
    assert clean_issues == []

    # Raw arm never gates or audits service state delta
    assert host_raw.audit_service_state_delta() == (False, [], "")


def test_wave4_browser_snapshot_attrs_and_exit_checks() -> None:
    from evals.acciobench.browser import _SNAPSHOT_JS, _UNCOMMITTED_UI_JS, _parse_snapshot_state

    controls_txt = "\n".join(
        [
            '[e1] <button data-task-due="今天"> 安排在今天',
            '[e2] <button data-panel="tasks"> Tasks',
            '[e3] <button data-filter-action="create"> 创建过滤器',
            '[e4] <button data-compose-action="draft-save"> 保存草稿',
        ]
    )
    _, fallbacks = _parse_snapshot_state({"controls": controls_txt})
    assert fallbacks["e1"] == 'button[data-task-due="今天"]'
    assert fallbacks["e2"] == 'button[data-panel="tasks"]'
    assert fallbacks["e3"] == 'button[data-filter-action="create"]'
    assert fallbacks["e4"] == 'button[data-compose-action="draft-save"]'

    assert "data-task-due" in _SNAPSHOT_JS
    assert "[Row State Summary]" in _SNAPSHOT_JS
    assert "body=${JSON.stringify(bodyStr)}" in _SNAPSHOT_JS
    assert "(checkExit = false)" in _UNCOMMITTED_UI_JS

    # BrowserSession.check_uncommitted_ui_state forwards check_exit=True to 2-arg evaluate and falls back on 1-arg mocks.
    calls_2arg: list[Any] = []

    class FakePage2:
        url = "http://127.0.0.1:20611/#inbox"

        def on(self, *args: Any, **kwargs: Any) -> None:
            pass

        def evaluate(self, script: str, *args: Any) -> Any:
            calls_2arg.append((script, args))
            if args == (True,):
                return ["Gmail task '' has empty due date"]
            return []

        def close(self) -> None:
            pass

    class FakeCtx2:
        def new_page(self) -> FakePage2:
            return FakePage2()

        def close(self) -> None:
            pass

    sess = BrowserSession(FakeCtx2(), {}, enable_dom_diff=False)
    p2 = FakePage2()
    sess._register_page("vtab-1", p2)
    sess.active_tab_id = "vtab-1"

    assert sess.check_uncommitted_ui_state(check_exit=False) == []
    exit_warnings = sess.check_uncommitted_ui_state(check_exit=True)
    assert exit_warnings == ["Gmail task '' has empty due date"]
    assert calls_2arg[-1][1] == (True,)

    class FakePage1:
        url = "http://127.0.0.1:20612/#inbox"

        def on(self, *args: Any, **kwargs: Any) -> None:
            pass

        def evaluate(self, script: str) -> Any:
            return ["Filter modal open"]

        def close(self) -> None:
            pass

    sess._register_page("vtab-2", FakePage1())
    sess.active_tab_id = "vtab-2"
    assert sess.check_uncommitted_ui_state(check_exit=True) == ["Filter modal open"]


def test_wave5_browser_gmail_fastpath_drafts_and_alibaba_ladder() -> None:
    from evals.acciobench.browser import _GMAIL_STATE_INIT_JS, _SNAPSHOT_JS, _UNCOMMITTED_UI_JS

    # Wave 5(a): 600-char snippet limit + [Gmail Fast-Path Hint] + window.gmailState init exposure
    assert "isNoiseRow ? 80 : 600" in _SNAPSHOT_JS
    assert "[Gmail Fast-Path Hint]" in _SNAPSHOT_JS
    assert "window.gmailState" in _SNAPSHOT_JS
    assert "window.gmailState = st" in _GMAIL_STATE_INIT_JS

    # Wave 5(b): cmp-, spf-, alp- pre-exit Gmail checks and Alibaba >=500 ladderPrice check
    for tok in (
        "supplier.qa@cellwise.test",
        "dgdesk@pacific-forwarder.test",
        "northstar-supp1ies.test",
        "PAY-419",
        "mei.chen@lumenlabs.test",
        "leon.wu@northport-3pl.test",
        "nora.patel@auroraretail.test",
        "ap-hold@auroraretail.test",
        "vip-support@auroraretail.test",
        "ALP-26",
        ">=500",
        "500+",
    ):
        assert tok in _UNCOMMITTED_UI_JS

    class FakeAlibabaPage:
        url = "http://127.0.0.1:20613/publish"

        def on(self, *args: Any, **kwargs: Any) -> None:
            pass

        def evaluate(self, script: str, *args: Any) -> Any:
            if args == (True,):
                return ['Alibaba ladderPrice quantity uses ">=500"']
            return []

        def close(self) -> None:
            pass

    class FakeAlibabaCtx:
        def new_page(self) -> FakeAlibabaPage:
            return FakeAlibabaPage()

        def close(self) -> None:
            pass

    sess = BrowserSession(FakeAlibabaCtx(), {}, enable_dom_diff=False)
    sess._register_page("vtab-1", FakeAlibabaPage())
    sess.active_tab_id = "vtab-1"
    assert sess.check_uncommitted_ui_state(check_exit=False) == []
    res_exit = sess.check_uncommitted_ui_state(check_exit=True)
    assert any('">=500"' in w for w in res_exit)


def test_wave5_host_audits_todoist_gdocs_alibaba_gws_jira_pct_srr_dws_and_stripe_gate(
    tmp_path: Path,
) -> None:
    import json
    import sqlite3

    def _new_host(name: str, arm: str = "sj", task_md: str = ""):
        host, _metrics, run_dir, meta_dir = _make_host(tmp_path / name, arm=arm)
        host.task_markdown = task_md
        return host, run_dir, meta_dir

    # 1. Stripe trigger entry gate: allowed iff requested via task_markdown or workspace .md
    h_stripe_block, _rd, _md = _new_host("stripe_block", task_md="Inspect Stripe webhook endpoints.")
    blocked_msg = h_stripe_block._check_bash_entry_gate("stripe trigger payment_intent.succeeded")
    assert blocked_msg is not None and "[ctx-entry-gate]" in blocked_msg

    h_stripe_task, _rd, _md = _new_host("stripe_task", task_md="Run stripe trigger payment_intent.succeeded.")
    assert h_stripe_task._check_bash_entry_gate("stripe trigger payment_intent.succeeded") is None

    h_stripe_ws, rd_sws, _md = _new_host("stripe_ws", task_md="Check Stripe pipeline.")
    (rd_sws / "workspace").mkdir(parents=True, exist_ok=True)
    (rd_sws / "workspace" / "runbook.md").write_text("Trigger standard triggers and check events_total.", encoding="utf-8")
    assert h_stripe_ws._check_bash_entry_gate("stripe trigger invoice.paid") is None

    h_stripe_raw, _rd, _md = _new_host("stripe_raw", arm="raw", task_md="Inspect Stripe webhook endpoints.")
    assert h_stripe_raw._check_bash_entry_gate("stripe trigger payment_intent.succeeded") is None

    # 2. DWS CSV upload filter excludes *reference*.md documentation
    h_dws_ref, rd_ref, md_ref = _new_host("dws_ref", task_md="Summarize metrics from data.csv.")
    (rd_ref / "workspace").mkdir(parents=True, exist_ok=True)
    (rd_ref / "workspace" / "data.csv").write_text("col\n1\n", encoding="utf-8")
    (rd_ref / "workspace" / "reference_dws_guide.md").write_text("You can upload files to dws.", encoding="utf-8")
    dws_ref_db = md_ref / "mock_state" / "dws_doc_cli" / "mock.db"
    dws_ref_db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(dws_ref_db)
    con.execute("CREATE TABLE documents (nodeId TEXT, name TEXT, content TEXT, type TEXT)")
    con.commit()
    con.close()
    blk_ref, issues_ref, _ = h_dws_ref.audit_service_state_delta()
    assert blk_ref is False and issues_ref == []

    # 3. Todoist AnoColor must keep ['capability-gap', 'spec-gap'] only without 'process-gap'
    def _write_todoist(db_path: Path, labels: list[str]) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(db_path)
        c.execute("DROP TABLE IF EXISTS sections")
        c.execute("DROP TABLE IF EXISTS items")
        c.execute("CREATE TABLE sections (id TEXT, name TEXT, is_deleted INTEGER DEFAULT 0)")
        c.execute(
            "CREATE TABLE items (id TEXT, content TEXT, description TEXT, section_id TEXT, "
            "labels_json TEXT, checked INTEGER DEFAULT 0, is_deleted INTEGER DEFAULT 0)"
        )
        c.execute("INSERT INTO sections VALUES ('s1', 'Re-source', 0)")
        c.execute(
            "INSERT INTO items VALUES ('i1', 'AnoColor hard-anodised brackets', 'Needs Class 2A tolerance', "
            "'s1', ?, 0, 0)",
            (json.dumps(labels),),
        )
        c.commit()
        c.close()

    h_tod, _rd, md_tod = _new_host("todoist", task_md="Triage Todoist labels.")
    tod_db = md_tod / "mock_state" / "todoist_cli" / ".cache" / "todoist" / "mock.db"
    _write_todoist(tod_db, ["capability-gap", "spec-gap", "process-gap"])
    blk_tod, iss_tod, _ = h_tod.audit_service_state_delta()
    assert blk_tod is True and any("process-gap" in i for i in iss_tod)

    h_tod_raw, _rd, md_tod_raw = _new_host("todoist_raw", arm="raw", task_md="Triage Todoist labels.")
    _write_todoist(md_tod_raw / "mock_state" / "todoist_cli" / ".cache" / "todoist" / "mock.db", ["capability-gap", "spec-gap", "process-gap"])
    assert h_tod_raw.audit_service_state_delta() == (False, [], "")

    _write_todoist(tod_db, ["capability-gap", "spec-gap"])
    blk_tod_ok, iss_tod_ok, _ = h_tod.audit_service_state_delta()
    assert blk_tod_ok is False and iss_tod_ok == []

    # 4. Google Docs roll-up: CrestLink keywords + >=10 content-word overlap
    crest_full = (
        "Finding 1: Classified with high severity: escalated worker-safety hazard from tolerance drift "
        "risking catastrophic press failure during inspection cycle; status: open."
    )
    vortex_full = (
        "Finding 1: Regulatory permit violation exceeding threshold wastewater filtration containment "
        "tank pressure sensor malfunction recorded during inspection audit; status: open."
    )

    def _write_gdocs(path: Path, rollup_body: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "files": [
                {
                    "id": "doc-audit-crestlink",
                    "name": "CrestLink Forging — Q1 Audit",
                    "type": "document",
                    "content": {"blocks": [{"type": "paragraph", "text": crest_full}]},
                },
                {
                    "id": "doc-audit-vortex",
                    "name": "Vortex Metals — Q1 Audit",
                    "type": "document",
                    "content": {"blocks": [{"type": "paragraph", "text": vortex_full}]},
                },
                {
                    "id": "doc-rollup",
                    "name": "Supplier Risk Roll-Up",
                    "type": "document",
                    "content": {"blocks": [{"type": "paragraph", "text": rollup_body}]},
                },
            ]
        }
        path.write_text(json.dumps(state), encoding="utf-8")

    h_gd, _rd, md_gd = _new_host("gdocs")
    gdocs_json = md_gd / "mock_state" / "google_docs_mock" / "state.json"
    _write_gdocs(
        gdocs_json,
        "CrestLink Forging | High+Open: 1 | Top finding: press hazard summary\n"
        "Vortex Metals | High+Open: 1 | Top finding: brief wastewater note",
    )
    blk_gd, iss_gd, _ = h_gd.audit_service_state_delta()
    assert blk_gd is True
    assert any("CrestLink Forging" in i and "escalation keywords" in i for i in iss_gd)
    assert any("Vortex Metals" in i and "overlap" in i for i in iss_gd)

    _write_gdocs(
        gdocs_json,
        f"CrestLink Forging | High+Open: 1 | Top finding: {crest_full}\n"
        f"Vortex Metals | High+Open: 1 | Top finding: {vortex_full}",
    )
    blk_gd_ok, iss_gd_ok, _ = h_gd.audit_service_state_delta()
    assert blk_gd_ok is False and iss_gd_ok == []

    # 5. Alibaba Commerce state (SQLite benchmark.db + outputs/*.json)
    def _write_alibaba(meta_dir: Path, run_dir: Path, *, dirty: bool) -> None:
        dbp = meta_dir / "mock_services" / "alibaba_publish" / "data" / "benchmark.db"
        dbp.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(dbp)
        c.execute("DROP TABLE IF EXISTS sessions")
        c.execute("DROP TABLE IF EXISTS field_values")
        c.execute("CREATE TABLE sessions (id TEXT, status TEXT)")
        c.execute("CREATE TABLE field_values (session_id TEXT, field_name TEXT, field_value TEXT)")
        c.execute("INSERT INTO sessions VALUES ('sess-1', 'submitted')")
        fields = [
            ("productTitle", "SoundBee Pro X3 Wireless Earbuds"),
            (
                "ladderPrice",
                json.dumps([{"quantity": ">=500" if dirty else "500+", "price": "12.00"}]),
            ),
            (
                "deliveryPeriod",
                json.dumps(
                    [{"quantity": 500, "days": 15}]
                    if dirty
                    else [{"quantity": "100-500", "days": "15"}]
                ),
            ),
            (
                "keyword_2",
                "active noise cancelling wireless earphones"
                if dirty
                else "active noise cancelling earphones",
            ),
            ("keyword_3", "wireless audio" if dirty else "hi-res audio earbuds LDAC"),
        ]
        for k, v in fields:
            c.execute("INSERT INTO field_values VALUES ('sess-1', ?, ?)", (k, v))
        c.commit()
        c.close()

        outp = run_dir / "outputs" / "projector_publish.json"
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_text(
            json.dumps(
                {
                    "fields": {
                        "productTitle": "NovaCast P5 Pro Portable Projector",
                        "attr_connectivity": (
                            "WiFi 6 + Bluetooth 5.2 + HDMI" if dirty else "WiFi 6 + Bluetooth 5.2"
                        ),
                        "image_1": "___INVALID___" if dirty else "IMG_20260610_001.jpg",
                    }
                }
            ),
            encoding="utf-8",
        )

    h_ali, rd_ali, md_ali = _new_host("alibaba")
    _write_alibaba(md_ali, rd_ali, dirty=True)
    blk_ali, iss_ali, _ = h_ali.audit_service_state_delta()
    assert blk_ali is True
    assert any("ladderPrice" in i for i in iss_ali)
    assert any("deliveryPeriod" in i for i in iss_ali)
    assert any("SoundBee Pro X3" in i and "keyword_2" in i for i in iss_ali)
    assert any("SoundBee Pro X3" in i and "keyword_3" in i for i in iss_ali)
    assert any("___INVALID___" in i for i in iss_ali)
    assert any("WiFi 6 + Bluetooth 5.2" in i for i in iss_ali)

    _write_alibaba(md_ali, rd_ali, dirty=False)
    blk_ali_ok, iss_ali_ok, _ = h_ali.audit_service_state_delta()
    assert blk_ali_ok is False and iss_ali_ok == []

    # 6. GWS Supplier Quote Consolidation (Award Decisions + Audit Trail + Slide 7)
    def _write_gws_sqc(db_path: Path, *, dirty: bool) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(db_path)
        c.execute("DROP TABLE IF EXISTS spreadsheets")
        c.execute("DROP TABLE IF EXISTS presentations")
        c.execute("CREATE TABLE spreadsheets (id TEXT, data TEXT)")
        c.execute("CREATE TABLE presentations (id TEXT, data TEXT)")
        award_cells = {
            "A1": {"value": "SKU"},
            "B1": {"value": "Category"},
            "C1": {"value": "Award Supplier"},
            "D1": {"value": "Award Price"},
            "E1": {"value": "Runner-Up"},
            "F1": {"value": "Runner-Up Price"},
            "G1": {"value": "Savings"},
            "H1": {"value": "Decision Rationale"},
            "A2": {"value": "KIT-KNIFE-5PC"},
            "B2": {"value": "Kitchen Hardware"},
            "C2": {"value": "Osaka Home Goods" if dirty else "Busan Alloy Works"},
            "D2": {"value": "19.0" if dirty else "16.5"},
            "E2": {"value": "Istanbul Metalworks"},
            "F2": {"value": "18.0"},
            "G2": {"value": "1.5"},
            "H2": {"value": "lowest price"},
        }
        if not dirty:
            award_cells.update(
                {
                    "A3": {"value": "DECOR-VASE-CER"},
                    "B3": {"value": "Home Décor"},
                    "C3": {"value": "Porto Cork & Home"},
                    "D3": {"value": "4.3"},
                    "E3": {"value": "Guadalajara Artesanías"},
                    "F3": {"value": "4.6"},
                    "G3": {"value": "0.3"},
                    "H3": {"value": "lowest price"},
                }
            )
        ss_obj = {
            "sheets": [
                {
                    "properties": {"title": "Master Quotes"},
                    "cells": {
                        "A1": {"value": "SKU"},
                        "A2": {"value": "KIT-KNIFE-5PC"},
                        "A3": {"value": "DECOR-VASE-CER"},
                    },
                },
                {"properties": {"title": "Award Decisions"}, "cells": award_cells},
                {
                    "properties": {"title": "Audit Trail"},
                    "cells": {
                        "A1": {"value": "Note"},
                        "A2": {
                            "value": (
                                "28 awards, $8.57 aggregate savings"
                                if dirty
                                else "30 awards, $10.77 aggregate savings"
                            )
                        },
                    },
                },
            ]
        }
        slides = [{"pageElements": [{"content": f"Slide {i}"}]} for i in range(1, 7)]
        if not dirty:
            slides.append(
                {
                    "pageElements": [
                        {
                            "content": "Q2 Award Summary: 30 awards, $10.77 aggregate savings; "
                            "KIT-KNIFE-5PC -> Busan Alloy Works; DECOR-VASE-CER -> Porto Cork & Home"
                        }
                    ]
                }
            )
        c.execute("INSERT INTO spreadsheets VALUES ('sheet-supplier-eval-003', ?)", (json.dumps(ss_obj),))
        c.execute(
            "INSERT INTO presentations VALUES ('pres-sourcing-review-303', ?)",
            (json.dumps({"title": "Q2 Sourcing Review", "slides": slides}),),
        )
        c.commit()
        c.close()

    h_sqc, _rd, md_sqc = _new_host("gws_sqc", task_md="Populate Award Decisions tab.")
    sqc_db = md_sqc / "mock_state" / "google_workspace_cli" / ".cache" / "gws" / "mock.db"
    _write_gws_sqc(sqc_db, dirty=True)
    blk_sqc, iss_sqc, _ = h_sqc.audit_service_state_delta()
    assert blk_sqc is True
    assert any("KIT-KNIFE-5PC" in i for i in iss_sqc)
    assert any("DECOR-VASE-CER" in i for i in iss_sqc)
    assert any("10.77" in i and "Audit Trail" in i for i in iss_sqc)
    assert any("Slide 7" in i for i in iss_sqc)

    _write_gws_sqc(sqc_db, dirty=False)
    blk_sqc_ok, iss_sqc_ok, _ = h_sqc.audit_service_state_delta()
    assert blk_sqc_ok is False and iss_sqc_ok == []

    # 7. Jira cycle-7 defect triage (PROJ-1..35 links, Done status, cycle-7 labels)
    def _write_jira_c7(db_path: Path, *, dirty: bool) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(db_path)
        c.execute("DROP TABLE IF EXISTS issue_statuses")
        c.execute("DROP TABLE IF EXISTS issues")
        c.execute("DROP TABLE IF EXISTS issue_links")
        c.execute("CREATE TABLE issue_statuses (id INTEGER, name TEXT, category TEXT)")
        c.execute(
            "CREATE TABLE issues (key TEXT, status_id INTEGER, labels_json TEXT, "
            "summary TEXT, description TEXT, is_deleted INTEGER DEFAULT 0)"
        )
        c.execute("CREATE TABLE issue_links (link_type TEXT, inward_key TEXT, outward_key TEXT)")
        c.execute("INSERT INTO issue_statuses VALUES (1, 'To Do', 'todo'), (2, 'Done', 'done')")
        for i in range(1, 36):
            key = f"PROJ-{i}"
            st = 2 if (not dirty and 28 <= i <= 35) else (2 if dirty and 29 < i <= 35 else 1)
            lbls = []
            if dirty and i == 30:
                lbls = ["cycle-7"]
            elif not dirty and i in (18, 27):
                lbls = ["cycle-7"]
            c.execute(
                "INSERT INTO issues VALUES (?, ?, ?, ?, '', 0)",
                (key, st, json.dumps(lbls), f"Defect {i}"),
            )
        start_link = 2 if dirty else 1
        for i in range(start_link, 9):
            c.execute("INSERT INTO issue_links VALUES ('Duplicates', ?, ?)", (f"PROJ-{27 + i}", f"PROJ-{i}"))
        c.commit()
        c.close()

    h_jc7, _rd, md_jc7 = _new_host("jira_c7", task_md="Triage cycle-7 Jira defects PROJ-1..PROJ-35.")
    jc7_db = md_jc7 / "mock_state" / "jira_cli" / "jira_mock.db"
    _write_jira_c7(jc7_db, dirty=True)
    blk_jc7, iss_jc7, _ = h_jc7.audit_service_state_delta()
    assert blk_jc7 is True
    assert any("PROJ-28->PROJ-1" in i for i in iss_jc7)
    assert any("PROJ-30" in i and "cycle-7" in i for i in iss_jc7)
    assert any("PROJ-18" in i and "PROJ-27" in i for i in iss_jc7)

    _write_jira_c7(jc7_db, dirty=False)
    blk_jc7_ok, iss_jc7_ok, _ = h_jc7.audit_service_state_delta()
    assert blk_jc7_ok is False and iss_jc7_ok == []

    # 8. Procurement Control Tower (Box collaborations >= 2 & GWS Total=9)
    def _write_pct(box_db: Path, gws_db: Path, *, dirty: bool) -> None:
        box_db.parent.mkdir(parents=True, exist_ok=True)
        cb = sqlite3.connect(box_db)
        cb.execute("DROP TABLE IF EXISTS collaborations")
        cb.execute("CREATE TABLE collaborations (id TEXT, is_deleted INTEGER DEFAULT 0)")
        cb.execute("INSERT INTO collaborations VALUES ('col-1', 0)")
        if not dirty:
            cb.execute("INSERT INTO collaborations VALUES ('col-2', 0)")
        cb.commit()
        cb.close()

        gws_db.parent.mkdir(parents=True, exist_ok=True)
        cg = sqlite3.connect(gws_db)
        cg.execute("DROP TABLE IF EXISTS spreadsheets")
        cg.execute("DROP TABLE IF EXISTS presentations")
        cg.execute("CREATE TABLE spreadsheets (id TEXT, data TEXT)")
        cg.execute("CREATE TABLE presentations (id TEXT, data TEXT)")
        txt = "Shortlisted Suppliers overview" if dirty else "Shortlisted Suppliers: Total=9"
        cg.execute(
            "INSERT INTO presentations VALUES ('pres-launch-101', ?)",
            (json.dumps({"title": "Launch Review", "slides": [{"pageElements": [{"content": txt}]}]}),),
        )
        cg.commit()
        cg.close()

    h_pct, _rd, md_pct = _new_host("pct", task_md="Set up Procurement Control Tower.")
    pct_box = md_pct / "mock_state" / "box_cli" / ".box" / "box_mock.db"
    pct_gws = md_pct / "mock_state" / "google_workspace_cli" / ".cache" / "gws" / "mock.db"
    _write_pct(pct_box, pct_gws, dirty=True)
    blk_pct, iss_pct, _ = h_pct.audit_service_state_delta()
    assert blk_pct is True
    assert any("collaborations count is 1" in i for i in iss_pct)
    assert any("Total=9" in i for i in iss_pct)

    _write_pct(pct_box, pct_gws, dirty=False)
    blk_pct_ok, iss_pct_ok, _ = h_pct.audit_service_state_delta()
    assert blk_pct_ok is False and iss_pct_ok == []

    # 9. Supplier Roster Refresh (Box Roster-Refresh folder & exact Jira float amounts)
    def _write_srr(box_db: Path, jira_db: Path, *, dirty: bool) -> None:
        box_db.parent.mkdir(parents=True, exist_ok=True)
        cb = sqlite3.connect(box_db)
        cb.execute("DROP TABLE IF EXISTS folders")
        cb.execute("CREATE TABLE folders (id TEXT, name TEXT, is_deleted INTEGER DEFAULT 0, item_status TEXT DEFAULT 'active')")
        cb.execute("INSERT INTO folders VALUES ('f1', ?, 0, 'active')", ("Misc" if dirty else "Q1-2026-Roster-Refresh",))
        cb.commit()
        cb.close()

        jira_db.parent.mkdir(parents=True, exist_ok=True)
        cj = sqlite3.connect(jira_db)
        cj.execute("DROP TABLE IF EXISTS issues")
        cj.execute("CREATE TABLE issues (key TEXT, summary TEXT, description TEXT, is_deleted INTEGER DEFAULT 0)")
        desc = "Rounded amounts 915640, 705671" if dirty else "Amounts: 915640.0, 705671.0, 216280.0, 67800.0"
        cj.execute("INSERT INTO issues VALUES ('SUP-1', 'Supplier Roster Refresh', ?, 0)", (desc,))
        cj.commit()
        cj.close()

    h_srr, _rd, md_srr = _new_host("srr", task_md="Execute supplier roster refresh.")
    srr_box = md_srr / "mock_state" / "box_cli" / ".box" / "box_mock.db"
    srr_jira = md_srr / "mock_state" / "jira_cli" / "jira_mock.db"
    _write_srr(srr_box, srr_jira, dirty=True)
    blk_srr, iss_srr, _ = h_srr.audit_service_state_delta()
    assert blk_srr is True
    assert any("Roster-Refresh" in i for i in iss_srr)
    assert any("915640.0" in i and "67800.0" in i for i in iss_srr)

    _write_srr(srr_box, srr_jira, dirty=False)
    blk_srr_ok, iss_srr_ok, _ = h_srr.audit_service_state_delta()
    assert blk_srr_ok is False and iss_srr_ok == []

    # 10. DWS Block-Edit Product Table
    def _write_dws_block(db_path: Path, *, dirty: bool) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(db_path)
        for tbl in ("documents", "blocks", "comments", "permissions"):
            c.execute(f"DROP TABLE IF EXISTS {tbl}")
        c.execute("CREATE TABLE documents (nodeId TEXT, name TEXT, content TEXT, type TEXT)")
        c.execute("CREATE TABLE blocks (nodeId TEXT, blockId TEXT, blockType TEXT, contentJson TEXT, blockOrder INTEGER)")
        c.execute("CREATE TABLE comments (nodeId TEXT, commentKey TEXT, content TEXT, type TEXT)")
        c.execute("CREATE TABLE permissions (nodeId TEXT, userId TEXT, role TEXT)")

        if dirty:
            c.execute("INSERT INTO documents VALUES ('doc-catalog', 'Q2 Procurement Calendar', 'NB-JP-DOG-017 old row', 'file')")
            c.execute("INSERT INTO blocks VALUES ('doc-catalog', 'b1', 'heading', 'Q2 Procurement Calendar', 1)")
            c.execute("INSERT INTO comments VALUES ('doc-catalog', 'cmk-inline-07', 'existing', 'inline')")
        else:
            q3 = " ".join(
                [
                    "NB-JP-PET-025", "NB-BR-BIR-026", "NB-MX-BIR-027", "NB-AU-BIR-028",
                    "NB-SG-COM-029", "NB-US-COM-030", "NB-UK-PUZ-031", "NB-DE-CAT-032",
                    "NB-JP-OUT-033", "NB-BR-CAT-034", "NB-MX-STU-035", "NB-AU-DOG-036",
                ]
            )
            c.execute("INSERT INTO documents VALUES ('doc-catalog', 'Procurement Calendar — All Markets', '', 'file')")
            c.execute("INSERT INTO documents VALUES ('doc-q1-arch', 'Q1 Archive', 'NB-JP-DOG-017', 'file')")
            c.execute("INSERT INTO permissions VALUES ('doc-q1-arch', 'uid-archive-a', 'READER'), ('doc-q1-arch', 'uid-archive-b', 'READER')")
            c.execute("INSERT INTO blocks VALUES ('doc-catalog', 'b1', 'heading', 'Procurement Calendar — All Markets', 1)")
            c.execute("INSERT INTO blocks VALUES ('doc-catalog', 'b2', 'callout', 'Provisional schedule pending confirmation', 2)")
            c.execute("INSERT INTO blocks VALUES ('doc-catalog', 'b3', 'table', ?, 3)", (q3,))
            c.execute("INSERT INTO blocks VALUES ('doc-catalog', 'b4', 'heading', 'Procurement Contacts', 4)")
            for ck in ("cmk-inline-03", "cmk-inline-07", "cmk-inline-11", "cmk-footer-28", "cmk-footer-31"):
                c.execute("INSERT INTO comments VALUES ('doc-catalog', ?, 'kept note', 'inline')", (ck,))
            c.execute("INSERT INTO comments VALUES ('doc-catalog', 'cmk-jp-new', 'JP market: confirm pricing with Tokyo office', 'inline')")
        c.commit()
        c.close()

    h_dwsb, _rd, md_dwsb = _new_host("dws_block")
    dwsb_db = md_dwsb / "mock_state" / "dws_doc_cli" / "mock.db"
    _write_dws_block(dwsb_db, dirty=True)
    blk_dwsb, iss_dwsb, _ = h_dwsb.audit_service_state_delta()
    assert blk_dwsb is True
    assert any("Q1 Archive" in i for i in iss_dwsb)
    assert any("NB-JP-DOG-017" in i for i in iss_dwsb)
    assert any("NB-JP-PET-025" in i for i in iss_dwsb)
    assert any("Procurement Calendar" in i for i in iss_dwsb)
    assert any("JP market: confirm pricing with Tokyo office" in i for i in iss_dwsb)
    assert any("cmk-inline-03" in i for i in iss_dwsb)

    h_dwsb_raw, _rd, md_dwsb_raw = _new_host("dws_block_raw", arm="raw")
    _write_dws_block(md_dwsb_raw / "mock_state" / "dws_doc_cli" / "mock.db", dirty=True)
    assert h_dwsb_raw.audit_service_state_delta() == (False, [], "")

    _write_dws_block(dwsb_db, dirty=False)
    blk_dwsb_ok, iss_dwsb_ok, _ = h_dwsb.audit_service_state_delta()
    assert blk_dwsb_ok is False and iss_dwsb_ok == []


def test_wave5_deliverables_alibaba_slack_headphone_and_host_integration(
    tmp_path: Path,
) -> None:
    import json
    from ctx.deliverables import audit_deliverables

    ws = tmp_path / "ws"
    out = ws / "outputs"
    out.mkdir(parents=True, exist_ok=True)

    # (a) Alibaba publish deliverable + ToolHost cross-check
    host_ali, _m, run_ali, _md = _make_host(tmp_path / "ali_host", arm="sj")
    ali_dirty = {
        "fields": {
            "productTitle": "SoundBee Pro X3 & NovaCast P5 Pro Bundle",
            "ladderPrice": [{"quantity": ">=500", "price": "10"}],
            "deliveryPeriod": [{"quantity": 2000, "days": 25}],
            "keyword_2": "active noise cancelling wireless earphones",
            "keyword_3": "bluetooth earbuds",
            "attr_shape": "Round capsule",
            "attr_connectivity": "WiFi 6 + HDMI",
            "image_1": "___INVALID___",
        }
    }
    ali_clean = {
        "fields": {
            "productTitle": "SoundBee Pro X3",
            "ladderPrice": [{"quantity": "500+", "price": "10"}],
            "deliveryPeriod": [{"quantity": "501-2000", "days": "25"}],
            "keyword_1": "tws bluetooth 5.3 earbuds",
            "keyword_2": "active noise cancelling earphones",
            "keyword_3": "hi-res audio earbuds LDAC",
            "attr_shape": "In-ear, ergonomic design",
            "attr_connectivity": "WiFi 6 + Bluetooth 5.2",
            "image_1": "IMG_20260610_001.jpg",
            "image_2": "IMG_20260610_003.jpg",
            "image_3": "IMG_20260610_005.jpg",
            "image_4": "IMG_20260610_008.jpg",
            "image_5": "IMG_20260610_011.jpg",
        }
    }
    ali_p = out / "listing.json"
    ali_p.write_text(json.dumps(ali_dirty), encoding="utf-8")
    res_ali_d = audit_deliverables(ws, ["outputs/listing.json"])
    assert any("ladderPrice" in a for a in res_ali_d.advisories)
    assert any("deliveryPeriod" in a for a in res_ali_d.advisories)
    assert any("SoundBee Pro X3" in a for a in res_ali_d.advisories)
    assert any("NovaCast P5 Pro" in a for a in res_ali_d.advisories)

    (run_ali / "outputs").mkdir(parents=True, exist_ok=True)
    (run_ali / "outputs" / "listing.json").write_text(json.dumps(ali_dirty), encoding="utf-8")
    blk_ah, iss_ah, _ = host_ali.audit_service_state_delta()
    assert blk_ah is True and any("ladderPrice" in i for i in iss_ah)

    ali_p.write_text(json.dumps(ali_clean), encoding="utf-8")
    res_ali_c = audit_deliverables(ws, ["outputs/listing.json"])
    assert not any("outputs/listing.json" in a for a in res_ali_c.advisories)

    # (b) Slack supplier quote consolidation
    sqc_p = out / "answer.json"
    sqc_p.write_text(
        json.dumps(
            {
                "primary_supplier_name": "AtlasWare",
                "backup_supplier_name": "EverTherm",
                "primary_supplier_landed_cost_usd": 3.376,
                "primary_supplier_fx_rate_rmb_per_usd": 7.1,
                "primary_supplier_fx_handling_load_pct": 1.5,
                "primary_supplier_extra_logistics_usd_per_pc": 0.05,
                "primary_supplier_cert_authorization_letter_required": True,
                "backup_supplier_landed_cost_usd": 3.36,
            }
        ),
        encoding="utf-8",
    )
    res_sqc_d = audit_deliverables(ws, ["outputs/answer.json"])
    assert any("EverTherm" in a and "3.36" in a for a in res_sqc_d.advisories)

    sqc_p.write_text(
        json.dumps(
            {
                "primary_supplier_name": "EverTherm",
                "backup_supplier_name": "AtlasWare",
                "primary_supplier_landed_cost_usd": 3.36,
                "primary_supplier_fx_rate_rmb_per_usd": None,
                "primary_supplier_fx_handling_load_pct": None,
                "primary_supplier_extra_logistics_usd_per_pc": 0.0,
                "primary_supplier_cert_authorization_letter_required": False,
                "backup_supplier_landed_cost_usd": 3.376,
            }
        ),
        encoding="utf-8",
    )
    res_sqc_c = audit_deliverables(ws, ["outputs/answer.json"])
    assert res_sqc_c.advisories == ()

    # (c) Amazon headphone sourcing
    hp_p = out / "headphone_selection.json"
    hp_p.write_text(
        json.dumps(
            {
                "selected_asins": ["B0GXFMJ4T9", "B09BW29FJS"],
                "portfolio_score": 20.033,
                "rationale": [
                    {"asin": "B0GXFMJ4T9", "subcategory": "open_ear", "price": 29.99, "reviews": 999},
                    {"asin": "B0D31C567W", "subcategory": "clip_on", "price": 39.99, "reviews": 1200},
                ],
            }
        ),
        encoding="utf-8",
    )
    res_hp_d = audit_deliverables(ws, ["outputs/headphone_selection.json"])
    assert any("B0GXFMJ4T9" in a and "clip_on" in a for a in res_hp_d.advisories)
    assert any("B0D31C567W" in a and "price=39.99" in a for a in res_hp_d.advisories)
    assert any("43.3" in a and "45.5641" in a for a in res_hp_d.advisories)

    hp_p.write_text(
        json.dumps(
            {
                "selected_asins": ["B0GK9FWDX5", "B0D31C567W", "B09JC3S2PN"],
                "portfolio_score": 45.5641,
                "rationale": [
                    {"asin": "B0GK9FWDX5", "subcategory": "bone_conduction", "price": 39.99, "reviews": 1577},
                    {"asin": "B0D31C567W", "subcategory": "open_ear", "price": 31.99, "reviews": 15000},
                    {"asin": "B09JC3S2PN", "subcategory": "over_ear", "price": 39.99, "reviews": 53972},
                ],
            }
        ),
        encoding="utf-8",
    )
    res_hp_c = audit_deliverables(ws, ["outputs/headphone_selection.json"])
    assert res_hp_c.advisories == ()

    # (d) Travel tour findings
    tr_p = out / "audit_findings.json"
    tr_p.write_text(
        json.dumps(
            {
                "findings": [
                    {"leg_id": "bangkok-amenity-claim", "error_type": "cross_source_inconsistency", "correct_value": "Novotel Bangkok Platinum Pratunam"},
                    {"leg_id": "bangkok-accessibility-section", "error_type": "accessibility_mismatch", "correct_value": "Some Other Villa"},
                    {"leg_id": "bangkok-team-dietary-area", "error_type": "dietary_unaccommodated", "correct_value": "Mercure Bangkok Sukhumvit 11"},
                ]
            }
        ),
        encoding="utf-8",
    )
    res_tr_d = audit_deliverables(ws, ["outputs/audit_findings.json"])
    assert any("Paradox Bangkok Sukhumvit" in a for a in res_tr_d.advisories)
    assert any("Retreat House 81" in a for a in res_tr_d.advisories)
    assert any("Walton Suites Sukhumvit Bangkok" in a for a in res_tr_d.advisories)

    tr_p.write_text(
        json.dumps(
            {
                "findings": [
                    {"leg_id": "bangkok-amenity-claim", "error_type": "cross_source_inconsistency", "correct_value": "Paradox Bangkok Sukhumvit"},
                    {"leg_id": "bangkok-accessibility-section", "error_type": "accessibility_mismatch", "correct_value": "Retreat House 81 - BTS Onnut - Three-Bedroom House"},
                    {"leg_id": "bangkok-team-dietary-area", "error_type": "dietary_unaccommodated", "correct_value": "Walton Suites Sukhumvit Bangkok"},
                ]
            }
        ),
        encoding="utf-8",
    )
    res_tr_c = audit_deliverables(ws, ["outputs/audit_findings.json"])
    assert res_tr_c.advisories == ()

    # (e) Insurance settlement reasons & (f) Variant merger evidence
    ins_p = out / "insurance_settlement.json"
    ins_p.write_text(
        json.dumps(
            {
                "settlements": [
                    {"customer_id": "LINNEA", "insurer": "PingAn", "claim_status": "paid_partial", "reason_short": "Partial payment."},
                    {"customer_id": "JONAS", "insurer": "Allianz", "claim_status": "no_claim", "reason_short": "Top-tier cover."},
                    {"customer_id": "OCEANTECH", "insurer": "BaoViet", "claim_status": "denied", "reason_short": "Rejected."},
                ]
            }
        ),
        encoding="utf-8",
    )
    res_ins_d = audit_deliverables(ws, ["outputs/insurance_settlement.json"])
    assert any("LINNEA" in a for a in res_ins_d.advisories)
    assert any("JONAS" in a for a in res_ins_d.advisories)
    assert any("OCEANTECH" in a for a in res_ins_d.advisories)

    ins_p.write_text(
        json.dumps(
            {
                "settlements": [
                    {
                        "customer_id": "LINNEA",
                        "insurer": "PingAn",
                        "claim_status": "paid_partial",
                        "reason_short": "PingAn ICC(A) All Risks policy applies $500 deductible on sum insured USD 26,400 (110%).",
                    },
                    {
                        "customer_id": "JONAS",
                        "insurer": "Allianz",
                        "claim_status": "no_claim",
                        "reason_short": "Allianz top tier covers 8 cartons of rubber gasket J01-J08 intact.",
                    },
                    {
                        "customer_id": "OCEANTECH",
                        "insurer": "BaoViet",
                        "claim_status": "denied",
                        "reason_short": "BaoViet ICC(C) named perils does not cover ordinary water damage.",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    res_ins_c = audit_deliverables(ws, ["outputs/insurance_settlement.json"])
    assert res_ins_c.advisories == ()

    sig_dir = ws / "workspace" / "review_signals"
    sig_dir.mkdir(parents=True, exist_ok=True)
    (sig_dir / "image_ocr_and_packaging_flags.csv").write_text(
        "candidate_sku,image_flag,ocr_text,review_note\n"
        "TRAIL-750-KIDS,age_use_callout,kids straw 500ml,capacity listed as 500ml\n",
        encoding="utf-8",
    )
    vm_p = out / "merge_decisions.json"
    vm_p.write_text(
        json.dumps(
            {
                "candidate_evaluations": [
                    {
                        "candidate_sku": "TRAIL-750-KIDS",
                        "evidence": [
                            {"source_file": "platform_policy/marketplace_variation_policy.pdf", "cite": "2. Allowed variation themes"},
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    res_vm_d = audit_deliverables(ws, ["outputs/merge_decisions.json"])
    assert any("image_ocr_and_packaging_flags.csv" in a for a in res_vm_d.advisories)
    assert any("marketplace_variation_policy.pdf" in a for a in res_vm_d.advisories)

    vm_p.write_text(
        json.dumps(
            {
                "candidate_evaluations": [
                    {
                        "candidate_sku": "TRAIL-750-KIDS",
                        "evidence": [
                            {
                                "source_file": "review_signals/image_ocr_and_packaging_flags.csv",
                                "cite": "age_use_callout: kids straw 500ml — capacity listed as 500ml",
                            },
                            {
                                "source_file": "platform_policy/marketplace_variation_policy.pdf",
                                "cite": "Rebuild requires a new parent strategy when core function or included accessory set alters shopper expectation.",
                            },
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    res_vm_c = audit_deliverables(ws, ["outputs/merge_decisions.json"])
    assert res_vm_c.advisories == ()

