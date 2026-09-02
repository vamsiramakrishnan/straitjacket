"""Trusted, host-neutral usage records for orchestrated one-shot runs.

The orchestrator must not scrape token-looking numbers from model prose.  This
module accepts only the structured envelopes emitted by supported hosts and
normalises them to the categories used by :mod:`ctx.pricing`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ctx import pricing


@dataclass(frozen=True)
class ActualUsage:
    """Measured usage for one host process attempt.

    ``input_tokens`` excludes cached input.  This matters for Codex/OpenAI,
    whose ``input_tokens`` count includes ``cached_input_tokens``; keeping the
    categories disjoint prevents charging cached tokens twice.
    """

    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    cost_basis: str = "priced_tokens"  # ``host_reported`` | ``priced_tokens``
    source: str = ""
    # Model turns the attempt took. The feedback signal the task steward reads:
    # a node burning past its claim is evidence its complexity was
    # underestimated, and that is a policy input, not a limit. 0 when the host
    # did not report it (Antigravity SDK), which the summary keeps distinct
    # from "took zero turns" by counting attempts that reported at all.
    turns: int = 0

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
            + self.output_tokens
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": self.cost_usd,
            "cost_basis": self.cost_basis,
            "source": self.source,
            "turns": self.turns,
        }


def _number(value: Any) -> int:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return max(0, int(value))
    return 0


def _money(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return max(0.0, float(value))
    return None


def _priced(
    *,
    model: str,
    workspace_root: Path | str | None,
    source: str,
    input_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    output_tokens: int = 0,
    reported_cost: float | None = None,
    turns: int = 0,
) -> ActualUsage | None:
    tokens = {
        "input": max(0, input_tokens),
        "cache_read": max(0, cache_read_tokens),
        "cache_write": max(0, cache_write_tokens),
        "output": max(0, output_tokens),
    }
    if not any(tokens.values()) and reported_cost is None:
        return None
    return ActualUsage(
        input_tokens=tokens["input"],
        cache_read_tokens=tokens["cache_read"],
        cache_write_tokens=tokens["cache_write"],
        output_tokens=tokens["output"],
        cost_usd=(
            reported_cost
            if reported_cost is not None
            else pricing.cost_usd(tokens, model, workspace_root=workspace_root)
        ),
        cost_basis="host_reported" if reported_cost is not None else "priced_tokens",
        source=source,
        turns=max(0, int(turns)),
    )


def parse_claude_json(
    stdout: str,
    *,
    model: str,
    workspace_root: Path | str | None = None,
) -> tuple[str, ActualUsage | None]:
    """Extract Claude Code's ``--output-format json`` result and usage."""
    try:
        doc = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return stdout, None
    if not isinstance(doc, dict):
        return stdout, None
    raw_usage = doc.get("usage")
    usage = raw_usage if isinstance(raw_usage, dict) else {}
    measured = _priced(
        model=model,
        workspace_root=workspace_root,
        source="claude_json",
        input_tokens=_number(usage.get("input_tokens")),
        cache_read_tokens=_number(usage.get("cache_read_input_tokens")),
        cache_write_tokens=_number(usage.get("cache_creation_input_tokens")),
        output_tokens=_number(usage.get("output_tokens")),
        reported_cost=_money(doc.get("total_cost_usd")),
        turns=_number(doc.get("num_turns")),
    )
    result = doc.get("result")
    if not isinstance(result, str):
        structured = doc.get("structured_output")
        result = json.dumps(structured, sort_keys=True) if structured is not None else stdout
    return result, measured


def parse_codex_jsonl(
    stdout: str,
    *,
    model: str,
    workspace_root: Path | str | None = None,
) -> tuple[str, ActualUsage | None]:
    """Extract Codex ``exec --json`` agent text and turn usage.

    Codex reports cached input as a subset of input, unlike Anthropic's
    disjoint counters, so the uncached category is reduced before pricing.
    """
    messages: list[str] = []
    totals = {"input": 0, "cache_read": 0, "cache_write": 0, "output": 0}
    saw_usage = False
    parsed_any = False
    turns = 0
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        parsed_any = True
        item = event.get("item")
        if (
            event.get("type") == "item.completed"
            and isinstance(item, dict)
            and item.get("type") == "agent_message"
            and isinstance(item.get("text"), str)
        ):
            messages.append(item["text"])
        raw = event.get("usage")
        if event.get("type") == "turn.completed":
            turns += 1
        if event.get("type") == "turn.completed" and isinstance(raw, dict):
            full_input = _number(raw.get("input_tokens"))
            cached = _number(raw.get("cached_input_tokens"))
            totals["input"] += max(0, full_input - cached)
            totals["cache_read"] += cached
            totals["cache_write"] += _number(raw.get("cache_write_input_tokens"))
            totals["output"] += _number(raw.get("output_tokens")) + _number(
                raw.get("reasoning_output_tokens")
            )
            saw_usage = True
    if not parsed_any:
        return stdout, None
    measured = None
    if saw_usage:
        measured = _priced(
            model=model,
            workspace_root=workspace_root,
            source="codex_jsonl",
            input_tokens=totals["input"],
            cache_read_tokens=totals["cache_read"],
            cache_write_tokens=totals["cache_write"],
            output_tokens=totals["output"],
            turns=turns,
        )
    return (messages[-1] if messages else stdout), measured


def parse_antigravity_sdk_json(
    stdout: str,
    *,
    model: str,
    workspace_root: Path | str | None = None,
) -> tuple[str, ActualUsage | None]:
    """Extract the final usage record emitted by ctx's SDK shim ``--json``."""
    lines = stdout.splitlines()
    if not lines:
        return stdout, None
    try:
        doc = json.loads(lines[-1])
    except (json.JSONDecodeError, TypeError):
        return stdout, None
    if not isinstance(doc, dict):
        return stdout, None
    measured = _priced(
        model=model,
        workspace_root=workspace_root,
        source="antigravity_sdk_json",
        input_tokens=_number(doc.get("input_tokens")),
        output_tokens=_number(doc.get("output_tokens")),
    )
    return "\n".join(lines[:-1]), measured


def parse_host_output(
    host: str,
    stdout: str,
    *,
    model: str,
    workspace_root: Path | str | None = None,
) -> tuple[str, ActualUsage | None]:
    """Dispatch a known structured host envelope; never parse plain prose."""
    if host == "claude":
        return parse_claude_json(stdout, model=model, workspace_root=workspace_root)
    if host == "codex":
        return parse_codex_jsonl(stdout, model=model, workspace_root=workspace_root)
    if host == "antigravity-sdk":
        return parse_antigravity_sdk_json(
            stdout, model=model, workspace_root=workspace_root
        )
    return stdout, None


def coerce_usage(value: Any) -> ActualUsage | None:
    """Accept typed usage or a small canonical mapping from injected launchers."""
    if isinstance(value, ActualUsage):
        return value
    if not isinstance(value, dict):
        return None
    try:
        usage = ActualUsage(
            input_tokens=_number(value.get("input_tokens")),
            cache_read_tokens=_number(value.get("cache_read_tokens")),
            cache_write_tokens=_number(value.get("cache_write_tokens")),
            output_tokens=_number(value.get("output_tokens")),
            cost_usd=_money(value.get("cost_usd")),
            cost_basis=str(value.get("cost_basis") or "priced_tokens"),
            source=str(value.get("source") or "injected"),
            turns=_number(value.get("turns")),
        )
    except Exception:
        return None
    return usage if usage.total_tokens or usage.cost_usd is not None else None


def summarize_usage(attempts: Iterable[ActualUsage | None]) -> dict[str, Any]:
    """Aggregate attempts without turning missing observations into zero use."""
    rows = list(attempts)
    measured = [row for row in rows if row is not None]
    status = (
        "available"
        if rows and len(measured) == len(rows)
        else "partial"
        if measured
        else "unavailable"
    )
    costs = [row.cost_usd for row in measured if row.cost_usd is not None]
    bases = sorted({row.cost_basis for row in measured})
    return {
        "status": status,
        "attempts_total": len(rows),
        "attempts_measured": len(measured),
        "input_tokens": sum(row.input_tokens for row in measured),
        "cache_read_tokens": sum(row.cache_read_tokens for row in measured),
        "cache_write_tokens": sum(row.cache_write_tokens for row in measured),
        "output_tokens": sum(row.output_tokens for row in measured),
        "total_tokens": sum(row.total_tokens for row in measured),
        "cost_usd": sum(costs) if costs else None,
        "cost_complete": bool(measured) and len(costs) == len(rows),
        "cost_basis": bases[0] if len(bases) == 1 else "mixed" if bases else None,
        "sources": sorted({row.source for row in measured if row.source}),
        "turns": sum(row.turns for row in measured),
        # How many measured attempts reported a turn count at all, so a 0
        # above can be read as "unreported" rather than "instant".
        "turns_reported": sum(1 for row in measured if row.turns > 0),
    }


__all__ = [
    "ActualUsage",
    "coerce_usage",
    "parse_antigravity_sdk_json",
    "parse_claude_json",
    "parse_codex_jsonl",
    "parse_host_output",
    "summarize_usage",
]
