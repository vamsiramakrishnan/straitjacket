"""Paired local benchmark: full verified setup versus receipt-backed repeat."""

from __future__ import annotations

import contextlib
import io
import json
import statistics
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from ctx.setup_telemetry import setup_fingerprint
from ctx.workspace import resolve_workspace
from ctx.wrap import guided_setup


def _timed(ws, *, repair: bool) -> tuple[float, int, int]:
    output = io.StringIO()
    started = time.perf_counter()
    with contextlib.redirect_stdout(output):
        code = guided_setup(ws, hosts=["codex"], force_repair=repair)
    elapsed_ms = (time.perf_counter() - started) * 1000
    return elapsed_ms, len(output.getvalue().encode()), code


def run_benchmark(repeats: int = 11) -> dict[str, Any]:
    full_ms: list[float] = []
    noop_ms: list[float] = []
    full_bytes: list[int] = []
    noop_bytes: list[int] = []
    unchanged = 0

    for _ in range(repeats):
        with tempfile.TemporaryDirectory(prefix="ctx-setup-bench-") as temp:
            root = Path(temp)
            subprocess.run(
                ["git", "init", "-q", "."], cwd=root, check=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            ws = resolve_workspace(str(root))
            baseline = _timed(ws, repair=True)
            before = setup_fingerprint(root)
            candidate = _timed(ws, repair=False)
            after = setup_fingerprint(root)
            full_ms.append(baseline[0])
            full_bytes.append(baseline[1])
            noop_ms.append(candidate[0])
            noop_bytes.append(candidate[1])
            unchanged += int(before == after)
            if baseline[2] or candidate[2]:
                raise RuntimeError(
                    f"setup failed: baseline={baseline[2]} candidate={candidate[2]}"
                )

    baseline_ms = statistics.median(full_ms)
    candidate_ms = statistics.median(noop_ms)
    baseline_bytes = statistics.median(full_bytes)
    candidate_bytes = statistics.median(noop_bytes)
    return {
        "schema": "ctx.setup-benchmark/v1",
        "repeats": repeats,
        "successes": {"full": repeats, "ready_noop": repeats},
        "median": {
            "full_ms": round(baseline_ms, 3),
            "ready_noop_ms": round(candidate_ms, 3),
            "full_visible_bytes": int(baseline_bytes),
            "ready_noop_visible_bytes": int(candidate_bytes),
        },
        "improvement": {
            "latency_multiplier": round(baseline_ms / candidate_ms, 2),
            "latency_reduction_pct": round((1 - candidate_ms / baseline_ms) * 100, 2),
            "visible_bytes_multiplier": round(baseline_bytes / candidate_bytes, 2),
            "visible_bytes_reduction_pct": round(
                (1 - candidate_bytes / baseline_bytes) * 100, 2
            ),
            "repeat_config_rewrites": 0 if unchanged == repeats else None,
        },
        "fingerprint_unchanged": f"{unchanged}/{repeats}",
    }


if __name__ == "__main__":
    print(json.dumps(run_benchmark(), indent=2, sort_keys=True))
