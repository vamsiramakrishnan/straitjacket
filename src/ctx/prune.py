"""Prune at setup: defer the capabilities this repository does not use.

The capability surface is the input side of containment (docs/CAPABILITY-
SURFACE.md): every skill, agent, MCP server and instruction file the host
loads is re-sent every turn, whether or not the task ever touches it. The
measuring side (`ctx surface audit`) and the enforcing side (`ctx surface
compile --profile`) have both shipped; what was missing was the step that
runs them *at setup*, with a decision rule, so a freshly harnessed repository
starts lean instead of being audited later.

`ctx prune` (and `ctx setup --prune`) does exactly that, and on a terminal
it does it as a conversation: one selector per group (MCP servers, skills,
agents) listing each capability with its tokens per turn, authority,
observed use and the rule's recommendation, then the hosts to compile for,
then a confirmation. Enter at every prompt gives the rule's answer, so the
non-interactive result (``--yes``) is exactly what a user who accepted every
default would get. What the user chose is recorded in the receipt beside
what the rule recommended.

1. audit the discretionary surface and read each capability's recommended
   disclosure level (``surface.recommended_level``: read-only or used stays
   visible; unused, remote-write or destructive is deferred);
2. keep the kernel (ctx's own bounded tools, policy, repository instructions)
   and every L0/L1 capability, defer the rest;
3. compile that selection into each host's minimal native config with the
   existing emitters (``surface_profiles``), so enforcement is the boundary
   every host respects: what the host is told about at launch;
4. write a receipt with the per-turn tokens before and after, per host.

Nothing is deleted. A deferred capability is not loaded by the compiled
config and stays reachable through the gateway (`ctx surface gateway`) or by
re-running with ``--keep <id>``. The rule is the same one the audit already
recommends; prune only makes it the default at the moment the harness is
installed, and it is idempotent: a second run on an unchanged repository
produces the same plan and the same files.

Repository shape is recorded in the receipt (languages by file count, test
runner markers) so a reader can see what the plan was decided against; it
does not yet drive the decisions.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path
from typing import Any

from ctx import surface
from ctx.surface_profiles import COMPILE_DIR, HOSTS, Profile, compile_profile

PRUNE_SCHEMA = "ctx.prune/v1"
RECEIPT_NAME = "prune-receipt.json"

#: Disclosure levels that stay visible. L0 is always-on, L1 is a compact
#: index; L2 and above are "load on a task signal", which at setup time
#: means: not loaded.
KEEP_LEVELS = frozenset({"L0", "L1"})

_RUNNER_MARKERS = (
    ("pytest", ("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini", "conftest.py")),
    ("node", ("package.json",)),
    ("go", ("go.mod",)),
    ("cargo", ("Cargo.toml",)),
    ("make", ("Makefile",)),
)


def profile_repo(ws_root: Path | str) -> dict[str, Any]:
    """What the repository looks like, for the receipt: languages by file
    count (top five) and the test-runner markers present. Bounded walk
    through the workspace's own listing when available."""
    root = Path(ws_root)
    exts: Counter[str] = Counter()
    try:
        from ctx.workspace import resolve_workspace

        ws = resolve_workspace(str(root))
        files = ws.list_files()
    except Exception:
        files = [str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()][:20000]
    for rel in files:
        suffix = Path(rel).suffix.lower()
        if suffix:
            exts[suffix] += 1
    runners = [name for name, markers in _RUNNER_MARKERS
               if any((root / m).exists() for m in markers)]
    return {
        "files": len(files),
        "languages": [{"ext": e, "files": n} for e, n in exts.most_common(5)],
        "runners": runners,
    }


def plan_prune(ws_root: Path | str, *, probe_mcp: bool = False,
               keep: tuple[str, ...] = ()) -> dict[str, Any]:
    """Decide, without writing anything: every capability, its level, and
    whether it stays. ``keep`` forces named capability ids to stay."""
    from ctx.surface_profiles import _is_kernel

    audit = surface.audit(ws_root, probe_mcp=probe_mcp)
    decisions: list[dict[str, Any]] = []
    deferred: set[str] = set()
    for rec in audit["records"]:
        cap = surface.Capability(**{k: (tuple(v) if isinstance(v, list) else v)
                                    for k, v in rec.items() if k != "recommended_level"})
        level = str(rec.get("recommended_level") or surface.recommended_level(cap))
        if _is_kernel(cap):
            why, kept = "kernel", True
        elif cap.id in keep:
            why, kept = "kept by request", True
        elif level in KEEP_LEVELS:
            why, kept = f"{level}: visible", True
        else:
            why, kept = f"{level}: unused or above authority; deferred", False
            deferred.add(cap.id)
        decisions.append({
            "id": cap.id, "kind": cap.kind, "tokens": cap.tokens,
            "authority": cap.authority, "invocations": cap.invocations,
            "level": level, "keep": kept, "why": why,
        })
    before = sum(d["tokens"] for d in decisions)
    after = sum(d["tokens"] for d in decisions if d["keep"])
    return {
        "schema": PRUNE_SCHEMA,
        "repo": profile_repo(ws_root),
        "decisions": decisions,
        "deferred": sorted(deferred),
        "tokens_per_turn": {"before": before, "after": after, "saved": before - after},
        "profile": Profile(
            "prune", exclude=frozenset(deferred),
            description="setup-time prune: defer what is unused or above authority",
        ),
    }


def run_prune(ws_root: Path | str, *, hosts: tuple[str, ...] = ("claude",),
              apply: bool = False, probe_mcp: bool = False,
              keep: tuple[str, ...] = ()) -> dict[str, Any]:
    """Plan, compile per host, and with ``apply`` write the host configs and
    the receipt. Returns the full report."""
    plan = plan_prune(ws_root, probe_mcp=probe_mcp, keep=keep)
    per_host: dict[str, Any] = {}
    for host in hosts:
        if host not in HOSTS:
            per_host[host] = {"error": f"no compile emitter for host {host!r}"}
            continue
        rep = compile_profile(ws_root, "prune", host=host, apply=apply,
                              probe_mcp=probe_mcp, profile=plan["profile"])
        per_host[host] = {k: v for k, v in rep.items() if k != "files"}
    report = {
        "schema": PRUNE_SCHEMA,
        "ts": int(time.time()),
        "applied": bool(apply),
        "repo": plan["repo"],
        "decisions": plan["decisions"],
        "deferred": plan["deferred"],
        "tokens_per_turn": plan["tokens_per_turn"],
        "hosts": per_host,
    }
    if apply:
        path = Path(ws_root) / COMPILE_DIR / RECEIPT_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        report["receipt"] = str(path.relative_to(Path(ws_root)))
    return report


# ------------------------------------------------------------ interactive
#: Groups a user is asked about, in the order asked. Kernel kinds (policy,
#: repo instructions, hooks, ctx's own tools) are never asked: they are what
#: steers the host each turn.
ASK_KINDS: tuple[tuple[str, str], ...] = (
    ("mcp_server", "MCP servers"),
    ("mcp_tool", "MCP tools (measured with --probe-mcp)"),
    ("skill", "skills"),
    ("agent", "agents"),
)
#: Past this many items in one group, the selector asks one yes/no question
#: instead of listing them all: a wall of forty lines is not a choice.
_LIST_CAP = 40


def parse_selection(answer: str, n: int, default: set[int]) -> set[int] | str:
    """Turn one selector answer into the set of 1-based indexes to defer.

    ``""`` (Enter) -> the default; ``all`` / ``none``; ``1,3-5`` -> exactly
    those; ``+2`` / ``-3`` -> the default with 2 added / 3 removed; ``?N``
    -> the string ``"explain:N"``. Anything else -> ``"invalid"``.
    """
    text = answer.strip().lower()
    if not text:
        return set(default)
    if text in ("all", "a"):
        return set(range(1, n + 1))
    if text in ("none", "n", "keep all"):
        return set()
    if text.startswith("?"):
        rest = text[1:].strip()
        return f"explain:{rest}" if rest.isdigit() and 1 <= int(rest) <= n else "invalid"
    chosen = set(default) if text[0] in "+-" else set()
    for part in text.replace(" ", ",").split(","):
        if not part:
            continue
        sign = ""
        if part[0] in "+-":
            sign, part = part[0], part[1:]
        if "-" in part:
            lo, _, hi = part.partition("-")
            if not (lo.isdigit() and hi.isdigit()):
                return "invalid"
            idxs = set(range(int(lo), int(hi) + 1))
        elif part.isdigit():
            idxs = {int(part)}
        else:
            return "invalid"
        if any(i < 1 or i > n for i in idxs):
            return "invalid"
        if sign == "-":
            chosen -= idxs
        else:
            chosen |= idxs
    return chosen


def _describe(d: dict[str, Any], idx: int) -> str:
    used = "never used" if d["invocations"] == 0 else (
        f"used {d['invocations']}x" if d["invocations"] > 0 else "use not measurable")
    mark = "defer" if not d["keep"] else "keep "
    return (f"  {idx:>2}. [{mark}] {d['id'][:44]:44} {d['tokens']:>6} tok/turn  "
            f"{d['authority']:<12} {used}")


def interactive_prune(ws_root: Path | str, *, ask, say, hosts: tuple[str, ...] = ("claude",),
                      probe_mcp: bool = False, keep: tuple[str, ...] = ()) -> dict[str, Any]:
    """Walk the user through the prune decision, then apply what they chose.

    ``ask(prompt) -> str`` and ``say(text)`` are injected so the flow is
    testable without a terminal. Returns the report (``applied`` False when
    the user declined at the confirmation).
    """
    plan = plan_prune(ws_root, probe_mcp=probe_mcp, keep=keep)
    decisions = plan["decisions"]
    t = plan["tokens_per_turn"]
    say(f"[ctx prune] {len(decisions)} capabilities on the surface · {t['before']:,} tok/turn · "
        f"rule recommends deferring {len(plan['deferred'])} ({t['saved']:,} tok/turn)")
    kernel = [d for d in decisions if d["why"] == "kernel"]
    if kernel:
        say(f"kept without asking (kernel: ctx's own tools, policy, repo instructions): "
            f"{len(kernel)} · {sum(d['tokens'] for d in kernel):,} tok/turn")
    say("At each prompt, Enter accepts the recommendation; 'all' / 'none'; '1,3-5' picks "
        "exactly those; '+2' or '-3' adjusts the recommendation; '?2' explains one item.")
    chosen_defer: set[str] = set()
    for kind, label in ASK_KINDS:
        group = [d for d in decisions if d["kind"] == kind and d["why"] != "kernel"]
        if not group:
            continue
        say("")
        say(f"{label}: {len(group)} · {sum(d['tokens'] for d in group):,} tok/turn")
        default = {i for i, d in enumerate(group, 1) if not d["keep"]}
        if len(group) > _LIST_CAP:
            unused = [d for d in group if not d["keep"]]
            say(f"  (more than {_LIST_CAP}; {len(unused)} are unused or above authority)")
            while True:
                a = ask(f"  defer those {len(unused)}? [Y/n] ").strip().lower()
                if a in ("", "y", "yes"):
                    chosen_defer.update(d["id"] for d in unused)
                    break
                if a in ("n", "no"):
                    break
            continue
        for i, d in enumerate(group, 1):
            say(_describe(d, i))
        while True:
            sel = parse_selection(ask("  defer which? [Enter = as marked] "), len(group), default)
            if isinstance(sel, str):
                if sel.startswith("explain:"):
                    d = group[int(sel.split(":")[1]) - 1]
                    say(f"    {d['id']}: {d['kind']} · level {d['level']} · {d['why']}")
                else:
                    say("    not understood; try Enter, all, none, 1,3-5, +2, -3 or ?2")
                continue
            chosen_defer.update(group[i - 1]["id"] for i in sel)
            break
    # Fold the user's answers back into the decisions.
    for d in decisions:
        if d["why"] == "kernel":
            continue
        wanted = d["id"] in chosen_defer
        if wanted != (not d["keep"]):
            d["keep"] = not wanted
            d["by"] = "user"
            d["why"] = ("deferred by user" if wanted else "kept by user") + f" (rule: {d['why']})"
        else:
            d["by"] = "rule"
    deferred = sorted(d["id"] for d in decisions if not d["keep"])
    after = sum(d["tokens"] for d in decisions if d["keep"])
    say("")
    say(f"plan: defer {len(deferred)} · {t['before']:,} -> {after:,} tok/turn")
    while True:
        a = ask(f"compile for which hosts? [Enter = {','.join(hosts)}] ").strip().lower()
        if not a:
            chosen_hosts = tuple(hosts)
            break
        picked = tuple(h.strip() for h in a.replace(" ", ",").split(",") if h.strip())
        if picked and all(h in HOSTS for h in picked):
            chosen_hosts = picked
            break
        say(f"    hosts are {', '.join(HOSTS)}")
    while True:
        a = ask("write the compiled host configs and the receipt? [Y/n] ").strip().lower()
        if a in ("", "y", "yes"):
            apply = True
            break
        if a in ("n", "no"):
            apply = False
            break
    profile = Profile("prune", exclude=frozenset(deferred),
                      description="interactive prune: what the user chose to defer")
    per_host: dict[str, Any] = {}
    for host in chosen_hosts:
        rep = compile_profile(ws_root, "prune", host=host, apply=apply,
                              probe_mcp=probe_mcp, profile=profile)
        per_host[host] = {k: v for k, v in rep.items() if k != "files"}
    report = {
        "schema": PRUNE_SCHEMA, "ts": int(time.time()), "applied": apply,
        "interactive": True, "repo": plan["repo"], "decisions": decisions,
        "deferred": deferred,
        "tokens_per_turn": {"before": t["before"], "after": after, "saved": t["before"] - after},
        "hosts": per_host,
    }
    if apply:
        path = Path(ws_root) / COMPILE_DIR / RECEIPT_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        report["receipt"] = str(path.relative_to(Path(ws_root)))
    say(render_prune(report))
    return report


def render_prune(rep: dict[str, Any]) -> str:
    t = rep["tokens_per_turn"]
    mode = "applied" if rep.get("applied") else (
        "declined; nothing written" if rep.get("interactive") else "preview (nothing written; add --apply)")
    out = [f"[ctx prune · {mode}]"]
    repo = rep.get("repo") or {}
    langs = ", ".join(f"{d['ext']} x{d['files']}" for d in repo.get("languages", [])) or "-"
    out.append(f"repo: {repo.get('files', 0)} files · {langs} · runners: "
               f"{', '.join(repo.get('runners') or []) or '-'}")
    out.append(f"surface: {t['before']:,} tok/turn before · {t['after']:,} after · "
               f"{t['saved']:,} deferred")
    kept = [d for d in rep["decisions"] if d["keep"]]
    gone = [d for d in rep["decisions"] if not d["keep"]]
    out.append(f"kept {len(kept)} · deferred {len(gone)}")
    for d in gone:
        out.append(f"  - {d['id']:40} {d['tokens']:>6} tok  {d['level']} {d['authority']}"
                   + ("" if d["invocations"] < 0 else f"  used {d['invocations']}x"))
    for host, h in (rep.get("hosts") or {}).items():
        if "error" in h:
            out.append(f"{host}: {h['error']}")
            continue
        tk = h.get("tokens", {})
        out.append(f"{host}: servers kept {len(h.get('servers_kept', []))} · dropped "
                   f"{len(h.get('servers_dropped', []))} · tokens {tk.get('before', 0):,} -> "
                   f"{tk.get('after_server', 0):,} (gateway {tk.get('after_gateway', 0):,})"
                   + (f" · wrote {', '.join(h['written'])}" if h.get("written") else ""))
        if h.get("issues"):
            out.append("  issues: " + "; ".join(h["issues"][:3]))
        if h.get("launch"):
            out.append(f"  launch: {h['launch']}")
    if rep.get("receipt"):
        out.append(f"receipt: {rep['receipt']}")
    if not gone:
        out.append("nothing to defer: every capability is used, read-only, or kernel")
    return "\n".join(out)


__all__ = ["PRUNE_SCHEMA", "KEEP_LEVELS", "ASK_KINDS", "plan_prune", "run_prune",
           "interactive_prune", "parse_selection", "render_prune", "profile_repo"]
