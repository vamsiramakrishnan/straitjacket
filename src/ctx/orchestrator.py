"""Harness collaboration: a cheap coordinator decides how to split a task across
the installed harnesses by *capability x price*, and a closed loop coordinates
the work — dispatching ready subtasks, feeding each one's addressed evidence to
its dependents through the shared CAS store, escalating failures to a stronger
harness, and re-planning from what came back.

This is the cross-*harness* generalization of the shipped ctx-explorer fork
(ROADMAP M-A), turned into task coordination rather than open-loop calling:

* **Coordinate** (:func:`invoke_coordinator`) — the cheapest installed harness
  (e.g. Antigravity on Gemini-flash-lite), guided by the routing contract in
  the ctx-harness skill, decomposes the task into a ``ctx.route/v1`` DAG and
  assigns each node to a harness by capability and price. When no coordinator
  can run, :func:`fallback_route` produces a deterministic capability-routed DAG
  so orchestration still works offline.
* **Price & show** (:func:`build_route_plan`, :func:`render_route_plan`) — the
  DAG is validated (acyclic, bounded, budgeted), priced up front, and shown
  before any spend (the project's rewrite-not-ask posture).
* **Run the loop** (:func:`run_route`) — topological waves: ready nodes run in
  parallel, each seeing only its dependencies' ``checkpoint:`` digests (not raw
  bytes); a failed node escalates to a stronger harness; after a wave the
  coordinator may patch the plan with follow-up nodes, bounded by
  ``max_waves`` / ``max_replans`` / ``budget_usd``.

Everything is fail-open and bounded. Planning/pricing/scheduling is pure and
deterministic; coordinator invocation and node execution are injectable so the
loop is tested without a live CLI.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from ctx.hosts import (
    DetectedHost,
    ModelChoice,
    installed_harnessable,
    pick_coordinator,
    pick_model,
    tier_rank,
)

# Parallel wave nodes launch concurrently (the expensive part), but their store
# writes — blob + checkpoint manifest into one SQLite catalog — must be
# serialized: concurrent writers otherwise contend on the WAL lock and a losing
# writer would fail-open to a dropped checkpoint. The launches stay parallel;
# only the fast checkpoint write is guarded.
_CHECKPOINT_LOCK = threading.Lock()

ROUTE_SCHEMA = "ctx.route/v1"

# The contract handed to the coordinator model. Kept in lockstep with the skill
# reference plugins/*/skills/ctx-harness/references/harness-collaboration.md so
# the coordinator behaves the same whether it read the skill or only this prompt.
ROUTING_CONTRACT = """\
You are the COORDINATOR of a multi-harness collaboration run under the ctx
harness. Do NOT do the task yourself. Split it into subtasks and assign each to
the harness whose capability fits, spending the cheapest harness that can do the
work. Output ONLY a JSON object of schema ctx.route/v1 — no prose.

Rules:
- Decompose only where it helps. A trivial task is ONE node. Fan out only when
  subtasks are genuinely independent or form a real dependency chain.
- Each node: {"id","goal","role","min_tier","needs":[tags],"deps":[ids],
  "est_input_tokens","est_output_tokens"}. Optionally pin "host":"<name>",
  "model":"<model id from the menu>", and/or "prefer":"strong".
- min_tier is the weakest capability that can do the node: economy < standard <
  frontier. Route by MODEL, not just harness:
    * exploration / search / triage / verify -> economy
    * IMPLEMENTATION is complexity-adaptive: a SIMPLE edit (a line, a small
      well-specified function) -> economy (the cheapest model, e.g.
      Gemini-3.5-flash-lite); a COMPLEX change (multiple files, real design,
      tricky logic) -> standard (Gemini-3.6-flash). Judge the task and set the
      tier; do not default everything to standard.
    * PLANNING / architecture / hard reasoning -> frontier, and set
      "prefer":"strong" so it takes the flagship (Opus), not the cheapest
      frontier model — a good plan is worth the strong model.
  The router picks the model that meets the tier and covers the roles; "prefer":
  "cheap" (default) takes the cheapest, "strong" takes the flagship.
- deps make a node wait for others; their evidence (a checkpoint:) is handed to
  it. Keep the graph acyclic.
Return: {"schema":"ctx.route/v1","nodes":[ ... ]}
"""


# ---------------------------------------------------------------------------
# Route IR
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RouteNode:
    id: str
    goal: str
    role: str
    min_tier: str
    need_tags: tuple[str, ...]
    deps: tuple[str, ...]
    est_input_tokens: int
    est_output_tokens: int
    host_pin: str = ""    # optional explicit harness name from the coordinator
    model_pin: str = ""   # optional explicit model id from the coordinator
    prefer: str = "cheap"  # "cheap" (default) | "strong" (flagship at the tier)


@dataclass
class AssignedNode:
    node: RouteNode
    host: DetectedHost
    model: ModelChoice
    est_cost_usd: float
    tier_met: bool  # False when no installed model met min_tier (assigned strongest)


@dataclass
class RoutePlan:
    task: str
    hosts: tuple[DetectedHost, ...]
    coordinator: DetectedHost | None
    assigned: list[AssignedNode] = field(default_factory=list)

    @property
    def est_total_usd(self) -> float:
        return sum(a.est_cost_usd for a in self.assigned)

    @property
    def est_single_premium_usd(self) -> float:
        frontier = _frontier_model(list(self.hosts))
        if frontier is None:
            return 0.0
        host, model = frontier
        price = host.model_price(model.id)
        i = sum(a.node.est_input_tokens for a in self.assigned)
        o = sum(a.node.est_output_tokens for a in self.assigned)
        return price.cost_usd(input_tokens=i, output_tokens=o)

    def waves(self) -> list[list[AssignedNode]]:
        """Topological layers: each wave's nodes depend only on earlier waves.
        Assumes a validated acyclic graph."""
        done: set[str] = set()
        layers: list[list[AssignedNode]] = []
        remaining = list(self.assigned)
        while remaining:
            ready = [a for a in remaining if all(d in done for d in a.node.deps)]
            if not ready:  # defensive: a cycle slipped through — stop cleanly
                layers.append(remaining)
                break
            layers.append(ready)
            for a in ready:
                done.add(a.node.id)
            remaining = [a for a in remaining if a.node.id not in done]
        return layers


def _frontier_model(hosts: list[DetectedHost]) -> tuple[DetectedHost, ModelChoice] | None:
    """The strongest model available (highest tier, then priciest) — the
    baseline the collaboration is measured against."""
    cands = [(h, m) for h in hosts if h.installed for m in h.models]
    if not cands:
        return None
    return sorted(
        cands, key=lambda hm: (tier_rank(hm[1].tier), hm[0].model_price(hm[1].id).output)
    )[-1]


def cost_ladder(hosts: list[DetectedHost]) -> list[DetectedHost]:
    """Installed harnesses ranked cheapest -> premium by output price."""
    return sorted(
        [h for h in hosts if h.installed],
        key=lambda d: (d.price.output, d.price.input, d.name),
    )


class RouteError(Exception):
    """A coordinator plan that could not be validated into a runnable DAG."""


def _assign_host(node: RouteNode, hosts: list[DetectedHost]) -> AssignedNode:
    """Resolve a node to a (harness, model): honour explicit host/model pins when
    valid, else pick the cheapest model that meets the tier and covers the roles
    across all harnesses (pick_model). Price the node on the chosen model."""
    host: DetectedHost | None = None
    model: ModelChoice | None = None
    if node.host_pin:
        host = next((h for h in hosts if h.installed and h.name == node.host_pin), None)
        if host is not None and node.model_pin:
            model = host.spec.model(node.model_pin)
    if host is not None and model is None:
        # Host pinned but not the model: best eligible model on that host.
        got1 = pick_model([host], min_tier=node.min_tier, need_tags=node.need_tags, prefer=node.prefer)
        model = got1[1] if got1 else None
    if host is None or model is None:
        got = pick_model(hosts, min_tier=node.min_tier, need_tags=node.need_tags, prefer=node.prefer)
        if got is None:
            raise RouteError("no installed harness/model to assign a node to")
        host, model = got
    price = host.model_price(model.id)
    cost = price.cost_usd(
        input_tokens=node.est_input_tokens, output_tokens=node.est_output_tokens
    )
    return AssignedNode(
        node=node, host=host, model=model, est_cost_usd=cost,
        tier_met=tier_rank(model.tier) >= tier_rank(node.min_tier),
    )


def _coerce_node(raw: dict, i: int) -> RouteNode:
    """One raw JSON node -> RouteNode, tolerant of missing/loose fields."""
    nid = str(raw.get("id") or f"n{i}").strip()
    tier = str(raw.get("min_tier") or "standard").strip().lower()
    return RouteNode(
        id=nid,
        goal=str(raw.get("goal") or raw.get("role") or "do the subtask"),
        role=str(raw.get("role") or "task"),
        min_tier=tier,
        need_tags=tuple(str(t) for t in (raw.get("needs") or raw.get("need_tags") or [])),
        deps=tuple(str(d) for d in (raw.get("deps") or [])),
        est_input_tokens=int(raw.get("est_input_tokens") or 20000),
        est_output_tokens=int(raw.get("est_output_tokens") or 3000),
        host_pin=str(raw.get("host") or raw.get("host_pin") or "").strip(),
        model_pin=str(raw.get("model") or raw.get("model_pin") or "").strip(),
        prefer=("strong" if str(raw.get("prefer") or "").strip().lower() == "strong" else "cheap"),
    )


def build_route_plan(
    task: str,
    raw: dict,
    hosts: list[DetectedHost],
    cfg,
    *,
    coordinator: DetectedHost | None = None,
) -> RoutePlan:
    """Validate a coordinator-emitted ``ctx.route/v1`` object into a priced DAG.
    Raises RouteError on anything unrunnable (no nodes, unknown deps, a cycle,
    over the node bound, or over budget) so the caller can fall back."""
    nodes_raw = raw.get("nodes") if isinstance(raw, dict) else None
    if not isinstance(nodes_raw, list) or not nodes_raw:
        raise RouteError("route plan has no nodes")
    max_nodes = getattr(cfg, "max_nodes", 12)
    if len(nodes_raw) > max_nodes:
        nodes_raw = nodes_raw[:max_nodes]
    nodes = [_coerce_node(n, i) for i, n in enumerate(nodes_raw) if isinstance(n, dict)]
    if not nodes:
        raise RouteError("route plan has no valid nodes")
    ids = [n.id for n in nodes]
    if len(set(ids)) != len(ids):
        raise RouteError("route plan has duplicate node ids")
    idset = set(ids)
    for n in nodes:
        for d in n.deps:
            if d not in idset:
                raise RouteError(f"node {n.id!r} depends on unknown node {d!r}")
    _assert_acyclic(nodes)
    assigned = [_assign_host(n, hosts) for n in nodes]
    plan = RoutePlan(
        task=task, hosts=tuple(hosts), coordinator=coordinator, assigned=assigned
    )
    budget = float(getattr(cfg, "budget_usd", 0.0) or 0.0)
    if budget > 0 and plan.est_total_usd > budget:
        raise RouteError(
            f"route plan est ${plan.est_total_usd:.2f} exceeds budget ${budget:.2f}"
        )
    return plan


def _assert_acyclic(nodes: list[RouteNode]) -> None:
    deps = {n.id: set(n.deps) for n in nodes}
    done: set[str] = set()
    while len(done) < len(nodes):
        ready = [nid for nid, ds in deps.items() if nid not in done and ds <= done]
        if not ready:
            raise RouteError("route plan has a dependency cycle")
        done.update(ready)


def fallback_route(task: str, hosts: list[DetectedHost], cfg) -> RoutePlan:
    """Deterministic model-routed DAG for when no coordinator can run:
    explore (economy) -> plan (frontier, prefer STRONG) -> implement -> verify
    (economy). Planning takes the frontier *flagship* (Opus when Claude is
    installed), because a good plan is worth the strong model. Implementation is
    complexity-adaptive: ``[orchestrate] implement_tier`` (default ``standard`` =
    Gemini-3.6-flash) for real work, set ``economy`` (Gemini-3.5-flash-lite) for
    simple edits. A live coordinator makes this call per task; the fallback uses
    the configured default. Same handoff/pricing as a coordinator plan."""
    plan_in = max(1, cfg.implement_input_tokens // 3)
    impl_tier = getattr(cfg, "implement_tier", "standard") or "standard"
    # A simple (economy) edit needs less than heavy code-gen: lighter role needs
    # let the cheap flash-lite implementer win; complex work asks for "code".
    impl_tags = ("implement", "edit") if impl_tier == "economy" else ("implement", "edit", "code")
    nodes = [
        RouteNode(
            "explore",
            "gather the evidence the task needs (search/map/read); deposit ctx "
            "handles; change nothing",
            "explore", "economy",
            ("search", "triage", "explore"), (),
            cfg.explore_input_tokens, cfg.explore_output_tokens,
        ),
        RouteNode(
            "plan",
            "from the explore checkpoint, decide the approach and the exact edits",
            "plan", "frontier",
            ("plan", "reason", "architect"), ("explore",),
            plan_in, cfg.explore_output_tokens,
            prefer="strong",   # the flagship planner (Opus), not the cheapest frontier
        ),
        RouteNode(
            "implement",
            "make the edits the plan checkpoint specifies",
            "implement", impl_tier,
            impl_tags, ("plan",),
            cfg.implement_input_tokens, cfg.implement_output_tokens,
        ),
        RouteNode(
            "verify",
            "run the acceptance check and inspect the diff",
            "verify", "economy",
            ("verify", "test"), ("implement",),
            cfg.review_input_tokens, cfg.review_output_tokens,
        ),
    ]
    assigned = [_assign_host(n, hosts) for n in nodes]
    return RoutePlan(
        task=task, hosts=tuple(hosts),
        coordinator=pick_coordinator(hosts), assigned=assigned,
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _usd(x: float) -> str:
    return f"${x:.2f}" if x >= 0.005 else f"${x:.4f}"


def build_menu(hosts: list[DetectedHost]) -> str:
    """The capability × price × model menu handed to the coordinator and shown to
    the user — every (harness, model) it may route to, so it can pin a model."""
    rows = ["harnesses & models available (model · tier · $in/$out per 1M · roles):"]
    for h in cost_ladder(hosts):
        rows.append(f"  {h.name}:")
        for m in sorted(h.models, key=lambda m: (-tier_rank(m.tier), h.model_price(m.id).output)):
            p = h.model_price(m.id)
            rows.append(
                f"    {m.id:22} {m.tier:9} {_usd(p.input)}/{_usd(p.output)}  "
                f"{', '.join(m.roles) or '—'}"
            )
    return "\n".join(rows)


def render_route_plan(plan: RoutePlan) -> str:
    lines = [f'[ctx orchestrate] task: "{plan.task}"']
    if plan.coordinator:
        lines.append(
            f"coordinator: {plan.coordinator.name} "
            f"[{plan.coordinator.spec.coord_model}] "
            f"~{_usd(plan.coordinator.coordinator_price().output)}/Mout"
        )
    lines.append(build_menu(list(plan.hosts)))
    lines.append(f"routing ({len(plan.assigned)} nodes, "
                 f"{len(plan.waves())} waves):")
    for wi, wave in enumerate(plan.waves(), 1):
        lines.append(f"  wave {wi}:")
        for a in wave:
            dep = f" ⇐ {','.join(a.node.deps)}" if a.node.deps else ""
            warn = "" if a.tier_met else "  ⚠ tier unmet (assigned strongest)"
            lines.append(
                f"    {a.node.id:10} → {a.host.name}/{a.model.id} "
                f"({a.node.min_tier}→{a.model.tier}) "
                f"est ~{_usd(a.est_cost_usd)}{dep}{warn}"
            )
    # Honest framing: show the all-flagship figure as a neutral reference, not a
    # claimed "saving" — routing only beats the baseline where you'd otherwise
    # have run every node on your most expensive model. It spends the flagship on
    # the plan node and keeps the rest cheap; it is not cheaper than a flat mid-
    # tier (e.g. Sonnet) run. See evals/orchestrator-cost-routing-2026-07-24.md.
    fm = _frontier_model(list(plan.hosts))
    fname = fm[1].id if fm else "the flagship"
    lines.append(
        f"estimated total: ~{_usd(plan.est_total_usd)}  "
        f"(for reference, running every node on {fname} would be "
        f"~{_usd(plan.est_single_premium_usd)})"
    )
    lines.append(
        "handoff: each node writes a checkpoint: to the shared store; its "
        "dependents read only that bounded digest and resolve handles with ctx get."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Coordinator invocation
# ---------------------------------------------------------------------------
def _launch_host(
    host: DetectedHost,
    ws_root: Path,
    prompt: str,
    exe: str,
    *,
    timeout: float,
    model: str = "",
) -> tuple[int, str, str]:
    """Run one harness in print mode with captured output, inside the harnessed
    workspace. Claude gets the ephemeral --settings hook injection; Codex /
    Antigravity discover their hooks from the workspace tree. ``model`` pins the
    model via the host's model flag (used for the cheap coordinator). Never
    raises."""
    spec = host.spec
    path = host.path or spec.cli_bins[0]
    argv = [path, *spec.print_flag, prompt]
    if model and spec.model_flag:
        argv = [path, spec.model_flag, model, *spec.print_flag, prompt]
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
            head = [path, "--settings", settings_tmp]
            argv = ([*head, spec.model_flag, model, *spec.print_flag, prompt]
                    if model and spec.model_flag
                    else [*head, *spec.print_flag, prompt])
        proc = subprocess.run(
            argv, cwd=ws_root, capture_output=True, text=True,
            timeout=timeout, env={**os.environ},
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except (OSError, subprocess.SubprocessError) as e:
        return 127, "", f"{type(e).__name__}: {e}"
    finally:
        if settings_tmp:
            with contextlib.suppress(OSError):
                os.unlink(settings_tmp)


def _extract_json(text: str) -> dict | None:
    """Pull the first balanced JSON object out of a model's stdout. Tolerant of
    surrounding prose or ```json fences."""
    if not text:
        return None
    start = text.find("{")
    while start != -1:
        depth = 0
        for j in range(start, len(text)):
            c = text[j]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    with contextlib.suppress(json.JSONDecodeError):
                        obj = json.loads(text[start : j + 1])
                        if isinstance(obj, dict):
                            return obj
                    break
        start = text.find("{", start + 1)
    return None


def invoke_coordinator(
    ws,
    task: str,
    hosts: list[DetectedHost],
    cfg,
    *,
    exe: str,
    extra: str = "",
    launch=_launch_host,
    timeout: float = 300.0,
) -> dict | None:
    """Ask the cheapest harness to emit a ctx.route/v1 plan. Returns the parsed
    dict, or None if no coordinator is available or it produced no parseable
    plan (the caller then uses fallback_route). Fail-open."""
    coord = pick_coordinator(hosts)
    if coord is None:
        return None
    prompt = (
        ROUTING_CONTRACT + "\n\n" + build_menu(hosts) + f"\n\nTask: {task}\n" + extra
    )
    try:
        code, out, _err = launch(
            coord, ws.root, prompt, exe, timeout=timeout, model=coord.spec.coord_model
        )
    except Exception:
        return None
    if code != 0:
        return None
    return _extract_json(out)


# ---------------------------------------------------------------------------
# Closed-loop execution
# ---------------------------------------------------------------------------
@dataclass
class NodeOutcome:
    node_id: str
    host_name: str
    status: str            # "ok" | "failed" | "skipped"
    checkpoint_ref: str | None
    detail: str
    escalated_to: str | None = None
    exit_code: int | None = None


@dataclass
class RouteResult:
    plan: RoutePlan
    outcomes: list[NodeOutcome]
    waves_run: int
    replans: int


def _checkpoint_node(ws, node: RouteNode, task: str, stdout: str, stderr: str) -> str | None:
    """Freeze a node's captured output into a checkpoint citing a blob of the
    full output — the addressed handoff to its dependents. Fail-open.

    Opens its own Store so it is safe to call from parallel worker threads
    (sqlite3 connections are check_same_thread by default; a shared connection
    would raise across threads)."""
    try:
        from ctx.checkpoint import create_checkpoint
        from ctx.store import Store
        from ctx.textutil import short_id

        payload = (stdout or stderr or "").encode("utf-8", "replace")
        state = (stdout or stderr or "").strip()[:600] or f"node {node.id}: no output"
        # Serialize the store mutation across parallel-wave threads (see the
        # module comment on _CHECKPOINT_LOCK). Own Store per call so the sqlite
        # connection is never shared across threads.
        with _CHECKPOINT_LOCK:
            store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
            blob_id = store.put_blob(payload) if payload else None
            evidence = [f"blob:{blob_id} node {node.id} full output"] if blob_id else []
            cp_id, _ = create_checkpoint(
                store, ws,
                goal=f"node {node.id} ({node.role}) of orchestrated task: {task}",
                state=state,
                evidence=evidence,
            )
            store.close()
        return f"checkpoint:{short_id(cp_id)}"
    except Exception:
        return None


def _node_prompt(node: RouteNode, task: str, dep_docs: list[str]) -> str:
    p = (
        f"You are node '{node.id}' ({node.role}) of a multi-harness collaboration "
        f"under the ctx harness.\nOverall task: {task}\n"
        f"Your subtask: {node.goal}.\n"
        "Use ctx verbs for retrieval; cite handles (run:/blob:/repo:file:line), "
        "do not paste output."
    )
    if dep_docs:
        p += (
            "\n\nUpstream nodes produced these checkpoints — resolve any handle "
            "with `ctx get`:\n" + "\n---\n".join(dep_docs)
        )
    return p


def _escalate(
    cur_model: ModelChoice, hosts: list[DetectedHost]
) -> tuple[DetectedHost, ModelChoice] | None:
    """The cheapest (host, model) strictly more capable than the failed node's
    model — the escalation target. One tier up, cheapest, across all harnesses."""
    better = [
        (h, m) for h in hosts if h.installed for m in h.models
        if tier_rank(m.tier) > tier_rank(cur_model.tier)
    ]
    if not better:
        return None
    return sorted(
        better, key=lambda hm: (tier_rank(hm[1].tier), hm[0].model_price(hm[1].id).output, hm[0].name)
    )[0]


def run_route(
    ws,
    plan: RoutePlan,
    cfg,
    *,
    exe: str | None = None,
    launch=_launch_host,
    coordinate=None,
    max_workers: int = 4,
) -> RouteResult:
    """Coordinate the DAG closed-loop. Ready nodes (deps satisfied) run in
    parallel; each sees its deps' checkpoints; a failed node escalates once to a
    stronger harness; between waves the coordinator may patch the plan with
    follow-up nodes. Bounded by cfg.max_waves / max_replans / budget_usd. Every
    step is fail-open."""
    from ctx.installer import _ctx_executable
    from ctx.store import Store

    resolved_exe = exe or _ctx_executable()
    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
    nodes: list[AssignedNode] = list(plan.assigned)
    state: dict[str, str] = {a.node.id: "pending" for a in nodes}
    cp: dict[str, str] = {}
    docs: dict[str, str] = {}
    outcomes: dict[str, NodeOutcome] = {}
    hosts = list(plan.hosts)
    max_waves = int(getattr(cfg, "max_waves", 4) or 4)
    replans_left = int(getattr(cfg, "max_replans", 2) or 0)
    budget = float(getattr(cfg, "budget_usd", 0.0) or 0.0)
    timeout = float(getattr(cfg, "node_timeout", 900.0) or 900.0)
    spent_est = 0.0
    waves_run = 0
    replans_done = 0

    def run_one(a: AssignedNode) -> NodeOutcome:
        dep_docs = [docs[d] for d in a.node.deps if d in docs]
        prompt = _node_prompt(a.node, plan.task, dep_docs)
        host, model = a.host, a.model
        code, out, err = launch(host, ws.root, prompt, resolved_exe, timeout=timeout, model=model.launch_id)
        escalated = None
        if code != 0:
            target = _escalate(a.model, hosts)
            if target is not None:
                host, model = target
                escalated = f"{host.name}/{model.id}"
                code, out, err = launch(host, ws.root, prompt, resolved_exe, timeout=timeout, model=model.launch_id)
        ref = _checkpoint_node(ws, a.node, plan.task, out, err)
        tail = (out or err or "").strip().splitlines()
        return NodeOutcome(
            node_id=a.node.id,
            host_name=f"{host.name}/{model.id}",
            status="ok" if code == 0 else "failed",
            checkpoint_ref=ref,
            detail=(tail[-1][:200] if tail else "(no output)"),
            escalated_to=escalated,
            exit_code=code,
        )

    while waves_run < max_waves:
        # Skip nodes whose dependency failed — they can never become ready.
        for a in nodes:
            if state[a.node.id] == "pending" and any(
                state.get(d) in ("failed", "skipped") for d in a.node.deps
            ):
                state[a.node.id] = "skipped"
                outcomes[a.node.id] = NodeOutcome(
                    a.node.id, a.host.name, "skipped", None, "dependency did not complete"
                )
        ready = [
            a for a in nodes
            if state[a.node.id] == "pending"
            and all(state.get(d) == "ok" for d in a.node.deps)
        ]
        if not ready:
            # Nothing runnable. Try a bounded re-plan if failures occurred.
            failed = [nid for nid, s in state.items() if s == "failed"]
            if coordinate and replans_left > 0 and failed:
                replans_left -= 1
                patch = coordinate(_replan_context(plan.task, outcomes))
                added = _merge_patch(nodes, state, patch, hosts, cfg)
                if added:
                    replans_done += 1
                    continue
            break
        waves_run += 1
        with ThreadPoolExecutor(max_workers=min(max_workers, len(ready))) as pool:
            results = list(pool.map(run_one, ready))
        for a, o in zip(ready, results):
            state[a.node.id] = o.status
            outcomes[a.node.id] = o
            if o.checkpoint_ref:
                cp[a.node.id] = o.checkpoint_ref
                with contextlib.suppress(Exception):
                    from ctx.checkpoint import show_checkpoint

                    docs[a.node.id] = show_checkpoint(store, ws, o.checkpoint_ref)
            spent_est += a.est_cost_usd
        if budget > 0 and spent_est >= budget:
            break

    # Any never-run node is recorded skipped for a complete ledger.
    for a in nodes:
        outcomes.setdefault(
            a.node.id,
            NodeOutcome(a.node.id, a.host.name, "skipped", None, "not reached (bounds)"),
        )
    ordered = [outcomes[a.node.id] for a in nodes]
    return RouteResult(plan=plan, outcomes=ordered, waves_run=waves_run, replans=replans_done)


def _replan_context(task: str, outcomes: dict[str, NodeOutcome]) -> str:
    lines = ["Some nodes failed. Emit ctx.route/v1 with ONLY follow-up nodes to "
             "recover (new ids; deps may reference completed nodes)."]
    for o in outcomes.values():
        lines.append(f"  {o.node_id}: {o.status} — {o.detail}")
    return "\n".join(lines)


def _merge_patch(nodes, state, patch, hosts, cfg) -> int:
    """Merge coordinator-supplied follow-up nodes into the live node set,
    respecting the node bound. Returns how many were added."""
    if not isinstance(patch, dict):
        return 0
    raw = patch.get("nodes")
    if not isinstance(raw, list):
        return 0
    max_nodes = int(getattr(cfg, "max_nodes", 12) or 12)
    existing = {a.node.id for a in nodes}
    added = 0
    for i, r in enumerate(raw):
        if not isinstance(r, dict) or len(nodes) >= max_nodes:
            break
        n = _coerce_node(r, len(nodes) + i)
        if n.id in existing:
            continue
        with contextlib.suppress(RouteError):
            nodes.append(_assign_host(n, hosts))
            state[n.id] = "pending"
            existing.add(n.id)
            added += 1
    return added


def render_result(result: RouteResult) -> str:
    lines = ["[ctx orchestrate] run complete"]
    for o in result.outcomes:
        ref = f" {o.checkpoint_ref}" if o.checkpoint_ref else ""
        esc = f" (escalated→{o.escalated_to})" if o.escalated_to else ""
        lines.append(f"  {o.node_id:10} → {o.host_name:11} [{o.status}]{esc}{ref}")
        if o.detail:
            lines.append(f"     {o.detail}")
    ok = sum(1 for o in result.outcomes if o.status == "ok")
    lines.append(
        f"nodes ok: {ok}/{len(result.outcomes)} · waves {result.waves_run} · "
        f"replans {result.replans} · resolve handles with `ctx get`"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Top-level entry
# ---------------------------------------------------------------------------
def orchestrate(
    ws,
    task: str,
    *,
    dry_run: bool = False,
    force_run: bool = False,
    exe: str | None = None,
    launch=_launch_host,
) -> tuple[int, str]:
    """Detect harnesses → coordinate a route (cheap model, or deterministic
    fallback) → price & show it → run the closed loop. Returns (exit_code, text)."""
    from ctx.installer import _ctx_executable

    cfg = ws.config.orchestrate
    resolved_exe = exe or _ctx_executable()
    hosts = installed_harnessable(workspace_root=ws.root)
    if not any(h.installed for h in hosts):
        return 1, (
            "ctx orchestrate: no installed harnessable CLI to orchestrate across.\n"
            "Install a supported CLI (claude, codex, antigravity); see `ctx wrap detect`."
        )

    coordinator = pick_coordinator(hosts)
    raw = None
    if not getattr(cfg, "fallback_only", False):
        raw = invoke_coordinator(ws, task, hosts, cfg, exe=resolved_exe, launch=launch)

    note = ""
    plan: RoutePlan
    if raw is not None:
        try:
            plan = build_route_plan(task, raw, hosts, cfg, coordinator=coordinator)
        except RouteError as e:
            note = f"coordinator plan rejected ({e}); using deterministic route"
            plan = fallback_route(task, hosts, cfg)
    else:
        plan = fallback_route(task, hosts, cfg)

    out = [render_route_plan(plan)]
    if note:
        out.append(note)
    if dry_run or (getattr(cfg, "confirm", False) and not force_run):
        out.append(
            "dry run — no harness launched." if dry_run
            else "[orchestrate] confirm=true — re-run with --run to execute."
        )
        return 0, "\n".join(out)

    def coordinate(extra: str) -> dict | None:
        return invoke_coordinator(
            ws, task, hosts, cfg, exe=resolved_exe, extra=extra, launch=launch
        )

    result = run_route(ws, plan, cfg, exe=resolved_exe, launch=launch, coordinate=coordinate)
    out.append("")
    out.append(render_result(result))
    return 0, "\n".join(out)


__all__ = [
    "ROUTE_SCHEMA",
    "ROUTING_CONTRACT",
    "RouteNode",
    "AssignedNode",
    "RoutePlan",
    "RouteError",
    "RouteResult",
    "NodeOutcome",
    "cost_ladder",
    "build_menu",
    "build_route_plan",
    "fallback_route",
    "render_route_plan",
    "invoke_coordinator",
    "run_route",
    "render_result",
    "orchestrate",
]
