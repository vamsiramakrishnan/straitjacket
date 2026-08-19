"""Deterministic, bounded evaluator for the straitjacket AlphaEvolve seed.

Candidates are syntax-checked, restricted to a small pure-Python surface, and
run in a child process with a timeout.  This is containment, not an operating-
system security boundary; run the controller in a disposable environment.
"""

from __future__ import annotations

import ast
import math
import multiprocessing
import queue
import re
import time
from pathlib import Path
from typing import Any

METRIC_NAME = "evidence_utility"
INVALID_SCORE = -1_000_000.0
TIMEOUT_SECONDS = 3.0
_START = "# EVOLVE-BLOCK-START"
_END = "# EVOLVE-BLOCK-END"
_PROGRAM_PATH = Path(__file__).with_name("program.py")
INITIAL_PROGRAM_CODE = _PROGRAM_PATH.read_text(encoding="utf-8")

# Frozen, model-free cases.  Values are zero-based line indices mapped to
# relevance grades: 3=decisive, 2=diagnostic, 1=context/anchor.
_CASES: tuple[dict[str, Any], ...] = (
    {
        "name": "pytest_failure",
        "budget": 4,
        "lines": [
            "================ test session starts ================",
            "collected 42 items",
            "tests/test_api.py ........F",
            "E   AssertionError: expected 201, got 500",
            "tests/test_api.py:81: AssertionError",
            "================ 1 failed, 41 passed ================",
        ],
        "relevance": {0: 1, 3: 3, 4: 2, 5: 3},
    },
    {
        "name": "compiler_root_cause",
        "budget": 4,
        "lines": [
            "Compiling ctx-harness v0.32.0",
            "warning: unused variable: candidate",
            "error[E0382]: borrow of moved value: candidate",
            "  --> src/main.rs:37:18",
            "note: move occurs because Candidate is not Copy",
            "error: aborting due to 1 previous error",
        ],
        "relevance": {0: 1, 2: 3, 3: 2, 4: 2, 5: 3},
    },
    {
        "name": "permission_chain",
        "budget": 3,
        "lines": [
            "Deploying service ctx-proxy",
            "Uploading source archive",
            "operation pending",
            "PERMISSION_DENIED: iam.serviceAccounts.actAs denied",
            "principal: ci@example.invalid",
            "deployment failed",
        ],
        "relevance": {0: 1, 3: 3, 4: 2, 5: 3},
    },
    {
        "name": "success_summary",
        "budget": 3,
        "lines": [
            "Running 1625 tests",
            "........................................",
            "slowest: test_index_large 1.42s",
            "1625 passed in 28.91s",
            "BUILD SUCCESS",
        ],
        "relevance": {0: 1, 2: 1, 3: 3, 4: 3},
    },
    {
        "name": "traceback",
        "budget": 5,
        "lines": [
            "starting worker",
            "Traceback (most recent call last):",
            "  File \"worker.py\", line 18, in run",
            "    return parse(payload)",
            "  File \"parser.py\", line 7, in parse",
            "ValueError: malformed manifest at field 'version'",
            "worker exited with status 1",
        ],
        "relevance": {0: 1, 1: 3, 2: 2, 4: 2, 5: 3, 6: 1},
    },
    {
        "name": "timeout_with_noise",
        "budget": 4,
        "lines": [
            "integration run started",
            "no errors detected during preflight",
            "poll 1: pending",
            "poll 2: pending",
            "upstream request timed out after 60.0s",
            "retry budget exhausted",
            "run failed",
        ],
        "relevance": {0: 1, 4: 3, 5: 2, 6: 3},
    },
    {
        "name": "structured_cli_result",
        "budget": 4,
        "lines": [
            "ctx doctor",
            "[ok] configuration parsed",
            "[ok] artifact store writable",
            "[warn] codex executable version is older than tested",
            "next: upgrade codex and rerun ctx doctor",
            "doctor completed with warnings",
        ],
        "relevance": {0: 1, 3: 3, 4: 3, 5: 2},
    },
    {
        "name": "http_failure",
        "budget": 4,
        "lines": [
            "POST /v1/messages",
            "connect 4.2ms",
            "HTTP/1.1 502 Bad Gateway",
            "x-request-id: req-7f91",
            "upstream connection refused",
            "request failed after 2 attempts",
        ],
        "relevance": {0: 1, 2: 3, 3: 2, 4: 3, 5: 2},
    },
)

_BLOCKED_NODES = (
    ast.AsyncFunctionDef,
    ast.Await,
    ast.ClassDef,
    ast.Delete,
    ast.Global,
    ast.Nonlocal,
    ast.Raise,
    ast.Try,
    ast.With,
    ast.Yield,
    ast.YieldFrom,
)
_ALLOWED_IMPORTS = {
    "collections",
    "heapq",
    "math",
    "re",
    "statistics",
    "string",
    "typing",
}
_BLOCKED_NAMES = {
    "__builtins__",
    "breakpoint",
    "compile",
    "delattr",
    "eval",
    "exec",
    "getattr",
    "globals",
    "help",
    "input",
    "locals",
    "open",
    "setattr",
    "vars",
}
_SAFE_BUILTINS = {
    "abs": abs,
    "all": all,
    "any": any,
    "bool": bool,
    "dict": dict,
    "enumerate": enumerate,
    "float": float,
    "int": int,
    "len": len,
    "list": list,
    "max": max,
    "min": min,
    "range": range,
    "reversed": reversed,
    "round": round,
    "set": set,
    "sorted": sorted,
    "str": str,
    "sum": sum,
    "tuple": tuple,
    "zip": zip,
}


def _extract_block(code: str) -> str:
    if code.count(_START) != 1 or code.count(_END) != 1:
        raise ValueError("candidate must contain exactly one EVOLVE-BLOCK")
    start = code.index(_START) + len(_START)
    end = code.index(_END, start)
    return code[start:end]


def _validate_block(block: str) -> None:
    tree = ast.parse(block)
    for node in ast.walk(tree):
        if isinstance(node, _BLOCKED_NODES):
            raise ValueError(f"blocked syntax: {type(node).__name__}")
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in _ALLOWED_IMPORTS:
                    raise ValueError(f"blocked import: {alias.name}")
        if isinstance(node, ast.ImportFrom):
            if node.level or node.module not in _ALLOWED_IMPORTS:
                raise ValueError(f"blocked import: {node.module or '<relative>'}")
        if isinstance(node, ast.Name) and node.id in _BLOCKED_NAMES:
            raise ValueError(f"blocked name: {node.id}")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ValueError(f"private attribute access is blocked: {node.attr}")


def _worker(block: str, output: multiprocessing.Queue[Any]) -> None:
    try:
        real_import = __import__

        def safe_import(
            name: str,
            globals: dict[str, Any] | None = None,
            locals: dict[str, Any] | None = None,
            fromlist: tuple[str, ...] = (),
            level: int = 0,
        ) -> Any:
            if level or name not in _ALLOWED_IMPORTS:
                raise ImportError(f"import is not allowlisted: {name}")
            return real_import(name, globals, locals, fromlist, level)

        namespace: dict[str, Any] = {
            "__builtins__": {**_SAFE_BUILTINS, "__import__": safe_import},
            "re": re,
            "Sequence": list,
        }
        exec(compile(block, "<alphaevolve-candidate>", "exec"), namespace)
        select = namespace.get("select_evidence")
        if not callable(select):
            raise ValueError("candidate must define select_evidence(lines, budget)")

        selections: list[list[int]] = []
        started = time.perf_counter()
        for case in _CASES:
            first = select(tuple(case["lines"]), case["budget"])
            second = select(tuple(case["lines"]), case["budget"])
            if first != second:
                raise ValueError(f"non-deterministic result for {case['name']}")
            if not isinstance(first, list) or any(type(i) is not int for i in first):
                raise ValueError(f"{case['name']}: result must be list[int]")
            if first != sorted(set(first)):
                raise ValueError(f"{case['name']}: indices must be unique and ordered")
            if len(first) > case["budget"]:
                raise ValueError(f"{case['name']}: selection exceeds budget")
            if any(i < 0 or i >= len(case["lines"]) for i in first):
                raise ValueError(f"{case['name']}: index outside input")
            selections.append(first)
        output.put((selections, (time.perf_counter() - started) * 1000.0, None))
    except BaseException as exc:  # child must always report a bounded failure
        output.put((None, None, f"{type(exc).__name__}: {exc}"))


def score_candidate(code: str, timeout_seconds: float = TIMEOUT_SECONDS) -> dict[str, Any]:
    """Return a local score document suitable for tests and preflight."""
    try:
        block = _extract_block(code)
        _validate_block(block)
    except (SyntaxError, ValueError) as exc:
        return {"score": INVALID_SCORE, "error": str(exc), "cases": {}}

    context = multiprocessing.get_context("spawn")
    output: multiprocessing.Queue[Any] = context.Queue(maxsize=1)
    process = context.Process(target=_worker, args=(block, output))
    process.start()
    process.join(timeout_seconds)
    if process.is_alive():
        process.terminate()
        process.join(1.0)
        return {"score": INVALID_SCORE, "error": "candidate timed out", "cases": {}}
    try:
        selections, elapsed_ms, error = output.get(timeout=1.0)
    except queue.Empty:
        return {
            "score": INVALID_SCORE,
            "error": f"candidate process exited {process.exitcode} without a result",
            "cases": {},
        }
    if error:
        return {"score": INVALID_SCORE, "error": error, "cases": {}}

    case_scores: dict[str, float] = {}
    for case, selected in zip(_CASES, selections, strict=True):
        relevance = case["relevance"]
        earned = sum(relevance.get(index, 0) for index in selected)
        possible = sum(sorted(relevance.values(), reverse=True)[: case["budget"]])
        case_scores[case["name"]] = earned / possible
    score = 100.0 * sum(case_scores.values()) / len(case_scores)
    if not math.isfinite(score):
        return {"score": INVALID_SCORE, "error": "non-finite score", "cases": {}}
    return {
        "score": score,
        "error": None,
        "cases": case_scores,
        "elapsed_ms": elapsed_ms,
    }


def alphaevolve_evaluation_function(program_candidate: dict[str, Any]) -> dict[str, Any]:
    """Adapt the local evaluator to AlphaEvolve's controller protocol."""
    try:
        code = program_candidate["content"]["files"][0]["content"]
        result = score_candidate(code)
    except (KeyError, IndexError, TypeError) as exc:
        result = {"score": INVALID_SCORE, "error": f"invalid candidate envelope: {exc}"}

    evaluation: dict[str, Any] = {
        "scores": {"scores": [{"metric": METRIC_NAME, "score": result["score"]}]}
    }
    insights = [
        {
            "label": "Evaluator",
            "text": result.get("error")
            or f"Passed all hard gates; mean utility {result['score']:.3f}",
        }
    ]
    evaluation["insights"] = {"insights": insights}
    return evaluation


if __name__ == "__main__":
    print(score_candidate(INITIAL_PROGRAM_CODE))
