"""Acceptance: search/v1 digest (grep/rg output structured) and the native
Grep/Glob interception (transparent head_limit bounding)."""

import subprocess

import pytest


def _ctx_for(tmp_path, text, argv):
    from ctx.digest.base import DigestContext, StreamView
    from ctx.workspace import resolve_workspace

    (tmp_path / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    ws = resolve_workspace(str(tmp_path))
    out = StreamView("stdout", len(text.encode()), len(text.splitlines()), "text/plain", text, True)
    err = StreamView("stderr", 0, 0, "text/plain", "", True)
    manifest = {
        "argv": argv, "cwd": ".", "shell": False,
        "result": {"exitCode": 0, "signal": None, "timedOut": False},
        "streams": {"stdout": {"blob": "sha256:x"}, "stderr": {"blob": "sha256:y"}},
    }
    return DigestContext(ws=ws, manifest=manifest, stdout=out, stderr=err)


GREP_OUT = "\n".join(
    f"src/mod{i % 4}.py:{i + 1}:    result = compute(x, {i})" for i in range(40)
)


def test_search_profile_digests_grep(tmp_path):
    from ctx.digest.searchprof import SearchProfile

    p = SearchProfile()
    ctx = _ctx_for(tmp_path, GREP_OUT, ["grep", "-rn", "compute", "src"])
    assert p.detect(ctx)
    body = p.render(ctx)
    assert "matches (exact): 40 across 4 files" in body
    assert "mod0.py×10" in body  # per-file histogram
    assert "top matches:" in body
    assert "compute(x, 0)" in body  # top hit verbatim with coordinate
    assert "more matches" in body  # span to the rest


def test_search_recognizes_tool_name_argv(tmp_path):
    from ctx.digest.searchprof import SearchProfile

    # The emission gate synthesizes argv=[tool_name]; the native Grep tool and
    # mcp grep-shaped faucets must still reach search/v1.
    for tool in ("Grep", "mcp__code__search_code", "mcp__x__grep_files"):
        assert SearchProfile().detect(_ctx_for(tmp_path, GREP_OUT, [tool])), tool
    # a non-search tool with the same shape must NOT be stolen
    assert SearchProfile().detect(_ctx_for(tmp_path, GREP_OUT, ["mcp__github__list_commits"])) is None


def test_search_declines_non_grep(tmp_path):
    from ctx.digest.searchprof import SearchProfile

    # Same file:line:content shape but NOT a grep command → decline
    # (this is the collision guard: logs/lint must not be stolen).
    ctx = _ctx_for(tmp_path, GREP_OUT, ["python", "-m", "pytest"])
    assert SearchProfile().detect(ctx) is None


def test_search_does_not_steal_lint_or_logs(tmp_path):
    from ctx.digest import detect_profile

    ruff = "\n".join(f"mod{i%3}.py:{i}:8: F401 `os` imported but unused" for i in range(20))
    assert detect_profile(_ctx_for(tmp_path, ruff, ["ruff", "check", "."]))[0].version == "lint/v1"
    grep = "\n".join(f"src/a{i%3}.py:{i}:    x = f()" for i in range(20))
    assert detect_profile(_ctx_for(tmp_path, grep, ["grep", "-rn", "f", "src"]))[0].version == "search/v1"


# ------------------------------------------------- native Grep interception
@pytest.fixture()
def ws(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    # These tests target the native-Grep head_limit *cap*, which is the
    # break-glass (collapse-off) behaviour; under the default posture native
    # search is removed from the surface instead (see test_substitute.py).
    (d / "ctx.toml").write_text("version = 1\n[guard]\ncollapse = false\n", encoding="utf-8")
    return d


def test_native_content_grep_gets_bounded(ws):
    from ctx.hook import classify

    d = classify({
        "tool_name": "Grep",
        "tool_input": {"pattern": "def ", "output_mode": "content", "path": "src"},
        "cwd": str(ws),
    })
    ui = d.get("rewrite", {}).get("updatedInput")
    assert ui is not None and ui["head_limit"] == 60  # transparent cap injected
    assert ui["pattern"] == "def "  # everything else untouched


def test_native_grep_respects_existing_bound_and_modes(ws):
    from ctx.hook import classify

    def dec(ti):
        return classify({"tool_name": "Grep", "tool_input": ti, "cwd": str(ws)})

    # already bounded → allow, no rewrite
    d = dec({"pattern": "x", "output_mode": "content", "head_limit": 20})
    assert "rewrite" not in d and d["decision"] == "allow"
    # files_with_matches → small, allow raw
    assert dec({"pattern": "x", "output_mode": "files_with_matches"})["decision"] == "allow"
    # count mode → allow raw
    assert dec({"pattern": "x", "output_mode": "count"})["decision"] == "allow"


def test_native_grep_strict_steering_denies(ws):
    from ctx.hook import classify

    # collapse off isolates the strict-steering deny path (with collapse on,
    # native search is removed by the replacement surface first).
    (ws / "ctx.toml").write_text(
        "version = 1\n[guard]\ncollapse = false\nsteering = \"deny\"\n", encoding="utf-8"
    )
    d = classify({
        "tool_name": "Grep",
        "tool_input": {"pattern": "x", "output_mode": "content"},
        "cwd": str(ws),
    })
    assert d["decision"] == "deny"
    assert "ctx run -- grep" in d["reason"]


def test_glob_passes_through(ws):
    from ctx.hook import classify

    d = classify({"tool_name": "Glob", "tool_input": {"pattern": "**/*.py"}, "cwd": str(ws)})
    assert d["decision"] == "allow"


def test_end_to_end_grep_digest(tmp_path, monkeypatch):
    """A real wrapped grep through `ctx run` lands on search/v1."""
    monkeypatch.setenv("CTX_STATE_HOME", str(tmp_path / "state"))
    from ctx.digest import render_run_digest
    from ctx.execution import run_capture
    from ctx.store import Store
    from ctx.workspace import resolve_workspace

    d = tmp_path / "proj"
    d.mkdir()
    (d / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    src = d / "src"
    src.mkdir()
    for i in range(6):
        (src / f"m{i}.py").write_text("\n".join(f"def f{j}(): pass" for j in range(6)))
    subprocess.run(["git", "init", "-q", "."], cwd=d, check=True)
    ws = resolve_workspace(str(d))
    store = Store(ws.workspace_id)
    cap = run_capture(ws, ["grep", "-rn", "def", "src"], shell=False, store=store)
    digest, manifest = render_run_digest(store, ws, cap.manifest)
    assert manifest["digest"]["profile"] == "search/v1"
    assert "matches (exact): 36" in digest
