"""ctx surface — capability-context audit (the input side of containment).

straitjacket already contains tool **output**: unbounded output becomes a
bounded, addressable digest. This module contains tool **input**: the
persistent *capability surface* — MCP tool schemas, skills, sub-agent
definitions, repository instructions, hooks, policy — that a host re-sends to
the model on every turn, before any tool is called. A 400-token tool schema
shown across 30 turns is 12,000 token-turns of tax whether or not it is ever
used.

    CAPABILITY CONTAINMENT   what the model is told it can do
    EVIDENCE CONTAINMENT     what the model is shown after doing it

Phase 1 is **measurement, not mutation**: inventory the discretionary surface
ctx can read, price it deterministically in tokens, attribute observed
utilization from the proxy wire log, and flag overlap / leakage / authority as
*shadow* signals. Trimming is preview-only; nothing is hidden or removed.

Honest blind spot: ctx cannot see the host's built-in system prompt or native
tool schemas (Read/Edit/Bash/Grep/Glob) — those live inside the host, not in
any file. This audit covers the **discretionary** surface (MCP servers,
skills, agents, repo instructions, hooks, policy), which is exactly where
over-provisioning, redundancy, and capability leakage accumulate.

Every collector fails open: an unreadable or malformed source contributes
nothing, never an error.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ctx.proxywindow import PROXY_SUBDIR
from ctx.sessiondir import session_reads_path
from ctx.textutil import EVIDENCE_LINE_CHARS, estimate_tokens

_PROBE_CACHE = ".ctx-surface/probe-cache.json"

SCHEMA = "ctx.surface/v1"

# ---------------------------------------------------------------- authority
# Ordered least→most powerful. A capability's authority is the highest tier any
# of its keywords implies; unknown when nothing matches.
AUTHORITY_ORDER = ("read", "local-write", "remote-write", "destructive", "unknown")
_AUTHORITY_KEYWORDS = {
    "destructive": (
        "delete", "destroy", "drop", "teardown", "terminate", "revoke", "purge",
        "force-push", "force_push", "rm ", "remove",
    ),
    "remote-write": (
        "deploy", "publish", "release", "merge", "push", "create_pull", "create-pull",
        "send", "email", "upload", "provision", "mutate", "production",
    ),
    "local-write": (
        "write", "edit", "create", "update", "apply", "commit", "format", "install",
        "execute", "run ", "exec",
    ),
    "read": (
        "read", "search", "get", "list", "view", "outline", "stats", "grep", "glob",
        "fetch", "describe", "show", "inspect", "map",
    ),
}
# Domains a *code* task rarely needs; visible authority here is planning noise.
_UNRELATED_DOMAINS = {
    "cloud/deploy": ("deploy", "kubernetes", "kubectl", "terraform", "cloudformation",
                     "ec2", "s3 ", "lambda", "gcloud", "azure"),
    "collaboration": ("email", "slack", "calendar", "jira", "notion", "confluence"),
    "database": ("database", "sql", "postgres", "mysql", "mongo", "redis"),
    "billing": ("billing", "payment", "invoice", "credit card"),
}
# Secret-adjacent shapes in descriptions/examples (names/paths, not values).
# The env-var pattern requires an underscore so real names (GEMINI_API_KEY)
# match but ordinary prose acronyms (SECTION, README) do not.
_SECRET_ADJACENT = (
    re.compile(r"\b[A-Z][A-Z0-9]*_[A-Z0-9_]{2,}\b"),       # ENV_VAR_NAMES
    re.compile(r"https?://[^\s\"'/]+\.[^\s\"']+"),          # URLs with a host
    re.compile(r"/(?:home|Users|var|etc|root)/[^\s\"']+"), # absolute paths
    re.compile(r"\b(?:sk|pk|ghp|xox[bap])[-_][A-Za-z0-9]{8,}\b"),  # token shapes
)

# Kinds whose authority is a real capability property (a tool can act). Prose
# kinds (skill/agent/instructions/policy) carry no authority of their own —
# they can only *mention* capabilities, which is a leakage class, not power.
_ACTION_KINDS = {"mcp_server", "mcp_tool"}


@dataclass(frozen=True)
class Capability:
    """One persistent context-bearing capability on the surface."""

    id: str
    kind: str            # mcp_server | mcp_tool | skill | agent | repo_instructions | hooks | policy
    provider: str
    source: str          # workspace-relative path
    tokens: int          # estimated static tokens contributed each turn
    authority: str = "unknown"
    activation: str = "always"  # current activation (file-resident ⇒ always)
    invocations: int = -1        # observed; -1 = not measurable for this kind
    sensitive_terms: tuple[str, ...] = ()
    leakage: tuple[str, ...] = ()
    overlaps: tuple[str, ...] = ()
    detail: str = ""
    # ---- Phase 2 graph fields (populated by enrich_graph) ----
    family: str = ""            # repository | testing | remote-source-control | ...
    provides: tuple[str, ...] = ()   # capability tags this satisfies (search, read, ...)
    requires: tuple[str, ...] = ()   # referenced capability/tool tokens it depends on
    phase: str = ""             # explore | edit | verify | deliver (typical task phase)
    unresolved: tuple[str, ...] = ()  # required references not found in the surface

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "provider": self.provider,
            "source": self.source, "tokens": self.tokens, "authority": self.authority,
            "activation": self.activation, "invocations": self.invocations,
            "sensitive_terms": list(self.sensitive_terms), "leakage": list(self.leakage),
            "overlaps": list(self.overlaps), "detail": self.detail,
            "family": self.family, "provides": list(self.provides),
            "requires": list(self.requires), "phase": self.phase,
            "unresolved": list(self.unresolved),
        }


# ---------------------------------------------------------------- helpers
def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _tokens_of(text: str) -> int:
    return estimate_tokens(len(text.encode("utf-8")))


def infer_authority(*texts: str) -> str:
    blob = " ".join(t.lower() for t in texts if t)
    for tier in ("destructive", "remote-write", "local-write", "read"):
        if any(kw in blob for kw in _AUTHORITY_KEYWORDS[tier]):
            return tier
    return "unknown"


def _leakage_tags(text: str, kind: str, authority: str, invocations: int
                  ) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(leakage classes, sample sensitive terms). Descriptive/heuristic.

    ``excessive-authority`` applies only to action kinds (a tool that can act
    but is never used). Prose kinds instead get ``capability-mention`` when
    they surface high-authority actions into planning — that is behavioural /
    capability leakage, not authority the prose itself holds."""
    low = text.lower()
    tags: list[str] = []
    for domain, kws in _UNRELATED_DOMAINS.items():
        if any(kw in low for kw in kws):
            tags.append(f"unrelated-domain:{domain}")
            break
    if kind in _ACTION_KINDS:
        if authority in ("remote-write", "destructive") and invocations in (0, -1):
            tags.append("excessive-authority")
    else:
        mentioned = infer_authority(text)
        if mentioned in ("remote-write", "destructive"):
            tags.append(f"capability-mention:{mentioned}")
    terms: list[str] = []
    for pat in _SECRET_ADJACENT:
        for m in pat.findall(text):
            if m not in terms:
                terms.append(m)
            if len(terms) >= 6:
                break
        if len(terms) >= 6:
            break
    if terms:
        tags.append("secret-adjacent")
    return tuple(tags), tuple(terms[:6])


# ---------------------------------------------------------------- collectors
_MCP_CONFIG_LOCATIONS = (
    ".mcp.json",
    ".claude/settings.json",
    ".claude/settings.local.json",
    ".cursor/mcp.json",
)


def _collect_mcp_from_json(root: Path, records: list[Capability]) -> None:
    for rel in _MCP_CONFIG_LOCATIONS:
        path = root / rel
        text = _read_text(path)
        if not text:
            continue
        try:
            doc = json.loads(text)
        except json.JSONDecodeError:
            continue
        servers = doc.get("mcpServers") if isinstance(doc, dict) else None
        for name, cfg in (servers or {}).items():
            if not isinstance(cfg, dict) or cfg.get("disabled") is True:
                continue
            detail = f"{cfg.get('command', '')} {' '.join(map(str, cfg.get('args', [])))}".strip()
            _add_mcp_server(records, name, _rel(path, root), detail)


def _collect_mcp_from_codex(root: Path, records: list[Capability]) -> None:
    text = _read_text(root / ".codex" / "config.toml")
    if not text:
        return
    try:
        import tomllib

        doc = tomllib.loads(text)
    except Exception:
        return
    for name, cfg in (doc.get("mcp_servers") or {}).items():
        if not isinstance(cfg, dict):
            continue
        detail = f"{cfg.get('command', '')} {' '.join(map(str, cfg.get('args', [])))}".strip()
        _add_mcp_server(records, name, ".codex/config.toml", detail)


def _collect_mcp_from_agents(root: Path, records: list[Capability]) -> None:
    plugins = root / ".agents" / "plugins"
    if not plugins.is_dir():
        return
    for cfg_path in sorted(plugins.glob("*/mcp_config.json")):
        text = _read_text(cfg_path)
        if not text:
            continue
        try:
            doc = json.loads(text)
        except json.JSONDecodeError:
            continue
        for name, cfg in (doc.get("mcpServers") or {}).items():
            if not isinstance(cfg, dict) or cfg.get("disabled") is True:
                continue
            detail = f"{cfg.get('command', '')} {' '.join(map(str, cfg.get('args', [])))}".strip()
            _add_mcp_server(records, name, _rel(cfg_path, root), detail)


def _add_mcp_server(records: list[Capability], name: str, source: str, detail: str) -> None:
    cid = f"mcp.{name}"
    if any(r.id == cid for r in records):  # dedupe across host configs
        return
    # A registration line is cheap; the real cost is the server's tool schemas,
    # measured only under --probe-mcp. Mark that here.
    tokens = _tokens_of(f"{name} {detail}")
    records.append(Capability(
        id=cid, kind="mcp_server", provider=name, source=source, tokens=tokens,
        authority=infer_authority(name, detail), activation="always", invocations=-1,
        detail=f"MCP server (tool schemas unmeasured; run --probe-mcp): {detail}"[:200],
    ))


_SKILL_GLOBS = (".claude/skills/**/*.md", ".agents/**/skills/**/*.md", ".cursor/rules/**/*.md")
_AGENT_GLOBS = (".claude/agents/*.md", ".agents/**/agents/*.md")
_REPO_INSTRUCTION_FILES = (
    "CLAUDE.md", "AGENTS.md", ".cursorrules", ".windsurfrules",
    ".github/copilot-instructions.md", "GEMINI.md",
)


_GENERIC_STEMS = {"skill", "agent", "agents", "index", "readme", "main"}


def _capability_name(path: Path) -> str:
    """A skill at ``skills/deployer/SKILL.md`` is named for its directory, not
    the generic filename, so ids don't collide."""
    if path.stem.lower() in _GENERIC_STEMS:
        return path.parent.name
    return path.stem


def _collect_files(root: Path, globs, kind: str, provider_of, records: list[Capability]) -> None:
    seen: set[Path] = set()
    ids: set[str] = set()
    for pattern in globs:
        for path in sorted(root.glob(pattern)):
            if path in seen or not path.is_file():
                continue
            seen.add(path)
            text = _read_text(path)
            if text is None:
                continue
            cid = f"{kind}.{_capability_name(path)}"
            while cid in ids:
                cid += "_"
            ids.add(cid)
            tokens = _tokens_of(text)
            leakage, terms = _leakage_tags(text, kind, "n/a", -1)
            rel = _rel(path, root)
            records.append(Capability(
                id=cid, kind=kind, provider=provider_of(rel),
                source=rel, tokens=tokens, authority="n/a", activation="always",
                invocations=-1, sensitive_terms=terms, leakage=leakage,
                detail=(text.strip().splitlines() or [""])[0][:EVIDENCE_LINE_CHARS],
            ))


def _collect_repo_instructions(root: Path, records: list[Capability]) -> None:
    for name in _REPO_INSTRUCTION_FILES:
        path = root / name
        text = _read_text(path)
        if not text:
            continue
        leakage, terms = _leakage_tags(text, "repo_instructions", "n/a", -1)
        records.append(Capability(
            id=f"repo.{name}", kind="repo_instructions", provider="repo",
            source=name, tokens=_tokens_of(text), authority="n/a",
            activation="always", invocations=-1, sensitive_terms=terms, leakage=leakage,
            detail=(text.strip().splitlines() or [""])[0][:EVIDENCE_LINE_CHARS],
        ))


def _collect_policy(root: Path, records: list[Capability]) -> None:
    for name in ("ctx.toml", "ctx-policy.toml"):
        text = _read_text(root / name)
        if not text:
            continue
        records.append(Capability(
            id=f"policy.{name}", kind="policy", provider="ctx", source=name,
            tokens=_tokens_of(text), authority="read", activation="always",
            invocations=-1, detail="harness policy text",
        ))


def collect_surface(ws_root: Path | str) -> list[Capability]:
    """Inventory the discretionary capability surface visible as files under
    the workspace. Deterministic (sorted), fail-open."""
    root = Path(ws_root)
    records: list[Capability] = []
    _collect_mcp_from_json(root, records)
    _collect_mcp_from_codex(root, records)
    _collect_mcp_from_agents(root, records)
    _collect_files(root, _SKILL_GLOBS, "skill",
                   lambda r: "claude" if r.startswith(".claude") else "repo", records)
    _collect_files(root, _AGENT_GLOBS, "agent",
                   lambda r: "claude" if r.startswith(".claude") else "repo", records)
    _collect_repo_instructions(root, records)
    _collect_policy(root, records)
    records.sort(key=lambda r: (-r.tokens, r.kind, r.id))
    return records


# ---------------------------------------------------------------- utilization
def observed_tool_counts(ws_root: Path | str) -> dict[str, int]:
    """Per-tool invocation counts observed by the proxy wire log across this
    workspace's sessions. Fail-open to empty."""
    counts: dict[str, int] = {}
    proxy = session_reads_path(ws_root, PROXY_SUBDIR)
    if not proxy.is_dir():
        return counts
    for wire in proxy.glob("**/wire.jsonl"):
        text = _read_text(wire)
        if not text:
            continue
        for line in text.splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            for name, n in (rec.get("tools") or {}).items():
                try:
                    counts[str(name)] = counts.get(str(name), 0) + int(n)
                except (TypeError, ValueError):
                    continue
    return counts


def _match_invocations(cap: Capability, counts: dict[str, int]) -> int:
    """Attribute observed invocations to a capability. MCP servers match any
    ``mcp__<provider>__*`` tool; unmeasurable kinds stay -1."""
    if not counts:
        return -1
    if cap.kind == "mcp_server":
        prefix = f"mcp__{cap.provider}"
        return sum(n for name, n in counts.items()
                   if name.startswith(prefix) or name.startswith(f"mcp.{cap.provider}"))
    if cap.kind == "mcp_tool":
        tool = cap.id.split(".", 2)[-1]
        want = {f"mcp__{cap.provider}__{tool}", f"mcp.{cap.provider}.{tool}", tool}
        return sum(n for name, n in counts.items() if name in want)
    return -1


# ---------------------------------------------------------------- overlap
_CAPABILITY_KEYS = (
    "search", "read", "get", "list", "delete", "write", "edit", "create",
    "deploy", "merge", "push", "test", "diff", "run", "review", "comment",
)


def _capability_key(cap: Capability) -> str | None:
    blob = f"{cap.id} {cap.detail}".lower()
    for key in _CAPABILITY_KEYS:
        if key in blob:
            return key
    return None


def detect_overlaps(records: list[Capability]) -> list[Capability]:
    """Cluster capabilities by a shared capability key (descriptive/shadow —
    semantic similarity is NOT interchangeability). Returns records with
    ``overlaps`` populated."""
    clusters: dict[str, list[str]] = {}
    for cap in records:
        key = _capability_key(cap)
        if key:
            clusters.setdefault(key, []).append(cap.id)
    out: list[Capability] = []
    for cap in records:
        key = _capability_key(cap)
        peers = tuple(cid for cid in clusters.get(key or "", []) if cid != cap.id)
        out.append(cap if not peers else _with(cap, overlaps=peers))
    return out


def _with(cap: Capability, **changes: Any) -> Capability:
    data = cap.as_dict()
    data.update(changes)
    return Capability(
        id=data["id"], kind=data["kind"], provider=data["provider"], source=data["source"],
        tokens=data["tokens"], authority=data["authority"], activation=data["activation"],
        invocations=data["invocations"], sensitive_terms=tuple(data["sensitive_terms"]),
        leakage=tuple(data["leakage"]), overlaps=tuple(data["overlaps"]), detail=data["detail"],
        family=data.get("family", ""), provides=tuple(data.get("provides", ())),
        requires=tuple(data.get("requires", ())), phase=data.get("phase", ""),
        unresolved=tuple(data.get("unresolved", ())),
    )


# ---------------------------------------------------------------- Phase 2 graph
# Capability families (the compact directory a compact index would show) and
# the task phase each typically serves. Ordered specific→general; first match
# wins against provider + id + detail.
FAMILY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("remote-source-control", ("github", "gitlab", "gitea", "bitbucket", "pull_request", "pull-request")),
    ("deployment", ("deploy", "kubernetes", "kubectl", "terraform", "cloudformation", "helm", "vercel", "netlify")),
    ("cloud", ("aws", "gcloud", "gcp", "azure", "ec2", "lambda", "cloudflare")),
    ("database", ("postgres", "mysql", "mongo", "redis", "sqlite", "database", "sql")),
    ("collaboration", ("slack", "email", "gmail", "jira", "notion", "confluence", "calendar", "linear")),
    ("browser", ("browser", "playwright", "puppeteer", "chromium", "webfetch", "websearch")),
    ("semantic-analysis", ("semgrep", "ast-grep", "astgrep", "scip", "sourcegraph", "tree-sitter", "codeql")),
    ("testing", ("pytest", "jest", "cargo test", "junit", "rspec", "gotest", "test-run", "vitest")),
    ("harness", ("ctx-harness", "ctx ", "ctx.", "mcp.ctx", "digest", "artifact")),
    ("repository", ("repository", "file", "read", "search", "grep", "glob", "edit", "outline")),
    ("docs", ("instruction", "readme", "guide", "steering")),
)
_PHASE_OF_FAMILY = {
    "repository": "explore", "semantic-analysis": "explore", "browser": "explore",
    "testing": "verify",
    "remote-source-control": "deliver", "deployment": "deliver", "cloud": "deliver",
    "collaboration": "deliver", "database": "deliver",
    "harness": "explore", "docs": "explore",
}
# Host-native tools + shells: a reference to these is always resolved (never a
# broken dependency), regardless of which host is active.
NATIVE_TOOLS = frozenset({
    "bash", "sh", "shell", "read", "edit", "write", "multiedit", "notebookedit",
    "grep", "glob", "ls", "cat", "cd", "git", "pytest", "npm", "node", "python",
    "python3", "cargo", "go", "make", "rg", "ripgrep", "sed", "awk", "find",
    "echo", "curl", "apply_patch", "view_file", "str_replace", "webfetch", "websearch",
})
# References to MCP tools: mcp__<server>__<tool> (Claude/Codex) or mcp.<server>.
# Capture ONLY the server segment (stops at the next `_`/`.` separator), so
# mcp__github__search_code resolves to the server "github", not the tool.
_MCP_REF_RE = re.compile(r"\bmcp(?:__|\.)([a-z0-9][a-z0-9-]*)")
# `ctx <verb>` references and backtick code spans naming a tool.
_CTX_REF_RE = re.compile(r"\bctx\s+([a-z][a-z-]+)")
_BACKTICK_RE = re.compile(r"`([^`]{1,60})`")


def family_of(cap: Capability) -> str:
    if cap.kind == "repo_instructions":
        return "docs"
    if cap.kind == "policy":
        return "harness"
    blob = f"{cap.provider} {cap.id} {cap.detail}".lower()
    for family, kws in FAMILY_RULES:
        if any(kw in blob for kw in kws):
            return family
    return "other"


def provides_of(cap: Capability) -> tuple[str, ...]:
    blob = f"{cap.id} {cap.detail}".lower()
    return tuple(k for k in _CAPABILITY_KEYS if k in blob)


def _extract_requires(text: str) -> tuple[list[str], list[str]]:
    """(mcp server refs, tool refs) named in prose. Precise on purpose: only
    references shaped like a tool are collected, so broken-dependency signals
    stay low-noise."""
    servers = sorted({m.lower() for m in _MCP_REF_RE.findall(text)})
    tools: set[str] = set()
    for m in _CTX_REF_RE.findall(text):
        tools.add(f"ctx {m.lower()}")
    for span in _BACKTICK_RE.findall(text):
        tok = span.strip().lower()
        head = tok.split()[0] if tok else ""
        if head in NATIVE_TOOLS or head == "ctx" or head.startswith("mcp"):
            tools.add(tok)
    return servers, sorted(tools)


def enrich_graph(records: list[Capability], ws_root: Path | str) -> list[Capability]:
    """Populate family / provides / requires / phase / unresolved. Broken
    dependency = a prose capability names an MCP server that is not configured
    in this workspace. Fail-open (re-reads sources; missing text ⇒ no refs)."""
    root = Path(ws_root)
    configured = {c.provider.lower() for c in records if c.kind in ("mcp_server", "mcp_tool")}
    out: list[Capability] = []
    for cap in records:
        family = family_of(cap)
        provides = provides_of(cap)
        phase = _PHASE_OF_FAMILY.get(family, "")
        requires: tuple[str, ...] = ()
        unresolved: tuple[str, ...] = ()
        if cap.kind in ("skill", "agent", "repo_instructions"):
            text = _read_text(root / cap.source) or ""
            servers, tools = _extract_requires(text)
            requires = tuple(sorted(set(servers) | set(tools)))
            unresolved = tuple(s for s in servers
                               if s not in configured and s not in ("ctx", "ctx-harness"))
        out.append(_with(cap, family=family, provides=provides, phase=phase,
                         requires=requires, unresolved=unresolved))
    return out


def build_graph(records: list[Capability]) -> dict[str, Any]:
    """Family rollup, redundancy clusters (shared provides), and broken
    dependencies (unresolved references). Descriptive — no mutation."""
    families: dict[str, dict[str, Any]] = {}
    for c in records:
        fam = families.setdefault(c.family or "other", {"count": 0, "tokens": 0, "ids": []})
        fam["count"] += 1
        fam["tokens"] += c.tokens
        fam["ids"].append(c.id)
    clusters: dict[str, list[str]] = {}
    for c in records:
        for tag in c.provides:
            clusters.setdefault(tag, []).append(c.id)
    redundancy = {tag: ids for tag, ids in sorted(clusters.items()) if len(ids) > 1}
    broken = {c.id: list(c.unresolved) for c in records if c.unresolved}
    return {
        "families": {k: families[k] for k in sorted(families)},
        "redundancy_clusters": redundancy,
        "broken_dependencies": broken,
    }


def render_graph(records: list[Capability], graph: dict[str, Any]) -> str:
    lines = ["CAPABILITY GRAPH", "─" * 56, "Families:"]
    for fam, slot in sorted(graph["families"].items(), key=lambda kv: -kv[1]["tokens"]):
        lines.append(f"  {fam:<22} {slot['count']:>3} · {slot['tokens']:>7,} tok")
    if graph["redundancy_clusters"]:
        lines.append("Redundancy clusters (shared capability, shadow):")
        for tag, ids in graph["redundancy_clusters"].items():
            lines.append(f"  {tag:<12} {', '.join(ids)}")
    if graph["broken_dependencies"]:
        lines.append("Broken dependencies (referenced but not configured):")
        for cid, refs in graph["broken_dependencies"].items():
            lines.append(f"  {cid} → {', '.join(refs)}")
    else:
        lines.append("Broken dependencies: none")
    return "\n".join(lines)


# ---------------------------------------------------------------- audit
def recommended_level(cap: Capability) -> str:
    """Suggested progressive-disclosure level (advisory only). Read-only,
    used, or ctx's own bounded tools stay L0; higher authority + unused →
    deferred. Never enforced in Phase 1."""
    if cap.provider == "ctx" or cap.id.startswith("mcp.ctx"):
        return "L0"
    if cap.kind in ("policy", "repo_instructions"):
        return "L0"  # steering the host needs each turn; not deferrable
    used = cap.invocations > 0
    # Action kinds: gate by real authority.
    if cap.kind in _ACTION_KINDS:
        if cap.authority == "destructive":
            return "L4" if not used else "L2"
        if cap.authority == "remote-write":
            return "L3" if not used else "L1"
        if cap.invocations == 0:
            return "L2"
        if cap.authority == "read" and used:
            return "L0"
        return "L1"
    # Prose kinds (skills, agents): a compact index by default (L1); a skill
    # that surfaces high-authority actions it can't gate is worth deferring to
    # a task-signal trigger (L2).
    if any(t.startswith("capability-mention:destructive") for t in cap.leakage):
        return "L2"
    return "L1"


def audit(ws_root: Path | str, *, probe_mcp: bool = False, timeout: float = 8.0) -> dict[str, Any]:
    """Full structured surface audit. Deterministic; fail-open per collector.
    ``probe_mcp`` spawns each configured MCP server to measure its real
    per-tool schema tokens (the static inventory sees only the registration
    line)."""
    base = collect_surface(ws_root)
    if probe_mcp:
        probed = probe_surface(ws_root, timeout=timeout)
        if probed:
            # Drop the coarse mcp_server placeholders whose tools we measured;
            # keep servers that failed to probe so they are still visible.
            probed_providers = {p.provider for p in probed}
            base = [c for c in base
                    if not (c.kind == "mcp_server" and c.provider in probed_providers)]
            base = base + probed
    records = enrich_graph(detect_overlaps(base), ws_root)
    counts = observed_tool_counts(ws_root)
    records = [_with(c, invocations=_match_invocations(c, counts)) for c in records]
    levels = {c.id: recommended_level(c) for c in records}
    graph = build_graph(records)

    by_kind: dict[str, dict[str, int]] = {}
    for c in records:
        slot = by_kind.setdefault(c.kind, {"count": 0, "tokens": 0})
        slot["count"] += 1
        slot["tokens"] += c.tokens
    total_tokens = sum(c.tokens for c in records)

    unused_authority = [c for c in records
                        if c.authority in ("remote-write", "destructive") and c.invocations == 0]
    never_used = [c for c in records if c.invocations == 0]
    leaky = [c for c in records if c.leakage]
    clusters: dict[str, list[str]] = {}
    for c in records:
        for peer in c.overlaps:
            key = tuple(sorted((c.id, peer)))
            clusters.setdefault("|".join(key), [])
    trim = [c for c in records if levels[c.id] not in ("L0", "L1")]
    trim_savings = sum(c.tokens for c in trim)

    record_dicts = []
    for c in records:
        d = c.as_dict()
        d["recommended_level"] = levels[c.id]
        record_dicts.append(d)

    return {
        "schema": SCHEMA,
        "workspace": str(ws_root),
        "totals": {"capabilities": len(records), "static_tokens": total_tokens,
                   "by_kind": by_kind},
        "records": record_dicts,
        "unused_high_authority": [c.id for c in unused_authority],
        "never_used": [c.id for c in never_used],
        "leakage": {c.id: list(c.leakage) for c in leaky},
        "overlap_clusters": sorted(clusters.keys()),
        "trim_preview": {"ids": [c.id for c in trim], "est_token_reduction": trim_savings},
        "graph": graph,
        "blind_spot": ("host system prompt + native tool schemas are not "
                       "file-visible and are excluded from this audit"),
    }


# ---------------------------------------------------------------- rendering
def render_inventory(records: list[Capability]) -> str:
    lines = [f"[ctx surface inventory · {len(records)} capabilities]"]
    for c in records:
        used = "—" if c.invocations < 0 else str(c.invocations)
        flags = ",".join(c.leakage) if c.leakage else ""
        lines.append(
            f"  {c.tokens:>6,} tok  {c.kind:<17} {c.id:<28} "
            f"auth={c.authority:<12} used={used:<4} {flags}".rstrip()
        )
    return "\n".join(lines)


def render_audit(a: dict[str, Any]) -> str:
    t = a["totals"]
    lines = ["SESSION SURFACE AUDIT",
             "─" * 56,
             "Discretionary context (host kernel excluded — blind spot):"]
    for kind, slot in sorted(t["by_kind"].items(), key=lambda kv: -kv[1]["tokens"]):
        lines.append(f"  {kind:<18} {slot['count']:>3} · {slot['tokens']:>7,} tok")
    lines.append(f"  {'TOTAL':<18} {t['capabilities']:>3} · {t['static_tokens']:>7,} tok/turn")
    if a["never_used"]:
        lines.append(f"Never used (observed): {len(a['never_used'])} — "
                     + ", ".join(a["never_used"][:6]))
    if a["unused_high_authority"]:
        lines.append("High-authority but unused: " + ", ".join(a["unused_high_authority"]))
    if a["leakage"]:
        lines.append(f"Leakage flags: {len(a['leakage'])} capabilities")
        for cid, tags in list(a["leakage"].items())[:6]:
            lines.append(f"  {cid}: {', '.join(tags)}")
    if a["overlap_clusters"]:
        lines.append(f"Overlap clusters (shadow): {len(a['overlap_clusters'])}")
    tp = a["trim_preview"]
    if tp["ids"]:
        lines.append(f"Preview-trim candidates: {len(tp['ids'])} · "
                     f"~{tp['est_token_reduction']:,} tok/turn recoverable "
                     f"(advisory — nothing hidden)")
    return "\n".join(lines)


def render_explain(cap: Capability) -> str:
    lvl = recommended_level(cap)
    used = "not observed" if cap.invocations < 0 else f"{cap.invocations} invocations"
    lines = [
        cap.id,
        f"  kind/provider : {cap.kind} / {cap.provider}",
        f"  source        : {cap.source}",
        f"  static cost   : {cap.tokens:,} tokens/turn",
        f"  authority     : {cap.authority}",
        f"  usage         : {used}",
    ]
    if cap.overlaps:
        lines.append(f"  overlaps      : {', '.join(cap.overlaps)}")
    if cap.leakage:
        lines.append(f"  leakage       : {', '.join(cap.leakage)}")
    if cap.sensitive_terms:
        lines.append(f"  sensitive     : {', '.join(cap.sensitive_terms)}")
    lines.append(f"  recommended   : {lvl} "
                 + ("(keep visible)" if lvl in ("L0", "L1") else "(defer until earned)"))
    return "\n".join(lines)


# ---------------------------------------------------------------- MCP probe
def _mcp_server_commands(ws_root: Path | str) -> dict[str, list[str]]:
    """Map server name → argv, from every host config. Fail-open."""
    root = Path(ws_root)
    out: dict[str, list[str]] = {}

    def _add(name: str, cfg: dict) -> None:
        cmd = cfg.get("command")
        if isinstance(cmd, str) and cmd and name not in out and cfg.get("disabled") is not True:
            out[name] = [cmd] + [str(a) for a in (cfg.get("args") or [])]

    for rel in _MCP_CONFIG_LOCATIONS:
        text = _read_text(root / rel)
        if not text:
            continue
        try:
            doc = json.loads(text)
        except json.JSONDecodeError:
            continue
        for name, cfg in ((doc.get("mcpServers") if isinstance(doc, dict) else {}) or {}).items():
            if isinstance(cfg, dict):
                _add(name, cfg)
    for cfg_path in (root / ".agents" / "plugins").glob("*/mcp_config.json"):
        text = _read_text(cfg_path)
        try:
            doc = json.loads(text) if text else {}
        except json.JSONDecodeError:
            doc = {}
        for name, cfg in (doc.get("mcpServers") or {}).items():
            if isinstance(cfg, dict):
                _add(name, cfg)
    codex = _read_text(root / ".codex" / "config.toml")
    if codex:
        try:
            import tomllib

            for name, cfg in (tomllib.loads(codex).get("mcp_servers") or {}).items():
                if isinstance(cfg, dict):
                    _add(name, cfg)
        except Exception:
            pass
    return out


def probe_mcp_tools(argv: list[str], *, timeout: float = 8.0) -> list[dict[str, Any]]:
    """Speak MCP over stdio to a server and return one record per advertised
    tool: name, its exact schema+description token cost, and inferred
    authority. This is the real per-turn tax of an MCP server — the thing the
    static file inventory cannot see. Fail-open to []."""
    import subprocess

    def _frame(obj: dict) -> bytes:
        return (json.dumps(obj) + "\n").encode("utf-8")

    init = _frame({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"protocolVersion": "2024-11-05",
                              "capabilities": {}, "clientInfo": {"name": "ctx-surface", "version": "1"}}})
    inited = _frame({"jsonrpc": "2.0", "method": "notifications/initialized"})
    listing = _frame({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    try:
        proc = subprocess.run(
            argv, input=init + inited + listing,
            capture_output=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    tools: list[dict[str, Any]] = []
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("id") == 2 and isinstance(msg.get("result"), dict):
            for tool in msg["result"].get("tools") or []:
                if not isinstance(tool, dict):
                    continue
                name = str(tool.get("name", ""))
                desc = str(tool.get("description", ""))
                schema = json.dumps(tool.get("inputSchema") or {}, sort_keys=True)
                tokens = _tokens_of(name + desc + schema)
                tools.append({
                    "name": name,
                    "tokens": tokens,
                    "description_tokens": _tokens_of(desc),
                    "schema_tokens": _tokens_of(schema),
                    "authority": infer_authority(name, desc),
                })
    return tools


def _probe_cache_key(argv: list[str]) -> str:
    return hashlib.sha256(json.dumps(argv, sort_keys=True).encode()).hexdigest()[:16]


def probe_surface(ws_root: Path | str, *, timeout: float = 8.0,
                  use_cache: bool = True) -> list[Capability]:
    """Expand each MCP server into per-tool records with measured schema
    tokens. Spawns each server, but caches the result under
    ``.ctx-surface/probe-cache.json`` keyed by the server's argv, so only the
    first session (or a changed command) pays the spawn cost — the pre-flight
    gate can then price the real MCP surface cheaply every session. A failed
    probe is not cached (retried next time). Returns [] when nothing probes."""
    root = Path(ws_root)
    cache: dict[str, Any] = {}
    cpath = root / _PROBE_CACHE
    if use_cache:
        try:
            cache = json.loads(cpath.read_text(encoding="utf-8"))
            if not isinstance(cache, dict):
                cache = {}
        except Exception:
            cache = {}
    out: list[Capability] = []
    dirty = False
    for name, argv in sorted(_mcp_server_commands(ws_root).items()):
        key = _probe_cache_key(argv)
        entry = cache.get(name) if use_cache else None
        if isinstance(entry, dict) and entry.get("key") == key:
            tools = entry.get("tools") or []
        else:
            tools = probe_mcp_tools(argv, timeout=timeout)
            if tools:  # cache only successful probes
                cache[name] = {"key": key, "tools": tools}
                dirty = True
        for tool in tools:
            leak, terms = _leakage_tags(tool["name"] + " " + str(tool.get("description", "")),
                                        "mcp_tool", tool["authority"], -1)
            out.append(Capability(
                id=f"mcp.{name}.{tool['name']}", kind="mcp_tool", provider=name,
                source=f"mcp:{name}", tokens=tool["tokens"], authority=tool["authority"],
                activation="always", invocations=-1, leakage=leak, sensitive_terms=terms,
                detail=f"schema {tool['schema_tokens']} + desc {tool['description_tokens']} tok",
            ))
    if use_cache and dirty:
        try:
            cpath.parent.mkdir(parents=True, exist_ok=True)
            cpath.write_text(json.dumps(cache, sort_keys=True), encoding="utf-8")
        except Exception:
            pass
    return out


# ------------------------------------------------------- SessionStart gate
def preflight(ws_root: Path | str, *, max_static_tokens: int = 8000,
              default_profile: str = "", gateway: bool = False,
              probe: bool = True) -> str:
    """Pre-flight surface audit run once at session start — 'bound before
    bloat', the mirror of the output side's 'capture before flood'. Returns a
    bounded advisory string (empty when the surface is within budget). Never
    raises: a broken audit yields no advisory, never a blocked session.

    Advisory, not blocking: true enforcement is structural (route MCP through
    ``ctx surface gateway`` so unrevealed schemas never load). This gate makes
    the harness *notice* bloat before the first turn and name the cheaper
    path."""
    try:
        # Probe (cached) so the biggest cost — real MCP tool schemas — is
        # counted; without it the gate is blind to server bloat.
        a = audit(ws_root, probe_mcp=probe)
    except Exception:
        return ""
    total = a["totals"]["static_tokens"]
    if total <= max_static_tokens:
        return ""
    lines = [
        f"CTX_SURFACE_GUARD: discretionary capability surface is {total:,} "
        f"tokens/turn (budget {max_static_tokens:,}). This is re-sent every "
        f"turn before any tool runs."
    ]
    by_kind = a["totals"]["by_kind"]
    top = sorted(by_kind.items(), key=lambda kv: -kv[1]["tokens"])[:3]
    lines.append("  heaviest: " + ", ".join(
        f"{k} {v['tokens']:,}tok" for k, v in top))
    if a.get("never_used"):
        lines.append(f"  never used (observed): {len(a['never_used'])}")
    if a.get("unused_high_authority"):
        lines.append("  unused high-authority: "
                     + ", ".join(a["unused_high_authority"][:4]))
    broken = a.get("graph", {}).get("broken_dependencies") or {}
    if broken:
        lines.append("  broken deps: " + ", ".join(
            f"{cid}→{','.join(refs)}" for cid, refs in list(broken.items())[:3]))
    prof = default_profile or "local-dev"
    if gateway:
        lines.append("  → gateway active: reveal families on demand "
                     "(surface_reveal); unrevealed tool schemas are not loaded")
    else:
        lines.append(f"  → bound it: ctx surface compile --profile {prof} --host <host> --apply")
        lines.append("     or route MCP through `ctx surface gateway` for per-tool disclosure")
    return "\n".join(lines)


__all__ = [
    "SCHEMA", "Capability", "collect_surface", "detect_overlaps",
    "observed_tool_counts", "audit", "recommended_level", "probe_surface",
    "probe_mcp_tools", "render_inventory", "render_audit", "render_explain",
    "infer_authority", "enrich_graph", "build_graph", "render_graph",
    "family_of", "provides_of", "FAMILY_RULES", "NATIVE_TOOLS", "preflight",
]
