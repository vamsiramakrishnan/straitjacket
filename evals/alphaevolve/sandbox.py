"""Shared restricted runner for AlphaEvolve policy experiments."""

from __future__ import annotations

import ast
import math
import multiprocessing
import queue
import re
import time
from typing import Any

INVALID_SCORE = -1_000_000.0
_START = "# EVOLVE-BLOCK-START"
_END = "# EVOLVE-BLOCK-END"
_ALLOWED_IMPORTS = {
    "collections",
    "heapq",
    "math",
    "re",
    "statistics",
    "string",
    "typing",
}
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
    "filter": filter,
    "float": float,
    "int": int,
    "isinstance": isinstance,
    "len": len,
    "list": list,
    "map": map,
    "max": max,
    "min": min,
    "next": next,
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


def extract_block(code: str) -> str:
    if code.count(_START) != 1 or code.count(_END) != 1:
        raise ValueError("candidate must contain exactly one EVOLVE-BLOCK")
    start = code.index(_START) + len(_START)
    end = code.index(_END, start)
    return code[start:end]


def validate_block(block: str) -> None:
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


def _worker(
    block: str,
    function_name: str,
    calls: list[dict[str, Any]],
    output: multiprocessing.Queue[Any],
) -> None:
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
            "Any": Any,
            "Mapping": dict,
            "Sequence": list,
            "math": math,
            "re": re,
        }
        exec(compile(block, "<alphaevolve-candidate>", "exec"), namespace)
        function = namespace.get(function_name)
        if not callable(function):
            raise ValueError(f"candidate must define {function_name}(...)")

        results: list[Any] = []
        started = time.perf_counter()
        for call in calls:
            first = function(**call)
            second = function(**call)
            if first != second:
                raise ValueError("candidate is non-deterministic")
            results.append(first)
        output.put((results, (time.perf_counter() - started) * 1000.0, None))
    except BaseException as exc:
        output.put((None, None, f"{type(exc).__name__}: {exc}"))


def run_candidate(
    code: str,
    function_name: str,
    calls: list[dict[str, Any]],
    *,
    timeout_seconds: float = 3.0,
) -> dict[str, Any]:
    """Validate and run a pure candidate twice per call in a child process."""
    try:
        block = extract_block(code)
        validate_block(block)
    except (SyntaxError, ValueError) as exc:
        return {"outputs": None, "elapsed_ms": None, "error": str(exc)}

    context = multiprocessing.get_context("spawn")
    output: multiprocessing.Queue[Any] = context.Queue(maxsize=1)
    process = context.Process(
        target=_worker,
        args=(block, function_name, calls, output),
    )
    process.start()
    process.join(timeout_seconds)
    if process.is_alive():
        process.terminate()
        process.join(1.0)
        return {"outputs": None, "elapsed_ms": None, "error": "candidate timed out"}
    try:
        outputs, elapsed_ms, error = output.get(timeout=1.0)
    except queue.Empty:
        return {
            "outputs": None,
            "elapsed_ms": None,
            "error": f"candidate process exited {process.exitcode} without a result",
        }
    return {"outputs": outputs, "elapsed_ms": elapsed_ms, "error": error}


def controller_evaluation(metric: str, score: float, detail: str) -> dict[str, Any]:
    return {
        "scores": {"scores": [{"metric": metric, "score": float(score)}]},
        "insights": {"insights": [{"label": "Evaluator", "text": detail}]},
    }


def candidate_code(program_candidate: dict[str, Any]) -> str:
    return str(program_candidate["content"]["files"][0]["content"])
