"""Harness collaboration: route a task's phases across installed harnesses by
model cost, handing off *addressed evidence* — not bytes — through the shared
CAS store.

This is the cross-*harness* generalization of the shipped ctx-explorer fork
(ROADMAP M-A): a cheap harness explores and deposits evidence handles into the
store; an expensive harness synthesizes from those handles, seeing only a
bounded checkpoint, never the raw exploration bytes. The economic lever is the
one already in the repo — :mod:`ctx.pricing` prices each harness by its model,
so the router can send lean work to the cheapest capable harness and reserve
the premium harness for the phase that needs it.

Two halves, deliberately separated so the valuable part is pure and testable:

* **Plan & price** (:func:`plan_orchestration`, :func:`render_plan`) — pure,
  deterministic. Given the detected harnesses it assigns each phase to a host
  by cost, estimates the spend per phase, and prints the priced plan. No CLI is
  launched. This is what the "priced plan, then run" posture shows first.
* **Run** (:func:`run_orchestration`) — executes each phase through its
  assigned harness in print mode inside the harnessed workspace, captures the
  phase output into the store, and threads a ``checkpoint:`` handoff to the
  next phase. Fully fail-open: a missing or failing harness records a skipped
  outcome and the run continues; if nothing is runnable it degrades to the
  printed plan.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ctx.hosts import DetectedHost, installed_harnessable


# ---------------------------------------------------------------------------
# The default collaboration pipeline. Roles map to cost tiers: "lean" -> the
# cheapest installed harness, "capable" -> the most expensive. This mirrors
# ctx.engagement's lean-vs-capable split exactly, one level up (whole harnesses
# instead of affordance surfaces). Overridable per repo via ctx.toml.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Phase:
    name: str
    goal: str
    role: str  # "lean" | "capable"
    est_input_tokens: int
    est_output_tokens: int


@dataclass(frozen=True)
class Assignment:
    phase: Phase
    host: DetectedHost
    est_cost_usd: float


@dataclass(frozen=True)
class OrchestrationPlan:
    task: str
    ladder: tuple[DetectedHost, ...]        # cheapest -> premium (installed)
    assignments: tuple[Assignment, ...]

    @property
    def est_total_usd(self) -> float:
        return sum(a.est_cost_usd for a in self.assignments)

    @property
    def est_single_premium_usd(self) -> float:
        """What the same token budget would cost run entirely on the premium
        harness — the baseline the collaboration is measured against."""
        if not self.ladder:
            return 0.0
        premium = self.ladder[-1]
        in_toks = sum(a.phase.est_input_tokens for a in self.assignments)
        out_toks = sum(a.phase.est_output_tokens for a in self.assignments)
        return premium.price.cost_usd(input_tokens=in_toks, output_tokens=out_toks)


def default_phases(cfg) -> tuple[Phase, ...]:
    """The canonical explore -> implement -> review pipeline, with per-phase
    token estimates from the [orchestrate] config block."""
    return (
        Phase(
            "explore",
            "gather the evidence the task needs: search, map, read — deposit "
            "ctx handles; do not change code",
            "lean",
            cfg.explore_input_tokens,
            cfg.explore_output_tokens,
        ),
        Phase(
            "implement",
            "make the change from the addressed evidence in the prior checkpoint",
            "capable",
            cfg.implement_input_tokens,
            cfg.implement_output_tokens,
        ),
        Phase(
            "review",
            "verify the change: run the acceptance check, inspect the diff",
            "lean",
            cfg.review_input_tokens,
            cfg.review_output_tokens,
        ),
    )


def cost_ladder(hosts: list[DetectedHost]) -> list[DetectedHost]:
    """Installed harnesses ranked cheapest -> premium. Output-token price is the
    dominant term for agent work; ties break on input price then name so the
    ladder is fully deterministic."""
    return sorted(hosts, key=lambda d: (d.price.output, d.price.input, d.name))


def _pick_host(role: str, ladder: list[DetectedHost], cfg) -> DetectedHost:
    """Cheapest for lean, premium for capable — unless a config pin names a
    host that is actually in the ladder."""
    pin = cfg.lean_host if role == "lean" else cfg.capable_host
    if pin:
        for d in ladder:
            if d.name == pin:
                return d
    return ladder[0] if role == "lean" else ladder[-1]


def plan_orchestration(
    task: str,
    hosts: list[DetectedHost],
    cfg,
    *,
    phases: tuple[Phase, ...] | None = None,
) -> OrchestrationPlan:
    """Assign each phase to a harness by cost and price the plan. Pure and
    deterministic. Raises ValueError only when no harnessable CLI is installed —
    the one condition the caller must handle."""
    ladder = cost_ladder(hosts)
    if not ladder:
        raise ValueError("no installed harnessable CLI to orchestrate across")
    phs = phases if phases is not None else default_phases(cfg)
    assignments = []
    for ph in phs:
        host = _pick_host(ph.role, ladder, cfg)
        cost = host.price.cost_usd(
            input_tokens=ph.est_input_tokens, output_tokens=ph.est_output_tokens
        )
        assignments.append(Assignment(phase=ph, host=host, est_cost_usd=cost))
    return OrchestrationPlan(task=task, ladder=tuple(ladder), assignments=tuple(assignments))


def _usd(x: float) -> str:
    return f"${x:.2f}" if x >= 0.005 else f"${x:.4f}"


def render_plan(plan: OrchestrationPlan) -> str:
    """The priced plan, in the repo's priced-context idiom: show the routing and
    the dollars before spending them."""
    lines = [f'[ctx orchestrate] task: "{plan.task}"']
    ladder_str = " · ".join(
        f"{d.name} {_usd(d.price.output)}/Mout" for d in plan.ladder
    )
    lines.append(f"harness cost ladder (installed): {ladder_str}   ← cheapest→premium")
    lines.append(f"routing ({len(plan.assignments)} phases):")
    for i, a in enumerate(plan.assignments, 1):
        lines.append(
            f"  {i}. {a.phase.name:10} → {a.host.name:11} ({a.phase.role:8}) "
            f"est ~{_usd(a.est_cost_usd)}   "
            f"{a.phase.est_input_tokens // 1000}k in / "
            f"{a.phase.est_output_tokens / 1000:g}k out  [{a.host.model}]"
        )
    baseline = plan.est_single_premium_usd
    saved = baseline - plan.est_total_usd
    lines.append(
        f"estimated total: ~{_usd(plan.est_total_usd)}  "
        f"(single-premium baseline ~{_usd(baseline)}"
        + (f", saves ~{_usd(saved)}" if saved > 0 else "")
        + ")"
    )
    lines.append(
        "handoff: each phase writes a checkpoint: to the shared store; the next "
        "phase reads only its bounded digest and resolves handles with ctx get."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Execution. Fail-open at every step: a phase that cannot run is recorded and
# skipped, never fatal. The handoff channel is the CAS store — a phase's output
# is stored as a blob and cited from a checkpoint the next phase reads.
# ---------------------------------------------------------------------------
@dataclass
class PhaseOutcome:
    phase: Phase
    host_name: str
    status: str            # "ok" | "skipped" | "failed"
    checkpoint_ref: str | None
    detail: str
    exit_code: int | None = None


@dataclass
class OrchestrationResult:
    plan: OrchestrationPlan
    outcomes: list[PhaseOutcome]


def _launch_host(
    host: DetectedHost, ws_root: Path, prompt: str, exe: str, *, timeout: float
) -> tuple[int, str, str]:
    """Run one harness in print mode with captured output, inside the harnessed
    workspace. Claude gets the ephemeral --settings hook injection (same source
    of truth as ctx.wrap); Codex/Antigravity discover their hooks from the
    workspace tree, so a print run there is already harnessed. Never raises."""
    spec = host.spec
    path = host.path or spec.cli_bins[0]
    argv = [path, *spec.print_flag, prompt]
    settings_tmp: str | None = None
    try:
        if spec.name == "claude":
            from ctx.installer import claude_hook_settings

            tmp = tempfile.NamedTemporaryFile(
                "w", prefix="ctx-orch-", suffix=".json", delete=False, encoding="utf-8"
            )
            json.dump(claude_hook_settings(exe), tmp)
            tmp.close()
            settings_tmp = tmp.name
            argv = [path, "--settings", settings_tmp, *spec.print_flag, prompt]
        proc = subprocess.run(
            argv,
            cwd=ws_root,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ},
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except (OSError, subprocess.SubprocessError) as e:
        return 127, "", f"{type(e).__name__}: {e}"
    finally:
        if settings_tmp:
            with contextlib.suppress(OSError):
                os.unlink(settings_tmp)


def _phase_prompt(phase: Phase, task: str, prior: str | None) -> str:
    """Build the phase instruction, threading the prior phase's checkpoint."""
    header = (
        f"You are the {phase.name.upper()} phase of a multi-harness "
        f"collaboration run under the ctx harness.\nTask: {task}\n"
        f"Your job this phase: {phase.goal}.\n"
        "Use ctx verbs (search/map/get/run) for all retrieval so evidence is "
        "addressed, not pasted. Cite handles (run:/blob:/repo:file:line)."
    )
    if prior:
        header += (
            "\n\nThe previous (cheaper) phase produced this checkpoint — resolve "
            "any handle with `ctx get`:\n" + prior
        )
    if phase.role == "capable":
        header += "\n\nMake the change now; keep narration terse."
    else:
        header += "\n\nDo not change code; end with a short findings summary."
    return header


def _checkpoint_phase(
    ws, store, phase: Phase, task: str, stdout: str, stderr: str
) -> str | None:
    """Store the phase's captured output as a blob and freeze a checkpoint that
    cites it — the lossless, addressed handoff to the next phase. Fail-open."""
    try:
        from ctx.checkpoint import create_checkpoint

        payload = (stdout or stderr or "").encode("utf-8", "replace")
        blob_id = store.put_blob(payload) if payload else None
        evidence = [f"blob:{blob_id} {phase.name} phase full output"] if blob_id else []
        state = (stdout or stderr or "").strip()[:600]
        cp_id, _ = create_checkpoint(
            store,
            ws,
            goal=f"{phase.name} phase of orchestrated task: {task}",
            state=state or f"{phase.name} phase produced no captured output",
            evidence=evidence,
        )
        return f"checkpoint:{cp_id[:12]}"
    except Exception:
        return None


def run_orchestration(
    ws,
    plan: OrchestrationPlan,
    *,
    exe: str | None = None,
    timeout: float = 900.0,
    launch=_launch_host,
) -> OrchestrationResult:
    """Execute the priced plan phase by phase, handing off checkpoints through
    the store. ``launch`` is injectable so tests exercise the handoff without a
    real CLI. Fail-open: a phase failure is recorded and the run continues."""
    from ctx.installer import _ctx_executable
    from ctx.store import Store

    resolved_exe = exe or _ctx_executable()
    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
    outcomes: list[PhaseOutcome] = []
    prior_checkpoint: str | None = None
    prior_doc: str | None = None

    for a in plan.assignments:
        phase, host = a.phase, a.host
        if not host.installed:
            outcomes.append(
                PhaseOutcome(phase, host.name, "skipped", None, "host not on PATH")
            )
            continue
        prompt = _phase_prompt(phase, plan.task, prior_doc)
        code, out, err = launch(host, ws.root, prompt, resolved_exe, timeout=timeout)
        status = "ok" if code == 0 else "failed"
        cp = _checkpoint_phase(ws, store, phase, plan.task, out, err)
        detail = (out or err or "").strip().splitlines()
        summary = detail[-1][:200] if detail else "(no output captured)"
        outcomes.append(
            PhaseOutcome(phase, host.name, status, cp, summary, exit_code=code)
        )
        if cp:
            prior_checkpoint = cp
            with contextlib.suppress(Exception):
                from ctx.checkpoint import show_checkpoint

                prior_doc = show_checkpoint(store, ws, cp)
        _ = prior_checkpoint  # threaded for future multi-hop policies
    return OrchestrationResult(plan=plan, outcomes=outcomes)


def render_result(result: OrchestrationResult) -> str:
    lines = ["[ctx orchestrate] run complete"]
    for i, o in enumerate(result.outcomes, 1):
        ref = f" {o.checkpoint_ref}" if o.checkpoint_ref else ""
        lines.append(
            f"  {i}. {o.phase.name:10} → {o.host_name:11} [{o.status}]{ref}"
        )
        if o.detail:
            lines.append(f"       {o.detail}")
    ran = [o for o in result.outcomes if o.status == "ok"]
    lines.append(
        f"phases ok: {len(ran)}/{len(result.outcomes)} · "
        "resolve any cited handle with `ctx get`"
    )
    return "\n".join(lines)


def orchestrate(
    ws,
    task: str,
    *,
    dry_run: bool = False,
    force_run: bool = False,
    exe: str | None = None,
) -> tuple[int, str]:
    """Top-level entry: detect harnesses, price the plan, show it, then run it
    (unless dry_run or [orchestrate] confirm is set). ``force_run`` overrides a
    configured confirm gate (the ``--run`` flag). Returns (exit_code, text)."""
    cfg = ws.config.orchestrate
    hosts = installed_harnessable(workspace_root=ws.root)
    try:
        plan = plan_orchestration(task, hosts, cfg)
    except ValueError as e:
        return 1, (
            f"ctx orchestrate: {e}.\n"
            "Install a supported CLI (claude, codex, antigravity) then re-run; "
            "see `ctx wrap detect`."
        )
    out = [render_plan(plan)]
    if dry_run or (cfg.confirm and not force_run):
        note = (
            "dry run — no harness launched."
            if dry_run
            else "[orchestrate] confirm=true — re-run without it, or `--run`, to execute."
        )
        out.append(note)
        return 0, "\n".join(out)
    result = run_orchestration(ws, plan, exe=exe)
    out.append("")
    out.append(render_result(result))
    return 0, "\n".join(out)


__all__ = [
    "Phase",
    "Assignment",
    "OrchestrationPlan",
    "PhaseOutcome",
    "OrchestrationResult",
    "default_phases",
    "cost_ladder",
    "plan_orchestration",
    "render_plan",
    "run_orchestration",
    "render_result",
    "orchestrate",
]
