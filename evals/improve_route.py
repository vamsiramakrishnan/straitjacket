"""The improvement route: hunt, verify, harvest, prove -- as one orchestrated task.

Round 17 (evals/bugbash-round17-2026-09-04.md) ran the self-improvement loop
by hand: an agent hunted defects, every claim was reproduced against the
tree, the survivors were harvested with a test that fails on the old code,
and the suite judged the result. The human-shaped parts were verification
and harvest. This is the same loop as a `ctx orchestrate` route, so the task
ledger records each step with a typed handback, a killed run resumes, and
the verdict is a number rather than a feeling:

    precision = findings reproduced / findings claimed

A round is *promotable* when precision clears the bar, at least one finding
survived, and the suite passed on the harvested tree. Otherwise it is
*held*, and the route says which of the three failed. Promotable means "a
human should review this diff", never "merge": the route proposes and
proves; it does not merge itself.

Four nodes, each a print-mode host run under the harness:

  hunt     explore   standard/strong   find defects in SCOPE, yield JSON
  verify   verify    standard          reproduce each claim; write a FAILING
                                        test per confirmed one; yield JSON
  harvest  implement standard          fix exactly what the failing tests pin
  prove    verify    economy           run the suite and lint; yield JSON

Every node is told it is a single-shot run (the wrap notice), to run any
subagents in the foreground, and to write its yield to
.ctx-session-reads/improve/<node>.json as well as print it, so the verdict
never depends on parsing a transcript.

    python evals/improve_route.py --dry-run            # priced plan, no launch
    python evals/improve_route.py --scope src/ctx/hook.py,src/ctx/reflex.py
    python evals/improve_route.py --json               # machine-readable receipt
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from ctx.hosts import installed_harnessable  # noqa: E402
from ctx.orchestrator import build_route_plan, run_route  # noqa: E402
from ctx.workspace import resolve_workspace  # noqa: E402

YIELD_DIR = ".ctx-session-reads/improve"
TEST_FILE = "tests/test_improve_round.py"
PRECISION_BAR = 0.8

_SINGLE_SHOT = (
    "This node is one single-shot run: no supervisor re-invokes you. Run any "
    "subagents in the FOREGROUND and collect their results before you finish; "
    "never end your turn to wait. "
)

_OBJ = {"type": "object"}
_ARR = {"type": "array"}


def _schema(**props: dict) -> dict:
    return {"type": "object", "properties": dict(props), "required": sorted(props)}


def raw_route(scope: str, *, test_file: str = TEST_FILE) -> dict:
    """The four-node route as a ctx.route/v1 object."""
    yd = YIELD_DIR
    return {"schema": "ctx.route/v1", "nodes": [
        {
            "id": "hunt", "role": "explore", "min_tier": "standard", "prefer": "strong",
            "deps": [],
            "goal": _SINGLE_SHOT + (
                f"Bug hunt in {scope} of this repository: find REAL defects -- logic "
                "errors, races, resource leaks, wrong edge cases, broken invariants -- "
                "not style. For each: file, line, one-paragraph explanation, a concrete "
                "failing scenario (inputs/state -> wrong behavior), severity high/medium/"
                "low. Verify each candidate by reading the surrounding code; discard "
                "anything speculative. Do NOT modify any code. Write your yield as JSON "
                f"to {yd}/hunt.json (create the directory) AND print it as your final "
                "message: {\"findings\":[{\"file\",\"line\",\"summary\",\"scenario\","
                "\"severity\"}]}."
            ),
            "output_schema": _schema(findings=_ARR),
            "est_input_tokens": 60000, "est_output_tokens": 6000,
        },
        {
            "id": "verify", "role": "verify", "min_tier": "standard", "deps": ["hunt"],
            "goal": _SINGLE_SHOT + (
                f"Read the hunt node's findings from {yd}/hunt.json. For EACH finding, "
                "reproduce it against the code: run the failing scenario (python -c, a "
                "small script, or a pytest). A finding that does not reproduce is "
                "refuted; say why. For each finding that DOES reproduce, write one test "
                f"function into {test_file} (create it with a module docstring naming "
                "this round) that FAILS on the current code and would pass once the "
                "defect is fixed; run it and confirm it fails. Do not fix anything. "
                f"Write your yield as JSON to {yd}/verify.json AND print it: "
                "{\"claimed\":N,\"verified\":[{\"file\",\"line\",\"test\",\"summary\"}],"
                "\"refuted\":[{\"file\",\"line\",\"why\"}]}."
            ),
            "output_schema": _schema(claimed={"type": "integer"}, verified=_ARR, refuted=_ARR),
            "est_input_tokens": 60000, "est_output_tokens": 6000,
        },
        {
            "id": "harvest", "role": "implement", "min_tier": "standard", "deps": ["verify"],
            "targets": ["src/ctx", test_file],
            "goal": _SINGLE_SHOT + (
                f"Read {yd}/verify.json. For each verified finding, make the smallest "
                f"fix in src/ctx that makes its test in {test_file} pass, with a short "
                "comment at the site saying what was wrong. Fix nothing that no test "
                "pins. Do not weaken or delete tests. Run the tests in that file after "
                "each fix. Do not run git commit. Write your yield as JSON to "
                f"{yd}/harvest.json AND print it: {{\"fixed\":[{{\"file\",\"test\"}}],"
                "\"skipped\":[{\"test\",\"why\"}]}}."
            ),
            "output_schema": _schema(fixed=_ARR, skipped=_ARR),
            "est_input_tokens": 60000, "est_output_tokens": 8000,
        },
        {
            "id": "prove", "role": "verify", "min_tier": "economy", "deps": ["harvest"],
            "goal": _SINGLE_SHOT + (
                "Run the full test suite: python3 -m pytest -q -x -p no:cacheprovider. "
                "Then run: ruff check on every .py file git reports as modified or "
                "untracked. Do not change any code. Write your yield as JSON to "
                f"{yd}/prove.json AND print it: {{\"suite_passed\":true|false,"
                "\"failures\":[..],\"lint_clean\":true|false}}."
            ),
            "output_schema": _schema(suite_passed={"type": "boolean"},
                                     failures=_ARR, lint_clean={"type": "boolean"}),
            "est_input_tokens": 20000, "est_output_tokens": 1500,
        },
    ]}


def _read_yield(root: Path, node: str) -> dict | None:
    path = root / YIELD_DIR / f"{node}.json"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else None
    except (OSError, ValueError):
        return None


def verdict(hunt: dict | None, verify: dict | None, harvest: dict | None,
            prove: dict | None, *, bar: float = PRECISION_BAR) -> dict:
    """The gate, as arithmetic over the four yields. Missing yields count as
    failures of that step, never as successes."""
    claimed = len((hunt or {}).get("findings") or [])
    if verify and isinstance(verify.get("claimed"), int):
        claimed = max(claimed, int(verify["claimed"]))
    verified = len((verify or {}).get("verified") or [])
    refuted = len((verify or {}).get("refuted") or [])
    precision = (verified / claimed) if claimed else 0.0
    fixed = len((harvest or {}).get("fixed") or [])
    suite = bool((prove or {}).get("suite_passed")) if prove else False
    lint = bool((prove or {}).get("lint_clean")) if prove else False
    reasons = []
    if claimed == 0:
        reasons.append("hunt claimed nothing")
    if verified == 0:
        reasons.append("nothing reproduced")
    if claimed and precision < bar:
        reasons.append(f"precision {precision:.2f} below bar {bar:.2f}")
    if not suite:
        reasons.append("suite did not pass on the harvested tree")
    if not lint:
        reasons.append("lint not clean")
    return {
        "claimed": claimed, "verified": verified, "refuted": refuted,
        "precision": round(precision, 3), "bar": bar, "fixed": fixed,
        "suite_passed": suite, "lint_clean": lint,
        "verdict": "promotable" if not reasons else "held",
        "reasons": reasons,
    }


def run(ws_root: Path, *, scope: str, dry_run: bool, budget_usd: float,
        node_timeout: float, hosts=None) -> dict:
    ws = resolve_workspace(str(ws_root))
    roster = hosts if hosts is not None else installed_harnessable(workspace_root=ws.root)
    if not roster:
        return {"error": "no installed harnessable host; nothing to route"}
    cfg = replace(ws.config.orchestrate, budget_usd=budget_usd, node_timeout=node_timeout,
                  isolated_worktrees=False, turn_ceiling=0, confirm=False)
    plan = build_route_plan(f"improve {scope}", raw_route(scope), roster, cfg)
    priced = [{"node": a.node.id, "role": a.node.role, "host": a.host.name,
               "model": a.model.id, "tier_met": a.tier_met,
               "est_usd": round(float(a.est_cost_usd), 3)} for a in plan.assigned]
    rec = {"schema": "ctx.improve-route/v1", "scope": scope, "plan": priced,
           "est_total_usd": round(float(plan.est_total_usd), 3), "dry_run": dry_run}
    if dry_run:
        return rec
    result = run_route(ws, plan, cfg)
    rec["task_id"] = result.task_id
    rec["outcomes"] = [{"node": o.node_id, "status": o.status, "host": o.host_name,
                        "attempts": o.attempts, "reason": o.reason,
                        "schema": o.output_schema_status, "detail": o.detail[:200]}
                       for o in result.outcomes]
    rec["spend_usd"] = round(float(result.ledger_spend_usd or result.estimated_spend_usd), 3)
    yields = {n: _read_yield(ws.root, n) for n in ("hunt", "verify", "harvest", "prove")}
    rec["yields_present"] = {n: y is not None for n, y in yields.items()}
    rec["gate"] = verdict(yields["hunt"], yields["verify"], yields["harvest"], yields["prove"])
    return rec


def render(rec: dict) -> str:
    if "error" in rec:
        return f"[improve route] {rec['error']}"
    out = [f"[improve route · scope {rec['scope']} · est ${rec['est_total_usd']:.2f}"
           + (" · dry run]" if rec["dry_run"] else "]")]
    out.append(f"{'node':8} {'role':10} {'host/model':34} {'est $':>6}")
    for p in rec["plan"]:
        out.append(f"{p['node']:8} {p['role']:10} {(p['host'] + '/' + p['model'])[:34]:34} {p['est_usd']:>6.3f}")
    if rec["dry_run"]:
        return "\n".join(out)
    out.append("")
    out.append(f"task {rec['task_id']} · spent ${rec['spend_usd']:.2f}")
    for o in rec["outcomes"]:
        out.append(f"  {o['node']:8} {o['status']:7} {o['host']:34} attempts {o['attempts']} "
                   f"yield {o['schema']}")
    g = rec["gate"]
    out.append("")
    out.append(f"claimed {g['claimed']} · verified {g['verified']} · refuted {g['refuted']} · "
               f"precision {g['precision']:.2f} (bar {g['bar']:.2f}) · fixed {g['fixed']} · "
               f"suite {'pass' if g['suite_passed'] else 'FAIL'} · lint "
               f"{'clean' if g['lint_clean'] else 'DIRTY'}")
    out.append(f"verdict: {g['verdict']}" + (f" -- {'; '.join(g['reasons'])}" if g["reasons"] else
               " -- review the diff; the route does not merge"))
    return "\n".join(out)


def _args(argv: list[str]) -> dict:
    def val(flag, default):
        return argv[argv.index(flag) + 1] if flag in argv else default
    return {
        "scope": val("--scope", "src/ctx"),
        "dry_run": "--dry-run" in argv,
        "budget_usd": float(val("--budget", "0")),
        "node_timeout": float(val("--node-timeout", "3600")),
        "as_json": "--json" in argv,
        "workspace": Path(val("--workspace", str(ROOT))),
    }


if __name__ == "__main__":
    a = _args(sys.argv[1:])
    record = run(a["workspace"], scope=a["scope"], dry_run=a["dry_run"],
                 budget_usd=a["budget_usd"], node_timeout=a["node_timeout"])
    print(json.dumps(record, indent=2, sort_keys=True) if a["as_json"] else render(record))
