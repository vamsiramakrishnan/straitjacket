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

# v2 (from v1): a Claude Code skill's ``tokens`` is now its LISTING entry (name
# plus description, capped as the host caps it), not its whole file; the body
# moved to ``deferred_tokens``. Records also carry ``scope``, ``transport``,
# ``upfront_deferred`` and ``pricing``, and discovery reaches user/local scope.
SCHEMA = "ctx.surface/v2"

#: Claude Code caps each skill listing entry at this many characters.
SKILL_LISTING_CAP_CHARS = 1536
#: Claude Code caps server instructions (and each up-front tool description).
MCP_INSTRUCTIONS_CAP_CHARS = 4096

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
    # ---- v2 pricing/discovery fields ----
    scope: str = "workspace"    # workspace | local | user (where the config lives)
    transport: str = ""         # mcp_server: stdio | http | sse | ws | unknown
    deferred_tokens: int = 0    # loaded only on demand (skill body, tool schema)
    upfront_deferred: int = -1  # up-front cost when the host DEFERS MCP; -1 = same as tokens
    pricing: str = ""           # which rule produced ``tokens`` (see _price_* helpers)

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
            "scope": self.scope, "transport": self.transport,
            "deferred_tokens": self.deferred_tokens,
            "upfront_deferred": self.upfront_deferred, "pricing": self.pricing,
        }

    @property
    def upfront_if_deferred(self) -> int:
        return self.tokens if self.upfront_deferred < 0 else self.upfront_deferred


# ---------------------------------------------------------------- helpers
def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


#: Config/skill files larger than this are not read (a foreign file must not
#: stall or flood discovery).
MAX_READ_BYTES = 256 << 20  # ~/.claude.json carries history; real files stay far below

#: Config files that exist but were skipped on their most recent read, with
#: the reason (size, type, parse). Surfaced by :func:`discovery_gaps`, so no
#: part of discovery fails silently.
_SKIPPED_CONFIGS: dict[str, str] = {}


def _read_text(path: Path, max_bytes: int | None = None) -> str | None:
    """Text of a REGULAR file up to ``max_bytes`` (default
    :data:`MAX_READ_BYTES`); None otherwise. A FIFO or device at a config
    path would block ``open`` forever, so the type is checked first and the
    file is opened non-blocking."""
    import os
    import stat as _stat

    if max_bytes is None:
        max_bytes = MAX_READ_BYTES
    try:
        st = os.stat(path)
        if not _stat.S_ISREG(st.st_mode) or st.st_size > max_bytes:
            return None
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as fh:
            if not _stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                return None
            data = fh.read(max_bytes + 1)
        if len(data) > max_bytes:
            return None
        return data.decode("utf-8")
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def _tokens_of(text: str) -> int:
    return estimate_tokens(len(text.encode("utf-8")))


def infer_authority(*texts: str) -> str:
    blob = " ".join(t.lower() for t in texts if t)
    for tier in ("destructive", "remote-write", "local-write", "read"):
        if any(kw in blob for kw in _AUTHORITY_KEYWORDS[tier]):
            return tier
    return "unknown"


def _provider_match(tool_name: str, provider: str) -> bool:
    """Is this observed tool call one of ``provider``'s?

    Both spellings, each terminated: ``mcp__<p>__<tool>`` and
    ``mcp.<p>.<tool>``, plus the bare server name itself.
    """
    for stem, sep in ((f"mcp__{provider}", "__"), (f"mcp.{provider}", ".")):
        if tool_name == stem or tool_name.startswith(stem + sep):
            return True
    return False


def _excessive_authority(kind: str, authority: str, invocations: int) -> bool:
    """The rule, in one place. It is DERIVED from invocations, which is why
    it cannot be computed once and carried."""
    return (
        kind in _ACTION_KINDS
        and authority in ("remote-write", "destructive")
        and invocations in (0, -1)
    )


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
        if _excessive_authority(kind, authority, invocations):
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

#: Enterprise-managed MCP config. When one exists, Claude Code EXITS with an
#: error if ``--mcp-config`` is passed, so the launch planner must never pass
#: it. Linux and macOS locations (verified); other platforms are not covered.
MANAGED_MCP_PATHS: tuple[str, ...] = (
    "/etc/claude-code/managed-mcp.json",
    "/Library/Application Support/ClaudeCode/managed-mcp.json",
)


def managed_mcp_config() -> str | None:
    """Path of an enterprise-managed MCP config, if one exists. Fail-open."""
    for p in MANAGED_MCP_PATHS:
        try:
            if Path(p).is_file():
                return p
        except OSError:
            continue
    return None


def claude_config_dir(env: dict[str, str] | None = None) -> Path:
    """Claude Code's user config directory: ``$CLAUDE_CONFIG_DIR`` or
    ``~/.claude``. Read-only use: ctx never writes here."""
    import os

    e = os.environ if env is None else env
    d = e.get("CLAUDE_CONFIG_DIR")
    return Path(d).expanduser() if d else Path(e.get("HOME") or Path.home()) / ".claude"


def claude_user_json(env: dict[str, str] | None = None) -> Path:
    """The file holding user-scope and local-scope MCP servers: ``~/.claude.json``,
    or ``$CLAUDE_CONFIG_DIR/.claude.json`` when the config dir is relocated."""
    import os

    e = os.environ if env is None else env
    if e.get("CLAUDE_CONFIG_DIR"):
        return claude_config_dir(e) / ".claude.json"
    return Path(e.get("HOME") or Path.home()) / ".claude.json"


@dataclass
class ServerSpec:
    """One configured MCP server as the host would launch it. ``env`` values
    can be secrets: they are never rendered by ``repr`` or any ctx output."""

    name: str
    host: str          # claude | codex | cursor | antigravity
    scope: str         # local | workspace | user
    source: str        # the config file it came from
    transport: str     # stdio | http | sse | ws | unknown
    command: str = ""
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    cwd: str = ""
    url: str = ""
    always_load: bool = False
    raw: dict[str, Any] = field(default_factory=dict)
    #: Claude project (.mcp.json) servers only: has the user approved it?
    #: None = not a project server / not determinable.
    approved: bool | None = None

    def __repr__(self) -> str:  # never print env values or argument values
        return (f"ServerSpec(name={self.name!r}, scope={self.scope!r}, "
                f"transport={self.transport!r}, command={safe_argv_text(self.argv)!r}, "
                f"env_keys={sorted(self.env)!r})")

    @property
    def argv(self) -> list[str]:
        return [self.command, *self.args] if self.command else []

    @property
    def url_host(self) -> str:
        try:
            from urllib.parse import urlparse

            return urlparse(self.url).hostname or ""
        except Exception:
            return ""


def _transport_of(cfg: dict[str, Any]) -> str:
    kind = str(cfg.get("type") or cfg.get("transport") or "").lower()
    if kind in ("http", "sse", "ws", "streamable-http", "streamable_http"):
        return "http" if kind.startswith("streamable") else kind
    if cfg.get("url") or cfg.get("serverUrl"):
        return "http"
    if isinstance(cfg.get("command"), str) and cfg.get("command"):
        return "stdio"
    return "unknown"


def _spec(name: str, cfg: Any, *, host: str, scope: str, source: str) -> ServerSpec | None:
    if not isinstance(cfg, dict) or cfg.get("disabled") is True or cfg.get("enabled") is False:
        return None
    env = cfg.get("env") if isinstance(cfg.get("env"), dict) else {}
    return ServerSpec(
        name=str(name), host=host, scope=scope, source=source,
        transport=_transport_of(cfg),
        command=str(cfg.get("command") or "") if isinstance(cfg.get("command"), str) else "",
        args=tuple(str(a) for a in (cfg.get("args") or []) if a is not None)
        if isinstance(cfg.get("args"), list) else (),
        env={str(k): str(v) for k, v in env.items()},
        cwd=str(cfg.get("cwd") or ""),
        url=str(cfg.get("url") or cfg.get("serverUrl") or ""),
        always_load=cfg.get("alwaysLoad") is True,
        raw=dict(cfg),
    )


def _skip_reason(path: Path) -> str | None:
    """Why ``_read_text`` returned nothing for an EXISTING path; None when
    the path does not exist (absence is not a gap)."""
    import os
    import stat as _stat

    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"unreadable ({type(exc).__name__})"
    if not _stat.S_ISREG(st.st_mode):
        return "not a regular file"
    if st.st_size > MAX_READ_BYTES:
        return f"larger than {MAX_READ_BYTES >> 20} MiB"
    return "unreadable or not UTF-8"


def _note_skip(path: Path, reason: str | None) -> None:
    key = str(path)
    if reason:
        _SKIPPED_CONFIGS[key] = reason
    else:
        _SKIPPED_CONFIGS.pop(key, None)


def _config_text(path: Path) -> str | None:
    text = _read_text(path)
    if text is None:
        _note_skip(path, _skip_reason(path))
    return text


def _json_doc(path: Path) -> dict[str, Any]:
    text = _config_text(path)
    if text is None:
        return {}
    if not text.strip():
        _note_skip(path, None)
        return {}
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        _note_skip(path, f"invalid JSON (line {exc.lineno})")
        return {}
    if not isinstance(doc, dict):
        _note_skip(path, "not a JSON object")
        return {}
    _note_skip(path, None)
    return doc


def _toml_doc(path: Path) -> dict[str, Any]:
    text = _config_text(path)
    if text is None:
        return {}
    try:
        import tomllib

        doc = tomllib.loads(text)
    except Exception:
        _note_skip(path, "invalid TOML")
        return {}
    _note_skip(path, None)
    return doc if isinstance(doc, dict) else {}


def _claude_local_servers(root: Path, env: dict[str, str] | None) -> tuple[dict, str]:
    """Local-scope servers: ``projects[<abs project path>].mcpServers`` in the
    user config file."""
    path = claude_user_json(env)
    projects = _json_doc(path).get("projects")
    if not isinstance(projects, dict):
        return {}, str(path)
    keys = []
    try:
        keys.append(str(root.resolve()))
    except OSError:
        pass
    keys.append(str(root))
    for key in keys:
        entry = projects.get(key)
        if isinstance(entry, dict) and isinstance(entry.get("mcpServers"), dict):
            return entry["mcpServers"], str(path)
    return {}, str(path)


def mcp_server_specs(ws_root: Path | str, *, user_scope: bool = True,
                     env: dict[str, str] | None = None) -> list[ServerSpec]:
    """Every configured MCP server, in precedence order, deduped by name
    (first wins): Claude local scope > workspace files > Claude user scope >
    Codex user config. Read-only and fail-open."""
    import os

    root = Path(ws_root)
    e = os.environ if env is None else env
    raw: list[tuple[str, Any, str, str, str]] = []  # name, cfg, host, scope, source
    if user_scope:
        servers, src = _claude_local_servers(root, e)
        raw += [(n, c, "claude", "local", src) for n, c in servers.items()]
    for rel in _MCP_CONFIG_LOCATIONS:
        doc = _json_doc(root / rel)
        servers = doc.get("mcpServers")
        host = "cursor" if rel.startswith(".cursor") else "claude"
        if isinstance(servers, dict):
            raw += [(n, c, host, "workspace", rel) for n, c in servers.items()]
    codex = _toml_doc(root / ".codex" / "config.toml").get("mcp_servers")
    if isinstance(codex, dict):
        raw += [(n, c, "codex", "workspace", ".codex/config.toml") for n, c in codex.items()]
    plugins = root / ".agents" / "plugins"
    if plugins.is_dir():
        for cfg_path in sorted(plugins.glob("*/mcp_config.json")):
            servers = _json_doc(cfg_path).get("mcpServers")
            if isinstance(servers, dict):
                raw += [(n, c, "antigravity", "workspace", _rel(cfg_path, root))
                        for n, c in servers.items()]
    if user_scope:
        path = claude_user_json(e)
        servers = _json_doc(path).get("mcpServers")
        if isinstance(servers, dict):
            raw += [(n, c, "claude", "user", str(path)) for n, c in servers.items()]
        if not e.get("CODEX_HOME"):
            cpath = Path(e.get("HOME") or Path.home()) / ".codex" / "config.toml"
            servers = _toml_doc(cpath).get("mcp_servers")
            if isinstance(servers, dict):
                raw += [(n, c, "codex", "user", str(cpath)) for n, c in servers.items()]
    approval = claude_project_approval(root, e) if user_scope else None
    out: list[ServerSpec] = []
    # Precedence is per HOST: a Cursor `remote1` must not hide Claude's.
    seen: set[tuple[str, str]] = set()
    for name, cfg, host, scope, source in raw:
        spec = _spec(name, cfg, host=host, scope=scope, source=source)
        key = (host, str(name))
        if spec is None or key in seen:
            # A disabled entry still claims its name, so a lower-precedence
            # definition cannot resurrect a server the user switched off.
            if isinstance(cfg, dict):
                seen.add(key)
            continue
        seen.add(key)
        if source == ".mcp.json":
            spec.approved = _approved(spec.name, approval)
        out.append(spec)
    return out


def claude_project_approval(root: Path, env: dict[str, str]) -> dict[str, Any]:
    """Claude Code starts a project (``.mcp.json``) server only once it is
    approved: ``enableAllProjectMcpServers``, or listed in
    ``enabledMcpjsonServers`` and not in ``disabledMcpjsonServers``. Those
    keys live in settings files and in the project entry of the user config."""
    enable_all = False
    enabled: set[str] = set()
    disabled: set[str] = set()
    docs = [_json_doc(claude_config_dir(env) / "settings.json"),
            _json_doc(root / ".claude" / "settings.json"),
            _json_doc(root / ".claude" / "settings.local.json")]
    projects = _json_doc(claude_user_json(env)).get("projects")
    if isinstance(projects, dict):
        for key in (str(root), _safe_resolve(root)):
            if isinstance(projects.get(key), dict):
                docs.append(projects[key])
                break
    for doc in docs:
        if doc.get("enableAllProjectMcpServers") is True:
            enable_all = True
        for k, bucket in (("enabledMcpjsonServers", enabled), ("disabledMcpjsonServers", disabled)):
            if isinstance(doc.get(k), list):
                bucket.update(str(x) for x in doc[k])
    return {"all": enable_all, "enabled": enabled, "disabled": disabled}


def _safe_resolve(p: Path) -> str:
    try:
        return str(p.resolve())
    except OSError:
        return str(p)


def _approved(name: str, approval: dict[str, Any] | None) -> bool | None:
    if approval is None:
        return None
    if name in approval["disabled"]:
        return False
    return bool(approval["all"] or name in approval["enabled"])


def discovery_gaps(ws_root: Path | str | None = None,
                   env: dict[str, str] | None = None) -> list[str]:
    """Sources ctx knows exist but deliberately does not read, each with the
    reason — declared omission rather than a silent blind spot."""
    import os

    e = os.environ if env is None else env
    gaps: list[str] = []
    plugins = claude_config_dir(e) / "plugins"
    try:
        if plugins.is_dir():
            gaps.append(f"claude plugins under {plugins}: plugin-provided MCP servers "
                        "and skills are not enumerated (on-disk layout not established)")
    except OSError:
        pass
    if e.get("CODEX_HOME"):
        gaps.append("CODEX_HOME is set: Codex user config not read (relocation "
                    "semantics not established in this repo)")
    if managed_mcp_config():
        gaps.append(f"managed MCP config {managed_mcp_config()}: not enumerated")
    for path, reason in sorted(_SKIPPED_CONFIGS.items()):
        gaps.append(f"config skipped: {path} ({reason}); its servers are not counted")
    return gaps


_FLAG_NAME_RE = re.compile(r"^--?[A-Za-z][A-Za-z0-9_-]{0,40}$")


def safe_argv_text(argv: list[str] | tuple[str, ...]) -> str:
    """A server command fit to SHOW: the program's base name, the number of
    arguments, and at most the flag NAMES (a dash-prefixed token up to any
    ``=``). Never an argument value, URL or positional — which values are
    secrets cannot be decided by pattern, so none are shown. The real argv
    is only ever used to spawn, or written to a 0600 launch file."""
    argv = [str(a) for a in argv]
    if not argv:
        return ""
    prog = re.split(r"[\\/]", argv[0])[-1]
    flags: list[str] = []
    for a in argv[1:]:
        name = a.split("=", 1)[0]
        if _FLAG_NAME_RE.match(name) and name not in flags:
            flags.append(name)
    n = len(argv) - 1
    text = f"{prog} ({n} arg{'s' if n != 1 else ''}"
    return text + (f"; flags {' '.join(flags)})" if flags else ")")


def _add_mcp_server(records: list[Capability], spec: ServerSpec) -> None:
    cid = f"mcp.{spec.name}"
    if any(r.id == cid for r in records):  # dedupe across host configs
        return
    if spec.transport == "stdio":
        detail = safe_argv_text(spec.argv)
        private = " ".join(spec.argv)  # classification only; never rendered
    else:
        # Never the full URL: paths and query strings carry tokens.
        detail = f"remote {spec.transport} {spec.url_host}".strip()
        private = detail
    # A registration line is cheap; the real cost is the server's tool schemas,
    # measured only under --probe-mcp. Mark that here.
    tokens = _tokens_of(f"{spec.name} {detail}")
    activation, pricing, prefix = "always", "unmeasured", "MCP server (tool schemas unmeasured; run --probe-mcp)"
    upfront = -1
    if spec.approved is False:
        # Claude Code does not start an unapproved/disabled project server,
        # so it costs nothing up front — the same verdict as the launch plan.
        activation, pricing, upfront = "not-started", "not-started (project server not approved)", 0
        prefix = "MCP project server NOT started by Claude Code (not approved or disabled)"
        tokens = 0
    family = family_of(Capability(id=cid, kind="mcp_server", provider=spec.name,
                                  source="", tokens=0, detail=private))
    records.append(Capability(
        id=cid, kind="mcp_server", provider=spec.name,
        source=spec.source, tokens=tokens,
        authority=infer_authority(spec.name, private), activation=activation, invocations=-1,
        detail=f"{prefix}: {detail}"[:200],
        scope=spec.scope, transport=spec.transport, pricing=pricing,
        upfront_deferred=upfront, family=family,
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


def _frontmatter(text: str) -> tuple[dict[str, str], str]:
    """(frontmatter scalars, body). A deliberately small YAML subset: ``key:
    value`` lines, with indented continuation lines folded into the value."""
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return {}, text
    meta: dict[str, str] = {}
    key = ""
    for line in lines[1:end]:
        if line[:1] in (" ", "\t") and key:
            meta[key] = (meta[key] + " " + line.strip()).strip()
            continue
        k, sep, v = line.partition(":")
        if not sep or not k.strip():
            continue
        key = k.strip()
        v = v.strip()
        meta[key] = "" if v in (">", "|", ">-", "|-") else v.strip("'\"")
    return meta, "\n".join(lines[end + 1:])


def skill_listing(path: Path, text: str, *, cap: int | None = SKILL_LISTING_CAP_CHARS
                  ) -> dict[str, Any]:
    """How Claude Code lists one skill up front: its name plus description
    (frontmatter ``description`` and ``when_to_use``, else the first paragraph
    of the body), capped at :data:`SKILL_LISTING_CAP_CHARS`. The body loads
    only on invocation. ``disable-model-invocation: true`` keeps the
    description out of context entirely."""
    meta, body = _frontmatter(text)
    name = meta.get("name") or path.parent.name
    desc = meta.get("description", "")
    if meta.get("when_to_use"):
        desc = (desc + " " + meta["when_to_use"]).strip()
    if not desc:
        para: list[str] = []
        for line in body.strip().splitlines():
            if not line.strip():
                if para:
                    break
                continue
            para.append(line.strip())
        desc = " ".join(para)
    entry = f"- {name}: {desc}"
    if cap is not None:
        entry = entry[:cap]
    hidden = str(meta.get("disable-model-invocation", "")).lower() == "true"
    return {"name": name, "entry": entry, "tokens": 0 if hidden else _tokens_of(entry),
            "body_tokens": _tokens_of(body), "model_invocable": not hidden}


def _is_claude_skill_dir(rel: str) -> bool:
    return rel.startswith(".claude/skills/") or rel.startswith("skills/")


def _is_antigravity_skill_dir(rel: str) -> bool:
    # Antigravity (documented, not observed): skill names + descriptions are
    # shown every turn, bodies on use; no documented per-entry cap or budget.
    return rel.startswith(".agents/") and "/skills/" in rel


#: Per-host up-front disclosure, as documented by each vendor (Claude Code
#: verified against its docs; Codex and Antigravity documented, not run live).
HOST_DISCLOSURE: dict[str, dict[str, str]] = {
    "claude": {"mcp": "deferred (tool search) unless ENABLE_TOOL_SEARCH=false, "
                      "experimental betas are disabled, a third-party base URL, "
                      "or a server sets alwaysLoad",
               "skills": "name + description, 1,536 chars/entry, 1% of window"},
    "codex": {"mcp": "deferred (tool search, always on)",
              "skills": "name + description + path within 2% of window"},
    "antigravity": {"mcp": "lazy: tool names only unless a tool sets eager: true",
                    "skills": "name + description every turn (no documented budget)"},
}


def _collect_files(root: Path, globs, kind: str, provider_of, records: list[Capability],
                   *, scope: str = "workspace") -> None:
    seen: set[Path] = set()
    ids: set[str] = {r.id for r in records}
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
            rel = _rel(path, root)
            tokens, deferred, pricing = _tokens_of(text), 0, "whole-file"
            listed = _is_claude_skill_dir(rel) or _is_antigravity_skill_dir(rel)
            if kind == "skill" and listed:
                if path.name.lower() == "skill.md":
                    listing = skill_listing(
                        path, text,
                        cap=None if _is_antigravity_skill_dir(rel) else SKILL_LISTING_CAP_CHARS)
                    tokens, deferred = listing["tokens"], listing["body_tokens"]
                    pricing = ("skill-listing" if listing["model_invocable"]
                               else "skill-hidden (disable-model-invocation)")
                else:
                    # A supporting file inside a skill dir is read only when
                    # the skill runs: nothing up front.
                    tokens, deferred, pricing = 0, _tokens_of(text), "skill-resource"
            leakage, terms = _leakage_tags(text, kind, "n/a", -1)
            records.append(Capability(
                id=cid, kind=kind, provider=provider_of(rel),
                source=rel if scope == "workspace" else str(path),
                tokens=tokens, authority="n/a", activation="always",
                invocations=-1, sensitive_terms=terms, leakage=leakage,
                detail=(text.strip().splitlines() or [""])[0][:EVIDENCE_LINE_CHARS],
                scope=scope, deferred_tokens=deferred, pricing=pricing,
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
            pricing="whole-file",
        ))


def _collect_policy(root: Path, records: list[Capability]) -> None:
    for name in ("ctx.toml", "ctx-policy.toml"):
        text = _read_text(root / name)
        if not text:
            continue
        records.append(Capability(
            id=f"policy.{name}", kind="policy", provider="ctx", source=name,
            tokens=_tokens_of(text), authority="read", activation="always",
            invocations=-1, detail="harness policy text", pricing="whole-file",
        ))


def collect_surface(ws_root: Path | str, *, user_scope: bool = True,
                    env: dict[str, str] | None = None) -> list[Capability]:
    """Inventory the discretionary capability surface: workspace files plus,
    with ``user_scope``, Claude Code user/local-scope MCP servers, user skills
    and agents, and Codex's user config. Deterministic (sorted), fail-open,
    read-only — no user-scope file is ever written."""
    root = Path(ws_root)
    records: list[Capability] = []
    for spec in mcp_server_specs(root, user_scope=user_scope, env=env):
        _add_mcp_server(records, spec)
    _collect_files(root, _SKILL_GLOBS, "skill",
                   lambda r: "claude" if r.startswith(".claude") else "repo", records)
    _collect_files(root, _AGENT_GLOBS, "agent",
                   lambda r: "claude" if r.startswith(".claude") else "repo", records)
    if user_scope:
        cdir = claude_config_dir(env)
        try:
            if cdir.is_dir() and cdir.resolve() != (root / ".claude").resolve():
                _collect_files(cdir, ("skills/*/SKILL.md", "skills/*/**/*.md"), "skill",
                               lambda r: "claude-user", records, scope="user")
                _collect_files(cdir, ("agents/*.md",), "agent",
                               lambda r: "claude-user", records, scope="user")
        except OSError:
            pass
    _collect_repo_instructions(root, records)
    _collect_policy(root, records)
    records.sort(key=lambda r: (-r.tokens, r.kind, r.id))
    return records


# ---------------------------------------------------------------- usage evidence
def claude_project_dir(ws_root: Path | str, env: dict[str, str] | None = None) -> Path:
    """Where Claude Code keeps this project's session transcripts:
    ``<config dir>/projects/<project path with every non-alphanumeric
    character replaced by '-'>``."""
    try:
        resolved = str(Path(ws_root).resolve())
    except OSError:
        resolved = str(ws_root)
    return claude_config_dir(env) / "projects" / re.sub(r"[^A-Za-z0-9]", "-", resolved)


_COMMAND_NAME_RE = re.compile(r"<command-name>/?([^<\s]+)</command-name>")


def _scan_transcript(path: Path, skills: dict[str, int], tools: dict[str, int],
                     budget: list[int]) -> tuple[int, bool]:
    """Fold one transcript's invocations into the counters. Returns
    (recognized conversation events, read to the end?)."""
    recognized = 0
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            budget[0] -= len(line)
            if budget[0] < 0:
                return recognized, False
            if not any(m in line for m in _EVENT_MARKERS):
                if "<command-name>" in line:
                    for m in _COMMAND_NAME_RE.findall(line):
                        skills[m] = skills.get(m, 0) + 1
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(ev, dict) or ev.get("type") not in ("user", "assistant") \
                    or not isinstance(ev.get("message"), dict):
                continue
            recognized += 1
            for m in _COMMAND_NAME_RE.findall(line):
                skills[m] = skills.get(m, 0) + 1
            content = ev["message"].get("content")
            if not isinstance(content, list):
                continue
            for b in content:
                if not isinstance(b, dict) or b.get("type") != "tool_use":
                    continue
                name = str(b.get("name") or "")
                if name == "Skill":
                    inp = b.get("input") or {}
                    sk = str(inp.get("skill") or inp.get("command") or "").lstrip("/")
                    if sk:
                        skills[sk] = skills.get(sk, 0) + 1
                elif name.startswith("mcp__"):
                    tools[name] = tools.get(name, 0) + 1
    return recognized, True


_EVENT_MARKERS = ('"type":"user"', '"type":"assistant"', '"type": "user"', '"type": "assistant"')


def transcript_usage(ws_root: Path | str, *, env: dict[str, str] | None = None,
                     max_files: int = 200, max_bytes: int = 64 << 20) -> dict[str, Any] | None:
    """Skill and MCP-tool invocations recorded in this project's Claude Code
    transcripts — usage evidence available from the first wrapped session.

    ``sessions`` counts only top-level session files READ TO THE END that
    hold at least one recognized conversation event; files with none are
    counted as ``unrecognized`` and give no evidence. Sub-agent transcripts
    (``<session>/subagents/*.jsonl``, the layout observed on Claude Code
    2.1.322) add invocations but are not sessions. ``partial`` is set when a
    cap stopped the scan. None when there is no transcript at all (no
    evidence, as opposed to evidence of zero use). Fail-open."""
    pdir = claude_project_dir(ws_root, env)
    try:
        files = sorted(pdir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    if not files:
        return None
    skills: dict[str, int] = {}
    tools: dict[str, int] = {}
    budget = [max_bytes]
    sessions = unrecognized = subagent_files = 0
    newest = 0.0
    partial = len(files) > max_files
    for path in files[:max_files]:
        try:
            recognized, complete = _scan_transcript(path, skills, tools, budget)
        except OSError:
            unrecognized += 1
            continue
        if not complete:
            partial = True
            break
        if not recognized:
            unrecognized += 1
            continue
        sessions += 1
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            pass
        for sub in sorted((path.parent / path.stem / "subagents").glob("*.jsonl")):
            try:
                _, done = _scan_transcript(sub, skills, tools, budget)
            except OSError:
                continue
            subagent_files += 1
            if not done:
                partial = True
                break
        if budget[0] < 0:
            partial = True
            break
    return {"sessions": sessions, "unrecognized": unrecognized,
            "subagent_files": subagent_files,
            "skills": dict(sorted(skills.items())),
            "mcp_tools": dict(sorted(tools.items())), "newest_mtime": newest,
            "source": "claude-transcripts", "partial": partial, "truncated": partial}


# ---------------------------------------------------------------- utilization
def observed_tool_counts(ws_root: Path | str) -> dict[str, int]:
    """Per-tool invocation counts observed by the proxy wire log across this
    workspace's sessions. Fail-open to empty."""
    counts: dict[str, int] = {}
    proxy = session_reads_path(ws_root, PROXY_SUBDIR)
    if not proxy.is_dir():
        return counts
    for wire in proxy.glob("**/wire.jsonl"):
        text = _read_text(wire, max_bytes=1 << 30)
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
        # The DELIMITER is what makes a prefix a provider name. Bare
        # startswith let a server called `git` absorb every `mcp__github__*`
        # invocation, so `github` looked never-used and `git` looked busy --
        # and never_used / unused_high_authority / excessive-authority are all
        # computed from this number. Same boundary defect as matching a path
        # glob or an intent keyword without one.
        return sum(n for name, n in counts.items()
                   if _provider_match(name, cap.provider))
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
    """A capability with fields replaced -- and its DERIVED fields reconciled.

    `excessive-authority` means "can act, never observed acting". probe_surface
    computes it with invocations hardcoded to -1 (unknown), and audit() then
    joins the real wire-log counts through this function. Nothing recomputed
    the tag, so a tool the audit had just PROVEN was used kept a label saying
    it never was. Derived state outliving its source is a defect class this
    branch has now met more than once; the single mutation door is where it
    stops.
    """
    data = cap.as_dict()
    data.update(changes)
    if "invocations" in changes:
        tags = [t for t in data["leakage"] if t != "excessive-authority"]
        if _excessive_authority(data["kind"], data["authority"], data["invocations"]):
            tags.append("excessive-authority")
        data["leakage"] = tuple(tags)
    return Capability(
        id=data["id"], kind=data["kind"], provider=data["provider"], source=data["source"],
        tokens=data["tokens"], authority=data["authority"], activation=data["activation"],
        invocations=data["invocations"], sensitive_terms=tuple(data["sensitive_terms"]),
        leakage=tuple(data["leakage"]), overlaps=tuple(data["overlaps"]), detail=data["detail"],
        family=data.get("family", ""), provides=tuple(data.get("provides", ())),
        requires=tuple(data.get("requires", ())), phase=data.get("phase", ""),
        unresolved=tuple(data.get("unresolved", ())),
        scope=data.get("scope", "workspace"), transport=data.get("transport", ""),
        deferred_tokens=data.get("deferred_tokens", 0),
        upfront_deferred=data.get("upfront_deferred", -1), pricing=data.get("pricing", ""),
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
        family = cap.family or family_of(cap)
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


def audit(ws_root: Path | str, *, probe_mcp: bool = False, timeout: float = 8.0,
          automatic: bool = False) -> dict[str, Any]:
    """Full structured surface audit. Deterministic; fail-open per collector.
    ``probe_mcp`` spawns each configured MCP server to measure its real
    per-tool schema tokens (the static inventory sees only the registration
    line). ``automatic`` (the SessionStart gate) restricts probing to
    workspace servers launched as before; a person's ``--probe-mcp`` also
    probes user/local-scope servers."""
    base = collect_surface(ws_root)
    if probe_mcp:
        probed = probe_surface(ws_root, timeout=timeout, explicit=not automatic)
        if probed:
            # Drop the coarse mcp_server placeholders whose tools we measured;
            # keep servers that failed to probe so they are still visible.
            probed_providers = {p.provider for p in probed}
            base = [c for c in base
                    if not (c.kind == "mcp_server" and c.provider in probed_providers)]
            base = base + probed
    # Unapproved/disabled Claude project servers cost nothing up front (Claude
    # Code does not start them) — measured or not, as the launch plan says.
    unapproved = {s.name for s in mcp_server_specs(ws_root)
                  if s.host == "claude" and s.approved is False}
    base = [_with(c, tokens=0, upfront_deferred=0, deferred_tokens=c.tokens,
                  activation="not-started", pricing="not-started (project server not approved)")
            if c.kind in ("mcp_server", "mcp_tool") and c.provider in unapproved
            and c.activation != "not-started" else c for c in base]
    # Remembered probe failures: the server is priced as its registration
    # line only, which is a floor, and the reader is told so.
    failed = probe_failures(ws_root)
    base = [_with(c, pricing="probe-failed (floor)",
                  detail=(f"probe FAILED {failed[c.provider]}; figure is a floor "
                          f"(registration line only); retry: {PROBE_RETRY_HINT}")[:200])
            if c.kind == "mcp_server" and c.provider in failed
            and c.activation != "not-started" else c for c in base]
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
    trim_savings_deferred = sum(c.upfront_if_deferred for c in trim)

    record_dicts = []
    for c in records:
        d = c.as_dict()
        d["recommended_level"] = levels[c.id]
        record_dicts.append(d)

    return {
        "schema": SCHEMA,
        "workspace": str(ws_root),
        "totals": {"capabilities": len(records), "static_tokens": total_tokens,
                   "by_kind": by_kind,
                   # ``static_tokens`` assumes the host inlines every MCP
                   # schema. When it defers them (Claude Code's tool search),
                   # only tool names and server instructions load up front.
                   "upfront_estimate": {
                       "mcp_inline": total_tokens,
                       "mcp_deferred": sum(c.upfront_if_deferred for c in records),
                       "deferred_on_demand": sum(c.deferred_tokens for c in records),
                       "which_applies": ("host-dependent: mcp_deferred applies where "
                                         "the host defers tool schemas (see by_host); "
                                         "mcp_inline otherwise"),
                       "by_host": HOST_DISCLOSURE,
                   }},
        "records": record_dicts,
        "probe_failed": {n: f"figure is a floor; retry: {PROBE_RETRY_HINT}"
                         for n in sorted(failed)},
        "unresolved_sources": discovery_gaps(ws_root),
        "unused_high_authority": [c.id for c in unused_authority],
        "never_used": [c.id for c in never_used],
        "leakage": {c.id: list(c.leakage) for c in leaky},
        "overlap_clusters": sorted(clusters.keys()),
        "trim_preview": {"ids": [c.id for c in trim], "est_token_reduction": trim_savings,
                         "est_token_reduction_deferred": trim_savings_deferred},
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
    up = t.get("upfront_estimate") or {}
    lines.append(f"  {'TOTAL':<18} {t['capabilities']:>3} · {t['static_tokens']:>7,} tok "
                 "(inline basis: every MCP schema loaded)")
    if up:
        lines.append(f"  {'':<18}     · {up['mcp_deferred']:>7,} tok up front on the deferred "
                     "basis (Claude Code/Codex/Antigravity default: tool names only) — estimates")
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
    for name, note in (a.get("probe_failed") or {}).items():
        lines.append(f"Probe failed: {name} — {note}")
    for gap in a.get("unresolved_sources") or []:
        lines.append(f"Not counted: {gap}")
    tp = a["trim_preview"]
    if tp["ids"]:
        lines.append(f"Preview-trim candidates: {len(tp['ids'])} · "
                     f"~{tp.get('est_token_reduction_deferred', 0):,} tok recoverable up front "
                     f"(deferred basis; ~{tp['est_token_reduction']:,} inline basis) "
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


_TRUTHY_ENV = ("1", "true", "yes", "on")


def claude_deferral_state(child_env: dict[str, str]) -> tuple[str, list[str]]:
    """Whether Claude Code will defer MCP tool schemas for this launch:
    ('deferred' | 'inline' | 'conditional' | 'unknown', reasons)."""
    why: list[str] = []
    ets = (child_env.get("ENABLE_TOOL_SEARCH") or "").strip().lower()
    if (child_env.get("CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS") or "").strip().lower() in _TRUTHY_ENV:
        return "inline", ["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS disables tool search"]
    if ets == "false":
        return "inline", ["ENABLE_TOOL_SEARCH=false loads every schema inline"]
    if ets.startswith("auto"):
        return "conditional", [f"ENABLE_TOOL_SEARCH={ets}: inline while definitions are "
                               "under the threshold share of the window"]
    for var in ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"):
        if (child_env.get(var) or "").strip().lower() in _TRUTHY_ENV:
            return "unknown", [f"{var}: tool search is unavailable on some third-party deployments"]
    base = child_env.get("ANTHROPIC_BASE_URL") or ""
    if base and ets != "true":
        try:
            from urllib.parse import urlparse

            host = (urlparse(base).hostname or "").lower()
        except Exception:
            host = ""
        if host not in ("api.anthropic.com",) and not host.endswith(".anthropic.com") \
                and host not in ("127.0.0.1", "localhost"):
            return "inline", [f"non-first-party ANTHROPIC_BASE_URL ({host or 'unparsed'}): "
                              "Claude Code turns deferred loading off"]
    why.append("ENABLE_TOOL_SEARCH=true" if ets == "true" else "tool search on by default")
    why.append("assumes a model that supports tool search")
    return "deferred", why


def host_basis(host: str, env: dict[str, str] | None = None) -> tuple[str, str]:
    """(basis, why) for pricing MCP up front on ``host``: 'deferred' where the
    host lists tool names only, else 'inline'. Claude Code is judged from the
    environment; Codex and Antigravity from their documentation (not
    observed live)."""
    import os

    e = os.environ if env is None else env
    if host == "claude":
        state, why = claude_deferral_state(dict(e))
        if state == "deferred":
            return "deferred", "Claude Code tool search (" + why[0] + ")"
        return "inline", f"Claude Code {state}: {why[0]}"
    if host == "codex":
        return "deferred", "Codex tool search, always on (documented)"
    if host == "antigravity":
        return "deferred", "Antigravity lazy tool loading (documented)"
    return "inline", f"no documented deferral for host {host!r}"


# ---------------------------------------------------------------- MCP probe
def _mcp_server_commands(ws_root: Path | str, *, user_scope: bool = False
                         ) -> dict[str, list[str]]:
    """Map server name → argv for every stdio server in the WORKSPACE's host
    configs. This feeds paths that spawn servers (probe, gateway) and paths
    that write server definitions into the workspace (compile, prune,
    install-gateway), so user/local-scope servers are included only when a
    caller asks explicitly: knowing a server exists never, by itself, starts
    or copies it. Remote (url) servers have no argv. Fail-open."""
    return {s.name: s.argv for s in mcp_server_specs(ws_root, user_scope=user_scope)
            if s.transport == "stdio"}


def servers_not_included(ws_root: Path | str) -> list[dict[str, str]]:
    """User/local-scope servers that exist but are deliberately left out of
    anything ctx writes into the workspace, with the reason."""
    return [{"name": s.name, "scope": s.scope, "host": s.host,
             "reason": f"{s.scope}-scope definition is never copied into a workspace file"}
            for s in mcp_server_specs(ws_root) if s.scope in ("user", "local")]


_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_vars(value: str, environ: dict[str, str]) -> str:
    """``${VAR}`` / ``${VAR:-default}`` expansion, as Claude Code applies it
    to ``.mcp.json`` command, args and env. An unset variable without a
    default expands to the empty string."""
    return _VAR_RE.sub(lambda m: environ.get(m.group(1), m.group(2) or ""), value)


def spawn_params(spec: ServerSpec, base_env: dict[str, str] | None = None
                 ) -> tuple[list[str], dict[str, str], str | None]:
    """(argv, env, cwd) to launch a stdio server the way its host would: the
    configured ``env`` layered over the inherited environment, variables
    expanded, the configured ``cwd`` honoured. Values stay in memory."""
    import os

    base = dict(os.environ if base_env is None else base_env)
    env = {**base, **{k: expand_vars(v, base) for k, v in spec.env.items()}}
    argv = [expand_vars(a, env) for a in spec.argv]
    cwd = expand_vars(spec.cwd, env) if spec.cwd else None
    return argv, env, cwd


def probe_mcp_server(argv: list[str], *, timeout: float = 8.0,
                     env: dict[str, str] | None = None,
                     cwd: str | None = None) -> dict[str, Any]:
    """Speak MCP over stdio to a server: ``{"tools": [...], "instructions":
    str}``. Each tool record carries its exact schema+description token cost
    and inferred authority — the real per-turn tax the static file inventory
    cannot see. Fail-open to no tools."""
    import subprocess

    def _frame(obj: dict) -> bytes:
        return (json.dumps(obj) + "\n").encode("utf-8")

    init = _frame({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"protocolVersion": "2024-11-05",
                              "capabilities": {}, "clientInfo": {"name": "ctx-surface", "version": "1"}}})
    inited = _frame({"jsonrpc": "2.0", "method": "notifications/initialized"})
    listing = _frame({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    out: dict[str, Any] = {"tools": [], "instructions": ""}
    try:
        proc = subprocess.run(
            argv, input=init + inited + listing,
            capture_output=True, timeout=timeout, env=env, cwd=cwd or None,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return out
    tools: list[dict[str, Any]] = out["tools"]
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict):
            continue
        if msg.get("id") == 1 and isinstance(msg.get("result"), dict):
            out["instructions"] = str(msg["result"].get("instructions") or "")
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
                    "summary": " ".join(desc.split())[:120],
                })
    return out


def probe_mcp_tools(argv: list[str], *, timeout: float = 8.0) -> list[dict[str, Any]]:
    """Tool records of one stdio server (see :func:`probe_mcp_server`)."""
    return probe_mcp_server(argv, timeout=timeout)["tools"]


def read_probe_cache(ws_root: Path | str) -> dict[str, Any]:
    """The cached probe results (never spawns). Fail-open to {}."""
    try:
        cache = json.loads((Path(ws_root) / _PROBE_CACHE).read_text(encoding="utf-8"))
    except Exception:
        return {}
    return cache if isinstance(cache, dict) else {}


def _probe_cache_key(argv: list[str]) -> str:
    return hashlib.sha256(json.dumps(argv, sort_keys=True).encode()).hexdigest()[:16]


#: A failed probe is not retried by an automatic caller for this long.
PROBE_FAILURE_TTL_S = 24 * 3600
PROBE_RETRY_HINT = "ctx surface audit --probe-mcp (automatic retry after 24h)"


def probe_failures(ws_root: Path | str) -> dict[str, str]:
    """Servers whose most recent probe failed (from the probe cache), with a
    short note on when. Never spawns."""
    import time

    out: dict[str, str] = {}
    for name, entry in read_probe_cache(ws_root).items():
        if isinstance(entry, dict) and not entry.get("tools") and entry.get("failed_at"):
            age = max(0, int(time.time() - float(entry["failed_at"])))
            out[str(name)] = f"{age // 3600}h ago" if age >= 3600 else f"{age // 60}m ago"
    return out


def probe_surface(ws_root: Path | str, *, timeout: float = 8.0,
                  use_cache: bool = True, explicit: bool = False) -> list[Capability]:
    """Expand each MCP server into per-tool records with measured schema
    tokens. Spawns each server, but caches the result under
    ``.ctx-surface/probe-cache.json`` keyed by the server's argv, so only the
    first session (or a changed command) pays the spawn cost — the pre-flight
    gate can then price the real MCP surface cheaply every session.

    ``explicit`` (a person passed ``--probe-mcp``) also probes user- and
    local-scope servers, launched with their configured env/cwd. Automatic
    callers (the SessionStart gate) probe only workspace servers, exactly as
    launched before (inherited environment, raw argv), and a failed probe is
    remembered for :data:`PROBE_FAILURE_TTL_S` so it is not respawned on every
    hook. Returns [] when nothing probes."""
    import time
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
    specs: dict[str, ServerSpec] = {}
    for s in mcp_server_specs(ws_root, user_scope=explicit):
        if s.transport == "stdio" and s.name not in specs:
            specs[s.name] = s
    entries = sorted((n, s.argv) for n, s in specs.items())
    now = time.time()
    to_probe: dict[str, list[str]] = {}
    tools_by_name: dict[str, list] = {}
    instr_by_name: dict[str, int] = {}
    for name, argv in entries:
        key = _probe_cache_key(argv)
        entry = cache.get(name) if use_cache else None
        if isinstance(entry, dict) and entry.get("key") == key and entry.get("tools"):
            tools_by_name[name] = entry.get("tools") or []
            instr_by_name[name] = int(entry.get("instructions_tokens") or 0)
        elif (isinstance(entry, dict) and entry.get("key") == key and not explicit
              and now - float(entry.get("failed_at") or 0) < PROBE_FAILURE_TTL_S):
            tools_by_name[name] = []  # failed recently: do not respawn from a hook
        else:
            to_probe[name] = argv
    if to_probe:
        # spawn uncached servers concurrently so N servers cost ~timeout, not N * timeout
        import concurrent.futures

        def _probe(name: str) -> dict[str, Any]:
            if not explicit:
                return probe_mcp_server(specs[name].argv, timeout=timeout)
            argv, env, cwd = spawn_params(specs[name])
            return probe_mcp_server(argv, timeout=timeout, env=env, cwd=cwd)

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(to_probe)) as ex:
            futures = {ex.submit(_probe, name): name for name in to_probe}
            for fut in concurrent.futures.as_completed(futures):
                name = futures[fut]
                res = fut.result()
                tools = res["tools"]
                tools_by_name[name] = tools
                itok = (_tokens_of(res["instructions"][:MCP_INSTRUCTIONS_CAP_CHARS])
                        if res["instructions"] else 0)
                instr_by_name[name] = itok
                if tools:
                    cache[name] = {"key": _probe_cache_key(to_probe[name]), "tools": tools,
                                   "instructions_tokens": itok}
                else:  # remembered, so automatic callers do not respawn it
                    cache[name] = {"key": _probe_cache_key(to_probe[name]), "tools": [],
                                   "failed_at": round(now)}
                dirty = True
    for name, argv in entries:
        tools = tools_by_name.get(name) or []
        if tools and instr_by_name.get(name):
            # Server instructions load up front whether or not the host
            # defers the tool schemas.
            out.append(Capability(
                id=f"mcp.{name}", kind="mcp_server", provider=name, source=f"mcp:{name}",
                tokens=instr_by_name[name], authority=infer_authority(name),
                activation="always", invocations=-1, detail="server instructions",
                scope=specs[name].scope, transport="stdio", pricing="mcp-instructions",
            ))
        for tool in tools:
            leak, terms = _leakage_tags(tool["name"] + " " + str(tool.get("description", "")),
                                        "mcp_tool", tool["authority"], -1)
            out.append(Capability(
                id=f"mcp.{name}.{tool['name']}", kind="mcp_tool", provider=name,
                source=f"mcp:{name}", tokens=tool["tokens"], authority=tool["authority"],
                activation="always", invocations=-1, leakage=leak, sensitive_terms=terms,
                detail=f"schema {tool['schema_tokens']} + desc {tool['description_tokens']} tok",
                scope=specs[name].scope, transport="stdio",
                # Deferred: only the tool's name is listed; its schema loads on demand.
                # alwaysLoad servers are exempt from deferral: full cost.
                upfront_deferred=(-1 if specs[name].always_load
                                  else _tokens_of(f"mcp__{name}__{tool['name']}")),
                deferred_tokens=tool["tokens"], pricing="mcp-tool-schema",
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
              probe: bool = True, host: str = "claude") -> str:
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
        a = audit(ws_root, probe_mcp=probe, automatic=True)
        basis, why = host_basis(host)
    except Exception:
        return ""
    inline = a["totals"]["static_tokens"]
    deferred = a["totals"]["upfront_estimate"]["mcp_deferred"]
    total = deferred if basis == "deferred" else inline
    if total <= max_static_tokens:
        return ""
    alt = (f"; ~{inline:,} if every schema were inlined" if basis == "deferred"
           else f"; ~{deferred:,} if the host deferred MCP schemas")
    lines = [
        f"CTX_SURFACE_GUARD: discretionary capability surface is ~{total:,} "
        f"tokens up front (estimate, {basis} basis: {why}{alt}; budget "
        f"{max_static_tokens:,}). This is re-sent every turn before any tool runs."
    ]
    by_kind: dict[str, dict[str, int]] = {}
    for r in a["records"]:
        slot = by_kind.setdefault(r["kind"], {"tokens": 0})
        ud = r.get("upfront_deferred", -1)
        slot["tokens"] += (r["tokens"] if basis != "deferred" or ud is None or ud < 0 else ud)
    top = sorted(by_kind.items(), key=lambda kv: -kv[1]["tokens"])[:3]
    lines.append("  heaviest: " + ", ".join(
        f"{k} {v['tokens']:,}tok" for k, v in top))
    if a.get("probe_failed"):
        lines.append("  probe failed (cost shown is a floor): "
                     + ", ".join(sorted(a["probe_failed"])) + f" — retry: {PROBE_RETRY_HINT}")
    skipped = [g for g in a.get("unresolved_sources") or [] if g.startswith("config skipped")]
    if skipped:
        lines.append(f"  {len(skipped)} config file(s) skipped; their servers are not counted")
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
    "ServerSpec", "mcp_server_specs", "discovery_gaps", "transcript_usage",
    "skill_listing", "spawn_params", "probe_mcp_server", "managed_mcp_config",
    "claude_config_dir", "claude_user_json", "read_probe_cache",
]
