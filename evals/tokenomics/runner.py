#!/usr/bin/env python3
"""Triage-mode referee on BigCodeBench-Hard: raw stderr vs LLM triage vs ctx digest.

Mirrors the arm structure of the tokenomics-benchmark-multi-llms suite, with
the four defects that made that suite unreportable removed:

1. **straitjacket actually runs.** The `sj` triage mode shells out to the real
   `ctx run -- <cmd>` CLI and uses the emitted digest verbatim. Import of `ctx`
   is asserted at startup; there is no fallback parser to silently stand in.
2. **No simulator.** Every model response comes from a live API call. Retries
   are bounded; a call that still fails marks the task `errored` and the task is
   excluded from the denominator *and reported*. Nothing is ever synthesized.
3. **One variable per comparison.** Arms come in matched families: identical
   model ladder, identical prompts, identical task list — only `triage` differs.
   Any pass-rate delta inside a family is attributable to the triage channel.
4. **Tokens are the primary currency.** USD is derived at report time from a
   price table stored in the results file, so a wrong price can be corrected
   without re-running, and never silently rewrites a pass rate.

Usage:
    python evals/tokenomics/runner.py --n 30 --arms cascade_raw cascade_llm cascade_sj
    python evals/tokenomics/runner.py --n 30 --arms all --out evals/tokenomics/results

Requires: GEMINI_API_KEY, `pip install -e .` (for ctx), and a sandbox
interpreter with the BigCodeBench library set installed (--sandbox-python).
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time

# --- Fail loudly if straitjacket is absent. The suite this mirrors wrapped this
# --- import in a bare except and silently measured a regex instead.
try:
    import ctx as _ctx  # noqa: F401
except ImportError as exc:  # pragma: no cover - startup guard
    raise SystemExit(
        "straitjacket is not importable; `pip install -e .` first. "
        "The sj arm measures the real ctx digest and has no fallback."
    ) from exc

HERE = pathlib.Path(__file__).resolve().parent
DATASET = HERE / "BigCodeBench-Hard-v0.1.4.jsonl"

FLASH = "gemini-3.6-flash"
LITE = "gemini-3.5-flash-lite"

# Published list prices are NOT verified by this harness. They are recorded into
# every results file so the report is a pure function of (counts, prices) and can
# be regenerated against a corrected table. Token counts are the real measurement.
PRICE_USD_PER_MTOK = {
    FLASH: {"input": 1.50, "output": 7.50},
    LITE: {"input": 0.30, "output": 2.50},
}
PRICE_PROVENANCE = (
    "Unverified: carried over from tokenomics-benchmark-multi-llms/src/config.py. "
    "Re-price with report.py --prices before quoting USD."
)

SOLVER_ROLE = (
    "You are an expert Python programmer. Complete the function below. You are given its imports, "
    "signature, and docstring; several real libraries must be used correctly. Output the COMPLETE "
    "solution: all needed imports and the full function definition, handling edge cases and the "
    "documented return/exception behavior exactly. Output ONLY one ```python code block, no "
    "explanation.\n\n"
)
REPAIR_ROLE = (
    "You are an expert Python programmer. A candidate solution to the problem below FAILED its "
    "unit tests. Analyze the test error output, find the bug, and fix the code. Output the "
    "COMPLETE corrected solution: all needed imports and the full function definition. Do not "
    "output a diff or a fragment. Output ONLY one ```python code block, no explanation.\n\n"
)
TRIAGE_ROLE = (
    "You are a test-failure triage tool. Compress the Python unittest stderr below into a SHORT "
    "digest (max 12 lines) preserving EXACTLY: (1) each failing test method name, (2) the "
    "exception type and message, (3) assertion diffs with expected vs actual values (truncate "
    "values longer than ~120 chars), and (4) traceback lines inside the candidate solution "
    '(file "prog.py") with their line numbers. DROP unittest boilerplate, separators, and '
    "library-internal frames. Copy identifiers and values VERBATIM -- never paraphrase numbers. "
    "Output plain text only, no code fences.\n\nStderr:\n"
)

_UNITTEST_RUNNER = (
    "\n\nimport unittest as _ut, sys as _sys\n"
    "_ut.TestCase.maxDiff = None\n"
    "_res = _ut.TextTestRunner(verbosity=0).run("
    "_ut.TestLoader().loadTestsFromTestCase(TestCases))\n"
    "_sys.exit(0 if _res.wasSuccessful() else 1)\n"
)

# Arms come in matched families. Within a family the ladder is byte-identical and
# only `triage` varies, so the delta isolates the triage channel.
ARMS = {
    # family: cascade (cheap draft, one escalation) -- mirrors their Arm 0
    "cascade_raw": {"ladder": [(LITE, None), (FLASH, "LOW")], "triage": "raw", "family": "cascade"},
    "cascade_llm": {"ladder": [(LITE, None), (FLASH, "LOW")], "triage": "llm", "family": "cascade"},
    "cascade_sj": {"ladder": [(LITE, None), (FLASH, "LOW")], "triage": "sj", "family": "cascade"},
    # family: smart_repair (reason first, cheap fix, reasoned escalation) -- their Arms 3/4
    "smart_raw": {
        "ladder": [(FLASH, "LOW"), (LITE, None), (FLASH, "MEDIUM")],
        "triage": "raw",
        "family": "smart_repair",
    },
    "smart_llm": {
        "ladder": [(FLASH, "LOW"), (LITE, None), (FLASH, "MEDIUM")],
        "triage": "llm",
        "family": "smart_repair",
    },
    "smart_sj": {
        "ladder": [(FLASH, "LOW"), (LITE, None), (FLASH, "MEDIUM")],
        "triage": "sj",
        "family": "smart_repair",
    },
}

_client = None


def client():
    global _client
    if _client is None:
        from google import genai

        _client = genai.Client()
    return _client


class ModelCallFailed(RuntimeError):
    """Raised after retries are exhausted. Never swallowed into a fake response."""


# Thinking tokens are billed and counted as output, so a fixed max_output_tokens
# silently truncates the visible answer once reasoning is on. Give each level its
# own headroom on top of the answer budget.
THINKING_HEADROOM = {"LOW": 4096, "MEDIUM": 8192, "HIGH": 16384}


def call_model(model_id: str, prompt: str, thinking: str | None, max_tokens: int = 4096) -> dict:
    """One live API call. Returns usage; raises ModelCallFailed rather than simulating."""
    from google.genai import types

    cfg_kw = {"max_output_tokens": max_tokens + THINKING_HEADROOM.get(thinking or "", 0)}
    if thinking:
        cfg_kw["thinking_config"] = types.ThinkingConfig(thinking_level=thinking)
    config = types.GenerateContentConfig(**cfg_kw)

    last = None
    for attempt in range(4):
        try:
            t0 = time.time()
            resp = client().models.generate_content(model=model_id, contents=prompt, config=config)
            dt = time.time() - t0
            m = resp.usage_metadata
            inp = m.prompt_token_count or 0
            out = (m.candidates_token_count or 0) + (m.thoughts_token_count or 0)
            finish = ""
            if resp.candidates:
                finish = str(getattr(resp.candidates[0], "finish_reason", "") or "")
            return {
                "provenance": "live",
                "model": model_id,
                "thinking": thinking,
                "text": resp.text or "",
                "input_tokens": inp,
                "output_tokens": out,
                # MAX_TOKENS here means the answer was cut off: a harness artifact,
                # not a model failure. Recorded so it is countable, never silent.
                "finish_reason": finish,
                "seconds": round(dt, 2),
            }
        except Exception as exc:  # noqa: BLE001 - retried, then surfaced
            last = exc
            time.sleep(2**attempt)
    raise ModelCallFailed(f"{model_id}: {last}")


def extract_code(text: str) -> str:
    """Pull the python block out of a response.

    A response truncated at max_output_tokens has an OPENING fence and no
    closing one. Matching only balanced fences makes that case fall through to
    the raw text, so the literal ```python line lands in prog.py and the task
    dies of SyntaxError -- a harness artifact scored as a model failure. Handle
    the unterminated block explicitly.
    """
    m = re.search(r"```(?:python)?\s*(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    m = re.search(r"```(?:python)?\s*\n(.*)", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return text.strip()


def _write_program(workdir: pathlib.Path, problem: dict, solution: str) -> pathlib.Path:
    path = workdir / "prog.py"
    path.write_text(solution + "\n\n" + problem["test"] + _UNITTEST_RUNNER, encoding="utf-8")
    return path


def evaluate(problem: dict, solution: str, sandbox_python: str, use_ctx: bool, workdir: pathlib.Path) -> dict:
    """Run the task's unittest suite. Returns pass/fail plus the failure channel.

    `use_ctx=True` executes through the real `ctx run` CLI and returns its digest.
    Pass/fail comes from the child's own exit code in both paths, so the arms are
    scored identically no matter which channel produced the failure text.

    `workdir` PERSISTS across the rungs of one task. That is load-bearing, not
    tidiness: straitjacket's store lives in the workspace, so a per-call temp dir
    would (a) delete the artifact the digest's retrieval addresses point at
    before anyone could resolve them, and (b) hide the fact that the repair
    attempt re-runs the same command, which is what arms the reflex arc's
    densify-on-re-run. A fresh dir per call measures the digest with its store
    amputated -- a false negative for the sj arm.
    """
    try:
        _write_program(workdir, problem, solution)
        env = {**os.environ, "MPLBACKEND": "Agg"}
        if use_ctx:
            r = subprocess.run(
                ["ctx", "run", "--", sandbox_python, "prog.py"],
                capture_output=True,
                text=True,
                timeout=120,
                cwd=workdir,
                env=env,
            )
            digest = (r.stdout or "") + (r.stderr or "")
            m = re.search(r"^exit:\s*(-?\d+)", digest, re.MULTILINE)
            if m is None:
                # ctx itself failed to produce a digest -- surface it, never guess.
                return {"passed": False, "channel": digest[-4000:], "digest_ok": False}
            return {"passed": m.group(1) == "0", "channel": digest.strip(), "digest_ok": True}

        r = subprocess.run(
            [sandbox_python, "prog.py"],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=workdir,
            env=env,
        )
        if r.returncode == 0:
            return {"passed": True, "channel": "", "digest_ok": True}
        return {"passed": False, "channel": (r.stderr.strip() or "test failed")[-4000:], "digest_ok": True}
    except subprocess.TimeoutExpired:
        return {"passed": False, "channel": "timeout: execution exceeded 120s", "digest_ok": True}


INFRA_RE = re.compile(r"ModuleNotFoundError|ImportError: cannot import name|No module named")


def solve_task(problem: dict, arm: dict, sandbox_python: str) -> dict:
    """Run one task through one arm's ladder. Every call is live or the task errors."""
    calls: list[dict] = []
    triage_calls: list[dict] = []
    use_ctx = arm["triage"] == "sj"
    prompt = SOLVER_ROLE + problem["complete_prompt"]
    code = ""
    outcome = None

    # One workspace for the whole repair loop -- see evaluate(). For the sj arm
    # this is what lets the store survive between attempts and lets ctx see the
    # repair as a re-run of the same command.
    workdir = pathlib.Path(tempfile.mkdtemp(prefix="bcb_"))
    if use_ctx:
        subprocess.run(["git", "init", "-q", "."], cwd=workdir, capture_output=True)
    try:
        return _run_ladder(problem, arm, sandbox_python, workdir, use_ctx, calls, triage_calls)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _run_ladder(problem, arm, sandbox_python, workdir, use_ctx, calls, triage_calls) -> dict:
    prompt = SOLVER_ROLE + problem["complete_prompt"]
    code = ""
    outcome = None

    for rung, (model_id, thinking) in enumerate(arm["ladder"]):
        try:
            call = call_model(model_id, prompt, thinking)
        except ModelCallFailed as exc:
            return {
                "status": "errored",
                "error": str(exc)[:300],
                "loops": rung,
                "calls": calls,
                "triage_calls": triage_calls,
            }
        calls.append({k: v for k, v in call.items() if k != "text"})
        code = extract_code(call["text"])

        outcome = evaluate(problem, code, sandbox_python, use_ctx, workdir)
        if outcome["passed"]:
            return {
                "status": "passed",
                "loops": rung,
                "calls": calls,
                "triage_calls": triage_calls,
                "channel_chars": 0,
            }

        if rung == len(arm["ladder"]) - 1:
            break

        # --- the measured variable: how the failure reaches the next model ---
        channel = outcome["channel"]
        if arm["triage"] == "llm":
            try:
                tri = call_model(LITE, TRIAGE_ROLE + "```\n" + channel + "\n```", None, max_tokens=768)
            except ModelCallFailed as exc:
                return {
                    "status": "errored",
                    "error": f"triage: {exc}"[:300],
                    "loops": rung,
                    "calls": calls,
                    "triage_calls": triage_calls,
                }
            triage_calls.append({k: v for k, v in tri.items() if k != "text"})
            channel = tri["text"].strip() or channel[-1200:]
        # raw: channel passes through untouched. sj: channel is already the ctx digest.

        prompt = (
            REPAIR_ROLE
            + problem["complete_prompt"]
            + "\n\nCurrent code:\n```python\n"
            + code
            + "\n```\n\nTest failure:\n"
            + channel
            + "\n"
        )

    status = "failed"
    if outcome and INFRA_RE.search(outcome["channel"]):
        status = "infra_error"
    return {
        "status": status,
        "loops": len(arm["ladder"]) - 1,
        "calls": calls,
        "triage_calls": triage_calls,
        "channel_chars": len(outcome["channel"]) if outcome else 0,
        "last_channel": (outcome["channel"][:600] if outcome else ""),
    }


def load_problems(n: int) -> list[dict]:
    rows = [json.loads(line) for line in DATASET.read_text().splitlines() if line.strip()]
    rows.sort(key=lambda r: int(r["task_id"].split("/")[1]))
    return rows[:n]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--arms", nargs="+", default=["cascade_raw", "cascade_llm", "cascade_sj"])
    ap.add_argument("--out", type=pathlib.Path, default=HERE / "results")
    ap.add_argument("--sandbox-python", default=sys.executable)
    ap.add_argument("--tag", default="")
    args = ap.parse_args()

    arm_names = list(ARMS) if args.arms == ["all"] else args.arms
    unknown = [a for a in arm_names if a not in ARMS]
    if unknown:
        raise SystemExit(f"unknown arms: {unknown}; choose from {list(ARMS)}")

    problems = load_problems(args.n)
    args.out.mkdir(parents=True, exist_ok=True)
    print(f"tasks: {len(problems)}  arms: {arm_names}  sandbox: {args.sandbox_python}", flush=True)

    for name in arm_names:
        arm = ARMS[name]
        records = []
        t_arm = time.time()
        for i, problem in enumerate(problems, 1):
            t0 = time.time()
            res = solve_task(problem, arm, args.sandbox_python)
            res["task_id"] = problem["task_id"]
            res["seconds"] = round(time.time() - t0, 1)
            records.append(res)
            print(
                f"  [{name}] {i}/{len(problems)} {problem['task_id']:22s} "
                f"{res['status']:11s} loops={res['loops']} {res['seconds']}s",
                flush=True,
            )

        payload = {
            "schema": "tokenomics.arm/v1",
            "arm": name,
            "family": arm["family"],
            "ladder": [{"model": m, "thinking": t} for m, t in arm["ladder"]],
            "triage": arm["triage"],
            "n": len(problems),
            "task_ids": [p["task_id"] for p in problems],
            "provenance": "live",
            "simulated": False,
            "prices_usd_per_mtok": PRICE_USD_PER_MTOK,
            "price_provenance": PRICE_PROVENANCE,
            "sandbox_python": args.sandbox_python,
            "wall_seconds": round(time.time() - t_arm, 1),
            "results": records,
        }
        path = args.out / f"{name}{args.tag}.json"
        path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        print(f"  -> {path}", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
