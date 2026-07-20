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


def _fmt_tokens(n: int) -> str:
    n = int(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(n)


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
        # cheaply from the session ledger. Absent = nothing shown (fail-open).
        saved = _harness_saved(workspace_root)
        if saved:
            segs.append(f"ctx◇ {saved}")

        return _SEP.join(str(s) for s in segs if s)
    except Exception:
        return ""


def _harness_saved(workspace_root: Path | str | None) -> str | None:
    """Tokens kept out of context so far this session, if the proxy left a
    cheap counter. Never raises; absent counter → None."""
    if workspace_root is None:
        return None
    try:
        path = Path(workspace_root) / ".ctx-session-reads" / "proxy" / "window.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        saved = doc.get("contained_tokens") or doc.get("saved_tokens")
        if isinstance(saved, (int, float)) and not isinstance(saved, bool) and saved > 0:
            return f"{_fmt_tokens(int(saved))} kept out"
    except Exception:
        return None
    return None


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
