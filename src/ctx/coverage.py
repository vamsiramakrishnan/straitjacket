"""`ctx coverage`: which tool calls ctx governs, by which route, and which escape.

Two truths, printed side by side and never blended:

* **static** — the governance table (:mod:`ctx.governance`) joined with what
  each host's registration actually hooks (the Claude Code settings builder,
  the Codex and Antigravity plugin JSON, the native plugins that forward
  every tool). A route whose stage is not registered is printed as
  ``UNHOOKED`` rather than assumed.
* **observed** — the coverage ledger the hook appends to on every hooked call
  (``.ctx-session-reads/coverage.jsonl``): calls per tool by route and
  decision, and the tools that put the most *ungoverned* bytes into context
  (results that were observed, captured without replacement, exempt, failed,
  or whose replacement the host would ignore).

Deterministic and bounded: rows are sorted, the observed tables are capped
with the omission declared, and nothing here calls a model.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from ctx import governance as gov

SCHEMA = "ctx.coverage-report/v1"
_TOP = 20

#: Post-stage decisions whose bytes reached the context without any bound.
UNGOVERNED_DECISIONS = frozenset(
    {"observed", "captured", "exempt", "failed", "replace_unshaped", "no_result",
     "media_untouched"}
)


# ------------------------------------------------------------ registrations
def _plugin_json(name: str) -> dict[str, Any]:
    from ctx.installer import _template_dir

    path = _template_dir(name, sentinel="hooks.json") / "hooks.json"
    return json.loads(path.read_text(encoding="utf-8"))


def registrations() -> dict[str, dict[str, Any]]:
    """Per host: a predicate per stage saying whether a tool name is hooked.

    Built from the real registration sources, so the report can never claim a
    stage the installed configuration does not have.
    """
    from ctx.installer import claude_hook_settings

    regs: dict[str, dict[str, Any]] = {}
    claude = claude_hook_settings("ctx")["hooks"]
    pre = claude["PreToolUse"][0]["matcher"]
    post = claude["PostToolUse"][0]["matcher"]
    regs["claude-code"] = {
        "pre": _claude_matches(pre), "post": _claude_matches(post),
        "pre_matcher": pre, "post_matcher": post,
    }
    codex = _plugin_json("codex")["hooks"]
    regs["claude-code"]["prompt"] = "UserPromptSubmit" in claude
    regs["codex"] = {
        "prompt": "UserPromptSubmit" in codex,
        "pre": _codex_matches(codex["PreToolUse"][0].get("matcher", "*")),
        "post": _codex_matches(codex["PostToolUse"][0].get("matcher", "*")),
        "pre_matcher": codex["PreToolUse"][0].get("matcher", "*"),
        "post_matcher": codex["PostToolUse"][0].get("matcher", "*"),
    }
    agy = _plugin_json("antigravity")["ctx-harness"]
    regs["antigravity"] = {
        "pre": _agy_matches(agy["PreToolUse"][0]["matcher"]),
        "post": _agy_matches(agy["PostToolUse"][0]["matcher"]),
        "pre_matcher": agy["PreToolUse"][0]["matcher"],
        "post_matcher": agy["PostToolUse"][0]["matcher"],
    }
    for host in ("hermes", "omp", "opencode", "dsh"):
        regs[host] = {"pre": lambda _n: True, "post": lambda _n: True,
                      "pre_matcher": "(plugin forwards every tool)",
                      "post_matcher": "(plugin forwards every tool)"}
    return regs


def _claude_matches(matcher: str):
    """Claude Code's matcher semantics (hooks reference, Matcher patterns)."""
    if matcher in ("", "*"):
        return lambda _n: True
    if re.fullmatch(r"[A-Za-z0-9_\- ,|]+", matcher):
        names = {p.strip() for p in re.split(r"[|,]", matcher) if p.strip()}
        return lambda n: n in names
    rx = re.compile(matcher)
    return lambda n: rx.search(n) is not None


def _codex_matches(matcher: str):
    if matcher in ("", "*"):
        return lambda _n: True
    rx = re.compile(matcher)
    return lambda n: rx.search(n) is not None


def _agy_matches(matcher: str):
    """Antigravity: a regex where a bare name matches exactly that tool."""
    if matcher in ("", "*"):
        return lambda _n: True
    rx = re.compile(matcher)
    return lambda n: rx.fullmatch(n) is not None


# ------------------------------------------------------------------ static
def _caps(host: str) -> dict[str, bool]:
    from ctx.hook import DIALECT_CAPS

    return DIALECT_CAPS.get(host, {})


def static_rows(host: str, regs: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """One row per listed native tool, plus the two catch-all rows."""
    regs = regs or registrations()
    reg = regs[host]
    caps = _caps(host)
    rows: list[dict[str, Any]] = []
    names = list(gov.NATIVE.get(host, {}).items())
    names.append(("mcp__<server>__<tool>", "mcp"))
    from ctx.hook import _governed_kind

    unlisted = "<unlisted tool>"
    names.append((unlisted, _governed_kind("x_unlisted_tool_x", host)))
    for name, kind in names:
        pre, post = gov.stage_routes(kind, caps, host=host, name=name)
        probe = "mcp__server__tool" if kind == "mcp" else (
            "x_unlisted_tool_x" if name == unlisted else name)
        pre_hooked = bool(reg["pre"](probe))
        post_hooked = bool(reg["post"](probe))
        if pre and pre != "exempt" and not pre_hooked:
            pre = "UNHOOKED"
        if post != "exempt" and not post_hooked:
            post = "UNHOOKED"
        rows.append({"tool": name, "kind": kind, "pre": pre, "post": post,
                     "pre_hooked": pre_hooked, "post_hooked": post_hooked})
    return rows


def ungoverned_static(rows: list[dict[str, Any]]) -> list[str]:
    """Tools neither stage governs (no route on any hooked stage)."""
    return sorted(r["tool"] for r in rows
                  if r["post"] == "UNHOOKED" and r["pre"] in (None, "UNHOOKED"))


def _fmt_route(route: str | None) -> str:
    return route or "-"


def render_static(host: str, regs: dict[str, dict[str, Any]] | None = None) -> list[str]:
    rows = static_rows(host, regs)
    caps = _caps(host)
    yn = {True: "yes", False: "no"}
    rewrite = yn[bool(caps.get('input_substitution'))]
    if caps.get("input_substitution") and host in gov.REWRITABLE:
        rewrite = ", ".join(sorted(gov.REWRITABLE[host])) + " only"
    if host in gov.REWRITE_WITHHELD:
        rewrite = "withheld"
    out = [f"{host}  (rewrite input: {rewrite}, "
           f"replace output: {yn[bool(caps.get('output_substitution'))]})"]
    groups: dict[tuple[str, str, str], list[str]] = {}
    for r in rows:
        if r["pre"] == "exempt":
            key = ("exempt", "", r["kind"])
        else:
            key = (_fmt_route(r["pre"]), r["post"], r["kind"] if r["kind"] in ("edit", "skill") else "")
        groups.setdefault(key, []).append(r["tool"])
    order = {"rewrite": 0, "bound": 1, "deny_steer": 2, "observe": 3, "-": 4,
             "UNHOOKED": 5, "exempt": 9}
    for (pre, post, kind) in sorted(groups, key=lambda k: (order.get(k[0], 6), k[1], k[2])):
        tools = ", ".join(groups[(pre, post, kind)])
        if pre == "exempt":
            out.append(f"  exempt                           {tools}")
            out.append(f"      why: {gov.exempt_reason(kind)}")
            continue
        out.append(f"  pre={pre:<11} post={post:<12} {tools}")
        if kind in gov.OBSERVE_REASONS and post == "observe":
            out.append(f"      why: {gov.OBSERVE_REASONS[kind]}")
    loose = ungoverned_static(rows)
    if loose:
        out.append("  UNGOVERNED (no hooked stage): " + ", ".join(loose))
    if host in gov.APPROVAL:
        out.append(f"  approval: {gov.APPROVAL[host]}")
    out.append("  footprint in the workspace (.ctx-session-reads/): "
               + ", ".join(gov.WORKSPACE_FOOTPRINT))
    out.extend(render_prompt(host, regs))
    out.append("  cannot govern:")
    for line in gov.LIMITS.get(host, ()):
        out.append(f"    - {line}")
    out.append("  media reads:")
    for line in gov.MEDIA_READS.get(host, ()):
        out.append(f"    - {line}")
    return out


def prompt_row(host: str, regs: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """What happens to an oversized prompt on ``host`` (the prompt gate)."""
    regs = regs or registrations()
    info = gov.PROMPT_GATE.get(host)
    if info is None or not regs.get(host, {}).get("prompt"):
        return {"gate": False, "limit": "no ctx prompt gate: an oversized prompt reaches the "
                "model unmodified"}
    return {"gate": True, **info}


def render_prompt(host: str, regs: dict[str, dict[str, Any]] | None = None) -> list[str]:
    row = prompt_row(host, regs)
    if not row["gate"]:
        return [f"  prompt (oversized paste): {row['limit']}"]
    return [
        f"  prompt (oversized paste): {row['event']} hook, [intake] mode advise (default) | block | off",
        f"      advise: {row['advise']}",
        f"      block:  {row['block']}",
        f"      limit:  {row['limit']}",
        f"      evidence: {row['evidence']}",
    ]


def installed_drift(root) -> list[str]:
    """Notes when this workspace's installed Claude matchers predate the table."""
    notes: list[str] = []
    path = os.path.join(str(root), ".claude", "settings.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return notes
    from ctx.installer import claude_hook_settings

    want = claude_hook_settings("ctx")["hooks"]
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict):
        return notes
    for stage, marker in (("PreToolUse", "pre-tool-use"), ("PostToolUse", "post-tool-use"),
                          ("PostToolUseFailure", "post-tool-use-failure")):
        groups = hooks.get(stage, [])
        ours = [g.get("matcher") for g in (groups if isinstance(groups, list) else [])
                if isinstance(g, dict) and any(
                    str(h.get("command", "")).endswith(f"hook claude-code {marker}")
                    for h in g.get("hooks", []) if isinstance(h, dict))]
        expected = want[stage][0]["matcher"]
        if not ours:
            notes.append(f".claude/settings.json has no ctx {stage} hook (expected matcher {expected!r}); "
                         "rerun ctx setup")
        elif expected not in ours:
            notes.append(f".claude/settings.json {stage} matcher {ours[0]!r} predates the governance "
                         f"table (expected {expected!r}); rerun ctx setup, which updates ctx-owned entries in place")
    return notes


# ---------------------------------------------------------------- observed
#: Hard ceiling on what the report reads per generation, independent of how
#: the ledger got there (the hook rotates at hook.COVERAGE_MAX_BYTES).
_READ_CAP_BYTES = 1024 * 1024


def read_ledger(root) -> list[dict[str, Any]]:
    """Rows from the kept generation and the current file, oldest first.

    Read cost is bounded by two generations of at most ``_READ_CAP_BYTES``
    each (the tail is read if a file is somehow larger), never by total
    history.
    """
    from ctx.sessiondir import session_reads_dir

    rows: list[dict[str, Any]] = []
    for name in ("coverage.1.jsonl", "coverage.jsonl"):
        try:
            with open(session_reads_dir(root, name), "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - _READ_CAP_BYTES))
                data = fh.read(_READ_CAP_BYTES)
        except OSError:
            continue
        lines = data.split(b"\n")
        if size > _READ_CAP_BYTES:
            lines = lines[1:]  # first line is partial
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("schema") == "ctx.coverage/v1":
                rows.append(row)
    return rows


def _identity(row: dict[str, Any]) -> str:
    if row.get("kind") == "mcp" and row.get("mcp_server"):
        return f"mcp:{row['mcp_server']}/{row.get('mcp_tool', '')}"
    if row.get("kind") == "skill" and row.get("skill"):
        return f"{row.get('tool', '')}:{row['skill']}"
    return str(row.get("tool", ""))


def observed(rows: list[dict[str, Any]], *, host: str | None = None,
             session: str | None = None) -> dict[str, Any]:
    """Aggregate ledger rows. ``session='last'`` picks the newest session."""
    if host:
        rows = [r for r in rows if r.get("host") == host]
    sessions = sorted({str(r.get("session")) for r in rows if r.get("session")})
    if session == "last":
        latest: dict[str, int] = {}
        for r in rows:
            s = r.get("session")
            if s:
                latest[s] = max(latest.get(s, 0), int(r.get("ts") or 0))
        session = max(latest, key=lambda s: (latest[s], s)) if latest else None
    if session and session != "all":
        rows = [r for r in rows if r.get("session") == session]
    calls: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    escaped: dict[str, dict[str, Any]] = {}
    media: dict[str, int] = {}
    for r in rows:
        note = str(r.get("note") or "")
        if r.get("stage") == "pre" and note.startswith("media:"):
            mkey = f"{r.get('host', '')} {_identity(r)} {note}"
            media[mkey] = media.get(mkey, 0) + 1
        key = (str(r.get("host", "")), _identity(r), str(r.get("stage", "")), str(r.get("route", "")))
        agg = calls.setdefault(key, {"calls": 0, "decisions": {}, "raw_bytes": 0, "emitted_bytes": 0})
        agg["calls"] += 1
        d = str(r.get("decision", ""))
        agg["decisions"][d] = agg["decisions"].get(d, 0) + 1
        agg["raw_bytes"] += int(r.get("raw_bytes") or 0)
        agg["emitted_bytes"] += int(r.get("emitted_bytes") or 0)
        if r.get("stage") in ("post", "post_failure") and d in UNGOVERNED_DECISIONS:
            e = escaped.setdefault(f"{r.get('host', '')} {_identity(r)}",
                                   {"bytes": 0, "calls": 0, "why": {}})
            e["bytes"] += int(r.get("emitted_bytes") or 0)
            e["calls"] += 1
            e["why"][d] = e["why"].get(d, 0) + 1
    by_calls = [
        {"host": k[0], "tool": k[1], "stage": k[2], "route": k[3], **v}
        for k, v in sorted(calls.items(), key=lambda kv: (-kv[1]["calls"], kv[0]))
    ]
    top_escaped = [
        {"tool": k, **v} for k, v in sorted(escaped.items(), key=lambda kv: (-kv[1]["bytes"], kv[0]))
    ]
    return {"rows": len(rows), "session": session or "all", "sessions": len(sessions),
            "calls": by_calls, "ungoverned": top_escaped,
            "media": [{"key": k, "calls": v} for k, v in sorted(media.items())]}


def render_observed(obs: dict[str, Any], top: int = _TOP) -> list[str]:
    out = [f"observed  (.ctx-session-reads/coverage.jsonl: {obs['rows']} rows, "
           f"session {obs['session']}, {obs['sessions']} session(s) on record; "
           "history is bounded to the current and one rotated generation)"]
    if not obs["rows"]:
        out.append("  no hooked calls recorded yet")
        return out
    out.append(f"  {'host':<12} {'tool':<34} {'stage':<12} {'route':<12} {'calls':>5}  "
               f"{'raw B':>9} {'emitted B':>9}  decisions")
    for c in obs["calls"][:top]:
        dec = ",".join(f"{k}={v}" for k, v in sorted(c["decisions"].items()))
        out.append(f"  {c['host']:<12} {c['tool'][:34]:<34} {c['stage']:<12} {c['route']:<12} "
                   f"{c['calls']:>5}  {c['raw_bytes']:>9} {c['emitted_bytes']:>9}  {dec}")
    if len(obs["calls"]) > top:
        out.append(f"  ... {len(obs['calls']) - top} more rows omitted (ctx coverage --json lists all)")
    out.append("  most ungoverned bytes into context (observed, exempt, captured-not-replaced, "
               "failed, or a replacement the host ignores):")
    if not obs["ungoverned"]:
        out.append("    none recorded")
    for e in obs["ungoverned"][:10]:
        why = ",".join(f"{k}={v}" for k, v in sorted(e["why"].items()))
        out.append(f"    {e['bytes']:>9} B  {e['calls']:>4} calls  {e['tool']}  ({why})")
    if len(obs["ungoverned"]) > 10:
        out.append(f"    ... {len(obs['ungoverned']) - 10} more omitted (--json lists all)")
    if obs.get("media"):
        out.append("  media reads (host tool decision: calls):")
        for m in obs["media"][:10]:
            out.append(f"    {m['key']}: {m['calls']}")
        if len(obs["media"]) > 10:
            out.append(f"    ... {len(obs['media']) - 10} more omitted (--json lists all)")
    return out


# ------------------------------------------------------------------ report
def report(root, *, host: str | None = None, session: str | None = None) -> dict[str, Any]:
    regs = registrations()
    hosts = [host] if host else list(gov.HOSTS)
    data: dict[str, Any] = {"schema": SCHEMA, "hosts": {}}
    for h in hosts:
        rows = static_rows(h, regs)
        data["hosts"][h] = {
            "caps": _caps(h),
            "rewrite_only": sorted(gov.REWRITABLE.get(h, ())),
            "pre_matcher": regs[h]["pre_matcher"],
            "post_matcher": regs[h]["post_matcher"],
            "tools": rows,
            "ungoverned": ungoverned_static(rows),
            "cannot_govern": list(gov.LIMITS.get(h, ())),
            "approval": gov.APPROVAL.get(h, ""),
            "media_reads": list(gov.MEDIA_READS.get(h, ())),
            "prompt": prompt_row(h, regs),
        }
    data["installed_drift"] = installed_drift(root) if (host in (None, "claude-code")) else []
    data["guard_mode"] = _guard_mode(root)
    data["prompt_policy"] = _prompt_policy(root)
    data["observed"] = observed(read_ledger(root), host=host, session=session)
    return data


def _prompt_policy(root) -> dict[str, Any]:
    """The prompt gate's configured mode and threshold in this workspace."""
    try:
        from ctx.promptgate import policy

        mode, threshold = policy(str(root))
        return {"mode": mode, "threshold_bytes": threshold}
    except Exception:
        return {"mode": "unknown", "threshold_bytes": None}


def _guard_mode(root) -> str:
    try:
        from ctx.hook import _load_guard_policy

        return str(_load_guard_policy(str(root)).get("mode", "guarded"))
    except Exception:
        return "unknown"


def render(data: dict[str, Any]) -> str:
    out = ["ctx coverage: governance table (static) and coverage ledger (observed)", "",
           "routes: rewrite | bound | deny_steer | output_gate | observe | exempt; "
           "pre=- means the input side has nothing to do", ""]
    for h, info in data["hosts"].items():
        out.extend(render_static(h))
        out.append("")
    pp = data.get("prompt_policy") or {}
    if pp.get("mode"):
        out.append(f"prompt gate in this workspace: [intake] mode = {pp['mode']!r}, stores a paste or "
                   f"prompt over {pp.get('threshold_bytes')} bytes (stored prompt text is kept verbatim "
                   "in ctx's local state for [store] retention_days; mode off stores nothing)")
    if data.get("guard_mode") == "strict":
        out.append("note: [guard] mode = \"strict\" has no code path of its own; it behaves as \"guarded\".")
    elif data.get("guard_mode") == "advisory":
        out.append("note: [guard] mode = \"advisory\": every route above is switched off in this workspace.")
    for note in data.get("installed_drift", []):
        out.append("note: " + note)
    if out[-1] != "":
        out.append("")
    out.extend(render_observed(data["observed"]))
    return "\n".join(out) + "\n"
