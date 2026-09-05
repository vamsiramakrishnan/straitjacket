"""Improvement route, first live run (evals/improve_route.py, 2026-09-05):
the tests the verify node wrote, kept where the hand review kept the fix.

The route's hunt node (frontier model) claimed 33 findings in its yield and
more in its transcript; the verify node reproduced 66 and wrote one failing
test per claim; the harvest node fixed 69 of 70 before its hour timeout; the
prove step (run by hand) found 3 regressions in the command-substitution
family, so the gate held the round. The hand review then took every fix
whose test failed on the old tree and whose change did not contradict a
contract an existing test pins, and refused six: four substitution tests
that re-decide a collapse rule rounds 12-14 settled, one that redefines a
replay metric, and one that cannot force fd recycling reliably (its fix,
removing a double close, is taken). evals/bugbash-round17-2026-09-04.md
records the run.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import signal
import stat
import subprocess
import sys
import threading
import time
import types

import pytest

from conftest import make_store, make_ws


# =====================================================================
# Finding 1 -- src/ctx/hook.py:2050
# =====================================================================
def test_collapse_does_not_bypass_secret_path_force_ask(tmp_path):
    from ctx.hook import classify

    env_file = tmp_path / ".env"
    env_file.write_text("SECRET=abc123\n" * 5, encoding="utf-8")

    def _classify(command):
        payload = {
            "tool_name": "run_command",
            "tool_input": {"CommandLine": command, "Cwd": str(tmp_path)},
            "workspacePaths": [str(tmp_path)],
        }
        return classify(payload)

    baseline = _classify("cat .env")
    assert baseline["decision"] == "force_ask"

    collapsible = _classify("head -n 50 .env")
    # The collapse/substitution surface must never turn a secret-path
    # force_ask into an allow+rewrite that hands the model a `ctx get`
    # command reading the same secret file.
    assert collapsible["decision"] == "force_ask", (
        f"expected force_ask for a secret-path read routed through the "
        f"collapse surface, got: {collapsible!r}"
    )


# =====================================================================
# Finding 2 -- src/ctx/hook.py:1837
# =====================================================================
def test_classify_read_relative_path_matches_workspace_root(tmp_path, monkeypatch):
    """classify_read must judge a relative path against workspace_root,
    not the hook process's CWD -- otherwise it disagrees with itself
    depending only on where the hook happens to be running from.
    """
    from ctx.hook import classify_read, _load_guard_policy

    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "README.md").write_text("hello\n", encoding="utf-8")

    policy = _load_guard_policy(str(ws))

    # (a) CWD outside the workspace must not force-ask an in-workspace file.
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    monkeypatch.chdir(outside)
    d = classify_read("README.md", str(ws), dict(policy), "s")
    assert d["decision"] == "allow", (
        f"in-workspace relative file wrongly rejected due to CWD: {d!r}"
    )

    # (b) CWD inside a subdirectory of the workspace must resolve the same
    # relative path the same way as its absolute equivalent -- not silently
    # skip the inline-byte budget by mis-resolving to a nonexistent path.
    big = ws / "BIG.md"
    big.write_text("x" * 20000, encoding="utf-8")
    monkeypatch.chdir(ws / "src")
    rel = classify_read("BIG.md", str(ws), dict(policy), "s2")
    abs_ = classify_read(str(big), str(ws), dict(policy), "s3")
    assert rel["decision"] == abs_["decision"], (
        f"relative vs absolute disagree: rel={rel!r} abs={abs_!r}"
    )


# =====================================================================
# Finding 3 -- src/ctx/textutil.py:16
# =====================================================================
def test_strip_control_unterminated_osc_does_not_eat_remainder():
    from ctx.textutil import strip_control

    text = "row A: \x1b]0;clipped title\nrow B: keeps\ncoverage: x\nnext: y"
    out = strip_control(text)
    assert "row B: keeps" in out
    assert "coverage: x" in out
    assert "next: y" in out


# =====================================================================
# Finding 4 -- src/ctx/digest/base.py:36
# =====================================================================
def test_stream_view_text_lines_matches_store_nl_only_split():
    from ctx.digest.base import StreamView
    from ctx.textutil import index_lines

    data = b"a\rb\n"
    text = data.decode("utf-8")
    sv = StreamView("stdout", len(data), 1, "text/plain", text, True)

    # The store's line index / manifest line count split on \n only, so a
    # digest's own line enumeration must agree with that, not with
    # str.splitlines()'s wider notion of a line break.
    assert sv.text_lines == index_lines(text)


# =====================================================================
# Finding 5 -- src/ctx/installer.py:54
# =====================================================================
def test_generated_ctx_toml_redacts_common_secret_types():
    """ctx.toml written by `ctx init`/`ctx setup` pins [redaction].patterns to
    only 3 of the 16 code-default patterns, so a GitHub token (one of the 13
    dropped patterns) survives redact() under the generated config even
    though the code default would catch it.
    """
    from ctx.config import Redaction
    from ctx.installer import _CTX_TOML_TEMPLATE
    from ctx.textutil import sanitize_for_model

    m = re.search(r"patterns\s*=\s*\[(.*?)\]", _CTX_TOML_TEMPLATE, re.S)
    assert m, "expected [redaction] patterns entry in the generated ctx.toml template"
    template_patterns = tuple(p.strip().strip('"') for p in m.group(1).split(","))

    template_cfg = Redaction(patterns=template_patterns)

    gh_token = "gh" + "p_" + ("A1b2C3d4E5f6G7h8I9j0" * 2)
    text = "token: %s\n" % gh_token

    out, fired = sanitize_for_model(text, template_cfg)

    assert fired == ["github-token"], (
        "installer's ctx.toml template only pins %r (missing github-token, "
        "among 12 other code-default patterns), so a GitHub token is not "
        "redacted for a freshly-initialized workspace: fired=%r"
        % (template_patterns, fired)
    )
    assert gh_token not in out


# =====================================================================
# Finding 6 -- src/ctx/plan_exec.py:286
# =====================================================================
def test_explicit_null_wall_seconds_passes_validation_but_crashes_execute(tmp_path, state_home):
    """An explicit "wall_seconds": null in a plan doc must either be rejected
    by validate_plan() or handled by execute_plan() -- it must not pass
    validation and then blow up with an uncaught TypeError from float(None).
    """
    from ctx.plan_ir import parse_plan, validate_plan
    from ctx.plan_exec import execute_plan

    ws_dir = tmp_path / "proj"
    ws_dir.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q"], cwd=ws_dir, check=True, env=env)
    (ws_dir / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=ws_dir, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=ws_dir, check=True, env=env)

    plan_doc = {
        "version": "ctx.plan/v1",
        "objective": {"question": "q"},
        "budget": {"wall_seconds": None},
        "steps": [{"id": "a", "op": "repo.changed"}],
    }

    plan = parse_plan(plan_doc)
    rejections = validate_plan(plan, plan_policy=None)

    ws = make_ws(ws_dir)
    store = make_store(ws)

    if rejections:
        # Acceptable fix: validate_plan now rejects an explicit null budget.
        return

    # Otherwise, validation accepted the plan -- execution must not crash.
    text, code = execute_plan(ws, store, plan_doc)
    assert code in (0, 2, 3)


# =====================================================================
# Finding 7 -- src/ctx/orchestrator.py:900 (via ctx._proc.wait_or_kill)
# =====================================================================
def _spawn(script: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _alive(pid: int) -> bool:
    """Running, as distinct from a zombie: an orphan killed after its parent
    died is reaped by PID 1 on its own schedule, and `os.kill(pid, 0)` still
    succeeds on a zombie. The route's harvest made the test process a child
    subreaper at import time to reap it itself; the review kept the kill and
    dropped the subreaper, so the probe reads the state instead."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().split(")")[-1].split()[0] != "Z"
    except OSError:
        return False


def _wait_dead(pid: int, seconds: float = 3.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.02)
    return not _alive(pid)


def test_wait_or_kill_zero_misses_the_orphaned_grandchild_when_leader_already_exited(tmp_path):
    """This is the exact shape `_run_bounded`'s TimeoutExpired handler hits:
    the leader forks a grandchild and exits immediately (long before the
    grandchild is done), so by the time the caller notices a timeout (e.g.
    because `communicate()` was still blocked reading the leader's inherited
    stdout/stderr pipe, held open by the grandchild) and calls
    `wait_or_kill(proc, 0)`, the leader has ALREADY terminated.

    `wait_or_kill(proc, 0)` must still reap/kill the orphaned grandchild's
    process group in this case -- that is the entire point of calling it
    from a timeout handler. Currently `proc.wait(timeout=0)` returns
    immediately (no TimeoutExpired, since the leader is already dead) and
    the killpg branch never runs, so the grandchild is never touched.
    """
    from ctx._proc import wait_or_kill

    marker = tmp_path / "grandchild.pid"
    script = (
        "import os, subprocess, sys, time\n"
        f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10)'])\n"
        f"open({str(marker)!r}, 'w').write(str(p.pid))\n"
    )
    proc = _spawn(script)

    for _ in range(200):
        if proc.poll() is not None:
            break
        time.sleep(0.01)
    assert proc.poll() is not None, "leader should have exited by now"

    for _ in range(200):
        if marker.exists() and marker.read_text().strip():
            break
        time.sleep(0.01)
    grandchild = int(marker.read_text().strip())
    assert _alive(grandchild), "grandchild should still be running at this point"

    try:
        wait_or_kill(proc, 0)
        time.sleep(0.3)
        assert _wait_dead(grandchild), (
            "wait_or_kill(proc, 0) left the orphaned grandchild (pid %d) "
            "running -- it only kills when the direct child is still alive, "
            "which is exactly the case a timeout handler that reaches this "
            "point is NOT in" % grandchild
        )
    finally:
        if _alive(grandchild):
            try:
                os.killpg(grandchild, signal.SIGKILL)
            except OSError:
                pass


# =====================================================================
# Finding 8 -- src/ctx/callgraph.py:740
# =====================================================================
@pytest.fixture()
def unmatched_dotted_ws(tmp_path, monkeypatch):
    monkeypatch.setenv("CTX_STATE_HOME", str(tmp_path / "state"))
    from ctx.store import Store
    from ctx.workspace import resolve_workspace

    d = tmp_path / "proj"
    d.mkdir()
    (d / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    pkg = d / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "mod.py").write_text(
        "class Baz:\n"
        "    def bar(self):\n"
        "        return 1\n"
        "\n"
        "def use_it():\n"
        "    return Baz().bar()\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q", "."], cwd=d, check=True)
    ws = resolve_workspace(str(d))
    return ws, Store(ws.workspace_id)


def test_dotted_query_with_no_qualified_match_does_not_silently_answer_for_a_different_symbol(
    unmatched_dotted_ws,
):
    """`Foo.bar` names no definition anywhere in this repo (there is no Foo
    class at all -- only `Baz.bar`). `_resolve_target` must not silently
    fall back to the bare-name lookup and answer as if `Foo.bar` were an
    exact, unambiguous match for `Baz.bar`. It should either say no
    definition matches, or at minimum disclose that the answer is for a
    different / ambiguous symbol -- never present it as a clean 1-target
    exact hit."""
    from ctx.callgraph import cmd_callers

    ws, store = unmatched_dotted_ws
    out = cmd_callers(store, ws, "Foo.bar")

    assert "no definition matching" in out or "ambiguous" in out or "Baz" in out, (
        "cmd_callers('Foo.bar') answered with no trace of the fact that "
        "'Foo.bar' does not exist and the real match is a different, "
        "unrelated symbol (Baz.bar):\n" + out
    )


# =====================================================================
# Finding 9 -- src/ctx/_retrieval/get.py:194
# =====================================================================
class _Budget:
    """Just the fields `_fit_window` reads."""

    def __init__(self, result_tokens: int) -> None:
        self.result_tokens = result_tokens
        self.max_inline_lines = 10_000
        self.max_inline_bytes = 1 << 20


def test_fit_window_never_emits_an_inverted_continuation_for_an_oversized_single_line():
    """A one-line window (a == b == total, e.g. a single minified/huge line)
    that alone exceeds the byte budget must not produce a continuation
    address at all, or must produce one that `anchors.parse_span` accepts
    (A <= B). `kept = max(1, kept)` currently forces `kept=1` even though
    nothing actually fit, which pushes `new_b` up to `b` and makes the
    computed "next" start (`new_b + 1`) exceed its own end
    (`min(total, new_b + span))`) -- an inverted range like `2:1`.
    """
    from ctx._retrieval.get import _fit_window
    from ctx.anchors import parse_span

    huge_line = "x" * 5000
    rendered = [huge_line]

    new_b, nxt = _fit_window("--lines", "blob:deadbeef", 1, 1, 1, rendered, _Budget(100))

    if nxt is None:
        # Acceptable fix: no further continuation is offered for a single
        # line that cannot be fit at all.
        return

    value = nxt.rsplit(" ", 1)[-1]
    a, b, _anchor = parse_span(value)  # must not raise, and a <= b
    assert a <= b, f"inverted continuation range emitted: {nxt!r}"


# =====================================================================
# Finding 10 -- src/ctx/command_spans.py:151
# =====================================================================
def test_gh_api_attached_shorthand_mutation_flags_are_recognised_as_mutations():
    """`gh api` accepts pflag-style flags in three spellings: detached
    shorthand (`-X POST`), attached shorthand (`-XPOST`), and long
    `--flag=value`. The mutation-flag scan in `_gh_span` only recognises the
    first and third: it checks each arg for an exact `-X`/`-f`/... match, or
    a `--long=value` prefix, but never `-X` glued to its value. So
    `-XDELETE` and `-fquery=...` are invisible to it, and a `gh api`
    mutation spelled that way is classified the same as a harmless
    unparameterised GET (`capture`, i.e. NOT the mutation-shaped/`None`
    result that routes it back through the stricter permission boundary).
    """
    from ctx.command_spans import classify_command_span

    spaced = classify_command_span(
        ["gh", "api", "-X", "DELETE", "repos/o/r/issues/comments/1"]
    )
    attached = classify_command_span(
        ["gh", "api", "-XDELETE", "repos/o/r/issues/comments/1"]
    )
    attached_field = classify_command_span(
        ["gh", "api", "graphql", "-fquery=mutation{}"]
    )
    plain_get = classify_command_span(["gh", "api", "repos/o/r/issues"])

    assert spaced is None

    assert attached is None, (
        "`-XDELETE` (attached shorthand) was not recognised as a mutation "
        "flag and fell through to %r, same as a plain read (%r)"
        % (attached, plain_get)
    )
    assert attached_field is None, (
        "`-fquery=...` (attached shorthand field flag) was not recognised "
        "as a mutation flag and fell through to %r, same as a plain read "
        "(%r)" % (attached_field, plain_get)
    )


# =====================================================================
# Finding 11 -- src/ctx/substitute.py:335
# =====================================================================
# =====================================================================
# Finding 12 -- src/ctx/substitute.py:208
# =====================================================================
# =====================================================================
# Finding 13 -- src/ctx/hook.py:1512
# =====================================================================
def test_piped_follow_forever_rewrite_gets_bg():
    from ctx.hook import _load_guard_policy, classify_command

    pol = _load_guard_policy(None)
    r = classify_command("tail -f app.log | grep ERROR", pol)
    assert r["decision"] == "force_ask"
    assert "_rewrite" in r
    assert "--bg" in r["_rewrite"]["command"], (
        f"never-terminating piped command steered into a blocking foreground "
        f"capture (no --bg): {r['_rewrite']['command']}"
    )


# =====================================================================
# Finding 14 -- src/ctx/mcp.py:421
# =====================================================================
class _FakeStdinBuffer:
    def __init__(self, lines):
        self._lines = lines

    def __iter__(self):
        for line in self._lines:
            yield line.encode()


def test_serve_survives_non_object_json_line(monkeypatch):
    import io
    import ctx.mcp as mcp

    lines = [
        json.dumps([]) + "\n",
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n",
    ]
    fake_stdin = type("S", (), {"buffer": _FakeStdinBuffer(lines)})()
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", fake_stdin)
    monkeypatch.setattr(sys, "stdout", out)
    mcp.serve()  # must not raise, and must still answer the ping after
    assert '"id":1' in out.getvalue() or '"id": 1' in out.getvalue(), (
        f"ping after a non-object JSON line was never answered: {out.getvalue()!r}"
    )


# =====================================================================
# Finding 15 -- src/ctx/surface_gateway.py:409
# =====================================================================
def test_serve_gateway_survives_non_object_json_line(monkeypatch, tmp_path):
    import io
    import ctx.surface_gateway as sg

    lines = [
        json.dumps([]) + "\n",
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n",
    ]

    class FakeStdin:
        def __iter__(self):
            return iter(lines)

    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", FakeStdin())
    monkeypatch.setattr(sys, "stdout", out)
    sg.serve_gateway(ws_root=str(tmp_path))  # must not raise
    assert '"id": 1' in out.getvalue() or '"id":1' in out.getvalue(), (
        f"ping after a non-object JSON line was never answered: {out.getvalue()!r}"
    )


# =====================================================================
# Finding 16 -- src/ctx/config.py:308
# =====================================================================
def test_scalar_budgets_section_fails_open(tmp_path):
    from ctx.config import load_config

    (tmp_path / "ctx.toml").write_text("budgets = 5\n", encoding="utf-8")
    cfg = load_config(tmp_path)  # should fail open to defaults, not raise
    assert cfg.budgets.max_inline_bytes  # defaults intact


def test_scalar_guard_section_fails_open(tmp_path):
    from ctx.config import load_config

    (tmp_path / "ctx.toml").write_text('guard = "strict"\n', encoding="utf-8")
    cfg = load_config(tmp_path)  # should fail open to defaults, not raise
    assert cfg.guard.mode  # defaults intact


# =====================================================================
# Finding 17 -- src/ctx/installer.py:461
# =====================================================================
def test_reinstall_adds_missing_hook_stage(tmp_path):
    from ctx.workspace import resolve_workspace
    from ctx.installer import claude_hook_settings, install_claude, _ctx_executable

    (tmp_path / ".git").mkdir()
    ws = resolve_workspace(str(tmp_path))
    exe = _ctx_executable()
    full = claude_hook_settings(exe)
    assert "SessionStart" in full["hooks"]

    partial_hooks = {k: v for k, v in full["hooks"].items() if k != "SessionStart"}
    (tmp_path / ".claude").mkdir()
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.write_text(json.dumps({"hooks": partial_hooks}), encoding="utf-8")

    install_claude(ws)  # re-run `ctx setup`

    after = json.loads(settings_path.read_text())
    assert "SessionStart" in after.get("hooks", {}), (
        "re-running install_claude did not add the SessionStart stage that "
        "an earlier ctx version's install lacked"
    )


# =====================================================================
# Finding 18 -- src/ctx/store.py:669
# =====================================================================
def test_gc_race_deletes_concurrently_written_object(tmp_path, monkeypatch):
    from ctx.store import Store

    monkeypatch.setenv("CTX_STATE_HOME", str(tmp_path / "state"))
    store = Store("race-repro", retention_days=30)

    old_id = store.put_blob(b"old-and-unleased")
    store.db.execute("UPDATE objects SET created_at = 0 WHERE id = ?", (old_id,))
    store.db.commit()

    injected = {}
    real_db = store.db

    class SpyingConnection:
        def execute(self, sql, params=()):
            if sql.strip() == "SELECT id, kind FROM objects" and "new_id" not in injected:
                concurrent_store = Store("race-repro", retention_days=30)
                injected["new_id"] = concurrent_store.put_blob(
                    b"written-during-the-gc-window"
                )
            return real_db.execute(sql, params)

        def __getattr__(self, name):
            return getattr(real_db, name)

        def __enter__(self):
            return real_db.__enter__()

        def __exit__(self, *a):
            return real_db.__exit__(*a)

    store._db = SpyingConnection()
    store.gc(retention_days=30)

    new_id = injected["new_id"]
    assert store.blob_path(new_id).exists(), (
        f"blob {new_id} written during the gc() window was deleted by gc() "
        "even though it was never dead -- a concurrently-written object lost "
        "to the unsynchronized mark/sweep race (live set materialized before "
        "the write; sweep query ran after it, with nothing spanning both)"
    )
    row = store.db.execute("SELECT id FROM objects WHERE id = ?", (new_id,)).fetchone()
    assert row is not None, (
        f"catalog row for {new_id}, written during the gc() window, was "
        "removed by gc() despite never having been dead"
    )


# =====================================================================
# Finding 19 -- src/ctx/worktree_isolation.py:194
# =====================================================================
def test_cleanup_leaks_stale_worktree_admin_entry_when_remove_fails(git_workspace):
    """IsolatedWorktree._cleanup (~188-198) runs `git worktree remove
    --force` under contextlib.suppress(Exception), then `git worktree
    prune` while the temp directory still exists on disk, and only
    afterward shutil.rmtree's the directory. If `remove` fails (e.g. the
    worktree is locked), `prune` -- which only reaps entries whose working
    tree is already gone -- has nothing to reap yet, so the directory's
    later removal leaves the `.git/worktrees/<id>` admin entry stale
    forever: prune is never called again.
    """
    from ctx.worktree_isolation import IsolatedWorktree, _git

    root = git_workspace
    iso = IsolatedWorktree(root, "nodeA", ())
    iso.__enter__()
    assert iso.path is not None and iso.path.exists()

    locked = _git(root, "worktree", "lock", str(iso.path))
    assert locked.returncode == 0, locked.stderr

    iso._cleanup()

    listing = subprocess.run(
        ["git", "worktree", "list", "--porcelain"], cwd=root,
        capture_output=True, text=True, check=True,
    ).stdout
    assert "nodeA" not in listing, (
        "cleanup leaked a stale worktree admin entry: prune ran while the "
        "directory still existed (before shutil.rmtree), so it found "
        f"nothing to reap and the entry survives forever:\n{listing}"
    )


# =====================================================================
# Finding 20 -- src/ctx/orchestrator.py:2003
# =====================================================================
def test_merge_patch_lets_route_error_escape_from_bad_replan_node():
    """_merge_patch (orchestrator.py ~1989-2008) calls `_coerce_node(r, ...)`
    at line 2003, one line *above* the
    `with contextlib.suppress(RouteError):` block that starts at 2006. A
    model-supplied replan node with unsafe/invalid targets makes
    `_coerce_node` raise RouteError (via `normalize_targets` ->
    WorktreeIsolationError), and that raise happens outside the suppress
    guard, so it escapes `_merge_patch` entirely instead of being
    swallowed as one bad replan node among others.
    """
    from ctx.orchestrator import _merge_patch

    nodes = []
    state = {}
    patch = {"nodes": [{"id": "bad", "targets": ["/etc/hosts"]}]}
    hosts = []
    cfg = types.SimpleNamespace(max_nodes=12)

    # Desired/fixed behavior: one malformed coordinator-supplied node
    # should be skipped (and simply not added), not blow up the caller
    # (run_route has no try/except around this call, so today this abends
    # an in-flight run instead of just dropping the bad node).
    added = _merge_patch(nodes, state, patch, hosts, cfg)
    assert added == 0
    assert nodes == []


# =====================================================================
# Finding 21 -- src/ctx/jobs.py:235
# =====================================================================
@pytest.fixture()
def ws_store_f21(tmp_path, monkeypatch):
    monkeypatch.setenv("CTX_STATE_HOME", str(tmp_path / "state"))
    from ctx.store import Store
    from ctx.workspace import resolve_workspace

    d = tmp_path / "proj"
    d.mkdir()
    (d / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "."], cwd=d, check=True)
    ws = resolve_workspace(str(d))
    return ws, Store(ws.workspace_id)


def test_start_job_read_modify_write_clobbers_concurrent_running_state(ws_store_f21, monkeypatch):
    """start_job's post-spawn read-then-write of meta.json (jobs.py ~233-237)
    is not atomic against the supervisor concurrently writing "running".

    We isolate the exact race by (a) faking Popen so no real supervisor
    process is competing, and (b) widening the window between start_job's
    `_read_meta` call and its subsequent `_write_meta` call so a background
    thread -- standing in for the supervisor -- can land its "running"
    write inside that window, exactly as the finding describes.
    """
    import ctx.jobs as jobs_mod

    ws, store = ws_store_f21

    monkeypatch.setattr(jobs_mod, "_new_job_id", lambda: "aaaaaaaaaaaa")

    class _FakeProc:
        pid = 999999

    monkeypatch.setattr(jobs_mod.subprocess, "Popen", lambda *a, **k: _FakeProc())

    real_read_meta = jobs_mod._read_meta
    supervisor_wrote = threading.Event()

    def _slow_read_meta(jobdir):
        result = real_read_meta(jobdir)
        supervisor_wrote.wait(timeout=2)
        return result

    monkeypatch.setattr(jobs_mod, "_read_meta", _slow_read_meta)

    jobdir = jobs_mod._job_dir(store, "aaaaaaaaaaaa")

    def _simulate_supervisor():
        meta_path = jobdir / "meta.json"
        for _ in range(200):
            if meta_path.exists():
                break
            time.sleep(0.01)
        current = real_read_meta(jobdir)
        current["state"] = "running"
        current["pid"] = 424242
        jobs_mod._write_meta(jobdir, current)
        supervisor_wrote.set()

    t = threading.Thread(target=_simulate_supervisor)
    t.start()
    try:
        job_id = jobs_mod.start_job(ws, store, ["true"])
    finally:
        t.join(timeout=2)

    assert job_id == "aaaaaaaaaaaa"
    final = jobs_mod._read_meta(jobdir)
    assert final["state"] == "running", (
        "start_job's unsynchronized read-then-write clobbered the "
        f"supervisor's concurrently-written 'running' state: {final!r}"
    )


# =====================================================================
# Finding 22 -- src/ctx/proxy.py:327
# =====================================================================
class _FakeStaleConn:
    """Stands in for an upstream connection the pool handed back that the
    peer has since idled out -- looks fine, fails the instant it is used."""

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _FakeRelayResp:
    status = 200
    reason = "OK"
    will_close = False

    def getheaders(self):
        return []

    def getheader(self, name, default=None):
        return default

    def read1(self, n):
        return b""

    def close(self):
        pass


class _FakeFreshConn:
    """A brand-new connection -- succeeds if the code ever tries one."""

    def __init__(self, *a, **k):
        self.closed = False
        self.connected = False

    def connect(self):
        self.connected = True

    def getresponse(self):
        return _FakeRelayResp()

    def close(self):
        self.closed = True


def test_relay_reconnects_instead_of_502ing_when_pool_is_idle_expired(monkeypatch):
    """proxy.py's `_relay` retry loop (~326-346) calls `self._acquire(server)`
    on its one retry, and `_acquire` always prefers `server.ctx_pool` over
    building a fresh connection. So when the pool holds 2+ idle-expired
    connections, BOTH attempts of `for _attempt in (0, 1)` draw from the
    pool, both fail, and the handler gives up with a 502 -- without ever
    constructing a fresh connection, even though one (made to trivially
    succeed here) was available the whole time.
    """
    import http.client
    from types import SimpleNamespace
    import ctx.proxy as proxy

    fresh_created = []

    def _make_fresh(*a, **k):
        fresh_created.append(1)
        return _FakeFreshConn(*a, **k)

    monkeypatch.setattr(http.client, "HTTPSConnection", _make_fresh)

    handler = object.__new__(proxy._RelayHandler)
    handler.command = "POST"
    handler.path = "/v1/messages"
    handler.headers = {}
    handler.close_connection = False
    handler._read_request_body = lambda: b""
    handler.send_response_only = lambda *a, **k: None
    handler.send_header = lambda *a, **k: None
    handler.end_headers = lambda *a, **k: None

    stale_a, stale_b = _FakeStaleConn(), _FakeStaleConn()
    handler.server = SimpleNamespace(
        ctx_rescue=None,
        ctx_observer=None,
        ctx_pool_lock=threading.Lock(),
        ctx_pool=[stale_a, stale_b],
        ctx_upstream=SimpleNamespace(
            scheme="https", hostname="api.example.com", port=443,
            path="", netloc="api.example.com",
        ),
        ctx_ssl=None,
    )

    def _send_upstream(server, conn, body):
        if conn in (stale_a, stale_b):
            raise ConnectionResetError("simulated idle-expired pooled connection")

    handler._send_upstream = _send_upstream

    bad_gateway_calls = []
    handler._bad_gateway = lambda: bad_gateway_calls.append(1)

    handler._relay()

    assert fresh_created, (
        "_relay never constructed a fresh HTTPSConnection: both retry "
        "attempts re-acquired from the idle-expired pool instead of "
        "forcing a fresh connection"
    )
    assert not bad_gateway_calls, (
        "_relay answered 502 even though a fresh connection was "
        f"available and would have succeeded (bad_gateway_calls={bad_gateway_calls!r})"
    )


# =====================================================================
# Finding 23 -- src/ctx/pathglob.py:43
# =====================================================================
def test_matches_normalizes_leading_dot_slash_off_pattern_too():
    """matches() strips a leading './' off `rel` (pathglob.py:43,
    `rel = rel.removeprefix("./")`) but never off `pattern`, so a selector
    written as './src/*.py' silently matches nothing even though the
    equivalent pattern without the prefix matches fine.
    """
    from ctx import pathglob

    assert pathglob.matches("src/a.py", "src/*.py") is True
    assert pathglob.matches("src/a.py", "./src/*.py") is True


# =====================================================================
# Finding 24 -- src/ctx/digest/jsonprof.py:44
# =====================================================================
def test_dominant_array_prefix_is_rfc6901_escaped():
    """_dominant_array (jsonprof.py:44, `return v, f"/{k}"`) builds a JSON
    Pointer prefix from a raw object key without RFC 6901 ~0/~1 escaping.
    A key containing '/' (e.g. "src/a.py") then yields a pointer prefix
    that resolves to the wrong (nonexistent) location.
    """
    from ctx.digest.jsonprof import _dominant_array
    from ctx.textutil import json_pointer, JsonPointerError

    doc = json.loads('{"src/a.py": [1, 2, 3, 4, 5, 6, 7]}')
    _array, prefix = _dominant_array(doc)

    assert prefix == "/src~1a.py", (
        f"_dominant_array emitted an unescaped JSON pointer prefix {prefix!r}"
    )
    assert json_pointer(doc, prefix + "/5") == 6

    assert json_pointer(doc, "/src~1a.py/5") == 6
    try:
        json_pointer(doc, "/src/a.py/5")
    except JsonPointerError:
        pass
    else:
        raise AssertionError("expected the unescaped pointer to fail to resolve")


# =====================================================================
# Finding 25 -- src/ctx/binfmt.py:218
# =====================================================================
def test_inspect_sniffs_with_a_window_wide_enough_to_match_sniff_format():
    """inspect() (binfmt.py:218, `fmt = sniff_format(data[:64])`) only
    passes the first 64 bytes to sniff_format, while sniff_format's own
    NUL heuristic (binfmt.py:73, `if b"\\x00" in head[:1024]`) is written
    against a 1 KiB window. A magic-less binary blob whose first NUL byte
    lands past byte 64 is therefore classified as binary by
    sniff_format(data) directly, but as text by inspect(data) -- the two
    should agree.
    """
    from ctx.binfmt import inspect, sniff_format

    data = b"A" * 100 + b"\x00" * 50
    assert sniff_format(data) == "binary"
    assert inspect(data).format == "binary", (
        "inspect() disagreed with sniff_format() on the same bytes: "
        "it only sniffed the first 64 bytes, missing the NUL at offset 100 "
        "that sniff_format's own 1 KiB window would have caught"
    )


# =====================================================================
# Finding 26 -- src/ctx/statusline.py:311
# =====================================================================
def test_codex_rollout_summary_treats_cached_input_as_subset(tmp_path):
    """Codex reports cached_input_tokens as a SUBSET of input_tokens, so
    pricing must charge (input - cached) as fresh input, not the full
    input_tokens on top of the cache_read charge."""
    from ctx import pricing, statusline

    model = "gpt-5.3-codex"
    roll = tmp_path / "rollout.jsonl"
    roll.write_text("\n".join([
        json.dumps({"payload": {"model": model}}),
        json.dumps({"payload": {"info": {"total_token_usage": {
            "input_tokens": 100_000,
            "cached_input_tokens": 90_000,
            "output_tokens": 2_000,
        }}}}),
    ]), encoding="utf-8")

    line = statusline.codex_rollout_summary(roll)
    m = re.search(r"\$([0-9.,]+)", line)
    assert m, f"no dollar figure found in {line!r}"
    reported_cost = float(m.group(1).replace(",", ""))

    price = pricing.price_for(model)
    correct_cost = price.cost_usd(
        input_tokens=10_000,       # 100_000 - 90_000 cached (subset, not extra)
        cache_read_tokens=90_000,
        output_tokens=2_000,
    )
    buggy_cost = price.cost_usd(
        input_tokens=100_000,      # double-charges the cached tokens
        cache_read_tokens=90_000,
        output_tokens=2_000,
    )
    assert correct_cost != buggy_cost, "test setup must distinguish the two"
    assert abs(reported_cost - correct_cost) < 0.001, (
        f"reported cost {reported_cost} should match the subtracted cost "
        f"{correct_cost}, not the double-counted {buggy_cost}"
    )


# =====================================================================
# Finding 27 -- src/ctx/evidence_outcomes.py:314
# =====================================================================
def test_failing_ids_preserves_parametrized_bracket_suffix():
    """A parametrized pytest node id like `tests/test_a.py::test_f[case1]`
    must survive into EvidenceEmission.failing_ids, not be dropped because
    _NODEID_RE truncates the bracketed part while _FAILING_ID_RE keeps it."""
    from ctx.evidence_outcomes import emissions_from_calls

    calls = [
        {
            "tool": "Bash",
            "input": {"command": "pytest -v tests/test_a.py"},
            "result": "tests/test_a.py::test_f[case1] FAILED\n",
        }
    ]
    emissions = emissions_from_calls(calls)
    assert len(emissions) == 1
    em = emissions[0]
    assert em.failing_ids, "expected a non-empty failing_ids set"
    assert "tests/test_a.py::test_f[case1]" in em.failing_ids


# =====================================================================
# Finding 28 -- src/ctx/replay.py:290
# =====================================================================
def _tool_use_f28(name, inp, uid):
    return {
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "id": uid, "name": name, "input": inp}]},
    }


def _tool_result_f28(uid, text):
    return {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "tool_use_id": uid, "content": text}]},
    }


def _write_transcript_f28(path, entries):
    path.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")


# =====================================================================
# Finding 29 -- src/ctx/callgraph.py:481
# =====================================================================
_SRC_F29 = {
    "src/conftest.py": "",  # loose top-level file that must NOT poison "src"
    "src/ctx/__init__.py": "",
    "src/ctx/store.py": "def helper():\n    return 1\n",
    "src/ctx/user.py": (
        "from ctx.store import helper\n\n\n"
        "def use():\n"
        "    return helper()\n"
    ),
}


def _make_ws_f29(tmp_path, monkeypatch):
    monkeypatch.setenv("CTX_STATE_HOME", str(tmp_path / "state"))
    from ctx.store import Store
    from ctx.workspace import resolve_workspace

    d = tmp_path / "proj"
    d.mkdir()
    (d / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    for rel, content in _SRC_F29.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    subprocess.run(["git", "init", "-q", "."], cwd=d, check=True)
    ws = resolve_workspace(str(d))
    return ws, Store(ws.workspace_id)


def test_loose_top_level_file_does_not_poison_src_as_package_root(tmp_path, monkeypatch):
    """A loose `src/conftest.py` sitting next to the real nested package
    `src/ctx/` must not turn "src" itself into the registered package root.
    `ctx.user.use` calls `ctx.store.helper` via `from ctx.store import
    helper`, a genuine direct import, so that edge must resolve at the
    scoped "import" tier -- not fall back to the unscoped "repo" tier."""
    from ctx.callgraph import _load_graph

    ws, store = _make_ws_f29(tmp_path, monkeypatch)
    g = _load_graph(store, ws)

    helper_id = next(n for n, d in g.nodes.items() if d.qual == "helper")
    edges = [c for c in g.in_edges.get(helper_id, []) if g.nodes[c[0]].qual == "use"]
    assert edges, "use() must have a resolved edge to helper()"
    assert any(c[2] == "import" for c in edges), (
        f"expected an 'import'-tier edge from use() to helper(), got tiers "
        f"{[c[2] for c in edges]!r} -- 'src' was likely poisoned into a "
        f"pkg_dir by the loose src/conftest.py file"
    )


_SRC_NESTED = {
    "src/conftest.py": "",
    "src/pkg/__init__.py": "",
    "src/pkg/sub/__init__.py": "",
    "src/pkg/sub/store.py": "def helper():\n    return 1\n",
    "src/pkg/user.py": "from pkg.sub.store import helper\n\n\ndef use():\n    return helper()\n",
}


def test_nested_package_keeps_its_outer_prefix_beside_the_loose_root(tmp_path, monkeypatch):
    """Codex review of the fix above: deepest-first keyed `src/pkg/sub/store.py`
    only as `sub.store`, so `from pkg.sub.store import helper` fell to the
    unscoped tier. Every package root the file is importable from is
    registered: `pkg.sub.store` resolves at the import tier, and the loose
    `src/conftest.py` still does not make `src.pkg.sub.store` the only name."""
    monkeypatch.setenv("CTX_STATE_HOME", str(tmp_path / "state"))
    from ctx.callgraph import _load_graph
    from ctx.store import Store
    from ctx.workspace import resolve_workspace

    d = tmp_path / "proj"
    d.mkdir()
    (d / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    for rel, content in _SRC_NESTED.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    subprocess.run(["git", "init", "-q", "."], cwd=d, check=True)
    ws = resolve_workspace(str(d))
    g = _load_graph(Store(ws.workspace_id), ws)
    helper_id = next(n for n, dd in g.nodes.items() if dd.qual == "helper")
    edges = [c for c in g.in_edges.get(helper_id, []) if g.nodes[c[0]].qual == "use"]
    assert edges and any(c[2] == "import" for c in edges), [c[2] for c in edges]


# =====================================================================
# Finding 30 -- src/ctx/callgraph.py:582
# =====================================================================
_SRC_F30 = {
    "dira/profile.py": "class Profile:\n    pass\n",
    "dirb/profile.py": "class Profile:\n    pass\n",
    "dirc/logprofile.py": "class LogProfile(Profile):\n    pass\n",
}


def _make_ws_f30(tmp_path, monkeypatch):
    monkeypatch.setenv("CTX_STATE_HOME", str(tmp_path / "state"))
    from ctx.store import Store
    from ctx.workspace import resolve_workspace

    d = tmp_path / "proj"
    d.mkdir()
    (d / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    for rel, content in _SRC_F30.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    subprocess.run(["git", "init", "-q", "."], cwd=d, check=True)
    ws = resolve_workspace(str(d))
    return ws, Store(ws.workspace_id)


def test_unscoped_inheritance_edge_is_disclosed(tmp_path, monkeypatch):
    """LogProfile's base `Profile` cannot be scoped -- it is not imported
    from either candidate module, so the match is a repo-wide, unscoped,
    ambiguous name match. Like every other verb (callers/callees/impact),
    `impls` must disclose that the subtype relation is unscoped rather than
    silently attaching LogProfile as a real subclass of both unrelated
    Profile classes with no marker at all."""
    from ctx.callgraph import cmd_impls, _load_graph

    ws, store = _make_ws_f30(tmp_path, monkeypatch)

    g = _load_graph(store, ws)
    log_id = next(n for n, d in g.nodes.items() if d.qual == "LogProfile")
    profile_ids = [n for n, d in g.nodes.items() if d.qual == "Profile"]
    assert len(profile_ids) == 2
    attached_to = [p for p in profile_ids if log_id in g.subclasses.get(p, [])]
    assert len(attached_to) == 2, (
        "expected LogProfile silently attached as a subclass of BOTH "
        "unrelated Profile classes via unscoped name resolution"
    )

    out = cmd_impls(store, ws, "Profile")
    assert "LogProfile" in out
    assert "unscoped" in out.lower(), (
        "impls must disclose that LogProfile's inheritance edge is an "
        "unscoped/ambiguous name match, the same way callers/callees/impact "
        "disclose unscoped edges -- currently no such marker exists"
    )


# =====================================================================
# Finding 31 -- src/ctx/scip_ingest.py:147
# =====================================================================
def test_scip_refs_bare_name_match_conflates_unrelated_symbols(
    state_home, workspace_dir, monkeypatch
):
    """SCIP reference lookup matches on the bare last identifier of a
    dotted query, so 'Profile.render' collides with an unrelated
    'Widget.render'. Protobuf ([scip] extra) is not installed in this
    environment, so the protobuf-dependent seams (`available`,
    `iter_occurrences`) are monkeypatched; the bare-name filter itself
    (`if occ.name != want: continue`) runs unmodified against two
    hand-built Occurrence instances."""
    from ctx import scip_ingest
    from ctx.scip_ingest import Occurrence

    ws = make_ws(workspace_dir)
    (workspace_dir / "pkg").mkdir()
    (workspace_dir / "pkg" / "core.py").write_text(
        "class Profile:\n    def render(self):\n        return 1\n",
        encoding="utf-8",
    )
    (workspace_dir / "pkg" / "other.py").write_text(
        "class Widget:\n    def render(self):\n        return 2\n",
        encoding="utf-8",
    )
    (workspace_dir / "index.scip").write_bytes(b"")

    occ_profile = Occurrence(
        file="pkg/core.py", line=2, col_a=9, col_b=15,
        symbol="scip-python python scipproj 0.0.1 `pkg.core`/Profile#render().",
        name="render", is_definition=True,
    )
    occ_widget = Occurrence(
        file="pkg/other.py", line=2, col_a=9, col_b=15,
        symbol="scip-python python scipproj 0.0.1 `pkg.other`/Widget#render().",
        name="render", is_definition=True,
    )

    monkeypatch.setattr(scip_ingest, "available", lambda: True)
    monkeypatch.setattr(
        scip_ingest, "iter_occurrences", lambda index_path: iter([occ_profile, occ_widget])
    )

    hits = scip_ingest.refs(ws, "Profile.render")
    files_hit = {f for f, _ln, _text in hits}

    assert "pkg/core.py" in files_hit
    assert "pkg/other.py" not in files_hit, (
        "querying 'Profile.render' matched the unrelated 'Widget.render' "
        "occurrence too -- refs() only compares the bare last identifier "
        "('render'), ignoring the qualifier, so unrelated symbols sharing a "
        "method name collide"
    )


# =====================================================================
# Finding 32 -- src/ctx/skeleton.py:536
# =====================================================================
_TRY_SOURCE_F32 = (
    "try:\n"
    "    class FastImpl:\n"
    "        def run(self):\n"
    "            pass\n"
    "except ImportError:\n"
    "    class FastImpl:\n"
    "        def run(self):\n"
    "            pass\n"
)


def test_ast_skeleton_sees_definitions_nested_in_try_except(
    state_home, workspace_dir, monkeypatch
):
    """The ast-based skeleton backend's statement walker only recurses into
    module top-level and class bodies -- never into try/except (or if/with)
    bodies -- so a class/def defined only inside a `try:`/`except:` block
    (a very common conditional-import pattern) is completely invisible to
    the skeleton census."""
    import ctx.skeleton as skel

    def raiser(*a, **k):
        raise skel.BackendUnavailable("disabled for test")

    monkeypatch.setattr(skel, "_tree_sitter_extract", raiser)
    monkeypatch.setattr(skel, "_ctags_path", lambda: None)

    (workspace_dir / "sample.py").write_text(_TRY_SOURCE_F32, encoding="utf-8")
    ws = make_ws(workspace_dir)
    store = make_store(ws)

    sk = skel.skeleton_for(store, ws, "sample.py")
    assert sk["parser"] == "ast"
    names = {s["name"] for s in sk["symbols"]}
    assert "FastImpl" in names, (
        f"FastImpl (defined only inside try/except) missing from skeleton "
        f"symbols: {sorted(names)!r}"
    )
    assert "run" in names, (
        f"run (defined only inside try/except) missing from skeleton "
        f"symbols: {sorted(names)!r}"
    )


# =====================================================================
# Finding 33 -- src/ctx/ladders.py:549
# =====================================================================
def test_report_corpus_handles_an_unmeasurable_ladder_over_an_empty_corpus(tmp_path):
    """An empty directory (no workspaces at all) still contains ladders with
    signal=None (e.g. "solution"), which are unmeasurable. measure_corpus
    must return the same {"reason": ...} shape that measure() does for a
    single workspace, so report_corpus can render "not scored: <reason>"
    instead of raising KeyError."""
    from ctx import ladders as L

    report = L.report_corpus(tmp_path)
    assert "not scored" in report


# =====================================================================
# Finding 34 -- src/ctx/seq.py:63
# =====================================================================
@pytest.fixture()
def ws_store_f34(tmp_path, monkeypatch):
    monkeypatch.setenv("CTX_STATE_HOME", str(tmp_path / "state"))
    from ctx.store import Store
    from ctx.workspace import resolve_workspace

    d = tmp_path / "proj"
    d.mkdir()
    (d / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "."], cwd=d, check=True)
    return resolve_workspace(str(d)), Store("ws_seq_test")


def test_seq_keep_going_survives_a_step_that_fails_to_spawn(ws_store_f34, monkeypatch):
    """halt_on_fail=False (--keep-going) must run every step even when an
    early one raises ExecutionError (fails to start/spawn, e.g. the shell
    itself cannot be exec'd) -- not just when a step starts and exits
    nonzero. The `except ExecutionError` branch in run_seq currently
    `break`s unconditionally, ignoring halt_on_fail."""
    import ctx.execution as execmod
    import ctx.seq as seqmod

    real_popen = execmod.subprocess.Popen

    def fake_popen(popen_args, *a, **kw):
        if isinstance(popen_args, str) and "boom-step-one" in popen_args:
            raise FileNotFoundError("simulated spawn failure")
        return real_popen(popen_args, *a, **kw)

    monkeypatch.setattr(execmod.subprocess, "Popen", fake_popen)

    ws, store = ws_store_f34
    text, code, _timed_out = seqmod.run_seq(
        ws, store, ["boom-step-one", "echo survivor"], halt_on_fail=False
    )

    assert "step 2" in text, (
        "step 2 should have run under --keep-going even though step 1 "
        f"failed to start; got:\n{text}"
    )
    assert "survivor" in text


# =====================================================================
# Finding 35 -- src/ctx/surface.py:900
# =====================================================================
def test_probe_surface_does_not_serialize_unboundedly_past_its_own_timeout(tmp_path):
    """probe_surface (called from the SessionStart preflight hook, which the
    host registers with a 15s timeout in installer.claude_hook_settings)
    probes each configured MCP server one at a time via subprocess.run(...,
    timeout=...), so N slow/hanging servers cost N * timeout wall-clock time
    instead of bounded-by-timeout total time."""
    from ctx import surface as S

    (tmp_path / ".mcp.json").write_text(json.dumps({
        "mcpServers": {
            "slow-a": {"command": "sleep", "args": ["9"]},
            "slow-b": {"command": "sleep", "args": ["9"]},
        }
    }), encoding="utf-8")

    t0 = time.time()
    S.probe_surface(tmp_path, timeout=1.0, use_cache=False)
    elapsed = time.time() - t0

    assert elapsed < 1.5, (
        f"probing 2 hanging MCP servers at a 1.0s timeout took {elapsed:.2f}s "
        "-- probes are running serially (~N * timeout) rather than bounded "
        "by a shared budget, which will blow the 15s SessionStart hook "
        "timeout as the number of configured servers grows"
    )


# =====================================================================
# Finding 36 -- src/ctx/surface_reconcile.py:138
# =====================================================================
def test_required_families_recognizes_already_decoded_server_names(tmp_path):
    """surface.enrich_graph populates Capability.requires with BARE decoded
    server names (e.g. "github"), extracted by matching _MCP_REF_RE against
    raw prose (which requires an "mcp__"/"mcp." prefix) and stripping that
    prefix off. required_families then re-runs the SAME prefixed regex over
    " ".join(c.requires) -- but requires no longer carries the prefix, so a
    plain server reference like "github" can never match and the family is
    never recognized as required."""
    from ctx import surface
    from ctx import surface_reconcile as sr

    skill_cap = surface.Capability(
        id="skill.foo", kind="skill", provider="foo",
        source="skills/foo/SKILL.md", tokens=100, requires=("github",),
    )
    server_cap = surface.Capability(
        id="mcp.github", kind="mcp_server", provider="github",
        source="mcp:github", tokens=50, family="remote-source-control",
    )

    result = sr.required_families(tmp_path, [skill_cap, server_cap])

    assert "remote-source-control" in result


# =====================================================================
# Finding 37 -- src/ctx/digest/evidence_render.py:445
# =====================================================================
def _make_evidence_graph(n):
    from ctx.evidence import EvidenceGraph, EvidenceItem

    items = tuple(
        EvidenceItem(
            id=f"test_mod_{i}.py::test_case_{i}",
            kind="test_failure",
            severity="error",
            summary=f"AssertionError: case {i} failed",
            failure_class="AssertionError",
            location=f"test_mod_{i}.py:{10 + i}",
            causal_rank=i,
        )
        for i in range(n)
    )
    return EvidenceGraph(
        family="pytest", profile_version="v2", outcome="fail",
        aggregate={"failed": n, "passed": 0, "skipped": 0, "error": 0,
                   "xfailed": 0, "xpassed": 0},
        items=items, artifacts={},
        coverage={"parsed": n, "total_estimate": n, "complete": True},
    )


def test_flood_render_scales_linearly_not_quadratically_with_item_count():
    """_render_flood walks k linearly downward from len(items) with a tiny
    budget, calling build(k) -- which re-renders and re-encodes the WHOLE
    census -- at every single value of k on the way down. That makes the
    flood path O(n^2) in the number of failing items: doubling the item
    count should double the render time for a linear implementation, but
    roughly quadruples it here."""
    from ctx.contracts import contract_for_family
    from ctx.digest.evidence_render import DefaultPlan, render_fail_evidence

    contract = contract_for_family("pytest")
    plan = DefaultPlan(mode="flood", token_budget=5)  # forces k all the way to 1

    g_small = _make_evidence_graph(250)
    t0 = time.perf_counter()
    render_fail_evidence(g_small, contract, plan)
    small_elapsed = time.perf_counter() - t0

    g_large = _make_evidence_graph(500)
    t0 = time.perf_counter()
    render_fail_evidence(g_large, contract, plan)
    large_elapsed = time.perf_counter() - t0

    ratio = large_elapsed / max(small_elapsed, 1e-9)
    assert ratio < 3.0, (
        f"doubling item count 250->500 took {ratio:.2f}x longer "
        f"({small_elapsed:.3f}s -> {large_elapsed:.3f}s); expected roughly "
        "2x for a linear flood render, not the ~4x quadratic blowup from "
        "rebuilding the whole census at every k on the way down"
    )


# =====================================================================
# Finding 38 -- src/ctx/scorecard.py:721
# =====================================================================
def test_scorecard_counts_files_and_lines_in_a_new_untracked_directory(git_workspace):
    """git collapses a whole new untracked directory into one porcelain
    entry (`?? newpkg/`) instead of listing each file inside it.
    attach_deliverable treats that entry as an ordinary file: `p.is_file()`
    is False for a directory, so the walk that sums lines_new skips it
    entirely, and files_new is left at 1 (the single collapsed entry)
    instead of the 3 real files a creation task actually wrote."""
    from ctx.scorecard import attach_deliverable

    pkg = git_workspace / "newpkg"
    pkg.mkdir()
    (pkg / "a.py").write_text("line1\nline2\nline3\n", encoding="utf-8")
    (pkg / "b.py").write_text("line1\nline2\n", encoding="utf-8")
    (pkg / "c.py").write_text("line1\nline2\nline3\nline4\n", encoding="utf-8")

    sc = attach_deliverable({}, git_workspace)

    assert sc["deliverable"]["files_new"] == 3
    assert sc["deliverable"]["lines_new"] == 9


# =====================================================================
# Finding 39 -- src/ctx/reflex.py:312
# =====================================================================
def test_redirection_targets_do_not_change_the_signature():
    """_signature_parts drops the redirection operator token (">", "2>&1",
    ...) via _META_TOKENS, but the very next token -- the redirect's
    target filename -- does not start with "-" and is not itself in
    _META_TOKENS, so it falls through and gets appended as if it were a
    positional argument. A command with a stdout/stderr redirect therefore
    gets a DIFFERENT signature than the same command without one, even
    though redirection doesn't change what is being run."""
    from ctx.reflex import command_signature

    base = command_signature("pytest tests/x.py")
    assert base == "pytest tests/x.py"

    redirected_file = command_signature("pytest tests/x.py > out.log")
    redirected_devnull = command_signature("pytest tests/x.py > /dev/null 2>&1")

    assert redirected_file == base
    assert redirected_devnull == base


# =====================================================================
# Finding 40 -- src/ctx/plan_ir.py:241
# =====================================================================
def test_infinite_budget_degrades_to_rejection_not_overflow():
    """_budget_int does not catch OverflowError, so a budget value of
    Infinity (which json.loads accepts by default) raises out of
    validate_plan instead of degrading to a typed Rejection."""
    from ctx.plan_ir import parse_plan, validate_plan

    doc = {
        "version": "ctx.plan/v1",
        "objective": {"kind": "diagnose", "question": "why?"},
        "budget": {"max_nodes": float("inf")},
        "steps": [{"id": "a", "op": "repo.changed"}],
    }
    plan = parse_plan(doc)
    rejections = validate_plan(plan)  # currently raises OverflowError
    assert rejections, "expected typed rejection(s) for an infinite budget value"


# =====================================================================
# Finding 41 -- src/ctx/commands/emit.py:151
# =====================================================================
@pytest.fixture
def ws_store_f41(state_home, workspace_dir):
    ws = make_ws(workspace_dir)
    return ws, make_store(ws)


def test_investigation_with_candidates_is_not_classified_failure(ws_store_f41, monkeypatch):
    """A digest reporting a successful census (candidates found) must not
    ride the failure budget just because the literal string
    'candidates (census): 0' happens not to appear."""
    from ctx.commands import emit as emit_mod

    ws, store = ws_store_f41
    captured = {}

    def fake_delivery_plan(ws, *, outcome, family, base_tokens, signature=None):
        captured["outcome"] = outcome
        return object()

    monkeypatch.setattr(emit_mod, "_delivery_plan", fake_delivery_plan)
    monkeypatch.setattr(emit_mod, "_emit_bounded_digest", lambda *a, **k: None)

    text = "conclusion candidates (census): 5\nsome findings here"
    emit_mod._emit_investigation(ws, store, text)

    assert captured["outcome"] == "success"


# =====================================================================
# Finding 42 -- src/ctx/steward.py:78
# =====================================================================
def _classify_failure(**kw):
    from ctx.steward import classify_failure

    base = dict(code=1, stdout="", stderr="", turns=0, attempt=1, expected_turns=12,
                contract_failed=True)
    base.update(kw)
    return classify_failure(**base)


def test_bare_401_substring_in_unrelated_output_is_not_auth_failure():
    """A stack trace or test name that happens to contain the three digits
    '401' (e.g. a source line number or a test-name like
    'test_401_unauthorized') must not be classified as an auth failure --
    only genuine authentication wording should trigger auth_failure, which
    the recovery policy maps to stop_blocked (no retry)."""
    c = _classify_failure(stdout='File "api.py", line 401, in handler\n    raise ValueError("boom")')
    assert c.failure_kind != "auth_failure"


# =====================================================================
# Finding 43 -- src/ctx/facts.py:898
# =====================================================================
def test_newest_captured_matches_fail_table_run_id_format(tmp_path, state_home):
    """_newest_captured must return an id in the same id-space that
    fail.run_id rows are keyed on (the 12-hex short id from derive_run),
    or the fails_sites fast lookup can never match an existing row."""
    import ctx.execution as execution
    import ctx.facts as facts

    proj = tmp_path / "proj"
    (proj / "tests").mkdir(parents=True)
    (proj / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    (proj / "tests" / "test_seed.py").write_text(
        "def test_pass():\n    assert True\n\n\n"
        "def test_boom():\n    assert 1 == 2\n",
        encoding="utf-8",
    )

    ws = make_ws(proj)
    store = make_store(ws)

    cap = execution.run_capture(
        ws, [sys.executable, "-m", "pytest", "tests", "-q"], store=store,
    )
    run = facts.derive_run(store, ws, cap.manifest)
    assert run["ok"], run
    short_run_id = run["run"]
    assert len(short_run_id) == 12, short_run_id

    conn = facts._connect(store)
    fail_rows = conn.execute(
        "SELECT run_id, test FROM fail WHERE run_id=?", (short_run_id,)
    ).fetchall()
    conn.close()
    assert fail_rows, "expected a fail row keyed on the 12-hex short run id"
    assert fail_rows[0][1].endswith("::test_boom")

    newest = facts._newest_captured(store)
    assert newest is not None
    assert newest != short_run_id
    assert len(newest) == 64, f"expected a full 64-hex object id, got {newest!r}"

    matched = facts._fails_for(store, newest)
    assert matched, (
        f"_newest_captured() returned {newest!r} (64-hex) which does not "
        f"match any fail.run_id row (stored short id: {short_run_id!r})"
    )


# =====================================================================
# Finding 44 -- src/ctx/query.py:590
# =====================================================================
def test_search_line_numbers_match_house_nl_only_geometry(git_workspace, state_home):
    """ctx q search reports line numbers using str.splitlines() geometry,
    which disagrees with the \\n-only geometry that store.line_index (and
    therefore `ctx get --lines`) uses whenever content contains a
    non-\\n line-break character (form feed, vertical tab, CR, etc).
    The line number a search hit reports must be resolvable via the same
    \\n-only addressing ctx get uses."""
    from ctx.query import run_query
    from ctx.store import Store
    from ctx.textutil import index_lines

    content = "a\n\x0cb\nTARGET\n"
    (git_workspace / "weird.txt").write_text(content, encoding="utf-8", newline="")
    subprocess.run(["git", "add", "-A"], cwd=git_workspace, check=True)
    subprocess.run(["git", "commit", "-qm", "add weird file"], cwd=git_workspace, check=True)

    ws = make_ws(git_workspace)
    store = Store(ws.workspace_id)

    out, code = run_query(ws, store, "search TARGET")
    assert code == 0

    hit_line = None
    for line in out.splitlines():
        if "weird.txt:L" in line:
            hit_line = int(line.split(":L", 1)[1].split(":", 1)[0])
    assert hit_line is not None, out

    house_line = index_lines(content).index("TARGET") + 1

    assert hit_line == house_line, (
        f"search reported line {hit_line} (str.splitlines() geometry) but "
        f"the house \\n-only geometry (ctx get --lines) puts TARGET at "
        f"line {house_line} -- ctx get --lines {hit_line} would not "
        f"retrieve the reported hit"
    )


# =====================================================================
# Finding 45 -- src/ctx/_retrieval/get.py:574
# =====================================================================
def test_json_pointer_truncation_handle_preserves_selector(state_home, workspace_dir):
    """A budget-truncated --json-pointer answer's `next:` continuation must
    still address the same json-pointer selector (with an offset/next
    fragment) -- not collapse to a bare ref that re-reads the blob as
    head lines and drops the pointer entirely."""
    from ctx.execution import run_capture
    from ctx.retrieval import Selector, get

    ws = make_ws(workspace_dir)
    store = make_store(ws)

    blob = {
        "rows": [
            {"id": i, "name": f"item-{i}", "value": i * 3.14159, "note": "x" * 20}
            for i in range(200)
        ]
    }
    script = "import json,sys; sys.stdout.write(json.dumps(" + repr(blob) + "))"
    cap = run_capture(ws, [sys.executable, "-c", script], store=store)

    out = get(
        store, ws, f"run:{cap.manifest_id[:12]}#stdout",
        Selector(json_pointer="/rows"),
    )

    assert "truncated" in out, "expected the /rows answer to exceed the budget"
    next_lines = [ln for ln in out.splitlines() if ln.startswith("next:")]
    assert next_lines, out
    assert "--json-pointer" in next_lines[0], (
        f"truncation continuation dropped the json-pointer selector: {next_lines[0]!r}"
    )


# =====================================================================
# Finding 46 -- src/ctx/_retrieval/targets.py:143
# =====================================================================
def test_resolve_repo_targets_considered_count_discloses_the_cap(git_workspace, state_home):
    """When the file list is truncated to max_files, the returned
    'considered' count must not silently claim the post-cap size as the
    total -- callers reporting `considered` as "how many files we looked
    at" would otherwise never learn that files were dropped."""
    from ctx._retrieval.targets import _resolve_repo_targets
    from ctx.refs import parse_ref

    for i in range(10):
        (git_workspace / f"f{i}.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=git_workspace, check=True)
    subprocess.run(["git", "commit", "-qm", "add files"], cwd=git_workspace, check=True)

    ws = make_ws(git_workspace)
    store = make_store(ws)

    targets, considered, skipped_binary = _resolve_repo_targets(
        store, ws, parse_ref("repo:"), glob=None, scope=None, max_files=3,
    )

    assert len(targets) == 3  # the cap is applied, as intended
    assert considered != 3, (
        f"'considered' reports the post-cap size ({considered}) with no way "
        f"to tell the {11 - 3} dropped files were ever there"
    )


# =====================================================================
# Finding 47 -- src/ctx/command_spans.py:76
# =====================================================================
def test_git_diff_patch_with_stat_is_classified_as_capture_not_allow():
    from ctx.command_spans import classify_command_span

    result = classify_command_span(["git", "diff", "--stat", "--patch-with-stat"])
    assert result == "capture"


# =====================================================================
# Finding 48 -- src/ctx/catalog.py:64
# =====================================================================
def test_workspace_override_with_new_specific_match_beats_general_shipped_row(tmp_path):
    """A repo override introducing a new, more specific `match` (not already
    present in the shipped table) must be tried before the shipped table's
    more general row, per entry_for's documented specific->general first-match
    order. `claude-opus` ships as a general row; overriding the specific id
    `claude-opus-4.6` should make entry_for return the override, not the
    shipped `claude-opus` row.
    """
    from ctx import catalog

    (tmp_path / ".ctx-catalog.json").write_text(json.dumps({
        "models": [{"match": "claude-opus-4.6", "latency_class": "fast",
                    "latency_source": "local measurement"}]
    }))
    assert catalog.latency_class("claude-opus-4.6", workspace_root=tmp_path) == "fast"


# =====================================================================
# Finding 49 -- src/ctx/query.py:810
# =====================================================================
def test_unscoped_omission_reason_survives_group_and_top(state_home, tmp_path):
    """_stage_callers attaches the specific _UNSCOPED_REASON to an omission
    it makes, but every downstream combinator (_stage_group, _stage_top, ...)
    rebuilds Stream carrying `omitted` while dropping `omitted_reason`. So a
    query that pipes an unscoped-edge omission through `group`/`top` loses
    the actionable "resolve with --unscoped" remedy and reports the generic
    fallback text instead, even though the omission is still there.
    """
    from ctx.query import run_query

    (tmp_path / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    (tmp_path / "a.py").write_text("def shared():\n    return 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text(
        "def caller():\n    return shared()\n", encoding="utf-8"
    )
    ws = make_ws(tmp_path)
    store = make_store(ws)

    direct, _ = run_query(ws, store, "callers shared")
    piped, _ = run_query(ws, store, "callers shared | group file | top 3")

    assert "resolve with --unscoped" in direct
    assert "resolve with --unscoped" in piped


# =====================================================================
# Finding 50 -- src/ctx/jobs.py:410
# =====================================================================
def test_kill_job_shared_deadline_fails_a_successful_kill_after_slow_start(tmp_path, monkeypatch):
    """kill_job computes ONE `deadline = time.monotonic() + settle_s` and reuses
    it for both the wait-for-pid phase and the post-signal settle phase. If the
    job is slow to record its pid (consuming most/all of settle_s), the
    post-kill settle loop's `while time.monotonic() < deadline` runs ZERO
    iterations regardless of whether the kill actually worked, so kill_job
    raises JobError reporting failure for a kill that succeeded and would have
    settled almost immediately if given any time budget at all.
    """
    import ctx.jobs as jobs

    settle_s = 2.0
    fake_time = {"t": 0.0}

    def fake_monotonic():
        return fake_time["t"]

    read_calls = {"n": 0}

    def fake_read_meta(jobdir):
        read_calls["n"] += 1
        n = read_calls["n"]
        if n == 1:
            return {}
        elif n == 2:
            return {"pid": 4242, "state": "running"}
        else:
            return {"pid": 4242, "state": "done"}

    def fake_sleep(_seconds):
        fake_time["t"] += settle_s

    jobdir = tmp_path / "job"
    jobdir.mkdir()
    finalize_sentinel = ("deadbeef0000", {"result": {"ok": True}})

    monkeypatch.setattr(jobs.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(jobs.time, "sleep", fake_sleep)
    monkeypatch.setattr(jobs, "_read_meta", fake_read_meta)
    monkeypatch.setattr(jobs, "_job_dir", lambda store, job_id: jobdir)
    monkeypatch.setattr(jobs.os, "killpg", lambda pid, sig: None)
    monkeypatch.setattr(jobs, "finalize_job", lambda ws, store, job_id: finalize_sentinel)

    result = jobs.kill_job(None, None, "deadbeef", settle_s=settle_s)
    assert result == finalize_sentinel


# =====================================================================
# Finding 51 -- src/ctx/plan_ops.py:714
# =====================================================================
def test_code_context_explicit_zero_is_not_silently_widened_to_default(state_home, workspace_dir):
    """code.context computes `context = max(0, int(args.get("context", 3) or 3))`.
    The `or 3` half of that idiom treats an explicit 0 the same as "absent",
    silently widening a caller's request for the matched line ONLY into the
    full default +/-3 line window.
    """
    from ctx.plan_ops import OPS, PlanContext
    from ctx.store import Store
    from ctx.workspace import resolve_workspace

    ws = resolve_workspace(str(workspace_dir))
    store = Store(ws.workspace_id)
    lines = [f"line{i} = {i}" for i in range(1, 20)]
    (workspace_dir / "m.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    pc = PlanContext(ws=ws, store=store)
    inp = {"kind": "sites", "rows": [{"file": "m.py", "line": 10}]}

    out = OPS["code.context"].fn(pc, {"context": 0}, inp)
    text = out["rows"][0]["text"]
    code_lines = [ln for ln in text.splitlines() if ln.startswith("L")]

    assert code_lines == ["L10: line10 = 10"], text


# =====================================================================
# Finding 52 -- src/ctx/astgrep.py:522
# =====================================================================
@pytest.fixture()
def git_ws_f52(tmp_path, state_home):
    ws = tmp_path / "proj"
    ws.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True, env=env)
    (ws / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    (ws / "m.py").write_text(
        "client = None\n"
        "def go(x):\n"
        "    return old_client.fetch(x)\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=ws, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=ws, check=True, env=env)
    return ws


def _fake_astgrep_binary(tmp_path, monkeypatch, script_body: str):
    from ctx import astgrep

    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    exe = bindir / "ast-grep"
    exe.write_text("#!/usr/bin/env python3\n" + script_body, encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ.get('PATH', '')}")
    astgrep.binary.cache_clear()
    return exe


_FAKE_REWRITE_SCRIPT = """
import json, sys, pathlib
args = sys.argv[1:]
if "--version" in args:
    print("ast-grep 9.9.9-test"); raise SystemExit(0)
data = pathlib.Path("m.py").read_bytes()
needle = b"old_client.fetch(x)"
start = data.find(needle)
print(json.dumps({
    "file": "m.py",
    "range": {"start": {"line": 2, "column": 11},
              "byteOffset": {"start": start, "end": start + len(needle)}},
    "replacement": "new_client.fetch(resource=x)",
}))
"""


def test_unverifiable_guard_state_fails_open_instead_of_refusing(
    git_ws_f52, tmp_path, monkeypatch
):
    """_guard_state returns the literal string "unknown" whenever
    generation_hash fails AND ws.list_files() raises. Two separate calls to
    _guard_state under those conditions both return "unknown", which compares
    EQUAL to itself -- so rewrite_apply's freshness guard
    (`gen_now != expect_generation`) treats "cannot verify freshness" as
    "nothing changed" and proceeds to apply, instead of refusing.
    """
    from ctx import astgrep

    def _always_unknown(ws):
        return "unknown"

    monkeypatch.setattr(astgrep, "_guard_state", _always_unknown)

    _fake_astgrep_binary(tmp_path, monkeypatch, _FAKE_REWRITE_SCRIPT)
    try:
        ws = make_ws(git_ws_f52)
        store = make_store(ws)
        _rows, meta = astgrep.rewrite_preview(
            ws, store, "old_client.fetch($X)", "new_client.fetch(resource=$X)"
        )
        assert meta["generation"] == "unknown"

        with pytest.raises(astgrep.RewriteError):
            astgrep.rewrite_apply(ws, store, meta["patch_blob"], meta["generation"])
    finally:
        astgrep.binary.cache_clear()


# =====================================================================
# Finding 53 -- src/ctx/execution.py:177
# =====================================================================
def test_run_capture_leaks_stdin_spool_fd_when_stdout_spool_open_fails(
    workspace_dir, monkeypatch
):
    """run_capture opens the stdin spool (`in_fh = in_path.open("rb")`,
    execution.py:177) OUTSIDE the `with out_path.open('wb') as out_fh, \\
    err_path.open('wb') as err_fh:` block whose failure this closes. The
    only `in_fh.close()` lives in a `finally` that belongs to an inner
    `try` wrapping the Popen call, which is reached only once that `with`
    block has been entered successfully. So a failure opening the stdout
    (or stderr) spool leaves the already-opened stdin spool file handle
    never closed.
    """
    from ctx.execution import run_capture
    from ctx.workspace import resolve_workspace

    ws = resolve_workspace(str(workspace_dir))

    orig_open = pathlib.Path.open
    state = {"stdin_closed": False}

    class TrackedFile:
        def __init__(self, real):
            self._real = real

        def close(self):
            state["stdin_closed"] = True
            self._real.close()

        def __getattr__(self, name):
            return getattr(self._real, name)

    def _mode_of(args, kwargs):
        return args[0] if args else kwargs.get("mode", "r")

    def tracking_open(self, *args, **kwargs):
        mode = _mode_of(args, kwargs)
        if self.name == "stdin" and mode == "rb":
            return TrackedFile(orig_open(self, *args, **kwargs))
        if self.name == "stdout":
            raise PermissionError("simulated: cannot open stdout spool")
        return orig_open(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "open", tracking_open)

    with pytest.raises(PermissionError):
        run_capture(ws, ["true"], stdin_bytes=b"hello")

    assert state["stdin_closed"], (
        "in_fh (the stdin spool handle) was never closed after the stdout "
        "spool failed to open -- the fd leaked"
    )


# =====================================================================
# Finding 54 -- src/ctx/orchestrator.py:1226
# =====================================================================
def test_checkpoint_node_closes_store_connection_on_create_checkpoint_failure(
    state_home, workspace_dir, monkeypatch
):
    """_checkpoint_node opens a Store with no try/finally: if create_checkpoint
    raises after put_blob already opened the sqlite connection, the connection
    is leaked (never closed) even though the whole call fails open."""
    from ctx.orchestrator import RouteNode, _checkpoint_node
    from ctx import store as store_mod
    import ctx.checkpoint as checkpoint_mod

    ws = make_ws(workspace_dir)
    node = RouteNode(
        id="n1", goal="g", role="worker", min_tier="cheap", need_tags=(), deps=(),
        est_input_tokens=1, est_output_tokens=1,
    )

    created_stores = []
    orig_init = store_mod.Store.__init__

    def spy_init(self, *a, **kw):
        orig_init(self, *a, **kw)
        created_stores.append(self)

    monkeypatch.setattr(store_mod.Store, "__init__", spy_init)

    def boom_create_checkpoint(*a, **kw):
        raise RuntimeError("boom on create_checkpoint")

    monkeypatch.setattr(checkpoint_mod, "create_checkpoint", boom_create_checkpoint)

    result = _checkpoint_node(ws, node, "task", "some stdout output", "", handoff_strategy=None)

    assert result is None  # fail-open contract still holds
    assert len(created_stores) == 1
    leaked_store = created_stores[0]
    assert leaked_store._db is None, (
        "Store's sqlite connection was left open after _checkpoint_node's "
        "fail-open except swallowed the create_checkpoint failure"
    )


# =====================================================================
# Finding 55 -- src/ctx/store.py:166
# =====================================================================
# =====================================================================
# Finding 56 -- src/ctx/reflex.py:298
# =====================================================================
def test_scope_flag_value_with_spaces_breaks_narrower_comparison():
    """_signature_parts joins a scope flag and its value with a space, and
    _split_signature re-splits on whitespace, so a -k value containing
    spaces is parsed back as phantom positional targets, breaking is_narrower."""
    from ctx.reflex import command_signature, is_narrower

    narrowed = "pytest tests/x.py -k 'auth and login'"
    broad = "pytest tests/x.py"

    sig = command_signature(narrowed)
    assert sig == "pytest tests/x.py -k auth and login"

    assert is_narrower(narrowed, broad) is True, (
        "a -k scoped rerun of the same target should be narrower than the "
        "unscoped run, but phantom tokens from the split -k value make the "
        "target sets incomparable"
    )


# =====================================================================
# Finding 57 -- src/ctx/surface_profiles.py:85
# =====================================================================
def test_authority_ok_fails_open_on_unrecognized_ceiling():
    """_authority_ok swallows ValueError from AUTHORITY_ORDER.index(ceiling)
    and returns True (fail OPEN) for any misspelled/unrecognized ceiling,
    even for a destructive capability."""
    from ctx import surface, surface_profiles

    cap = surface.Capability(
        id="c1", kind="mcp_tool", provider="p", source="s", tokens=10,
        authority="destructive",
    )

    assert "read-only" not in surface.AUTHORITY_ORDER

    assert surface_profiles._authority_ok(cap, "read-only") is False, (
        "an unrecognized authority_ceiling must fail CLOSED (reject) for a "
        "destructive capability, not silently allow it through"
    )


# =====================================================================
# Finding 58 -- src/ctx/textutil.py:180
# =====================================================================
def test_json_pointer_raises_pointer_error_not_raw_value_error():
    """json_pointer validates array indices with str.isdigit(), which accepts
    non-ASCII digit characters (e.g. U+00B2 superscript two) that int() then
    rejects, so a raw ValueError escapes instead of JsonPointerError."""
    from ctx import textutil

    assert "²".isdigit() is True  # confirms the root cause precondition

    with pytest.raises(textutil.JsonPointerError):
        textutil.json_pointer([1, 2, 3], "/²")


# =====================================================================
# Finding 59 -- src/ctx/reflex.py:1592
# =====================================================================
def test_fold_q_ledger_does_not_consume_a_partial_trailing_line(tmp_path):
    """_fold_q_ledger must not advance the q_ops cursor past a partially
    written trailing line (a writer caught mid-append); doing so causes the
    event to be skipped forever once the line is completed."""
    from ctx.reflex import _fold_q_ledger

    qd_path = tmp_path / "q-dry.jsonl"
    complete_line = json.dumps({"op": "q_dry_rerun", "pipeline": "foo | bar", "rows": 0})
    partial_line = '{"op":"q_dry_rerun","pipeline":"partial-writ'
    qd_path.write_text(complete_line + "\n" + partial_line, encoding="utf-8")

    state = {"q_dry": {}, "q_ops": 0}
    _fold_q_ledger(state, str(qd_path), None)

    lines = qd_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert state["q_ops"] == 1, (
        f"cursor advanced past an unparseable trailing line "
        f"(q_ops={state['q_ops']!r}); that line will never be folded even "
        f"after the writer completes it"
    )


# =====================================================================
# Finding 60 -- src/ctx/stream_rules.py:69
# =====================================================================
def test_stream_rule_engine_handles_none_valued_persisted_state():
    """StreamRuleEngine's constructor must tolerate a persisted state document
    whose 'fires'/'activated' fields are None (load_state's only validation
    is isinstance(doc, dict)) rather than crashing with AttributeError."""
    from ctx.stream_rules import StreamRule, StreamRuleEngine, STREAM_RULE_SCHEMA

    state = {"schema": STREAM_RULE_SCHEMA, "fires": None, "activated": None}
    rules = [StreamRule(name="r1", pattern="foo", reminder="bar")]

    engine = StreamRuleEngine(rules, prior_state=state)
    assert engine._fires == {"r1": 0}
    assert engine._activated == []


# =====================================================================
# Finding 61 -- src/ctx/route_telemetry.py:133
# =====================================================================
def test_route_summary_kinds_histogram_counts_missing_kind_as_zero(tmp_path):
    """route_summary's 'kinds' histogram builds its key set with an
    'unknown' default (str(...get('kind', 'unknown'))) but counts rows with
    the raw defaultless value (...get('kind') == kind), so a run whose
    task_profile lacks 'kind' always contributes a bucket of 0, not 1."""
    from ctx.sessiondir import session_reads_path

    path = session_reads_path(tmp_path, "route.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "schema": "ctx.route-run/v1",
        "run_id": "route-missing-kind",
        "task_profile": {},
        "measurement": {},
    }
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")

    from ctx.route_telemetry import route_summary

    summary = route_summary(tmp_path)
    assert summary["kinds"] == {"unknown": 1}


# =====================================================================
# Finding 62 -- src/ctx/replay.py:477
# =====================================================================
def test_render_outcomes_hides_requery_when_window_is_censored():
    """render_outcomes divides equivalent_requery by non_censored (obs -
    censored) but never removes censored windows from the NUMERATOR, so a
    detected requery inside a censored (never-closed) window renders as an
    em dash instead of surfacing the count."""
    from ctx.evidence_outcomes import Action, EvidenceEmission, followup_join
    from ctx.replay import render_outcomes
    from ctx import reflex

    em = EvidenceEmission(
        index=0, operator="op:test", signature="pytest",
        test_ids=frozenset({"tests/test_x.py::t"}),
    )
    cmd = "pytest -v 2>&1 | tail -50"
    act = Action(
        index=1,
        kind="retrieval" if reflex.landing_ref(cmd) else "bash",
        command=cmd,
        signature=reflex.command_signature(cmd),
        result_text="",
    )
    (ev,) = followup_join([em], [act], session_complete=False)
    assert ev.censored is True
    assert ev.equivalent_requery is True

    rendered = render_outcomes([ev])
    lines = [ln for ln in rendered.splitlines() if ln.startswith("op:test")]
    assert len(lines) == 1
    # columns: operator, n, exact-use, valid-assoc, requery, censored
    requery_cell = lines[0].split()[4]
    assert requery_cell != "—", (
        f"requery cell rendered as {requery_cell!r} despite equivalent_requery=1: {lines[0]!r}"
    )


# =====================================================================
# Finding 63 -- src/ctx/callgraph.py:820
# =====================================================================
def test_rows_production_partition_is_not_quadratic():
    """_rows partitions caller entries into production/other via `e not in
    prod` where `prod` is a list, making the partition O(n*m). A set-backed
    membership check keeps this near-linear; 6000 entries must resolve well
    under the ~0.2s the list-based scan takes."""
    from ctx.callgraph import _Def, _Graph, _rows

    def build(n):
        g = _Graph()
        entries = []
        for i in range(n):
            nid = f"mod{i}.py::fn{i}"
            rel = f"tests/mod{i}.py" if i % 3 != 0 else f"src/mod{i}.py"
            g.nodes[nid] = _Def(qual=f"fn{i}", rel=rel, lineno=1, end=2)
            entries.append((nid, i, "repo"))
        return g, entries

    g, entries = build(6000)
    t0 = time.perf_counter()
    _rows(g, entries)
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.05, f"_rows took {elapsed:.3f}s for 6000 entries (quadratic scan)"


# =====================================================================
# Finding 64 -- src/ctx/digest/searchprof.py:129
# =====================================================================
def test_search_v1_run_glob_suggestion_never_filters_run_search(state_home, workspace_dir):
    """search/v1 suggests `--glob` against a `run:` reference, but --glob is
    never threaded through the run: branch of retrieval search -- so the
    flag is a silent no-op, and the resulting command reads no differently
    with or without it."""
    from ctx.execution import run_capture
    from ctx.retrieval import search

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    script = (
        "[print(f'src/ctx/digest/mod{i%4}.py:{i+1}:    result = compute(x, {i})') "
        "for i in range(40)]"
    )
    cap = run_capture(ws, [sys.executable, "-c", script], store=store)
    short = cap.manifest_id[:12]
    out = search(store, ws, f"run:{short}", ["compute"], glob="**/*.this-cannot-match-anything")
    assert "matches: 0" in out, (
        "expected --glob to filter run: search results once threaded through; "
        f"got:\n{out}"
    )


# =====================================================================
# Finding 65 -- src/ctx/astgrep.py:356
# =====================================================================
@pytest.fixture()
def git_ws_f65(tmp_path, state_home):
    ws = tmp_path / "proj"
    ws.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True, env=env)
    (ws / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    body = "\n".join(f"def f{i}():\n    return old_client.fetch({i})" for i in range(6))
    (ws / "m.py").write_text(body + "\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=ws, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=ws, check=True, env=env)
    return ws


def test_lib_search_files_previewed_counts_matches_not_files(git_ws_f65, monkeypatch):
    """ast.search's library rung (Finding 65): with ast-grep-py the only
    available engine, a single file with 6 matches must not be reported as
    if it were 6 distinct files in an in-flight rewrite patch -- ast_search
    computes no patch at all."""
    pytest.importorskip("ast_grep_py")
    from ctx import astgrep

    monkeypatch.setenv("PATH", "/nonexistent")
    astgrep.binary.cache_clear()
    astgrep.lib_available.cache_clear()
    ws = make_ws(git_ws_f65)
    store = make_store(ws)
    assert astgrep.binary() is None
    assert astgrep.lib_available() is True

    rows, meta = astgrep.ast_search(ws, store, "old_client.fetch($X)")

    distinct_files = {r["file"] for r in rows}
    assert distinct_files == {"m.py"}
    assert len(rows) == 6  # one row per match, all in the one file

    assert meta["files_previewed"] == len(distinct_files), (
        f"files_previewed ({meta['files_previewed']}) should count files "
        f"({len(distinct_files)}), not match rows ({len(rows)})"
    )


def test_lib_search_omission_note_does_not_claim_a_patch(git_ws_f65, monkeypatch):
    """Finding 65: a pure ast.search call (no rewrite) must never tell the
    user omitted rows 'ARE in the patch' -- ast_search built no patch."""
    pytest.importorskip("ast_grep_py")
    from ctx import astgrep

    monkeypatch.setenv("PATH", "/nonexistent")
    astgrep.binary.cache_clear()
    astgrep.lib_available.cache_clear()
    body = "\n".join(f"def f{i}():\n    return old_client.fetch({i})" for i in range(10))
    (git_ws_f65 / "m.py").write_text(body + "\n", encoding="utf-8")
    ws = make_ws(git_ws_f65)
    store = make_store(ws)

    _rows, meta = astgrep.ast_search(ws, store, "old_client.fetch($X)", cap=3)

    note = meta.get("note", "")
    assert "patch" not in note.lower(), (
        f"a pure search call minted a rewrite-patch claim in its meta note: {note!r}"
    )


# =====================================================================
# Finding 66 -- src/ctx/rundiff.py:70
# =====================================================================
def test_pytest_failures_line_numbers_match_store_line_index(tmp_path, state_home):
    """_pytest_failures mints line numbers with str.splitlines(), which also
    breaks on bare \\r -- but those numbers are later used as store span
    coordinates (register_span / read_blob_lines), which are resolved
    against the \\n-only line index (ctx.textutil.index_lines / Store.line_index).
    A transcript with a stray \\r before a FAILED line gets a span that
    resolves to the wrong line (or nothing)."""
    from ctx.rundiff import _pytest_failures
    from ctx.store import Store
    from ctx.workspace import resolve_workspace

    (tmp_path / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    ws = resolve_workspace(str(tmp_path))
    store = Store(ws.workspace_id)

    text = (
        "progress 10%\rprogress 50%\rprogress 100%\n"
        "FAILED tests/test_x.py::test_thing - AssertionError: boom\n"
    )
    failures = _pytest_failures(text)
    line_no = failures["tests/test_x.py::test_thing"]

    blob = store.put_blob(text.encode("utf-8"))
    resolved = store.read_blob_lines(blob, line_no, line_no).decode("utf-8", "replace")

    assert "FAILED" in resolved, (
        f"_pytest_failures said the FAILED line was L{line_no}, but the "
        f"store's \\n-only line index resolves L{line_no} to {resolved!r}"
    )
