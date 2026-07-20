"""Phase 3 — compile a minimal capability surface from a named profile and
emit the enforceable per-host config.

Research finding (docs/CAPABILITY-SURFACE.md §hosts): the only surface bound
that *every* host respects is the one set at launch/compile time. Claude Code
honours dynamic tool changes on its normal path but not the ToolSearch index;
Codex snapshots tools at startup; Antigravity needs a manual refresh. So the
durable, cross-host enforcement is: **decide the exposed set before launch and
emit each host its minimal native config.**

A profile names the capability families a task needs and an authority ceiling.
`compile_profile` selects the covering capabilities, checks the selection
(dependency closure, duplicate providers, authority ceiling, schema budget),
and renders — or with ``apply=True`` writes — the minimal config per host:

* **Claude Code** — a ``.mcp.json`` with only the selected servers, launched
  with ``--strict-mcp-config``, plus ``permissions.deny`` wildcards for the
  excluded servers.
* **Codex** — ``[mcp_servers.*]`` for the selected servers only, with
  ``disabled_tools`` for any gated tool.
* **Antigravity** — a minimal ``mcp_config.json`` with only selected servers.

Kernel capabilities (ctx's own bounded tools, policy, repo steering) are always
kept. Everything is deterministic and fails open.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ctx import surface

COMPILE_DIR = ".ctx-surface"
HOSTS = ("claude", "codex", "antigravity")


@dataclass(frozen=True)
class Profile:
    """A task-shaped capability budget. Empty ``families`` means all."""

    name: str
    families: frozenset[str] = frozenset()
    authority_ceiling: str = "destructive"
    max_schema_tokens: int | None = None
    include: frozenset[str] = frozenset()
    exclude: frozenset[str] = frozenset()
    description: str = ""


# Built-in profiles: the common task shapes. `local-dev` is the sensible
# default (read + edit + test, no remote mutation, no cloud/collab).
BUILTIN_PROFILES: dict[str, Profile] = {
    "read-only": Profile(
        "read-only",
        families=frozenset({"repository", "semantic-analysis", "docs", "harness"}),
        authority_ceiling="read",
        description="explore only: no mutation, no remote, no deploy",
    ),
    "local-dev": Profile(
        "local-dev",
        families=frozenset({"repository", "testing", "semantic-analysis", "docs", "harness"}),
        authority_ceiling="local-write",
        description="read + edit + test locally; no remote mutation or cloud",
    ),
    "review": Profile(
        "review",
        families=frozenset({"repository", "testing", "semantic-analysis",
                            "remote-source-control", "docs", "harness"}),
        authority_ceiling="remote-write",
        description="local-dev plus PR/review remote-source-control",
    ),
    "full": Profile("full", families=frozenset(), authority_ceiling="destructive",
                    description="everything currently configured (no trimming)"),
}


def _authority_ok(cap: surface.Capability, ceiling: str) -> bool:
    order = surface.AUTHORITY_ORDER
    if cap.authority not in order or cap.authority in ("n/a", "unknown"):
        return True  # prose / unclassified is gated by family, not authority
    try:
        return order.index(cap.authority) <= order.index(ceiling)
    except ValueError:
        return True


def _is_kernel(cap: surface.Capability) -> bool:
    return (cap.kind in ("policy", "repo_instructions")
            or cap.provider in ("ctx", "ctx-harness")
            or cap.id.startswith("mcp.ctx"))


def load_profile(name: str, ws_root: Path | str) -> Profile | None:
    """Built-in profile, or one defined in ctx.toml
    ``[surface.profiles.<name>]``. Repo definitions win over built-ins."""
    text = surface._read_text(Path(ws_root) / "ctx.toml")
    if text:
        try:
            import tomllib

            doc = tomllib.loads(text)
            spec = (((doc.get("surface") or {}).get("profiles") or {}).get(name))
            if isinstance(spec, dict):
                return Profile(
                    name=name,
                    families=frozenset(str(f) for f in spec.get("families", [])),
                    authority_ceiling=str(spec.get("authority_ceiling", "destructive")),
                    max_schema_tokens=spec.get("max_schema_tokens"),
                    include=frozenset(str(x) for x in spec.get("include", [])),
                    exclude=frozenset(str(x) for x in spec.get("exclude", [])),
                    description=str(spec.get("description", "")),
                )
        except Exception:
            pass
    return BUILTIN_PROFILES.get(name)


def select(records: list[surface.Capability], profile: Profile
           ) -> tuple[list[surface.Capability], list[tuple[surface.Capability, str]]]:
    """(selected, [(excluded, reason)]). Kernel always kept; explicit include
    forces keep; explicit exclude forces drop; otherwise family membership and
    authority ceiling decide."""
    selected: list[surface.Capability] = []
    excluded: list[tuple[surface.Capability, str]] = []
    for cap in records:
        if cap.id in profile.exclude:
            excluded.append((cap, "explicitly excluded"))
            continue
        if _is_kernel(cap) or cap.id in profile.include:
            selected.append(cap)
            continue
        if profile.families and (cap.family or "other") not in profile.families:
            excluded.append((cap, f"family {cap.family or 'other'} not in profile"))
            continue
        if not _authority_ok(cap, profile.authority_ceiling):
            excluded.append((cap, f"authority {cap.authority} > ceiling {profile.authority_ceiling}"))
            continue
        selected.append(cap)
    return selected, excluded


def check(selected: list[surface.Capability], profile: Profile) -> list[str]:
    """Compile-time checks. Returns human-readable issues ([] = clean)."""
    issues: list[str] = []
    kept_providers = {c.provider for c in selected if c.kind in ("mcp_server", "mcp_tool")}
    # dependency closure: a selected prose capability must not reference an MCP
    # server that the selection dropped.
    for c in selected:
        for ref in c.unresolved:
            issues.append(f"{c.id} references unconfigured server '{ref}'")
        for ref in c.requires:
            head = ref.split("__")[0] if "__" in ref else ref
            if ref.startswith(("mcp__", "mcp.")):
                srv = surface._MCP_REF_RE.findall(ref)
                if srv and srv[0] not in kept_providers and srv[0] not in ("ctx", "ctx-harness"):
                    issues.append(f"{c.id} needs server '{srv[0]}' excluded by this profile")
    # duplicate providers for the same capability family
    seen: dict[str, list[str]] = {}
    for c in selected:
        if c.kind == "mcp_server":
            seen.setdefault(c.family, []).append(c.provider)
    for fam, provs in seen.items():
        if len(provs) > 1:
            issues.append(f"duplicate {fam} providers: {', '.join(sorted(provs))}")
    # authority ceiling breach (should not happen after select, defensive)
    for c in selected:
        if not _authority_ok(c, profile.authority_ceiling):
            issues.append(f"{c.id} exceeds authority ceiling {profile.authority_ceiling}")
    # schema budget
    if profile.max_schema_tokens is not None:
        total = sum(c.tokens for c in selected)
        if total > profile.max_schema_tokens:
            issues.append(f"selected {total} tok > budget {profile.max_schema_tokens} tok")
    return sorted(set(issues))


# ------------------------------------------------------------ per-host emit
def _selected_servers(selected, ws_root) -> dict[str, list[str]]:
    kept = {c.provider for c in selected if c.kind in ("mcp_server", "mcp_tool")}
    return {name: argv for name, argv in surface._mcp_server_commands(ws_root).items()
            if name in kept}


def _excluded_servers(records, selected, ws_root) -> list[str]:
    kept = {c.provider for c in selected if c.kind in ("mcp_server", "mcp_tool")}
    return sorted(n for n in surface._mcp_server_commands(ws_root) if n not in kept)


def emit_claude(servers: dict[str, list[str]], excluded: list[str],
                deferred_tools: dict[str, list[str]]) -> dict[str, Any]:
    mcp = {name: {"command": argv[0], "args": argv[1:]} for name, argv in servers.items()}
    # Whole-server drops reduce context (the server isn't loaded). Per-tool
    # deny gates *callability* only — a denied tool is still listed to the
    # model, so it still costs tokens; use `ctx surface gateway` for per-tool
    # token reduction within a kept server.
    deny = [f"mcp__{name}__*" for name in excluded]
    for srv, tools in sorted(deferred_tools.items()):
        deny.extend(f"mcp__{srv}__{t}" for t in tools)
    return {
        "files": {
            f"{COMPILE_DIR}/mcp.claude.json": json.dumps({"mcpServers": mcp}, indent=2) + "\n",
            f"{COMPILE_DIR}/settings.claude.json":
                json.dumps({"permissions": {"deny": deny}}, indent=2) + "\n" if deny else "",
        },
        "launch": f"claude --strict-mcp-config --mcp-config {COMPILE_DIR}/mcp.claude.json",
    }


def emit_codex(servers: dict[str, list[str]], excluded: list[str],
               deferred_tools: dict[str, list[str]]) -> dict[str, Any]:
    lines = ["# ctx surface compile — minimal Codex MCP surface for this profile",
             "# (excluded servers are absent; per-tool defers use disabled_tools)"]
    for name, argv in sorted(servers.items()):
        lines.append(f"\n[mcp_servers.{name}]")
        lines.append(f'command = "{argv[0]}"')
        if argv[1:]:
            lines.append("args = [" + ", ".join(json.dumps(a) for a in argv[1:]) + "]")
        gated = deferred_tools.get(name)
        if gated:
            lines.append("disabled_tools = [" + ", ".join(json.dumps(t) for t in gated) + "]")
    return {
        "files": {f"{COMPILE_DIR}/config.codex.toml": "\n".join(lines) + "\n"},
        "launch": f"codex --config {COMPILE_DIR}/config.codex.toml   # or merge into .codex/config.toml",
    }


def emit_antigravity(servers: dict[str, list[str]], excluded: list[str],
                     deferred_tools: dict[str, list[str]]) -> dict[str, Any]:
    mcp = {name: {"command": argv[0], "args": argv[1:], "disabled": False}
           for name, argv in servers.items()}
    note = ""
    if deferred_tools:
        # Antigravity has no per-tool gating: tools deferred within a kept
        # server can only be bounded by the gateway.
        note = ("  NOTE: per-tool defers within kept servers are not enforceable "
                "in Antigravity config — route through `ctx surface gateway`.")
    return {
        "files": {f"{COMPILE_DIR}/mcp_config.antigravity.json":
                  json.dumps({"mcpServers": mcp}, indent=2) + "\n"},
        "launch": "point ~/.gemini/antigravity-cli config at this file, then Refresh MCP servers"
                  + ("\n" + note if note else ""),
    }


_EMITTERS = {"claude": emit_claude, "codex": emit_codex, "antigravity": emit_antigravity}


def compile_profile(ws_root: Path | str, name: str, *, host: str = "claude",
                    apply: bool = False, probe_mcp: bool = False) -> dict[str, Any]:
    """Compile ``name`` for ``host``. Returns a structured report; with
    ``apply`` also writes the emitted files under ``.ctx-surface/``."""
    profile = load_profile(name, ws_root)
    if profile is None:
        return {"error": f"unknown profile {name!r}; built-ins: {', '.join(BUILTIN_PROFILES)}"}
    if host not in _EMITTERS:
        return {"error": f"unknown host {host!r}; one of {', '.join(HOSTS)}"}

    a = surface.audit(ws_root, probe_mcp=probe_mcp)
    records = [surface.Capability(**{k: (tuple(v) if isinstance(v, list) else v)
                                     for k, v in r.items() if k != "recommended_level"})
               for r in a["records"]]
    selected, excluded = select(records, profile)
    issues = check(selected, profile)
    servers = _selected_servers(selected, ws_root)
    excl_servers = _excluded_servers(records, selected, ws_root)
    kept_providers = set(servers)

    # Tools deferred *within a kept server* (server stays, some tools excluded).
    deferred_tools: dict[str, list[str]] = {}
    for cap, _reason in excluded:
        if cap.kind == "mcp_tool" and cap.provider in kept_providers:
            tool = cap.id.split(".", 2)[-1]
            deferred_tools.setdefault(cap.provider, []).append(tool)
    for srv in deferred_tools:
        deferred_tools[srv].sort()

    emitted = _EMITTERS[host](servers, excl_servers, deferred_tools)

    before = sum(c.tokens for c in records)
    # Two honest numbers. after_gateway honours per-tool selection (achievable
    # only by the gateway, which controls exactly what it lists). after_server
    # is what dropping whole servers alone yields — kept servers keep ALL their
    # tools' tokens, because a host deny/disable lists the tool anyway on
    # Claude (tokens unchanged). The gap is what the gateway recovers.
    after_gateway = sum(c.tokens for c in selected)
    after_server = (sum(c.tokens for c in selected if c.kind != "mcp_tool")
                    + sum(c.tokens for c in records
                          if c.kind == "mcp_tool" and c.provider in kept_providers))

    written: list[str] = []
    if apply:
        root = Path(ws_root)
        for rel, content in emitted["files"].items():
            if not content:
                continue
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            written.append(rel)

    return {
        "schema": "ctx.surface-compile/v1",
        "profile": profile.name,
        "host": host,
        "selected": [c.id for c in selected],
        "excluded": [{"id": c.id, "reason": r} for c, r in excluded],
        "servers_kept": sorted(servers),
        "servers_dropped": excl_servers,
        "deferred_tools": deferred_tools,
        "issues": issues,
        "tokens": {"before": before,
                   "after_server": after_server, "saved_server": before - after_server,
                   "after_gateway": after_gateway, "saved_gateway": before - after_gateway},
        "launch": emitted["launch"],
        "files": emitted["files"],
        "written": written,
    }


def render_compile(rep: dict[str, Any]) -> str:
    if "error" in rep:
        return f"error: {rep['error']}"
    t = rep["tokens"]
    lines = [
        f"[ctx surface compile · profile={rep['profile']} · host={rep['host']}]",
        f"selected {len(rep['selected'])} capabilities of {t['before']:,} tok/turn",
        f"  enforced by config (drop whole servers): {t['before']:,} → "
        f"{t['after_server']:,} tok (−{t['saved_server']:,})",
    ]
    if t["after_gateway"] < t["after_server"]:
        lines.append(f"  with `ctx surface gateway` (per-tool): {t['before']:,} → "
                     f"{t['after_gateway']:,} tok (−{t['saved_gateway']:,})")
    if rep["servers_kept"]:
        lines.append("  keep servers: " + ", ".join(rep["servers_kept"]))
    if rep["servers_dropped"]:
        lines.append("  drop servers: " + ", ".join(rep["servers_dropped"]))
    if rep["deferred_tools"]:
        n = sum(len(v) for v in rep["deferred_tools"].values())
        lines.append(f"  defer {n} tools within kept servers "
                     "(gateway for token savings; config deny/disable = callability only)")
    if rep["issues"]:
        lines.append("  ISSUES:")
        for i in rep["issues"]:
            lines.append(f"    ! {i}")
    else:
        lines.append("  checks: clean (dep closure · providers · authority · budget)")
    if rep["written"]:
        lines.append("  wrote: " + ", ".join(rep["written"]))
    else:
        lines.append("  preview only — pass --apply to write the config")
    lines.append(f"  launch: {rep['launch']}")
    return "\n".join(lines)


__all__ = [
    "Profile", "BUILTIN_PROFILES", "COMPILE_DIR", "HOSTS",
    "load_profile", "select", "check", "compile_profile", "render_compile",
]
