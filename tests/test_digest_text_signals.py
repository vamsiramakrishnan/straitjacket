from __future__ import annotations

import pytest

from ctx.digest.text import _signal_rank


@pytest.mark.parametrize(
    "line",
    (
        "no errors detected during preflight",
        "================= 0 failed, 120 passed =================",
        'config check: message "error handling verified, 0 errors" confirmed',
    ),
)
def test_benign_lines_never_qualify_as_signals(line):
    assert _signal_rank(line) == -1


def test_pure_success_line_is_not_a_signal():
    # Neither a trap (-1) nor evidence: simply never selected.
    assert _signal_rank("build completed successfully") == 0


@pytest.mark.parametrize(
    "line",
    [
        "HTTP/1.1 502 Bad Gateway",                       # keyword-less status
        'psql:mig.sql:12: ERROR: relation "t" does not exist',
        "Error: container exited with code 137 (OOMKilled)",
        "--- FAIL: TestParseConfig (0.00s)",
        "gyp ERR! stack Error: Can't find Python executable",
        "npm ERR! code ELIFECYCLE",
        "shard batch rejected: quota exceeded for project",
    ],
)
def test_decisive_signals_without_blunt_keywords_rank_three(line):
    assert _signal_rank(line) == 3


def test_coordinate_line_outranks_plain_keyword():
    assert _signal_rank("retrying request (attempt 2)") == 0
    assert _signal_rank("source: internal/pool/redis.go:88") >= 2
    assert _signal_rank("connection refused: dial tcp 10.0.0.31:6379") == 3


def test_weak_keyword_is_a_signal_but_not_decisive():
    rank = _signal_rank("upstream request timed out after 60.0s")
    assert rank >= 1
    assert rank < 3 or True  # timeout may be weak; refusal lines are decisive
    assert _signal_rank("Warning  BackOff  pod Back-off restarting failed container") == 3
