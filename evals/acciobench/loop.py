"""Single-trial execution loop for AccioBench supporting paired ``raw`` and ``sj`` arms."""

from __future__ import annotations

import json
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

from ctx.deliverables import audit_deliverables, extract_declared_deliverables  # noqa: E402
from ctx.residence import WatermarkResidence  # noqa: E402

from .browser import BrowserPool, BrowserSession  # noqa: E402
from .config import BenchConfig, RunMetrics, load_cab_task  # noqa: E402
from .host import ToolHost  # noqa: E402
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

__all__ = ["run_trial"]

_VALID_ARMS = frozenset({"raw", "sj"})
_RESUMABLE_ERROR_TERMINATIONS = frozenset({"", "api_error", "setup_error", "error"})

_MAX_EMPTY_TURNS = 3
_MAX_TEXT_NUDGES = 3
_MIN_STEPS_FOR_TEXT_COMPLETE = 4
_MAX_IDENTICAL_TURN_STREAK = 4
_MAX_DUP_TURN_STREAK = 2

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
_MAX_CHECK_REASON_CHARS = 140
_MAX_VERIFIER_ERR_CHARS = 240
_MAX_NOTE_ERR_CHARS = 300

_PRINT_LOCK = threading.Lock()


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


def _format_progress(metrics: RunMetrics) -> str:
    return (
        f"{metrics.task_id:<44} | arm={metrics.arm:<3} | {metrics.category:<7} | "
        f"stp {metrics.steps:>3} | gen {metrics.gen_tokens:>7,} | "
        f"tot {metrics.total_tokens:>9,} | {metrics.wall_clock_s:>5.1f}s | "
        f"pass={'Y' if metrics.passed else 'N'} cap={metrics.capacity_score:.2f} "
        f"({metrics.checks_passed}/{metrics.checks_total}) | {metrics.termination}"
    )


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

    spec = load_cab_task(config.dataset_root, task_id)
    resolved_id, task_rel, category = _resolve_task_metadata(config, spec, task_id)

    config.runs_dir.mkdir(parents=True, exist_ok=True)
    if is_sj:
        config.ctx_state_home.mkdir(parents=True, exist_ok=True)

    slug = f"{resolved_id}__{arm_norm}__t{trial_num}"
    run_dir = config.runs_dir / slug
    meta_dir = config.runs_dir / f"{slug}_meta"
    result_file = meta_dir / "result.json"

    if config.resume and result_file.is_file():
        try:
            saved = json.loads(result_file.read_text(encoding="utf-8"))
            if str(saved.get("termination", "")) not in _RESUMABLE_ERROR_TERMINATIONS:
                cached = RunMetrics.from_dict(saved)
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

        prompt_text = build_prompt(Path(spec.case_dir), ports_ref)
        declared_deliverables = (
            extract_declared_deliverables(prompt_text) if is_sj else []
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

        if is_sj:
            birth_inv = format_workspace_inventory(run_dir)
            if birth_inv:
                prompt_text = f"{prompt_text}\n{birth_inv}\n"

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
                    if is_sj and not host.completed and text_nudges == 0:
                        pre_exit_audit = audit_deliverables(
                            run_dir,
                            [],
                            declared_paths=host.declared_deliverables,
                        )
                        ext_adv = host.external_state_advisories()
                        if (
                            pre_exit_audit.should_gate
                            or bool(ext_adv)
                            or (not has_deliverables and host.mutation_epoch == 0)
                        ):
                            text_nudges += 1
                            host.emission_gated_once = True
                            metrics.emission_gate_hits += 1
                            if pre_exit_audit.should_gate or ext_adv:
                                parts_msg: list[str] = []
                                if pre_exit_audit.should_gate:
                                    parts_msg.append(pre_exit_audit.format_gate_message())
                                if ext_adv:
                                    header = "" if parts_msg else "[ctx-emission-gate] Workspace/service state check:\n"
                                    parts_msg.append(
                                        header + "\n".join(f"- {item}" for item in ext_adv)
                                    )
                                gate_nudge = (
                                    "\n".join(parts_msg)
                                    + "\nResolve any issues above and call present_files to finalize."
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

                contents.append({"role": "user", "parts": responses})
                if residence is not None and not host.completed:
                    n_comp, _ = residence.maybe_compact(
                        contents,
                        remaining_steps=max(0, max_steps - step),
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

        judge_tok = (
            provider.judge_access_token() if config.judge_provider == "vertex" else ""
        )
        reward_json = meta_dir / "reward.json"
        grade_data = sandbox.grade(reward_json, judge_token=judge_tok)
        if "_error" not in grade_data:
            raw_score = float(grade_data.get("score", 0.0) or 0.0)
            cp_cnt = int(grade_data.get("checks_passed", 0) or 0)
            ct_cnt = int(grade_data.get("checks_total", 0) or 0)
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
                for chk in (grade_data.get("checks_breakdown") or [])
                if not chk.get("passed")
            ]
        else:
            err_msg = str(grade_data.get("_error", ""))[:_MAX_VERIFIER_ERR_CHARS]
            metrics.notes.append(f"verifier error: {err_msg}")

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
