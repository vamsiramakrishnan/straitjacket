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

    subprocess.run(["git", "init", "-q", "."], cwd=ws_root, check=True)
    from ctx.jobs import _job_dir, _write_meta, jobs_root, start_job
    from ctx.store import Store
    from ctx.workspace import resolve_workspace

    ws = resolve_workspace(str(ws_root))
    store = Store(ws.workspace_id)
    job_id = start_job(ws, store, ["sleep", "30"])
    # Park the job in the terminal 'failed' state the supervisor writes when a
    # launch dies (jobs.py: command not found / spawn failed).
    jobdir = _job_dir(store, job_id)
    meta = __import__("json").loads((jobdir / "meta.json").read_text(encoding="utf-8"))
    meta.update(state="failed", error="command not found: ctx-no-such-cmd-xyz", pid=None)
    _write_meta(jobdir, meta)
    assert jobs_root(store).is_dir()

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
