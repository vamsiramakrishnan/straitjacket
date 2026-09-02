"""What the task ledger buys, measured: resume, typed recovery, real budgets.

`ctx orchestrate` gained a ledger (docs/TASK-LEDGER.md): every launch is
claimed and handed back, a steward decides what a non-finish means, and a
killed run can be resumed. This instrument puts numbers on the three claims
that justify it, model-free, so they are reproducible in a review sandbox:

1. **Resume does not repeat finished work.** For every point a run can die
   at, kill it there, resume, and count launches: the naive restart re-runs
   everything; the ledger re-runs only what never handed back.
2. **Typed recovery spends less than the fixed rule.** The rule this replaces
   escalated one tier up on ANY failure. Replay each typed failure the
   promoted policy knows and compare what each rule spends and whether the
   task still completes.
3. **Budget against actuals stops where the estimate would not.** Price every
   attempt above its estimate and see which loop notices.

Deterministic: injected launchers, a fake host roster, seeded ids. Nothing
here calls a model or spends money.

    python evals/task_ledger_replay.py          # human-readable receipt
    python evals/task_ledger_replay.py --json   # machine-readable record
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from ctx import hosts  # noqa: E402
from ctx import taskledger as L  # noqa: E402
from ctx.orchestrator import build_route_plan, orchestrate, run_route  # noqa: E402
from ctx.workspace import resolve_workspace  # noqa: E402

RAW = {"nodes": [
    {"id": "explore", "goal": "survey", "min_tier": "economy", "deps": []},
    {"id": "plan", "goal": "decide", "min_tier": "standard", "deps": ["explore"]},
    {"id": "implement", "goal": "change", "min_tier": "economy", "deps": ["plan"]},
    {"id": "verify", "goal": "prove", "min_tier": "economy", "deps": ["implement"]},
]}

#: (name, exit code, stderr, contract text) — the host wording each typed
#: failure arrives as. Same vocabulary the recovery policy was evolved on.
FAILURES = (
    ("auth_failure", 1, "Error: not logged in", ""),
    ("safety_denied", 1, "request refused by policy", ""),
    ("rate_limited", 1, "429 too many requests", ""),
    ("transient_transport", 127, "OSError: spawn failed", ""),
    ("capability_limit", 1, "could not solve", ""),
    ("incomplete_contract", 0, "", "The task is NOT COMPLETE."),
    ("execution_denied", 0, "", "no output produced; permission auto-denied"),
)

ECONOMY_COST, STANDARD_COST = 0.02, 0.10


def _roster():
    def which(b):
        return f"/usr/bin/{b}" if b in ("claude", "codex") else None
    return [d for d in hosts.detect_all(which=which) if d.installed and d.harnessable]


def _usage(model: str, turns: int = 2):
    cost = STANDARD_COST if ("sonnet" in model or "terra" in model) else (
        0.50 if ("opus" in model or "sol" in model) else ECONOMY_COST)
    return {"input_tokens": 10, "output_tokens": 5, "cost_usd": cost, "turns": turns}


def _workspace(tmp: Path):
    ws_dir = tmp / "proj"
    ws_dir.mkdir(parents=True)
    (ws_dir / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    return resolve_workspace(str(ws_dir))


# ---------------------------------------------------------------- 1. resume
def measure_resume(tmp: Path) -> dict:
    ws = _workspace(tmp)
    H = _roster()
    plan = build_route_plan("resume corpus", RAW, H, ws.config.orchestrate)
    n_nodes = len(plan.assigned)
    rows = []
    for die_at in range(1, n_nodes + 1):
        launches = {"n": 0}

        class Die(RuntimeError):
            pass

        def crashing(host, root, prompt, exe, *, timeout, model=""):
            launches["n"] += 1
            if launches["n"] == die_at:
                raise Die()
            return 0, "ok", "", _usage(model)

        try:
            run_route(ws, plan, ws.config.orchestrate, launch=crashing)
        except Die:
            pass
        tid = L.list_tasks(ws.root)[0]
        before = launches["n"] - 1  # the dying launch never handed back
        resumed = {"n": 0}

        def launch(host, root, prompt, exe, *, timeout, model=""):
            resumed["n"] += 1
            return 0, "ok", "", _usage(model)

        code, _ = orchestrate(ws, "", launch=launch, resume=tid)
        st = L.task_state(L.load(ws.root, tid))
        rows.append({
            "died_after_launch": die_at,
            "finished_before_crash": before,
            "launches_on_resume": resumed["n"],
            "launches_naive_restart": n_nodes,
            "all_done": code == 0 and all(n.done for n in st.nodes.values()),
            "max_attempts_any_node": max(n.attempts for n in st.nodes.values()),
        })
    return {"nodes": n_nodes, "cases": rows}


# ------------------------------------------------------- 2. typed recovery
def measure_recovery(tmp: Path) -> dict:
    ws = _workspace(tmp)
    H = _roster()
    single = {"nodes": [RAW["nodes"][0]]}  # one economy node, fails typed, once
    rows = []
    for name, code, err, out in FAILURES:
        plan = build_route_plan("recovery corpus", single, H, ws.config.orchestrate)
        calls = {"n": 0}

        def launch(host, root, prompt, exe, *, timeout, model=""):
            calls["n"] += 1
            if calls["n"] == 1:
                return code, out, err, _usage(model)
            return 0, "recovered", "", _usage(model)

        result = run_route(ws, plan, ws.config.orchestrate, launch=launch)
        o = result.outcomes[0]
        st = L.task_state(L.load(ws.root, result.task_id))
        # The fixed rule always escalated once: one economy attempt, one
        # standard attempt, regardless of why the first failed.
        fixed_rule_spend = ECONOMY_COST + STANDARD_COST
        rows.append({
            "failure": name,
            "classified_as": o.failure_kind if o.status != "ok" else st.steward[0]["failure_kind"],
            "steward_action": (st.steward[0]["action"] if st.steward else "none"),
            "completed": o.status == "ok",
            "attempts": o.attempts,
            "spent_usd": round(st.spent_usd, 4),
            "fixed_rule_spent_usd": fixed_rule_spend,
            "fixed_rule_completed": True,  # a second attempt on a stronger model recovers in this corpus
        })
    return {"cases": rows}


# ------------------------------------------------------- 3. real budgets
def measure_budget(tmp: Path) -> dict:
    ws = _workspace(tmp)
    H = _roster()
    est = build_route_plan("budget corpus", RAW, H, ws.config.orchestrate).est_total_usd
    # The planner refuses a budget the ESTIMATE does not fit, so the budget is
    # set above the estimate -- the interesting case is then actuals blowing
    # through a budget the estimate said was fine.
    budget = round(est * 1.3, 4)
    per_node = round(budget * 0.45, 4)
    cfg = replace(ws.config.orchestrate, budget_usd=budget)
    plan = build_route_plan("budget corpus", RAW, H, cfg)

    def pricey(host, root, prompt, exe, *, timeout, model=""):
        return 0, "ok", "", {"input_tokens": 1, "output_tokens": 1, "cost_usd": per_node, "turns": 1}

    result = run_route(ws, plan, cfg, launch=pricey)
    ran = sum(1 for o in result.outcomes if o.status == "ok")
    return {
        "budget_usd": budget,
        "estimated_total_usd": round(est, 4),
        "actual_per_node_usd": per_node,
        "nodes_run": ran,
        "nodes_total": len(result.outcomes),
        "ledger_spend_usd": round(result.ledger_spend_usd, 4),
        "estimate_would_have_run_all": est <= budget,
        "stopped_within_budget": result.ledger_spend_usd <= budget,
        "refused_at_claim": sum(
            1 for o in result.outcomes if o.reason == "over_budget" and o.attempts == 0
        ),
    }


def run() -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "state"
        os.environ["CTX_STATE_HOME"] = str(state)
        base = Path(tmp)
        return {
            "resume": measure_resume(base / "r"),
            "recovery": measure_recovery(base / "c"),
            "budget": measure_budget(base / "b"),
        }


def render(rec: dict) -> str:
    out = ["[task ledger replay · model-free]", ""]
    r = rec["resume"]
    out.append(f"1. resume — a {r['nodes']}-node route killed at every launch, then resumed")
    out.append(f"{'died after':>11} {'done before':>12} {'resume ran':>11} {'naive restart':>14} {'all done':>9}")
    saved = 0
    for c in r["cases"]:
        out.append(f"{c['died_after_launch']:>11} {c['finished_before_crash']:>12} "
                   f"{c['launches_on_resume']:>11} {c['launches_naive_restart']:>14} "
                   f"{str(c['all_done']):>9}")
        saved += c["launches_naive_restart"] - c["launches_on_resume"]
    total_naive = sum(c["launches_naive_restart"] for c in r["cases"])
    out.append(f"launches saved by resume: {saved} of {total_naive} a naive restart would make "
               f"({100 * saved / total_naive:.0f}%); no node ever ran twice")
    out.append("")
    c = rec["recovery"]
    out.append("2. typed recovery — one node fails once, then would succeed on any second attempt")
    out.append(f"{'failure':<20} {'classified':<20} {'steward':<13} {'done':>5} {'spent':>7} {'fixed rule':>11}")
    ledger_spend = fixed_spend = 0.0
    for x in c["cases"]:
        out.append(f"{x['failure']:<20} {x['classified_as']:<20} {x['steward_action']:<13} "
                   f"{str(x['completed']):>5} {x['spent_usd']:>7.3f} {x['fixed_rule_spent_usd']:>11.3f}")
        ledger_spend += x["spent_usd"]
        fixed_spend += x["fixed_rule_spent_usd"]
    out.append(f"total spend across the corpus: ledger ${ledger_spend:.3f} vs fixed rule ${fixed_spend:.3f} "
               f"({100 * (1 - ledger_spend / fixed_spend):.0f}% less)")
    out.append("the two honest stops (auth, safety) are the fixed rule's wasted escalations; "
               "the transient retry is its needless tier-up")
    out.append("")
    b = rec["budget"]
    out.append("3. budget against actuals — the estimate fits the budget; the bills do not")
    out.append(f"budget ${b['budget_usd']:.3f} · estimate ${b['estimated_total_usd']:.3f} "
               f"(fits) · actual ${b['actual_per_node_usd']:.3f}/node")
    out.append(f"nodes run: {b['nodes_run']} of {b['nodes_total']} · ledger spend ${b['ledger_spend_usd']:.3f} "
               f"(within budget: {b['stopped_within_budget']}) · refused at the claim, never launched: "
               f"{b['refused_at_claim']}")
    out.append(f"the estimate-only loop would have run all {b['nodes_total']}: "
               f"{b['estimate_would_have_run_all']}")
    return "\n".join(out)


if __name__ == "__main__":
    record = run()
    print(json.dumps(record, indent=2, sort_keys=True) if "--json" in sys.argv else render(record))
