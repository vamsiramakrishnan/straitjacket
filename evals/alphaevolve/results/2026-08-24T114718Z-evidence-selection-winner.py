"""Seed program for evolving generic-text evidence selection.

Only the marked block is mutable.  The production implementation remains in
``ctx.digest.text`` until a winning candidate passes the normal straitjacket
test and evaluation gates.
"""

from __future__ import annotations

import re
from collections.abc import Sequence


# EVOLVE-BLOCK-START

import re
from typing import Sequence

_COORDINATE_RES = [
    re.compile(r'\bfile "[^"]+", line \d+', re.IGNORECASE),
    re.compile(r'\b(?:[a-zA-Z]:[\\/])?[\w\.\-/\\]+\.\w+:\d+(?::\d+)?\b'),
    re.compile(r'\bat\s+[\w\.\$]+\([\w\.]+\.java:\d+\)', re.IGNORECASE),
    re.compile(r'\b(?:[a-zA-Z]:[\\/])?[\w\.\-/\\]+\.\w+\(\d+(?:,\d+)?\)', re.IGNORECASE),
]

_BENIGN_RE = re.compile(
    r'\b(no errors?|0 errors?|zero errors?|no failures?|0 failures?|0 failed|no exceptions?|warnings?: 0|errors?: 0|failures?: 0|0 warnings?|no warnings?)\b',
    re.IGNORECASE
)

_REAL_FAILURES_RE = re.compile(
    r'\b(?:failures?|errors?|failed|failed tests?)\s*[:=]\s*[1-9]\d*\b|\b[1-9]\d*\s+(?:failed|failures?|errors?)\b',
    re.IGNORECASE
)

_STRONG_ERR_RE = re.compile(r'\b(FAIL|FAILED|ERROR|FATAL|CRITICAL|Traceback|Exception|AssertionError)\b')
_WEAK_ERR_RE = re.compile(r'\b(failed|error|fatal|traceback|exception|assertionerror|panic|denied|refused|timeout|timed out|cannot|unable|unauthorized|forbidden|invalid|rejected|failure|unsuccessful|erroneous|corrupted|missing|not\s*found|disallowed|blocked)\b', re.IGNORECASE)
_WARN_RE = re.compile(r'\b(warning|warn|critical|emergency|alert)\b', re.IGNORECASE)
_SUCCESS_RE = re.compile(r'\b(passed|success|successful|successfully)\b', re.IGNORECASE)

_SUMMARY_RE = re.compile(r'(?:(=+|-+)\s*\d+\s+failed|\bfailures=\d+|\b(?:tests run|tests|suites|specs|cases)\s*[:=]\s*\d+|\b\d+\s+of\s+\d+\s+(?:tests|specs|cases|assertions)\s+failed)\b', re.IGNORECASE)
_TESTS_RUN_RE = re.compile(r'\b(tests run:|results:)\b', re.IGNORECASE)

_REQ_ID_RE = re.compile(r'\b(request_?id|req_?id|correlation_?id|trace_?id|transaction_?id|session_?id|job_?id|task_?id|build_?id|run_?id)\b', re.IGNORECASE)
_UUID_RE = re.compile(r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b', re.IGNORECASE)

# Additional error context and class patterns
_EXCEPTION_CLASS_RE = re.compile(r'\b[A-Z]\w*(?:Error|Exception|Panic|Failure)\b')
_CPP_EXCEPTION_RE = re.compile(r'\b(?:std|boost)::\w*(?:error|exception)\b', re.IGNORECASE)
_HTTP_ERR_RE = re.compile(r'\bHTTP/\d\.\d[^\d]+([45]\d\d)\b')
_STATUS_CODE_RE = re.compile(r'\bstatus(?:_code)?\s*[:=]\s*([45]\d\d)\b', re.IGNORECASE)
_EXIT_CODE_RE = re.compile(r'\b(?:exit\s+code|exit\s+status|exited\s+(?:with\s+)?code|exited\s+(?:with\s+)?status|returned\s+status|returned)\s*[:=]?\s*([1-9]\d*)\b', re.IGNORECASE)
_CMD_RE = re.compile(r'^\s*(?:\$\s+|\+\s+|>\s+|run:|executing:)\s*\w+', re.IGNORECASE)
_CHAIN_EXC_RE = re.compile(r'\b(?:During handling|The above exception|Caused by|caused\s+by|wrapped\s+by)\b', re.IGNORECASE)
_PREFIX_ERR_RE = re.compile(r'^\s*(?:npm\s+ERR!|stderr:|\[\s*err(?:or)?\s*\]|\[?stderr\]?|\[?error\]?:?)', re.IGNORECASE)
_CRASH_RE = re.compile(r'\b(segmentation fault|segfault|core dumped|killed|aborted|sigsegv|sigabrt|nullpointerexception|outofmemory|oom|panicked)\b', re.IGNORECASE)


def select_evidence(lines: Sequence[str], budget: int) -> list[int]:
    """Return ordered, zero-based line indices worth showing in a digest."""
    if budget <= 0 or not lines:
        return []

    total = len(lines)
    if total <= budget:
        return list(range(total))

    raw_scores = []
    line_weights = []
    for index, line in enumerate(lines):
        score = 0.0
        
        # Check coordinates
        if _COORDINATE_RES[0].search(line):
            score += 15.0
        elif _COORDINATE_RES[1].search(line):
            score += 12.0
        elif _COORDINATE_RES[2].search(line):
            score += 12.0
        elif _COORDINATE_RES[3].search(line):
            score += 12.0

        # Check if benign (e.g., "0 errors")
        is_benign = bool(_BENIGN_RE.search(line))
        if is_benign and _REAL_FAILURES_RE.search(line):
            is_benign = False

        if not is_benign:
            if _STRONG_ERR_RE.search(line):
                score += 15.0
            elif _WEAK_ERR_RE.search(line):
                score += 10.0
            
            if _EXCEPTION_CLASS_RE.search(line):
                score += 12.0

            if _CPP_EXCEPTION_RE.search(line):
                score += 12.0

            if _PREFIX_ERR_RE.search(line):
                score += 10.0

            if _CHAIN_EXC_RE.search(line):
                score += 10.0

            if _HTTP_ERR_RE.search(line) or _STATUS_CODE_RE.search(line):
                score += 10.0

            if _EXIT_CODE_RE.search(line):
                score += 12.0

            if _WARN_RE.search(line):
                score += 4.0

            if _CRASH_RE.search(line):
                score += 15.0

        if _SUCCESS_RE.search(line):
            score += 5.0

        if _SUMMARY_RE.search(line) or "===+" in line or "---+ " in line:
            score += 10.0
        if _TESTS_RUN_RE.search(line):
            score += 8.0

        if _REQ_ID_RE.search(line):
            score += 8.0
        elif _UUID_RE.search(line):
            score += 6.0

        if _CMD_RE.search(line):
            score += 8.0

        # Prefix start matches
        stripped = line.lstrip()
        if stripped.startswith(("E ", "Caused by:", "AssertionError", "FAIL:", "ERROR:", "WARNING:", "PANIC:")):
            score += 6.0

        # Position scoring for context
        if index == 0:
            score += 12.0
        elif index == total - 1:
            score += 12.0
        elif index < 3:
            score += 8.0 - index
        elif index >= total - 3:
            score += 8.0 - (total - 1 - index)

        # Compute line weight based on how meaningful the line content is
        stripped_full = line.strip()
        if not stripped_full:
            weight = 0.1
        elif not any(c.isalnum() for c in stripped_full):
            weight = 0.1
        elif len(stripped_full) <= 3 and sum(c.isalnum() for c in stripped_full) <= 1:
            weight = 0.2
        else:
            weight = 1.0

        raw_scores.append(score * weight)
        line_weights.append(weight)

    # Local score propagation/convolution (PageRank / diffusion style)
    final_ranked = []
    for i in range(total):
        s = raw_scores[i]
        
        # Propagate from immediate neighbors (1-hop)
        left1 = raw_scores[i-1] if i > 0 else 0.0
        right1 = raw_scores[i+1] if i < total - 1 else 0.0
        
        # Propagate from 2-hop neighbors
        left2 = raw_scores[i-2] if i > 1 else 0.0
        right2 = raw_scores[i+2] if i < total - 2 else 0.0
        
        prop = 0.5 * max(left1, right1) + 0.25 * max(left2, right2)
        s_final = s + prop * line_weights[i]
        
        final_ranked.append((s_final, i))

    # Sort by score descending, then index ascending for determinism
    final_ranked.sort(key=lambda x: (-x[0], x[1]))

    chosen_indices = [idx for _, idx in final_ranked[:budget]]
    return sorted(chosen_indices)

# EVOLVE-BLOCK-END
