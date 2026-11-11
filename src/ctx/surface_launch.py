"""The launch-time surface plan: progressive disclosure applied when
``ctx wrap claude`` starts a session, not only when someone runs an audit.

    discover (workspace + user scope) → price honestly → choose the cheapest
    disclosure mechanism the host already has → apply it through ephemeral
    launch channels only → leave a receipt

Doctrine, in the order it is applied:

* **Host first.** Claude Code already defers MCP tool schemas (tool search)
  and lists skills by name + description only. Where those mechanisms hold,
  ctx interposes nothing. ``auto`` never routes MCP through the gateway.
* **Evidence, never a guess.** A skill is narrowed to ``name-only`` only when
  this project's own transcripts show zero invocations of it. No transcripts
  at all is no evidence, and changes nothing. Nothing is turned ``off`` in
  ``auto``; ctx's own skill is never touched.
* **The caller wins.** ``--mcp-config``, ``--strict-mcp-config``, a caller
  ``--settings``, the deferral/skill environment variables, or a
  ``skillOverrides`` entry the user already set: that dimension (or skill) is
  left alone and the receipt says why.
* **Ephemeral.** Everything rides the temp settings file, extra flags and
  temp files in a private temp directory removed after the session. The only
  thing written to the workspace is the documented receipt
  (``.ctx-surface/wrap-receipt.json``); nothing is written under HOME.
* **Fail-open.** :func:`plan_claude_launch` may raise; the wrapper catches it
  and launches unchanged.

Every token figure is an estimate (~4 bytes/token) and is labelled as one.
"""

from __future__ import annotations

import json
import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ctx import surface

WRAP_SCHEMA = "ctx.surface.wrap/v1"
RECEIPT_FILE = ".ctx-surface/wrap-receipt.json"
#: ctx's own skill(s): never narrowed, whatever the evidence says.
CTX_SKILLS = frozenset({"ctx-harness"})
GATEWAY_NAME = "ctx-surface-gateway"

_FIRST_PARTY = ("api.anthropic.com",)
_TRUTHY = ("1", "true", "yes", "on")


@dataclass
class LaunchPlan:
    """What the wrapper adds to one launch. ``args`` are prepended to the
    agent args; ``settings`` merged into the temp settings file; ``env``
    added to the child environment only."""

    mode: str
    args: list[str] = field(default_factory=list)
    settings: dict[str, Any] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    receipt: dict[str, Any] = field(default_factory=dict)
    line: str = ""


# ------------------------------------------------------------------ caller
def _has_flag(args: list[str], flag: str) -> bool:
    return any(a == flag or a.startswith(flag + "=") for a in args)


def caller_overrides(agent_args: list[str], parent_env: dict[str, str]) -> dict[str, str]:
    """Dimension → why ctx must leave it alone (the caller already chose)."""
    out: dict[str, str] = {}
    if _has_flag(agent_args, "--mcp-config"):
        out["mcp"] = "caller passed --mcp-config"
    elif _has_flag(agent_args, "--strict-mcp-config"):
        out["mcp"] = "caller passed --strict-mcp-config"
    if _has_flag(agent_args, "--settings"):
        out["skills"] = "caller passed their own --settings"
    elif "SLASH_COMMAND_TOOL_CHAR_BUDGET" in parent_env:
        out["skills"] = "caller set SLASH_COMMAND_TOOL_CHAR_BUDGET"
    for var in ("ENABLE_TOOL_SEARCH", "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"):
        if var in parent_env:
            out["mcp-deferral"] = f"caller set {var}"
            break
    return out


# ------------------------------------------------------------------ MCP
def deferral_state(child_env: dict[str, str]) -> tuple[str, list[str]]:
    """Whether Claude Code will defer MCP tool schemas for this launch
    (see :func:`ctx.surface.claude_deferral_state`)."""
    return surface.claude_deferral_state(child_env)


def _server_cost(spec: surface.ServerSpec, cache: dict[str, Any]) -> dict[str, int] | None:
    """(inline, deferred) up-front estimate from the probe cache — never
    spawns. None when the server was never probed (or is remote)."""
    entry = cache.get(spec.name)
    if spec.transport != "stdio" or not isinstance(entry, dict):
        return None
    if entry.get("key") != surface._probe_cache_key(spec.argv):
        return None
    tools = [t for t in entry.get("tools") or [] if isinstance(t, dict)]
    instr = int(entry.get("instructions_tokens") or 0)
    inline = instr + sum(int(t.get("tokens") or 0) for t in tools)
    deferred = instr + sum(surface._tokens_of(f"mcp__{spec.name}__{t.get('name', '')}")
                           for t in tools)
    return {"inline": inline, "deferred": deferred}


def _claude_servers(ws_root: Path, parent_env: dict[str, str]) -> list[surface.ServerSpec]:
    return [s for s in surface.mcp_server_specs(ws_root, env=parent_env) if s.host == "claude"]


# ------------------------------------------------------------------ skills
def claude_skills(ws_root: Path, parent_env: dict[str, str]) -> list[dict[str, Any]]:
    """Skills Claude Code lists for this project: project ``.claude/skills``
    then user ``<config>/skills``. Each with its listing price."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    roots = [("project", Path(ws_root) / ".claude" / "skills"),
             ("user", surface.claude_config_dir(parent_env) / "skills")]
    for scope, base in roots:
        try:
            paths = sorted(base.glob("*/SKILL.md"))
        except OSError:
            continue
        for path in paths:
            text = surface._read_text(path)
            if text is None:
                continue
            info = surface.skill_listing(path, text)
            if info["name"] in seen:
                continue
            seen.add(info["name"])
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0.0
            out.append({**info, "scope": scope, "mtime": mtime})
    return out


def _settings_skill_overrides(ws_root: Path, parent_env: dict[str, str]) -> dict[str, str]:
    """skillOverrides the user already set in any settings file we can see."""
    out: dict[str, str] = {}
    for path in (surface.claude_config_dir(parent_env) / "settings.json",
                 Path(ws_root) / ".claude" / "settings.json",
                 Path(ws_root) / ".claude" / "settings.local.json"):
        doc = surface._json_doc(path)
        ov = doc.get("skillOverrides")
        if isinstance(ov, dict):
            out.update({str(k): str(v) for k, v in ov.items()})
    return out


def _invocations(name: str, usage: dict[str, Any]) -> int:
    n = 0
    for key, count in (usage.get("skills") or {}).items():
        if key == name or key.rsplit(":", 1)[-1] == name:
            n += int(count)
    return n


# ------------------------------------------------------------------ plan
def resolve_mode(requested: str | None, ws_root: Path) -> tuple[str, str]:
    """(mode, note). The command line may name a profile; configuration that
    travels with a repository (ctx.toml) may only select ``auto`` or
    ``off`` — a profile launch passes ``--mcp-config``, whose servers skip
    Claude Code's project trust prompt, so a cloned repository must never be
    able to choose one for you."""
    if requested:
        return requested.strip(), ""
    try:
        from ctx.config import load_config

        value = (load_config(ws_root).surface.wrap or "auto").strip()
    except Exception:
        return "auto", ""
    if value in ("auto", "off"):
        return value, ""
    return "auto", (f"ctx.toml [surface] wrap = {value!r} ignored: a profile can only be "
                    "chosen on the command line (--surface)")


DEFAULT_MIN_SESSIONS = 5


def min_sessions_floor(ws_root: Path) -> int:
    """``[surface] wrap_min_sessions``: sessions of usage evidence required
    before any skill is narrowed. A non-positive or unreadable value degrades
    to the default."""
    try:
        from ctx.config import load_config

        n = int(load_config(ws_root).surface.wrap_min_sessions)
    except Exception:
        return DEFAULT_MIN_SESSIONS
    return n if n >= 1 else DEFAULT_MIN_SESSIONS


def _gateway_command(ctx_exe: str) -> list[str]:
    return shlex.split(ctx_exe) or ["ctx"]


def _write_private(path: Path, content: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)


def plan_claude_launch(ws_root: Path, agent_args: list[str], *, mode: str,
                       child_env: dict[str, str], parent_env: dict[str, str] | None = None,
                       ctx_exe: str = "ctx", tmpdir: Path | None = None) -> LaunchPlan:
    """Compute (and, for a profile, stage into ``tmpdir``) the surface plan
    for one Claude Code launch. Pure apart from reading config/transcripts
    and writing private files inside ``tmpdir``."""
    root = Path(ws_root)
    penv = dict(os.environ if parent_env is None else parent_env)
    plan = LaunchPlan(mode=mode)
    caller = caller_overrides(agent_args, penv)
    left: list[dict[str, str]] = []
    state, state_why = deferral_state(child_env)
    cache = surface.read_probe_cache(root)
    failures = surface.probe_failures(root)

    # ---------------------------------------------------------------- MCP
    servers_out: list[dict[str, Any]] = []
    mcp_before = mcp_after = 0
    unmeasured: list[str] = []
    basis = "deferred" if state == "deferred" else "inline"
    specs = _claude_servers(root, penv)
    for spec in specs:
        if spec.approved is False:
            servers_out.append({
                "name": spec.name, "scope": spec.scope, "transport": spec.transport,
                "mechanism": "not-started", "est_upfront_tokens": None,
                "reason": "project server not approved (or disabled) in Claude Code "
                          "settings; Claude Code would not start it, so neither does ctx"})
            continue
        cost = _server_cost(spec, cache)
        est = None if cost is None else cost["deferred" if (basis == "deferred"
                                                             and not spec.always_load) else "inline"]
        if est is None:
            unmeasured.append(spec.name)
        mech = ("host-deferred" if state == "deferred" and not spec.always_load
                else "host-inline")
        reason = (state_why[0] if mech == "host-inline" and not spec.always_load
                  else "alwaysLoad: true exempts it from deferral" if spec.always_load
                  else "Claude Code tool search defers its schemas")
        if spec.transport != "stdio":
            reason += f"; remote {spec.transport} server passed through unchanged"
        if spec.name in failures:
            reason += (f"; probe FAILED {failures[spec.name]}: cost unmeasured, any figure is a "
                       f"floor; retry: {surface.PROBE_RETRY_HINT}")
        if "mcp" in caller:
            mech, reason = "left-alone", caller["mcp"] + f"; deferral state {state}"
        servers_out.append({"name": spec.name, "scope": spec.scope, "transport": spec.transport,
                            "mechanism": mech, "est_upfront_tokens": est, "reason": reason,
                            "probe_failed": spec.name in failures})
        mcp_before += est or 0
    mcp_after = mcp_before
    if "mcp-deferral" in caller and state != "deferred":
        left.append({"dimension": "mcp", "item": "deferral", "reason": caller["mcp-deferral"]})

    profile_note = ""
    if mode not in ("auto", "off"):
        profile_note = _apply_profile(root, mode, plan, specs, servers_out, caller, left,
                                      ctx_exe=ctx_exe, tmpdir=tmpdir, cache=cache,
                                      basis=basis)
        mcp_after = sum(s.get("est_upfront_tokens") or 0 for s in servers_out
                        if s["mechanism"] not in ("dropped-by-profile", "behind-gateway"))
        if any(s["mechanism"] == "behind-gateway" for s in servers_out):
            mcp_after += _gateway_schema_tokens()
    elif "mcp" in caller:
        left.append({"dimension": "mcp", "item": "servers", "reason": caller["mcp"]})

    # ---------------------------------------------------------------- skills
    skills_out: list[dict[str, Any]] = []
    overrides: dict[str, str] = {}
    sk_before = sk_after = 0
    usage = None
    floor = min_sessions_floor(root)
    skills = claude_skills(root, penv)
    user_ov = _settings_skill_overrides(root, penv)
    if "skills" in caller:
        left.append({"dimension": "skills", "item": "*", "reason": caller["skills"]})
    else:
        usage = surface.transcript_usage(root, env=penv)
    # Sessions actually read (the reader's caps already applied), never the
    # number of transcript files that merely exist.
    sessions = int(usage["sessions"]) if usage else 0
    floor_met = usage is not None and sessions >= floor
    partial = bool(usage and usage.get("partial"))
    partial_note = " (partial evidence: the transcript scan hit its cap)" if partial else ""
    for sk in skills:
        name = sk["name"]
        before = sk["tokens"]
        entry = {"name": name, "scope": sk["scope"], "mechanism": "unchanged",
                 "invocations": None, "est_before": before, "est_after": before, "reason": ""}
        if "skills" in caller:
            entry["reason"] = caller["skills"]
        elif name in CTX_SKILLS:
            entry["reason"] = "ctx's own skill is never narrowed"
        elif not sk["model_invocable"]:
            entry["reason"] = "disable-model-invocation already keeps its description out"
        elif name in user_ov:
            entry["reason"] = f"user already set skillOverrides={user_ov[name]!r}"
            left.append({"dimension": "skills", "item": name, "reason": entry["reason"]})
        elif usage is None:
            entry["reason"] = "no usage evidence (no Claude Code transcripts for this project)"
        elif not floor_met:
            entry["invocations"] = _invocations(name, usage)
            entry["reason"] = (f"insufficient usage evidence: {sessions} of {floor} "
                               "sessions required; unchanged" + partial_note)
        else:
            n = _invocations(name, usage)
            entry["invocations"] = n
            if n > 0:
                entry["reason"] = f"invoked {n}x in this project's transcripts" + partial_note
            elif sk["mtime"] > float(usage.get("newest_mtime") or 0):
                entry["reason"] = "skill is newer than the usage evidence"
            else:
                overrides[name] = "name-only"
                entry["mechanism"] = "name-only"
                entry["est_after"] = surface._tokens_of(f"- {name}")
                entry["reason"] = (f"0 invocations across {sessions} session(s); "
                                   "name stays listed, description dropped" + partial_note)
        sk_before += before
        sk_after += entry["est_after"]
        skills_out.append(entry)
    if overrides:
        # Claude Code merges skillOverrides key-by-key across settings sources
        # (observed live on 2.1.322). The user's own entries are carried
        # forward anyway, so a build that replaced the object would still be
        # right.
        plan.settings["skillOverrides"] = dict(sorted({**overrides, **user_ov}.items()))
    if usage is not None and not floor_met and "skills" not in caller and skills:
        left.append({"dimension": "skills", "item": "*",
                     "reason": f"insufficient usage evidence: {sessions} of {floor} sessions; "
                               "skills unchanged" + partial_note})
    if usage is None and "skills" not in caller and skills:
        left.append({"dimension": "skills", "item": "*",
                     "reason": "no usage evidence; skills unchanged"})

    # ---------------------------------------------------------------- receipt
    measured = len(unmeasured) < len(specs)

    def _redact(args: list[str]) -> list[str]:
        out = []
        for a in args:
            out.append("<tmp>/" + Path(a).name if tmpdir and str(a).startswith(str(tmpdir)) else a)
        return out

    plan.receipt = {
        "schema": WRAP_SCHEMA,
        "host": "claude",
        "mode": mode,
        "estimates": "ESTIMATES (~4 bytes/token). Skill figures are listing entries before the "
                     "host's own listing budget; MCP figures come from the probe cache and are "
                     "absent for unprobed or remote servers.",
        "tokens_est": {
            # MCP sums cover MEASURED servers only (the probe cache); the
            # rest are named, never priced at a guessed number.
            "mcp": {"before": mcp_before if measured else None,
                    "after": mcp_after if measured else None, "basis": basis,
                    "covers": "probe-cached servers only",
                    "unmeasured_servers": sorted(unmeasured)},
            "skills": {"before": sk_before, "after": sk_after},
            "total_measured": {"before": mcp_before + sk_before, "after": mcp_after + sk_after},
        },
        "mcp": {"deferral": {"state": state, "why": state_why}, "servers": servers_out},
        "skills": {"evidence": None if usage is None else {
                       "source": usage["source"], "sessions": usage["sessions"],
                       "unrecognized_files": usage.get("unrecognized", 0),
                       "subagent_files": usage.get("subagent_files", 0),
                       "partial": partial},
                   "min_sessions": floor, "floor_met": floor_met,
                   "items": skills_out},
        "applied": {"args": _redact(plan.args), "settings_keys": sorted(plan.settings),
                    "env_added": sorted(plan.env)},
        "left_alone": left,
        "unresolved_sources": surface.discovery_gaps(root, env=penv),
    }
    if profile_note:
        plan.receipt["profile"] = profile_note
    n_name_only = len(overrides)
    parts = [f"ctx wrap: surface {mode}"]
    if skills:
        parts.append(f"skills {n_name_only}/{len(skills)} name-only"
                     + (f" (est. ~{sk_before:,}→{sk_after:,} tok)" if n_name_only else
                        " (no usage evidence)" if usage is None and "skills" not in caller else
                        f" (evidence {sessions}/{floor} sessions, below floor)"
                        if usage is not None and not floor_met and "skills" not in caller else "")
                     + (" [partial evidence]" if partial else ""))
    if specs:
        mechs: dict[str, int] = {}
        for s in servers_out:
            mechs[s["mechanism"]] = mechs.get(s["mechanism"], 0) + 1
        parts.append("mcp " + ", ".join(f"{n} {m}" for m, n in sorted(mechs.items())))
    if left:
        parts.append(f"{len(left)} left alone")
    failed_here = sorted(s["name"] for s in servers_out if s.get("probe_failed"))
    if failed_here:
        parts.append(f"probe failed: {', '.join(failed_here)} (cost unmeasured)")
    skipped = [g for g in plan.receipt["unresolved_sources"] if g.startswith("config skipped")]
    if skipped:
        parts.append(f"{len(skipped)} config file(s) skipped, servers not counted")
    plan.line = " · ".join(parts) + f" · receipt {RECEIPT_FILE}"
    return plan


def _gateway_schema_tokens() -> int:
    from ctx.surface_gateway import _META_TOOLS

    return surface._tokens_of(json.dumps(_META_TOOLS))


def _apply_profile(root: Path, name: str, plan: LaunchPlan, specs: list[surface.ServerSpec],
                   servers_out: list[dict[str, Any]], caller: dict[str, str],
                   left: list[dict[str, str]], *, ctx_exe: str, tmpdir: Path | None,
                   cache: dict[str, Any], basis: str) -> str:
    """Stage a named profile's server selection for this session only."""
    from ctx import surface_profiles

    profile = surface_profiles.load_profile(name, root)
    if profile is None:
        left.append({"dimension": "mcp", "item": "servers",
                     "reason": f"unknown profile {name!r}; servers unchanged"})
        return f"unknown profile {name!r}"
    if "mcp" in caller:
        left.append({"dimension": "mcp", "item": "servers", "reason": caller["mcp"]})
        return f"{name}: not applied ({caller['mcp']})"
    managed = surface.managed_mcp_config()
    if managed:
        left.append({"dimension": "mcp", "item": "servers",
                     "reason": f"managed MCP config {managed} exists; --mcp-config would make "
                               "Claude Code exit"})
        return f"{name}: not applied (managed MCP config)"
    if tmpdir is None:
        left.append({"dimension": "mcp", "item": "servers", "reason": "no private temp dir"})
        return f"{name}: not applied"
    rep = surface_profiles.compile_profile(root, name, host="claude", profile=profile)
    if rep.get("error"):
        left.append({"dimension": "mcp", "item": "servers", "reason": rep["error"]})
        return f"{name}: not applied ({rep['error']})"
    selected = set(rep.get("selected") or [])
    kept = {s.name for s in specs
            if f"mcp.{s.name}" in selected or s.name in set(rep.get("servers_kept") or [])
            or any(c.startswith(f"mcp.{s.name}.") for c in selected)}
    deferred_tools = {k: v for k, v in (rep.get("deferred_tools") or {}).items() if k in kept}
    try:
        from ctx.config import load_config

        want_gateway = bool(deferred_tools) or bool(load_config(root).surface.gateway)
    except Exception:
        want_gateway = bool(deferred_tools)
    mcp: dict[str, Any] = {}
    backends: dict[str, Any] = {}
    by_name = {s["name"]: s for s in servers_out}
    for spec in specs:
        row = by_name[spec.name]
        if row["mechanism"] == "not-started":
            continue  # never start a project server Claude Code would not
        if spec.name not in kept:
            row["mechanism"] = "dropped-by-profile"
            row["reason"] = f"not in profile {name}"
            continue
        # ctx's own server is kernel: passed through directly, so the bytes
        # and tool name the model sees for it never change.
        own = spec.name in ("ctx", "ctx-harness") or spec.args[:1] == ("mcp",)
        if want_gateway and spec.transport == "stdio" and not own:
            row["mechanism"] = "behind-gateway"
            row["reason"] = "revealed on demand through ctx surface gateway"
            entry: dict[str, Any] = {"command": spec.command, "args": list(spec.args),
                                     "scope": spec.scope}
            if spec.cwd:
                entry["cwd"] = spec.cwd
            if spec.env:
                # Names only. The gateway re-reads the values from the user's
                # own config file when it spawns the backend.
                entry["env_keys"] = sorted(spec.env)
            if deferred_tools.get(spec.name):
                entry["exclude_tools"] = list(deferred_tools[spec.name])
            backends[spec.name] = entry
        else:
            if spec.transport != "stdio":
                row["reason"] = (row["reason"].split(";")[0]
                                 + f"; remote {spec.transport} server passed through unchanged")
            mcp[spec.name] = spec.raw
    if backends:
        bpath = tmpdir / "backends.json"
        _write_private(bpath, json.dumps({"mcpServers": backends}, indent=2, sort_keys=True))
        cmd = _gateway_command(ctx_exe)
        mcp[GATEWAY_NAME] = {"command": cmd[0], "args": [*cmd[1:], "surface", "gateway",
                                                         "--workspace", str(root),
                                                         "--backends", str(bpath)]}
    mpath = tmpdir / "mcp.json"
    # The server entries are copied verbatim from the user's own config,
    # so ``${VAR}`` references stay unexpanded and Claude Code expands them;
    # literal values are exposed no wider than a 0600 file in a 0700 temp
    # dir that is removed when the session ends.
    _write_private(mpath, json.dumps({"mcpServers": mcp}, indent=2, sort_keys=True))
    # --strict-mcp-config directly after the variadic --mcp-config ends its
    # value list, so a positional prompt in the agent args is never consumed.
    plan.args += ["--mcp-config", str(mpath), "--strict-mcp-config"]
    left.append({"dimension": "mcp", "item": "plugins/connectors",
                 "reason": "--strict-mcp-config also drops plugin and connector servers "
                           "ctx does not enumerate"})
    return (f"{name}: kept {len(kept)} of {len(specs)} servers"
            + (f", {len(backends)} behind the gateway" if backends else ""))


def write_receipt(ws_root: Path, receipt: dict[str, Any]) -> str | None:
    """Write the receipt atomically (temp file + rename in the same
    directory); returns its workspace-relative path or None."""
    import tempfile

    tmp = None
    try:
        path = Path(ws_root) / RECEIPT_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".wrap-receipt-", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, path)
        return RECEIPT_FILE
    except OSError:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return None


__all__ = ["LaunchPlan", "WRAP_SCHEMA", "RECEIPT_FILE", "plan_claude_launch",
           "deferral_state", "caller_overrides", "claude_skills", "resolve_mode",
           "write_receipt"]
