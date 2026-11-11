"""Programmatic standalone HTML benchmark & trajectory report generator for AccioBench.

Builds a self-contained, single-file interactive HTML evaluation report directly
from trial artifacts (``result.json``, ``trajectory.jsonl``, ``reward.json``,
optional ``ctx`` sidecar manifests, task prompts, historical baseline JSON/HTML
artifacts, and optional multi-arm ablation summaries).
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import gzip
import html as html_lib
import json
import math
from pathlib import Path
import random
import re
import statistics
from typing import Any

from .config import RunMetrics
from .html_template import render_html_document
from .report import (
    _EXCLUDED_PATH_PARTS,
    _INFRA_ERROR_TERMINATIONS,
    _collapse_arm_runs,
    _infer_arm_from_legacy,
    count_grading_errors,
    load_baseline_records,
    load_run_results,
)

__all__ = [
    "build_html_report_payload",
    "parse_accio_baseline_html",
    "parse_baseline_html_spec",
    "render_html_report",
]

_ARM_PALETTE = (
    {"color": "#1A73E8", "soft_bg": "#E8F0FE", "badge_fg": "#174EA6"},
    {"color": "#E37400", "soft_bg": "#FEF7E0", "badge_fg": "#B06000"},
    {"color": "#5F6368", "soft_bg": "#F1F3F4", "badge_fg": "#3C4043"},
    {"color": "#7C3AED", "soft_bg": "#F3E8FF", "badge_fg": "#6D28D9"},
    {"color": "#0D9488", "soft_bg": "#CCFBF1", "badge_fg": "#0F766E"},
)

_FAILURE_META = (
    {
        "key": "near_miss_80",
        "label": "Near-miss single assertion (>=80% checks met)",
        "desc": "Completed deliverables and passed >=80% of rubric checks, missing one formatting, count, or citation check.",
        "tone": "primary",
    },
    {
        "key": "incomplete_mutation",
        "label": "Incomplete UI or CLI side-effect mutation",
        "desc": "Missed a secondary state mutation across mail labels/stars/drafts, booking fields, or cloud folder uploads.",
        "tone": "warning",
    },
    {
        "key": "domain_rule_mismatch",
        "label": "Domain calculation or SKU rule mismatch",
        "desc": "Completed workflow cleanly but diverged on FX tie-break, tier threshold, taxonomy mapping, or price rounding.",
        "tone": "neutral",
    },
    {
        "key": "ceiling_or_gate",
        "label": "Hit step ceiling or mock auth gate",
        "desc": "Reached the step safety ceiling or encountered an HTTP 403 mock service gate before completing all checks.",
        "tone": "danger",
    },
    {
        "key": "missing_deliverable",
        "label": "Exited before writing required deliverable",
        "desc": "Concluded with a text summary or partial state before writing one of the verifier's required output files.",
        "tone": "danger",
    },
)


def _wilson_ci(k: int, n: int, z: float = 1.959964) -> list[float]:
    if n <= 0:
        return [0.0, 0.0]
    p = k / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2.0 * n)) / denom
    half = (z * math.sqrt((p * (1.0 - p) + z * z / (4.0 * n)) / n)) / denom
    return [round(max(0.0, center - half) * 100.0, 1), round(min(1.0, center + half) * 100.0, 1)]


def _boot_mean_ci(vals: list[float], iters: int = 3000, seed: int = 202611) -> list[float]:
    if not vals:
        return [0.0, 0.0]
    rng = random.Random(seed)
    n = len(vals)
    means = sorted(sum(vals[rng.randrange(n)] for _ in range(n)) / n for _ in range(iters))
    return [round(means[int(0.025 * iters)] * 100.0, 1), round(means[int(0.975 * iters)] * 100.0, 1)]


def _mcnemar_p(b: int, c: int) -> float:
    m = b + c
    if m == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(m, i) for i in range(k + 1)) * (0.5**m)
    return round(min(1.0, 2.0 * tail), 4)


def _wilcoxon_p(diffs: list[float]) -> float:
    nz = [d for d in diffs if abs(d) > 1e-12]
    n = len(nz)
    if n == 0:
        return 1.0
    abs_sorted = sorted((abs(d), i) for i, d in enumerate(nz))
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and abs(abs_sorted[j + 1][0] - abs_sorted[i][0]) < 1e-12:
            j += 1
        avg_r = (i + 1 + j + 1) / 2.0
        for u in range(i, j + 1):
            ranks[abs_sorted[u][1]] = avg_r
        i = j + 1
    w_pos = sum(r for r, d in zip(ranks, nz) if d > 0)
    w_neg = sum(r for r, d in zip(ranks, nz) if d < 0)
    w = min(w_pos, w_neg)
    mean_w = n * (n + 1) / 4.0
    var_w = n * (n + 1) * (2 * n + 1) / 24.0
    if var_w <= 0:
        return 1.0
    z = (abs(w - mean_w) - 0.5) / math.sqrt(var_w)
    return round(math.erfc(max(0.0, z) / math.sqrt(2.0)), 6)


def _quantile(vals: list[float], q: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    idx = (len(s) - 1) * q
    lo, hi = int(math.floor(idx)), int(math.ceil(idx))
    return s[lo] + (s[hi] - s[lo]) * (idx - lo)


def _short_model_label(model_id: str) -> str:
    m = re.search(r"(\d+\.\d+)[- ]?([a-zA-Z]+)?", model_id or "")
    if m:
        ver = m.group(1)
        suf = (m.group(2) or "").capitalize()
        return f"{ver} {suf}".strip() if suf else ver
    return model_id or "Model"


def _detect_ctx_version() -> str:
    try:
        import ctx  # type: ignore[import-not-found]

        ver = str(getattr(ctx, "__version__", "") or "").strip()
        if ver:
            return f"v{ver}" if not ver.startswith("v") else ver
    except Exception:
        pass
    return ""


def _classify_failure(
    *,
    passed: bool,
    termination: str,
    capacity_score: float,
    category: str,
    failed_checks: list[str],
) -> str:
    if passed:
        return "passed"
    fc_join = " ".join(failed_checks).lower()
    if termination in ("hard_rail", "max_steps", "loop_detected") or "403" in fc_join:
        return "ceiling_or_gate"
    if capacity_score == 0.0 or "no such file or directory" in fc_join or "missing or empty" in fc_join:
        return "missing_deliverable"
    if capacity_score >= 0.80:
        return "near_miss_80"
    if category in ("browser", "cli", "api") and any(
        kw in fc_join
        for kw in (
            "label",
            "star",
            "draft",
            "pickup",
            "calendar",
            "scheduled_send",
            "collaborations",
            "upload",
            "marked_read",
            "handoff_page",
            "extra_pages",
        )
    ):
        return "incomplete_mutation"
    return "domain_rule_mismatch"


def parse_baseline_html_spec(spec: str) -> tuple[str, Path]:
    """Parse ``LABEL=PATH`` or ``PATH`` into ``(arm_label, resolved_path)``."""
    if "=" in spec:
        label, raw_path = spec.split("=", 1)
        path = Path(raw_path).expanduser().resolve()
        clean_label = label.strip() or path.stem
        return clean_label, path
    path = Path(spec).expanduser().resolve()
    return path.stem, path


def parse_accio_baseline_html(path: Path) -> dict[str, dict[str, Any]]:
    """Extract per-task validation checks and step trajectories from an Accio HTML report."""
    if not path.is_file():
        return {}
    raw = path.read_text(encoding="utf-8", errors="ignore")
    trials_pos = raw.find('data-tab="trials"')
    table_end = raw.find("</table>", trials_pos) if trials_pos != -1 else -1
    table_html = raw[trials_pos:table_end] if (trials_pos != -1 and table_end != -1) else ""

    chk_by_task: dict[str, str] = {}
    status_by_task: dict[str, bool] = {}
    for status_attr, query, row_body in re.findall(
        r'<tr data-status="([^"]+)" data-query="([^"]+)">(.*?)</tr>', table_html, re.DOTALL
    ):
        parts = query.split()
        if len(parts) >= 2:
            tname = parts[1].strip()
            status_by_task[tname] = status_attr.strip().lower() in ("passed", "success", "pass")
            cm = re.search(r"<span class='checks-total'>([^<]+)</span>", row_body)
            if cm:
                chk_by_task[tname] = cm.group(1).strip()

    trial_splits = re.split(r'<details class="trial-details" id="trial-(\d+)"', raw)
    out: dict[str, dict[str, Any]] = {}
    for i in range(1, len(trial_splits), 2):
        chunk = trial_splits[i + 1]
        m_name = re.search(r'<span class="trial-name">\d+\.\s*([^<]+)</span>', chunk)
        if not m_name:
            continue
        task_id = m_name.group(1).strip()

        checks: list[dict[str, Any]] = []
        m_val = re.search(
            r"<h4>Validation checks</h4><table class='summary'><tbody>(.*?)</tbody></table>",
            chunk,
            re.DOTALL,
        )
        if m_val:
            for cid, passed_s, _, reason in re.findall(
                r"<tr><td>([^<]*)</td><td>([^<]*)</td><td>([^<]*)</td><td>(.*?)</td></tr>",
                m_val.group(1),
                re.DOTALL,
            ):
                checks.append(
                    {
                        "id": html_lib.unescape(cid.strip()),
                        "p": passed_s.strip() == "True",
                        "r": html_lib.unescape(re.sub(r"<[^>]+>", "", reason).strip())[:240],
                    }
                )

        m_ft = re.search(r"<h2>Agent Final Text</h2>.*?<pre>(.*?)</pre>", chunk, re.DOTALL)
        final_text = html_lib.unescape(m_ft.group(1)).strip()[:450] if m_ft else ""

        steps: list[dict[str, Any]] = []
        pos_traj = chunk.find("<h2>Trajectory</h2>")
        traj_chunk = chunk[pos_traj:] if pos_traj != -1 else ""
        parts = traj_chunk.split('<div class="step-card">')[1:]
        msg_chars_sum = 0
        reas_chars_sum = 0
        for idx, sc in enumerate(parts, 1):
            r_m = re.search(
                r"Model reasoning &middot; (\d+) chars</summary><pre><code>(.*?)</code></pre>",
                sc,
                re.DOTALL,
            )
            r_chars = int(r_m.group(1)) if r_m else 0
            r_head = html_lib.unescape(r_m.group(2)).strip()[:240] if r_m else ""
            reas_chars_sum += r_chars

            m_m = re.search(
                r"Model message &middot; (\d+) chars</strong><pre><code>(.*?)</code></pre>",
                sc,
                re.DOTALL,
            )
            m_chars = int(m_m.group(1)) if m_m else 0
            m_head = html_lib.unescape(m_m.group(2)).strip()[:240] if m_m else ""
            msg_chars_sum += m_chars

            t_calls = re.findall(
                r'<div class="tool-call"><span class="fn-name">([^<]+)</span><pre><code>(.*?)</code></pre></div>',
                sc,
                re.DOTALL,
            )
            calls_parsed = []
            for fn, arg_raw in t_calls:
                arg_txt = html_lib.unescape(arg_raw).strip()
                summary = arg_txt
                try:
                    ao = json.loads(arg_txt)
                    if isinstance(ao, dict):
                        for k in ("command", "file_path", "path", "pattern", "url", "selector", "text"):
                            if k in ao and ao[k]:
                                summary = str(ao[k])
                                break
                        else:
                            summary = json.dumps(ao, ensure_ascii=False)
                except Exception:
                    pass
                calls_parsed.append(
                    {
                        "t": fn.strip(),
                        "sum": summary.replace("\n", " ").strip()[:140],
                        "arg": arg_txt[:320],
                    }
                )

            o_m = re.search(
                r"<div class='observation-result'>.*?<pre><code>(.*?)</code></pre>", sc, re.DOTALL
            )
            obs_head = html_lib.unescape(o_m.group(1)).strip()[:260] if o_m else ""
            steps.append(
                {
                    "s": idx,
                    "rc": r_chars,
                    "rh": r_head,
                    "mc": m_chars,
                    "mh": m_head,
                    "c": calls_parsed,
                    "o": obs_head,
                }
            )

        cp = sum(1 for c in checks if c["p"])
        ct = len(checks)
        chk_str = chk_by_task.get(task_id, f"{cp}/{ct}" if ct > 0 else "—")
        if ct == 0 and "/" in chk_str:
            try:
                a_s, b_s = chk_str.split("/", 1)
                cp, ct = int(a_s), int(b_s)
            except Exception:
                pass
        passed = status_by_task.get(task_id, (cp == ct and ct > 0))
        out[task_id] = {
            "chk_str": chk_str,
            "checks_passed": cp,
            "checks_total": ct,
            "passed": passed,
            "cap": (cp / ct) if ct > 0 else (1.0 if passed else 0.0),
            "checks": checks,
            "final": final_text,
            "steps": steps,
            "msg_chars": msg_chars_sum,
            "reas_chars": reas_chars_sum,
        }
    return out


def _discover_meta_dirs(runs_dir: Path) -> tuple[dict[tuple[str, str], Path], dict[tuple[str, str], dict[str, Any]], float]:
    """Map ``(task_id, arm)`` to its representative ``*_meta`` directory and raw ``result.json``."""
    root = Path(runs_dir)
    if not root.exists():
        return {}, {}, 0.0
    search_roots = [root / "runs", root] if (root / "runs").is_dir() and root.name != "runs" else [root]
    seen: set[Path] = set()
    meta_by_key: dict[tuple[str, str], Path] = {}
    raw_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    latest_mtime = 0.0

    for base in search_roots:
        for rf in sorted(base.rglob("result.json")):
            resolved = rf.resolve()
            if resolved in seen:
                continue
            rel_parts = set(rf.relative_to(base).parts[:-1])
            if rel_parts & _EXCLUDED_PATH_PARTS:
                continue
            seen.add(resolved)
            try:
                raw = json.loads(rf.read_text(encoding="utf-8"))
                mtime = rf.stat().st_mtime
            except Exception:
                continue
            if not isinstance(raw, dict) or not raw.get("task_id"):
                continue
            latest_mtime = max(latest_mtime, mtime)
            tid = str(raw["task_id"])
            arm = _infer_arm_from_legacy(raw)
            term = str(raw.get("termination") or "")
            key = (tid, arm)
            if key not in meta_by_key or term not in _INFRA_ERROR_TERMINATIONS:
                meta_by_key[key] = rf.parent
                raw_by_key[key] = raw
    return meta_by_key, raw_by_key, latest_mtime


def _load_ctx_manifests(ctx_state_dir: Path | None) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Load command invocation manifests from ``ctx_state/workspaces/*/manifests``."""
    by_trial: dict[tuple[str, str], list[dict[str, Any]]] = {}
    if ctx_state_dir is None or not ctx_state_dir.exists():
        return by_trial
    ws_root = ctx_state_dir / "workspaces" if (ctx_state_dir / "workspaces").is_dir() else ctx_state_dir
    if not ws_root.is_dir():
        return by_trial

    for ws in ws_root.iterdir():
        man_dir = ws / "manifests"
        if not man_dir.is_dir():
            continue
        items: list[dict[str, Any]] = []
        task_slug = ""
        arm_slug = "sj"
        for mf in man_dir.rglob("*.json"):
            try:
                md = json.loads(mf.read_text(encoding="utf-8"))
                cwd = str(md.get("cwd") or "")
                for part in reversed(Path(cwd).parts):
                    if "__" in part:
                        segs = part.split("__")
                        task_slug = segs[0]
                        arm_slug = _infer_arm_from_legacy({"arm_key": segs[1]})
                        break
                argv = "\n".join(md.get("argv") or []).strip()
                ec = (md.get("result") or {}).get("exitCode")
                so_b = int(((md.get("streams") or {}).get("stdout") or {}).get("bytes", 0) or 0)
                se_b = int(((md.get("streams") or {}).get("stderr") or {}).get("bytes", 0) or 0)
                items.append(
                    {
                        "ts": mf.stat().st_mtime,
                        "cmd": argv[:420],
                        "ec": ec,
                        "raw_b": so_b + se_b,
                    }
                )
            except Exception:
                continue
        if task_slug and items:
            items.sort(key=lambda x: x["ts"])
            by_trial[(task_slug, arm_slug)] = items
    return by_trial


def _read_reward_checks(meta_dir: Path | None) -> list[dict[str, Any]]:
    if meta_dir is None:
        return []
    rew_path = meta_dir / "reward.json"
    if not rew_path.is_file():
        return []
    out: list[dict[str, Any]] = []
    try:
        rew = json.loads(rew_path.read_text(encoding="utf-8"))
        for cb in rew.get("checks_breakdown") or []:
            atoms_failed = [
                f"{a.get('id')}: {a.get('reason')}"
                for a in (cb.get("atoms") or [])
                if not a.get("passed", True)
            ]
            out.append(
                {
                    "id": str(cb.get("id") or ""),
                    "p": bool(cb.get("passed")),
                    "type": str(cb.get("check_type") or ""),
                    "desc": str(cb.get("description") or "")[:200],
                    "r": str(cb.get("reason") or "")[:240],
                    "af": atoms_failed[:4],
                }
            )
    except Exception:
        pass
    return out


def _read_live_trajectory(
    meta_dir: Path | None,
    ctx_mans: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if meta_dir is None:
        return []
    traj_path = meta_dir / "trajectory.jsonl"
    if not traj_path.is_file():
        return []

    steps: list[dict[str, Any]] = []
    bash_cursor = 0
    prev_ptok = 0
    epoch_counter = 0

    for line in traj_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.strip():
            continue
        try:
            so = json.loads(line)
        except Exception:
            continue
        ptok = int(so.get("p_tok") or 0)
        ctok = int(so.get("c_tok") or 0)
        ttok = int(so.get("t_tok") or 0)
        otok = int(so.get("o_tok") or 0)

        compacted_drop = 0
        epoch_idx = 0
        if prev_ptok > 0 and ptok < prev_ptok - 1500:
            epoch_counter += 1
            epoch_idx = epoch_counter
            compacted_drop = prev_ptok - ptok
        prev_ptok = ptok

        calls_clean = []
        has_digest = False
        for c in so.get("calls") or []:
            tname = str(c.get("tool") or "")
            rchars = int(c.get("result_chars") or 0)
            head = str(c.get("head") or "")
            cmd_str = ""
            ec = None
            raw_b = 0
            is_dig = ("run:" in head and ("next:" in head or "digest" in head.lower())) or (
                '"delivery": "digest"' in head
            )
            if tname == "bash" and bash_cursor < len(ctx_mans):
                m_item = ctx_mans[bash_cursor]
                bash_cursor += 1
                cmd_str = m_item["cmd"]
                ec = m_item["ec"]
                raw_b = m_item["raw_b"]
                if raw_b > 16384 and rchars < raw_b * 0.6:
                    is_dig = True
            if is_dig:
                has_digest = True
            if cmd_str:
                sum_lbl = cmd_str.splitlines()[0].strip()[:120]
            else:
                sum_lbl = head.replace("\n", " ").strip()[:110]
            calls_clean.append(
                {
                    "t": tname,
                    "rc": rchars,
                    "h": head[:320],
                    "cmd": cmd_str,
                    "sum": sum_lbl,
                    "ec": ec,
                    "rb": raw_b,
                    "dig": is_dig,
                }
            )

        steps.append(
            {
                "s": int(so.get("step") or len(steps) + 1),
                "lat": float(so.get("lat_s") or 0.0),
                "p": ptok,
                "cp": ctok,
                "t": ttok,
                "o": otok,
                "ep": epoch_idx,
                "drop": compacted_drop,
                "dig": has_digest,
                "txt": str(so.get("text") or "")[:280],
                "c": calls_clean,
            }
        )
    return steps


def _read_task_prompt(dataset_root: Path | None, task_rel: str, task_id: str) -> str:
    if dataset_root is None or not dataset_root.exists():
        return ""
    suite = dataset_root / "datasets_domain_v1" if (dataset_root / "datasets_domain_v1").is_dir() else dataset_root
    candidates = [suite / task_rel / "task.md"]
    if "/" not in task_rel:
        cat = task_id.split("-", 1)[0]
        candidates.append(suite / cat / task_id / "task.md")
    for p in candidates:
        if p.is_file():
            return p.read_text(encoding="utf-8", errors="ignore")[:2600]
    for p in suite.rglob(f"{task_id}/task.md"):
        return p.read_text(encoding="utf-8", errors="ignore")[:2600]
    return ""


def _load_optional_summary_meta(workdir: Path | None) -> dict[str, Any]:
    if workdir is None or not workdir.is_dir():
        return {}
    for name in ("full107_summary.json", "summary.json"):
        cand = workdir / name
        if cand.is_file():
            try:
                data = json.loads(cand.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
    return {}


def _parse_ablation(
    ablation_json: Path | None,
    tasks_by_rel: dict[str, dict[str, Any]],
    primary_arm: str,
    harness_label: str,
) -> tuple[dict[str, Any], dict[str, float]]:
    """Parse optional controlled multi-arm ablation JSON and return (ablation_payload, step_tok_ratios)."""
    ratios: dict[str, float] = {}
    if ablation_json is None or not ablation_json.is_file():
        return {"arms": [], "tasks": []}, ratios
    try:
        raw = json.loads(ablation_json.read_text(encoding="utf-8"))
    except Exception:
        return {"arms": [], "tasks": []}, ratios
    if not isinstance(raw, list) or not raw:
        return {"arms": [], "tasks": []}, ratios

    label_map = {
        "37base": "3.7 Baseline (Default Thinking)",
        "38base": "3.8 Baseline (Default Thinking)",
        "38sj": "3.8 + Straitjacket v1 (Entry Gate Sidecar)",
        "38sj_v2": "3.8 + Straitjacket v2 (+ WatermarkResidence)",
        "38sj_v3": "3.8 + Straitjacket v3 (+ state_note & Smart-Peg)",
    }
    seen_order: list[str] = []
    for item in raw:
        if isinstance(item, dict) and item.get("arm_key") and item["arm_key"] not in seen_order:
            seen_order.append(str(item["arm_key"]))
    preferred = [k for k in ("37base", "38base", "38sj", "38sj_v2", "38sj_v3") if k in seen_order]
    arm_keys = preferred + [k for k in seen_order if k not in preferred]

    ablation_arms: list[dict[str, Any]] = []
    tasks_map: dict[str, dict[str, Any]] = {}
    for akey in arm_keys:
        sub = [x for x in raw if isinstance(x, dict) and x.get("arm_key") == akey]
        if not sub:
            continue
        tot_tok = sum(int(x.get("total_tokens") or 0) for x in sub)
        tot_steps = sum(int(x.get("steps") or 0) for x in sub)
        p_tok = sum(int(x.get("prompt_tokens") or 0) for x in sub)
        c_tok = sum(int(x.get("cached_tokens") or 0) for x in sub)
        t_tok = sum(int(x.get("thought_tokens") or 0) for x in sub)
        cp = sum(int(x.get("checks_passed") or 0) for x in sub)
        ct = sum(int(x.get("checks_total") or 0) for x in sub)
        if tot_steps > 0 and tot_tok > 0:
            ratios[akey] = tot_tok / tot_steps
        ablation_arms.append(
            {
                "key": akey,
                "label": label_map.get(akey, akey),
                "n": len(sub),
                "tot_tokens": tot_tok,
                "med_tokens": round(statistics.median(int(x.get("total_tokens") or 0) for x in sub)),
                "mean_steps": round(statistics.mean(int(x.get("steps") or 0) for x in sub), 1),
                "med_wall_s": round(statistics.median(float(x.get("wall_clock_s") or 0.0) for x in sub), 1),
                "checks_passed": cp,
                "checks_total": ct,
                "checks_pct": round(cp / max(1, ct) * 100.0, 1),
                "cache_pct": round(c_tok / max(1, p_tok) * 100.0, 1) if p_tok > 0 else 0.0,
                "thought_tokens": t_tok,
            }
        )
        for x in sub:
            tk = str(x.get("task_key") or x.get("task_rel") or "")
            trel = str(x.get("task_rel") or "")
            entry = tasks_map.setdefault(tk, {"task_key": tk, "task_rel": trel, "arms": {}})
            entry["arms"][akey] = {
                "tok": int(x.get("total_tokens") or 0),
                "steps": int(x.get("steps") or 0),
                "chk": f"{int(x.get('checks_passed') or 0)}/{int(x.get('checks_total') or 0)}",
                "wall": round(float(x.get("wall_clock_s") or 0.0), 1),
            }

    overlap = [tasks_by_rel[v["task_rel"]] for v in tasks_map.values() if v["task_rel"] in tasks_by_rel]
    if overlap and len(overlap) == len(tasks_map):
        p_runs = [t["by_arm"][primary_arm] for t in overlap]
        tot_vtx = sum(r["vtx_total"] for r in p_runs)
        tot_p = sum(r["vtx_prompt"] for r in p_runs)
        tot_c = sum(r["vtx_cached"] for r in p_runs)
        tot_th = sum(r["vtx_thought"] for r in p_runs)
        cp = sum(r["checks_passed"] for r in p_runs)
        ct = sum(r["checks_total"] for r in p_runs)
        ablation_arms.append(
            {
                "key": "suite_overlap",
                "label": f"{harness_label} (Full-Suite Exact Overlap)",
                "n": len(overlap),
                "tot_tokens": tot_vtx,
                "med_tokens": round(statistics.median(r["vtx_total"] for r in p_runs)),
                "mean_steps": round(statistics.mean(r["steps"] for r in p_runs), 1),
                "med_wall_s": round(statistics.median(r["dur"] for r in p_runs), 1),
                "checks_passed": cp,
                "checks_total": ct,
                "checks_pct": round(cp / max(1, ct) * 100.0, 1),
                "cache_pct": round(tot_c / max(1, tot_p) * 100.0, 1) if tot_p > 0 else 0.0,
                "thought_tokens": tot_th,
            }
        )
        for v in tasks_map.values():
            ft = tasks_by_rel.get(v["task_rel"])
            if ft:
                pr = ft["by_arm"][primary_arm]
                v["arms"]["suite_overlap"] = {
                    "tok": pr["vtx_total"],
                    "steps": pr["steps"],
                    "chk": pr["checks_str"],
                    "wall": pr["dur"],
                }

    return {"arms": ablation_arms, "tasks": list(tasks_map.values())}, ratios


def _compute_scope(
    tasks: list[dict[str, Any]],
    arms_order: list[dict[str, Any]],
    primary_arm: str,
    baseline_arm: str | None,
    reference_arm: str | None,
    scope_id: str,
    label: str,
) -> dict[str, Any]:
    n = len(tasks)
    arms_stats: dict[str, Any] = {}
    for arm_meta in arms_order:
        aid = arm_meta["id"]
        sub = [t["by_arm"][aid] for t in tasks if aid in t["by_arm"]]
        m = len(sub)
        if m == 0:
            continue
        caps = [x["cap"] for x in sub]
        steps = [x["steps"] for x in sub]
        durs = [x["dur"] for x in sub]
        pk = sum(1 for x in sub if x["passed"])
        tot_steps = sum(steps)
        vtx_tot = sum(x["vtx_total"] for x in sub)
        vtx_p = sum(x["vtx_prompt"] for x in sub)
        vtx_c = sum(x["vtx_cached"] for x in sub)
        vtx_th = sum(x["vtx_thought"] for x in sub)
        vtx_o = sum(x["vtx_out"] for x in sub)
        rep_tot = sum(x["rep_tok"] for x in sub)
        arms_stats[aid] = {
            "id": aid,
            "name": arm_meta["name"],
            "short": arm_meta["short"],
            "n": m,
            "cap": round(statistics.mean(caps) * 100.0, 1),
            "cap_ci": _boot_mean_ci(caps),
            "pass_k": pk,
            "pass_pct": round(pk / m * 100.0, 1),
            "pass_ci": _wilson_ci(pk, m),
            "steps_tot": tot_steps,
            "steps_p50": int(round(_quantile(steps, 0.5))),
            "steps_p90": int(round(_quantile(steps, 0.9))),
            "lat_p50": int(round(_quantile(durs, 0.5))),
            "lat_p90": int(round(_quantile(durs, 0.9))),
            "wall_min": round(sum(durs) / 60.0, 1),
            "vtx_tot": vtx_tot,
            "tok_per_step": round(vtx_tot / max(1, tot_steps)) if vtx_tot > 0 else 0,
            "cache_hit_pct": round(vtx_c / max(1, vtx_p) * 100.0, 1) if vtx_p > 0 else 0.0,
            "vtx_thought": vtx_th,
            "vtx_out": vtx_o,
            "rep_tot": rep_tot,
            "is_calibrated_tok": bool(arm_meta.get("is_calibrated_tok")),
        }

    deltas: dict[str, Any] = {}
    if baseline_arm and primary_arm in arms_stats and baseline_arm in arms_stats:
        paired = [t for t in tasks if primary_arm in t["by_arm"] and baseline_arm in t["by_arm"]]
        p_st = arms_stats[primary_arm]
        b_st = arms_stats[baseline_arm]
        gained = sum(1 for t in paired if t["by_arm"][primary_arm]["passed"] and not t["by_arm"][baseline_arm]["passed"])
        lost = sum(1 for t in paired if not t["by_arm"][primary_arm]["passed"] and t["by_arm"][baseline_arm]["passed"])
        shorter = sum(1 for t in paired if t["by_arm"][primary_arm]["steps"] < t["by_arm"][baseline_arm]["steps"])
        deltas = {
            "cap_vs_base": round(p_st["cap"] - b_st["cap"], 1),
            "pass_vs_base_pp": round(p_st["pass_pct"] - b_st["pass_pct"], 1),
            "steps_vs_base_pct": round((p_st["steps_tot"] - b_st["steps_tot"]) / max(1, b_st["steps_tot"]) * 100.0, 1),
            "tok_per_step_vs_base_pct": (
                round((p_st["tok_per_step"] - b_st["tok_per_step"]) / max(1, b_st["tok_per_step"]) * 100.0, 1)
                if b_st["tok_per_step"] > 0
                else None
            ),
            "tot_tok_vs_base_pct": (
                round((p_st["vtx_tot"] - b_st["vtx_tot"]) / max(1, b_st["vtx_tot"]) * 100.0, 1)
                if b_st["vtx_tot"] > 0
                else None
            ),
            "shorter_than_base_k": shorter,
            "gained_vs_base": gained,
            "lost_vs_base": lost,
            "p_wilcoxon_steps": _wilcoxon_p(
                [t["by_arm"][primary_arm]["steps"] - t["by_arm"][baseline_arm]["steps"] for t in paired]
            ),
            "p_wilcoxon_cap": _wilcoxon_p(
                [t["by_arm"][primary_arm]["cap"] - t["by_arm"][baseline_arm]["cap"] for t in paired]
            ),
            "p_mcnemar_base": _mcnemar_p(gained, lost),
        }
        if reference_arm and reference_arm in arms_stats:
            r_st = arms_stats[reference_arm]
            deltas["cap_vs_ref"] = round(p_st["cap"] - r_st["cap"], 1)
            deltas["pass_vs_ref_k"] = p_st["pass_k"] - r_st["pass_k"]
            if b_st["steps_tot"] > r_st["steps_tot"]:
                deltas["steps_excess_elim_pct"] = round(
                    (b_st["steps_tot"] - p_st["steps_tot"]) / (b_st["steps_tot"] - r_st["steps_tot"]) * 100.0, 1
                )

    return {"id": scope_id, "label": label, "n": n, "arms": arms_stats, "deltas": deltas}


def _compute_slice(
    tasks: list[dict[str, Any]],
    arms_order: list[dict[str, Any]],
    primary_arm: str,
    baseline_arm: str | None,
    key: str,
    label: str,
    filter_spec: dict[str, str],
) -> dict[str, Any]:
    n = len(tasks)
    by_arm: dict[str, Any] = {}
    caps_present: list[float] = []
    for arm_meta in arms_order:
        aid = arm_meta["id"]
        sub = [t["by_arm"][aid] for t in tasks if aid in t["by_arm"]]
        if not sub:
            continue
        cap_pct = round(statistics.mean(x["cap"] for x in sub) * 100.0, 1)
        caps_present.append(cap_pct)
        pk = sum(1 for x in sub if x["passed"])
        st = sum(x["steps"] for x in sub)
        vt = sum(x["vtx_total"] for x in sub)
        vp = sum(x["vtx_prompt"] for x in sub)
        vc = sum(x["vtx_cached"] for x in sub)
        by_arm[aid] = {
            "cap": cap_pct,
            "pass_k": pk,
            "pass_pct": round(pk / len(sub) * 100.0, 1),
            "steps": st,
            "wall_m": round(sum(x["dur"] for x in sub) / 60.0, 1),
            "vtx_tot": vt,
            "tok_per_step": round(vt / max(1, st)) if vt > 0 else 0,
            "cache_hit_pct": round(vc / max(1, vp) * 100.0, 1) if vp > 0 else 0.0,
        }

    p_arm = by_arm.get(primary_arm, {})
    b_arm = by_arm.get(baseline_arm, {}) if baseline_arm else {}
    return {
        "key": key,
        "label": label,
        "n": n,
        "filter": filter_spec,
        "by_arm": by_arm,
        "cap_spread": round(max(caps_present) - min(caps_present), 1) if len(caps_present) >= 2 else 0.0,
        "cap_delta_base": round(p_arm.get("cap", 0.0) - b_arm.get("cap", 0.0), 1) if b_arm else 0.0,
        "steps_delta_base_pct": (
            round((p_arm.get("steps", 0) - b_arm.get("steps", 0)) / max(1, b_arm.get("steps", 1)) * 100.0, 1)
            if b_arm
            else 0.0
        ),
        "wall_delta_base_pct": (
            round((p_arm.get("wall_m", 0.0) - b_arm.get("wall_m", 0.0)) / max(0.1, b_arm.get("wall_m", 0.1)) * 100.0, 1)
            if b_arm
            else 0.0
        ),
        "epochs": sum(t["epochs"] for t in tasks),
        "digests": sum(t["digests"] for t in tasks),
    }


def _select_dynamic_presets(
    tasks: list[dict[str, Any]],
    primary_arm: str,
    baseline_arm: str | None,
) -> list[dict[str, str]]:
    """Select up to 6 informative trajectory presets directly from the evaluated dataset."""
    presets: list[dict[str, str]] = []
    used: set[str] = set()

    def _add(task: dict[str, Any] | None, label: str, tag: str) -> None:
        if task is None or task["id"] in used or len(presets) >= 6:
            return
        used.add(task["id"])
        presets.append({"id": task["id"], "label": label, "tag": tag})

    # Only tasks that have the arms a preset compares (a missing arm never raises).
    tasks = [t for t in tasks if primary_arm in t["by_arm"]]
    base_tasks = [t for t in tasks if baseline_arm and baseline_arm in t["by_arm"]]

    # 1. File/multimodal compaction showcase
    file_comp = [t for t in tasks if t["cat"] == "file" and t["epochs"] >= 1 and t["by_arm"][primary_arm]["cap"] >= 0.85]
    if file_comp:
        best = max(file_comp, key=lambda t: ( -t["steps_delta_base_pct"], t["epochs"], t["by_arm"][primary_arm]["cap"]))
        pr = best["by_arm"][primary_arm]
        _add(best, "Multimodal Pinning & Compaction", f"{best['steps_delta_base_pct']:+.0f}% steps · {pr['checks_str']}")

    # 2. Largest baseline monologue / token-ratio outlier
    if baseline_arm:
        monologue = sorted(
            base_tasks,
            key=lambda t: (t.get("ratio_tok_base", 0.0), t["by_arm"].get(baseline_arm, {}).get("msg_chars", 0)),
            reverse=True,
        )
        if monologue and (monologue[0].get("ratio_tok_base", 0.0) >= 5.0 or monologue[0]["by_arm"].get(baseline_arm, {}).get("msg_chars", 0) > 10000):
            m0 = monologue[0]
            mc_b = m0["by_arm"][baseline_arm].get("msg_chars", 0)
            mc_p = m0["by_arm"][primary_arm].get("msg_chars", 0)
            tag = f"{mc_b // 1000}k -> {mc_p} msg chars" if mc_b >= 1000 else f"{m0.get('ratio_tok_base', 0):.1f}x outlier"
            _add(m0, "Monologue Outlier Suppressed", tag)

    # 3. CLI / Digests showcase that passed 100% with big step cut
    cli_wins = [t for t in tasks if t["cat"] == "cli" and t["by_arm"][primary_arm]["passed"] and t["steps_delta_base"] < -5]
    if cli_wins:
        c0 = min(cli_wins, key=lambda t: t["steps_delta_base_pct"])
        b_s = c0["by_arm"].get(baseline_arm or primary_arm, c0["by_arm"][primary_arm])["steps"]
        p_s = c0["by_arm"][primary_arm]["steps"]
        _add(c0, "CLI Fast-Path & Birth Gate", f"{b_s} -> {p_s} steps · {c0['by_arm'][primary_arm]['checks_str']}")

    # 4. Browser UI step reduction showcase
    br_wins = [t for t in tasks if t["cat"] == "browser" and t["by_arm"][primary_arm]["passed"]]
    if br_wins:
        b0 = min(br_wins, key=lambda t: t["steps_delta_base_pct"])
        if baseline_arm and baseline_arm in b0["by_arm"] and b0["steps_delta_base"] < 0:
            _add(
                b0,
                "Browser UI Control Strip",
                f"{b0['by_arm'][baseline_arm]['steps']} -> {b0['by_arm'][primary_arm]['steps']} steps · 100%",
            )
        else:
            _add(b0, "Browser UI Workflow", f"{b0['by_arm'][primary_arm]['steps']} steps · 100%")

    # 5. Bucket-B / Baseline-failed rescue (0 -> 100%)
    if baseline_arm:
        rescues = [
            t
            for t in base_tasks
            if t["by_arm"][primary_arm]["passed"]
            and not t["by_arm"][baseline_arm]["passed"]
            and t["cap_delta_base"] >= 30.0
        ]
        rescues.sort(key=lambda t: t["cap_delta_base"], reverse=True)
        if rescues:
            r0 = rescues[0]
            _add(
                r0,
                "Silent-Exit Rescue",
                f"{r0['by_arm'][baseline_arm]['cap']*100:.0f}% -> {r0['by_arm'][primary_arm]['cap']*100:.0f}% cap",
            )
        if len(rescues) >= 2:
            r1 = rescues[1]
            _add(
                r1,
                "Early-Exit Recovery",
                f"{r1['by_arm'][baseline_arm]['cap']*100:.0f}% -> {r1['by_arm'][primary_arm]['cap']*100:.0f}% cap",
            )

    # Fill up to 4 if small synthetic run
    for t in tasks:
        if len(presets) >= 4:
            break
        _add(t, t["id"][:28], t["by_arm"][primary_arm]["checks_str"])
    return presets


def build_html_report_payload(
    results: list[RunMetrics],
    *,
    workdir: Path | None = None,
    dataset_root: Path | None = None,
    ctx_state_dir: Path | None = None,
    baseline_json: Path | None = None,
    baseline_htmls: list[tuple[str, Path]] | None = None,
    ablation_json: Path | None = None,
    title: str | None = None,
) -> dict[str, Any]:
    """Assemble the complete multi-arm benchmark + trajectory payload dictionary."""
    resolved_workdir = Path(workdir).resolve() if workdir is not None else None
    if not results and resolved_workdir is not None:
        results = load_run_results(resolved_workdir)

    meta_dirs, raw_jsons, latest_mtime = (
        _discover_meta_dirs(resolved_workdir) if resolved_workdir is not None else ({}, {}, 0.0)
    )
    summary_meta = _load_optional_summary_meta(resolved_workdir)

    resolved_ctx_dir = Path(ctx_state_dir).resolve() if ctx_state_dir is not None else (
        (resolved_workdir / "ctx_state") if resolved_workdir is not None else None
    )
    ctx_by_trial = _load_ctx_manifests(resolved_ctx_dir)

    by_arm_runs: dict[str, list[RunMetrics]] = {}
    for r in results:
        by_arm_runs.setdefault(r.arm, []).append(r)

    live_arms = sorted(by_arm_runs.keys(), key=lambda a: (0 if a == "sj" else (1 if a == "raw" else 2), a))
    collapsed_live: dict[str, dict[str, RunMetrics]] = {}
    for a in live_arms:
        collapsed_live[a], _ = _collapse_arm_runs(by_arm_runs[a])

    primary_arm = "sj" if "sj" in collapsed_live else (live_arms[0] if live_arms else "sj")

    # Parse optional baseline_json (supports both multi-column 37/38 combined_analysis.json and single-arm JSON).
    comb_rows_by_task: dict[str, dict[str, Any]] = {}
    single_ext_baseline: dict[str, dict[str, Any]] = {}
    if baseline_json is not None and Path(baseline_json).is_file():
        raw_bj = json.loads(Path(baseline_json).read_text(encoding="utf-8"))
        bj_list = raw_bj if isinstance(raw_bj, list) else (raw_bj.get("runs") or raw_bj.get("results") or [])
        if isinstance(bj_list, list) and bj_list and isinstance(bj_list[0], dict) and (
            "steps38" in bj_list[0] or "steps37" in bj_list[0]
        ):
            for row in bj_list:
                tid = str(row.get("task_id") or row.get("task") or "").strip()
                if tid:
                    comb_rows_by_task[tid] = row
        else:
            single_ext_baseline = load_baseline_records(Path(baseline_json))

    # Also check if individual result.json files carry embedded steps38/steps37 baseline columns.
    for (tid, _), rj in raw_jsons.items():
        if ("steps38" in rj or "steps37" in rj) and tid not in comb_rows_by_task:
            comb_rows_by_task[tid] = rj

    # Parse optional Accio HTML baseline reports.
    parsed_html_by_label: dict[str, dict[str, dict[str, Any]]] = {}
    for lbl, hpath in baseline_htmls or []:
        parsed = parse_accio_baseline_html(hpath)
        if parsed:
            parsed_html_by_label[lbl] = parsed

    # Pre-check ablation step ratios if ablation_json exists (for calibrating historical HTML baselines).
    raw_ablation_ratios: dict[str, float] = {}
    if ablation_json is not None and Path(ablation_json).is_file():
        try:
            ab_raw = json.loads(Path(ablation_json).read_text(encoding="utf-8"))
            if isinstance(ab_raw, list):
                for ak in ("37base", "38base"):
                    sub = [x for x in ab_raw if isinstance(x, dict) and x.get("arm_key") == ak]
                    st = sum(int(x.get("steps") or 0) for x in sub)
                    tt = sum(int(x.get("total_tokens") or 0) for x in sub)
                    if st > 0 and tt > 0:
                        raw_ablation_ratios[ak] = tt / st
        except Exception:
            pass

    # Determine model & harness version metadata.
    sample_metric = next(iter(collapsed_live.get(primary_arm, {}).values()), None)
    primary_model = sample_metric.model_id if sample_metric else str(summary_meta.get("model") or "model")
    ctx_ver = _detect_ctx_version()
    if summary_meta.get("harness"):
        m_v = re.search(r"\b0\.\d+\.\d+\b", str(summary_meta["harness"]))
        if m_v:
            ctx_ver = f"v{m_v.group(0)}"
    sj_suffix = f"Straitjacket {ctx_ver}".strip() if ctx_ver else "Straitjacket"

    # Build ordered arms metadata.
    arms_order: list[dict[str, Any]] = []

    def _register_arm(aid: str, name: str, short: str, role: str, *, is_calibrated: bool = False) -> None:
        pal = _ARM_PALETTE[len(arms_order) % len(_ARM_PALETTE)]
        arms_order.append(
            {
                "id": aid,
                "name": name,
                "short": short,
                "role": role,
                "color": pal["color"],
                "soft_bg": pal["soft_bg"],
                "badge_fg": pal["badge_fg"],
                "is_calibrated_tok": is_calibrated,
            }
        )

    for a in live_arms:
        m_any = next(iter(collapsed_live[a].values()), None)
        mid = m_any.model_id if m_any else primary_model
        if a == "sj":
            _register_arm("sj", f"{mid} + {sj_suffix}", f"{_short_model_label(mid)} + SJ", "primary")
        elif a == "raw":
            _register_arm("raw", f"{mid} (raw)", f"{_short_model_label(mid)} (raw)", "baseline")
        else:
            _register_arm(a, f"{mid} ({a})", a, "arm")

    has_b38_ext = bool(comb_rows_by_task) and "raw" not in collapsed_live
    has_b37_ext = any("steps37" in r for r in comb_rows_by_task.values())
    b38_html_key = next((k for k in parsed_html_by_label if "3.8" in k or "38" in k or k == "report_1"), None)
    b37_html_key = next((k for k in parsed_html_by_label if "3.7" in k or "37" in k or k == "report"), None)

    if has_b38_ext:
        b38_name = b38_html_key if (b38_html_key and b38_html_key != "report_1") else primary_model
        _register_arm(
            "b38",
            b38_name,
            _short_model_label(b38_name),
            "baseline",
            is_calibrated=("38base" in raw_ablation_ratios),
        )
    elif single_ext_baseline and "raw" not in collapsed_live:
        _register_arm("ext_base", "Baseline (JSON)", "Baseline", "baseline")

    if has_b37_ext:
        b37_name = b37_html_key if (b37_html_key and b37_html_key != "report") else "gemini-3.7-flash"
        _register_arm(
            "b37",
            b37_name,
            _short_model_label(b37_name),
            "reference",
            is_calibrated=("37base" in raw_ablation_ratios),
        )

    # Any extra --baseline-html specs that weren't matched to b38/b37:
    for lbl in parsed_html_by_label:
        if lbl in (b38_html_key, b37_html_key) and (has_b38_ext or has_b37_ext):
            continue
        if all(a["id"] != lbl for a in arms_order):
            _register_arm(lbl, lbl, _short_model_label(lbl), "reference")

    baseline_arm: str | None = None
    reference_arm: str | None = None
    for a in arms_order:
        if a["id"] == primary_arm:
            continue
        if baseline_arm is None:
            baseline_arm = a["id"]
        elif reference_arm is None:
            reference_arm = a["id"]

    # Tasks every live arm has a scorable result for. A task one live arm lacks (ungraded,
    # API/setup error, not run, superseded) cannot be compared; it is left out of every
    # view and listed as unpaired in the header instead.
    primary_map = collapsed_live.get(primary_arm, {})
    live_task_sets = [set(m) for m in collapsed_live.values()]
    all_live_ids = set().union(*live_task_sets) if live_task_sets else set()
    task_ids = sorted(set.intersection(*live_task_sets)) if live_task_sets else []
    unpaired_tasks = [
        {
            "id": tid,
            "present": [a for a in live_arms if tid in collapsed_live[a]],
            "missing": [a for a in live_arms if tid not in collapsed_live[a]],
        }
        for tid in sorted(all_live_ids - set(task_ids))
    ]

    tasks_list: list[dict[str, Any]] = []
    traces_map: dict[str, dict[str, Any]] = {}

    for tid in task_ids:
        pm = primary_map[tid]
        comb = comb_rows_by_task.get(tid, {})
        raw_p = raw_jsons.get((tid, primary_arm), {})

        cat = pm.category if pm.category and pm.category != "unknown" else str(comb.get("category") or "unknown")
        bucket = pm.bucket if pm.bucket and pm.bucket != "unknown" else str(
            comb.get("bucket")
            or (
                "better"
                if ("diff_tok" in comb and float(comb.get("diff_tok") or 0.0) <= 0)
                else ("worse" if "diff_tok" in comb else "unknown")
            )
        )
        trel = pm.task_rel or tid
        prompt_md = _read_task_prompt(dataset_root, trel, tid)

        by_arm_task: dict[str, dict[str, Any]] = {}
        trace_arms: dict[str, dict[str, Any]] = {}

        # 1. Populate all live arms for this task
        for la in live_arms:
            lm = collapsed_live[la].get(tid)
            if lm is None:
                continue
            mdir = meta_dirs.get((tid, la))
            checks = _read_reward_checks(mdir)
            steps_trace = _read_live_trajectory(mdir, ctx_by_trial.get((tid, la), []))
            chk_str = f"{lm.checks_passed}/{lm.checks_total}" if lm.checks_total > 0 else (
                "PASS" if lm.passed else "FAIL"
            )
            by_arm_task[la] = {
                "passed": bool(lm.passed),
                "cap": round(float(lm.capacity_score), 4),
                "checks_str": chk_str,
                "checks_passed": int(lm.checks_passed),
                "checks_total": int(lm.checks_total),
                "steps": int(lm.steps),
                "dur": round(float(lm.wall_clock_s), 1),
                "vtx_prompt": int(lm.prompt_tokens),
                "vtx_cached": int(lm.cached_tokens),
                "vtx_thought": int(lm.thought_tokens),
                "vtx_out": int(lm.output_tokens),
                "vtx_total": int(lm.total_tokens),
                "tok_per_step": round(int(lm.total_tokens) / max(1, int(lm.steps))),
                "cache_pct": round(int(lm.cached_tokens) / max(1, int(lm.prompt_tokens)) * 100.0, 1)
                if lm.prompt_tokens > 0
                else 0.0,
                "rep_tok": int(lm.gen_tokens),
                "msg_chars": int(lm.message_chars),
            }
            trace_arms[la] = {"kind": "live", "checks": checks, "final": "", "steps": steps_trace}

        # 2. Populate b38 / b37 / single_ext_baseline / extra baseline HTMLs
        if has_b38_ext and comb:
            h38 = parsed_html_by_label.get(b38_html_key or "", {}).get(tid, {})
            s38 = int(comb.get("steps38") or len(h38.get("steps") or []))
            cap38 = float(comb.get("cap38", comb.get("reward38", h38.get("cap", 0.0))) or 0.0)
            p38 = (
                bool(comb["passed38"])
                if "passed38" in comb
                else (str(comb.get("status38", "")).lower() == "success")
            )
            ratio38 = raw_ablation_ratios.get("38base", 0.0)
            est38 = round(s38 * ratio38) if ratio38 > 0 else 0
            msg38 = int(h38.get("msg_chars") or round(float(comb.get("msg_tok38") or 0.0) * 4))
            by_arm_task["b38"] = {
                "passed": p38,
                "cap": round(cap38, 4),
                "checks_str": h38.get("chk_str", "PASS" if p38 else f"{cap38*100:.0f}%"),
                "checks_passed": int(h38.get("checks_passed") or 0),
                "checks_total": int(h38.get("checks_total") or 0),
                "steps": s38,
                "dur": round(float(comb.get("dur38") or 0.0), 1),
                "vtx_prompt": 0,
                "vtx_cached": 0,
                "vtx_thought": round(float(comb.get("reas_tok38") or 0.0)),
                "vtx_out": round(float(comb.get("msg_tok38") or 0.0)),
                "vtx_total": est38,
                "tok_per_step": round(ratio38) if ratio38 > 0 else 0,
                "cache_pct": 0.0,
                "rep_tok": round(float(comb.get("tok38") or 0.0)),
                "msg_chars": msg38,
            }
            trace_arms["b38"] = {
                "kind": "html",
                "checks": h38.get("checks", []),
                "final": h38.get("final", ""),
                "steps": h38.get("steps", []),
            }
        elif "ext_base" in {a["id"] for a in arms_order} and tid in single_ext_baseline:
            eb = single_ext_baseline[tid]
            by_arm_task["ext_base"] = {
                "passed": bool(eb["passed"]),
                "cap": round(float(eb["capacity_score"]), 4),
                "checks_str": "PASS" if eb["passed"] else f"{eb['capacity_score']*100:.0f}%",
                "checks_passed": 0,
                "checks_total": 0,
                "steps": int(eb["steps"]),
                "dur": round(float(eb["wall_clock_s"]), 1),
                "vtx_prompt": int(eb["prompt_tokens"]),
                "vtx_cached": int(eb["cached_tokens"]),
                "vtx_thought": int(eb["thought_tokens"]),
                "vtx_out": int(eb["output_tokens"]),
                "vtx_total": int(eb["total_tokens"]),
                "tok_per_step": round(int(eb["total_tokens"]) / max(1, int(eb["steps"]))),
                "cache_pct": round(int(eb["cached_tokens"]) / max(1, int(eb["prompt_tokens"])) * 100.0, 1)
                if eb["prompt_tokens"] > 0
                else 0.0,
                "rep_tok": int(eb["gen_tokens"]),
                "msg_chars": 0,
            }
            trace_arms["ext_base"] = {"kind": "summary", "checks": [], "final": "", "steps": []}

        if has_b37_ext and comb:
            h37 = parsed_html_by_label.get(b37_html_key or "", {}).get(tid, {})
            s37 = int(comb.get("steps37") or len(h37.get("steps") or []))
            cap37 = float(comb.get("cap37", comb.get("reward37", h37.get("cap", 0.0))) or 0.0)
            p37 = (
                bool(comb["passed37"])
                if "passed37" in comb
                else (str(comb.get("status37", "")).lower() == "success")
            )
            ratio37 = raw_ablation_ratios.get("37base", 0.0)
            est37 = round(s37 * ratio37) if ratio37 > 0 else 0
            msg37 = int(h37.get("msg_chars") or round(float(comb.get("msg_tok37") or 0.0) * 4))
            by_arm_task["b37"] = {
                "passed": p37,
                "cap": round(cap37, 4),
                "checks_str": h37.get("chk_str", "PASS" if p37 else f"{cap37*100:.0f}%"),
                "checks_passed": int(h37.get("checks_passed") or 0),
                "checks_total": int(h37.get("checks_total") or 0),
                "steps": s37,
                "dur": round(float(comb.get("dur37") or 0.0), 1),
                "vtx_prompt": 0,
                "vtx_cached": 0,
                "vtx_thought": round(float(comb.get("reas_tok37") or 0.0)),
                "vtx_out": round(float(comb.get("msg_tok37") or 0.0)),
                "vtx_total": est37,
                "tok_per_step": round(ratio37) if ratio37 > 0 else 0,
                "cache_pct": 0.0,
                "rep_tok": round(float(comb.get("tok37") or 0.0)),
                "msg_chars": msg37,
            }
            trace_arms["b37"] = {
                "kind": "html",
                "checks": h37.get("checks", []),
                "final": h37.get("final", ""),
                "steps": h37.get("steps", []),
            }

        for lbl, hmap in parsed_html_by_label.items():
            if lbl in by_arm_task or (lbl in (b38_html_key, b37_html_key) and (has_b38_ext or has_b37_ext)):
                continue
            ht = hmap.get(tid)
            if ht:
                by_arm_task[lbl] = {
                    "passed": bool(ht["passed"]),
                    "cap": round(float(ht["cap"]), 4),
                    "checks_str": ht["chk_str"],
                    "checks_passed": int(ht["checks_passed"]),
                    "checks_total": int(ht["checks_total"]),
                    "steps": len(ht["steps"]),
                    "dur": 0.0,
                    "vtx_prompt": 0,
                    "vtx_cached": 0,
                    "vtx_thought": 0,
                    "vtx_out": 0,
                    "vtx_total": 0,
                    "tok_per_step": 0,
                    "cache_pct": 0.0,
                    "rep_tok": 0,
                    "msg_chars": int(ht["msg_chars"]),
                }
                trace_arms[lbl] = {
                    "kind": "html",
                    "checks": ht["checks"],
                    "final": ht["final"],
                    "steps": ht["steps"],
                }

        failed_preview = list(raw_p.get("failed_checks") or [])[:4]
        if not failed_preview and trace_arms.get(primary_arm, {}).get("checks"):
            failed_preview = [
                f"{c['id']}: {c['r']}"
                for c in trace_arms[primary_arm]["checks"]
                if not c["p"]
            ][:4]

        fail_cat = _classify_failure(
            passed=pm.passed,
            termination=pm.termination,
            capacity_score=pm.capacity_score,
            category=cat,
            failed_checks=failed_preview,
        )

        p_rec = by_arm_task[primary_arm]
        b_rec = by_arm_task.get(baseline_arm) if baseline_arm else None
        cap_delta_base = round((p_rec["cap"] - b_rec["cap"]) * 100.0, 1) if b_rec else 0.0
        steps_delta_base = (p_rec["steps"] - b_rec["steps"]) if b_rec else 0
        steps_delta_base_pct = (
            round((p_rec["steps"] - b_rec["steps"]) / max(1, b_rec["steps"]) * 100.0, 1) if b_rec else 0.0
        )

        # Bytes kept out: prefer explicit RunMetrics.bytes_kept_out, else compute from correlated manifests >16KB
        bytes_out = int(pm.bytes_kept_out)
        if bytes_out == 0 and trace_arms.get(primary_arm, {}).get("steps"):
            for st_item in trace_arms[primary_arm]["steps"]:
                for cl in st_item.get("c") or []:
                    if cl.get("dig") and int(cl.get("rb") or 0) > int(cl.get("rc") or 0):
                        bytes_out += int(cl["rb"]) - int(cl["rc"])

        tasks_list.append(
            {
                "id": tid,
                "rel": trel,
                "cat": cat,
                "bucket": bucket,
                "wave": pm.wave or str(raw_p.get("wave") or ""),
                "term": pm.termination,
                "fail_cat": fail_cat,
                "failed_preview": failed_preview,
                "max_win": int(pm.max_step_prompt_tokens),
                "epochs": int(pm.compaction_epochs),
                "thoughts_comp": int(pm.thoughts_compacted),
                "notes_emit": int(pm.state_notes_emitted),
                "notes_pres": int(pm.state_notes_preserved),
                "muts_pres": int(pm.mutations_preserved or raw_p.get("mutations_preserved") or 0),
                "digests": int(pm.digests_delivered),
                "bytes_out": bytes_out,
                "ui_strip": int(pm.ui_controls_stripped_chars or raw_p.get("ui_controls_stripped_chars") or 0),
                "ratio_tok_base": (
                    float(comb.get("ratio_tok") or 0.0)
                    if float(comb.get("tok37", 1.0) or 0.0) > 0
                    else 0.0
                ),
                "cap_delta_base": cap_delta_base,
                "steps_delta_base": steps_delta_base,
                "steps_delta_base_pct": steps_delta_base_pct,
                "by_arm": by_arm_task,
            }
        )
        traces_map[tid] = {"prompt": prompt_md, "arms": trace_arms}

    # Build scopes (All N tasks + optional outlier-excluded scope if a >=15x baseline outlier exists).
    scopes: list[dict[str, Any]] = []
    if tasks_list:
        scopes.append(
            _compute_scope(
                tasks_list,
                arms_order,
                primary_arm,
                baseline_arm,
                reference_arm,
                "all",
                f"All {len(tasks_list)} tasks",
            )
        )
        outliers = [t for t in tasks_list if t.get("ratio_tok_base", 0.0) >= 15.0]
        if len(outliers) == 1 and len(tasks_list) > 10:
            out_tid = outliers[0]["id"]
            short_out = out_tid.split("-")[1].capitalize() if "-" in out_tid else out_tid[:12]
            scopes.append(
                _compute_scope(
                    [t for t in tasks_list if t["id"] != out_tid],
                    arms_order,
                    primary_arm,
                    baseline_arm,
                    reference_arm,
                    "ex_outlier",
                    f"{len(tasks_list) - 1} tasks (excl. {short_out} outlier)",
                )
            )

    # Build stratified slices.
    slices: list[dict[str, Any]] = []
    if tasks_list:
        slices.append(
            _compute_slice(
                tasks_list, arms_order, primary_arm, baseline_arm, "all", f"All {len(tasks_list)} Benchmark Tasks", {"cat": "all", "outcome": "all"}
            )
        )
        if len(scopes) > 1:
            out_tid = max(tasks_list, key=lambda t: t.get("ratio_tok_base", 0.0))["id"]
            slices.append(
                _compute_slice(
                    [t for t in tasks_list if t["id"] != out_tid],
                    arms_order,
                    primary_arm,
                    baseline_arm,
                    "ex_outlier",
                    f"{len(tasks_list) - 1} Tasks (Excl. Outlier)",
                    {"cat": "all", "outcome": "all"},
                )
            )
        b_tasks = [t for t in tasks_list if t["bucket"] == "better"]
        w_tasks = [t for t in tasks_list if t["bucket"] == "worse"]
        if b_tasks:
            slices.append(
                _compute_slice(
                    b_tasks,
                    arms_order,
                    primary_arm,
                    baseline_arm,
                    "bucket_b",
                    f"Bucket B: Silent / Early-Exit ({len(b_tasks)})",
                    {"cat": "all", "outcome": "bucket_b"},
                )
            )
        if w_tasks:
            slices.append(
                _compute_slice(
                    w_tasks,
                    arms_order,
                    primary_arm,
                    baseline_arm,
                    "bucket_w",
                    f"Bucket W: Token Blowout ({len(w_tasks)})",
                    {"cat": "all", "outcome": "bucket_w"},
                )
            )
        cat_labels = {
            "cli": "CLI Workflows",
            "browser": "Browser UI Automation",
            "file": "File & Multimodal Audits",
            "api": "API Consolidation",
        }
        for c in ("cli", "browser", "file", "api"):
            c_sub = [t for t in tasks_list if t["cat"] == c]
            if c_sub:
                slices.append(
                    _compute_slice(
                        c_sub,
                        arms_order,
                        primary_arm,
                        baseline_arm,
                        c,
                        f"{cat_labels[c]} ({len(c_sub)})",
                        {"cat": c, "outcome": "all"},
                    )
                )

    # Failure distribution across unsolved primary_arm tasks.
    unsolved_n = sum(1 for t in tasks_list if not t["by_arm"][primary_arm]["passed"])
    failures_summary = []
    for fm in _FAILURE_META:
        cnt = sum(1 for t in tasks_list if t["fail_cat"] == fm["key"])
        failures_summary.append({**fm, "count": cnt, "pct": round(cnt / max(1, unsolved_n) * 100.0) if unsolved_n else 0})

    # Optional controlled ablation.
    tasks_by_rel = {t["rel"]: t for t in tasks_list}
    ablation_payload, _ = _parse_ablation(
        ablation_json,
        tasks_by_rel,
        primary_arm,
        f"{_short_model_label(primary_model)} + {sj_suffix}",
    )

    # Lever aggregates (falling back to summary_meta["levers"] for any field aggregated only at suite level).
    meta_lev = summary_meta.get("levers") if isinstance(summary_meta.get("levers"), dict) else {}
    sum_bytes_out = sum(t["bytes_out"] for t in tasks_list) or int(meta_lev.get("bytes_kept_out") or 0)
    sum_ui_strip = sum(t["ui_strip"] for t in tasks_list) or int(meta_lev.get("ui_controls_stripped_chars") or 0)
    sum_muts = sum(t["muts_pres"] for t in tasks_list) or int(meta_lev.get("mutations_preserved") or 0)
    msg_p = sum(t["by_arm"][primary_arm]["msg_chars"] for t in tasks_list)
    msg_b = (
        sum(t["by_arm"][baseline_arm]["msg_chars"] for t in tasks_list if baseline_arm in t["by_arm"])
        if baseline_arm
        else 0
    )
    if msg_b == 0 and isinstance(summary_meta.get("message_chars"), dict):
        msg_b = int(summary_meta["message_chars"].get("38_report") or 0)

    p_rows = list(collapsed_live.get(primary_arm, {}).values())
    levers_summary = {
        "compaction_epochs": sum(t["epochs"] for t in tasks_list),
        "thoughts_compacted": sum(t["thoughts_comp"] for t in tasks_list),
        "digests_delivered": sum(t["digests"] for t in tasks_list),
        "bytes_kept_out": sum_bytes_out,
        "state_notes_emitted": sum(t["notes_emit"] for t in tasks_list),
        "state_notes_preserved": sum(t["notes_pres"] for t in tasks_list),
        "mutations_preserved": sum_muts,
        "ui_controls_stripped_chars": sum_ui_strip,
        "cli_lsp_enrichments": sum(r.cli_lsp_enrichments for r in p_rows) or int(meta_lev.get("cli_lsp_enrichments") or 0),
        "dup_blocked": sum(r.dup_blocked for r in p_rows) or int(meta_lev.get("dup_blocked") or 0),
        "file_dedup_hits": sum(r.file_dedup_hits for r in p_rows),
        "emission_gate_hits": sum(r.emission_gate_hits for r in p_rows),
        "msg_chars_primary": msg_p,
        "msg_chars_baseline": msg_b,
    }

    run_date = (
        datetime.fromtimestamp(latest_mtime, tz=timezone.utc).strftime("%Y-%m-%d")
        if latest_mtime > 0
        else datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    )
    cats_count: dict[str, int] = {}
    for t in tasks_list:
        cats_count[t["cat"]] = cats_count.get(t["cat"], 0) + 1

    ablation_runs = sum(int(a.get("n") or 0) for a in ablation_payload["arms"] if a["key"] != "suite_overlap")
    judge_label = str(summary_meta.get("judge_model") or "official grader")

    return {
        "meta": {
            "title": title or "Straitjacket Eval",
            "suite": "CommerceAgentBench",
            "harness": f"Straitjacket ctx-harness {ctx_ver}".strip(),
            "judge": judge_label,
            "date": run_date,
            "total_tasks": len(tasks_list),
            "total_runs": sum(len(t["by_arm"]) for t in tasks_list),
            "ablation_runs": ablation_runs,
            "categories": cats_count,
            "primary_arm": primary_arm,
            "baseline_arm": baseline_arm,
            "reference_arm": reference_arm,
            "levers": levers_summary,
            # Ungraded trials (grader failed): excluded from every solve/score figure.
            "grading_errors": {a: count_grading_errors(by_arm_runs[a]) for a in live_arms},
            "grading_errors_total": sum(count_grading_errors(by_arm_runs[a]) for a in live_arms),
            "unpaired_tasks": len(unpaired_tasks),
            "unpaired": unpaired_tasks,
        },
        "arms_order": arms_order,
        "scopes": scopes,
        "slices": slices,
        "failures": failures_summary,
        "unsolved_total": unsolved_n,
        "ablation": ablation_payload,
        "presets": _select_dynamic_presets(tasks_list, primary_arm, baseline_arm),
        "tasks": tasks_list,
        "traces": traces_map,
    }


def render_html_report(
    results: list[RunMetrics],
    *,
    workdir: Path | None = None,
    dataset_root: Path | None = None,
    ctx_state_dir: Path | None = None,
    baseline_json: Path | None = None,
    baseline_htmls: list[tuple[str, Path]] | None = None,
    ablation_json: Path | None = None,
    title: str | None = None,
) -> str:
    """Build and render a standalone, single-file interactive HTML benchmark report."""
    payload = build_html_report_payload(
        results,
        workdir=workdir,
        dataset_root=dataset_root,
        ctx_state_dir=ctx_state_dir,
        baseline_json=baseline_json,
        baseline_htmls=baseline_htmls,
        ablation_json=ablation_json,
        title=title,
    )
    raw_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    gz_b64 = base64.b64encode(gzip.compress(raw_json, compresslevel=9)).decode("ascii")
    doc_title = f"{payload['meta']['title']} · {payload['meta']['suite']} ({payload['meta']['total_tasks']} tasks)"
    return render_html_document(doc_title=doc_title, payload_gz_b64=gz_b64)
