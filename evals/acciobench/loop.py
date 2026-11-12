"""Single-trial execution loop for AccioBench supporting paired ``raw`` and ``sj`` arms."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import threading
import time
import tomllib
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
_REPO_SRC = _REPO_ROOT / "src"
for _p in (_REPO_ROOT, _REPO_SRC):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from ctx.deliverables import (  # noqa: E402
    audit_deliverables,
    extract_declared_deliverables,
    format_deliverable_contract,
)
from ctx.residence import WatermarkResidence  # noqa: E402

from .browser import BrowserPool, BrowserSession  # noqa: E402
from .config import (  # noqa: E402
    BenchConfig,
    RunMetrics,
    config_fingerprint,
    ctx_code_identity,
    ctx_version,
    harness_ctx_code_identity,
    load_cab_task,
    task_inputs_digest,
)
from .host import ToolHost, bwrap_sandbox_executable, unsandboxed_refusal_message  # noqa: E402
from .providers import ModelProvider  # noqa: E402
from .sandbox import TaskSandbox  # noqa: E402
from .schemas import (  # noqa: E402
    MUTATING_SHELL_RE,
    SJ_EXECUTION_PROTOCOL,
    build_prompt,
    build_tools,
    canonical_tool_sig,
    format_workspace_inventory,
    is_mutating_tool_call,
)

__all__ = ["UnsandboxedRunRefused", "run_trial", "scored_run_sandbox"]

_VALID_ARMS = frozenset({"raw", "sj"})
_RESUMABLE_ERROR_TERMINATIONS = frozenset({"", "api_error", "setup_error", "error"})

_MAX_EMPTY_TURNS = 3
_MAX_TEXT_NUDGES = 3
_MIN_STEPS_FOR_TEXT_COMPLETE = 4
_MAX_IDENTICAL_TURN_STREAK = 4
_MAX_DUP_TURN_STREAK = 2
_MAX_SERVICE_DELTA_GATE_HITS = 2
_COMPACT_MIN_TURN_GAP = 6

_MICRO_PY_MAX_RESULT_CHARS = 650
_MICRO_PY_STREAK_LIMIT = 3
_MICRO_PY_MAX_HINTS = 2
_MICRO_PY_HINT = (
    "\n[ctx-hint: batch remaining checks/extractions into a single python3 script "
    "to verify all items in one turn]"
)

_TRAJ_TEXT_PREVIEW_CHARS = 800
_TRAJ_HEAD_PREVIEW_CHARS = 200
_SESSION_TASK_PREFIX_CHARS = 32
_PASS_SCORE_THRESHOLD = 0.9999
_SCORE_SCALE_TOLERANCE = 1e-6
_MAX_CHECK_REASON_CHARS = 140
_MAX_VERIFIER_ERR_CHARS = 240
_MAX_NOTE_ERR_CHARS = 300

_PRINT_LOCK = threading.Lock()


class UnsandboxedRunRefused(RuntimeError):
    """A scored run was asked for without the bwrap sandbox and without the override."""


def scored_run_sandbox(config: BenchConfig) -> str:
    """How a scored trial under ``config`` is sandboxed: ``"bwrap"`` when the sandbox is
    usable, ``"none"`` when it is not and ``config.allow_unsandboxed`` says to run anyway.
    Otherwise raises :class:`UnsandboxedRunRefused` with the reason, how to install
    bubblewrap and the override. ``ACCIOBENCH_NO_BWRAP`` alone is never enough."""
    if bwrap_sandbox_executable() is not None:
        return "bwrap"
    if config.allow_unsandboxed:
        return "none"
    raise UnsandboxedRunRefused(unsandboxed_refusal_message())


def _resolve_task_metadata(config: BenchConfig, spec: Any, fallback_task_id: str) -> tuple[str, str, str]:
    """Return ``(resolved_task_id, task_rel, category)`` safely for dataset or temp tasks."""
    resolved_id = str(getattr(spec, "task_id", "") or fallback_task_id)
    case_dir = Path(spec.case_dir).resolve()
    suite_dir = config.suite_dir.resolve()
    dataset_root = config.dataset_root.resolve()

    if case_dir.is_relative_to(suite_dir):
        task_rel = case_dir.relative_to(suite_dir).as_posix()
        category = task_rel.split("/", 1)[0] if "/" in task_rel else resolved_id.split("-", 1)[0]
    elif case_dir.is_relative_to(dataset_root):
        task_rel = case_dir.relative_to(dataset_root).as_posix()
        prefix = resolved_id.split("-", 1)[0]
        category = (
            prefix
            if prefix in ("file", "api", "cli", "browser")
            else task_rel.split("/", 1)[0]
        )
    else:
        task_rel = case_dir.name
        category = resolved_id.split("-", 1)[0] if "-" in resolved_id else "custom"
    return resolved_id, task_rel, category


def _task_requires_network(spec: Any) -> bool:
    """Inspect ``task.toml`` to determine whether public web search is enabled."""
    toml_path = Path(spec.case_dir) / "task.toml"
    if toml_path.is_file():
        try:
            raw_toml = tomllib.loads(toml_path.read_text(encoding="utf-8"))
            return bool((raw_toml.get("environment") or {}).get("requires_network", False))
        except Exception:
            pass
    return bool(getattr(spec, "requires_network", False))


def _collect_external_advisories(host: Any) -> list[str]:
    """Collect external-service verification advisories, including per-service readbacks."""
    ext_adv_fn = getattr(host, "external_state_advisories", None)
    adv = list(ext_adv_fn()) if callable(ext_adv_fn) else []
    unverified_fn = getattr(host, "unverified_mutated_services", None)
    unverified = sorted(str(s) for s in unverified_fn()) if callable(unverified_fn) else []
    if unverified:
        if not any("without a subsequent read-only check for:" in item for item in adv):
            adv.append(
                "External service state was mutated without a subsequent read-only check for: "
                f"{', '.join(unverified)} — run one consolidated list/get readback for each "
                "to confirm final remote item/file/link counts and statuses before finalizing."
            )
    elif not adv:
        ext_mut_cnt = int(getattr(host, "external_mutation_count", 0) or 0)
        last_mut_step = int(getattr(host, "last_external_mutation_step", 0) or 0)
        last_read_step = int(getattr(host, "last_external_readback_step", 0) or 0)
        if ext_mut_cnt > 0 and last_read_step < last_mut_step:
            adv.append(
                f"External service state was mutated at step {last_mut_step} "
                "without a subsequent read-only check — run one consolidated list/get readback "
                "to confirm final remote item/file/link counts and statuses before finalizing."
            )
    return adv


def _collect_service_and_ui_gate(host: Any) -> tuple[bool, list[str], str]:
    """Collect pre-exit service-state delta and browser uncommitted-UI blockers."""
    blocking = False
    issues: list[str] = []
    receipts: list[str] = []

    audit_fn = getattr(host, "audit_service_state_delta", None)
    if callable(audit_fn):
        res = audit_fn()
        if isinstance(res, tuple) and len(res) == 3:
            sd_blocking, sd_issues, sd_receipt = res
            if bool(sd_blocking):
                blocking = True
            if isinstance(sd_issues, (list, tuple, set)):
                for item in sd_issues:
                    item_s = str(item).strip()
                    if item_s:
                        issues.append(item_s)
            receipt_s = str(sd_receipt or "").strip()
            if receipt_s:
                receipts.append(receipt_s)

    browser = getattr(host, "browser", None)
    ui_fn = getattr(browser, "check_uncommitted_ui_state", None) if browser is not None else None
    if callable(ui_fn):
        try:
            ui_res = ui_fn(check_exit=True)
        except TypeError:
            ui_res = ui_fn()
        ui_issues: list[str] = []
        if isinstance(ui_res, str):
            raw_ui = ui_res.strip()
            if raw_ui:
                ui_issues = [raw_ui]
        elif isinstance(ui_res, (list, tuple, set)):
            ui_issues = [str(item).strip() for item in ui_res if str(item).strip()]
        if ui_issues:
            blocking = True
            issues.extend(ui_issues)
            if len(ui_issues) == 1 and ui_issues[0].startswith("[uncommitted-ui-state:"):
                receipts.append(ui_issues[0])
            else:
                receipts.append(f"[uncommitted-ui-state: {'; '.join(ui_issues)}]")

    return blocking, issues, "\n".join(receipts)


def _format_gate_message(
    *,
    prefix_header: str,
    pre_exit_audit: Any | None = None,
    ext_adv: list[str] | None = None,
    sd_issues: list[str] | None = None,
    sd_receipt: str = "",
) -> str:
    """Format an actionable pre-exit gate message combining deliverable, service, and UI checks."""
    sections: list[str] = []
    if pre_exit_audit is not None and getattr(pre_exit_audit, "should_gate", False):
        sections.append(pre_exit_audit.format_gate_message())
    bullets: list[str] = []
    for item in ext_adv or ():
        bullets.append(f"- [CHECK] {item}" if "Verification" in prefix_header else f"- {item}")
    for item in sd_issues or ():
        if item not in (ext_adv or ()):
            bullets.append(f"- [CHECK] {item}" if "Verification" in prefix_header else f"- {item}")
    if bullets:
        if not sections:
            sections.append(prefix_header)
        sections.extend(bullets)
    if sd_receipt:
        sections.append(sd_receipt)
    sections.append("Resolve any issues above and call present_files to finalize.")
    return "\n".join(sections)


def _format_progress(metrics: RunMetrics) -> str:
    return (
        f"{metrics.task_id:<44} | arm={metrics.arm:<3} | {metrics.category:<7} | "
        f"stp {metrics.steps:>3} | gen {metrics.gen_tokens:>7,} | "
        f"tot {metrics.total_tokens:>9,} | {metrics.wall_clock_s:>5.1f}s | "
        f"pass={'Y' if metrics.passed else 'N'} cap={metrics.capacity_score:.2f} "
        f"({metrics.checks_passed}/{metrics.checks_total}) | {metrics.termination}"
        + (" | UNGRADED (grading error)" if metrics.grading_error else "")
    )


def _interpret_reward(data: Any) -> tuple[float, int, int, list[dict[str, Any]]]:
    """The one place a grader's reward is read: ``(score, checks_passed, checks_total,
    checks_breakdown)``. Anything the harness cannot interpret raises ``ValueError`` and
    becomes a grading error (ungraded), never a task failure."""
    import math

    if not isinstance(data, dict):
        raise ValueError(f"reward.json is a {type(data).__name__}, not an object")
    if "_error" in data:
        raise ValueError(str(data.get("_error") or "grader failed"))

    def _num(key: str) -> float | None:
        v = data.get(key)
        if v is None:
            return None
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise ValueError(f"reward.json {key!r} is not a finite number: {v!r}"[:_MAX_VERIFIER_ERR_CHARS])
        return float(v)

    score = _num("score")
    cp = _num("checks_passed")
    ct = _num("checks_total")
    for key, v in (("checks_passed", cp), ("checks_total", ct)):
        if v is not None and (v < 0 or v != int(v)):
            raise ValueError(f"reward.json {key!r} is not a non-negative integer: {v!r}")
    cp_i, ct_i = int(cp or 0), int(ct or 0)
    if cp_i > ct_i:
        raise ValueError(f"reward.json checks_passed {cp_i} > checks_total {ct_i}")
    if score is None and ct_i == 0:
        raise ValueError("reward.json has neither a score nor any checks")
    if score is not None and score < 0:
        raise ValueError(f"reward.json score is negative: {score!r}")
    if score is not None and score > 1.0 + _SCORE_SCALE_TOLERANCE:
        # Scores are on a 0..1 scale (the pass threshold, the reports and every grader in
        # this repository assume it); 85 or 5 would otherwise be recorded as "passed".
        raise ValueError(f"reward.json score {score!r} is above 1, outside the 0..1 scale")
    if score is not None:
        score = min(score, 1.0)  # float noise just above 1
    breakdown = data.get("checks_breakdown")
    if breakdown is None:
        breakdown = []
    if not isinstance(breakdown, list) or not all(isinstance(c, dict) for c in breakdown):
        raise ValueError("reward.json 'checks_breakdown' is not a list of objects")
    return float(score or 0.0), cp_i, ct_i, breakdown


def _grade_into(
    config: BenchConfig,
    provider: ModelProvider,
    sandbox: TaskSandbox,
    meta_dir: Path,
    metrics: RunMetrics,
) -> None:
    """Run the official grader and record its verdict, or a grading error, in ``metrics``.

    A grader failure (timeout, missing or invalid ``reward.json``) is NOT a task
    failure: ``metrics.grading_error`` is set, the score fields stay at zero and
    reports leave the trial out of every aggregate until it is re-graded."""
    judge_tok = (
        provider.judge_access_token() if config.judge_provider == "vertex" else ""
    )
    reward_json = meta_dir / "reward.json"
    reward_json.unlink(missing_ok=True)  # never read a previous grade as this one
    try:
        grade_data: Any = sandbox.grade(reward_json, judge_token=judge_tok)
    except Exception as exc:
        grade_data = {"_error": f"grader could not run: {type(exc).__name__}: {exc}"}
    metrics.notes = [n for n in metrics.notes if not n.startswith("verifier error:")]
    try:
        raw_score, cp_cnt, ct_cnt, breakdown = _interpret_reward(grade_data)
    except ValueError as exc:
        grade_data = {"_error": str(exc)}
    if "_error" not in grade_data:
        metrics.grading_error = ""
        metrics.raw_verifier_score = raw_score
        metrics.checks_passed = cp_cnt
        metrics.checks_total = ct_cnt
        if ct_cnt > 0:
            metrics.capacity_score = cp_cnt / ct_cnt
            metrics.passed = (cp_cnt == ct_cnt) or (raw_score >= _PASS_SCORE_THRESHOLD)
        else:
            metrics.capacity_score = raw_score
            metrics.passed = raw_score >= _PASS_SCORE_THRESHOLD
        metrics.failed_checks = [
            f"{chk.get('id')}: {str(chk.get('reason', ''))[:_MAX_CHECK_REASON_CHARS]}"
            for chk in breakdown
            if not chk.get("passed")
        ]
    else:
        err_msg = str(grade_data.get("_error", ""))[:_MAX_VERIFIER_ERR_CHARS]
        metrics.grading_error = err_msg.strip() or f"grader exited {grade_data.get('_exit')}"
        metrics.passed = False
        metrics.capacity_score = 0.0
        metrics.raw_verifier_score = 0.0
        metrics.checks_passed = 0
        metrics.checks_total = 0
        metrics.failed_checks = []
        metrics.notes.append(f"verifier error: {err_msg}")


def _regrade_cached(
    config: BenchConfig,
    spec: Any,
    provider: ModelProvider,
    cached: RunMetrics,
    run_dir: Path,
    meta_dir: Path,
    result_file: Path,
    slot: int,
) -> RunMetrics | None:
    """Grade a finished trial's outputs again, without any model call and without
    touching its run directory. ``None`` when that is impossible (directories gone)."""
    sandbox = TaskSandbox(config, spec, run_dir, meta_dir, slot)
    try:
        try:
            sandbox.prepare_regrade()
        except FileNotFoundError:
            return None
        _grade_into(config, provider, sandbox, meta_dir, cached)
    except Exception as exc:
        cached.grading_error = f"re-grade failed: {str(exc)[:_MAX_VERIFIER_ERR_CHARS]}"
    finally:
        sandbox.cleanup()
    result_file.write_text(
        json.dumps(cached.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return cached


#: Harness constants that change what a trial measures (residence/compaction cadence,
#: loop gates); part of the resume fingerprint alongside config.FINGERPRINT_FIELDS.
_HARNESS_BEHAVIOUR: dict[str, Any] = {
    "max_empty_turns": _MAX_EMPTY_TURNS,
    "max_text_nudges": _MAX_TEXT_NUDGES,
    "min_steps_for_text_complete": _MIN_STEPS_FOR_TEXT_COMPLETE,
    "max_identical_turn_streak": _MAX_IDENTICAL_TURN_STREAK,
    "max_dup_turn_streak": _MAX_DUP_TURN_STREAK,
    "max_service_delta_gate_hits": _MAX_SERVICE_DELTA_GATE_HITS,
    "compact_min_turn_gap": _COMPACT_MIN_TURN_GAP,
    "micro_py_streak_limit": _MICRO_PY_STREAK_LIMIT,
    "pass_score_threshold": _PASS_SCORE_THRESHOLD,
}


def _accept_for_config(saved: dict[str, Any], config: BenchConfig, fingerprint: str, slug: str) -> bool:
    """May a cached result stand for a trial under ``config``? One line explains a refusal
    or a legacy acceptance."""
    saved_fp = str(saved.get("config_fingerprint") or "")
    if saved_fp:
        if saved_fp == fingerprint:
            return True
        with _PRINT_LOCK:
            print(
                f"  [rerun] {slug}: configuration changed since this result "
                f"(fingerprint {saved_fp} -> {fingerprint})",
                flush=True,
            )
        return False
    saved_model = str(saved.get("model_id") or "")
    with _PRINT_LOCK:
        if saved_model == config.model:
            print(
                f"  [legacy] {slug}: result predates config fingerprints; accepted because "
                f"model_id={saved_model} matches",
                flush=True,
            )
            return True
        print(
            f"  [rerun] {slug}: result predates config fingerprints and was produced by "
            f"model_id={saved_model or '?'}, not {config.model}",
            flush=True,
        )
    return False


class SupersedeError(RuntimeError):
    """A previous configuration's trial could not be moved aside; nothing was deleted."""


def _supersede(config: BenchConfig, run_dir: Path, meta_dir: Path, saved: dict[str, Any]) -> Path:
    """Move a previous configuration's trial out of ``runs/`` (reports never load it from
    there) into its OWN new directory ``<workdir>/superseded/<fingerprint>[.N]/``.

    Never deletes: an existing destination gets a counter suffix, and the moves are plain
    renames (no copy-then-delete). If a rename fails, the ones already done are undone,
    the trial's directories are left where they were, and ``SupersedeError`` stops this
    trial so the caller never wipes them."""
    tag = str(saved.get("config_fingerprint") or "") or (
        "legacy-" + "".join(ch if ch.isalnum() or ch in "-._" else "_" for ch in str(saved.get("model_id") or "unknown"))
    )
    srcs = [p for p in (run_dir, meta_dir) if p.exists()]
    moved: list[tuple[Path, Path]] = []
    created: Path | None = None  # the directory this call made, if it got that far
    try:
        parent = config.workdir / "superseded"
        parent.mkdir(parents=True, exist_ok=True)
        n = 1
        while True:
            dest_root = parent / (tag if n == 1 else f"{tag}.{n}")
            try:
                dest_root.mkdir()  # exclusive: never reuse a directory that holds a trial
                created = dest_root
                break
            except FileExistsError:
                n += 1
        for src in srcs:
            dest = dest_root / src.name
            os.rename(src, dest)
            moved.append((src, dest))
    except OSError as exc:
        for src, dest in reversed(moved):
            try:
                os.rename(dest, src)
            except OSError:
                pass
        if created is not None:
            try:
                os.rmdir(created)  # removes it only if it is empty again
            except OSError:
                pass
        raise SupersedeError(
            f"{run_dir.name}: could not move the previous configuration's trial aside "
            f"({exc}); it was left in place and this trial was not run"
        ) from exc
    return dest_root


def run_trial(
    config: BenchConfig,
    task_id: str,
    arm: str,
    slot: int,
    provider: ModelProvider,
    browser_pool: BrowserPool | None = None,
    *,
    wave: str = "custom",
    bucket: str = "unknown",
    trial_num: int = 1,
) -> RunMetrics:
    """Execute a single AccioBench task trial under ``arm`` (``'raw'`` or ``'sj'``)."""
    arm_norm = (arm or "").strip().lower()
    if arm_norm not in _VALID_ARMS:
        raise ValueError(f"Unsupported arm {arm!r}. Expected one of {sorted(_VALID_ARMS)}.")
    is_sj = arm_norm == "sj"
    # Before anything is staged, graded or sent to a model: every caller is covered.
    sandbox_kind = scored_run_sandbox(config)

    spec = load_cab_task(config.dataset_root, task_id)
    resolved_id, task_rel, category = _resolve_task_metadata(config, spec, task_id)

    config.runs_dir.mkdir(parents=True, exist_ok=True)
    if is_sj:
        config.ctx_state_home.mkdir(parents=True, exist_ok=True)

    slug = f"{resolved_id}__{arm_norm}__t{trial_num}"
    run_dir = config.runs_dir / slug
    meta_dir = config.runs_dir / f"{slug}_meta"
    result_file = meta_dir / "result.json"

    trial_inputs: dict[str, Any] = {
        "task_inputs": task_inputs_digest(Path(spec.case_dir)),
        "bwrap_sandbox": sandbox_kind == "bwrap",
        "ctx_version": ctx_version(config.ctx_bin) if is_sj else None,
    }
    if is_sj:
        # The code itself, not only the version it reports: the ctx the trial launches and
        # the ctx package this process runs in-process (residence, deliverable audits, the
        # hook grammar). The raw arm uses neither, so its fingerprint leaves both out.
        trial_inputs["ctx_code"] = ctx_code_identity(config)
        trial_inputs["harness_ctx_code"] = harness_ctx_code_identity()
    fingerprint = config_fingerprint(config, _HARNESS_BEHAVIOUR, trial_inputs)
    if config.resume and result_file.is_file():
        try:
            saved = json.loads(result_file.read_text(encoding="utf-8"))
        except Exception:
            saved = None
        if isinstance(saved, dict) and not _accept_for_config(saved, config, fingerprint, slug):
            _supersede(config, run_dir, meta_dir, saved)  # SupersedeError stops this trial
            saved = None
        try:
            if saved is None:
                pass
            elif str(saved.get("termination", "")) not in _RESUMABLE_ERROR_TERMINATIONS:
                cached = RunMetrics.from_dict(saved)
                if not cached.sandbox and str(saved.get("config_fingerprint") or "") == fingerprint:
                    # The fingerprint records whether bwrap was active, so a matching
                    # result ran in the current mode; record it (a legacy result accepted
                    # on model_id alone stays "" = unknown).
                    cached.sandbox = sandbox_kind
                    result_file.write_text(
                        json.dumps(cached.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
                    )
                if cached.grading_error:
                    # The agent finished; only the grader failed. Grade its outputs again.
                    regraded = _regrade_cached(
                        config, spec, provider, cached, run_dir, meta_dir, result_file, slot
                    )
                    if regraded is not None:
                        with _PRINT_LOCK:
                            print(f"  [regraded] {_format_progress(regraded)}", flush=True)
                        return regraded
                    with _PRINT_LOCK:
                        print(
                            f"  [rerun] {slug}: grading error and the trial's directories are gone",
                            flush=True,
                        )
                else:
                    with _PRINT_LOCK:
                        print(f"  [cached] {_format_progress(cached)}", flush=True)
                    return cached
        except Exception:
            pass

    for path in (run_dir, meta_dir):
        if path.exists():
            shutil.rmtree(path)
    run_dir.mkdir(parents=True, exist_ok=True)
    meta_dir.mkdir(parents=True, exist_ok=True)

    metrics = RunMetrics(
        task_id=resolved_id,
        task_rel=task_rel,
        category=category,
        arm=arm_norm,
        model_id=config.model,
        wave=wave,
        bucket=bucket,
        config_fingerprint=fingerprint,
        sandbox=sandbox_kind,
    )

    sandbox = TaskSandbox(config, spec, run_dir, meta_dir, slot)
    browser: BrowserSession | None = None
    local_browser_pool: BrowserPool | None = None
    t0 = time.time()

    with _PRINT_LOCK:
        print(
            f">>> START [s{slot:02d}] {resolved_id} (arm={arm_norm}, {category})",
            flush=True,
        )

    try:
        try:
            sandbox.setup()
        except Exception as setup_exc:
            metrics.termination = "setup_error"
            metrics.notes.append(f"setup: {setup_exc}")
            raise

        ports_ref = (
            sandbox.compiled_ports
            if hasattr(sandbox, "compiled_ports")
            else sandbox.port_map
        )
        requires_browser = bool(getattr(spec, "requires_browser", False))
        if requires_browser:
            active_pool = browser_pool
            if active_pool is None:
                local_browser_pool = BrowserPool(config.chrome_bin)
                active_pool = local_browser_pool
            browser = active_pool.acquire_session(ports_ref, enable_dom_diff=is_sj)

        task_prompt = build_prompt(Path(spec.case_dir), ports_ref)
        declared_deliverables = (
            extract_declared_deliverables(task_prompt) if is_sj else []
        )

        max_steps = max(1, int(config.max_steps))
        host = ToolHost(
            config,
            run_dir,
            meta_dir,
            ports_ref,
            sandbox.service_env(),
            metrics,
            browser,
            arm=arm_norm,
            max_steps=max_steps,
            declared_deliverables=declared_deliverables,
            task_markdown=task_prompt if is_sj else "",
        )

        residence: WatermarkResidence | None = (
            WatermarkResidence(is_mutating_call=is_mutating_tool_call) if is_sj else None
        )
        requires_network = _task_requires_network(spec)
        tools = build_tools(
            requires_browser=requires_browser,
            requires_network=requires_network,
            enable_state_note=is_sj,
        )

        prompt_text = task_prompt
        deliverable_contract_text = ""
        if is_sj:
            ws_dir = run_dir / "workspace"
            contract_root = ws_dir if ws_dir.is_dir() else run_dir
            deliverable_contract_text = format_deliverable_contract(
                task_prompt,
                contract_root,
            )
            birth_inv = format_workspace_inventory(run_dir, task_markdown=task_prompt)
            if birth_inv:
                prompt_text = f"{task_prompt}\n{birth_inv}\n"

        system_instruction = SJ_EXECUTION_PROTOCOL if is_sj else None
        contents: list[dict[str, Any]] = [
            {"role": "user", "parts": [{"text": prompt_text}]}
        ]

        last_sigs: list[str] | None = None
        same_run = 0
        dup_turn_streak = 0
        micro_py_streak = 0
        micro_hints = 0
        next_tool_config: dict[str, Any] | None = None
        empty_turns = 0
        text_nudges = 0
        service_delta_gate_hits = 0
        session_id = f"accio-{arm_norm}-{resolved_id[:_SESSION_TASK_PREFIX_CHARS]}-{int(t0)}"

        with (meta_dir / "trajectory.jsonl").open("w", encoding="utf-8") as traj:
            for step in range(1, max_steps + 1):
                host.step = step
                metrics.steps = step
                step_t0 = time.time()

                step_tool_config = next_tool_config
                next_tool_config = None
                if (
                    is_sj
                    and step == max_steps
                    and host.has_user_deliverables()
                    and not host.completed
                ):
                    step_tool_config = {
                        "functionCallingConfig": {
                            "mode": "ANY",
                            "allowedFunctionNames": ["present_files"],
                        }
                    }
                    metrics.loop_gate_triggers += 1

                try:
                    data = provider.generate(
                        config.model,
                        contents,
                        tools,
                        session_id=session_id,
                        system_instruction=system_instruction,
                        tool_config=step_tool_config,
                    )
                except Exception as api_exc:
                    metrics.notes.append(f"step {step}: {api_exc}")
                    metrics.termination = "api_error"
                    break

                lat_s = time.time() - step_t0
                usage = data.get("usageMetadata") or {}
                p_tok = int(usage.get("promptTokenCount", 0) or 0)
                c_tok = int(usage.get("cachedContentTokenCount", 0) or 0)
                t_tok = int(usage.get("thoughtsTokenCount", 0) or 0)
                o_tok = int(usage.get("candidatesTokenCount", 0) or 0)

                metrics.prompt_tokens += p_tok
                metrics.cached_tokens += c_tok
                metrics.thought_tokens += t_tok
                metrics.output_tokens += o_tok
                metrics.max_step_prompt_tokens = max(metrics.max_step_prompt_tokens, p_tok)
                if is_sj:
                    host.update_window_telemetry(config.model, step, usage)
                if residence is not None:
                    residence.observe_cache_usage(p_tok, c_tok)

                cand = (data.get("candidates") or [{}])[0]
                finish = str(cand.get("finishReason", ""))
                content = cand.get("content") or {"role": "model", "parts": []}
                parts = content.get("parts") or []
                texts = [
                    str(p.get("text", ""))
                    for p in parts
                    if "text" in p and not p.get("thought")
                ]
                calls = [p["functionCall"] for p in parts if "functionCall" in p]
                metrics.message_chars += sum(len(t) for t in texts)

                if not parts:
                    empty_turns += 1
                    if empty_turns >= _MAX_EMPTY_TURNS:
                        metrics.termination = "empty_turns"
                        break
                    contents.append({"role": "user", "parts": [{"text": "Continue."}]})
                    continue

                contents.append(content)
                model_turn_idx = len(contents) - 1

                if not calls:
                    traj.write(
                        json.dumps(
                            {
                                "step": step,
                                "lat_s": round(lat_s, 2),
                                "p_tok": p_tok,
                                "c_tok": c_tok,
                                "t_tok": t_tok,
                                "o_tok": o_tok,
                                "finish": finish,
                                "text": "\n".join(texts)[:_TRAJ_TEXT_PREVIEW_CHARS],
                                "calls": [],
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                    traj.flush()

                    visible_text = "\n".join(texts).strip()
                    if is_sj and not visible_text and not host.completed:
                        empty_turns += 1
                        if empty_turns >= _MAX_EMPTY_TURNS:
                            metrics.termination = "empty_turns"
                            break
                        contents.append(
                            {
                                "role": "user",
                                "parts": [
                                    {
                                        "text": (
                                            "Your previous turn produced no tool calls and no text. "
                                            "Continue executing the remaining steps and call present_files when done."
                                        )
                                    }
                                ],
                            }
                        )
                        continue

                    has_deliverables = host.has_user_deliverables()
                    if is_sj and not host.completed:
                        sd_blocking = False
                        sd_issues: list[str] = []
                        sd_receipt = ""
                        if service_delta_gate_hits < _MAX_SERVICE_DELTA_GATE_HITS:
                            sd_blocking, sd_issues, sd_receipt = _collect_service_and_ui_gate(host)

                        if not host.emission_gated_once:
                            pre_exit_audit = audit_deliverables(
                                run_dir,
                                [],
                                declared_paths=host.declared_deliverables,
                                task_markdown=task_prompt,
                            )
                            ext_adv = _collect_external_advisories(host)
                            zero_mut = not has_deliverables and host.mutation_epoch == 0
                        else:
                            pre_exit_audit = None
                            ext_adv = []
                            zero_mut = False

                        should_audit_gate = bool(
                            pre_exit_audit is not None and pre_exit_audit.should_gate
                        )
                        if should_audit_gate or ext_adv or sd_blocking or zero_mut:
                            text_nudges += 1
                            host.emission_gated_once = True
                            metrics.emission_gate_hits += 1
                            if sd_blocking:
                                service_delta_gate_hits += 1
                            if should_audit_gate or ext_adv or sd_blocking:
                                gate_nudge = _format_gate_message(
                                    prefix_header="[ctx-emission-gate] Workspace/service state check:",
                                    pre_exit_audit=pre_exit_audit if should_audit_gate else None,
                                    ext_adv=ext_adv,
                                    sd_issues=sd_issues if sd_blocking else [],
                                    sd_receipt=sd_receipt,
                                )
                            else:
                                gate_nudge = (
                                    "[ctx-emission-gate] No output deliverables or service/UI mutations were recorded yet. "
                                    "Complete all required changes or output files and call present_files when finished."
                                )
                            contents.append({"role": "user", "parts": [{"text": gate_nudge}]})
                            continue

                    if host.completed or has_deliverables or step >= _MIN_STEPS_FOR_TEXT_COMPLETE:
                        if (
                            text_nudges >= 1
                            or has_deliverables
                            or requires_browser
                            or category in ("cli", "api")
                        ):
                            metrics.termination = "completed_text"
                            break
                    text_nudges += 1
                    if text_nudges >= _MAX_TEXT_NUDGES:
                        metrics.termination = "no_deliverables"
                        break
                    contents.append(
                        {
                            "role": "user",
                            "parts": [
                                {
                                    "text": (
                                        "If deliverables under /task/outputs are required, save them "
                                        "and call present_files; otherwise confirm final completion."
                                    )
                                }
                            ],
                        }
                    )
                    continue

                if residence is not None:
                    residence.record_model_turn(model_turn_idx, t_tok)

                sigs = [
                    canonical_tool_sig(
                        str(c.get("name") or ""),
                        c.get("args") or {},
                        normalize_meta=is_sj,
                    )
                    for c in calls
                ]
                if sigs == last_sigs:
                    same_run += 1
                else:
                    same_run = 0
                    last_sigs = sigs
                if same_run >= _MAX_IDENTICAL_TURN_STREAK:
                    metrics.termination = "loop_detected"
                    break

                responses: list[dict[str, Any]] = []
                step_calls: list[dict[str, Any]] = []
                all_dups_this_turn = True
                for call in calls:
                    c_name = str(call.get("name") or "")
                    c_args = call.get("args") or {}
                    fr, n_chars, head = host.execute_part(c_name, c_args)
                    if not host.last_call_was_dup:
                        all_dups_this_turn = False
                    step_calls.append(
                        {
                            "tool": c_name,
                            "result_chars": n_chars,
                            "head": head[:_TRAJ_HEAD_PREVIEW_CHARS],
                        }
                    )
                    responses.append(fr)

                if is_sj:
                    if all_dups_this_turn:
                        dup_turn_streak += 1
                        if dup_turn_streak >= _MAX_DUP_TURN_STREAK:
                            metrics.loop_gate_triggers += 1
                            dup_turn_streak = 0
                            if host.has_user_deliverables():
                                next_tool_config = {
                                    "functionCallingConfig": {
                                        "mode": "ANY",
                                        "allowedFunctionNames": ["present_files"],
                                    }
                                }
                            else:
                                next_tool_config = {
                                    "functionCallingConfig": {"mode": "NONE"}
                                }
                    else:
                        dup_turn_streak = 0

                    if len(calls) == 1 and str(calls[0].get("name") or "") == "bash":
                        bcmd = str((calls[0].get("args") or {}).get("command") or "")
                        if (
                            "python" in bcmd
                            and not MUTATING_SHELL_RE.search(bcmd)
                            and 0 < step_calls[0]["result_chars"] < _MICRO_PY_MAX_RESULT_CHARS
                        ):
                            micro_py_streak += 1
                            if (
                                micro_py_streak >= _MICRO_PY_STREAK_LIMIT
                                and micro_hints < _MICRO_PY_MAX_HINTS
                            ):
                                resp_obj = responses[0]["functionResponse"]["response"]
                                cur_out = str(resp_obj.get("output") or "")
                                if "[ctx-hint: batch remaining checks" not in cur_out:
                                    resp_obj["output"] = cur_out + _MICRO_PY_HINT
                                micro_hints += 1
                                micro_py_streak = 0
                        else:
                            micro_py_streak = 0
                    else:
                        micro_py_streak = 0

                if is_sj and host.completed:
                    sd_blocking = False
                    sd_issues = []
                    sd_receipt = ""
                    if service_delta_gate_hits < _MAX_SERVICE_DELTA_GATE_HITS:
                        sd_blocking, sd_issues, sd_receipt = _collect_service_and_ui_gate(host)
                    ext_adv = (
                        _collect_external_advisories(host)
                        if not host.emission_gated_once
                        else []
                    )
                    if ext_adv or sd_blocking:
                        host.completed = False
                        host.emission_gated_once = True
                        metrics.emission_gate_hits += 1
                        if sd_blocking:
                            service_delta_gate_hits += 1
                        gate_msg = _format_gate_message(
                            prefix_header="[ctx-emission-gate] Pre-finalization verification check found items to confirm:",
                            ext_adv=ext_adv,
                            sd_issues=sd_issues if sd_blocking else [],
                            sd_receipt=sd_receipt,
                        )
                        updated_pf_resp = False
                        for resp in reversed(responses):
                            f_resp = resp.get("functionResponse")
                            if isinstance(f_resp, dict) and f_resp.get("name") == "present_files":
                                f_resp["response"] = {"output": gate_msg}
                                updated_pf_resp = True
                                break
                        if not updated_pf_resp:
                            responses.append({"text": gate_msg})

                contents.append({"role": "user", "parts": responses})
                has_final_output_write = bool(
                    getattr(host, "outputs_written", None)
                ) or host.has_user_deliverables()
                if residence is not None and not host.completed:
                    n_comp, _ = residence.maybe_compact(
                        contents,
                        remaining_steps=max(0, max_steps - step),
                        has_final_output_write=has_final_output_write,
                        min_turn_gap=_COMPACT_MIN_TURN_GAP,
                        deliverable_contract=deliverable_contract_text,
                    )
                    if n_comp:
                        metrics.compaction_epochs = residence.epochs
                        metrics.thoughts_compacted = residence.thoughts_compacted
                        metrics.state_notes_preserved = residence.state_notes_preserved
                        metrics.mutations_preserved = residence.mutations_preserved
                        metrics.ui_controls_stripped_chars = residence.ui_controls_stripped_chars

                traj.write(
                    json.dumps(
                        {
                            "step": step,
                            "lat_s": round(lat_s, 2),
                            "p_tok": p_tok,
                            "c_tok": c_tok,
                            "t_tok": t_tok,
                            "o_tok": o_tok,
                            "finish": finish,
                            "calls": step_calls,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                traj.flush()

                if host.completed:
                    metrics.termination = "present_files"
                    break
            else:
                metrics.termination = "hard_rail"

        metrics.wall_clock_s = round(time.time() - t0, 1)

        # Close trial BrowserContext before grading while keeping mock servers running.
        if browser is not None:
            browser.close()
            browser = None

        _grade_into(config, provider, sandbox, meta_dir, metrics)

    except Exception as exc:
        metrics.wall_clock_s = round(time.time() - t0, 1)
        if not metrics.termination:
            metrics.termination = "error"
        err_text = str(exc)[:_MAX_NOTE_ERR_CHARS]
        if not any(err_text in n for n in metrics.notes):
            metrics.notes.append(err_text)
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        if local_browser_pool is not None:
            try:
                local_browser_pool.close_thread_browser()
            finally:
                local_browser_pool.close_all()
        sandbox.cleanup()

    result_file.write_text(
        json.dumps(metrics.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    with _PRINT_LOCK:
        print(f"<<< DONE  [s{slot:02d}] {_format_progress(metrics)}", flush=True)
    return metrics
