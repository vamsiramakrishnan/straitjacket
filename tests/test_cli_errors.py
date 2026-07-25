"""The CLI's error contract: what a failing `ctx` invocation says, on which
stream, and with which exit code.

`ctx` is invoked from hooks, scripts, and — most of all — by agents, so its
failure surface is a real interface. These tests pin the three things the
audit found unpinned:

  * the most common agent-facing error (a handle that GC or retention has
    already collected) is attributed to the verb and names its own cause;
  * bad input exits with ONE code across the whole verb surface;
  * an inner failure is not reported as a ctx failure.

tests/test_retrieval.py exercises the same exceptions at library level; this
file is the CLI half, which had no coverage at all.
"""

import pytest


@pytest.fixture()
def ws_root(tmp_path, monkeypatch):
    monkeypatch.setenv("CTX_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "proj"
    root.mkdir()
    (root / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    (root / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    return root


def _run(ws_root, *args):
    from ctx.cli import main

    return main(["--workspace", str(ws_root), *args])


# ------------------------------------------------- 1. the collected handle
def test_missing_handle_is_attributed_and_names_gc_and_retention(ws_root, capsys):
    """`ctx get run:<id>` after a `ctx gc` is the error an agent hits most.

    Before: it fell through cli.py's blanket handler as
    `ctx: no object matches id prefix 'abc123def' in this workspace` —
    generic prefix, no cause, no remediation.
    """
    rc = _run(ws_root, "get", "run:abc123def")
    err = capsys.readouterr().err
    assert rc == 2  # ctx rejected the invocation; see the exit-code table below
    assert err.startswith("ctx get: "), err  # attributed to the verb, not bare `ctx:`
    assert "gc" in err and "retention" in err  # the likely cause is named
    assert "ctx pin" in err  # and a remediation for next time


def test_missing_handle_is_attributed_for_every_retrieval_verb(ws_root, capsys):
    for verb, args in (
        ("get", ["get", "run:abc123def"]),
        ("search", ["search", "run:abc123def", "x"]),
        ("stats", ["stats", "run:abc123def"]),
        ("diff", ["diff", "run:abc123def", "run:def456abc"]),
    ):
        _run(ws_root, *args)
        err = capsys.readouterr().err
        assert err.startswith(f"ctx {verb}: "), (verb, err)


def test_unknown_id_error_still_carries_its_prefix(ws_root):
    """The message gained a cause and a remediation; it must not lose the
    thing that identifies WHICH handle failed."""
    from ctx.store import Store, UnknownIdError
    from ctx.workspace import resolve_workspace

    store = Store(resolve_workspace(str(ws_root)).workspace_id)
    with pytest.raises(UnknownIdError, match="abc123def"):
        store.resolve_id("abc123def")


# ------------------------------------------- 2. truncation keeps the handle
def _tiny_budget_ws(root, *, engagement="active"):
    (root / "ctx.toml").write_text(
        "version = 1\n"
        "[budgets]\ndigest_tokens = 60\nresult_tokens = 60\n"
        f'[engagement]\nmode = "{engagement}"\n',
        encoding="utf-8",
    )
    return root


NOISY = "for i in range(400): print('line %d some content here to fill space' % i)"


@pytest.mark.parametrize("engagement", ["active", "passive"])
def test_truncated_run_digest_still_ends_with_a_retrieval_handle(
    ws_root, capsys, engagement
):
    """bounded() cuts from the bottom and `next:` is last in every profile,
    so the clamp deleted the retrieval affordance exactly when it was needed.
    filter_digest compounded it: cap 0 (passive / lean-model sessions, the
    default) dropped the whole block before the clamp ever ran."""
    import sys

    _tiny_budget_ws(ws_root, engagement=engagement)
    rc = _run(ws_root, "run", "--", sys.executable, "-c", NOISY)
    out = capsys.readouterr().out
    assert rc == 0
    assert "[ctx:truncated" in out, out  # precondition: it really was cut
    last = [ln for ln in out.strip().splitlines() if ln.strip()][-1]
    assert last.startswith("next: "), out  # a handle, not a dead end
    assert "ctx " in last  # ...carrying a verb
    assert "run:" in last  # ...and an address


def test_untruncated_digest_gains_no_extra_next_line(ws_root, capsys):
    """The handle is a truncation remedy, not a new affordance: a digest that
    fits must stay byte-identical, and a passive session must still get no
    suggestions."""
    import sys

    (ws_root / "ctx.toml").write_text(
        'version = 1\n[engagement]\nmode = "passive"\n', encoding="utf-8"
    )
    rc = _run(ws_root, "run", "--", sys.executable, "-c", "print('ok')")
    out = capsys.readouterr().out
    assert rc == 0
    assert "[ctx:truncated" not in out
    assert "next:" not in out


def test_truncation_handle_reads_through_the_engagement_filter():
    """The handle is read from the UNFILTERED digest, so cap 0 cannot hide
    it, and it falls back to the first artifact handle when a digest carries
    no `next:` block at all."""
    from ctx.commands.emit import _truncation_handle

    digest = (
        "[ctx run:abc123abc123 profile=text/v1]\nbody\nnext:\n"
        "  ctx get run:abc123abc123#stdout --lines 6:395\n"
        "  ctx search run:abc123abc123 '<pattern>'\n"
    )
    assert _truncation_handle(digest) == "ctx get run:abc123abc123#stdout --lines 6:395"
    assert _truncation_handle("[ctx run:abc123abc123]\nbody\n") == (
        "ctx get run:abc123abc123"
    )
    assert _truncation_handle("no handle anywhere") is None


def test_bounded_truncation_continuation_only_fires_on_a_cut():
    from ctx.textutil import bounded

    assert bounded("short", 100, truncation_continuation="ctx get run:x") == "short"
    cut = bounded("x" * 4000, 10, truncation_continuation="ctx get run:x")
    assert cut.endswith("next: ctx get run:x")


# ------------------------------------------------ 3. one code for bad input
BAD_INPUT = [
    # (argv, verb) — one class of user mistake, formerly split 1 vs 2 purely
    # by which verb family happened to catch it.
    (["get", "repo:a.py", "--lines", "nope"], "get"),
    (["get", "zzz:xyz"], "get"),
    (["get", "run:abc123def"], "get"),
    (["search", "zzz:xyz", "pat"], "search"),
    (["stats", "zzz:xyz"], "stats"),
    (["diff", "repo:a.py", "repo:a.py"], "diff"),
    (["def", "nonsense"], "def"),
    (["def", "repo:a.py:Nope"], "def"),
    (["pin", "zzz:xyz"], "pin"),
]


@pytest.mark.parametrize(
    "argv,verb", BAD_INPUT, ids=[" ".join(a) for a, _ in BAD_INPUT]
)
def test_bad_input_exits_2_and_is_attributed(ws_root, capsys, argv, verb):
    rc = _run(ws_root, *argv)
    err = capsys.readouterr().err
    assert err.startswith(f"ctx {verb}: "), (argv, err)
    assert rc == 2, (argv, rc, err)


def test_bad_input_agrees_across_the_two_verb_families(ws_root, capsys):
    """`ctx q` and `ctx plan` (and argparse) already said 2; the retrieval
    family said 1 for the same class of mistake, so a calling script got
    opposite signals depending on which verb it reached for."""
    assert _run(ws_root, "q", "nosuchstage") == 2
    capsys.readouterr()
    assert _run(ws_root, "get", "repo:a.py", "--lines", "nope") == 2
    capsys.readouterr()


# --------------------------------- 4. a failed job is not a ctx failure
def test_failed_job_exits_3_not_1(ws_root, capsys, monkeypatch):
    """`ctx job <id>` on a job that failed returned 1 — the same code as the
    JobError handler on the very next line, so a caller could not tell a
    failed job from a bad job id. run/py/seq already reserve 1 for ctx's own
    failure and report an inner failure as 3."""
    import subprocess
    import time

    subprocess.run(["git", "init", "-q", "."], cwd=ws_root, check=True)
    from ctx.jobs import _job_dir, _read_meta, jobs_root, start_job
    from ctx.store import Store
    from ctx.workspace import resolve_workspace

    ws = resolve_workspace(str(ws_root))
    store = Store(ws.workspace_id)
    # Let the job fail for real. Starting a live `sleep` and hand-writing
    # state="failed" into meta.json races the supervisor, which owns that file
    # and rewrites it — CI lost that race and read back "running".
    job_id = start_job(ws, store, ["ctx-no-such-cmd-xyz"])
    jobdir = _job_dir(store, job_id)
    assert jobs_root(store).is_dir()

    deadline = time.monotonic() + 30
    while _read_meta(jobdir).get("state") not in ("failed", "done", "finalized"):
        assert time.monotonic() < deadline, f"job never reached a terminal state: {_read_meta(jobdir)}"
        time.sleep(0.05)
    assert _read_meta(jobdir)["state"] == "failed", _read_meta(jobdir)

    rc = _run(ws_root, "job", job_id)
    out = capsys.readouterr().out
    assert "failed" in out
    assert rc == 3, out


def test_unknown_job_id_still_reports_a_ctx_failure(ws_root, capsys):
    """The other half of the pair: 3 only means the job failed, so an id ctx
    cannot resolve must not also be 3."""
    import subprocess

    subprocess.run(["git", "init", "-q", "."], cwd=ws_root, check=True)
    rc = _run(ws_root, "job", "0123456789ab")
    assert rc != 3
    assert "ctx job:" in capsys.readouterr().err


# ------------------------------------------- 5. the contract is documented
def _cli_doc() -> str:
    import pathlib

    return (
        pathlib.Path(__file__).resolve().parent.parent / "docs" / "CLI.md"
    ).read_text(encoding="utf-8")


def test_every_exit_code_the_cli_uses_is_documented():
    """0/1/2/3/124/127 are all used deliberately and `ctx` is called from
    hooks and scripts, but docs/CLI.md never mentioned exit codes at all."""
    doc = _cli_doc()
    assert "## Exit codes" in doc
    for code in ("`0`", "`1`", "`2`", "`3`", "`124`", "`127`"):
        assert code in doc, f"exit code {code} is used but undocumented"


def test_exit_code_doc_draws_the_line_that_matters():
    """1 vs 2 vs 3 is the distinction a calling script acts on; a table that
    lists the numbers without it would not be worth writing."""
    doc = _cli_doc().lower()
    assert "gc" in doc and "retention" in doc  # why a well-formed handle can 2
    assert "timed out" in doc and "127" in doc
    assert "stderr" in doc and "stdout" in doc


# ---------------------------------- 6. the blanket handler, and its hatch
def _boom(exc):
    def handler(ws, ns):
        raise exc

    return handler


def test_unhandled_error_names_the_command_and_the_exception_type(
    ws_root, capsys, monkeypatch
):
    """A KeyError escaping a handler used to render as `ctx: 'focus'` —
    unactionable, and unattributable to any command."""
    from ctx import cli

    monkeypatch.setattr(cli, "_handler_for", lambda cmd: (_boom(KeyError("focus")), True))
    rc = _run(ws_root, "diag")
    err = capsys.readouterr().err
    assert rc == 1  # ctx itself failed
    assert "diag" in err  # attributable to a command
    assert "KeyError" in err  # and to a kind of failure
    assert "focus" in err  # without losing what str(e) said
    assert "CTX_DEBUG" in err  # and it says how to get more


def test_ctx_debug_promotes_the_message_to_a_traceback(ws_root, capsys, monkeypatch):
    from ctx import cli

    monkeypatch.setattr(cli, "_handler_for", lambda cmd: (_boom(KeyError("focus")), True))
    monkeypatch.setenv(cli.DEBUG_ENV, "1")
    assert _run(ws_root, "diag") == 1
    err = capsys.readouterr().err
    assert "Traceback (most recent call last)" in err
    assert "KeyError" in err
    assert "CTX_DEBUG" not in err  # already on: no point advertising it


def test_cli_and_mcp_share_one_error_prefix():
    """The same handler said `ctx:` on the CLI and `ctx error:` over MCP."""
    from ctx.cli import format_error
    from ctx.mcp import _tool_call

    res = _tool_call({"name": "ctx", "arguments": {"op": "no-such-op"}})
    text = res["content"][0]["text"]
    assert res["isError"] is True
    assert text.startswith("ctx "), text
    assert "ctx error:" not in text
    assert "RetrievalError" in text
    # ...and MCP spends no context on a hint the model cannot act on.
    assert "CTX_DEBUG" not in text
    assert format_error(None, ValueError("x")).startswith("ctx: ValueError: x")


def test_ctx_debug_is_documented():
    assert "CTX_DEBUG" in _cli_doc()


# --------------------- 7. "the harness did nothing" blames the right thing
def test_gain_with_no_telemetry_goes_to_stderr_and_points_at_doctor(ws_root, capsys):
    """It printed to stdout (every other error goes to stderr) and told the
    user to "run some commands under the harness first" — assuming the
    harness works, when the usual cause is that nothing is hooked."""
    rc = _run(ws_root, "gain")
    cap = capsys.readouterr()
    assert rc == 1
    assert cap.out == ""  # not stdout
    assert "ctx doctor" in cap.err  # the check that answers this question
    assert "wrap" in cap.err  # ...and the fix
    assert "hooked" in cap.err  # ...naming the real cause


def test_statusline_distinguishes_off_from_idle(tmp_path):
    """`_harness_saved` returning None omitted the segment, so "ctx is off"
    and "ctx is on and idle" rendered identically — the one failure the
    status line exists to surface was the one it could not show."""
    from ctx import statusline

    unhooked = tmp_path / "cold"
    unhooked.mkdir()
    hooked = tmp_path / "warm"
    (hooked / ".claude").mkdir(parents=True)
    (hooked / ".claude" / "settings.json").write_text(
        '{"hooks": {"PreToolUse": [{"hooks": [{"command": '
        '"/usr/bin/ctx hook claude-code pre-tool-use"}]}]}}',
        encoding="utf-8",
    )
    payload = {"model": {"display_name": "gemini-3-pro"}}
    off = statusline.render("antigravity", payload, workspace_root=unhooked)
    idle = statusline.render("antigravity", payload, workspace_root=hooked)
    assert "ctx◇ off" in off
    assert "ctx◇ idle" in idle
    assert off != idle


def test_statusline_still_prefers_the_number_and_never_raises(tmp_path):
    from ctx import statusline

    root = tmp_path / "busy"
    (root / ".ctx-session-reads" / "proxy").mkdir(parents=True)
    (root / ".ctx-session-reads" / "proxy" / "window.json").write_text(
        '{"contained_tokens": 12000}', encoding="utf-8"
    )
    line = statusline.render(
        "antigravity", {"model": {"display_name": "x"}}, workspace_root=root
    )
    assert "ctx◇ 12K kept out" in line
    # No workspace to speak about ⇒ still no claim either way.
    assert "ctx◇" not in statusline.render(
        "antigravity", {"model": {"display_name": "x"}}
    )
    assert statusline._harness_segment(object()) is None  # fail-open


# --------------------------- 8. the message names the file it actually wrote
def test_append_ledger_names_the_file_it_wrote(ws_root, capsys, tmp_path):
    """It wrote evidence-followups.jsonl and printed evidence-outcomes.jsonl,
    so a user who followed the message found nothing there."""
    from ctx.cli import main

    transcript = tmp_path / "t.jsonl"
    transcript.write_text("", encoding="utf-8")
    rc = main([
        "--workspace", str(ws_root),
        "replay", "--outcomes", "--append-ledger", str(transcript),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    ledger = ws_root / ".ctx-session-reads" / "evidence-followups.jsonl"
    assert ledger.is_file()  # this is the file that exists...
    assert str(ledger) in out  # ...and this is the file the message names
    assert "evidence-outcomes.jsonl" not in out


# ------------------------------ 9. bounded output, in the bounding tool
def test_ambiguous_id_message_is_capped(ws_root):
    """Every candidate is 64 hex characters and the list was uncapped, in a
    message an agent reads — an unbounded flood emitted by the flood guard."""
    from ctx.store import MAX_AMBIGUOUS_CANDIDATES, AmbiguousIdError

    candidates = [f"abc{i:061d}" for i in range(200)]
    e = AmbiguousIdError("abc", candidates)
    msg = str(e)
    named = [c for c in candidates if c in msg]
    assert len(named) == MAX_AMBIGUOUS_CANDIDATES
    assert len(msg) < 1000  # bounded, not proportional to the catalog
    assert "200 candidates" in msg  # the true count is still stated
    assert f"and {200 - MAX_AMBIGUOUS_CANDIDATES} more" in msg
    assert "use a longer prefix" in msg  # the remediation survives the cap
    assert e.candidates == candidates  # callers still see everything


def test_ambiguous_id_short_list_is_unchanged_in_substance(ws_root):
    from ctx.store import AmbiguousIdError

    msg = str(AmbiguousIdError("ab", ["a" * 64, "b" * 64]))
    assert "a" * 64 in msg and "b" * 64 in msg
    assert "more" not in msg


# --------------------------------- 10. one home for each rendered quantity
def test_statusline_uses_the_shared_token_formatter():
    """statusline had a private third rendering of the same quantity."""
    from ctx import statusline
    from ctx.textutil import fmt_tokens_compact

    assert statusline._fmt_tokens is fmt_tokens_compact
    assert fmt_tokens_compact(2_104) == "2K"
    assert fmt_tokens_compact(1_500_000) == "1.5M"


def test_gain_renders_bytes_with_the_shared_byte_formatter(ws_root, capsys):
    """`ctx gain` hand-rolled `{n:,} bytes` while every digest header used
    fmt_bytes."""
    from ctx.retrieval import record_telemetry
    from ctx.store import Store
    from ctx.workspace import resolve_workspace

    ws = resolve_workspace(str(ws_root))
    record_telemetry(Store(ws.workspace_id), "run", 1_500_000, 2_000)
    assert _run(ws_root, "gain") == 0
    out = capsys.readouterr().out
    assert "1.4 MiB" in out
    assert "bytes raw" not in out
    assert "1,500,000" not in out


def test_coarse_docstring_no_longer_overclaims():
    """`est 4,072 tokens` in the digest header IS deliberate — a measurement
    of an exact byte count next to the `15.9 KiB` it reconciles with, inside
    a content-addressed digest. The docstring, not the digest, was wrong."""
    from ctx.textutil import fmt_tokens_coarse

    doc = fmt_tokens_coarse.__doc__ or ""
    assert "forecast" in doc.lower()
    assert "fmt_tokens_compact" in doc
    assert "estimate_tokens" in doc  # names the exact-measurement counterpart
    # ...and the forecast rendering itself is unchanged.
    assert fmt_tokens_coarse(8_432) == "~8k"
