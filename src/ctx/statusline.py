"""Host-neutral status line rendering.

Every supported host surfaces session state to a status line differently:

* **Claude Code** runs a command (settings.json ``statusLine``) that receives
  session JSON on stdin — including a host-computed ``cost.total_cost_usd``.
* **Antigravity** runs a command (``~/.gemini/antigravity-cli/settings.json``
  ``statusLine``) that receives session JSON on stdin — model, context-window
  %, token counts, git — but *no* dollar cost.
* **Codex** does NOT run a command: its ``tui.status_line`` is an ordered list
  of built-in items it renders itself, so we configure items there and, for a
  dollar figure, summarise the session rollout JSONL out of band.

``render`` normalises each host's payload into one compact line and derives
cost with the same rule everywhere: **prefer a host-reported dollar cost;
otherwise price token counts through** :mod:`ctx.pricing`. Fail-open by
contract — malformed input yields a short line or an empty string, never a
crash, because a status line must never break the host's REPL.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ctx import pricing
from ctx.proxywindow import read_window_doc
from ctx.sessiondir import session_reads_path
from ctx.textutil import fmt_tokens_compact

_SEP = "  ·  "


def _dig(obj: Any, *paths: str) -> Any:
    """First present value among dotted paths (e.g. ``model.display_name``)."""
    for path in paths:
        cur: Any = obj
        ok = True
        for key in path.split("."):
            if isinstance(cur, dict) and key in cur:
                cur = cur[key]
            else:
                ok = False
                break
        if ok and cur is not None:
            return cur
    return None


def _short_model(model: str) -> str:
    """Trim vendor/date noise: ``claude-sonnet-5`` stays, long dated ids lose
    the trailing date stamp for width."""
    m = str(model)
    parts = m.split("-")
    if parts and parts[-1].isdigit() and len(parts[-1]) >= 8:
        parts = parts[:-1]
    return "-".join(parts)


# Shared with ctx.textutil rather than re-implemented here; that module
# documents which of the token renderings does which job.
_fmt_tokens = fmt_tokens_compact


def _fmt_usd(x: float) -> str:
    if x >= 100:
        return f"${x:,.0f}"
    if x >= 1:
        return f"${x:.2f}"
    return f"${x:.3f}"


def _tokens_from_payload(host: str, payload: dict[str, Any]) -> dict[str, int]:
    """Best-effort token breakdown from a host status payload. Antigravity's
    exact token paths vary by version, so several aliases are probed; missing
    categories are simply absent (priced as zero)."""
    out: dict[str, int] = {}
    mapping = {
        "input": ("usage.input_tokens", "tokens.input", "prompt_token_count",
                  "context_window.input_tokens"),
        "cache_read": ("usage.cache_read_input_tokens", "cached_content_token_count",
                       "tokens.cache_read"),
        "cache_write": ("usage.cache_creation_input_tokens", "tokens.cache_write"),
        "output": ("usage.output_tokens", "candidates_token_count", "tokens.output"),
    }
    for cat, paths in mapping.items():
        v = _dig(payload, *paths)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[cat] = int(v)
    return out


def _session_cost(host: str, payload: dict[str, Any], model: str,
                  workspace_root: Path | str | None) -> tuple[float | None, bool]:
    """(cost_usd, host_reported). Prefer a dollar cost the host already
    computed (Claude Code); otherwise price token counts, returning None when
    no tokens are available to price."""
    reported = _dig(payload, "cost.total_cost_usd", "cost_usd", "total_cost_usd")
    if isinstance(reported, (int, float)) and not isinstance(reported, bool):
        return float(reported), True
    tokens = _tokens_from_payload(host, payload)
    if tokens:
        return pricing.cost_usd(tokens, model, workspace_root=workspace_root), False
    return None, False


def render(host: str, payload: dict[str, Any],
           workspace_root: Path | str | None = None) -> str:
    """One compact status line for ``host`` from its session ``payload``.
    Never raises."""
    try:
        if not isinstance(payload, dict):
            return ""
        host = (host or "").lower()
        segs: list[str] = []

        model = _dig(payload, "model.display_name", "model.id", "model") or ""
        model = str(model)
        if model:
            segs.append(_short_model(model))

        pct = _dig(payload, "context_window.used_percentage",
                   "context.used_percentage", "context_used_pct")
        if isinstance(pct, (int, float)) and not isinstance(pct, bool):
            segs.append(f"ctx {float(pct):.0f}%")
        elif _dig(payload, "exceeds_200k_tokens") is True:
            segs.append("ctx >200K")

        cost, reported = _session_cost(host, payload, model, workspace_root)
        if cost is not None:
            tag = _fmt_usd(cost)
            segs.append(tag if reported else f"~{tag}")

        added = _dig(payload, "cost.total_lines_added")
        removed = _dig(payload, "cost.total_lines_removed")
        if isinstance(added, int) and isinstance(removed, int) and (added or removed):
            segs.append(f"+{added}/-{removed}")

        branch = _dig(payload, "vcs.branch", "git.branch", "workspace.branch")
        if branch:
            dirty = "*" if _dig(payload, "vcs.dirty", "git.dirty") else ""
            segs.append(f"⎇ {branch}{dirty}")

        # Harness signature segment: what ctx has contained this session, read
        # cheaply from the session ledger.
        seg = _harness_segment(workspace_root)
        if seg:
            segs.append(seg)

        return _SEP.join(str(s) for s in segs if s)
    except Exception:
        return ""


# Host config files whose presence-with-a-ctx-hook means "this repo is
# harnessed". Matched as plain substrings so the check costs one small read
# and no JSON parse — a status line re-renders on every REPL turn.
_HOOK_MARKERS: tuple[tuple[str, str], ...] = (
    (".claude/settings.json", "hook claude-code"),
    (".claude/settings.local.json", "hook claude-code"),
    (".codex/hooks.json", "hook codex"),
)
_PLUGIN_REL = ".agents/plugins/ctx-harness"


def _harness_installed(root: Path) -> bool:
    """Whether any agent in this repo is actually hooked into ctx.

    Cheap and fail-open: a status line must never raise, and must never make
    the host's REPL wait. Mirrors doctor's "an agent is wrapped" check."""
    try:
        for rel, marker in _HOOK_MARKERS:
            try:
                if marker in (root / rel).read_text(encoding="utf-8", errors="replace"):
                    return True
            except OSError:
                continue
        return (root / _PLUGIN_REL).is_dir()
    except Exception:
        return False


def _harness_segment(workspace_root: Path | str | None) -> str | None:
    """The status line's one statement about ctx.

    Before, this segment was simply omitted whenever there was nothing to
    report — so "ctx is off" and "ctx is on and idle" rendered identically,
    and the failure mode the status line exists to surface (nothing is
    hooked, so nothing is being contained) was the one it could not show.
    Three distinguishable states now:

        ctx◇ 12K kept out   the harness is working, here is the number
        ctx◇ idle           hooked, nothing contained yet this session
        ctx◇ off            nothing is hooked — run `ctx setup`

    None only when there is no workspace to speak about at all."""
    if workspace_root is None:
        return None
    try:
        root = Path(workspace_root)
        saved = _harness_saved(root)
        if saved:
            return f"ctx◇ {saved}"
        return "ctx◇ idle" if _harness_installed(root) else "ctx◇ off"
    except Exception:
        return None


def _harness_saved(workspace_root: Path | str | None) -> str | None:
    """Tokens kept out of context, for the status line.

    This is the one number that tells a user the harness is doing anything, so
    it must appear on the ordinary `ctx wrap` path — not only when someone
    opted into `--proxy`. Order: the proxy's exact counter if present, else the
    same capture telemetry `ctx gain` reads.

    Never raises; nothing to report → None."""
    if workspace_root is None:
        return None
    root = Path(workspace_root)
    doc = read_window_doc(root)
    saved = doc.get("contained_tokens") or doc.get("saved_tokens")
    if isinstance(saved, (int, float)) and not isinstance(saved, bool) and saved > 0:
        return f"{_fmt_tokens(int(saved))} kept out"
    contained = _contained_tokens_from_telemetry(root)
    if contained and contained > 0:
        return f"{_fmt_tokens(contained)} kept out"
    return None


def _contained_tokens_from_telemetry(root: Path) -> int | None:
    """Bytes contained so far, as tokens, from the capture telemetry.

    A status line re-renders on every REPL turn, and telemetry.jsonl only
    grows — a full re-read costs ~15 ms at 10k events and ~76 ms at 50k, which
    would show as lag. So keep a tiny sidecar of (offset, raw, emitted) and read
    only the bytes appended since last time. Truncation or rotation (size <
    offset) falls back to a full recount. Fail-open at every step."""
    try:
        from ctx.workspace import resolve_workspace

        from ctx.store import Store

        ws = resolve_workspace(str(root))
        store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
        path = store.audit_dir / "telemetry.jsonl"
        size = path.stat().st_size
    except Exception:
        return None

    cache_path = session_reads_path(root, "gain-cache.json")
    offset = raw = emitted = 0
    try:
        c = json.loads(cache_path.read_text(encoding="utf-8"))
        if int(c.get("size", 0)) <= size:
            offset, raw, emitted = int(c["size"]), int(c["raw"]), int(c["emitted"])
    except Exception:
        pass

    if size > offset:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                fh.seek(offset)
                for line in fh:
                    try:
                        ev = json.loads(line)
                    except Exception:
                        continue
                    raw += int(ev.get("raw_bytes") or 0)
                    emitted += int(ev.get("emitted_bytes") or 0)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps({"size": size, "raw": raw, "emitted": emitted}), encoding="utf-8"
            )
        except Exception:
            return None

    kept = raw - emitted
    return kept // 4 if kept > 0 else None  # ~4 bytes/token, as `ctx gain` estimates


# --------------------------------------------------------------- Codex rollout
def codex_rollout_summary(rollout_path: Path | str,
                          workspace_root: Path | str | None = None) -> str:
    """Codex cannot render custom status text, so summarise a session rollout
    JSONL (``~/.codex/sessions/.../rollout-*.jsonl``) into one line for a Stop
    hook or ``notify`` program. Reads the last ``TokenCount`` event's
    ``total_token_usage`` and prices it. Fail-open to an empty string."""
    try:
        model = ""
        totals: dict[str, int] | None = None
        for line in Path(rollout_path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            m = _dig(item, "payload.model", "model")
            if isinstance(m, str) and m:
                model = m
            tu = _dig(item, "payload.info.total_token_usage",
                      "info.total_token_usage", "total_token_usage")
            if isinstance(tu, dict):
                totals = tu
        if not totals:
            return ""
        tokens = {
            "input": int(totals.get("input_tokens", 0) or 0),
            "cache_read": int(totals.get("cached_input_tokens", 0) or 0),
            "cache_write": int(totals.get("cache_write_input_tokens", 0) or 0),
            "output": int(totals.get("output_tokens", 0) or 0)
                      + int(totals.get("reasoning_output_tokens", 0) or 0),
        }
        total = int(totals.get("total_tokens", 0) or 0) or sum(tokens.values())
        cost = pricing.cost_usd(tokens, model, workspace_root=workspace_root)
        segs = [_short_model(model)] if model else []
        segs.append(f"{_fmt_tokens(total)} tok")
        segs.append(f"~{_fmt_usd(cost)}")
        return _SEP.join(segs)
    except Exception:
        return ""


__all__ = ["render", "codex_rollout_summary"]
