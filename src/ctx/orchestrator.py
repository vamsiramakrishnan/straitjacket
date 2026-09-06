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
import re
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path

from ctx.hosts import (
    DetectedHost,
    ModelChoice,
    installed_harnessable,
    pick_coordinator,
    pick_model,
    tier_rank,
)
from ctx import steward as _steward
from ctx import taskledger as _ledger
from ctx.handoff_policy import choose_handoff
from ctx.textutil import short_id
from ctx.mutation_policy import choose_mutation_isolation
from ctx.usage import ActualUsage, coerce_usage, parse_host_output, summarize_usage
from ctx.verification_policy import choose_verification
from ctx.wave_policy import choose_wave
from ctx.worker_yield import WorkerYieldSchemaError, check_schema, validate as validate_worker_yield
from ctx.worktree_isolation import (
    IsolatedWorktree,
    WorktreeIsolationError,
    WorktreePatch,
    apply_patches,
    clean_git_root,
    normalize_targets,
    preflight_patch,
    targets_overlap,
)

# Parallel wave nodes launch concurrently (the expensive part), but their store
# writes — blob + checkpoint manifest into one SQLite catalog — must be
# serialized: concurrent writers otherwise contend on the WAL lock and a losing
# writer would fail-open to a dropped checkpoint. The launches stay parallel;
# only the fast checkpoint write is guarded.
_CHECKPOINT_LOCK = threading.Lock()

ROUTE_SCHEMA = "ctx.route/v1"
ROUTE_RUN_SCHEMA = "ctx.route-run/v1"

_TASK_CLASSIFIER_LOOKAHEAD_CHARS = 128
_POLITE_TASK_PREFIX = re.compile(
    r"^(?:(?:please|kindly)\s+|(?:can|could|would)\s+you\s+)",
    re.IGNORECASE,
)
_TEST_TASK = re.compile(
    rf"^(?:(?:run|rerun|execute)\b.{{0,{_TASK_CLASSIFIER_LOOKAHEAD_CHARS}}}"
    r"\b(?:pytest|tests?|unittest|specs?)\b|(?:pytest|python\s+-m\s+pytest)\b)",
    re.IGNORECASE,
)
_REVIEW_TASK = re.compile(
    r"^(?:review|summarize|inspect)\b.{0,80}\b(?:diff|patch|changes)\b",
    re.IGNORECASE,
)
_SIMPLE_EDIT_TASK = re.compile(
    r"^(?:fix|edit|update|change|rename|replace)\b",
    re.IGNORECASE,
)
_SIMPLE_EDIT_MARKER = re.compile(
    r"\b(?:typo|spelling|one[- ]line|single[- ]line|known file)\b",
    re.IGNORECASE,
)
_BOUNDED_FEATURE_TASK = re.compile(
    r"^(?:add|implement|create)\b", re.IGNORECASE
)
_ANSWER_TASK = re.compile(r"^(?:explain|describe|summarize)\b", re.IGNORECASE)
_INSPECT_TASK = re.compile(r"^(?:inspect|read|show)\b", re.IGNORECASE)
_NAMED_TARGET = re.compile(
    r"(?:^|\s)[\w./-]+\.(?:py|js|ts|tsx|jsx|go|rs|java|md|toml|ya?ml)(?:\b|$)",
    re.IGNORECASE,
)
_NAMED_ACCEPTANCE = re.compile(
    r"\b(?:pytest|tests?|specs?|acceptance checks?)\b", re.IGNORECASE
)
_HIGH_RISK_SCOPE = re.compile(
    r"\b(?:architecture|authorization|authentication|security|migration|migrate|"
    r"database|schema|deploy|production|breaking)\b",
    re.IGNORECASE,
)
_EXPLICIT_CONTRACT = re.compile(
    r"\b(?:must|returns?|expected|acceptance criteria)\b", re.IGNORECASE
)

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
  "model":"<model id from the menu>", and/or "prefer":"strong". Pins are
  advisory and never bypass unattended eligibility.
- Mutation nodes may declare repository-relative "targets":[paths]. Independent
  mutation nodes are eligible for isolated worktrees only when every target is
  declared and the target sets do not overlap.
- A node may request a typed final yield with "output_schema":{...} and opt into
  fail-closed enforcement with "strict_output_schema":true.
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
  it. Keep the graph acyclic. Every mutation node MUST have a separate
  downstream verify/test node; combining implementation and claimed
  verification into one node is rejected.
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
    # Declared repository-relative mutation scope. Multiple ready mutation
    # nodes are worktree-eligible only when every node declares disjoint paths.
    targets: tuple[str, ...] = ()
    # Optional dependency-free JSON-Schema subset for the worker's final yield.
    output_schema: dict | None = None
    strict_output_schema: bool = False
    edit_shape: str = ""


@dataclass
class AssignedNode:
    node: RouteNode
    host: DetectedHost
    model: ModelChoice
    est_cost_usd: float
    tier_met: bool  # False when no installed model met min_tier (assigned strongest)
    verification_policy: str = ""


@dataclass
class RoutePlan:
    task: str
    hosts: tuple[DetectedHost, ...]
    coordinator: DetectedHost | None
    assigned: list[AssignedNode] = field(default_factory=list)
    source: str = "coordinator"
    task_kind: str = "general"

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


def _assign_host(
    node: RouteNode,
    hosts: list[DetectedHost],
    *,
    allow_interactive_pin: bool = False,
) -> AssignedNode:
    """Resolve a node to a (harness, model): honour explicit host/model pins when
    valid, else pick the cheapest model that meets the tier and covers the roles
    across all harnesses (pick_model). Price the node on the chosen model."""
    host: DetectedHost | None = None
    model: ModelChoice | None = None
    if node.host_pin:
        host = next((h for h in hosts if h.installed and h.name == node.host_pin), None)
        if host is not None and not host.spec.unattended and not allow_interactive_pin:
            host = None
        if host is not None and node.model_pin:
            model = host.spec.model(node.model_pin)
    if host is not None and model is None:
        # Host pinned but not the model: best eligible model on that host.
        got1 = pick_model([host], min_tier=node.min_tier, need_tags=node.need_tags, prefer=node.prefer)
        model = got1[1] if got1 else None
    if host is None or model is None:
        unattended = [h for h in hosts if h.spec.unattended]
        got = pick_model(
            unattended,
            min_tier=node.min_tier,
            need_tags=node.need_tags,
            prefer=node.prefer,
        )
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


def _is_mutation_node(node: RouteNode) -> bool:
    return node.role == "implement" or "edit" in node.need_tags


def _is_verification_node(node: RouteNode) -> bool:
    return node.role in {"verify", "test"} or bool(
        {"verify", "test"} & set(node.need_tags)
    )


def _apply_verification_policy(
    task: str,
    assigned: list[AssignedNode],
    hosts: list[DetectedHost],
) -> list[AssignedNode]:
    """Route verification independently when risk justifies the extra host."""
    by_id = {item.node.id: item for item in assigned}
    available = [host for host in hosts if host.installed and host.spec.unattended]
    host_names = {host.name for host in available}
    kind = _fallback_task_kind(task)
    complexity = {
        "answer": 1,
        "inspect": 1,
        "review": 2,
        "test": 1,
        "simple_edit": 1,
        "bounded_feature": 3,
        "general": 4,
    }.get(kind, 4)
    high_risk = bool(_HIGH_RISK_SCOPE.search(task))
    routed: list[AssignedNode] = []
    for item in assigned:
        node = item.node
        if not _is_verification_node(node):
            routed.append(item)
            continue
        mutation_hosts = {
            by_id[dep].host.name
            for dep in node.deps
            if dep in by_id and _is_mutation_node(by_id[dep].node)
        }
        alternate = bool(host_names - mutation_hosts) and bool(mutation_hosts)
        strategy = choose_verification(
            {
                "mutation": bool(mutation_hosts),
                "complexity": complexity,
                "high_risk": high_risk,
                "alternate_host": alternate,
            }
        )
        selected = item
        if strategy.startswith("independent_") and alternate and not (
            node.host_pin or node.model_pin
        ):
            eligible = [host for host in available if host.name not in mutation_hosts]
            target_tier = "standard" if strategy.endswith("standard") else "economy"
            candidate_node = replace(node, min_tier=target_tier)
            try:
                selected = _assign_host(candidate_node, eligible)
            except RouteError:
                strategy += "_fallback"
        selected.verification_policy = strategy
        routed.append(selected)
    return routed


def _coerce_node(raw: dict, i: int) -> RouteNode:
    """One raw JSON node -> RouteNode, tolerant of missing/loose fields."""
    nid = str(raw.get("id") or f"n{i}").strip()
    tier = str(raw.get("min_tier") or "standard").strip().lower()
    raw_targets = raw.get("targets") or []
    if isinstance(raw_targets, str):
        raw_targets = [raw_targets]
    try:
        targets = normalize_targets(tuple(str(value) for value in raw_targets))
    except WorktreeIsolationError as exc:
        raise RouteError(f"node {nid!r} has invalid targets: {exc}") from exc
    output_schema = raw.get("output_schema")
    if output_schema is not None:
        try:
            check_schema(output_schema)
        except WorkerYieldSchemaError as exc:
            raise RouteError(f"node {nid!r} has invalid output_schema: {exc}") from exc
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
        targets=targets,
        output_schema=output_schema,
        strict_output_schema=bool(raw.get("strict_output_schema", False)),
        edit_shape=str(raw.get("edit_shape") or "")[:64],
    )


def build_route_plan(
    task: str,
    raw: dict,
    hosts: list[DetectedHost],
    cfg,
    *,
    coordinator: DetectedHost | None = None,
    allow_interactive_pins: bool = False,
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
    _assert_mutations_have_downstream_verification(nodes)
    assigned = [
        _assign_host(n, hosts, allow_interactive_pin=allow_interactive_pins)
        for n in nodes
    ]
    assigned = _apply_verification_policy(task, assigned, hosts)
    plan = RoutePlan(
        task=task,
        hosts=tuple(hosts),
        coordinator=coordinator,
        assigned=assigned,
        source="coordinator",
        task_kind=_fallback_task_kind(task),
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


def _assert_mutations_have_downstream_verification(nodes: list[RouteNode]) -> None:
    """Reject coordinator plans that can mutate without a later check."""
    by_id = {node.id: node for node in nodes}

    def depends_on(node: RouteNode, target: str, seen: set[str]) -> bool:
        if target in node.deps:
            return True
        return any(
            dep not in seen
            and dep in by_id
            and depends_on(by_id[dep], target, seen | {dep})
            for dep in node.deps
        )

    mutations = [
        node
        for node in nodes
        if node.role == "implement" or "edit" in node.need_tags
    ]
    verifiers = [
        node
        for node in nodes
        if node.role in {"verify", "test"}
        or bool({"verify", "test"} & set(node.need_tags))
    ]
    for mutation in mutations:
        if not any(
            verifier.id != mutation.id
            and depends_on(verifier, mutation.id, {verifier.id})
            for verifier in verifiers
        ):
            raise RouteError(
                f"mutation node {mutation.id!r} has no downstream verification"
            )


def _fallback_task_kind(task: str) -> str:
    """Normalize only high-confidence simple-task shapes.

    This deliberately avoids free-text substring inference. In particular,
    words such as ``latest`` and ``testimony`` are not test requests. Unknown
    or potentially complex work stays on the complete four-stage route.
    """
    normalized = " ".join(task.split())
    while True:
        stripped = _POLITE_TASK_PREFIX.sub("", normalized, count=1)
        if stripped == normalized:
            break
        normalized = stripped.lstrip()
    if _TEST_TASK.search(normalized):
        return "test"
    if _REVIEW_TASK.search(normalized):
        return "review"
    if _SIMPLE_EDIT_TASK.search(normalized) and _SIMPLE_EDIT_MARKER.search(normalized):
        return "simple_edit"
    if (
        _BOUNDED_FEATURE_TASK.search(normalized)
        and _NAMED_TARGET.search(normalized)
        and _NAMED_ACCEPTANCE.search(normalized)
        and _EXPLICIT_CONTRACT.search(normalized)
        and not _HIGH_RISK_SCOPE.search(normalized)
    ):
        return "bounded_feature"
    if _ANSWER_TASK.search(normalized):
        return "answer"
    if _INSPECT_TASK.search(normalized):
        return "inspect"
    return "general"


def _fast_fallback_nodes(task: str, cfg) -> list[RouteNode] | None:
    """Compile a high-confidence task shape to its smallest completing DAG."""
    kind = _fallback_task_kind(task)
    if kind in {"answer", "inspect"}:
        return [
            RouteNode(
                "answer",
                "complete the task directly; retrieve only focused evidence if needed",
                "answer",
                "standard",
                ("summarize", "explore"),
                (),
                max(1, cfg.explore_input_tokens // 2),
                cfg.explore_output_tokens,
            )
        ]
    if kind == "review":
        return [
            RouteNode(
                "review",
                "inspect only the requested diff or changes and return the review",
                "review",
                "standard",
                ("review", "summarize"),
                (),
                cfg.review_input_tokens,
                cfg.review_output_tokens,
            )
        ]
    if kind == "test":
        return [
            RouteNode(
                "verify",
                "run only the requested test target and report the result",
                "verify",
                "economy",
                ("verify", "test"),
                (),
                cfg.review_input_tokens,
                cfg.review_output_tokens,
            )
        ]
    if kind == "simple_edit":
        return [
            RouteNode(
                "implement",
                "make the small, explicitly targeted edit",
                "implement",
                "economy",
                ("implement", "edit"),
                (),
                max(1, cfg.implement_input_tokens // 2),
                max(1, cfg.implement_output_tokens // 2),
            ),
            RouteNode(
                "verify",
                "run the focused acceptance check and inspect the diff",
                "verify",
                "economy",
                ("verify", "test"),
                ("implement",),
                cfg.review_input_tokens,
                cfg.review_output_tokens,
            ),
        ]
    if kind == "bounded_feature":
        return [
            RouteNode(
                "explore",
                "inspect only the named target and its focused test surface",
                "explore",
                "economy",
                ("search", "explore"),
                (),
                12000,
                2000,
            ),
            RouteNode(
                "implement",
                "implement the explicit behavioral contract and focused tests",
                "implement",
                "standard",
                # `review` makes the live-proven Claude/Sonnet arm outrank the
                # Codex/Terra arm that explicitly reported a read-only block.
                ("implement", "edit", "code", "review"),
                ("explore",),
                32000,
                6000,
            ),
            RouteNode(
                "verify",
                "run the named acceptance tests and inspect the diff",
                "verify",
                "economy",
                ("verify", "test"),
                ("implement",),
                16000,
                2000,
            ),
        ]
    return None


def fallback_route(task: str, hosts: list[DetectedHost], cfg) -> RoutePlan:
    """Deterministic model-routed DAG for when no coordinator can run.

    High-confidence answer, inspect, review, test, and explicitly small-edit
    tasks take a one- or two-node completion-gated fast path. Ambiguous work
    retains the complete explore (economy) -> plan (frontier, prefer STRONG) ->
    implement -> verify (economy) route. Planning takes the frontier *flagship*
    (Opus when Claude is installed), because a good plan is worth the strong
    model. Implementation is complexity-adaptive via
    ``[orchestrate] implement_tier``. Same handoff/pricing as a coordinator
    plan."""
    fast_nodes = _fast_fallback_nodes(task, cfg)
    unattended = [host for host in hosts if host.spec.unattended]
    coordinator_hosts = unattended
    if fast_nodes is not None:
        assigned = _apply_verification_policy(
            task, [_assign_host(node, hosts) for node in fast_nodes], hosts
        )
        return RoutePlan(
            task=task,
            hosts=tuple(hosts),
            coordinator=pick_coordinator(coordinator_hosts),
            assigned=assigned,
            source="deterministic_fast",
            task_kind=_fallback_task_kind(task),
        )

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
    assigned = _apply_verification_policy(
        task, [_assign_host(n, hosts) for n in nodes], hosts
    )
    return RoutePlan(
        task=task, hosts=tuple(hosts),
        coordinator=pick_coordinator(coordinator_hosts), assigned=assigned,
        source="deterministic_fallback", task_kind="general",
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
    max_turns: int = 0,
    idle_timeout: float = 0.0,
    edit_attempt: str = "",
) -> tuple[int, str, str, ActualUsage | None]:
    """Run one harness in print mode with captured output, inside the harnessed
    workspace. Claude gets the ephemeral --settings hook injection; Codex /
    Antigravity discover their hooks from the workspace tree. ``model`` pins the
    model via the host's model flag (used for the cheap coordinator).
    ``max_turns`` > 0 hard-bounds a Claude node (``--max-turns``); other hosts
    expose no equivalent and are bounded by observation only. ``idle_timeout``
    > 0 kills a node that emits nothing for that long (``NodeStalled``), and
    switches Claude to ``stream-json`` so its per-event lines are the beacon;
    Codex ``exec --json`` already streams. Never raises."""
    spec = host.spec
    path = host.path or spec.cli_bins[0]
    argv = [path, *spec.print_flag, prompt]
    if model and spec.model_flag:
        if spec.name == "antigravity":
            # Current agy exposes a base Gemini id plus a mandatory effort
            # flag. Keep the internal canonical/pricing ids stable while
            # adapting them at the vendor CLI boundary.
            launch_model = model.removesuffix("-preview").removesuffix("-lite")
            effort = "high" if "pro" in launch_model else (
                "medium" if "3.6" in launch_model else "low"
            )
            argv = [
                path,
                spec.model_flag,
                launch_model,
                "--effort",
                effort,
                *spec.print_flag,
                prompt,
            ]
        else:
            argv = [path, spec.model_flag, model, *spec.print_flag, prompt]
    settings_tmp: str | None = None
    try:
        if spec.name == "claude":
            from ctx.installer import claude_hook_settings
            from ctx.wrap import _SINGLE_SHOT_NOTICE

            tmp = tempfile.NamedTemporaryFile(
                "w", prefix="ctx-orch-", suffix=".json", delete=False, encoding="utf-8"
            )
            json.dump(claude_hook_settings(exe), tmp)
            tmp.close()
            settings_tmp = tmp.name
            head = [path, "--settings", settings_tmp]
            # A node is itself a print-mode run (round 17): if it fans out to
            # background subagents it must not end its turn to "wait" for
            # them. Same opt-out as the wrap.py path.
            if not os.environ.get("CTX_WRAP_NO_DISCIPLINE"):
                head += ["--append-system-prompt", _SINGLE_SHOT_NOTICE]
            if max_turns > 0:
                head += ["--max-turns", str(int(max_turns))]
            # ``json`` arrives in one piece at the very end, which is no
            # beacon at all; ``stream-json`` (print mode requires --verbose
            # with it) emits a line per assistant/tool event and ends with
            # the same result document. Only when the idle bound is on, so
            # the default launch stays byte-identical.
            structured = (["--output-format", "stream-json", "--verbose"]
                          if idle_timeout > 0 else ["--output-format", "json"])
            argv = ([*head, spec.model_flag, model, *structured, *spec.print_flag, prompt]
                    if model and spec.model_flag
                    else [*head, *structured, *spec.print_flag, prompt])
        elif spec.name == "codex":
            prefix = [path, spec.model_flag, model] if model and spec.model_flag else [path]
            argv = [*prefix, "exec", "--json", prompt]
        elif spec.name == "antigravity-sdk":
            prefix = [path, spec.model_flag, model] if model and spec.model_flag else [path]
            argv = [*prefix, "--json", *spec.print_flag, prompt]
        # Name the model and host for the hooks the launched process will
        # run: the edit-outcome ledger splits by model, and a PostToolUse
        # payload carries no model of its own.
        env = {**os.environ, "CTX_MODEL": model or spec.default_model or "",
               "CTX_HOST": spec.name, "CTX_EDIT_ATTEMPT": edit_attempt}
        if spec.name == "claude":
            # Print mode kills background subagents 600 s after the main
            # turn ends; a node that fans out and then waits would lose its
            # work on that timer (round 17). The per-node ``timeout`` above
            # is the bound; the user's own setting wins.
            env.setdefault("CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS", "0")
        proc = (_run_bounded(argv, cwd=ws_root, timeout=timeout, env=env, idle_timeout=idle_timeout)
                if idle_timeout > 0
                else _run_bounded(argv, cwd=ws_root, timeout=timeout, env=env))
        stdout, usage = parse_host_output(
            spec.name,
            proc.stdout or "",
            model=model or spec.default_model,
            workspace_root=ws_root,
        )
        return proc.returncode, stdout, proc.stderr or "", usage
    except (OSError, subprocess.SubprocessError) as e:
        return 127, "", f"{type(e).__name__}: {e}", None
    finally:
        if settings_tmp:
            with contextlib.suppress(OSError):
                os.unlink(settings_tmp)


class NodeStalled(subprocess.TimeoutExpired):
    """A node killed for silence: no byte on stdout or stderr for
    ``idle_timeout`` seconds while the wall clock still had room. A subclass
    of ``TimeoutExpired`` so every existing ``except SubprocessError`` path
    handles it; its own name so the steward can tell it from the wall
    bound (``stalled`` vs ``wall_timeout``)."""

    def __init__(self, cmd, idle: float, *, elapsed: float, wall: float, output=None, stderr=None):
        super().__init__(cmd, idle, output=output, stderr=stderr)
        self.idle = float(idle)
        self.elapsed = float(elapsed)
        self.wall = float(wall)

    def __str__(self) -> str:
        return (f"no output for {self.idle:.0f}s "
                f"(stalled at {self.elapsed:.0f}s of a {self.wall:.0f}s wall clock)")


_IDLE_POLL_S = 0.25


def _run_bounded(argv, *, cwd, timeout, env, idle_timeout: float = 0.0) -> subprocess.CompletedProcess:
    """``subprocess.run`` with a process group, so a timeout kills what the
    host forked, not just the host.

    ``subprocess.run(timeout=...)`` kills the direct child only. A harness
    CLI that forked a sandboxed test run or a background subagent past
    ``node_timeout`` left that grandchild running, invisible and possibly
    still writing into the workspace after the orchestrator had moved on.
    Same pattern as ``ctx._proc.wait_or_kill``: the child leads its own
    session, and on timeout the whole group gets SIGKILL before the
    ``TimeoutExpired`` propagates to the caller's existing handling.

    ``idle_timeout`` > 0 adds headlong's inactivity bound beside the wall
    clock: every byte the host writes on either stream is a beacon, and a
    node silent for that long is killed the same way and raised as
    :class:`NodeStalled`. The wall bound still applies to a node that keeps
    talking. With ``idle_timeout`` off the call is the plain ``communicate``
    it always was.
    """
    if idle_timeout <= 0:
        proc = subprocess.Popen(
            argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=env, start_new_session=True,
        )
        try:
            out, err = proc.communicate(timeout=timeout)
        except BaseException:
            from ctx._proc import kill_and_reap

            kill_and_reap(proc)
            raise
        finally:
            proc.stdout.close()
            proc.stderr.close()
        return subprocess.CompletedProcess(argv, proc.returncode, out, err)

    from ctx._proc import kill_and_reap

    proc = subprocess.Popen(
        argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, env=env, start_new_session=True,
    )
    started = time.monotonic()
    last_activity = [started]
    chunks: dict[str, list[bytes]] = {"out": [], "err": []}

    def _pump(stream, key: str) -> None:
        # Raw reads, not lines: a host that prints a progress character
        # without a newline is still alive.
        fd = stream.fileno()
        while True:
            try:
                data = os.read(fd, 65536)
            except OSError:
                break
            if not data:
                break
            chunks[key].append(data)
            last_activity[0] = time.monotonic()

    pumps = [threading.Thread(target=_pump, args=(proc.stdout, "out"), daemon=True),
             threading.Thread(target=_pump, args=(proc.stderr, "err"), daemon=True)]
    def _text(key: str) -> str:
        return b"".join(chunks[key]).decode("utf-8", errors="replace")

    try:
        for t in pumps:
            t.start()
        while True:
            try:
                proc.wait(timeout=_IDLE_POLL_S)
                break
            except subprocess.TimeoutExpired:
                pass
            now = time.monotonic()
            if timeout is not None and now - started >= timeout:
                raise subprocess.TimeoutExpired(argv, timeout)
            if now - last_activity[0] >= idle_timeout:
                raise NodeStalled(argv, idle_timeout, elapsed=now - started,
                                  wall=(float(timeout) if timeout is not None else 0.0))
    except BaseException as exc:
        # Cancellation owns the same cleanup as wall/idle timeout. A host
        # launched in another session does not receive our terminal's SIGINT.
        kill_and_reap(proc)
        for t in pumps:
            if t.ident is not None:
                t.join(timeout=5.0)
        if isinstance(exc, subprocess.TimeoutExpired):
            # Read after cleanup/drain so the failure receipt includes the
            # final bytes already written by the child.
            exc.output, exc.stderr = _text("out"), _text("err")
        raise
    finally:
        for t, stream in zip(pumps, (proc.stdout, proc.stderr)):
            if t.ident is not None:
                t.join(timeout=5.0)
            # An escaped descendant may still own a pipe. Do not block in
            # close while its reader holds the stream; this is not a sandbox.
            if not t.is_alive():
                stream.close()
    return subprocess.CompletedProcess(argv, proc.returncode, _text("out"), _text("err"))


def _launch_result(value) -> tuple[int, str, str, ActualUsage | None]:
    """Normalise launch adapters while retaining the original 3-tuple API."""
    try:
        if len(value) == 3:
            code, stdout, stderr = value
            return int(code), str(stdout or ""), str(stderr or ""), None
        if len(value) == 4:
            code, stdout, stderr, usage = value
            return (
                int(code),
                str(stdout or ""),
                str(stderr or ""),
                coerce_usage(usage),
            )
    except (TypeError, ValueError):
        pass
    raise ValueError("launch adapter must return (code, stdout, stderr[, usage])")


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
    usage_sink: list[ActualUsage | None] | None = None,
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
    usage_index: int | None = None
    if usage_sink is not None:
        usage_index = len(usage_sink)
        usage_sink.append(None)
    try:
        code, out, _err, usage = _launch_result(
            launch(
                coord,
                ws.root,
                prompt,
                exe,
                timeout=timeout,
                model=coord.spec.coord_model,
            )
        )
        if usage_sink is not None and usage_index is not None:
            usage_sink[usage_index] = usage
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
    usage_attempts: tuple[ActualUsage | None, ...] = ()
    handoff_policy: str = ""
    isolation: str = "shared_workspace"
    changed_paths: tuple[str, ...] = ()
    merge_status: str = "not_applicable"
    output_schema_status: str = "not_requested"
    attempts: int = 1
    reason: str = "done"            # last handback reason (taskledger)
    failure_kind: str = "none"      # last handback failure_kind
    steward_action: str | None = None  # what the steward decided, if consulted


@dataclass
class _NodeExecution:
    outcome: NodeOutcome
    stdout: str
    stderr: str
    patch: WorktreePatch | None = None


@dataclass
class RouteResult:
    plan: RoutePlan
    outcomes: list[NodeOutcome]
    waves_run: int
    replans: int
    estimated_spend_usd: float = 0.0
    duration_ms: float = 0.0
    actual_usage: dict = field(default_factory=lambda: summarize_usage([]))
    wave_policies: tuple[str, ...] = ()
    task_id: str = ""
    resumed_nodes: tuple[str, ...] = ()
    ledger_spend_usd: float = 0.0
    ledger_spend_complete: bool = False


def _task_profile(task: str, kind: str) -> dict[str, object]:
    """Privacy-safe structural task profile; raw task text never enters telemetry."""
    return {
        "kind": kind,
        "high_confidence": kind != "general",
        "mutation": kind == "simple_edit",
        "review": kind == "review",
        "verification_required": kind == "simple_edit",
        "characters": len(task),
        "words": len(task.split()),
        "multiline": "\n" in task,
        "named_target": bool(_NAMED_TARGET.search(task)),
        "named_acceptance": bool(_NAMED_ACCEPTANCE.search(task)),
        "high_risk_scope": bool(_HIGH_RISK_SCOPE.search(task)),
        "explicit_contract": bool(_EXPLICIT_CONTRACT.search(task)),
    }


def _append_route_receipt(ws, result: RouteResult, nodes: list[AssignedNode]) -> None:
    """Append one fail-open, privacy-safe route execution receipt."""
    try:
        from ctx.sessiondir import session_reads_path

        node_by_id = {assigned.node.id: assigned.node for assigned in nodes}
        verification_required = any(
            node.role == "implement" or "edit" in node.need_tags
            for node in node_by_id.values()
        )
        verification_passed = any(
            node_by_id.get(outcome.node_id)
            and (
                node_by_id[outcome.node_id].role in {"verify", "test"}
                or bool(
                    {"verify", "test"}
                    & set(node_by_id[outcome.node_id].need_tags)
                )
            )
            and outcome.status == "ok"
            for outcome in result.outcomes
        )
        route_completed = bool(result.outcomes) and all(
            outcome.status == "ok" for outcome in result.outcomes
        )
        task_profile = _task_profile(result.plan.task, result.plan.task_kind)
        task_profile["mutation"] = verification_required
        task_profile["verification_required"] = verification_required
        receipt = {
            "schema": ROUTE_RUN_SCHEMA,
            "run_id": f"route-{time.time_ns():x}",
            "recorded_at_unix": time.time(),
            "task_profile": task_profile,
            "route": {
                "source": result.plan.source,
                "nodes": [
                    {
                        "id": assigned.node.id,
                        "role": assigned.node.role,
                        "min_tier": assigned.node.min_tier,
                        "need_tags": list(assigned.node.need_tags),
                        "deps": list(assigned.node.deps),
                        "host": assigned.host.name,
                        "model": assigned.model.id,
                        "verification_policy": assigned.verification_policy or None,
                        "estimated_input_tokens": assigned.node.est_input_tokens,
                        "estimated_output_tokens": assigned.node.est_output_tokens,
                        "estimated_cost_usd": assigned.est_cost_usd,
                    }
                    for assigned in nodes
                ],
            },
            "orchestration_policy": {
                "wave_policies": list(result.wave_policies),
                "shared_workspace_mutations_serialized": all(
                    outcome.isolation != "git_worktree" for outcome in result.outcomes
                ),
                "isolated_worktrees_used": any(
                    outcome.isolation == "git_worktree" for outcome in result.outcomes
                ),
            },
            "outcomes": [
                {
                    "node_id": outcome.node_id,
                    "status": outcome.status,
                    "host_model": outcome.host_name,
                    "handoff_policy": outcome.handoff_policy or None,
                    "exit_code": outcome.exit_code,
                    "escalated": outcome.escalated_to is not None,
                    "checkpoint": outcome.checkpoint_ref is not None,
                    "isolation": outcome.isolation,
                    "changed_paths": list(outcome.changed_paths),
                    "merge_status": outcome.merge_status,
                    "output_schema_status": outcome.output_schema_status,
                    "actual_usage": summarize_usage(outcome.usage_attempts),
                    "attempts": outcome.attempts,
                    "reason": outcome.reason,
                    "failure_kind": outcome.failure_kind,
                    "steward_action": outcome.steward_action,
                }
                for outcome in result.outcomes
            ],
            "task_id": result.task_id,
            "resumed_nodes": list(result.resumed_nodes),
            "measurement": {
                "route_completed": route_completed,
                "task_success": "unmeasured",
                "verification_required": verification_required,
                "verification_passed": verification_passed,
                "waves": result.waves_run,
                "replans": result.replans,
                "duration_ms": result.duration_ms,
                "estimated_spend_usd": result.estimated_spend_usd,
                "actual_usage": result.actual_usage,
                "ledger_spend_usd": result.ledger_spend_usd,
                "ledger_spend_complete": result.ledger_spend_complete,
            },
        }
        path = session_reads_path(ws.root, "route.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(receipt, sort_keys=True) + "\n")
    except Exception:
        pass


_HANDOFF_LIMITS = {
    "address_only": 0,
    "compact": 600,
    "standard": 1200,
    "expanded": 2400,
}


def _bounded_handoff_state(node: RouteNode, text: str, strategy: str) -> str:
    """Render deterministic head/tail evidence while the blob keeps every byte."""
    limit = _HANDOFF_LIMITS.get(strategy, _HANDOFF_LIMITS["compact"])
    if limit <= 0:
        return f"node {node.id}: full output stored at the checkpoint evidence address"
    cleaned = text.strip()
    if not cleaned:
        return f"node {node.id}: no output"
    if len(cleaned) <= limit:
        return cleaned
    marker = "\n... [bounded handoff; resolve blob: evidence for omitted bytes] ...\n"
    available = max(2, limit - len(marker))
    head = max(1, available * 2 // 3)
    tail = max(1, available - head)
    return cleaned[:head] + marker + cleaned[len(cleaned) - tail :]


def _checkpoint_node(
    ws,
    node: RouteNode,
    task: str,
    stdout: str,
    stderr: str,
    *,
    handoff_strategy: str | None = None,
) -> str | None:
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
        text = stdout or stderr or ""
        strategy = handoff_strategy or choose_handoff(
            {
                "failed": bool(stderr and not stdout),
                "mutation": _is_mutation_node(node),
                "verification": _is_verification_node(node),
                "has_dependents": True,
                "output_bytes": len(payload),
            }
        )
        state = _bounded_handoff_state(node, text, strategy)
        # Serialize the store mutation across parallel-wave threads (see the
        # module comment on _CHECKPOINT_LOCK). Own Store per call so the sqlite
        # connection is never shared across threads.
        with _CHECKPOINT_LOCK:
            store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
            try:  # ensure the sqlite connection is closed even if create_checkpoint raises
                blob_id = store.put_blob(payload) if payload else None
                evidence = [f"blob:{blob_id} node {node.id} full output"] if blob_id else []
                cp_id, _ = create_checkpoint(
                    store, ws,
                    goal=f"node {node.id} ({node.role}) of orchestrated task: {task}",
                    state=state,
                    evidence=evidence,
                )
            finally:
                store.close()
        return f"checkpoint:{short_id(cp_id)}"
    except Exception:
        return None


#: A request to inspect evidence, never proof that an edit happened.
PREWALK_SENTINEL = "CTX_PREWALK_HANDOFF"


def _node_prompt(
    node: RouteNode,
    task: str,
    dep_docs: list[str],
    inbox: list[dict] | None = None,
    *,
    continuation_doc: str = "",
    prewalk_hint: bool = False,
) -> str:
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
    if node.targets:
        p += "\n\nYou may mutate only these declared targets: " + ", ".join(node.targets)
    if node.output_schema is not None:
        p += (
            "\n\nYour final yield must be one JSON value matching this schema. "
            "Do not put the JSON in a markdown fence:\n"
            + json.dumps(node.output_schema, sort_keys=True, separators=(",", ":"))
        )
    if inbox:
        p += "\n\nMessages addressed to you (addresses, resolve with `ctx get`):\n"
        p += "\n".join(
            f"  from {m.get('from')}: {m.get('ref')}"
            + (f" — {m['note']}" if m.get("note") else "")
            for m in inbox
        )
    if continuation_doc:
        p += (
            "\n\nA prior attempt on this same subtask (a stronger model) already "
            "made an edit with a passing behavioral check. Continue directly from there. "
            "This is checkpoint continuation, not a restored native session. "
            "Use the checklist and cited evidence; re-read when freshness or missing "
            "context requires it. Its record:\n" + continuation_doc
        )
    if prewalk_hint:
        p += (
            "\n\nPrewalk: plan, make one meaningful edit using ctx edit replace/apply, "
            "then run ctx edit verify with an explicit behavioral check. "
            "Build a JSON state file with checklist (1..12 objects, each with id, task, "
            "validation, status=done|pending), hypotheses, ruledOut, and evidence arrays. "
            "Keep at least one done and one pending item. Request handoff with "
            "ctx edit handoff --verification <blob:proof> --state <file>. "
            "Return its two signal lines exactly. A printed marker alone is not proof. "
            "If the whole task is done, report completion instead."
        )
    return p


def _worker_yield_status(node: RouteNode, stdout: str) -> tuple[str, str]:
    if node.output_schema is None:
        return "not_requested", ""
    value = _extract_json(stdout)
    if value is None:
        return "invalid", "worker output did not contain a JSON value"
    errors = validate_worker_yield(value, node.output_schema)
    if errors:
        return "invalid", "; ".join(errors[:4])
    return "valid", ""


def _execution_contract_failed(code: int, stdout: str, stderr: str) -> bool:
    """Detect a host that exited zero without completing a one-shot contract."""
    if code != 0:
        return True
    combined = "\n".join((stdout, stderr)).strip().lower()
    if not combined:
        return True
    if "no output produced" in combined and "auto-denied" in combined:
        return True
    plain = combined.replace("*", "")
    return any(
        marker in plain
        for marker in (
            "task is not complete",
            "verification result: implementation incomplete",
            "blocked by the read-only workspace",
            "no source or test files could be modified",
            "unable to make changes due to workspace constraints",
        )
    )


def _escalate(
    cur_model: ModelChoice, hosts: list[DetectedHost]
) -> tuple[DetectedHost, ModelChoice] | None:
    """Kept for callers of the old name; the target selection now lives in the
    steward (``ctx.steward.escalation_target``) so the recovery policy and the
    orchestrator agree about what "one tier up" means."""
    return _steward.escalation_target(cur_model, hosts)


def _actual_cost(assigned, outcome) -> float:
    """Conservative estimate for every model attempt made by this node.

    Derived state must not outlive its source: an escalation changes the
    model, so it changes the price.  The failed original attempt was still
    billed, so escalation adds its estimate rather than replacing it.
    """
    est = float(getattr(assigned, "est_cost_usd", 0.0) or 0.0)
    # `escalated_to` is host-qualified ("antigravity/gemini-3.6-flash"); the
    # planned model is assigned.model (NOT assigned.host.model, which does not
    # exist -- the first cut read it, got None, and silently charged the cheap
    # estimate exactly as before).
    ran = str(getattr(outcome, "escalated_to", "") or "").rsplit("/", 1)[-1]
    planned = str(getattr(getattr(assigned, "model", None), "id", "") or "")
    if not ran or ran == planned:
        return est
    try:
        from ctx.pricing import price_for

        old_p, new_p = price_for(planned), price_for(ran)
        old_rate = float(getattr(old_p, "output", 0.0) or 0.0)
        new_rate = float(getattr(new_p, "output", 0.0) or 0.0)
        if old_rate > 0 and new_rate > 0 and new_rate > old_rate:
            return est + est * (new_rate / old_rate)
    except Exception:
        pass
    # The escalation HAPPENED -- we are here only because a different model
    # ran -- but pricing could not tell the two apart (both unknown, or both
    # on the vendor-neutral fallback). Charging the original estimate would
    # say the escalation was free. A bound that guesses low is the direction
    # that overruns, which is the defect being fixed, so guess high.
    return est * 3


def _select_wave(
    ready: list[AssignedNode],
    max_workers: int,
    *,
    final_wave: bool = False,
    isolated_worktrees: bool = False,
    repository_clean: bool = False,
) -> tuple[list[AssignedNode], int, str]:
    """Apply evolved wave and mutation policies to one topological frontier."""
    mutations = [item for item in ready if _is_mutation_node(item.node)]
    readonly = [item for item in ready if not _is_mutation_node(item.node)]
    declared = bool(mutations) and all(item.node.targets for item in mutations)
    overlap = True
    if declared:
        try:
            overlap = targets_overlap([item.node.targets for item in mutations])
        except WorktreeIsolationError:
            overlap = True
    isolation = choose_mutation_isolation(
        {
            "mutation_count": len(mutations),
            "shared_workspace": True,
            "isolated_worktrees": isolated_worktrees and repository_clean,
            "targets_declared": declared,
            "target_overlap": overlap,
        }
    )
    action = choose_wave(
        {
            "ready_count": len(ready),
            "mutation_count": len(mutations),
            "readonly_count": len(readonly),
            # ``max_workers=1`` is the caller's bounded back-pressure signal;
            # do not classify that frontier as parallel even though the
            # executor cap would ultimately serialize it.
            "provider_rate_limited": max_workers <= 1,
            "isolation": isolation,
        }
    )
    if mutations and isolation == "serial_workspace":
        if final_wave:
            return ready, 1, f"mutation_serial/{isolation}"
        if action == "readonly_first" and readonly:
            workers = min(max_workers, 4, len(readonly))
            return readonly, max(1, workers), f"{action}/{isolation}"
        # Keep the whole logical frontier in one configured wave, but execute
        # its nodes with one worker so multiple writers never overlap.
        return ready, 1, f"mutation_serial/{isolation}"
    if action == "serial":
        return ready, 1, f"{action}/{isolation}"
    cap = 2 if action == "parallel_two" else 4
    workers = min(max_workers, cap, len(ready))
    return ready, max(1, workers), f"{action}/{isolation}"


def run_route(
    ws,
    plan: RoutePlan,
    cfg,
    *,
    exe: str | None = None,
    launch=_launch_host,
    coordinate=None,
    max_workers: int = 4,
    prior_usage_attempts: list[ActualUsage | None] | None = None,
    task_id: str | None = None,
    resume: bool = False,
) -> RouteResult:
    """Coordinate the DAG closed-loop over the task ledger.

    Ready nodes (deps satisfied) run in parallel; each sees its deps'
    checkpoints and any inbox addresses sent to it. Every launch is preceded by
    a ``ctx.claim/v1`` row and followed by a ``ctx.handback/v1`` row; a node
    that does not finish is handed to the steward, whose decision is recorded
    as a ``ctx.steward/v1`` row before it is acted on (retry, escalate, leave
    for re-plan, or stop). Disjoint mutation waves may run in isolated
    worktrees and merge as one transaction; a typed worker yield is validated
    and, when strict, treated as a contract failure. Between waves the
    coordinator may patch the plan with follow-up nodes. Budget is checked
    against ledger ACTUALS, falling back to the estimate only for attempts no
    host priced, and a claim the ledger cannot cover is refused before launch.

    ``resume=True`` replays the ledger for ``task_id`` first: nodes with a
    ``done`` handback are restored (status, checkpoint, handoff document) and
    not re-run; everything else runs as pending. Bounded by cfg.max_waves /
    max_replans / budget_usd / max_attempts. Every step is fail-open.
    """
    from ctx.installer import _ctx_executable
    from ctx.store import Store

    started = time.monotonic()
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
    idle_timeout = max(0.0, float(getattr(cfg, "idle_timeout", 0.0) or 0.0))
    max_attempts = max(1, int(getattr(cfg, "max_attempts", 2) or 2))
    expected_turns = max(0, int(getattr(cfg, "expected_turns", 12) or 0))
    turn_ceiling = max(0, int(getattr(cfg, "turn_ceiling", 0) or 0))
    spent_est = 0.0
    waves_run = 0
    wave_policies: list[str] = []
    replans_done = 0
    usage_attempts = (
        prior_usage_attempts if prior_usage_attempts is not None else []
    )
    isolated_worktrees = bool(getattr(cfg, "isolated_worktrees", False))
    strict_worker_yields = bool(getattr(cfg, "strict_worker_yields", False))
    isolated_node_ids: set[str] = set()

    # ------------------------------------------------ the ledger: open/resume
    tid = task_id or _ledger.new_task_id()
    resumed: list[str] = []
    if resume:
        prior = _ledger.task_state(_ledger.load(ws.root, tid))
        for a in nodes:
            n = prior.nodes.get(a.node.id)
            if n is None or not n.done:
                continue  # claimed-but-not-handed-back re-runs: the safe direction
            hb = n.last_handback or {}
            state[a.node.id] = "ok"
            resumed.append(a.node.id)
            outcomes[a.node.id] = NodeOutcome(
                a.node.id, f"{hb.get('host')}/{hb.get('model')}", "ok", n.checkpoint,
                "resumed from ledger", attempts=n.attempts,
            )
            if n.checkpoint:
                cp[a.node.id] = n.checkpoint
                with contextlib.suppress(Exception):
                    from ctx.checkpoint import show_checkpoint

                    docs[a.node.id] = show_checkpoint(store, ws, n.checkpoint)
    else:
        _open_task(ws, store, tid, plan, nodes, budget)

    def _append_unlocked(row: dict) -> None:
        # Fail-open: the ledger is bookkeeping for a run that must finish. A
        # dropped claim or handback reads on resume as "not done", which
        # re-runs the node -- the direction that costs money, never truth.
        with contextlib.suppress(Exception):
            _ledger.append(ws.root, row)

    def _record(row: dict) -> None:
        with _LEDGER_LOCK:
            _append_unlocked(row)

    def _state_unlocked():
        return _ledger.task_state(_ledger.load(ws.root, tid))

    def _ledger_state():
        with _LEDGER_LOCK:
            return _state_unlocked()

    def _remaining_from(st) -> float:
        if budget <= 0:
            return float("inf")
        # Actuals when every attempt was priced; otherwise the larger of what
        # was measured and what was estimated, so an unpriced attempt cannot
        # make the budget look healthier than the estimate already said. Open
        # claims reserve their estimate: nodes launched in parallel each see
        # the others' reservations, so one wave cannot spend the same dollar
        # twice.
        actual = st.spent_usd if st.cost_complete else max(st.spent_usd, spent_est)
        return budget - actual - st.reserved_usd

    def _remaining_budget() -> float:
        return _remaining_from(_ledger_state())

    def _claim_or_refuse(a: AssignedNode, attempt: int, expected_turns: int) -> bool:
        """Check the budget and write the claim as ONE step under the ledger
        lock, so two nodes of the same wave cannot both pass a check against
        the same remaining balance. Returns False (with the refusal on record
        as a steward decision) when the node's own estimate would overspend."""
        with _LEDGER_LOCK:
            st = _state_unlocked()
            # A claim this node left open in a run that died is superseded by
            # the one being written now; it must not count against itself.
            own = st.nodes[a.node.id].reserved_usd if a.node.id in st.nodes else 0.0
            remaining = _remaining_from(st) + own
            if budget > 0 and float(a.est_cost_usd) > remaining:
                _append_unlocked(_ledger.steward_row(
                    tid, a.node.id, attempt=attempt - 1, on_reason="over_budget",
                    failure_kind="none", action="stop_budget", target=None,
                    budget_remaining_usd=remaining,
                ))
                return False
            _append_unlocked(_ledger.claim_row(
                tid, a.node.id, attempt=attempt, host=a.host.name, model=a.model.id,
                tier=a.model.tier, expected_turns=expected_turns,
                expected_cost_usd=a.est_cost_usd,
            ))
            return True

    def _do_launch(host, work_root, prompt: str, model, edit_attempt: str = ""):
        # The extra bounds are keyword-only on the real launcher; an injected
        # launcher (tests, evals) keeps the historical signature.
        extra: dict = {}
        if launch is _launch_host:
            if edit_attempt:
                extra["edit_attempt"] = edit_attempt
            if turn_ceiling > 0:
                extra["max_turns"] = turn_ceiling
            if idle_timeout > 0:
                extra["idle_timeout"] = idle_timeout
        return launch(host, work_root, prompt, resolved_exe, timeout=timeout,
                      model=model.launch_id, **extra)

    def run_one(a: AssignedNode) -> _NodeExecution:
        dep_docs = [docs[d] for d in a.node.deps if d in docs]
        st = _ledger_state()
        inbox = _ledger.inbox_for(st, a.node.id)
        host, model = a.host, a.model
        # Opt-in (cfg.prewalk): a frontier model on a mutation node is asked
        # to plan, make one edit, then hand off -- never offered to a node
        # already routed cheap, since there is nothing to save by handing off
        # from economy to economy.
        # Armed only when the handoff it asks for can happen: a second attempt
        # must be allowed (the cheap model runs as the SAME node's next
        # attempt) and a cheaper unattended model must be installed. Asking a
        # compliant frontier worker to stop after one edit with nobody to hand
        # to turned a task it could have finished into a guaranteed
        # stop_blocked (Codex review, PR #33).
        prewalk_enabled = (
            bool(getattr(cfg, "prewalk", False))
            and _is_mutation_node(a.node) and model.tier == "frontier"
            and (st.nodes[a.node.id].attempts if a.node.id in st.nodes else 0) + 1 < max_attempts
            and _steward.de_escalation_target(model, hosts) is not None
        )
        prewalk_policy_note = ""
        if prewalk_enabled and getattr(cfg, "prewalk_policy_file", ""):
            from ctx.edit_policy import choose_prewalk, load_rows
            target = _steward.de_escalation_target(model, hosts)
            try:
                decision = choose_prewalk(
                    load_rows(ws.confine(cfg.prewalk_policy_file, must_exist=True)),
                    guide_model=model.id, executor_model=target[1].id, shape=a.node.edit_shape)
                prewalk_enabled = decision["strategy"] == "prewalk"
                prewalk_policy_note = (f"\nPrewalk strategy: {decision['strategy']}; {decision['reason']}; "
                                       f"evidence {decision['evidenceSha256']}.")
            except (OSError, ValueError):
                prewalk_enabled = False
                prewalk_policy_note = "\nPrewalk policy unavailable; finish on the assigned model."
        prompt = _node_prompt(a.node, plan.task, dep_docs, inbox, prewalk_hint=prewalk_enabled)
        prompt += prewalk_policy_note
        node_usage: list[ActualUsage | None] = []
        use_isolation = a.node.id in isolated_node_ids
        checkout = IsolatedWorktree(Path(ws.root), a.node.id, a.node.targets) if use_isolation else None
        work_root = Path(ws.root)
        patch: WorktreePatch | None = None
        attempt = st.nodes[a.node.id].attempts if a.node.id in st.nodes else 0
        escalated: str | None = None
        last_action: str | None = None
        cls = _steward.Classification("failed", "unknown")
        handoff_strategy = ""
        ref: str | None = None
        schema_status = "not_requested"
        code, out, err = 1, "", ""
        has_dependents = any(a.node.id in item.node.deps for item in nodes)

        def _outcome() -> NodeOutcome:
            tail = (out or err or "").strip().splitlines()
            return NodeOutcome(
                node_id=a.node.id, host_name=f"{host.name}/{model.id}",
                status="ok" if cls.reason == "done" else "failed", checkpoint_ref=ref,
                detail=(tail[-1][:200] if tail else "(no output)"), escalated_to=escalated,
                exit_code=code, usage_attempts=tuple(node_usage), handoff_policy=handoff_strategy,
                isolation="git_worktree" if use_isolation else "shared_workspace",
                changed_paths=patch.changed_paths if patch else (),
                merge_status="pending" if patch is not None else (
                    "no_changes" if use_isolation and cls.reason == "done" else "not_applicable"
                ),
                output_schema_status=schema_status,
                attempts=attempt, reason=cls.reason, failure_kind=cls.failure_kind,
                steward_action=last_action,
            )

        # Refuse the claim before the launch when the ledger cannot cover it.
        # The per-wave check below catches a budget already blown; this one
        # stops a node whose own estimate would blow it, so the loop never
        # STARTS work it knows it cannot pay for. The check and the first
        # claim are one atomic step: the claim reserves the estimate, so a
        # sibling launched in parallel sees it. Recorded as a steward decision
        # with no attempt, which is exactly what happened.
        first_attempt = attempt + 1
        if not _claim_or_refuse(a, first_attempt, expected_turns):
            cls = _steward.Classification("over_budget", "none")
            last_action = "stop_budget"
            code, err = None, "not started: estimate exceeds remaining budget"
            return _NodeExecution(outcome=_outcome(), stdout="", stderr=err, patch=None)

        try:
            scope = checkout if checkout is not None else contextlib.nullcontext()
            with scope:
                if checkout is not None and checkout.path is not None:
                    work_root = checkout.path
                while True:
                    attempt += 1
                    if attempt != first_attempt:
                        # A retry the steward already priced against the
                        # budget (escalation cost vs remaining) in its menu.
                        _record(_ledger.claim_row(
                            tid, a.node.id, attempt=attempt, host=host.name, model=model.id,
                            tier=model.tier, expected_turns=expected_turns,
                            expected_cost_usd=a.est_cost_usd,
                        ))
                    attempt_key = f"{tid}/{a.node.id}/{attempt}"
                    if prewalk_enabled and attempt == first_attempt:
                        prompt += f"\nPrewalk attempt key: {attempt_key}"
                    attempt_prompt = prompt
                    if getattr(cfg, "edit_policy_file", "") and a.node.edit_shape:
                        from ctx.edit_policy import choose_format, format_hint, load_rows
                        try:
                            policy_rows = load_rows(ws.confine(cfg.edit_policy_file, must_exist=True))
                            decision = choose_format(policy_rows, model=model.id, shape=a.node.edit_shape)
                            attempt_prompt += "\n" + format_hint(decision)
                        except (OSError, ValueError) as exc:
                            attempt_prompt += f"\nEdit-format policy unavailable ({type(exc).__name__}); use native."
                    code, out, err, usage = _launch_result(_do_launch(
                        host, work_root, attempt_prompt, model,
                        edit_attempt=attempt_key if prewalk_enabled else ""))
                    node_usage.append(usage)
                    from ctx import prewalk as _prewalk
                    prewalk_requested = (prewalk_enabled and attempt == first_attempt
                                         and _prewalk.requested(out or ""))
                    prewalk_signaled = False
                    verified_continuation = ""
                    if prewalk_requested and code == 0:
                        from ctx.workspace import resolve_workspace
                        from ctx.store import Store as _ProofStore
                        proof_ws = resolve_workspace(str(work_root))
                        proof_store = _ProofStore(proof_ws.workspace_id)
                        try:
                            accepted = _prewalk.accept_handoff(
                                proof_ws, proof_store, out, attempt_key)
                            verified_continuation = accepted["text"]
                            prewalk_signaled = True
                        except Exception as exc:
                            err = f"prewalk evidence rejected: {type(exc).__name__}: {exc}"
                            code = 1
                        finally:
                            proof_store.close()
                    contract_failed = _execution_contract_failed(code, out, err)
                    schema_status, schema_error = _worker_yield_status(a.node, out)
                    strict_schema = a.node.strict_output_schema or strict_worker_yields
                    schema_failed = schema_status == "invalid" and strict_schema
                    if schema_failed:
                        err = f"typed worker yield invalid: {schema_error}"
                    turns = usage.turns if usage is not None else 0
                    cls = _steward.classify_failure(
                        code=code, stdout=out, stderr=err, turns=turns, attempt=attempt,
                        expected_turns=expected_turns,
                        contract_failed=contract_failed or schema_failed,
                    )
                    if prewalk_signaled and not schema_failed:
                        cls = _steward.Classification("prewalk_handoff", "none")
                    elif contract_failed or schema_failed:
                        code = code or 1
                    handoff_strategy = choose_handoff({
                        "failed": cls.reason != "done",
                        "mutation": _is_mutation_node(a.node),
                        "verification": _is_verification_node(a.node),
                        "has_dependents": has_dependents,
                        "output_bytes": len((out or err or "").encode("utf-8", "replace")),
                    })
                    # Every attempt's output is evidence and gets an address;
                    # the node's handoff is the last one.
                    ref = _checkpoint_node(
                        ws, a.node, plan.task, out, err, handoff_strategy=handoff_strategy,
                    )
                    _record(_ledger.handback_row(
                        tid, a.node.id, attempt=attempt, reason=cls.reason,
                        failure_kind=cls.failure_kind, checkpoint=ref, turns=turns,
                        cost_usd=(usage.cost_usd if usage is not None else None),
                        tokens=(usage.total_tokens if usage is not None else 0),
                        exit_code=code, host=host.name, model=model.id,
                    ))
                    if cls.reason == "done":
                        break
                    remaining = _remaining_budget()
                    if remaining <= 0:
                        cls = _steward.Classification("over_budget", cls.failure_kind)
                    if cls.reason == "prewalk_handoff":
                        # Not a failure recovery decision -- the attempt
                        # succeeded at its narrower goal by design, so this
                        # bypasses the recovery policy entirely rather than
                        # forcing choose_recovery to explain a non-failure.
                        target = (
                            _steward.de_escalation_target(model, hosts)
                            if attempt < max_attempts else None
                        )
                        action = "handoff_cheap" if target is not None else "stop_blocked"
                        last_action = action
                        _record(_ledger.steward_row(
                            tid, a.node.id, attempt=attempt, on_reason="prewalk_handoff",
                            failure_kind="none", action=action,
                            target=(f"{target[0].name}/{target[1].id}" if target else None),
                            budget_remaining_usd=remaining,
                        ))
                        if action == "handoff_cheap" and target is not None:
                            # The steward's menu prices an escalation against
                            # the balance; this branch bypasses the steward,
                            # so it prices the cheap attempt itself. Only
                            # `remaining <= 0` was checked above, and a
                            # frontier attempt that spent most of an explicit
                            # budget could hand off into an attempt the ledger
                            # knew it could not cover (Codex review, PR #33).
                            cheap_est = target[0].model_price(target[1].id).cost_usd(
                                input_tokens=a.node.est_input_tokens,
                                output_tokens=a.node.est_output_tokens,
                            )
                            if budget > 0 and cheap_est > remaining:
                                last_action = "stop_budget"
                                _record(_ledger.steward_row(
                                    tid, a.node.id, attempt=attempt, on_reason="over_budget",
                                    failure_kind="none", action="stop_budget", target=None,
                                    budget_remaining_usd=remaining,
                                ))
                                cls = _steward.Classification("over_budget", "none")
                                break
                            # The edit already landed and is real progress --
                            # unlike a failed attempt's retry, there is
                            # nothing here to discard, so (unlike escalate
                            # below) the worktree is NOT reset.
                            host, model = target
                            a = replace(a, host=host, model=model, est_cost_usd=cheap_est)
                            escalated = f"{target[0].name}/{target[1].id}"
                            continuation_doc = verified_continuation
                            if ref and not continuation_doc:
                                # A fresh Store, not the run_route-scoped one:
                                # run_one executes inside a wave's thread
                                # pool, and sqlite3 connections are
                                # check_same_thread (see _checkpoint_node's
                                # own docstring for the same reason).
                                with contextlib.suppress(Exception):
                                    from ctx.checkpoint import show_checkpoint
                                    from ctx.store import Store as _Store

                                    cp_store = _Store(
                                        ws.workspace_id,
                                        retention_days=ws.config.store.retention_days,
                                    )
                                    try:
                                        continuation_doc = show_checkpoint(cp_store, ws, ref)
                                    finally:
                                        cp_store.close()
                            prompt = _node_prompt(
                                a.node, plan.task, dep_docs, inbox,
                                continuation_doc=continuation_doc,
                            )
                            continue
                        break
                    decision = _steward.decide(
                        classification=cls, attempt=attempt, max_attempts=max_attempts,
                        budget_remaining_usd=remaining, est_cost_usd=a.est_cost_usd,
                        cur_model=model, hosts=hosts,
                        replan_available=(coordinate is not None and replans_left > 0),
                    )
                    last_action = decision.action
                    _record(_ledger.steward_row(
                        tid, a.node.id, attempt=attempt, on_reason=cls.reason,
                        failure_kind=cls.failure_kind, action=decision.action,
                        target=decision.target_name, budget_remaining_usd=remaining,
                    ))
                    if decision.action in ("escalate", "retry_same"):
                        # A fresh attempt starts from a clean tree: whatever the
                        # failed attempt wrote in its worktree is not evidence
                        # the next one should inherit.
                        if checkout is not None:
                            checkout.reset()
                        if decision.action == "escalate" and decision.target is not None:
                            host, model = decision.target
                            escalated = decision.target_name
                        continue
                    break  # replan / stop_*: the node ends failed; the wave loop decides
                if cls.reason == "done" and checkout is not None:
                    patch = checkout.capture()
        except WorktreeIsolationError as exc:
            code, out, err = 1, "", f"isolated worktree failed: {exc}"
            cls = _steward.Classification("failed", "verification_failure")
            _record(_ledger.handback_row(
                tid, a.node.id, attempt=max(attempt, 1), reason="failed",
                failure_kind="verification_failure", checkpoint=ref, turns=0,
                cost_usd=None, tokens=0, exit_code=1, host=host.name, model=model.id,
            ))
        return _NodeExecution(outcome=_outcome(), stdout=out, stderr=err, patch=patch)

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
                    # The added nodes exist only in memory until they are on
                    # the ledger; a resume rebuilds the route from task rows,
                    # so an unrecorded re-plan would silently vanish.
                    _open_task(ws, store, tid, plan, nodes[len(nodes) - added:], budget, source="replan")
                    continue
            break
        repository_clean = clean_git_root(Path(ws.root)) if isolated_worktrees else False
        selected, workers, wave_policy = _select_wave(
            ready,
            max_workers,
            final_wave=waves_run + 1 >= max_waves,
            isolated_worktrees=isolated_worktrees,
            repository_clean=repository_clean,
        )
        isolated_node_ids = (
            {item.node.id for item in selected if _is_mutation_node(item.node)}
            if wave_policy.endswith("/parallel_worktrees")
            else set()
        )
        wave_policies.append(wave_policy)
        waves_run += 1
        with ThreadPoolExecutor(max_workers=workers) as pool:
            executions = list(pool.map(run_one, selected))

        # Worktree mutations form one apply transaction per wave: every patch
        # preflights against the unchanged real workspace before any is applied.
        isolated_execs = [execution for execution in executions if execution.patch is not None]
        conflicts: list[tuple[_NodeExecution, str]] = []
        for execution in isolated_execs:
            ok, detail = preflight_patch(Path(ws.root), execution.patch)
            if not ok:
                conflicts.append((execution, detail))
        merge_failed: list[_NodeExecution] = []
        if conflicts:
            conflict_ids = {id(execution): detail for execution, detail in conflicts}
            for execution in isolated_execs:
                execution.outcome.status = "failed"
                execution.outcome.exit_code = 1
                if id(execution) in conflict_ids:
                    execution.outcome.merge_status = "conflict"
                    execution.stderr = f"isolated patch conflict: {conflict_ids[id(execution)]}"
                else:
                    execution.outcome.merge_status = "aborted"
                    execution.stderr = "isolated wave aborted because another patch conflicted"
                execution.outcome.detail = execution.stderr[:200]
                merge_failed.append(execution)
        elif isolated_execs:
            ok, detail = apply_patches(
                Path(ws.root), [execution.patch for execution in isolated_execs if execution.patch]
            )
            for execution in isolated_execs:
                execution.outcome.merge_status = "applied" if ok else "conflict"
                if not ok:
                    execution.outcome.status = "failed"
                    execution.outcome.exit_code = 1
                    execution.stderr = f"isolated patch apply failed: {detail}"
                    execution.outcome.detail = execution.stderr[:200]
                    merge_failed.append(execution)

        results: list[NodeOutcome] = []
        for a, execution in zip(selected, executions):
            o = execution.outcome
            if execution in merge_failed:
                # The node handed back `done`; the wave's merge then failed it.
                # Re-checkpoint the merge evidence and correct the ledger, or
                # resume would restore a node whose change never landed.
                o.checkpoint_ref = _checkpoint_node(
                    ws, a.node, plan.task, execution.stdout, execution.stderr,
                    handoff_strategy=o.handoff_policy,
                )
                o.reason, o.failure_kind = "failed", "verification_failure"
                _record(_ledger.handback_row(
                    tid, a.node.id, attempt=max(o.attempts, 1), reason="failed",
                    failure_kind="verification_failure", checkpoint=o.checkpoint_ref,
                    turns=0, cost_usd=0.0, tokens=0, exit_code=1,
                    host=o.host_name.split("/", 1)[0], model=o.host_name.split("/", 1)[-1],
                ))
            elif o.checkpoint_ref is None and o.exit_code is not None:
                o.checkpoint_ref = _checkpoint_node(
                    ws, a.node, plan.task, execution.stdout, execution.stderr,
                    handoff_strategy=o.handoff_policy,
                )
            results.append(o)
        for a, o in zip(selected, results):
            state[a.node.id] = o.status
            outcomes[a.node.id] = o
            usage_attempts.extend(o.usage_attempts)
            if o.checkpoint_ref:
                cp[a.node.id] = o.checkpoint_ref
                with contextlib.suppress(Exception):
                    from ctx.checkpoint import show_checkpoint

                    docs[a.node.id] = show_checkpoint(store, ws, o.checkpoint_ref)
            spent_est += _actual_cost(a, o)
        if budget > 0 and _remaining_budget() <= 0:
            break

    # Any never-run node is recorded skipped for a complete ledger.
    for a in nodes:
        outcomes.setdefault(
            a.node.id,
            NodeOutcome(a.node.id, a.host.name, "skipped", None, "not reached (bounds)"),
        )
    ordered = [outcomes[a.node.id] for a in nodes]
    final = _ledger_state()
    result = RouteResult(
        plan=plan,
        outcomes=ordered,
        waves_run=waves_run,
        replans=replans_done,
        estimated_spend_usd=spent_est,
        duration_ms=(time.monotonic() - started) * 1000.0,
        actual_usage=summarize_usage(usage_attempts),
        wave_policies=tuple(wave_policies),
        task_id=tid,
        resumed_nodes=tuple(resumed),
        ledger_spend_usd=final.spent_usd,
        ledger_spend_complete=final.cost_complete,
    )
    _append_route_receipt(ws, result, nodes)
    return result


_LEDGER_LOCK = threading.Lock()


def _open_task(
    ws, store, task_id: str, plan: RoutePlan, nodes: list[AssignedNode], budget: float,
    *, source: str | None = None,
) -> None:
    """Write a ``ctx.task/v1`` row. Task text goes into the store as a blob
    and the ledger carries its address, so the ledger stays export-safe like
    the route receipt. The opening row carries the whole DAG; an accepted
    coordinator re-plan appends another row (``source="replan"``) with only
    the nodes it added, so ``--resume`` restores them too. Fail-open."""
    with contextlib.suppress(Exception):
        replan = source == "replan"
        doc = {"task": "" if replan else plan.task, "nodes": {a.node.id: a.node.goal for a in nodes}}
        blob = store.put_blob(json.dumps(doc, sort_keys=True).encode("utf-8"))
        rows = [
            {
                "id": a.node.id, "role": a.node.role, "min_tier": a.node.min_tier,
                "need_tags": list(a.node.need_tags), "deps": list(a.node.deps),
                "host": a.host.name, "model": a.model.id, "prefer": a.node.prefer,
                "targets": list(a.node.targets),
                "edit_shape": a.node.edit_shape,
                "est_input_tokens": a.node.est_input_tokens,
                "est_output_tokens": a.node.est_output_tokens,
                "est_cost_usd": a.est_cost_usd,
            }
            for a in nodes
        ]
        with _LEDGER_LOCK:
            _ledger.append(ws.root, _ledger.task_row(
                task_id, goal_ref=f"blob:{short_id(blob)}", nodes=rows,
                budget_usd=budget, task_kind=plan.task_kind, source=source or plan.source,
            ))


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
        with contextlib.suppress(RouteError):  # _coerce_node can also raise RouteError; keep it inside the guard
            n = _coerce_node(r, len(nodes) + i)
            if n.id in existing:
                continue
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
    usage = result.actual_usage
    if usage.get("status") != "unavailable":
        cost = usage.get("cost_usd")
        cost_text = f" · {_usd(float(cost))}" if cost is not None else ""
        lines.append(
            f"actual usage: {usage['total_tokens']:,} tokens{cost_text} "
            f"({usage['status']}, {usage['attempts_measured']}/"
            f"{usage['attempts_total']} attempts measured)"
        )
    lines.append(f"task: {result.task_id}")
    if result.resumed_nodes:
        lines.append(f"resumed from ledger: {', '.join(result.resumed_nodes)}")
    if any(o.status != "ok" for o in result.outcomes):
        lines.append(f"resume: ctx orchestrate --resume {result.task_id}")
    lines.append(f"ledger: ctx task show {result.task_id}")
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
    resume: str | None = None,
) -> tuple[int, str]:
    """Detect harnesses → coordinate a route (cheap model, or deterministic
    fallback) → price & show it → run the closed loop. Returns (exit_code, text).

    ``resume`` names a task ledger: the plan is rebuilt from its ``ctx.task/v1``
    row (goal from the store, assignment re-resolved against the hosts
    installed now), the coordinator is skipped, and finished nodes are
    restored rather than re-run."""
    from ctx.installer import _ctx_executable

    cfg = ws.config.orchestrate
    if not resume and not (task or "").strip():
        return 1, (
            "ctx orchestrate: a task is required, or --resume <task-id> to replay a ledger; "
            "see `ctx task ls`."
        )
    resolved_exe = exe or _ctx_executable()
    hosts = installed_harnessable(workspace_root=ws.root)
    if not any(h.installed for h in hosts):
        return 1, (
            "ctx orchestrate: no installed harnessable CLI to orchestrate across.\n"
            "Install a supported CLI (claude, codex, antigravity); see `ctx wrap detect`."
        )
    if not any(h.installed and h.spec.unattended for h in hosts):
        return 1, (
            "ctx orchestrate: no installed host can run unattended.\n"
            "Install or enable claude, codex, or antigravity-sdk; interactive "
            "agy remains available only through an explicitly pinned route."
        )

    note = ""
    coordinator_usage: list[ActualUsage | None] = []
    plan: RoutePlan
    if resume:
        try:
            plan, task = _plan_from_ledger(ws, resume, hosts)
        except (RouteError, _ledger.LedgerError) as error:
            return 1, f"ctx orchestrate: cannot resume {resume}: {error}"
        note = f"resumed {resume} from the task ledger; coordinator skipped"
        fast_kind = "resumed"
    else:
        fast_kind = _fallback_task_kind(task)
    if resume:
        pass
    elif fast_kind != "general":
        plan = fallback_route(task, hosts, cfg)
        note = f"deterministic {fast_kind} fast path; coordinator skipped"
    else:
        unattended = [host for host in hosts if host.spec.unattended]
        coordinator = pick_coordinator(unattended)
        raw = None
        if not getattr(cfg, "fallback_only", False):
            raw = invoke_coordinator(
                ws,
                task,
                hosts,
                cfg,
                exe=resolved_exe,
                launch=launch,
                usage_sink=coordinator_usage,
            )
        if raw is not None:
            try:
                plan = build_route_plan(
                    task, raw, hosts, cfg, coordinator=coordinator
                )
            except RouteError as error:
                note = (
                    f"coordinator plan rejected ({error}); "
                    "using deterministic route"
                )
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
            ws,
            task,
            hosts,
            cfg,
            exe=resolved_exe,
            extra=extra,
            launch=launch,
            usage_sink=coordinator_usage,
        )

    result = run_route(
        ws,
        plan,
        cfg,
        exe=resolved_exe,
        launch=launch,
        coordinate=coordinate,
        prior_usage_attempts=coordinator_usage,
        task_id=resume,
        resume=bool(resume),
    )
    out.append("")
    out.append(render_result(result))
    return 0, "\n".join(out)


def _plan_from_ledger(ws, task_id: str, hosts) -> tuple[RoutePlan, str]:
    """Rebuild a RoutePlan from a task ledger's ``ctx.task/v1`` row.

    The recorded assignment becomes a pin per node; a pinned host that is no
    longer installed is ignored by ``_assign_host`` and the node re-routes to
    the cheapest model that clears its tier, which is the same rule a fresh
    plan follows. The DAG was validated when the task was opened, so it is
    not re-validated here."""
    from ctx.store import Store

    st = _ledger.task_state(_ledger.load(ws.root, task_id))
    if not st.task:
        raise RouteError("no ctx.task/v1 row in the ledger")
    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
    task = ""
    goals: dict[str, str] = {}
    recorded: list[dict] = []
    try:
        # The opening row plus one row per accepted re-plan, in order; nodes
        # a re-plan added are as much a part of the route as the originals.
        for row in st.task_rows:
            goal_ref = str(row.get("goal_ref") or "")
            doc = json.loads(store.get_blob(goal_ref.removeprefix("blob:")).decode("utf-8"))
            task = task or str(doc.get("task") or "")
            goals.update({str(k): str(v) for k, v in (doc.get("nodes") or {}).items()})
            recorded.extend(row.get("nodes") or [])
    finally:
        store.close()
    assigned: list[AssignedNode] = []
    seen: set[str] = set()
    for n in recorded:
        if str(n["id"]) in seen:
            continue
        seen.add(str(n["id"]))
        node = RouteNode(
            id=str(n["id"]), goal=str(goals.get(n["id"], "")), role=str(n.get("role") or ""),
            min_tier=str(n.get("min_tier") or "economy"),
            need_tags=tuple(n.get("need_tags") or ()), deps=tuple(n.get("deps") or ()),
            est_input_tokens=int(n.get("est_input_tokens") or 0),
            est_output_tokens=int(n.get("est_output_tokens") or 0),
            host_pin=str(n.get("host") or ""), model_pin=str(n.get("model") or ""),
            prefer=str(n.get("prefer") or "cheap"),
            targets=tuple(n.get("targets") or ()),
            edit_shape=str(n.get("edit_shape") or ""),
        )
        assigned.append(_assign_host(node, list(hosts)))
    return RoutePlan(
        task=task, hosts=tuple(hosts), coordinator=None, assigned=assigned,
        source=str(st.task.get("source") or "resumed"),
        task_kind=str(st.task.get("task_kind") or "general"),
    ), task


__all__ = [
    "ROUTE_SCHEMA",
    "ROUTE_RUN_SCHEMA",
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
    "_plan_from_ledger",
]
