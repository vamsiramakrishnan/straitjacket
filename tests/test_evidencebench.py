"""SJ-EvidenceBench (evals/BENCHMARK.md): evidence-channel conformance
tests for the two verified gaps — scenario F (stdout/stderr descriptor
graphs) and scenario H (machine-format negotiation baseline).

These are conformance tests, not model benchmarks: they pin the semantics
of classification and digest negotiation deterministically and unpaid.
"""

import json
import textwrap

import pytest

pytestmark = pytest.mark.sj_canary


# ---------------------------------------------------------------- scenario F
# Redirect graphs are semantically different shell programs. Only forms
# where BOTH streams provably leave the console may bypass capture.


@pytest.mark.parametrize(
    "cmd,expected",
    [
        # dup AFTER stdout redirect: both streams to file — proven small.
        ("pytest > out.log 2>&1", "allow"),
        ("pytest >> out.log 2>&1", "allow"),
        ("pytest &> out.log", "allow"),
        # dup BEFORE stdout redirect: stderr goes to the CONSOLE (POSIX
        # processes redirections left to right) — a failing run floods.
        ("pytest 2>&1 > out.log", "force_ask"),
        # stderr-only redirect: stdout still unbounded.
        ("pytest 2> err.log", "force_ask"),
        # tee duplicates to the console by design.
        ("pytest | tee out.log", "force_ask"),
        # pseudo-device targets defeat the "file holds the bytes" proof.
        ("cat big.txt > /dev/null 2>&1", "force_ask"),
    ],
)
def test_descriptor_graph_classification(cmd, expected):
    from ctx.hook import classify_command

    assert classify_command(cmd, {}).get("decision") == expected, cmd


# ---------------------------------------------------------------- scenario H
# The same facts in different serializations must land in the profile that
# can extract structure, and every rendering must be bounded and
# deterministic. This is the negotiation BASELINE: it pins where each
# format lands today, so improvements (an XML/SARIF tier) show up as
# deliberate diffs to this file, never as silent drift.

_DIAG = {
    "file": "src/pay.c",
    "line": 41,
    "severity": "error",
    "message": "implicit declaration of function 'settle'",
}


def _ctx_for(tmp_path, text, argv=("tool",)):
    from ctx.digest.base import DigestContext, StreamView
    from ctx.workspace import resolve_workspace

    (tmp_path / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    ws = resolve_workspace(str(tmp_path))
    out = StreamView(
        "stdout", len(text.encode()), len(text.splitlines()), "text/plain", text, True
    )
    err = StreamView("stderr", 0, 0, "text/plain", "", True)
    manifest = {
        "argv": list(argv), "cwd": ".", "shell": False,
        "result": {"exitCode": 1, "signal": None, "timedOut": False},
        "streams": {"stdout": {"blob": "sha256:x"}, "stderr": {"blob": "sha256:y"}},
    }
    return DigestContext(ws=ws, manifest=manifest, stdout=out, stderr=err)


def test_json_diagnostics_claimed_by_json_profile(tmp_path):
    from ctx.digest import detect_profile

    doc = json.dumps({"diagnostics": [_DIAG] * 40}, indent=2)
    profile, reason = detect_profile(_ctx_for(tmp_path, doc))
    assert profile.version.startswith("json"), (profile.version, reason)


def test_jsonl_diagnostics_claimed_by_jsonl_profile(tmp_path):
    from ctx.digest import detect_profile

    doc = "\n".join(json.dumps(_DIAG) for _ in range(40)) + "\n"
    profile, _ = detect_profile(_ctx_for(tmp_path, doc))
    assert profile.version.startswith("json"), profile.version


def test_prose_diagnostics_claimed_by_lint_family(tmp_path):
    from ctx.digest import detect_profile

    doc = "\n".join(
        f"src/pay.c:{40 + i}:7: error: implicit declaration of function 'settle'"
        for i in range(40)
    )
    profile, _ = detect_profile(_ctx_for(tmp_path, doc))
    assert profile.version in ("lint/v1", "build/v1"), profile.version


def test_junit_xml_baseline_bounded_and_deterministic(tmp_path):
    """No XML tier exists (recorded gap, evals/BENCHMARK.md build list):
    JUnit XML must still fall through bounded and byte-deterministic."""
    from ctx.digest import detect_profile

    doc = '<?xml version="1.0"?>\n<testsuite tests="200" failures="3">\n' + "".join(
        f'  <testcase classname="pay" name="t{i}"'
        + ('><failure message="boom"/></testcase>\n' if i in (5, 63, 199) else "/>\n")
        for i in range(200)
    ) + "</testsuite>\n"
    ctx = _ctx_for(tmp_path, doc)
    profile, _ = detect_profile(ctx)
    body_a = profile.render(ctx)
    body_b = profile.render(_ctx_for(tmp_path, doc))
    assert body_a == body_b  # deterministic
    assert len(body_a.encode()) < len(doc.encode())  # bounded below raw


def test_sarif_baseline_lands_in_json_family(tmp_path):
    """SARIF is JSON: shape dispatch must claim it structurally even with
    no dedicated tier — the census beats a blind head/tail."""
    from ctx.digest import detect_profile

    sarif = json.dumps(
        {
            "version": "2.1.0",
            "runs": [
                {
                    "tool": {"driver": {"name": "clangsa"}},
                    "results": [
                        {
                            "ruleId": "core.NullDereference",
                            "level": "error",
                            "message": {"text": "null deref"},
                            "locations": [
                                {
                                    "physicalLocation": {
                                        "artifactLocation": {"uri": f"src/m{i}.c"},
                                        "region": {"startLine": 10 + i},
                                    }
                                }
                            ],
                        }
                        for i in range(60)
                    ],
                }
            ],
        },
        indent=2,
    )
    profile, _ = detect_profile(_ctx_for(tmp_path, sarif))
    assert profile.version.startswith("json"), profile.version
