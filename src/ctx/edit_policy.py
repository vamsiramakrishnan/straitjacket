"""Conservative edit-format selection from paired, independently checked runs."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from ctx.store import canonical_json

FORMATS = {"native", "anchored", "structural"}


def _upper_rate(k: int, n: int) -> float:
    # One-sided 95% Wilson upper bound; cases, not repeated runs, are units.
    z = 1.6448536269514722
    p = k / n
    return (p + z*z/(2*n) + z*math.sqrt(p*(1-p)/n + z*z/(4*n*n))) / (1+z*z/n)


def choose_format(rows, *, model: str, shape: str, min_cases: int = 60, max_regression: float = 0.05):
    selected = [r for r in rows if r.get("model") == model and r.get("shape") == shape]
    result = {"schema": "ctx.edit-policy/v1", "model": model, "shape": shape,
              "format": "native", "reason": "insufficient_paired_evidence",
              "evidenceSha256": hashlib.sha256(canonical_json(selected)).hexdigest(),
              "candidates": []}
    if not selected or any(r.get("measurement") != "live" for r in selected):
        result["reason"] = "live_evidence_required"
        return result
    arms = {}
    for row in selected:
        arm = row.get("format")
        if arm not in FORMATS:
            continue
        key = (row.get("case"), row.get("repeat", 0))
        if (not isinstance(key[0], str) or type(key[1]) is not int or key in arms.setdefault(arm, {})
                or type(row.get("task_success")) is not bool or type(row.get("wrong_target")) is not bool):
            result["reason"] = "invalid_or_duplicate_observations"
            return result
        arms[arm][key] = row
    native = arms.get("native", {})
    winners = []
    for arm, observations in sorted(arms.items()):
        if arm == "native":
            continue
        if set(observations) != set(native) or not native:
            result["candidates"].append({"format": arm, "reason": "unpaired_cases"})
            continue
        cases = {key[0] for key in native}
        if len(cases) < min_cases:
            continue
        if any(r["wrong_target"] for r in observations.values()):
            result["candidates"].append({"format": arm, "reason": "wrong_target_observed"})
            continue
        lost = {key[0] for key, base in native.items()
                if base["task_success"] and not observations[key]["task_success"]}
        upper = _upper_rate(len(lost), len(cases))
        costs = [r.get("cost_usd") for r in [*native.values(), *observations.values()]]
        if any(type(c) not in (float, int) or not math.isfinite(c) or c < 0 for c in costs):
            result["candidates"].append({"format": arm, "reason": "cost_unmeasured"})
            continue
        base_cost = sum(r["cost_usd"] for r in native.values())
        arm_cost = sum(r["cost_usd"] for r in observations.values())
        base_success = sum(r["task_success"] for r in native.values())
        arm_success = sum(r["task_success"] for r in observations.values())
        eligible = upper <= max_regression and arm_success > 0 and arm_success >= base_success and arm_cost < base_cost
        cell = {"format": arm, "cases": len(cases), "pairedRuns": len(native),
                "regressionUpper95": upper, "cost_usd": arm_cost,
                "reason": "eligible" if eligible else "quality_or_cost_gate"}
        result["candidates"].append(cell)
        if eligible:
            winners.append((arm_cost, arm))
    if winners:
        result["format"] = min(winners)[1]
        result["reason"] = "paired_quality_and_cost_gate"
    return result


def load_rows(path: Path):
    with path.open("rb") as stream:
        data = stream.read(4 * 1024 * 1024 + 1)
    if len(data) > 4 * 1024 * 1024:
        raise ValueError("edit evaluation exceeds 4 MiB")
    rows = [json.loads(line) for line in data.splitlines() if line.strip()]
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError("edit evaluation rows must be objects")
    return rows


def format_hint(decision):
    fmt = decision["format"]
    if fmt == "anchored":
        action = "Prefer ctx edit replace with observed anchored spans; verify behavioral outcomes."
    elif fmt == "structural":
        action = "Prefer ctx edit expand for repeated changes after a verified example."
    else:
        action = "Use the host's native edit format."
    return (f"Edit-format policy for this attempt: {fmt}; {decision['reason']}. "
            f"Evidence {decision['evidenceSha256']}. {action} "
            "This is format advice, not authorization to edit additional files.")
