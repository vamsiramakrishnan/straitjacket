"""Versioned inventory of every Straitjacket optimization lever.

The registry separates mutable policy from protected safety/measurement planes
and maps production seams to small experiments with independent credit.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent

EXPERIMENT_MODULES: dict[str, str] = {
    "context-budget": "evals.alphaevolve.context_budget.evaluate",
    "turn-policy": "evals.alphaevolve.turn_policy.evaluate",
    "route-policy": "evals.alphaevolve.route_policy.evaluate",
    "naive-fast-path": "evals.alphaevolve.naive_fast_path.evaluate",
    "route-replay": "evals.alphaevolve.route_replay.evaluate",
    "escalation-policy": "evals.alphaevolve.escalation_policy.evaluate",
    "surface-policy": "evals.alphaevolve.surface_policy.evaluate",
    "profile-policy": "evals.alphaevolve.profile_policy.evaluate",
    "emission-policy": "evals.alphaevolve.emission_policy.evaluate",
    "retrieval-policy": "evals.alphaevolve.retrieval_policy.evaluate",
    "guard-policy": "evals.alphaevolve.guard_policy.evaluate",
    "engagement-policy": "evals.alphaevolve.engagement_policy.evaluate",
    "plan-policy": "evals.alphaevolve.plan_policy.evaluate",
    "context-policy": "evals.alphaevolve.context_policy.evaluate",
    "execution-policy": "evals.alphaevolve.execution_policy.evaluate",
    "setup-policy": "evals.alphaevolve.setup_policy.evaluate",
    "wave-policy": "evals.alphaevolve.wave_policy.evaluate",
    "mutation-policy": "evals.alphaevolve.mutation_policy.evaluate",
    "handoff-policy": "evals.alphaevolve.handoff_policy.evaluate",
    "verification-policy": "evals.alphaevolve.verification_policy.evaluate",
}


@dataclass(frozen=True)
class LeverSpec:
    id: str
    experiment: str | None
    wave: str
    risk: str
    evidence: str
    rollout: str
    production_seam: str
    candidate_api: str
    mutable: bool = True

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _lever(
    lever_id: str,
    experiment: str | None,
    wave: str,
    risk: str,
    evidence: str,
    rollout: str,
    seam: str,
    api: str,
    *,
    mutable: bool = True,
) -> LeverSpec:
    return LeverSpec(
        lever_id, experiment, wave, risk, evidence, rollout, seam, api, mutable
    )


LEVERS: tuple[LeverSpec, ...] = (
    _lever("capability-surface", "surface-policy", "containment", "medium", "replay", "shadow", "src/ctx/surface_profiles.py:select", "choose_surface(state, options)"),
    _lever("small-result-pass-through", "emission-policy", "containment", "low", "corpus", "offline", "src/ctx/digest/__init__.py:_pass_through_if_digest_earned_nothing", "choose_emission(state, options)"),
    _lever("profile-detection", "profile-policy", "containment", "medium", "corpus", "shadow", "src/ctx/digest/__init__.py:detect_profile", "choose_profile(state, options)"),
    _lever("evidence-selection", "context-budget", "containment", "medium", "corpus", "shadow", "src/ctx/digest/evidence_render.py", "allocate_context(items, budget_tokens)"),
    _lever("delivery-budget", "emission-policy", "containment", "medium", "replay", "shadow", "src/ctx/resolver.py:resolve_delivery", "choose_emission(state, options)"),
    _lever("retrieval-budget", "retrieval-policy", "retrieval", "medium", "replay", "shadow", "src/ctx/resolver.py:resolve_retrieval_budget", "choose_retrieval(state, options)"),
    _lever("retrieval-strategy", "retrieval-policy", "retrieval", "medium", "replay", "shadow", "src/ctx/_retrieval", "choose_retrieval(state, options)"),
    _lever("command-span-registry", "guard-policy", "birth-gate", "high", "generated-matrix", "shadow", "src/ctx/command_spans.py:classify_command_span", "choose_guard(state, options)"),
    _lever("command-rewrite", "guard-policy", "birth-gate", "high", "adversarial", "shadow", "src/ctx/hook.py:classify_command", "choose_guard(state, options)"),
    _lever("read-pressure", "guard-policy", "birth-gate", "high", "replay", "shadow", "src/ctx/hook.py:classify_read", "choose_guard(state, options)"),
    _lever("graduated-engagement", "engagement-policy", "behavior", "medium", "ledger", "shadow", "src/ctx/engagement.py", "choose_engagement(state, options)"),
    _lever("reflex-circuit", "engagement-policy", "behavior", "medium", "ledger", "shadow", "src/ctx/reflex.py", "choose_engagement(state, options)"),
    _lever("plan-compiler", "plan-policy", "planning", "medium", "corpus", "shadow", "src/ctx/ask.py;src/ctx/plan_ir.py", "choose_plan(state, options)"),
    _lever("operator-ordering", "plan-policy", "planning", "medium", "ledger", "shadow", "src/ctx/plan_value.py:rank_followup", "choose_plan(state, options)"),
    _lever("turn-policy", "turn-policy", "planning", "medium", "simulator", "shadow", "src/ctx/plan_exec.py", "choose_action(state)"),
    _lever("repository-context", "context-policy", "context", "medium", "corpus", "shadow", "src/ctx/filesets.py;src/ctx/repomap.py", "choose_context(state, options)"),
    _lever("naive-fast-path", "naive-fast-path", "orchestration", "medium", "hybrid", "canary", "src/ctx/orchestrator.py:_fast_fallback_nodes", "choose_fast_path(task, plans)"),
    _lever("model-route", "route-replay", "orchestration", "high", "actual-usage", "canary", "src/ctx/orchestrator.py:fallback_route", "choose_route(profile, routes)"),
    _lever("dag-construction", "route-policy", "orchestration", "high", "simulator", "canary", "src/ctx/orchestrator.py:build_route_plan", "choose_route(task, routes)"),
    _lever("recovery-escalation", "escalation-policy", "orchestration", "high", "actual-usage", "canary", "src/ctx/orchestrator.py:run_route", "choose_recovery(state)"),
    _lever("wave-scheduler", "wave-policy", "orchestration", "high", "route-replay", "shadow", "src/ctx/orchestrator.py:run_route", "choose_wave(state, options)"),
    _lever("mutation-isolation", "mutation-policy", "orchestration", "high", "adversarial", "shadow", "src/ctx/orchestrator.py:run_route", "choose_mutation_isolation(state, options)"),
    _lever("handoff-budget", "handoff-policy", "orchestration", "medium", "route-replay", "shadow", "src/ctx/orchestrator.py:_checkpoint_node", "choose_handoff(state, options)"),
    _lever("verification-route", "verification-policy", "orchestration", "high", "actual-usage", "shadow", "src/ctx/orchestrator.py:build_route_plan", "choose_verification(state, options)"),
    _lever("backgrounding", "execution-policy", "execution", "medium", "replay", "shadow", "src/ctx/jobs.py;src/ctx/execution.py", "choose_execution(state, options)"),
    _lever("cache-materialization", "execution-policy", "execution", "high", "adversarial", "shadow", "src/ctx/store.py;src/ctx/skeleton.py", "choose_execution(state, options)"),
    _lever("setup-fast-noop", "setup-policy", "devex", "low", "setup-receipt", "canary", "src/ctx/wrap.py:guided_setup;src/ctx/setup_telemetry.py", "choose_setup(state, options)"),
    _lever("setup-repair", "setup-policy", "devex", "medium", "adversarial", "canary", "src/ctx/installer.py;src/ctx/wrap.py:guided_setup", "choose_setup(state, options)"),
    _lever("actual-usage-accounting", None, "protected", "protected", "wire-truth", "immutable", "src/ctx/usage.py", "measurement oracle", mutable=False),
    _lever("secret-workspace-guards", None, "protected", "protected", "security", "immutable", "src/ctx/config.py;src/ctx/hook.py", "safety oracle", mutable=False),
    _lever("receipt-integrity", None, "protected", "protected", "schema", "immutable", "src/ctx/route_telemetry.py;src/ctx/store.py", "promotion oracle", mutable=False),
)

WAVES = tuple(sorted({lever.wave for lever in LEVERS if lever.mutable}))


def experiments_for_wave(wave: str | None = None) -> tuple[str, ...]:
    if wave is None:
        return tuple(EXPERIMENT_MODULES)
    return tuple(
        name
        for name in EXPERIMENT_MODULES
        if any(lever.experiment == name and lever.wave == wave for lever in LEVERS)
    )


def levers_for_experiment(name: str) -> tuple[LeverSpec, ...]:
    return tuple(lever for lever in LEVERS if lever.experiment == name)


def experiment_fingerprint(name: str) -> str:
    module = EXPERIMENT_MODULES[name]
    experiment_dir = ROOT / module.split(".")[-2]
    digest = hashlib.sha256()
    for path in sorted(experiment_dir.glob("*")):
        if path.is_file() and path.suffix in {".py", ".md", ".json"}:
            digest.update(path.name.encode("utf-8"))
            digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def registry_document(levers: Iterable[LeverSpec] = LEVERS) -> dict[str, object]:
    return {
        "schema": "ctx.alphaevolve-levers/v1",
        "levers": [lever.as_dict() for lever in levers],
        "experiments": {
            name: {
                "module": module,
                "fingerprint": experiment_fingerprint(name),
                "levers": [lever.id for lever in levers_for_experiment(name)],
            }
            for name, module in EXPERIMENT_MODULES.items()
        },
    }
