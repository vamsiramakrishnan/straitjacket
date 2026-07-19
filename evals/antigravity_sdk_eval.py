#!/usr/bin/env python3
"""Antigravity SDK A/B: naive vs straitjacket on a long, flood-heavy task.

This drives Google's **Antigravity Agent SDK** (`google-antigravity`, default
model ``gemini-3.5-flash``) headlessly with a Gemini API key — the same agent
harness the Antigravity IDE/CLI runs, authenticated via ``GEMINI_API_KEY``
instead of the interactive OAuth the ``agy`` binary requires.

Two arms differ in exactly ONE variable: whether the agent's shell-command
output is contained through ``ctx`` (straitjacket) or returned raw and re-sent
every model turn (naive).

  naive : ``shell(cmd)`` returns the raw combined stdout+stderr.  A long
          diagnostic flood lands in the transcript and is resent on every
          subsequent turn.
  sj    : ``shell(cmd)`` runs the command through ``ctx run`` and returns the
          bounded digest; a second ``ctx_query`` tool resolves any omitted
          bytes by the address the digest prints.  This is straitjacket's
          birth-gate containment, expressed as ctx-routed tools rather than
          the IDE plugin's PostToolUse hook (the SDK's PostToolCall hook is
          inspect-only, so containment is applied at the tool boundary).

Everything else is identical between arms: the Antigravity SDK ``Agent``, the
model, the system instructions, the task prompt, the fixture, the builtin file
tools, and the ``allow_all`` autonomous policy.

The task is a genuine long agentic job with an engineered flood: a failing
test whose *root cause* is documented by a single "INCIDENT NOTE" line buried
in a large diagnostic log.  The test says *what* is wrong; only the flood says
*why* and *how* to fix it — so the agent must actually handle the flood.

Metrics per arm (billed tokens come from the SDK's UsageMetadata):
  - billed total / input(resend) / output / thoughts tokens
  - tool-output tokens that entered context (containment metric)
  - raw command bytes the shell actually produced (the "would-have-flooded"
    baseline; ~equal across arms since both run the same commands)
  - tool calls, compaction events, wall time
  - completed / correct (independent test re-run) / needle cited

Usage:
  GEMINI_API_KEY=... <sdk-venv>/bin/python evals/antigravity_sdk_eval.py \
      --out evals/_runs/antigravity --flood-lines 4000 --model gemini-3.5-flash

Requires: the ``google-antigravity`` SDK (a venv is fine) and ``ctx`` on PATH.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import subprocess
import sys
import time

# Reuse the repo's token estimator so numbers are comparable with other evals.
_REPO_SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
if _REPO_SRC.is_dir():
    sys.path.insert(0, str(_REPO_SRC))
try:
    from ctx.textutil import estimate_tokens as _est_bytes  # type: ignore
except Exception:  # pragma: no cover - fallback if ctx not importable
    def _est_bytes(n_bytes: int) -> int:
        return max(1, n_bytes // 4) if n_bytes else 0


def est_tokens(text: str) -> int:
    """Estimate tokens for a string via the repo's byte-based estimator."""
    return _est_bytes(len(text.encode("utf-8", "replace")))

from google.antigravity import (  # noqa: E402
    Agent,
    CapabilitiesConfig,
    LocalAgentConfig,
)
from google.antigravity import hooks  # noqa: E402
from google.antigravity.hooks import policy  # noqa: E402
from google.antigravity.types import BuiltinTools, ToolResult  # noqa: E402


# ----------------------------------------------------------------------------
# Fixture: a failing test whose root cause is buried in a diagnostic flood.
# ----------------------------------------------------------------------------

# Scenario "keyword": the needle announces itself with a distinctive phrase, so
# a shell-savvy agent can `grep` for it and skip the flood. Scenario "quiet":
# the needle is structurally rare but lexically normal — it looks like every
# other INFO line except one field value differs — so keyword grep cannot find
# it; the agent must ingest the flood (naive) or read the structural digest that
# ctx's logtemplate profile surfaces automatically (sj).

NEEDLE = (
    "INCIDENT NOTE: apac fallback was a temporary circuit-breaker workaround, "
    "reverted 2026-06 — apac must route to primary again. "
    "Fix: remove 'apac' from FALLBACK_REGIONS in router.py."
)

_ROUTER_PY = '''\
"""Region -> gateway router."""

# BUG: 'apac' was added as a temporary circuit-breaker fallback and never
# removed. Per the incident log it must route to 'primary' again.
FALLBACK_REGIONS = {"apac", "emea"}


def route(region: str) -> str:
    """Return the gateway a region should use."""
    return "legacy" if region in FALLBACK_REGIONS else "primary"
'''

_VERIFY_PY = '''\
"""Stdlib-only test runner (no pytest dependency)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from router import route

cases = [
    ("apac", "primary"),   # currently fails: apac wrongly falls back to legacy
    ("us", "primary"),
    ("emea", "legacy"),
]
failed = []
for region, expected in cases:
    got = route(region)
    ok = got == expected
    print(f"{'PASS' if ok else 'FAIL'} route({region!r}) == {expected!r} (got {got!r})")
    if not ok:
        failed.append(region)

if failed:
    print(f"FAILED: {len(failed)} case(s): {failed}")
    sys.exit(1)
print("ALL TESTS PASSED")
'''


def _diagnose_py(flood_lines: int, needle_pos: int, scenario: str) -> str:
    if scenario == "quiet":
        # Every line is shape-identical; the needle differs only in that it
        # carries gateway=legacy (all others gateway=primary). No error/incident
        # keyword. region=apac on the needle is the evidence for the fix.
        return f'''\
"""Replays the routing gateway access log."""
import sys

N = {flood_lines}
NEEDLE_AT = {needle_pos}

for i in range(1, N + 1):
    if i == NEEDLE_AT:
        print(
            f"[{{i:06d}}] INFO worker-{{i % 8}} checkout request "
            f"req-{{100000 + i}} region=apac gateway=legacy latency=7ms ok"
        )
    else:
        print(
            f"[{{i:06d}}] INFO worker-{{i % 8}} checkout request "
            f"req-{{100000 + i}} region=us gateway=primary latency=6ms ok"
        )
sys.stderr.write("diagnose: replayed %d access-log lines\\n" % N)
'''
    return f'''\
"""Replays a production incident log for the routing regression."""
import sys

N = {flood_lines}
NEEDLE_AT = {needle_pos}
NEEDLE = {NEEDLE!r}

for i in range(1, N + 1):
    if i == NEEDLE_AT:
        print(f"[{{i:06d}}] {{NEEDLE}}")
    else:
        print(
            f"[{{i:06d}}] INFO worker-{{i % 8}} checkout request "
            f"req-{{100000 + i}} region=us gateway=primary completed in budget"
        )
sys.stderr.write("diagnose: replayed %d log lines\\n" % N)
'''


def build_fixture(root: pathlib.Path, flood_lines: int, scenario: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "router.py").write_text(_ROUTER_PY, encoding="utf-8")
    (root / "diagnose.py").write_text(
        _diagnose_py(flood_lines, flood_lines // 2 + 137, scenario),
        encoding="utf-8",
    )
    tests = root / "tests"
    tests.mkdir(exist_ok=True)
    (tests / "verify.py").write_text(_VERIFY_PY, encoding="utf-8")
    # Make it a ctx workspace (git root + ctx.toml) so `ctx run` resolves it.
    subprocess.run(["git", "init", "-q", "."], cwd=root, check=True)
    subprocess.run(
        ["ctx", "init"], cwd=root, check=False, capture_output=True, text=True
    )


TASK_KEYWORD = (
    "This project has a failing test (tests/verify.py). Fix it.\n"
    "1. Use the `shell` tool to run the tests: `python3 tests/verify.py` — "
    "observe the failure.\n"
    "2. The root cause is documented in a production incident log. Use the "
    "`shell` tool to run `python3 diagnose.py` and find the single "
    "'INCIDENT NOTE' line that explains the required fix. The log is large and "
    "the note is buried in the middle of it.\n"
    "3. Apply the minimal source fix that the incident note describes.\n"
    "4. Use the `shell` tool to re-run `python3 tests/verify.py` until it "
    "prints ALL TESTS PASSED.\n"
    "5. Call finish with a one-sentence summary that quotes the incident note "
    "and names the one-line fix you made.\n"
    "Do NOT modify any file under tests/."
)

TASK_QUIET = (
    "This project has a failing test (tests/verify.py). Fix it.\n"
    "1. Use the `shell` tool to run the tests: `python3 tests/verify.py` — "
    "observe the failure.\n"
    "2. Use the `shell` tool to run `python3 diagnose.py`. It prints a large "
    "gateway access log. Exactly ONE request in that log was routed to a "
    "different gateway than every other request — that anomalous line is the "
    "evidence for the bug. It is NOT flagged by any error/incident keyword; it "
    "is shape-identical to every other line except for one field value, so you "
    "cannot find it by grepping for a keyword. Identify which region was "
    "mis-routed.\n"
    "3. Apply the minimal fix to router.py so that region routes to the same "
    "gateway as the others.\n"
    "4. Use the `shell` tool to re-run `python3 tests/verify.py` until it "
    "prints ALL TESTS PASSED.\n"
    "5. Call finish with a one-sentence summary naming the anomalous request "
    "(region + gateway) and the one-line fix you made.\n"
    "Do NOT modify any file under tests/."
)

TASKS = {"keyword": TASK_KEYWORD, "quiet": TASK_QUIET}

SYSTEM = (
    "You are Antigravity, an autonomous coding agent working in a real "
    "repository. Work step by step using the available tools. Prefer the "
    "`shell` tool for running commands. Keep going until the task is complete, "
    "then call finish. Be concise."
)

MAX_SHELL_CHARS = 300_000  # guardrail so one pathological output can't 400 the API


# ----------------------------------------------------------------------------
# Per-arm run
# ----------------------------------------------------------------------------


class Meter:
    """Records tool-output volume entering context, via the inspect hook."""

    def __init__(self) -> None:
        self.tool_calls = 0
        self.tool_output_tokens = 0  # est tokens of results the model received
        self.raw_command_bytes = 0  # bytes the shell actually produced
        self.compactions = 0

    def hook(self):
        @hooks.post_tool_call
        async def _meter(result: ToolResult):  # noqa: ANN001
            self.tool_calls += 1
            out = "" if result.result is None else str(result.result)
            self.tool_output_tokens += est_tokens(out)

        return _meter

    def compaction_hook(self):
        @hooks.on_compaction
        async def _comp(_step):  # noqa: ANN001
            self.compactions += 1

        return _comp


def make_tools(arm: str, root: pathlib.Path, meter: Meter):
    """Return the tool callables for an arm. Only `shell` differs."""

    def _run_raw(command: str) -> str:
        p = subprocess.run(
            command, shell=True, capture_output=True, text=True, cwd=str(root)
        )
        combined = p.stdout + p.stderr
        meter.raw_command_bytes += len(combined.encode("utf-8", "replace"))
        return combined[:MAX_SHELL_CHARS]

    if arm == "naive":

        def shell(command: str) -> str:
            """Execute a shell command in the workspace; returns stdout+stderr."""
            return _run_raw(command)

        return [shell]

    # straitjacket arm: route shell output through `ctx run`, add a retrieval tool.
    def shell(command: str) -> str:
        """Execute a shell command in the workspace; returns stdout+stderr."""
        # Measure the true raw volume (for the baseline) without letting it
        # enter context: run once captured, then return only the ctx digest.
        raw = subprocess.run(
            command, shell=True, capture_output=True, text=True, cwd=str(root)
        )
        meter.raw_command_bytes += len(
            (raw.stdout + raw.stderr).encode("utf-8", "replace")
        )
        cap = subprocess.run(
            ["ctx", "run", "--", "bash", "-lc", command],
            capture_output=True,
            text=True,
            cwd=str(root),
        )
        return (cap.stdout + cap.stderr)[:MAX_SHELL_CHARS]

    def ctx_query(args: str) -> str:
        """Run a bounded `ctx` retrieval command (e.g. 'get run:<id>#stdout
        --lines 100:140' or 'search run:<id> INCIDENT'). Use the run id and
        address printed by the `shell` digest to pull exact omitted lines."""
        parts = args.split()
        cap = subprocess.run(
            ["ctx", *parts], capture_output=True, text=True, cwd=str(root)
        )
        return (cap.stdout + cap.stderr)[:MAX_SHELL_CHARS]

    return [shell, ctx_query]


async def run_arm(
    arm: str, root: pathlib.Path, model: str, wall_timeout: float, task: str
) -> dict:
    meter = Meter()
    root = root.resolve()
    disabled = [BuiltinTools.RUN_COMMAND]
    cfg = LocalAgentConfig(
        model=model,
        api_key=os.environ["GEMINI_API_KEY"],
        workspaces=[str(root)],
        tools=make_tools(arm, root, meter),
        capabilities=CapabilitiesConfig(disabled_tools=disabled),
        hooks=[meter.hook(), meter.compaction_hook()],
        policies=[policy.allow_all()],
        system_instructions=SYSTEM,
        app_data_dir=str(root / f".agdata_{arm}"),
    )
    t0 = time.monotonic()
    final_text = ""
    timed_out = False
    async with Agent(cfg) as agent:
        try:
            resp = await asyncio.wait_for(agent.chat(task), timeout=wall_timeout)
            final_text = await resp.text()
        except asyncio.TimeoutError:
            timed_out = True
        usage = agent.conversation.total_usage
    wall_s = time.monotonic() - t0

    correct = grade_correct(root)
    needle_cited = ("apac" in final_text.lower()) and (
        "fallback_regions" in final_text.lower()
        or "circuit" in final_text.lower()
        or "primary" in final_text.lower()
    )
    return {
        "arm": arm,
        "model": model,
        "billed_total_tokens": usage.total_token_count or 0,
        "billed_input_tokens": usage.prompt_token_count or 0,
        "billed_output_tokens": usage.candidates_token_count or 0,
        "billed_thoughts_tokens": usage.thoughts_token_count or 0,
        "billed_cached_tokens": usage.cached_content_token_count or 0,
        "tool_output_tokens_into_context": meter.tool_output_tokens,
        "raw_command_bytes": meter.raw_command_bytes,
        "tool_calls": meter.tool_calls,
        "compactions": meter.compactions,
        "wall_s": round(wall_s, 1),
        "timed_out": timed_out,
        "completed": bool(final_text) and not timed_out,
        "correct": correct,
        "needle_cited": needle_cited,
        "final_text": final_text[:600],
    }


def grade_correct(root: pathlib.Path) -> bool:
    p = subprocess.run(
        ["python3", "tests/verify.py"],
        cwd=str(root),
        capture_output=True,
        text=True,
    )
    return p.returncode == 0


# ----------------------------------------------------------------------------
# Report
# ----------------------------------------------------------------------------


def render_report(records: list[dict], flood_lines: int, model: str) -> str:
    by = {r["arm"]: r for r in records}
    n = by.get("naive", {})
    s = by.get("sj", {})
    scenario = (records[0].get("scenario") if records else "?") or "?"

    def ratio(a, b):
        return f"{a / b:.1f}×" if b else "—"

    lines = [
        "# Antigravity SDK A/B — naive vs straitjacket on a long flood task",
        "",
        f"Host: **Google Antigravity Agent SDK** (`google-antigravity`), "
        f"model **{model}**, headless via `GEMINI_API_KEY`.",
        f"Scenario: **{scenario}** · fix a failing test whose root cause is one "
        f"anomalous line buried in a {flood_lines:,}-line diagnostic flood "
        f"(`keyword` = greppable phrase; `quiet` = structurally-rare, no "
        f"keyword). Single variable: whether `shell` output is ctx-contained.",
        "",
        "| metric | naive | straitjacket | delta |",
        "|---|--:|--:|--:|",
    ]

    def row(label, key, better="low", fmt="{:,}"):
        a, b = n.get(key), s.get(key)
        if a is None or b is None:
            return f"| {label} | — | — | — |"
        d = ""
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            if better == "low" and b:
                d = f"{ratio(a, b)} less" if a >= b else f"{ratio(b, a)} more"
            elif better == "low":
                d = "—"
        return f"| {label} | {fmt.format(a)} | {fmt.format(b)} | {d} |"

    lines.append(row("billed total tokens", "billed_total_tokens"))
    lines.append(row("  · input (resend)", "billed_input_tokens"))
    lines.append(row("  · output", "billed_output_tokens"))
    lines.append(row("  · thoughts", "billed_thoughts_tokens"))
    lines.append(
        row("tool-output tokens into context", "tool_output_tokens_into_context")
    )
    lines.append(row("raw command bytes produced", "raw_command_bytes"))
    lines.append(row("tool calls", "tool_calls", better="none"))
    lines.append(row("compaction events", "compactions", better="none"))
    lines.append(row("wall seconds", "wall_s", better="none", fmt="{}"))
    lines.append(
        f"| completed | {n.get('completed')} | {s.get('completed')} | |"
    )
    lines.append(
        f"| **correct** (tests pass) | {n.get('correct')} | {s.get('correct')} | |"
    )
    lines.append(
        f"| needle cited | {n.get('needle_cited')} | {s.get('needle_cited')} | |"
    )
    lines.append("")
    for arm in ("naive", "sj"):
        r = by.get(arm, {})
        lines.append(f"### {arm} — final")
        lines.append("```")
        lines.append((r.get("final_text") or "").strip() or "(no final text)")
        lines.append("```")
    lines.append("")
    lines.append(
        "_Billed tokens are the SDK's `UsageMetadata` totals. "
        "`tool-output tokens into context` is the est-token size of the tool "
        "results the model received (the containment metric). `raw command "
        "bytes` is what the commands actually emitted — ~equal across arms, "
        "since both run the same commands; the naive arm puts those bytes in "
        "context while straitjacket replaces them with a bounded digest plus a "
        "retrieval address._"
    )
    return "\n".join(lines)


async def main_async(args) -> int:
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    task = TASKS[args.scenario]
    records = []
    for arm in ("naive", "sj"):
        root = out / f"fixture_{arm}"
        if root.exists():
            subprocess.run(["rm", "-rf", str(root)], check=False)
        build_fixture(root, args.flood_lines, args.scenario)
        print(f"[run] arm={arm} model={args.model} scenario={args.scenario} "
              f"flood={args.flood_lines} …", flush=True)
        rec = await run_arm(arm, root, args.model, args.wall_timeout, task)
        rec["scenario"] = args.scenario
        records.append(rec)
        print(
            f"[done] {arm}: total={rec['billed_total_tokens']:,} tok · "
            f"tool-out={rec['tool_output_tokens_into_context']:,} tok · "
            f"calls={rec['tool_calls']} · correct={rec['correct']} · "
            f"{rec['wall_s']}s",
            flush=True,
        )
    (out / "records.json").write_text(json.dumps(records, indent=2), "utf-8")
    report = render_report(records, args.flood_lines, args.model)
    (out / "report.md").write_text(report, "utf-8")
    print("\n" + report)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="evals/_runs/antigravity")
    ap.add_argument("--model", default="gemini-3.5-flash")
    ap.add_argument("--scenario", choices=["keyword", "quiet"], default="quiet")
    ap.add_argument("--flood-lines", type=int, default=4000)
    ap.add_argument("--wall-timeout", type=float, default=420.0)
    args = ap.parse_args()
    if not os.environ.get("GEMINI_API_KEY"):
        print("error: GEMINI_API_KEY not set", file=sys.stderr)
        return 2
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
