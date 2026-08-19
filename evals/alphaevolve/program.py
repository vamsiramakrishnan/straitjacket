"""Seed program for evolving generic-text evidence selection.

Only the marked block is mutable.  The production implementation remains in
``ctx.digest.text`` until a winning candidate passes the normal straitjacket
test and evaluation gates.
"""

from __future__ import annotations

import re
from collections.abc import Sequence


# EVOLVE-BLOCK-START

_SIGNAL_RE = re.compile(
    r"\b(error|failed|failure|exception|traceback|fatal|panic|denied|refused|"
    r"timeout|timed out|cannot|unable|warning|warn|passed|success)\b",
    re.IGNORECASE,
)


def select_evidence(lines: Sequence[str], budget: int) -> list[int]:
    """Return ordered, zero-based line indices worth showing in a digest."""
    if budget <= 0 or not lines:
        return []

    chosen: set[int] = {0, len(lines) - 1}
    ranked: list[tuple[int, int]] = []
    for index, line in enumerate(lines):
        score = 0
        if _SIGNAL_RE.search(line):
            score += 10
        if any(token in line for token in ("FAILED", "ERROR", "FATAL", "Traceback")):
            score += 5
        if line.lstrip().startswith(("E ", "Caused by:", "AssertionError")):
            score += 4
        ranked.append((-score, index))

    for _neg_score, index in sorted(ranked):
        if len(chosen) >= budget:
            break
        chosen.add(index)
    return sorted(chosen)[:budget]

# EVOLVE-BLOCK-END
