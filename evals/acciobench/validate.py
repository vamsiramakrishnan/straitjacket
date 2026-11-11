"""Model-free environment ``doctor`` and task ``validate`` referee for AccioBench.

Proves toolchain readiness, isolated ``TaskSandbox`` staging, ``CompiledPortMap``
remapping, mock-service startup, ``ToolHost`` + ``ctx`` sidecar digest/retrieval
round-trip, official ``grader/run.sh`` zero-leakage baseline scoring, and
zero-orphan process teardown before spending any LLM API budget.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any

from .config import BenchConfig, RunMetrics, discover_all_tasks, load_cab_task, resolve_ctx_bin
from .host import ToolHost, bwrap_sandbox_executable, bwrap_unavailable_reason
from .sandbox import TaskSandbox, _resolve_runtime_paths, kill_by_slot_tag

__all__ = ["run_doctor", "validate_task_referee"]

_RUN_HANDLE_RE = re.compile(r"\brun:([0-9a-f]{6,64})\b")


def _find_slot_pids(slot_tag: str) -> list[int]:
    """Return alive PIDs owned by current user carrying ``SJ_SLOT_TOKEN=<slot_tag>``."""
    if not slot_tag or not Path("/proc").is_dir():
        return []
    needle = f"SJ_SLOT_TOKEN={slot_tag}".encode("utf-8")
    my_pid = os.getpid()
    my_uid = os.getuid()
    found: list[int] = []
    try:
        entries = list(os.scandir("/proc"))
    except OSError:
        return []

    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == my_pid:
            continue
        try:
            if entry.stat(follow_symlinks=False).st_uid != my_uid:
                continue
            # Ignore zombie processes awaiting parent wait() that are already dead.
            stat_txt = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="ignore")
            rparen = stat_txt.rfind(")")
            if rparen != -1:
                state_ch = stat_txt[rparen + 2 : rparen + 3]
                if state_ch == "Z":
                    continue
            env_bytes = Path(f"/proc/{pid}/environ").read_bytes()
            if needle in env_bytes:
                found.append(pid)
        except OSError:
            continue
    return sorted(found)


def _detect_browser_binary(config: BenchConfig) -> tuple[str, str]:
    """Return ``(status, message)`` for Chromium/Chrome availability without cold-starting a browser."""
    if config.chrome_bin is not None:
        if config.chrome_bin.is_file() and os.access(config.chrome_bin, os.X_OK):
            return "OK", f"browser binary: {config.chrome_bin}"
        return (
            "MISSING",
            f"browser binary: explicit --chrome-bin not executable at {config.chrome_bin}",
        )

    for cand in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(cand)
        if found:
            return "OK", f"browser binary: {found}"

    if importlib.util.find_spec("playwright") is not None:
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as pw:
                pw_exec = Path(pw.chromium.executable_path)
                if pw_exec.is_file():
                    return "OK", f"browser binary: {pw_exec} (Playwright bundled)"
        except Exception:
            pass

    return (
        "WARN",
        "browser binary: Chrome/Chromium not found (needed for browser_* tasks; run 'python -m playwright install chromium')",
    )


def run_doctor(config: BenchConfig) -> tuple[bool, list[str]]:
    """Verify dependencies, dataset layout, ``ctx`` sidecar, browser, and credentials.

    Returns ``(ok, lines)`` where ``ok`` is ``True`` iff all core model-free
    requirements (Python >= 3.11, ``ctx`` CLI, valid dataset root, and any
    explicitly passed binary/runtime paths) are present and functional.
    """
    lines: list[str] = []
    core_ok = True

    # 1. Python runtime
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if sys.version_info >= (3, 11):
        lines.append(f"[OK] Python {py_ver} ({sys.executable})")
    else:
        core_ok = False
        lines.append(f"[MISSING] Python >= 3.11 required; found {py_ver}")

    # 2. Straitjacket `ctx` binary / module
    try:
        ctx_bin = resolve_ctx_bin(config.ctx_bin)
        cmd = [str(ctx_bin), "-P", "-m", "ctx", "--version"] if "python" in ctx_bin.name.lower() else [str(ctx_bin), "--version"]
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        ver = (cp.stdout or cp.stderr).strip().splitlines()[0] if (cp.stdout or cp.stderr).strip() else "ok"
        if cp.returncode == 0:
            lines.append(f"[OK] ctx sidecar: {ctx_bin} ({ver})")
        else:
            core_ok = False
            lines.append(f"[MISSING] ctx sidecar exited {cp.returncode}: {ver} (install via 'pip install -e .')")
    except Exception as exc:
        core_ok = False
        lines.append(f"[MISSING] ctx sidecar unavailable: {exc} (install via 'pip install -e .')")

    # 3. Python libraries used by graders, document/spreadsheet tasks, & browser pool
    for mod_name, pkg_hint, purpose in (
        ("pandas", "pandas>=2.0", "tabular CSV/Excel data analysis"),
        ("openpyxl", "openpyxl>=3.1", "Excel spreadsheet graders & inspection"),
        ("yaml", "PyYAML>=6.0", "YAML listing graders & configs"),
        ("PIL", "pillow>=10.0", "image asset inspection & dimensions"),
        ("pypdf", "pypdf>=4.0", "PDF structure & text extraction"),
        ("pdfplumber", "pdfplumber>=0.10", "PDF table & layout extraction"),
        ("fitz", "pymupdf>=1.23", "PyMuPDF page rendering & PDF parsing"),
        ("bs4", "beautifulsoup4>=4.12", "HTML document parsing"),
        ("lxml", "lxml>=5.0", "XML/HTML XPath & schema parsing"),
        ("playwright", "playwright", "headless browser_* tool tasks"),
    ):
        if importlib.util.find_spec(mod_name) is not None:
            lines.append(f"[OK] Python package '{mod_name}' installed ({purpose})")
        else:
            lines.append(
                f"[WARN] Python package '{mod_name}' missing ({purpose}; install via: pip install '{pkg_hint}')"
            )

    # 4. Dataset root & task index
    if not config.dataset_root.exists():
        core_ok = False
        lines.append(
            f"[MISSING] dataset_root not found: {config.dataset_root} "
            "(clone https://github.com/Accio-org/CommerceAgentBench or run 'bash evals/acciobench/bootstrap.sh')"
        )
    else:
        try:
            task_ids = discover_all_tasks(config.dataset_root)
        except Exception as exc:
            task_ids = []
            lines.append(f"[MISSING] dataset_root discovery failed ({config.dataset_root}): {exc}")
            core_ok = False
        else:
            if task_ids:
                lines.append(f"[OK] dataset_root: {config.dataset_root} ({len(task_ids)} tasks discovered)")
            else:
                core_ok = False
                lines.append(
                    f"[MISSING] dataset_root contains 0 tasks: {config.dataset_root} "
                    "(expected datasets_domain_v1/**/task.toml)"
                )

    # 5. Runtime directory & host binaries (pdftotext, bun, node)
    if config.runtime_dir is not None and not config.runtime_dir.is_dir():
        core_ok = False
        lines.append(
            f"[MISSING] explicit --runtime-dir does not exist: {config.runtime_dir} "
            "(run 'bash evals/acciobench/bootstrap.sh --runtime-dir ...')"
        )
    golden_mocks, runtime_bin, _ = _resolve_runtime_paths(config)
    if golden_mocks.is_dir():
        lines.append(f"[OK] mock_services: {golden_mocks}")
    else:
        lines.append(
            "[WARN] mock_services directory not staged (pure file tasks work; "
            "run 'bash evals/acciobench/bootstrap.sh' or pass --runtime-dir for CLI/API/browser mocks)"
        )

    extra_path = (
        f"{runtime_bin}{os.pathsep}{os.environ.get('PATH', '')}"
        if runtime_bin is not None
        else os.environ.get("PATH", "")
    )
    for tool_name, hint in (
        ("pdftotext", "needed for PDF reading; install poppler-utils / poppler"),
        ("bun", "needed for Bun-backed mock services/CLIs; run evals/acciobench/bootstrap.sh"),
        ("node", "needed for Node-backed mock services; install Node.js >= 18"),
    ):
        resolved = shutil.which(tool_name, path=extra_path)
        if resolved:
            lines.append(f"[OK] {tool_name}: {resolved}")
        else:
            lines.append(f"[WARN] {tool_name} not found ({hint})")

    bwrap_bin = bwrap_sandbox_executable()
    if bwrap_bin:
        lines.append(f"[OK] bwrap sandbox: {bwrap_bin} (mount + PID namespace isolation active)")
    else:
        lines.append(
            f"[WARN] bwrap sandbox: unavailable ({bwrap_unavailable_reason()}). doctor and "
            "validate still work, but a scored 'run' will be REFUSED: without bubblewrap there "
            "is no filesystem isolation. Install bubblewrap (e.g. sudo apt-get install -y "
            "bubblewrap) or pass --allow-unsandboxed to 'run' to override."
        )

    # 6. Headless browser executable
    b_status, b_msg = _detect_browser_binary(config)
    if b_status == "MISSING":
        core_ok = False
    lines.append(f"[{b_status}] {b_msg}")

    # 7. Live LLM API credentials (advisory for doctor/validate; required only for `run`)
    provider = (config.provider or "vertex").strip().lower()
    if provider == "gemini":
        has_key = bool(
            (config.api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()
        )
        if has_key:
            lines.append("[OK] credentials (gemini): API key configured")
        else:
            lines.append(
                "[WARN] credentials (gemini): GEMINI_API_KEY not set "
                "(not required for doctor/validate; export GEMINI_API_KEY=... before 'run')"
            )
    else:
        proj = (
            config.project_id
            or os.environ.get("VERTEX_PROJECT_ID")
            or os.environ.get("GOOGLE_CLOUD_PROJECT")
            or os.environ.get("GCLOUD_PROJECT")
            or ""
        ).strip()
        has_gcloud = bool(shutil.which("gcloud") or os.environ.get("VERTEX_ACCESS_TOKEN"))
        if proj and has_gcloud:
            lines.append(f"[OK] credentials (vertex): project={proj}, ADC/gcloud present")
        else:
            lines.append(
                "[WARN] credentials (vertex): set --project / VERTEX_PROJECT_ID and Application Default Credentials "
                "before 'run' (or pass --provider gemini with GEMINI_API_KEY)"
            )

    return core_ok, lines


def _infer_category(config: BenchConfig, spec: Any) -> str:
    case_dir = Path(spec.case_dir).resolve()
    suite_dir = config.suite_dir.resolve()
    if case_dir.is_relative_to(suite_dir):
        parts = case_dir.relative_to(suite_dir).parts
        if parts:
            return parts[0]
    tid = str(getattr(spec, "task_id", ""))
    return tid.split("-", 1)[0] if "-" in tid else "unknown"


def validate_task_referee(config: BenchConfig, task_id: str, slot: int = 0) -> dict[str, Any]:
    """Run a deterministic model-free referee check on ``task_id`` ($0 API cost).

    Verifies:
      1. Isolated ``TaskSandbox`` staging, port remapping, and mock-service boot.
      2. ``ToolHost`` (``arm='sj'``) + ``ctx`` Entry Gate digest capture & ``ctx get`` retrieval.
      3. Official ``grader/run.sh`` execution on untouched/empty ``/task/outputs`` with
         ``judge_provider='mock'``, confirming zero setup leakage (baseline must NOT pass).
      4. Clean ``TaskSandbox.cleanup()`` teardown with 0 leaked ``SJ_SLOT_TOKEN`` processes.
    """
    t0 = time.perf_counter()
    errors: list[str] = []
    setup_ok = False
    ctx_capture_ok = False
    grader_ok = False
    baseline_passed = False
    baseline_score = 0.0
    checks_passed = 0
    checks_total = 0
    digests_delivered = 0
    bytes_kept_out = 0
    port_map: dict[int, int] = {}
    has_mocks = False
    resolved_task_id = str(task_id)
    category = "unknown"
    leaked_pids: list[int] = []

    with tempfile.TemporaryDirectory(prefix=f"acciobench_val_s{slot}_") as tmp_str:
        tmp_root = Path(tmp_str).resolve()
        val_cfg = dataclasses.replace(config, workdir=tmp_root, judge_provider="mock")

        try:
            spec = load_cab_task(val_cfg.dataset_root, task_id)
            resolved_task_id = str(getattr(spec, "task_id", "") or task_id)
            category = _infer_category(val_cfg, spec)
        except Exception as exc:
            return {
                "ok": False,
                "task_id": resolved_task_id,
                "category": category,
                "slot": slot,
                "has_mocks": False,
                "port_map": {},
                "setup_ok": False,
                "ctx_capture_ok": False,
                "digests_delivered": 0,
                "bytes_kept_out": 0,
                "grader_ok": False,
                "baseline_passed": False,
                "baseline_score": 0.0,
                "checks_passed": 0,
                "checks_total": 0,
                "leaked_processes": 0,
                "leaked_pids": [],
                "elapsed_s": round(time.perf_counter() - t0, 2),
                "errors": [f"load_cab_task failed: {exc}"],
            }

        run_dir = val_cfg.runs_dir / f"{resolved_task_id}__referee"
        meta_dir = val_cfg.runs_dir / f"{resolved_task_id}__referee_meta"
        sandbox = TaskSandbox(val_cfg, spec, run_dir, meta_dir, slot)

        try:
            sandbox.setup()
            has_mocks = bool(sandbox.has_mocks)
            port_map = dict(sandbox.port_map)
            expected_base = 20000 + int(slot) * 100
            ports_in_slot = all(expected_base <= new_p < expected_base + 100 for new_p in port_map.values())
            if not ports_in_slot:
                errors.append(f"port_map {port_map} fell outside slot {slot} range [{expected_base}, {expected_base + 99}]")
            if (run_dir / "workspace").is_dir() and (run_dir / "outputs").is_dir() and ports_in_slot:
                setup_ok = True
            else:
                errors.append("sandbox workspace/outputs directories missing after setup()")

            # Exercise ToolHost (`arm="sj"`) + `ctx` sidecar Entry Gate & retrieval without touching /task/outputs
            metrics = RunMetrics(
                task_id=resolved_task_id,
                task_rel=resolved_task_id,
                category=category,
                arm="sj",
                model_id="referee",
            )
            host = ToolHost(
                val_cfg,
                run_dir,
                meta_dir,
                sandbox.compiled_ports,
                sandbox.service_env(),
                metrics,
                None,
                arm="sj",
            )
            host.step = 1
            fr_list, _, _ = host.execute_part("list", {"path": "/task/workspace"})
            list_out = str((fr_list.get("functionResponse") or {}).get("response", {}).get("output", ""))
            if list_out.startswith("ERROR:"):
                errors.append(f"ToolHost list(/task/workspace) failed: {list_out[:200]}")

            host.step = 2
            n_lines = max(300, (int(val_cfg.entry_gate_bytes) // 55) + 120)
            flood_cmd = (
                f"{shlex.quote(sys.executable)} -B -c "
                f"\"for i in range({n_lines}): print(f'REFEREE_ROW_{{i:04d}}_' + 'x'*48)\""
            )
            fr_bash, _, _ = host.execute_part("bash", {"command": flood_cmd})
            bash_out = str((fr_bash.get("functionResponse") or {}).get("response", {}).get("output", ""))
            m_run = _RUN_HANDLE_RE.search(bash_out)
            retrieve_ok = False
            if m_run is not None:
                handle = f"run:{m_run.group(1)}"
                host.step = 3
                fr_get, _, _ = host.execute_part(
                    "bash",
                    {"command": f"ctx get {handle}#stdout --lines 1:3"},
                )
                get_out = str((fr_get.get("functionResponse") or {}).get("response", {}).get("output", ""))
                retrieve_ok = "REFEREE_ROW_0000_" in get_out and metrics.ctx_retrievals.get("get", 0) >= 1
            else:
                errors.append(f"ctx Entry Gate did not emit a run:<hash> digest handle: {bash_out[:220]}")

            digests_delivered = metrics.digests_delivered
            bytes_kept_out = metrics.bytes_kept_out
            if not list_out.startswith("ERROR:") and digests_delivered >= 1 and retrieve_ok:
                ctx_capture_ok = True
            elif m_run is not None and not retrieve_ok:
                errors.append("ctx get retrieval failed to return stored REFEREE_ROW_0000_ line")

            if host.has_user_deliverables():
                errors.append("unexpected files present in /task/outputs prior to baseline grading")

            # Run official grader on empty outputs — must execute cleanly and NOT pass
            reward_json = meta_dir / "reward.json"
            grade_data = sandbox.grade(reward_json, judge_token="")
            if "_error" in grade_data:
                errors.append(f"grader failed: {str(grade_data.get('_error'))[:260]}")
            else:
                grader_ok = True
                baseline_score = float(grade_data.get("score", 0.0) or 0.0)
                checks_passed = int(grade_data.get("checks_passed", 0) or 0)
                checks_total = int(grade_data.get("checks_total", 0) or 0)
                if checks_total > 0:
                    baseline_passed = (checks_passed == checks_total) or (baseline_score >= 0.9999)
                else:
                    baseline_passed = bool(grade_data.get("passed", False)) or (baseline_score >= 0.9999)
                if baseline_passed:
                    errors.append(
                        f"baseline setup leakage: untouched empty workspace resolved as passed "
                        f"(score={baseline_score}, checks={checks_passed}/{checks_total})"
                    )
        except Exception as exc:
            errors.append(f"referee exception: {exc}")
        finally:
            sandbox.cleanup()
            # Allow brief kernel process-table reaping before checking for leaked slot processes.
            deadline = time.perf_counter() + 0.15
            leaked_pids = _find_slot_pids(sandbox.slot_tag)
            while leaked_pids and time.perf_counter() < deadline:
                time.sleep(0.01)
                leaked_pids = _find_slot_pids(sandbox.slot_tag)
            if leaked_pids:
                errors.append(f"leaked {len(leaked_pids)} mock process(es) after cleanup: {leaked_pids}")
                kill_by_slot_tag(sandbox.slot_tag)

    ok = bool(
        setup_ok
        and ctx_capture_ok
        and grader_ok
        and (not baseline_passed)
        and len(leaked_pids) == 0
        and not errors
    )
    return {
        "ok": ok,
        "task_id": resolved_task_id,
        "category": category,
        "slot": slot,
        "has_mocks": has_mocks,
        "port_map": port_map,
        "setup_ok": setup_ok,
        "ctx_capture_ok": ctx_capture_ok,
        "digests_delivered": digests_delivered,
        "bytes_kept_out": bytes_kept_out,
        "grader_ok": grader_ok,
        "baseline_passed": baseline_passed,
        "baseline_score": baseline_score,
        "checks_passed": checks_passed,
        "checks_total": checks_total,
        "leaked_processes": len(leaked_pids),
        "leaked_pids": leaked_pids,
        "elapsed_s": round(time.perf_counter() - t0, 2),
        "errors": errors,
    }
