"""Repository policy: committed ``ctx.toml`` plus hard defaults from SPEC §13."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# One source of truth for the host-neutral lean-model default (stdlib-only
# module, no import cycle) so the config default never drifts from the
# engagement mechanism's own default.
from ctx.engagement import DEFAULT_LEAN_MODELS as _DEFAULT_LEAN_MODELS

CONFIG_FILENAME = "ctx.toml"
IGNORE_FILENAME = ".ctxignore"

# Secret-bearing paths excluded from automatic capture regardless of config
# (SPEC §5.5). ``.ctxignore`` adds to these; nothing can remove them silently.
BUILTIN_DENY_GLOBS = (
    ".env",
    ".env.*",
    "**/.env",
    "**/.env.*",
    "**/secrets/**",
    "**/credentials/**",
    "**/*.pem",
    "**/*.key",
    "**/id_rsa*",
    "**/id_ed25519*",
    "**/.aws/**",
    "**/.config/gcloud/**",
    "**/.ssh/**",
)


@dataclass(frozen=True)
class Budgets:
    digest_tokens: int = 480
    result_tokens: int = 1200
    turn_retrieval_tokens: int = 2800
    max_inline_bytes: int = 16384
    max_inline_lines: int = 240
    max_matches: int = 80
    session_read_budget_bytes: int = 262144
    # Universal emission gate (ctx.hook._emission_gate): a PostToolUse tool
    # result larger than this many bytes is replaced by a bounded digest with
    # a working retrieval ref. Decoupled from max_inline_bytes (input path) on
    # purpose; keep in sync with hook._MAX_TOOL_OUTPUT_BYTES_DEFAULT.
    max_tool_output_bytes: int = 16384
    # Window-pressure threshold (percent): when the Tier-0 proxy reports the
    # context window at or above this fullness, the guard tightens its inline
    # and session read budgets (see ctx.hook._apply_window_pressure).
    window_pressure_pct: int = 70
    # Failure asymmetry (measured, rtk-corpus eval): success output is
    # boilerplate, failure output is evidence. A failing run's digest gets
    # this multiple of the standard emission budget.
    failure_budget_factor: float = 2.0
    # HEAD/TAIL evidence window for text/v1 floods (eval-collapse S-C: CLIs
    # put conclusions at the END — the script's own SUMMARY tail line was
    # omitted when only "head stdout:L1" rode). First H and last T lines are
    # shown; the middle is declared-omitted with a span/--lines address.
    digest_head_lines: int = 5
    digest_tail_lines: int = 5


@dataclass(frozen=True)
class Guard:
    mode: str = "guarded"  # advisory | guarded | strict
    unknown_command: str = "force_ask"  # allow | deny | ask | force_ask
    internal_error: str = "allow"  # availability-safe default (SPEC §10.2)
    steering: str = "auto"  # auto | rewrite | deny — deny keeps pure deny-with-remediation
    # Replacement surface (default posture): substitute loop-shapes with
    # collapsed ctx ops; set collapse=false to break-glass off.
    collapse: bool = True
    # Repo-tunable classification: prefix matches against the canonical argv.
    allow_commands: tuple[str, ...] = ()
    deny_commands: tuple[str, ...] = ()
    # NOTE: the guard hot path (ctx.hook._load_guard_policy) re-reads these
    # keys with its own stdlib-only parser for latency. tests/test_config_hook_
    # parity.py pins the two readers so they can never silently drift.


@dataclass(frozen=True)
class Engagement:
    """Graduated engagement (mechanism C): affordance surface scales with
    measured task scale. See ctx.engagement for the graduation rules."""

    mode: str = "auto"  # auto | active | passive
    activate_after_calls: int = 8
    lean_models: tuple[str, ...] = _DEFAULT_LEAN_MODELS
    # Emission-gate nudge budget (read on the hot path by ctx.hook); pinned to
    # the guard reader by tests/test_config_hook_parity.py.
    emission_nudge_tokens: int = 20000


@dataclass(frozen=True)
class OrchestratePolicy:
    """Harness collaboration (ctx.orchestrator): a cheap coordinator splits a
    task across the installed harnesses by capability x price and a closed loop
    coordinates it. The coordinator emits a ``ctx.route/v1`` DAG; when none can
    run, a deterministic capability-routed fallback is used. ``confirm`` is off
    by default — the plan is priced, shown, then run (rewrite-not-ask)."""

    confirm: bool = False       # print the priced plan and stop before running
    fallback_only: bool = False  # skip the coordinator model; always use the fallback route
    # Closed-loop totality bounds (mirrors PlanPolicy). The loop stops at the
    # first bound it hits; a single installed harness degrades gracefully.
    max_nodes: int = 12
    max_waves: int = 4
    max_replans: int = 2
    budget_usd: float = 0.0     # 0 = unbounded (still bounded by nodes/waves)
    node_timeout: float = 900.0
    # Complexity-adaptive implementation tier for the deterministic fallback:
    # "standard" (Gemini-3.6-flash) for real work, "economy" (3.5-flash-lite) for
    # simple edits. A live coordinator overrides this per task.
    implement_tier: str = "standard"
    # Coarse per-node token estimates for the deterministic fallback route and
    # for pricing the plan up front; real spend is reconciled from wire truth.
    explore_input_tokens: int = 24000
    explore_output_tokens: int = 3000
    implement_input_tokens: int = 48000
    implement_output_tokens: int = 9000
    review_input_tokens: int = 20000
    review_output_tokens: int = 2500


@dataclass(frozen=True)
class StorePolicy:
    backend: str = "user-state"  # user-state | local (advisory only)
    retention_days: int = 30


@dataclass(frozen=True)
class PlanPolicy:
    """Compiled evidence plans (docs/EVIDENCE-PLANS.md): totality bounds and
    the epochal-control default. ``max_nodes``/``max_fanout`` are ceilings —
    a plan's own budget may only tighten them, never exceed them."""

    max_nodes: int = 24
    max_fanout: int = 64
    wall_seconds: float = 120.0
    replans: int = 1


@dataclass(frozen=True)
class WorkspacePolicy:
    allow_outside_root: bool = False
    follow_symlinks: bool = False
    nested_repos: str = "separate"
    respect_gitignore: bool = True


def _all_redaction_patterns() -> tuple[str, ...]:
    from ctx.textutil import REDACTION_PATTERNS

    return tuple(REDACTION_PATTERNS)


@dataclass(frozen=True)
class Redaction:
    enabled: bool = True
    # Default: every vendored pattern. A committed ctx.toml may narrow this.
    patterns: tuple[str, ...] = field(default_factory=_all_redaction_patterns)


@dataclass(frozen=True)
class SurfacePolicy:
    """Capability-surface containment (the input side). A SessionStart
    pre-flight gate audits the discretionary surface before any work begins —
    'bound before bloat', the mirror of the output side's 'capture before
    flood'. See docs/CAPABILITY-SURFACE.md."""

    max_static_tokens: int = 8000   # discretionary-surface budget per turn
    gate: str = "warn"              # off | warn (advisory at SessionStart)
    default_profile: str = ""       # profile to suggest when over budget
    gateway: bool = False           # gateway is the MCP delivery (progressive disclosure)
    probe: bool = True              # measure real MCP tool schemas (cached) in the gate


@dataclass(frozen=True)
class Config:
    version: int = 1
    repo_key: str | None = None
    workspace: WorkspacePolicy = field(default_factory=WorkspacePolicy)
    budgets: Budgets = field(default_factory=Budgets)
    guard: Guard = field(default_factory=Guard)
    engagement: Engagement = field(default_factory=Engagement)
    orchestrate: OrchestratePolicy = field(default_factory=OrchestratePolicy)
    store: StorePolicy = field(default_factory=StorePolicy)
    plan: PlanPolicy = field(default_factory=PlanPolicy)
    redaction: Redaction = field(default_factory=Redaction)
    surface: SurfacePolicy = field(default_factory=SurfacePolicy)
    scopes: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # ws:<alias> routing targets: alias -> workspace path (absolute, or
    # relative to this workspace root). Committed in ctx.toml [aliases].
    aliases: dict[str, str] = field(default_factory=dict)
    deny_globs: tuple[str, ...] = BUILTIN_DENY_GLOBS


def _pick(data: dict[str, Any], cls: type, **overrides: Any) -> Any:
    names = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
    kwargs = {k: v for k, v in data.items() if k in names}
    kwargs.update(overrides)
    return cls(**kwargs)


def load_config(workspace_root: Path | None) -> Config:
    """Load ``ctx.toml`` from the workspace root; malformed files degrade to
    defaults rather than blocking the harness (fail-open, SPEC §15)."""
    if workspace_root is None:
        return Config()
    path = workspace_root / CONFIG_FILENAME
    if not path.is_file():
        return Config()
    # Lazy: tomllib costs ~4ms at import and the common hook-path case
    # (no ctx.toml present) returns above without ever needing it.
    import tomllib

    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return Config()

    scopes: dict[str, tuple[str, ...]] = {}
    for name, body in (raw.get("scopes") or {}).items():
        roots = body.get("roots") if isinstance(body, dict) else None
        if isinstance(roots, list):
            scopes[str(name)] = tuple(str(r) for r in roots)

    budgets = _pick(raw.get("budgets") or {}, Budgets)
    # Guard is hand-built (not _pick) so list keys coerce to tuples and bools
    # coerce honestly — and so Config models every key the hot-path guard reads.
    guard_raw = raw.get("guard") or {}
    gd = Guard()
    guard = Guard(
        mode=str(guard_raw.get("mode", gd.mode)),
        unknown_command=str(guard_raw.get("unknown_command", gd.unknown_command)),
        internal_error=str(guard_raw.get("internal_error", gd.internal_error)),
        steering=str(guard_raw.get("steering", gd.steering)),
        collapse=bool(guard_raw.get("collapse", gd.collapse)),
        allow_commands=tuple(str(x) for x in guard_raw.get("allow_commands", ())),
        deny_commands=tuple(str(x) for x in guard_raw.get("deny_commands", ())),
    )
    orchestrate = _pick(raw.get("orchestrate") or {}, OrchestratePolicy)
    store = _pick(raw.get("store") or {}, StorePolicy)
    plan = _pick(raw.get("plan") or {}, PlanPolicy)
    ws = _pick(raw.get("workspace") or {}, WorkspacePolicy)
    surface = _pick(raw.get("surface") or {}, SurfacePolicy)

    eng_raw = raw.get("engagement") or {}
    ed = Engagement()
    engagement = Engagement(
        mode=str(eng_raw.get("mode", ed.mode)),
        activate_after_calls=int(eng_raw.get("activate_after_calls", ed.activate_after_calls)),
        lean_models=tuple(
            str(m) for m in eng_raw.get("lean_models", ed.lean_models)
        ),
        emission_nudge_tokens=int(
            eng_raw.get("emission_nudge_tokens", ed.emission_nudge_tokens)
        ),
    )

    red_raw = raw.get("redaction") or {}
    # A non-list `patterns` (e.g. a bare string from a missing-brackets typo)
    # would be iterated character-by-character, silently disabling all secret
    # redaction. Fall back to the full default set unless it is a real list.
    red_patterns = red_raw.get("patterns", None)
    if not isinstance(red_patterns, list):
        red_patterns = list(Redaction().patterns)
    redaction = Redaction(
        enabled=bool(red_raw.get("enabled", True)),
        patterns=tuple(str(p) for p in red_patterns),
    )

    aliases = {
        str(k): str(v)
        for k, v in (raw.get("aliases") or {}).items()
        if isinstance(v, str)
    }

    return Config(
        version=int(raw.get("version", 1)),
        repo_key=raw.get("repo_key"),
        workspace=ws,
        budgets=budgets,
        guard=guard,
        engagement=engagement,
        orchestrate=orchestrate,
        store=store,
        plan=plan,
        redaction=redaction,
        surface=surface,
        scopes=scopes,
        aliases=aliases,
    )


def load_ctxignore(workspace_root: Path | None) -> tuple[str, ...]:
    """Globs from ``.ctxignore`` merged with the built-in secret deny list."""
    patterns: list[str] = list(BUILTIN_DENY_GLOBS)
    if workspace_root is not None:
        path = workspace_root / IGNORE_FILENAME
        if path.is_file():
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#"):
                        patterns.append(line)
            except (OSError, UnicodeDecodeError):
                pass
    return tuple(dict.fromkeys(patterns))
