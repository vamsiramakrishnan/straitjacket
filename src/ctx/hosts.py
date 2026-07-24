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


@dataclass(frozen=True)
class HostSpec:
    """Everything the harness needs to know about one coding-agent CLI.

    ``installer`` / ``wrapper`` name functions in :mod:`ctx.installer` and
    :mod:`ctx.wrap` by string to avoid an import cycle; an empty string means
    "not wired yet" (detected and priced, but not harnessable)."""

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
    notes: str = ""

    @property
    def harnessable(self) -> bool:
        """True when both an installer and a wrapper are wired for this host."""
        return bool(self.wrapper)


# ---------------------------------------------------------------------------
# The registry. Order is display order (fully wired hosts first). The three
# shipped hosts carry real installers/wrappers; the rest are detected and
# priced so `ctx wrap detect` shows the whole board, but are not yet wrappable.
# ---------------------------------------------------------------------------
_REGISTRY: tuple[HostSpec, ...] = (
    HostSpec(
        name="antigravity",
        cli_bins=("antigravity",),
        # Antigravity's default tier is Gemini flash (see ctx.engagement notes).
        default_model="gemini-3-flash",
        model_env=("ANTIGRAVITY_MODEL", "GEMINI_MODEL"),
        installer="install_antigravity",
        wrapper="wrap_antigravity",
        # No upstream output-substitution field yet -> nudge-only (see README).
        output_substitution=False,
        supports_mcp=True,
        supports_hooks=True,
        vendor_hint="google",
        notes="built-for host; persistent workspace plugin; output gate nudge-only",
    ),
    HostSpec(
        name="claude",
        cli_bins=("claude",),
        default_model="claude-sonnet",
        model_env=("ANTHROPIC_MODEL",),
        installer="install_claude",
        wrapper="wrap_claude",
        output_substitution=True,   # updatedToolOutput
        supports_mcp=True,
        supports_hooks=True,
        vendor_hint="anthropic",
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
        notes="persistent .codex/ MCP + hooks",
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
    def output_dollars_per_mtok(self) -> float:
        """The dominant term for agent work: output-token list price."""
        return self.price.output


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


__all__ = [
    "HostSpec",
    "DetectedHost",
    "all_hosts",
    "host_by_name",
    "harnessable_hosts",
    "model_for",
    "detect",
    "detect_all",
    "installed_harnessable",
]
