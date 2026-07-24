"""Graduated engagement: the harness scales its footprint to measured task
scale instead of applying full affordances to every session (mechanism C).

Measured motivation (evals/matrix-2026-07-18.md): on a one-line surgical fix,
haiku under the harness took 2x the turns/time of naive — the small model
treated every digest suggestion as a work item. The safety core
(artifactization, budgets, secret guards) is always on; what graduates is
the *affordance surface* — the "next:" suggestion lines in digests.

Levels:
  passive  — session starts here under mode "auto": digests carry no
             suggestions; everything else is byte-identical
  active   — full affordances; reached when any measured signal fires:
             * hook call count crosses ``activate_after_calls``
             * proxy-observed window fullness crosses half the
               window-pressure threshold
             * a digest actually truncated something (the tool output that
               proved the task is not small) — reported by ``note_truncation``
  Once active, a session never regresses to passive.

Model profiles: a lean model (window.json model matching ``lean_models``)
keeps a single suggestion even when active — measured on haiku, which
over-executes affordances where sonnet exploits them. The default set maps
the *small/fast tier of every supported host* by name — Claude ``haiku``,
Gemini ``flash``/``flash-lite`` (the Antigravity default), OpenAI
``mini``/``nano`` — so the harness behaves the same across Antigravity,
Claude, and Codex without any host hardcoded as canonical. Matching is a
case-insensitive substring test against ``window.json``'s ``model`` field
and is fully overridable per repo via ``[engagement] lean_models`` in
ctx.toml. The default is a conservative heuristic, not a per-model
measurement: the costly error is treating a lean model as capable (measured
2x turns), so mapping known fast tiers to fewer affordances fails safe;
only haiku is measured — tune the list for your model.

State: one flock-guarded JSON blob per workspace at
``.ctx-session-reads/engagement.json`` (also carries the emission governor's
tier, mechanism B). All entry points fail open.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

_STATE_NAME = "engagement.json"
_LEDGER_DIR = ".ctx-session-reads"

DEFAULT_MODE = "auto"  # auto | active | passive
DEFAULT_ACTIVATE_AFTER_CALLS = 8
# Small/fast tier of every supported host, matched case-insensitively as a
# substring of window.json's model id. Host-neutral by construction (no
# single host is canonical) and fully overridable via ctx.toml
# [engagement] lean_models. Kept as the one source of truth — config.py
# imports this so the dataclass default never drifts.
#   Claude:  claude-haiku-*            -> "haiku"
#   Gemini:  gemini-*-flash[-lite]     -> "flash" (also catches flash-lite)
#   OpenAI:  gpt-*-mini / gpt-*-nano   -> "mini", "nano"
#   generic: *-lite / *-small          -> "lite", "small"
DEFAULT_LEAN_MODELS = ("haiku", "flash", "mini", "nano", "lite", "small")
DEFAULT_SUGGESTIONS = 3
LEAN_SUGGESTIONS = 1


def _state_path(workspace_root: Path | str) -> Path:
    return Path(workspace_root) / _LEDGER_DIR / _STATE_NAME


def _mutate_state(workspace_root: Path | str, fn) -> dict[str, Any]:
    """flock'd read-modify-write of the state blob; returns the new state.
    Fail-open: any problem returns whatever ``fn`` produces from {}."""
    try:
        path = _state_path(workspace_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            try:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX)
            except ImportError:
                pass
            raw = os.read(fd, 65536)
            try:
                state = json.loads(raw.decode("utf-8")) if raw.strip() else {}
            except (json.JSONDecodeError, UnicodeDecodeError):
                state = {}
            if not isinstance(state, dict):
                state = {}
            state = fn(state)
            payload = json.dumps(state, sort_keys=True).encode("utf-8")
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, payload)
            os.ftruncate(fd, len(payload))
            return state
        finally:
            os.close(fd)
    except Exception:
        try:
            return fn({})
        except Exception:
            return {}


def read_state(workspace_root: Path | str) -> dict[str, Any]:
    try:
        raw = _state_path(workspace_root).read_text(encoding="utf-8")
        state = json.loads(raw)
        return state if isinstance(state, dict) else {}
    except Exception:
        return {}


def _proxy_doc(workspace_root: Path | str) -> dict[str, Any]:
    try:
        path = Path(workspace_root) / _LEDGER_DIR / "proxy" / "window.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else {}
    except Exception:
        return {}


def session_model(workspace_root: Path | str) -> str:
    return str(_proxy_doc(workspace_root).get("model") or "")


def model_matches_lean(model: str, lean_models=DEFAULT_LEAN_MODELS) -> bool:
    """True when a lean-model tag appears as a *tier token* in ``model``,
    not merely as a substring. Letter boundaries only: a tag must not be
    glued to another letter on either side, so ``mini`` matches
    ``gpt-5-mini`` but NOT ``gemini-3-pro`` ("ge**mini**"); digits and
    separators are allowed neighbours so ``flash2``/``haiku4`` still match.
    Case-insensitive. Host-neutral by construction."""
    if not model:
        return False
    low = model.lower()
    for tag in lean_models:
        t = str(tag).lower().strip()
        if t and re.search(rf"(?<![a-z]){re.escape(t)}(?![a-z])", low):
            return True
    return False


def is_lean_model(workspace_root: Path | str, lean_models=DEFAULT_LEAN_MODELS) -> bool:
    return model_matches_lean(session_model(workspace_root), lean_models)


def note_call(
    workspace_root: Path | str | None,
    *,
    mode: str = DEFAULT_MODE,
    activate_after_calls: int = DEFAULT_ACTIVATE_AFTER_CALLS,
    window_pressure_pct: int = 70,
) -> str:
    """Count one hook interception and apply graduation rules. Returns the
    session's engagement level after this call."""
    if workspace_root is None or mode == "active":
        return "active"
    if mode == "passive":
        return "passive"

    window_doc = _proxy_doc(workspace_root)
    pct = window_doc.get("window_pct")
    window_hot = isinstance(pct, (int, float)) and not isinstance(pct, bool) and (
        float(pct) >= window_pressure_pct / 2
    )

    def step(state: dict[str, Any]) -> dict[str, Any]:
        state["calls"] = int(state.get("calls") or 0) + 1
        if state.get("level") != "active" and (
            state["calls"] >= activate_after_calls or window_hot
        ):
            state["level"] = "active"
            state.setdefault(
                "activated_by",
                "window_pressure" if window_hot else "call_count",
            )
        state.setdefault("level", "passive")
        return state

    return str(_mutate_state(workspace_root, step).get("level", "active"))


def note_truncation(workspace_root: Path | str | None) -> None:
    """A digest omitted content: the session has provably outgrown 'small'.
    Flips auto-mode sessions to active immediately."""
    if workspace_root is None:
        return

    def step(state: dict[str, Any]) -> dict[str, Any]:
        if state.get("level") != "active":
            state["level"] = "active"
            state["activated_by"] = "truncation"
        return state

    _mutate_state(workspace_root, step)


def claim_emission_tier(workspace_root: Path | str | None, tier: int) -> bool:
    """Emission governor (mechanism B): claim a pressure tier exactly once
    per session. Returns True when ``tier`` is newly crossed — the caller
    should nudge; False otherwise — stay silent. Concurrency-safe (flock)."""
    if workspace_root is None or tier < 1:
        return False
    claimed = {"new": False}

    def step(state: dict[str, Any]) -> dict[str, Any]:
        prev = int(state.get("emission_tier") or 0)
        if tier > prev:
            state["emission_tier"] = tier
            claimed["new"] = True
        return state

    _mutate_state(workspace_root, step)
    return claimed["new"]


def note_symbol_grep(workspace_root: Path | str | None, symbol: str) -> tuple[int, bool]:
    """Record a bare-identifier grep (navigation governor). Returns
    (distinct_symbols_grepped, already_nudged). Concurrency-safe; fail-open."""
    if workspace_root is None:
        return 0, True
    result = {"count": 0, "fired": False}

    def step(state: dict[str, Any]) -> dict[str, Any]:
        syms = state.get("grep_symbols")
        if not isinstance(syms, list):
            syms = []
        if symbol not in syms:
            syms.append(symbol[:64])
        state["grep_symbols"] = syms[:64]  # bounded
        result["count"] = len(state["grep_symbols"])
        result["fired"] = bool(state.get("nav_nudged"))
        # Latch the nudge the moment the threshold is first reached, so the
        # governor fires exactly once per session.
        if not result["fired"] and result["count"] >= 3:
            state["nav_nudged"] = True
        return state

    _mutate_state(workspace_root, step)
    return result["count"], result["fired"]


def filter_digest(text: str, cap: int) -> str:
    """Emission-boundary affordance filter. The stored digest is canonical
    and deterministic (SPEC §8); what enters model context is this filtered
    copy: cap 0 drops the "next:" block entirely, cap N keeps N suggestions."""
    if cap >= DEFAULT_SUGGESTIONS:
        return text
    out: list[str] = []
    in_next = kept = 0
    for line in text.splitlines():
        if line == "next:":
            in_next = 1
            if cap > 0:
                out.append(line)
            continue
        if in_next and line.startswith("  ") and line.strip():
            kept += 1
            if kept <= cap:
                out.append(line)
            continue
        in_next = 0
        out.append(line)
    return "\n".join(out)


def suggestion_cap(
    workspace_root: Path | str | None,
    *,
    mode: str = DEFAULT_MODE,
    lean_models=DEFAULT_LEAN_MODELS,
) -> int:
    """How many "next:" suggestions a digest may carry right now."""
    if workspace_root is None:
        return DEFAULT_SUGGESTIONS
    if mode == "passive":
        return 0
    level = "active"
    if mode == "auto":
        level = str(read_state(workspace_root).get("level") or "passive")
    if level != "active":
        return 0
    if is_lean_model(workspace_root, lean_models):
        return LEAN_SUGGESTIONS
    return DEFAULT_SUGGESTIONS


def note_taught(workspace_root: Path | str | None, lesson: str) -> bool:
    """Record that ``lesson`` has been taught this session; True the first time.

    The guard's remediation text is itself context. Replaying this repo's own
    transcripts showed sessions where the harness *cost* tokens rather than
    saving them (worst: 128 -> 439, -243%), because six denials each re-emitted
    the same ~50-token explanation of a lesson the model had already taken on
    call one. So a lesson is spelled out once and then referred to.

    Fail-open: any problem returns True (teach in full), because an unexplained
    denial is worse than a repeated one."""
    if workspace_root is None or not lesson:
        return True
    try:
        seen: list[str] = []

        def _fn(state: dict[str, Any]) -> dict[str, Any]:
            taught = state.get("taught")
            if not isinstance(taught, list):
                taught = []
            seen.append(lesson if lesson in taught else "")
            if lesson not in taught:
                taught = [*taught, lesson][-32:]  # bounded: a session's lessons
            state["taught"] = taught
            return state

        _mutate_state(workspace_root, _fn)
        return not (seen and seen[0])
    except Exception:
        return True
