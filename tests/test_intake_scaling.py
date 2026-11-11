"""Intake never hands a redaction pattern an unbounded run.

Several redaction patterns are quadratic when a regex is given an unbounded
run: ``eyJ`` + 20 class characters + ``-``, repeated, gives a word-boundary
start every 24 characters, each scanning to the end of the run before it
fails. Intake therefore only ever runs a pattern over bounded windows around
what it displays.

These tests do not depend on wall-clock time or a quiet machine:

* every call into a redaction pattern during an intake is recorded (the
  length of text it was handed); the largest must not grow with the payload
  -- the quadratic commit (2f762d6) handed each pattern the WHOLE payload,
  so it fails this at once;
* process CPU time (not wall time) for two sizes of every pattern's
  adversarial shape must grow no worse than ~2.5x per doubling.
"""

from __future__ import annotations

import time

import pytest
from conftest import make_store, make_ws

from ctx import textutil
from ctx.intake import intake_bytes

PREFIXES = {
    "aws-access-key": "AKIA", "aws-secret-key": "aws_secret_access_key=",
    "private-key": "-----BEGIN RSA PRIVATE KEY-----", "github-token": "ghp_",
    "github-pat": "github_pat_", "gitlab-token": "glpat-", "slack-token": "xoxb-",
    "slack-hook": "https://hooks.slack.com/services/T", "stripe-key": "sk_live_",
    "twilio-key": "SK", "sendgrid-key": "SG.", "npm-token": "npm_",
    "pypi-token": "pypi-AgEIcHlwaS5vcmc", "huggingface-token": "hf_",
    "anthropic-key": "sk-ant-", "jwt": "eyJ", "google-api-key": "AIza",
    "generic-api-token": "sk-",
}
SEPARATORS = {"private-key": " "}


def _shape(prefix: str, sep: str, size: int) -> bytes:
    unit = prefix + "a" * 21 + sep
    return (unit * (size // len(unit) + 1))[:size].encode()


BOUND = 256 * 1024  # the most text any pattern may be handed in one call


class WindowTooLarge(AssertionError):
    pass


class _Recorder:
    """Wraps a compiled pattern; records the length of every text it scans
    and RAISES before the pattern runs when it exceeds :data:`BOUND`, so a
    regression fails at once instead of hanging in a quadratic scan."""

    def __init__(self, rx, sink):
        self._rx, self._sink = rx, sink

    def _note(self, text, pos=0, endpos=None):
        end = len(text) if endpos is None else min(endpos, len(text))
        size = max(0, end - pos)
        self._sink.append(size)
        if size > BOUND:
            raise WindowTooLarge(f"a redaction pattern was handed {size:,} characters")

    def finditer(self, text, *a, **k):
        self._note(text, *a, **k)
        return self._rx.finditer(text, *a, **k)

    def search(self, text, *a, **k):
        self._note(text, *a, **k)
        return self._rx.search(text, *a, **k)

    def match(self, text, *a, **k):
        self._note(text, *a, **k)
        return self._rx.match(text, *a, **k)

    def sub(self, repl, text, *a, **k):
        self._note(text)
        return self._rx.sub(repl, text, *a, **k)

    def __getattr__(self, name):
        return getattr(self._rx, name)


@pytest.fixture()
def env(state_home, workspace_dir):
    ws = make_ws(workspace_dir)
    return ws, make_store(ws)


@pytest.fixture(autouse=True)
def recorded(monkeypatch):
    sink: list[int] = []
    for name, rx in list(textutil.REDACTION_PATTERNS.items()):
        monkeypatch.setitem(textutil.REDACTION_PATTERNS, name, _Recorder(rx, sink))
    return sink


@pytest.mark.parametrize("size", [1 << 20, 8 << 20])
def test_no_pattern_is_handed_more_than_a_bounded_window(env, recorded, size):
    ws, store = env
    intake_bytes(store, ws, _shape("eyJ", "-", size), label="adv", record_delivery=False)
    assert recorded, "the intake path ran no redaction pattern at all"
    # Bounded by the context windows (a few tens of KiB), independent of size.
    assert max(recorded) <= BOUND, max(recorded)


def _cpu(store, ws, data: bytes, runs: int = 3) -> float:
    best = float("inf")
    for _ in range(runs):
        t = time.process_time()
        intake_bytes(store, ws, data, label="adv", record_delivery=False)
        best = min(best, time.process_time() - t)
    return best


@pytest.mark.parametrize("name", sorted(PREFIXES))
def test_cpu_time_scales_linearly_per_pattern(env, name):
    ws, store = env
    sep = SEPARATORS.get(name, "-")
    small = _cpu(store, ws, _shape(PREFIXES[name], sep, 512 * 1024))
    large = _cpu(store, ws, _shape(PREFIXES[name], sep, 1024 * 1024))
    assert large <= max(2.5 * small, 0.2), (name, small, large)


def test_thirty_two_mebibytes_of_the_worst_shape_finish(env):
    ws, store = env
    data = _shape("eyJ", "-", 32 * 1024 * 1024)
    t = time.process_time()
    rec = intake_bytes(store, ws, data, label="adv32", record_delivery=False)
    assert rec.profile == "chunkmap/v1"
    assert time.process_time() - t < 60   # ~5 s of CPU; was unbounded
