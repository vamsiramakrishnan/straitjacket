"""Properties the three intake shape profiles share: registry order, intake-only
gating (command-output digests stay byte-identical), linear time."""

from __future__ import annotations

import time

import pytest
from conftest import make_store, make_ws
from shape_helpers import (
    csv_text, go_trace, java_trace, js_trace, padded, python_trace, rust_trace, unified_diff,
)

from ctx import digest
from ctx.digest.base import INTAKE_TOOL, DigestContext
from ctx.digest.delimprof import DelimitedDataProfile
from ctx.digest.traceprof import StackTraceProfile
from ctx.digest.udiffprof import UnifiedDiffProfile
from ctx.intake import intake_bytes

MINE = (StackTraceProfile, DelimitedDataProfile, UnifiedDiffProfile)


@pytest.fixture()
def env(state_home, workspace_dir):
    ws = make_ws(workspace_dir)
    return ws, make_store(ws)


def payloads() -> dict[str, str]:
    return {
        "python": padded(python_trace(recursion=120)), "js": js_trace(), "java": java_trace(),
        "go": go_trace(), "rust": rust_trace(), "csv": csv_text(600), "tsv": csv_text(600, "\t"),
        "semicolon": csv_text(600, ";"), "udiff": unified_diff(12),
        "udiff_in_log": "2024 INFO x\n" * 200 + unified_diff(4) + "2024 INFO y\n" * 200,
    }


# ------------------------------------------------------------------ registry
def test_registry_order_and_why():
    names = [type(p).__name__ for p in digest._PROFILES]
    at = names.index
    # Plain diffs and traces: after every strictly anchored command/test-runner
    # profile and the git-diff profile, before the loose shape profiles
    # (lint/search/build) that could otherwise claim their file:line shapes.
    assert at("GitDiffProfile") < at("UnifiedDiffProfile") < at("StackTraceProfile") < at("LintProfile")
    # Delimited data: after JSON / JSON-lines (a JSON-lines paste has a
    # consistent comma count) and before the aligned-table and log profiles,
    # which claim CSVs with numeric columns and level words.
    assert at("JsonProfile") < at("DelimitedDataProfile") < at("TableProfile") < at("LogTemplateProfile")
    # The generic fallbacks still catch everything these decline.
    assert at("DelimitedDataProfile") < at("ChunkMapProfile") < at("TextProfile") == len(names) - 1
    assert at("BinaryProfile") == 0


def test_every_new_profile_has_a_version_a_detect_and_a_render():
    versions = []
    for cls in MINE:
        p = cls()
        assert isinstance(p.version, str) and p.version.endswith("/v1")
        assert cls.detect is not digest.Profile.detect and cls.render is not digest.Profile.render
        versions.append(p.version)
    assert len(set(versions)) == 3 and not set(versions) & {q.version for q in digest._PROFILES if type(q) not in MINE}


# --------------------------------------------------------- intake-only gating
def _ctx(env, text: str, argv):
    ws, store = env
    manifest = digest.synthesize_manifest(store, ws, argv[0], text, "", argv=argv)
    return DigestContext.load(store, ws, manifest, focus="")


@pytest.mark.parametrize("name", sorted(payloads()))
def test_profiles_decline_everything_that_is_not_an_intake(env, name):
    text = payloads()[name]
    intake_claims = [type(p).__name__ for p in (c() for c in MINE)
                     if p.detect(_ctx(env, text, [INTAKE_TOOL, "label:paste"]))]
    if name.endswith("_in_log"):
        assert not intake_claims, "a diff inside a long log is the log's, not a diff paste"
        return
    assert intake_claims, f"{name} should be claimed under intake"
    for argv in (["Bash"], ["cat", "x"], ["git", "diff"], ["python", "x.py"], ["ctx:intake-lookalike"]):
        ctx = _ctx(env, text, argv)
        assert not any(c().detect(ctx) for c in MINE), argv
    # a command that merely carries the intake name, but is not one
    ws, store = env
    manifest = digest.synthesize_manifest(store, ws, INTAKE_TOOL, text, "", argv=[INTAKE_TOOL])
    for mutated in ({**manifest, "shell": True}, {**manifest, "source": {"gitHead": "abc", "worktreeHash": None}}):
        ctx = DigestContext.load(store, ws, mutated, focus="")
        assert not any(c().detect(ctx) for c in MINE)


@pytest.mark.parametrize("name", sorted(payloads()))
def test_command_output_digests_are_byte_identical_with_and_without_the_profiles(env, name, monkeypatch):
    """The published receipts pin command-output digests: shape-matching
    payloads that arrive as tool results must digest exactly as they did
    before these profiles existed."""
    ws, store = env
    text = payloads()[name]
    with_mine = {}
    for argv in (None, ["cat", "x"], ["git", "diff"], ["grep", "-rn", "x"]):
        tool = "Bash" if argv is None else argv[0]
        with_mine[str(argv)] = digest.digest_output(store, ws, tool, text, "", argv=argv)
    monkeypatch.setattr(digest, "_PROFILES", tuple(p for p in digest._PROFILES if type(p) not in MINE))
    for argv in (None, ["cat", "x"], ["git", "diff"], ["grep", "-rn", "x"]):
        tool = "Bash" if argv is None else argv[0]
        assert digest.digest_output(store, ws, tool, text, "", argv=argv) == with_mine[str(argv)], (name, argv)


# --------------------------------------------------------------- linear time
SLOW = 20.0   # seconds; the quadratic patterns this guards against took minutes


def _timed(env, data: bytes) -> float:
    ws, store = env
    t = time.perf_counter()
    r = intake_bytes(store, ws, data, label="paste")
    elapsed = time.perf_counter() - t
    assert len(r.digest.encode()) <= ws.config.budgets.digest_tokens * 4 or r.passthrough
    return elapsed


@pytest.mark.parametrize("name,make", [
    ("one long line of prose", lambda: b"word " * 400_000),
    ("one long line of commas", lambda: b"," * 2_000_000),
    ("one long line of tabs", lambda: b"\t" * 1_000_000),
    ("one long line of pipes and quotes", lambda: b'|"' * 800_000),
    ("trace marker then one long line", lambda: b"Traceback (most recent call last):\n" + b"q" * 2_000_000),
    ("python frame prefix repeated on one line", lambda: b'  File "x", line 1, in f' * 60_000 + b"\n"),
    ("open parens after 'at'", lambda: b"    at " + b"(" * 400_000 + b"\n" + b"x" * 5000),
    ("quotes after File", lambda: b'  File "' + b'"' * 400_000 + b"\ngoroutine 1 [x]\n" + b"x\n" * 2000),
    ("diff header with a huge path", lambda: b"--- " + b"a" * 400_000 + b"\n+++ " + b"b" * 400_000 + b"\n@@ -1 +1 @@\n" + b"-x\n+y\n" * 2000),
    ("hunk header with a huge count", lambda: b"--- a\n+++ b\n@@ -1," + b"9" * 300_000 + b" +1 @@\n" + b" x\n" * 2000),
    ("a million short lines", lambda: b"a\n" * 1_000_000),
    ("a million 'x,y' lines", lambda: b"x,y\n" * 500_000),
    ("a million delimiter-only lines", lambda: b",\n" * 500_000),
    ("a million 'at' frames", lambda: b"    at f (a.js:1:2)\n" * 300_000),
    ("a million hunk headers", lambda: b"--- a\n+++ b\n" + b"@@ -1 +1 @@\n-a\n+b\n" * 150_000),
])
def test_pathological_inputs_run_in_linear_time(env, name, make):
    assert _timed(env, make()) < SLOW, name
