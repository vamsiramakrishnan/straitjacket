"""Declarative host registry: every coding-agent CLI the harness can wrap.

`ctx wrap` used to know exactly three hosts by name. This module makes the set
*data*: one ``HostSpec`` per CLI states how to detect it on PATH, how to learn
its active model, which installer wires the harness in, and what the host's
output side can enforce. Adding a host is an edit here — the three shipped
installers already exist and are named by string so this module stays free of
import cycles with :mod:`ctx.installer`.

The model tie is the point. :func:`detect` joins each *installed* CLI to
:func:`ctx.pricing.price_for`, so ``ctx wrap detect`` prices every harness and
:mod:`ctx.orchestrator` can rank them cheapest->premium and route work by cost.

Honesty rules, mirroring the rest of the project:

* **Detection is fail-open.** A missing binary, an unreadable version, or a
  subprocess error degrades to "not installed"/"unknown" — never an exception.
* **Model/price are estimates.** Pre-run we approximate the active model from
  env overrides then the host default; a session's *real* spend is still read
  from wire truth (:mod:`ctx.scorecard`) after it runs.
* **Only wired hosts are harnessable.** Extra CLIs are detected and priced so
  the picture is complete, but ``harnessable`` is False until an installer and
  wrapper exist; setup and the orchestrator skip them with a note, never a
  silent drop.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ctx.pricing import Price, price_for


# Capability tiers, strongest -> weakest. A subtask's ``min_tier`` requirement
# is met by any host at or above it. Reasoning strength, *not* price — the two
# are correlated but the router weighs them separately (capability gates, price
# breaks ties). Declared heuristic, overridable per repo, in the honesty spirit
# of engagement.lean_models — the costly error is over-trusting a weak model, so
# the defaults fail safe.
CAPABILITY_TIERS = ("frontier", "standard", "economy")


def tier_rank(tier: str) -> int:
    """Higher = more capable. Unknown tiers rank lowest (fail safe)."""
    try:
        return len(CAPABILITY_TIERS) - CAPABILITY_TIERS.index(tier)
    except ValueError:
        return 0


@dataclass(frozen=True)
class ModelChoice:
    """One model a harness can run, with its capability tier and the subtask
    roles it is good at. Price comes from ``ctx.pricing.price_for(id)`` — this
    table is capability, not cost. Researched per harness (Claude Code `/model`,
    Codex model picker, Antigravity's BYO-model list) as of 2026-07; tiers are a
    declared, overridable heuristic (fail-safe: the costly error is over-trusting
    a weak model)."""

    id: str
    tier: str
    roles: tuple[str, ...] = ()
    # The id passed to the host's --model / API at launch, when it differs from
    # the display/pricing id. Verified against the live drivers (Claude Code
    # wants the alias `haiku`, not `claude-haiku-4.5`; the Gemini API serves
    # `gemini-3.1-pro-preview`). Defaults to `id`.
    cli_id: str = ""

    @property
    def launch_id(self) -> str:
        return self.cli_id or self.id


@dataclass(frozen=True)
class HostSpec:
    """Everything the harness needs to know about one coding-agent CLI.

    ``installer`` / ``wrapper`` name functions in :mod:`ctx.installer` and
    :mod:`ctx.wrap` by string to avoid an import cycle; an empty string means
    "not wired yet" (detected and priced, but not harnessable).

    The routing fields — ``capability_tier``, ``strengths``, ``coordinator_model``
    — let :mod:`ctx.orchestrator` route by *capability x price*, not price alone,
    and pick a cheap model when this host acts as the coordinator."""

    name: str
    cli_bins: tuple[str, ...]
    default_model: str
    model_env: tuple[str, ...] = ()
    version_argv: tuple[str, ...] = ("--version",)
    installer: str = ""            # ctx.installer.<installer>(ws, ...)
    wrapper: str = ""              # ctx.wrap.<wrapper>(...)
    output_substitution: bool = False  # PostToolUse can replace a result (enforced) vs nudge-only
    supports_mcp: bool = False
    supports_hooks: bool = False
    print_flag: tuple[str, ...] = ("-p",)   # one-shot / non-interactive run
    model_flag: str = "--model"             # flag that pins the model, if any
    vendor_hint: str = "unknown"
    # Routing (capability x price). ``models`` is the catalog of models this
    # harness can run, spanning tiers; the router picks a (harness, model) pair
    # per subtask — the cheapest model that meets the tier and covers the roles.
    # ``strengths`` are host-level orientation tags folded into role coverage.
    models: tuple[ModelChoice, ...] = ()
    strengths: tuple[str, ...] = ()
    # The cheap model this host runs when it is the *coordinator* (planning/
    # routing), pinned via model_flag. Defaults to the worker model.
    coordinator_model: str = ""
    notes: str = ""

    @property
    def harnessable(self) -> bool:
        """True when both an installer and a wrapper are wired for this host."""
        return bool(self.wrapper)

    @property
    def coord_model(self) -> str:
        return self.coordinator_model or self.default_model

    def model(self, model_id: str) -> ModelChoice | None:
        return next((m for m in self.models if m.id == model_id), None)

    @property
    def capability_tier(self) -> str:
        """Headline tier = the default worker model's tier (for the detect
        table). A harness typically spans several tiers via ``models``."""
        m = self.model(self.default_model)
        return m.tier if m else "standard"

    @property
    def max_tier(self) -> str:
        """The strongest tier this harness can reach across its catalog."""
        if not self.models:
            return self.capability_tier
        return max((m.tier for m in self.models), key=tier_rank)


# ---------------------------------------------------------------------------
# The registry. Order is display order (fully wired hosts first). The three
# shipped hosts carry real installers/wrappers; the rest are detected and
# priced so `ctx wrap detect` shows the whole board, but are not yet wrappable.
# ---------------------------------------------------------------------------
_REGISTRY: tuple[HostSpec, ...] = (
    HostSpec(
        name="antigravity",
        cli_bins=("antigravity",),
        # Antigravity is BYO-model; its worker default is a Gemini flash tier.
        default_model="gemini-3.6-flash",
        model_env=("ANTIGRAVITY_MODEL", "GEMINI_MODEL"),
        installer="install_antigravity",
        wrapper="wrap_antigravity",
        # No upstream output-substitution field yet -> nudge-only (see README).
        output_substitution=False,
        supports_mcp=True,
        supports_hooks=True,
        vendor_hint="google",
        # Antigravity runs Gemini across tiers (it can also BYO Claude/GPT, not
        # modeled here). Flash is a capable *implementation* model, not just
        # explore — the whole point of routing by model.
        models=(
            ModelChoice("gemini-3.1-pro", "frontier", ("plan", "reason", "review", "architect"),
                        cli_id="gemini-3.1-pro-preview"),
            ModelChoice("gemini-3.6-flash", "standard", ("implement", "edit", "code", "summarize")),
            # Flash-lite (the 3.5 line — only *flash* is 3.6) is the cheap
            # simple-implementer as well as the explorer: good for a small
            # well-specified edit (the economy implement tier).
            ModelChoice("gemini-3.5-flash-lite", "economy", ("explore", "search", "triage", "verify", "implement", "edit")),
        ),
        strengths=("search", "triage", "verify", "implement", "summarize", "explore"),
        coordinator_model="gemini-3.5-flash-lite",
        notes="built-for host; Gemini flash implements cheaply; output gate nudge-only",
    ),
    HostSpec(
        name="claude",
        cli_bins=("claude",),
        default_model="claude-sonnet-4.6",
        model_env=("ANTHROPIC_MODEL",),
        installer="install_claude",
        wrapper="wrap_claude",
        output_substitution=True,   # updatedToolOutput
        supports_mcp=True,
        supports_hooks=True,
        vendor_hint="anthropic",
        # Claude Code /model spans Opus (planning/reasoning) -> Sonnet (coding)
        # -> Haiku (fast exploration). Model-level routing within one harness.
        models=(
            ModelChoice("claude-opus-4.8", "frontier", ("plan", "reason", "synthesize", "decide", "review", "architect"), cli_id="opus"),
            ModelChoice("claude-sonnet-4.6", "standard", ("implement", "edit", "code", "review"), cli_id="sonnet"),
            ModelChoice("claude-haiku-4.5", "economy", ("explore", "search", "triage", "verify", "summarize"), cli_id="haiku"),
        ),
        strengths=("reason", "synthesize", "implement", "edit", "code", "review", "decide"),
        coordinator_model="claude-haiku-4.5",
        notes="ephemeral --settings wrap; reports real cost.total_cost_usd",
    ),
    HostSpec(
        name="codex",
        cli_bins=("codex",),
        default_model="gpt-5.6-terra",
        model_env=("CODEX_MODEL", "OPENAI_MODEL"),
        installer="install_codex",
        wrapper="wrap_codex",
        output_substitution=True,   # decision:block substitution
        supports_mcp=True,
        supports_hooks=True,
        vendor_hint="openai",
        # Codex is non-interactive via `codex exec "<prompt>"` (not -p). Exact
        # argv/flag order is unverified pending a live Codex run — Codex is not
        # installed in the environment where the live A/B ran.
        print_flag=("exec",),
        # Codex GPT-5.6 lineup: Sol (detail/polish) -> Terra (workhorse) ->
        # Luna (repeatable). Strong code-gen across tiers.
        models=(
            ModelChoice("gpt-5.6-sol", "frontier", ("plan", "reason", "review", "architect")),
            ModelChoice("gpt-5.6-terra", "standard", ("implement", "edit", "code", "test")),
            ModelChoice("gpt-5.6-luna", "economy", ("explore", "verify", "triage")),
        ),
        strengths=("code", "implement", "edit", "test"),
        coordinator_model="gpt-5.4-nano",
        notes="persistent .codex/ MCP + hooks; strong code-gen",
    ),
    # --- detected & priced, not yet harnessable (no installer/wrapper) --------
    HostSpec(
        name="gemini",
        cli_bins=("gemini",),
        default_model="gemini-3-pro",
        model_env=("GEMINI_MODEL",),
        vendor_hint="google",
        notes="Google Gemini CLI — detected/priced; harness wiring TODO",
    ),
    HostSpec(
        name="cursor",
        cli_bins=("cursor-agent",),
        default_model="claude-sonnet",
        model_env=("CURSOR_MODEL",),
        vendor_hint="anthropic",
        notes="Cursor CLI agent — detected/priced; harness wiring TODO",
    ),
    HostSpec(
        name="aider",
        cli_bins=("aider",),
        default_model="claude-sonnet",
        model_env=("AIDER_MODEL",),
        vendor_hint="unknown",
        notes="aider — multi-provider; detected/priced; harness wiring TODO",
    ),
    HostSpec(
        name="opencode",
        cli_bins=("opencode",),
        default_model="claude-sonnet",
        model_env=("OPENCODE_MODEL",),
        vendor_hint="unknown",
        notes="opencode — detected/priced; harness wiring TODO",
    ),
)


def all_hosts() -> tuple[HostSpec, ...]:
    """Every registered host, in display order."""
    return _REGISTRY


def host_by_name(name: str) -> HostSpec | None:
    key = (name or "").strip().lower()
    for spec in _REGISTRY:
        if spec.name == key:
            return spec
    return None


def harnessable_hosts() -> tuple[HostSpec, ...]:
    """Registered hosts with an installer + wrapper wired (today: the three)."""
    return tuple(s for s in _REGISTRY if s.harnessable)


def installer_for(spec: HostSpec) -> Callable | None:
    """Resolve ``spec.installer`` to the function in :mod:`ctx.installer`.

    The by-string indirection exists to keep this module free of an import
    cycle with ``ctx.installer``; the resolution therefore happens at call
    time, which is also what lets tests patch the target. None means the
    host names no installer (or names one that no longer exists) — the
    caller must say so rather than substituting a different host's.
    """
    if not spec.installer:
        return None
    from ctx import installer as _installer

    fn = getattr(_installer, spec.installer, None)
    return fn if callable(fn) else None


def wrapper_for(spec: HostSpec) -> Callable | None:
    """Resolve ``spec.wrapper`` to the function in :mod:`ctx.wrap`. Same
    contract as :func:`installer_for`."""
    if not spec.wrapper:
        return None
    from ctx import wrap as _wrap

    fn = getattr(_wrap, spec.wrapper, None)
    return fn if callable(fn) else None


def model_for(spec: HostSpec, env: dict[str, str] | None = None) -> str:
    """The model id we expect this host to use: first env override that is set,
    else the host default. An estimate — real spend is read from wire truth."""
    import os

    src = env if env is not None else os.environ
    for var in spec.model_env:
        val = src.get(var)
        if val:
            return val
    return spec.default_model


@dataclass(frozen=True)
class DetectedHost:
    """A registry host resolved against the current machine."""

    spec: HostSpec
    installed: bool
    path: str | None
    version: str | None
    model: str
    price: Price

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def harnessable(self) -> bool:
        return self.spec.harnessable

    @property
    def capability_tier(self) -> str:
        return self.spec.capability_tier

    @property
    def strengths(self) -> tuple[str, ...]:
        return self.spec.strengths

    @property
    def models(self) -> tuple[ModelChoice, ...]:
        return self.spec.models

    @property
    def output_dollars_per_mtok(self) -> float:
        """The dominant term for agent work: output-token list price."""
        return self.price.output

    def coordinator_price(
        self, workspace_root: Path | str | None = None
    ) -> Price:
        """List price of the cheap model this host runs when coordinating."""
        return price_for(self.spec.coord_model, workspace_root=workspace_root)

    def model_price(self, model_id: str, workspace_root: Path | str | None = None) -> Price:
        return price_for(model_id, workspace_root=workspace_root)

    def covers(self, need_tags: tuple[str, ...]) -> int:
        """How many of the needed capability tags this host is strong at
        (host-level orientation; per-model coverage is finer, see pick_model)."""
        s = set(self.strengths)
        return sum(1 for t in need_tags if t in s)

    def meets_tier(self, min_tier: str) -> bool:
        """True when the harness can reach ``min_tier`` with *some* model."""
        return tier_rank(self.spec.max_tier) >= tier_rank(min_tier)


def _probe_version(path: str, argv: tuple[str, ...], *, timeout: float = 4.0) -> str | None:
    """Best-effort one-line version string. Never raises."""
    try:
        proc = subprocess.run(
            [path, *argv], capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = (proc.stdout or proc.stderr or "").strip()
    if not out:
        return None
    return out.splitlines()[0].strip()[:80]


def detect(
    spec: HostSpec,
    *,
    workspace_root: Path | str | None = None,
    env: dict[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    probe_version: bool = False,
) -> DetectedHost:
    """Resolve one host against the machine: PATH lookup, model, and price.

    ``which`` and ``env`` are injectable so tests need no real binaries.
    ``probe_version`` runs the CLI to capture a version string (off by default;
    it is I/O and non-deterministic, wanted only for the human-facing table)."""
    path = None
    for cand in spec.cli_bins:
        path = which(cand)
        if path:
            break
    installed = path is not None
    model = model_for(spec, env)
    price = price_for(model, workspace_root=workspace_root)
    version = _probe_version(path, spec.version_argv) if (installed and probe_version) else None
    return DetectedHost(
        spec=spec,
        installed=installed,
        path=path,
        version=version,
        model=model,
        price=price,
    )


def detect_all(
    *,
    workspace_root: Path | str | None = None,
    env: dict[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    probe_version: bool = False,
    installed_only: bool = False,
) -> list[DetectedHost]:
    """Detect every registered host. Deterministic order (registry order)."""
    out = [
        detect(
            s,
            workspace_root=workspace_root,
            env=env,
            which=which,
            probe_version=probe_version,
        )
        for s in _REGISTRY
    ]
    if installed_only:
        out = [d for d in out if d.installed]
    return out


def installed_harnessable(
    *,
    workspace_root: Path | str | None = None,
    env: dict[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> list[DetectedHost]:
    """Installed hosts that the harness can actually wrap — the input set for
    both detection-driven setup and the cost-routing orchestrator."""
    return [
        d
        for d in detect_all(workspace_root=workspace_root, env=env, which=which)
        if d.installed and d.harnessable
    ]


def _model_candidates(hosts: list[DetectedHost]) -> list[tuple[DetectedHost, ModelChoice]]:
    """Every (installed host, model) pair the router may choose from."""
    out: list[tuple[DetectedHost, ModelChoice]] = []
    for h in hosts:
        if not h.installed:
            continue
        for m in h.models:
            out.append((h, m))
    return out


def pick_model(
    hosts: list[DetectedHost],
    *,
    min_tier: str = "economy",
    need_tags: tuple[str, ...] = (),
    prefer: str = "cheap",
) -> tuple[DetectedHost, ModelChoice] | None:
    """The (harness, model) to run a subtask on: capability gates, then a
    preference. This is the core of routing-by-model.

    ``prefer="cheap"`` (default) — among models that meet the tier and cover the
    roles, pick the *cheapest*. A subtask that only needs a standard model (an
    ordinary edit) lands on the cheapest standard model (Gemini-flash), not a
    frontier one.

    ``prefer="strong"`` — pick the most *capable* instead: highest tier, then the
    flagship (priciest, a capability proxy). A ``plan`` node prefers strong, so
    it lands on the frontier flagship (Opus) rather than the cheapest frontier
    model. Coverage is primary in both modes.

    If nothing meets the tier, fall back to the single strongest model available
    so a demanding subtask is never silently dropped onto a too-weak model — the
    caller can see the tier was unmet via ``ModelChoice.tier``."""
    cands = _model_candidates(hosts)
    if not cands:
        return None
    eligible = [(h, m) for (h, m) in cands if tier_rank(m.tier) >= tier_rank(min_tier)]
    if eligible:
        def score(hm: tuple[DetectedHost, ModelChoice]) -> tuple:
            h, m = hm
            # Coverage is per-MODEL (its roles), not host-level strengths —
            # otherwise a host tagged broadly (Claude: implement/edit/code) wins
            # every tier and pulls work off the cheaper model that fits.
            cover = len(set(need_tags) & set(m.roles))
            p = h.model_price(m.id)
            if prefer == "strong":
                # highest coverage, then most capable (tier, then flagship price)
                return (-cover, -tier_rank(m.tier), -p.output, h.name, m.id)
            return (-cover, p.output, p.input, h.name, m.id)

        return sorted(eligible, key=score)[0]
    # Tier unmet: strongest model available (highest tier, then cheapest).
    return sorted(
        cands, key=lambda hm: (-tier_rank(hm[1].tier), hm[0].model_price(hm[1].id).output, hm[0].name)
    )[0]


def pick_worker(
    hosts: list[DetectedHost],
    *,
    min_tier: str = "economy",
    need_tags: tuple[str, ...] = (),
) -> DetectedHost | None:
    """Back-compat host-level pick: the harness of the chosen (host, model)."""
    got = pick_model(hosts, min_tier=min_tier, need_tags=need_tags)
    return got[0] if got else None


def pick_coordinator(hosts: list[DetectedHost]) -> DetectedHost | None:
    """The cheapest harness to *plan/route* on, priced by its coordinator model
    (e.g. Antigravity running Gemini-flash-lite). Ties break on name."""
    installed = [h for h in hosts if h.installed and h.harnessable]
    if not installed:
        return None
    return sorted(
        installed,
        key=lambda h: (h.coordinator_price().output, h.coordinator_price().input, h.name),
    )[0]


__all__ = [
    "HostSpec",
    "ModelChoice",
    "DetectedHost",
    "CAPABILITY_TIERS",
    "tier_rank",
    "all_hosts",
    "host_by_name",
    "harnessable_hosts",
    "installer_for",
    "wrapper_for",
    "model_for",
    "detect",
    "detect_all",
    "installed_harnessable",
    "pick_model",
    "pick_worker",
    "pick_coordinator",
]
