"""Phase 5 — automatic surface reconciliation (shadow by default).

Continuously reconcile the *declared* capability surface against what the
session is actually doing, and recommend the smallest change:

* **reveal** a family when task intent calls for it;
* **hide** a high-cost family that went unused once its phase has passed;
* **never** hide a family an active task contract requires.

The governing law is unchanged: *do not place a capability in context until
the task has earned the cost — and never remove one the task still needs.*
Because a wrong hide is the dangerous direction, Phase 5 is **shadow by
default**: recommendations are logged to a ledger and a paired referee scores
whether each shadowed hide would have been safe (the family stayed unused
afterwards). Only ``--enforce`` applies actions, and only a referee-promoted
rule should be trusted to run unattended.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ctx import surface
from ctx.surface_gateway import KERNEL_FAMILIES, load_state, save_state

SHADOW_LEDGER = ".ctx-surface/reconcile-shadow.jsonl"

# Task-intent → family. A prompt that mentions any keyword reveals the family.
#
# Edge whitespace is SIGNIFICANT and is the boundary declaration: `" pr "` may
# only match the whole word, while an unpadded `"vulnerab"` or `"infra"` is a
# deliberate prefix that should still fire on "vulnerability" / "infrastructure".
# The matcher used to `.strip()` these before the substring test, throwing the
# declaration away -- "sprint" then revealed remote-source-control.
INTENT_TRIGGERS: dict[str, tuple[str, ...]] = {
    "remote-source-control": ("pull request", " pr ", "open a pr", "merge", "github",
                              "review the pr", "push the branch"),
    "testing": ("run the test", "pytest", "failing test", "verify", "regression"),
    "deployment": ("deploy", "release", "ship it", "rollout", "roll out"),
    "cloud": ("aws", "gcp", "kubernetes", "cluster", "terraform", "infra"),
    "database": ("sql", "migration", "query the db", "database"),
    "semantic-analysis": ("taint", "dataflow", "data flow", "source-to-sink",
                          "security", "vulnerab", "sink"),
    "browser": ("screenshot", "open the url", "browse", "web page"),
    "collaboration": ("slack", "email", "ticket", "jira", "notify the"),
}


@dataclass(frozen=True)
class Action:
    op: str          # reveal | hide
    family: str
    reason: str
    tokens: int = 0  # family token cost (hides are ordered high-cost first)


@dataclass
class Signals:
    revealed: set[str]
    current_phase: str
    intent_text: str
    usage_since_reveal: dict[str, int]
    family_tokens: dict[str, int]
    required_families: set[str]
    available_families: set[str] = field(default_factory=set)


_SEP_RE = re.compile(r"[^0-9a-z]+")


def _normalize(text: str) -> str:
    """Lowercase, collapse every non-alphanumeric run to one space, pad.

    Padding the TEXT is what makes padding a KEYWORD mean anything. Doing it
    with a bare `f" {text} "` was not enough: `"open a PR."` and `"the pr,"`
    have punctuation where the boundary is, so `" pr "` missed the very
    mentions it exists to catch while `.strip()` let it match inside
    "sprint". Normalizing separators fixes both directions at once.
    """
    return " " + _SEP_RE.sub(" ", text.lower()).strip() + " "


def _normalize_kw(kw: str) -> str:
    """Same normalization, but edge padding is preserved -- it is the
    keyword's boundary declaration, not incidental whitespace."""
    return _SEP_RE.sub(" ", kw.lower())


def reconcile(sig: Signals) -> list[Action]:
    """Pure reconciliation: signals → the minimal reveal/hide actions.
    Deterministic and side-effect-free."""
    actions: list[Action] = []
    low = _normalize(sig.intent_text)
    # 1. reveal on intent (smallest cover: only families that actually exist here)
    for fam, kws in INTENT_TRIGGERS.items():
        if fam in sig.revealed or (sig.available_families and fam not in sig.available_families):
            continue
        hit = next((k for k in kws if _normalize_kw(k) in low), None)
        if hit:
            actions.append(Action("reveal", fam, f"intent match: '{hit.strip()}'",
                                  sig.family_tokens.get(fam, 0)))
    # 2. hide unused high-cost families whose phase has passed — never a
    #    required one, never the kernel (high-cost first).
    for fam in sorted(sig.revealed, key=lambda f: -sig.family_tokens.get(f, 0)):
        if fam in KERNEL_FAMILIES or fam in sig.required_families:
            continue
        fam_phase = surface._PHASE_OF_FAMILY.get(fam, "")
        if fam_phase and fam_phase != sig.current_phase and sig.usage_since_reveal.get(fam, 0) == 0:
            actions.append(Action("hide", fam,
                                  f"unused in phase {sig.current_phase} (belongs to {fam_phase})",
                                  sig.family_tokens.get(fam, 0)))
    return actions


# ------------------------------------------------------- workspace adapter
def infer_phase(usage: dict[str, int]) -> str:
    """Coarse current-phase guess from recent tool usage. Deterministic."""
    edits = sum(n for t, n in usage.items() if t.lower() in
                ("edit", "write", "multiedit", "apply_patch", "str_replace"))
    tests = sum(n for t, n in usage.items() if "test" in t.lower() or "pytest" in t.lower())
    if tests and tests >= edits:
        return "verify"
    if edits:
        return "edit"
    return "explore"


def required_families(ws_root: Path | str, records: list[surface.Capability]) -> set[str]:
    """Families an active contract requires (never hide): families of kept
    skills' referenced servers, plus any active compiled profile's families."""
    req: set[str] = set()
    provider_family = {c.provider: c.family for c in records if c.kind in ("mcp_server", "mcp_tool")}
    for c in records:
        # c.requires already holds bare decoded names, not "mcp__"-prefixed text -- use as-is
        for srv in (c.requires or ()):
            if srv in provider_family:
                req.add(provider_family[srv])
    return req


def signals_from_workspace(ws_root: Path | str, *, intent: str = "",
                           phase: str | None = None) -> Signals:
    a = surface.audit(ws_root)
    records = [surface.Capability(**{k: (tuple(v) if isinstance(v, list) else v)
                                     for k, v in r.items() if k != "recommended_level"})
               for r in a["records"]]
    family_tokens = {f: slot["tokens"] for f, slot in a["graph"]["families"].items()}
    usage = surface.observed_tool_counts(ws_root)
    revealed = load_state(ws_root)
    available = set(a["graph"]["families"])
    # per-family invocations since reveal: fold tool counts by provider→family
    provider_family = {c.provider: c.family for c in records if c.kind in ("mcp_server", "mcp_tool")}
    fam_usage: dict[str, int] = {}
    for tool, n in usage.items():
        if tool.startswith("mcp__"):
            srv = tool.split("__")[1] if "__" in tool[5:] + "__" else ""
            fam = provider_family.get(srv)
            if fam:
                fam_usage[fam] = fam_usage.get(fam, 0) + n
    return Signals(
        revealed=revealed,
        current_phase=phase or infer_phase(usage),
        intent_text=intent,
        usage_since_reveal=fam_usage,
        family_tokens=family_tokens,
        required_families=required_families(ws_root, records),
        available_families=available,
    )


# ------------------------------------------------------- shadow + enforce
def _append_shadow(ws_root: Path | str, actions: list[Action], *, phase: str,
                   enforced: bool) -> None:
    try:
        path = Path(ws_root) / SHADOW_LEDGER
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for act in actions:
                fh.write(json.dumps({
                    "schema": "ctx.surface-reconcile/v1",
                    "op": act.op, "family": act.family, "reason": act.reason,
                    "tokens": act.tokens, "phase": phase, "enforced": enforced,
                }, sort_keys=True) + "\n")
    except Exception:
        pass


def apply_actions(ws_root: Path | str, actions: list[Action]) -> set[str]:
    """Apply reveal/hide to the gateway state. Enforcement path (opt-in)."""
    revealed = load_state(ws_root)
    for act in actions:
        if act.op == "reveal":
            revealed.add(act.family)
        elif act.op == "hide" and act.family not in KERNEL_FAMILIES:
            revealed.discard(act.family)
    save_state(ws_root, revealed)
    return revealed


def run_reconcile(ws_root: Path | str, *, intent: str = "", phase: str | None = None,
                  enforce: bool = False) -> dict[str, Any]:
    sig = signals_from_workspace(ws_root, intent=intent, phase=phase)
    actions = reconcile(sig)
    _append_shadow(ws_root, actions, phase=sig.current_phase, enforced=enforce)
    applied: list[str] = []
    if enforce and actions:
        apply_actions(ws_root, actions)
        applied = [f"{a.op} {a.family}" for a in actions]
    return {
        "schema": "ctx.surface-reconcile/v1",
        "phase": sig.current_phase,
        "revealed": sorted(sig.revealed),
        "required_protected": sorted(sig.required_families),
        "actions": [{"op": a.op, "family": a.family, "reason": a.reason, "tokens": a.tokens}
                    for a in actions],
        "enforced": enforce,
        "applied": applied,
    }


def referee(ws_root: Path | str) -> dict[str, Any]:
    """Paired referee: read the shadow ledger and, using subsequent observed
    usage, score whether each shadowed *hide* would have been safe (the family
    stayed unused). A rule promotes only when it never mis-hid a used family."""
    usage = surface.observed_tool_counts(ws_root)
    a = surface.audit(ws_root)
    records = [surface.Capability(**{k: (tuple(v) if isinstance(v, list) else v)
                                     for k, v in r.items() if k != "recommended_level"})
               for r in a["records"]]
    provider_family = {c.provider: c.family for c in records if c.kind in ("mcp_server", "mcp_tool")}
    fam_usage: dict[str, int] = {}
    for tool, n in usage.items():
        if tool.startswith("mcp__"):
            srv = tool.split("__")[1] if "__" in tool[5:] + "__" else ""
            fam = provider_family.get(srv)
            if fam:
                fam_usage[fam] = fam_usage.get(fam, 0) + n
    safe = unsafe = 0
    per_family: dict[str, dict[str, int]] = {}
    try:
        lines = (Path(ws_root) / SHADOW_LEDGER).read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("op") != "hide":
            continue
        fam = ev.get("family", "")
        slot = per_family.setdefault(fam, {"hides": 0, "later_used": 0})
        slot["hides"] += 1
        if fam_usage.get(fam, 0) > 0:
            slot["later_used"] += 1
            unsafe += 1
        else:
            safe += 1
    promotable = sorted(f for f, s in per_family.items() if s["later_used"] == 0 and s["hides"] > 0)
    return {
        "hides_scored": safe + unsafe,
        "safe": safe, "unsafe": unsafe,
        "per_family": per_family,
        "promotable": promotable,
        "verdict": ("promote" if unsafe == 0 and safe > 0 else
                    "hold" if safe + unsafe else "no-data"),
    }


def render_reconcile(rep: dict[str, Any]) -> str:
    lines = [f"[ctx surface reconcile · phase={rep['phase']} · "
             f"{'ENFORCE' if rep['enforced'] else 'shadow'}]"]
    if rep["revealed"]:
        lines.append("  revealed: " + ", ".join(rep["revealed"]))
    if rep["required_protected"]:
        lines.append("  protected (active contract, never hidden): "
                     + ", ".join(rep["required_protected"]))
    if not rep["actions"]:
        lines.append("  no change recommended")
    for act in rep["actions"]:
        tok = f" (~{act['tokens']:,} tok)" if act["tokens"] else ""
        lines.append(f"  {act['op']:<6} {act['family']}{tok} — {act['reason']}")
    if rep["applied"]:
        lines.append("  applied: " + ", ".join(rep["applied"]))
    elif rep["actions"]:
        lines.append("  shadow only — pass --enforce to apply (referee-gate first)")
    return "\n".join(lines)


__all__ = [
    "Action", "Signals", "reconcile", "signals_from_workspace", "run_reconcile",
    "referee", "apply_actions", "infer_phase", "required_families",
    "render_reconcile", "INTENT_TRIGGERS", "SHADOW_LEDGER",
]
